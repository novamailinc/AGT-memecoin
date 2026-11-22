"""Shadow-comparison harness (Phase 2).

Runs Alchemy + Chainstack calls ALONGSIDE the live Helius provider
on identical inputs, records per-provider outputs, and reports
agreement rates. NOTHING downstream consumes a shadow answer: the
runner returns the LIVE result only; shadow results are recorded to
a JSONL journal and summarised per workload. A shadow answer that
disagrees with the on-chain record is a failure, not a tie (see
ground_truth.py helpers below and probes.py).

Shadow order (cheapest proof first):
  1. background/intelligence   2. exit pricing   3. safety screening
  4. curve stats + creator     5. canonical pool + migration price
  6. detection mint resolution + armed-watch WSS
  7. bundle deep scan (never Helius — no removal decision applies)

Removal criterion per workload (audit file B §11): ALL must hold
before that workload leaves Helius —
  - the workload's sample size reached (see WORKLOADS),
  - agreement rate at/above the workload's threshold,
  - zero provider-attributable 429s in the window (throughput 429s
    count; monthly-quota 429s fail the workload outright),
  - latency within the workload's budget (e.g. safety screening
    inside the 8s budget; seen→decided p50/p95 not worse than the
    17–26s baseline for curve stats).

Journal: <home>/acquisition_shadow.jsonl (one line per comparison),
summary: <home>/acquisition_shadow_summary.json (atomic replace).
No key material is ever written to either file.
"""

from __future__ import annotations

import json
import logging
import os
import time

logger = logging.getLogger("forex_agent.daemon.intelligence.acquisition.shadow")

# workload -> {sample_size, agreement_threshold, latency_budget_s,
#              price_tolerance (relative, when prices are compared)}
WORKLOADS = {
    "background": {"sample_size": 50, "agreement": 1.0,
                   "latency_budget_s": None},
    "exit_pricing": {"sample_size": 100, "agreement": 0.99,
                     "latency_budget_s": None, "price_tolerance": 0.02},
    "safety": {"sample_size": 100, "agreement": 1.0,
               "latency_budget_s": 8.0},
    "curve_stats": {"sample_size": 100, "agreement": 1.0,
                    "latency_budget_s": None},
    "canonical_pool": {"sample_size": 30, "agreement": 1.0,
                       "latency_budget_s": None, "price_tolerance": 0.02},
    "detection": {"sample_size": 100, "agreement": 1.0,
                  "latency_budget_s": None},
    "bundle": {"sample_size": 30, "agreement": 1.0,
               "latency_budget_s": 45.0},
}
SHADOW_PROVIDERS = ("alchemy", "chainstack")
JOURNAL_FILE = "acquisition_shadow.jsonl"
SUMMARY_FILE = "acquisition_shadow_summary.json"


def canonical(value):
    """Canonical form for comparison: sorted JSON, numbers rounded
    to 9 significant digits (float noise is not disagreement)."""
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(v) for v in value]
    return value


def results_agree(live, shadow, price_tolerance=None):
    """Compare a live result with a shadow result.

    Exact agreement on canonical form, except: when both results
    are numbers (prices) and a tolerance is given, agreement is
    |a-b| / max(|a|,|b|,tiny) <= tolerance. A missing/failed shadow
    result (kind != ok) is a DISAGREEMENT, not a skip — the shadow
    provider failing to answer is evidence, not absence of evidence.
    """
    if live is None or shadow is None:
        return False
    if live.get("kind") != "ok" or shadow.get("kind") != "ok":
        return False
    a, b = live.get("result"), shadow.get("result")
    if price_tolerance is not None and isinstance(a, (int, float)) \
            and isinstance(b, (int, float)):
        denom = max(abs(a), abs(b), 1e-12)
        return abs(a - b) / denom <= price_tolerance
    return canonical(a) == canonical(b)


def check_ground_truth(shadow_result, ground_truth, fields):
    """On-chain ground-truth validation: ``ground_truth`` is the
    independently retrieved on-chain record (fields: mint, creation
    signature/slot/time, creator, curve account, reserves, buy/sell
    direction and amounts, migration venue, pool address). Returns
    (agree, mismatched_fields). A shadow answer disagreeing with
    the on-chain record is a FAILURE, not a tie."""
    mismatched = []
    result = (shadow_result or {}).get("result")
    if not isinstance(result, dict):
        return False, list(fields)
    for field in fields:
        expected = ground_truth.get(field)
        got = result.get(field)
        if canonical(got) != canonical(expected):
            mismatched.append(field)
    return (not mismatched), mismatched


class ShadowRunner:
    """Runs one logical call on live + shadow providers and records
    the comparison. ``live_call`` and ``shadow_calls`` are callables
    (method, params) -> RpcResult, injected so the harness works
    with the real ProviderManager or with fakes in tests."""

    def __init__(self, home=None, live_call=None, shadow_calls=None):
        self.home = home or os.environ.get("FOREX_AGENT_HOME") \
            or os.path.expanduser("~/.forex-agent")
        self.live_call = live_call
        self.shadow_calls = shadow_calls or {}
        self._journal_path = os.path.join(self.home, JOURNAL_FILE)
        self._summary_path = os.path.join(self.home, SUMMARY_FILE)
        self._summary = self._load_summary()

    def _load_summary(self):
        try:
            with open(self._summary_path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_summary(self):
        tmp = "%s.tmp.%d" % (self._summary_path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(self._summary, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._summary_path)

    def _record(self, workload, provider, method, live, shadow):
        entry = self._summary.setdefault(
            workload, {}).setdefault(provider, {
                "comparisons": 0, "agreements": 0,
                "disagreements": 0, "shadow_errors": 0,
                "provider_429s": 0, "latencies_s": []})
        entry["comparisons"] += 1
        tol = WORKLOADS.get(workload, {}).get("price_tolerance")
        if shadow.get("kind") == "rate_limited":
            entry["provider_429s"] += 1
            if shadow.get("rate_limit_kind") == "monthly_quota":
                entry["monthly_quota_429"] = True
        if shadow.get("kind") != "ok":
            entry["shadow_errors"] += 1
        if results_agree(live, shadow, price_tolerance=tol):
            entry["agreements"] += 1
        else:
            entry["disagreements"] += 1
        lat = shadow.get("elapsed")
        if lat is not None:
            entry["latencies_s"].append(round(lat, 3))
            if len(entry["latencies_s"]) > 1000:
                entry["latencies_s"] = entry["latencies_s"][-1000:]
        self._save_summary()
        line = {"ts": time.time(), "workload": workload,
                "provider": provider, "method": method,
                "live_kind": live.get("kind"),
                "shadow_kind": shadow.get("kind"),
                "agree": results_agree(live, shadow, price_tolerance=tol),
                "live_elapsed_s": live.get("elapsed"),
                "shadow_elapsed_s": shadow.get("elapsed")}
        try:
            with open(self._journal_path, "a") as fh:
                fh.write(json.dumps(line) + "\n")
        except OSError:
            logger.warning("could not append shadow journal")

    def compare(self, workload, method, params):
        """Run the call on live + each shadow provider. Returns the
        LIVE result only — shadow answers are recorded, never
        returned to a caller. Returns (live_result, per_provider)."""
        if self.live_call is None:
            raise ValueError("ShadowRunner needs a live_call")
        live = self.live_call(method, params)
        per_provider = {}
        for provider, fn in self.shadow_calls.items():
            try:
                shadow = fn(method, params)
            except Exception:
                shadow = {"result": None, "kind": "error",
                          "elapsed": 0.0, "provider": provider}
            per_provider[provider] = shadow
            self._record(workload, provider, method, live, shadow)
        return live, per_provider

    def summary(self, workload=None):
        if workload:
            return self._summary.get(workload, {})
        return self._summary

    def agreement_rate(self, workload, provider):
        entry = self._summary.get(workload, {}).get(provider)
        if not entry or not entry["comparisons"]:
            return None
        return entry["agreements"] / entry["comparisons"]

    def removal_ready(self, workload, provider):
        """Removal criterion for one workload on one provider. ALL
        must hold: sample size reached, agreement at threshold, zero
        provider-attributable 429s in the window, no monthly-quota
        429, latency within budget (when the workload has one)."""
        spec = WORKLOADS.get(workload)
        if spec is None:
            return False, "unknown workload %r" % workload
        entry = self._summary.get(workload, {}).get(provider)
        if not entry:
            return False, "no shadow comparisons yet"
        if entry["comparisons"] < spec["sample_size"]:
            return False, "sample %d/%d" % (entry["comparisons"],
                                            spec["sample_size"])
        rate = entry["agreements"] / entry["comparisons"]
        if rate < spec["agreement"]:
            return False, "agreement %.3f < %.3f" % (rate,
                                                     spec["agreement"])
        if entry.get("monthly_quota_429"):
            return False, "monthly-quota 429 observed"
        if entry["provider_429s"] > 0:
            return False, "%d provider 429s in window" % entry["provider_429s"]
        budget = spec.get("latency_budget_s")
        if budget is not None and entry["latencies_s"]:
            ordered = sorted(entry["latencies_s"])
            p95 = ordered[min(len(ordered) - 1,
                              int(0.95 * (len(ordered) - 1)))]
            if p95 > budget:
                return False, "p95 latency %.2fs > budget %.2fs" % (p95, budget)
        return True, ("removal criterion met (%d comparisons, "
                     "agreement %.3f)" % (entry["comparisons"], rate))
