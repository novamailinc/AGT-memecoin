"""Usage dashboard / report (Phase 9, spec §12).

Generates the usage report from the usage store + request journal
on demand — one row per provider x workload:

    Provider | Workload | Requests | Usage | Latency | Errors | Cost estimate

Rules (no invented numbers anywhere):

- Counts (requests, errors, cached) are read EXACTLY from the
  journal (``acquisition_provider_usage.jsonl``); the report
  reproduces them, it does not re-derive them from counters.
- Latency comes ONLY from measured router timings (``elapsed_s``
  on journal lines). Mocked-transport numbers are labelled as such
  via the ``provenance`` argument — never presented as live.
- Cost estimate uses ``METHOD_COSTS`` only, in the provider's own
  unit (Helius credits / Alchemy CU / Chainstack RU). Lines with a
  recorded cost use it; non-cached lines with no recorded cost are
  estimated from METHOD_COSTS and flagged ``cost_estimated``; cached
  lines cost 0 and their avoided cost is reported as ``saved_cost``.
  Lines whose method cost is UNVERIFIED on that provider are counted
  as ``usage_unknown`` — never estimated at 1.
- The report states its measurement window (month, first/last
  journal timestamp, journal line count) and its provenance.

Entry points:
- ``build_report(home, month, provenance)`` -> dict (JSON-able).
- ``format_text(report)`` -> the table as plain text.
- CLI: ``python -m daemon.intelligence.acquisition.usage_report
  [--home HOME] [--month YYYY-MM] [--provenance TEXT] [--json]``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

from . import provider_capabilities as caps
from .cost_management import (
    COST_UNIT,
    CostManager,
    cost_unit,
    method_cost,
)
from .provider_usage import JOURNAL_FILE, UsageStore
from .usage_tracker import UsageTracker

DEFAULT_PROVENANCE = (
    "unlabelled — the journal carries no provenance; treat these "
    "numbers as fixture/mocked-transport unless the run that seeded "
    "them was live. Never present as live measurements.")


def _month_key(ts=None):
    return time.strftime("%Y-%m", time.gmtime(ts or time.time()))


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _latency_stats(elapsed_s):
    """Latency (ms) from measured router timings only. Returns
    {mean_ms, p50_ms, p95_ms, samples} or Nones when no samples."""
    if not elapsed_s:
        return {"mean_ms": None, "p50_ms": None, "p95_ms": None,
                "samples": 0}
    ms = [e * 1000.0 for e in elapsed_s]
    return {
        "mean_ms": round(statistics.fmean(ms), 2),
        "p50_ms": round(_percentile(ms, 50), 2),
        "p95_ms": round(_percentile(ms, 95), 2),
        "samples": len(ms),
    }


def read_journal(home, month=None):
    """Journal lines for ``month`` (YYYY-MM; default current month),
    in file order. Lines from other months and malformed lines are
    skipped (malformed count returned for the report notes)."""
    month = month or _month_key()
    path = os.path.join(home, JOURNAL_FILE)
    entries = []
    malformed = 0
    try:
        with open(path) as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    malformed += 1
                    continue
                if _month_key(entry.get("ts", 0)) != month:
                    continue
                entries.append(entry)
    except OSError:
        pass
    return entries, malformed


def build_report(home=None, month=None, provenance=None, store=None,
                 cost_manager=None):
    """Build the usage report dict.

    ``home`` defaults to FOREX_AGENT_HOME / ~/.forex-agent.
    ``month`` defaults to the current month (YYYY-MM).
    ``provenance`` labels where the journal numbers came from
    (e.g. "mocked-transport fixture run 2026-11-22" or "live");
    when omitted the report says so explicitly.
    """
    home = home or os.environ.get("FOREX_AGENT_HOME") \
        or os.path.expanduser("~/.forex-agent")
    month = month or _month_key()
    provenance = provenance or DEFAULT_PROVENANCE
    cm = cost_manager or CostManager(home=home)
    entries, malformed = read_journal(home, month)

    # -- group by provider x workload ----------------------------------
    groups = {}
    for entry in entries:
        key = (entry.get("provider"), entry.get("workload"))
        group = groups.setdefault(key, {
            "provider": key[0], "workload": key[1],
            "requests": 0, "ok": 0, "errors": 0,
            "rate_limited": 0, "timeouts": 0,
            "cached_requests": 0, "elapsed_s": [],
            "usage": 0, "usage_estimated": False,
            "usage_unknown_calls": 0, "saved_cost": 0,
            "saved_unknown": 0, "accounts": set(),
        })
        group["requests"] += 1
        group["accounts"].add(entry.get("account"))
        kind = entry.get("kind")
        if kind == "ok":
            group["ok"] += 1
        elif kind == "rate_limited":
            group["rate_limited"] += 1
            group["errors"] += 1
        elif kind == "timeout":
            group["timeouts"] += 1
            group["errors"] += 1
        else:
            group["errors"] += 1
        if entry.get("elapsed_s") is not None:
            group["elapsed_s"].append(entry["elapsed_s"])
        cost = entry.get("cost")
        if entry.get("cached"):
            group["cached_requests"] += 1
            # Cached: spent 0; avoided cost is the METHOD_COSTS cost.
            avoided = method_cost(entry.get("provider"),
                                  entry.get("method"))
            if avoided is None:
                group["saved_unknown"] += 1
            else:
                group["saved_cost"] += avoided
        elif cost is not None:
            group["usage"] += cost
        else:
            # No recorded cost: estimate from METHOD_COSTS, flagged.
            est = method_cost(entry.get("provider"),
                              entry.get("method"))
            if est is None:
                group["usage_unknown_calls"] += 1
            else:
                group["usage"] += est
                group["usage_estimated"] = True

    rows = []
    for (provider, workload), group in sorted(
            groups.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        lat = _latency_stats(group["elapsed_s"])
        unit = cost_unit(provider)
        rows.append({
            "provider": provider,
            "workload": workload,
            "accounts": sorted(a for a in group["accounts"] if a),
            "requests": group["requests"],
            "ok": group["ok"],
            "errors": group["errors"],
            "rate_limited": group["rate_limited"],
            "timeouts": group["timeouts"],
            "cached_requests": group["cached_requests"],
            "usage": group["usage"],
            "usage_unit": unit,
            # "Cost estimate" column: METHOD_COSTS-based, in the
            # provider's own unit; never summed across providers.
            "cost_estimate": group["usage"],
            "cost_estimate_unit": unit,
            "cost_basis": "METHOD_COSTS",
            "cost_estimated": group["usage_estimated"],
            "usage_unknown_calls": group["usage_unknown_calls"],
            "saved_cost": group["saved_cost"],
            "saved_cost_unit": unit,
            "saved_unknown": group["saved_unknown"],
            "latency_mean_ms": lat["mean_ms"],
            "latency_p50_ms": lat["p50_ms"],
            "latency_p95_ms": lat["p95_ms"],
            "latency_samples": lat["samples"],
            "latency_source": "router elapsed_s from journal "
                              "(measured timings only)",
        })

    timestamps = [e["ts"] for e in entries
                  if isinstance(e.get("ts"), (int, float))]
    budgets = {}
    try:
        budgets = cm.tracker.summary()
    except Exception:  # store unreadable: report budgets as unknown
        budgets = {a: {"budget_enforced": False, "budget_pct": None}
                   for a in caps.ACCOUNTS}
    savings = cm.total_saved()
    ws = cm.accounting().get("ws", {})

    notes = []
    if malformed:
        notes.append("%d malformed journal line(s) skipped" % malformed)
    if not entries:
        notes.append("no journal lines for %s — report is empty, "
                     "not zero usage" % month)
    unmeasured_ws = sum(
        r.get("bytes_unmeasured_events", 0) for r in ws.values())
    if unmeasured_ws:
        notes.append("%d WebSocket event(s) recorded with bytes "
                     "UNMEASURED (transport did not report bytes); "
                     "no bandwidth figure invented for them"
                     % unmeasured_ws)
    notes.append("Helius budgets are PROVISIONAL (plan UNVERIFIED); "
                 "Alchemy/Chainstack live verification deferred to AGT.")

    return {
        "generated_ts": time.time(),
        "measurement_window": {
            "month": month,
            "journal_lines": len(entries),
            "first_ts": min(timestamps) if timestamps else None,
            "last_ts": max(timestamps) if timestamps else None,
            "journal_path": os.path.join(home, JOURNAL_FILE),
        },
        "provenance": provenance,
        "rows": rows,
        "budgets": budgets,
        "alerts": cm.alerts(),
        "savings": savings,
        "ws_bytes": {
            a: {"events": r.get("events", 0),
                "bytes_measured": r.get("bytes_measured", 0),
                "bytes_unmeasured_events": r.get(
                    "bytes_unmeasured_events", 0)}
            for a, r in ws.items()},
        "notes": notes,
    }


def format_text(report):
    """The report as a plain-text table."""
    window = report["measurement_window"]
    lines = [
        "Provider usage report — %s" % window["month"],
        "Window: %d journal line(s)%s" % (
            window["journal_lines"],
            ("; first %s, last %s"
             % (time.strftime("%Y-%m-%d %H:%M:%S",
                              time.gmtime(window["first_ts"])),
                time.strftime("%Y-%m-%d %H:%M:%S",
                              time.gmtime(window["last_ts"]))))
            if window["first_ts"] is not None else ""),
        "Provenance: %s" % report["provenance"],
        "",
        "%-11s %-28s %8s %14s %12s %7s %14s" % (
            "Provider", "Workload", "Requests", "Usage",
            "Latency p50", "Errors", "Cost estimate"),
    ]
    for row in report["rows"]:
        usage = ("%s %s" % (row["usage"], row["usage_unit"] or "")
                 if row["usage_unit"] else str(row["usage"]))
        if row["cost_estimated"]:
            usage += " (est.)"
        if row["usage_unknown_calls"]:
            usage += " +%d unknown" % row["usage_unknown_calls"]
        latency = ("%.1f ms" % row["latency_p50_ms"]
                   if row["latency_p50_ms"] is not None else "-")
        cost = ("%s %s" % (row["cost_estimate"],
                           row["cost_estimate_unit"] or "")
                if row["cost_estimate_unit"]
                else str(row["cost_estimate"]))
        lines.append("%-11s %-28s %8d %14s %12s %7d %14s" % (
            str(row["provider"]), str(row["workload"])[:28],
            row["requests"], usage, latency, row["errors"], cost))
    lines.append("")
    for alert in report["alerts"]:
        lines.append("ALERT %s: %s%% of budget%s%s" % (
            alert["account"],
            round(100 * alert["pct"], 1) if alert["pct"] is not None
            else "?",
            " — STOP nonessential" if alert["stop_nonessential"] else
            (" — WARN" if alert["warn"] else ""),
            " — DISABLED (%s)" % alert["disabled_reason"]
            if alert["disabled"] else ""))
    for note in report["notes"]:
        lines.append("NOTE: %s" % note)
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Provider usage report (Phase 9, §12).")
    parser.add_argument("--home", default=None,
                        help="FOREX_AGENT_HOME (default: env or "
                             "~/.forex-agent)")
    parser.add_argument("--month", default=None,
                        help="month YYYY-MM (default: current month)")
    parser.add_argument("--provenance", default=None,
                        help="label for where the journal numbers came "
                             "from (mocked-transport fixture vs live)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of the text table")
    args = parser.parse_args(argv)
    report = build_report(home=args.home, month=args.month,
                          provenance=args.provenance)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
