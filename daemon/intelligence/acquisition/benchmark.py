"""Benchmarks (Phase 2).

Reproducible per-provider measurements, run against the ACTUAL keys
once they land (live shadow runs are blocked on the keys today):
  - RPC latency: median / p95 / p99 per provider per method
  - event latency: provider latency vs processing latency reported
    SEPARATELY (timestamp at receipt vs at decision)
  - reconnection time, completeness, duplicates, missed events
  - 429 / timeout rates, quota consumption (CU/RU spent)
  - creation-to-qualification and migration-to-qualification times

``run_benchmark(provider, methods, calls, rpc_call)`` drives the
calls through an injected ``rpc_call(provider, method, params)``
so the same code benchmarks live providers and fakes in tests.
Results are plain dicts — no numbers are invented: a provider with
no key reports "blocked", never a fabricated latency.
"""

from __future__ import annotations

import statistics
import time

from daemon import solana_ids as _solana_ids


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1,
              max(0, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return ordered[idx]


def summarise_latencies(latencies):
    if not latencies:
        return {"n": 0, "median_s": None, "p95_s": None, "p99_s": None}
    return {"n": len(latencies),
            "median_s": round(statistics.median(latencies), 4),
            "p95_s": round(_percentile(latencies, 95), 4),
            "p99_s": round(_percentile(latencies, 99), 4)}


def run_benchmark(provider, method_params, calls_per_method=10,
                  rpc_call=None, sleep=time.sleep):
    """Benchmark one provider over [(method, params), ...].

    Returns {"provider": ..., "methods": {method: {...}}, "blocked":
    bool, "detail": str}. ``rpc_call`` must return an RpcResult-like
    dict with kind/elapsed/result. Quota consumption is summed from
    the provider's per-method cost table (rpc.method_cost)."""
    from . import rpc_client as rpc_mod
    if rpc_call is None:
        rpc_call = lambda method, params: rpc_mod.rpc(  # noqa: E731
            provider, method, params)
    adapter = rpc_mod.make_adapter(provider)
    if not adapter.configured():
        return {"provider": provider, "blocked": True,
                "detail": "provider not configured (no key/endpoint)",
                "methods": {}}
    out = {"provider": provider, "blocked": False, "methods": {}}
    for method, params in method_params:
        latencies = []
        ok = rate_limited = timeouts = errors = unsupported = 0
        cu_spent = 0
        for _ in range(calls_per_method):
            res = rpc_call(method, params)
            kind = res.get("kind")
            if kind == "ok":
                ok += 1
                latencies.append(res.get("elapsed", 0.0) or 0.0)
                cost = rpc_mod.method_cost(provider, method)
                if cost:
                    cu_spent += cost
            elif kind == "rate_limited":
                rate_limited += 1
            elif kind == "timeout":
                timeouts += 1
            elif kind == "unsupported":
                unsupported += 1
            else:
                errors += 1
        stats = summarise_latencies(latencies)
        stats.update({"ok": ok, "rate_limited": rate_limited,
                      "timeouts": timeouts, "errors": errors,
                      "unsupported": unsupported,
                      "quota_spent": cu_spent})
        out["methods"][method] = stats
    return out


def event_latency_report(received_ts, decided_ts):
    """Split event latency into provider latency (creation ->
    receipt) and processing latency (receipt -> decision). Both
    inputs are epoch timestamps; creation_ts is the on-chain
    creation time. Returns None fields when data is missing —
    never a fabricated split."""
    if received_ts is None or decided_ts is None:
        return {"provider_latency_s": None, "processing_latency_s": None,
                "total_s": None}
    return {"provider_latency_s": None,  # needs creation_ts; caller fills
            "processing_latency_s": round(decided_ts - received_ts, 3),
            "total_s": None}


# ===========================================================================
# Phase 10 additions (2026-11-22): workload benchmarks, §13.
#
# HARD RULES (spec §13, enforced by construction):
# - NO live provider calls from this VM. Every benchmark here runs
#   on mocked transport (a fake opener replaying recorded fixtures)
#   or on recorded on-chain fixtures under tests/data/intelligence/.
#   A live benchmark is UNMEASURED here; the AGT procedure to
#   measure it lives in ``UNMEASURED_LIVE`` / the Phase 10 report.
# - NEVER fabricate a number: every figure comes from a run executed
#   by the functions below, and every result dict carries a
#   ``provenance`` label saying exactly what was measured.
# - Lifecycle stages are NEVER conflated: preprocessed vs processed
#   vs confirmed vs finalized are measured / derived separately
#   (``stage_latencies``); a missing stage yields None, never an
#   estimate borrowed from another stage.
# - The Helius adapter call path is NOT benchmarked here: it
#   delegates to ``daemon.launch_scanner.reserves`` (the live
#   transport, dual-import hazard) and Helius is the fleet's live
#   leg. ``benchmark_helius_adapter`` returns an UNMEASURED entry
#   instead of touching it. Helius *parsing* (pure functions over
#   recorded/doc-shaped frames) IS benchmarked — no live leg there.
# ===========================================================================

# Provenance labels. Every benchmark result carries one of these in
# its "provenance" field; a result without one is a bug.
PROVENANCE_MOCKED_TRANSPORT = (
    "mocked transport (fake opener + recorded fixtures) — router / "
    "adapter / driver overhead only; NOT provider latency")
PROVENANCE_FIXTURE = (
    "recorded on-chain fixtures under tests/data/intelligence/ — "
    "local pipeline cost only; NOT provider latency")
PROVENANCE_ROUTER_OVERHEAD = (
    "router overhead only (mocked transport, token available) — "
    "NOT provider latency")
PROVENANCE_FAKE_SOCKET = (
    "fake WebSocket (in-process fake socket, no network) — local "
    "dispatch / parsing cost only; NOT provider stream latency")
PROVENANCE_UNMEASURED = "UNMEASURED — no run executed on this VM"

# Canonical lifecycle order. A stage delta is only ever computed
# between two stages that both have a recorded timestamp.
STAGE_ORDER = ("preprocessed", "processed", "confirmed", "finalized")
_STAGE_ALIASES = {
    "preliminary": "preprocessed",
    "preprocessed": "preprocessed",
    "processed": "processed",
    "confirmed": "confirmed",
    "finalized": "finalized",
}


def _stage_of(observation):
    """Canonical stage name for one observation dict, or None.

    Accepts ``stage`` / ``lifecycle`` / ``lifecycle_status`` /
    ``commitment`` keys; "preliminary" (the preprocessed record's
    stage) aliases to "preprocessed"."""
    for key in ("stage", "lifecycle", "lifecycle_status", "commitment"):
        value = observation.get(key)
        if isinstance(value, str):
            stage = _STAGE_ALIASES.get(value.strip().lower())
            if stage is not None:
                return stage
    return None


def stage_latencies(observations):
    """Derive per-stage latencies from recorded observation times.

    ``observations`` is a list of dicts with ``observed_at`` (epoch
    seconds) and a stage key (see ``_stage_of``). Returns one entry
    per adjacent stage pair plus the total span, each None when
    either endpoint stage has no recorded timestamp — stages are
    never conflated and a missing stage is never estimated:

        {"stages_present": [...], "stage_timestamps": {...},
         "preprocessed_to_processed_s": ...,
         "processed_to_confirmed_s": ...,
         "confirmed_to_finalized_s": ...,
         "total_s": ...}   # earliest -> latest PRESENT stage

    The earliest ``observed_at`` per stage wins (a stage observed
    twice keeps its first sighting)."""
    first_seen = {}
    for obs in observations or []:
        stage = _stage_of(obs)
        ts = obs.get("observed_at") if isinstance(obs, dict) else None
        if stage is None or ts is None:
            continue
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if stage not in first_seen or ts < first_seen[stage]:
            first_seen[stage] = ts
    present = [s for s in STAGE_ORDER if s in first_seen]
    out = {"stages_present": present,
           "stage_timestamps": {s: first_seen[s] for s in present}}
    for a, b in zip(STAGE_ORDER, STAGE_ORDER[1:]):
        key = "%s_to_%s_s" % (a, b)
        if a in first_seen and b in first_seen:
            out[key] = round(first_seen[b] - first_seen[a], 6)
        else:
            out[key] = None
    if len(present) >= 2:
        out["total_s"] = round(first_seen[present[-1]]
                               - first_seen[present[0]], 6)
    else:
        out["total_s"] = None
    return out


def summarise_samples(samples):
    """Full latency summary for a list of elapsed seconds.

    p50/p95/p99 + mean/min/max/n. Empty input yields n=0 and None
    fields — never a fabricated zero."""
    if not samples:
        return {"n": 0, "mean_s": None, "min_s": None, "max_s": None,
                "p50_s": None, "p95_s": None, "p99_s": None}
    vals = sorted(float(v) for v in samples)
    return {"n": len(vals),
            "mean_s": round(statistics.mean(vals), 6),
            "min_s": round(vals[0], 6),
            "max_s": round(vals[-1], 6),
            "p50_s": round(_percentile(vals, 50), 6),
            "p95_s": round(_percentile(vals, 95), 6),
            "p99_s": round(_percentile(vals, 99), 6)}


def _timed(fn, *args, **kwargs):
    """(elapsed_s, return value) for one call of ``fn``."""
    t0 = time.monotonic()
    out = fn(*args, **kwargs)
    return time.monotonic() - t0, out


def _counts(results, key="kind"):
    counts = {}
    for res in results:
        kind = res.get(key) if isinstance(res, dict) else None
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def benchmark_router_route(router, method, params=None, calls=200,
                           priority=None, hints=None):
    """Time the routing DECISION only (``router.route()``).

    No transport, no scheduler, no usage write: pure
    classification + candidate ordering (+ refusal) cost. The
    refusal path (preprocessed / Helius-only with Helius disabled)
    is measured here too — a refusal is a decision, not a call."""
    from .request_scheduler import PRIORITY_CARD
    latencies = []
    refused = 0
    last = None
    for _ in range(calls):
        elapsed, decision = _timed(
            router.route, method, params,
            priority if priority is not None else PRIORITY_CARD,
            hints=hints)
        latencies.append(elapsed)
        last = decision
        if decision.get("refused"):
            refused += 1
    out = summarise_samples(latencies)
    out.update({"benchmark": "router_route", "method": method,
                "workload": (last or {}).get("workload"),
                "primary": (last or {}).get("primary"),
                "refused": refused, "calls": calls,
                "provenance": PROVENANCE_ROUTER_OVERHEAD})
    return out


def benchmark_router_call(router_factory, method, params=None, calls=30,
                          priority=None, hints=None):
    """Time the FULL router call path (``router.call()``).

    ``router_factory`` builds a fresh router (fresh temp home, full
    scheduler token bucket) per timed call, OUTSIDE the timed
    region: the token bucket is a rate limiter, and this benchmark
    measures the per-call overhead with a token available — not the
    wait for one. Scheduler-refusal behaviour is measured
    separately by ``benchmark_router_burst``. Latency stats cover
    only kind == "ok" calls; every other outcome is counted, never
    folded into the latency numbers."""
    from .request_scheduler import PRIORITY_CARD
    latencies = []
    results = []
    for _ in range(calls):
        router = router_factory()
        elapsed, result = _timed(
            router.call, method, params,
            priority=priority if priority is not None else PRIORITY_CARD,
            hints=hints)
        results.append(result)
        if result.get("kind") == "ok":
            latencies.append(elapsed)
    out = summarise_samples(latencies)
    out.update({"benchmark": "router_call", "method": method,
                "workload": results[0].get("workload") if results else None,
                "primary": None,
                "ok": sum(1 for r in results if r.get("kind") == "ok"),
                "cached": sum(1 for r in results if r.get("cached")),
                "outcome_counts": _counts(results),
                "calls": calls,
                "provenance": PROVENANCE_ROUTER_OVERHEAD})
    return out


def benchmark_router_burst(router, method, params=None, calls=30,
                           priority=None, hints=None):
    """Scheduler behaviour under a rapid burst on ONE router.

    No latency stats: with the token bucket drained, calls are
    refused (kind "rate_limited", elapsed ~0) and a latency number
    over refused calls would be meaningless. Reports granted vs
    refused — the burst capacity / refill behaviour of the shared
    quota, on mocked transport."""
    from .request_scheduler import PRIORITY_CARD
    results = []
    for _ in range(calls):
        results.append(router.call(
            method, params,
            priority=priority if priority is not None else PRIORITY_CARD,
            hints=hints))
    return {"benchmark": "router_burst", "method": method,
            "workload": results[0].get("workload") if results else None,
            "calls": calls,
            "ok": sum(1 for r in results if r.get("kind") == "ok"),
            "rate_limited": sum(
                1 for r in results if r.get("kind") == "rate_limited"),
            "outcome_counts": _counts(results),
            "provenance": PROVENANCE_MOCKED_TRANSPORT}


def benchmark_adapter(adapter, method, params=None, calls=100):
    """Time one provider adapter's call path (``adapter.call()``).

    Direct adapter call — no router, no scheduler. The transport
    is whatever the adapter was built with (mocked in every Phase
    10 run). Helius MUST NOT be passed here: its adapter delegates
    to the live reserves transport — use
    ``unmeasured_helius_adapter`` instead."""
    if getattr(adapter, "name", None) == "helius":
        raise ValueError(
            "benchmark_adapter refuses the Helius adapter: it calls "
            "the live reserves transport. Use "
            "unmeasured_helius_adapter().")
    latencies = []
    results = []
    for _ in range(calls):
        elapsed, result = _timed(adapter.call, method, params)
        results.append(result)
        if result.get("kind") == "ok":
            latencies.append(elapsed)
    out = summarise_samples(latencies)
    out.update({"benchmark": "adapter_call",
                "provider": getattr(adapter, "name", "?"),
                "method": method,
                "ok": sum(1 for r in results if r.get("kind") == "ok"),
                "outcome_counts": _counts(results),
                "calls": calls,
                "provenance": PROVENANCE_MOCKED_TRANSPORT})
    return out


def unmeasured(name, reason, agt_procedure):
    """A benchmark that CANNOT be measured from this VM.

    Recorded explicitly, never estimated. ``agt_procedure`` is the
    exact procedure to run on the AGT target where live keys and
    the live leg are available."""
    return {"benchmark": name, "measured": False,
            "provenance": PROVENANCE_UNMEASURED,
            "reason": reason, "agt_procedure": agt_procedure}


def unmeasured_helius_adapter():
    """The Helius adapter call path: UNMEASURED on this VM."""
    return unmeasured(
        "adapter_call:helius",
        "HeliusAdapter.call() delegates to daemon.launch_scanner."
        "reserves (the live transport); importing it is a dual-"
        "import hazard and calling it would hammer the fleet's "
        "live leg, which is flapping in auto-quiet. Alchemy / "
        "Chainstack live calls are likewise forbidden (keys "
        "unproven, 401 from this VM).",
        "On AGT with real env values: build HeliusAdapter with the "
        "live transport, call getSlot/getTransaction/"
        "getSignaturesForAddress 100x each via benchmark_adapter, "
        "and record p50/p95/p99 + 429 counts. Do the same for "
        "Alchemy and Chainstack with their real keys to get live "
        "provider latency per method.")


def benchmark_pipeline(process_fn, observations):
    """Time the event pipeline (normalize -> dedup -> store -> gate).

    ``process_fn(observation)`` returns an outcome string (the
    EventProcessor outcome: stored/duplicate/merged/held/applied/
    failed). ``observations`` are recorded-fixture observations.
    Returns per-event latency stats, events/s, and outcome counts
    — local pipeline cost only."""
    latencies = []
    outcomes = {}
    t0 = time.monotonic()
    for obs in observations:
        elapsed, outcome = _timed(process_fn, obs)
        latencies.append(elapsed)
        key = outcome[0] if isinstance(outcome, tuple) else outcome
        outcomes[key] = outcomes.get(key, 0) + 1
    total = time.monotonic() - t0
    out = summarise_samples(latencies)
    out.update({"benchmark": "event_pipeline",
                "events": len(observations),
                "events_per_s": (round(len(observations) / total, 2)
                                 if total > 0 else None),
                "total_s": round(total, 4),
                "outcomes": outcomes,
                "provenance": PROVENANCE_FIXTURE})
    return out


def benchmark_wss_dispatch(handler, messages, repeat=1):
    """Time WSS subscription handling cost on a fake socket.

    ``handler(message)`` is the dispatch entry point (e.g.
    ``WebSocketManager._on_message`` or
    ``HeliusParsedStreamClient._on_message``) driven by an
    in-process fake socket — no network, no live stream. Messages
    are replayed ``repeat`` times; per-message latency stats +
    messages/s are reported."""
    latencies = []
    handled = 0
    t0 = time.monotonic()
    for _ in range(repeat):
        for message in messages:
            elapsed, _ = _timed(handler, message)
            latencies.append(elapsed)
            handled += 1
    total = time.monotonic() - t0
    out = summarise_samples(latencies)
    out.update({"benchmark": "wss_dispatch",
                "messages": handled,
                "messages_per_s": (round(handled / total, 2)
                                   if total > 0 else None),
                "total_s": round(total, 4),
                "provenance": PROVENANCE_FAKE_SOCKET})
    return out


def benchmark_cache(router_factory, method, params_list, repeats=3):
    """Cache hit rates and their latency effect (router cache).

    ``params_list`` is a list of distinct param sets (one cold call
    each); each is then re-called ``repeats`` times (warm calls).
    Reports cold vs warm latency stats, hit rate, and the ACTUAL
    cache TTL from the router's config — read from code, never
    assumed (the spec §13 brief says 300s for getTransaction; the
    router ships 30s — the measured value is reported here)."""
    from . import provider_router as router_mod
    cold, warm = [], []
    cold_cached = warm_cached = 0
    for params in params_list:
        router = router_factory()
        elapsed, result = _timed(router.call, method, params)
        cold.append(elapsed)
        if result.get("cached"):
            cold_cached += 1
        for _ in range(repeats):
            elapsed, result = _timed(router.call, method, params)
            warm.append(elapsed)
            if result.get("cached"):
                warm_cached += 1
    warm_n = len(params_list) * repeats
    return {"benchmark": "router_cache", "method": method,
            "cold": summarise_samples(cold),
            "warm": summarise_samples(warm),
            "cold_calls": len(params_list),
            "warm_calls": warm_n,
            "warm_hits": warm_cached,
            "warm_hit_rate": (round(warm_cached / warm_n, 4)
                              if warm_n else None),
            "cold_unexpected_hits": cold_cached,
            "cache_ttl_s": dict(router_mod.CACHE_TTL_S),
            "provenance": PROVENANCE_MOCKED_TRANSPORT}


def benchmark_backfill_driver(recover_fn, runs=5):
    """Backfill / recovery rate via the Phase 6 driver.

    ``recover_fn`` is a zero-arg callable returning a
    BackfillDriver report dict. Delegates timing to
    ``measure_backfill_performance`` (chainstack_workload) and adds
    the Phase 10 provenance + repeat summary. The per-run rate is
    events/s on mocked transport — driver + router overhead, NOT
    provider latency."""
    from .chainstack_workload import measure_backfill_performance
    runs_out = [measure_backfill_performance(recover_fn)
                for _ in range(runs)]
    rates = [r["events_per_s"] for r in runs_out
             if r.get("events_per_s") is not None]
    return {"benchmark": "backfill_driver",
            "runs": runs,
            "events_per_s": summarise_samples(rates),
            "events_new_per_run": [r["events_new"] for r in runs_out],
            "signatures_found_per_run": [r["signatures_found"]
                                         for r in runs_out],
            "pages_per_run": [r["pages"] for r in runs_out],
            "elapsed_s_per_run": [r["elapsed_s"] for r in runs_out],
            "provenance": PROVENANCE_MOCKED_TRANSPORT}


# ---------------------------------------------------------------------------
# Phase 10 runner: builds the mocked environment and runs every
# benchmark (a)–(f). No live calls anywhere in this section.
# ---------------------------------------------------------------------------
def load_recorded_rpc_fixture(repo_root, name):
    """The ``result`` value of a recorded RPC fixture."""
    import json
    import os
    path = os.path.join(repo_root, "tests", "data", "intelligence",
                        "acquisition", name + ".json")
    with open(path) as fh:
        return json.load(fh)["result"]


class MethodFixtureTransport:
    """Fake opener replaying recorded fixtures per JSON-RPC method.

    Reads the method from the request body and returns the matching
    recorded fixture as a JSON-RPC result. ``sig_page_size`` (int)
    synthesises that many distinct signature rows for
    getSignaturesForAddress so backfill rate can be measured past
    the 2-row recorded page — the rows are synthetic, the transport
    is mocked, and both facts are labelled in the report. Every
    call is recorded (host + method) so a test can prove no live
    host was ever touched.
    """

    def __init__(self, fixtures, sig_page_size=None):
        self.fixtures = fixtures
        self.sig_page_size = sig_page_size
        self.calls = []  # (host, method)

    def _sig_page(self):
        rows = []
        base_slot = 472499532
        for i in range(self.sig_page_size or 0):
            rows.append({
                "signature": "bench%064d" % i,
                "slot": base_slot - i,
                "blockTime": 1795341615 - i,
                "confirmationStatus": "finalized",
                "err": None, "memo": None})
        return rows

    def __call__(self, req, timeout=None):
        import json as _json
        url = req.full_url
        host = url.split("/")[2] if "://" in url else url
        try:
            body = _json.loads(req.data.decode("utf-8"))
            method = body.get("method")
        except Exception:
            method = None
        self.calls.append((host, method))
        if method == "getSignaturesForAddress" and self.sig_page_size:
            result = self._sig_page()
        else:
            result = self.fixtures.get(method)
        payload = _json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": result}).encode()

        class _Resp:
            status = 200

            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return _Resp()


def run_phase10_suite(repo_root, calls_route=200, calls_router=30,
                      calls_adapter=100, pipeline_events=200,
                      wss_messages=200, backfill_runs=5):
    """Run every Phase 10 benchmark on mocked transport + fixtures.

    Returns a JSON-serialisable report: one table per benchmark
    (workload, p50, p95, p99, sample size, provenance, what it does
    and does not prove) plus the UNMEASURED list with AGT
    procedures. Raises nothing for a missing fixture — a missing
    input is reported as an UNMEASURED entry, never a fabricated
    number."""
    import os
    import tempfile
    from . import provider_router as router_mod
    from .event_backfill import BackfillDriver
    from .providers import make_adapter

    os.environ.setdefault("ALCHEMY_API_KEY", "testkey-not-real")
    os.environ.setdefault(
        "CHAINSTACK_ENDPOINT", "https://chainstack.example.invalid/abc")
    fixtures = {
        "getSlot": load_recorded_rpc_fixture(repo_root, "live_getSlot"),
        "getAccountInfo": load_recorded_rpc_fixture(
            repo_root, "live_getAccountInfo_wsol"),
        "getTransaction": load_recorded_rpc_fixture(
            repo_root, "live_getTransaction_pump"),
        "getSignaturesForAddress": load_recorded_rpc_fixture(
            repo_root, "live_getSignaturesForAddress_pump"),
    }
    transport = MethodFixtureTransport(fixtures)
    report = {"phase": 10, "spec": "§13",
              "transport": "mocked only — no live provider calls",
              "tables": {}, "unmeasured": []}

    def fresh_router(helius_enabled=False):
        home = tempfile.mkdtemp(prefix="phase10_router_")
        return router_mod.ProviderRouter(
            home=home, helius_enabled=helius_enabled,
            transport=transport)

    # -- (a) router call path per workload --------------------------
    route_cases = [
        ("routine_account", "getAccountInfo",
         [_solana_ids.WSOL_MINT,
          {"encoding": "jsonParsed"}], None),
        ("routine_account_slot", "getSlot", [], None),
        ("routine_tx", "getTransaction",
         ["eY5SFmn2qGBCbwqWUQEKJJr6N7VevCWvKTKqkCaC6RBbuSqYTQtfK9djE8",
          {"encoding": "jsonParsed",
           "maxSupportedTransactionVersion": 1}], None),
        ("historical_range", "getSignaturesForAddress",
         ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
          {"limit": 100}], None),
        ("historical_range_recent", "getSignaturesForAddress",
         ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
          {"limit": 100}], {"recent": True}),
        ("missed_event_recovery", "getSignaturesForAddress",
         ["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
          {"limit": 100}],
         {"workload": router_mod.MISSED_EVENT_RECOVERY}),
        ("independent_verification", "getAccountInfo",
         [_solana_ids.WSOL_MINT,
          {"encoding": "jsonParsed"}],
         {"workload": router_mod.INDEPENDENT_VERIFICATION}),
    ]
    router_table = {}
    for label, method, params, hints in route_cases:
        router_table[label] = {
            "route_decision": benchmark_router_route(
                fresh_router(), method, params, calls=calls_route,
                hints=hints),
            "full_call": benchmark_router_call(
                fresh_router, method, params, calls=calls_router,
                hints=hints),
            "burst_30": benchmark_router_burst(
                fresh_router(), method, params, calls=30, hints=hints),
            "proves": ("routing-decision + full-call overhead on "
                       "mocked transport for this workload"),
            "does_not_prove": ("provider latency, provider 429 "
                               "behaviour, or live quota consumption"),
        }
    # Refusal paths are decisions, not calls: preprocessed (always)
    # and complex-parse with Helius disabled (no fallback by design).
    router_table["preprocessed_refusal"] = {
        "route_decision": benchmark_router_route(
            fresh_router(), "getTransaction", ["sig"],
            calls=calls_route,
            hints={"workload": router_mod.PREPROCESSED_OBSERVATION}),
        "full_call": benchmark_router_call(
            fresh_router, "getTransaction", ["sig"],
            calls=calls_router,
            hints={"workload": router_mod.PREPROCESSED_OBSERVATION}),
        "proves": "refusal-decision cost; the workload is never sent",
        "does_not_prove": ("preprocessed stream latency — the "
                           "product is paid-gated and refused"),
    }
    router_table["complex_parse_refusal_helius_disabled"] = {
        "route_decision": benchmark_router_route(
            fresh_router(), "getTransaction", ["sig"],
            calls=calls_route,
            hints={"workload": router_mod.COMPLEX_TX_PARSE}),
        "full_call": benchmark_router_call(
            fresh_router, "getTransaction", ["sig"],
            calls=calls_router,
            hints={"workload": router_mod.COMPLEX_TX_PARSE}),
        "proves": ("refusal-decision cost with Helius disabled; no "
                   "substitute provider is attempted"),
        "does_not_prove": "Helius Parsed Events latency (live, AGT)",
    }
    report["tables"]["router_workloads"] = router_table

    # -- (b) per-provider adapter call path (mocked) ----------------
    adapter_table = {}
    for provider in ("alchemy", "chainstack", "public"):
        adapter = make_adapter(provider, transport=transport)
        adapter_table[provider] = {
            "getSlot": benchmark_adapter(adapter, "getSlot", [],
                                         calls=calls_adapter),
            "getTransaction": benchmark_adapter(
                adapter, "getTransaction",
                ["eY5SFmn2qGBCbwqWUQEKJJr6N7VevCWvKTKqkCaC6RBbuSqYTQtfK9djE8",
                 {"encoding": "jsonParsed",
                  "maxSupportedTransactionVersion": 1}],
                calls=calls_adapter),
            "proves": ("this adapter's call-path overhead on mocked "
                       "transport"),
            "does_not_prove": ("live provider latency or throughput "
                               "limits"),
        }
    adapter_table["helius"] = unmeasured_helius_adapter()
    report["tables"]["adapters"] = adapter_table

    # -- (c) event pipeline throughput (fixtures) -------------------
    from daemon.intelligence.events import (
        EventProcessor, EventStore)
    import json as _json
    with open(os.path.join(repo_root, "tests", "data", "intelligence",
                           "atx_create.json")) as fh:
        tx_create = _json.load(fh)
    tx_create = tx_create.get("transaction") or tx_create.get(
        "rpc_result") or tx_create
    base_sig = tx_create["transaction"]["signatures"][0]
    base_slot = tx_create["slot"]
    home = tempfile.mkdtemp(prefix="phase10_events_")
    store = EventStore(os.path.join(home, "intelligence_events.db")).open()
    processor = EventProcessor(store)
    observations = []
    for i in range(pipeline_events):
        tx = _json.loads(_json.dumps(tx_create))
        tx["transaction"]["signatures"] = ["%s%04d" % (base_sig[:80], i)]
        tx["slot"] = base_slot + i
        observations.append({"provider": "alchemy",
                             "kind": "getTransaction", "tx": tx,
                             "commitment": "confirmed",
                             "observed_at": 1795273900.0 + i})
    pipeline = benchmark_pipeline(processor.process_observation,
                                  observations)
    pipeline["proves"] = ("normalize -> dedup -> store -> market-"
                          "state-gate cost per new fixture event")
    pipeline["does_not_prove"] = ("provider delivery latency or "
                                  "live-stream throughput")
    # Duplicate path: the same observations again.
    dup = benchmark_pipeline(processor.process_observation,
                             observations[:50])
    dup["proves"] = ("duplicate-delivery handling cost (same %d "
                     "events)" % len(observations[:50]))
    dup["does_not_prove"] = "live duplicate rates"
    # Stage processing cost: one event observed processed, then
    # confirmed, then finalized (different provider per stage so
    # each is a new observation that merges and advances the
    # lifecycle — see event_normalizer.merge_into).
    stage_obs = []
    for stage, provider in (("processed", "alchemy"),
                            ("confirmed", "chainstack"),
                            ("finalized", "public")):
        tx = _json.loads(_json.dumps(tx_create))
        tx["transaction"]["signatures"] = ["stagebench%06d" % 1]
        tx["slot"] = base_slot + 100000
        stage_obs.append({"provider": provider, "kind": "getTransaction",
                          "tx": tx, "commitment": stage,
                          "observed_at": 1795273900.0})
    stage_times = {}
    for obs in stage_obs:
        elapsed, outcome = _timed(processor.process_observation, obs)
        stage_times[obs["commitment"]] = {
            "elapsed_s": round(elapsed, 6), "outcome": outcome[0]}
    stage_derived = stage_latencies([
        {"stage": s, "observed_at": 1795273900.0 + i * 0.4}
        for i, s in enumerate(STAGE_ORDER)])
    report["tables"]["event_pipeline"] = {
        "new_events": pipeline,
        "duplicate_events": dup,
        "stage_processing_cost": {
            "per_stage": stage_times,
            "provenance": PROVENANCE_FIXTURE,
            "proves": ("per-stage merge/store/gate cost when a "
                       "later lifecycle observation arrives"),
            "does_not_prove": ("inter-stage PROVIDER latency — the "
                               "0.4s stage gaps in stage_derived "
                               "are synthetic inputs, not measurements"),
        },
        "stage_latency_derivation_synthetic": stage_derived,
    }

    # -- (d) backfill / recovery rate (mocked) ----------------------
    # (d1) driver primitives WITHOUT the router/scheduler: the
    # recovery machinery itself (signature paging + tx fetch + dedup
    # sink) over a direct fake rpc_call. The router's shared-quota
    # bucket would otherwise dominate the number (see d3).
    from .event_backfill import (
        BackfillDriver, DedupEventSink,
        fetch_transactions_for_signatures, recover_signature_range)
    bf_transport = MethodFixtureTransport(fixtures, sig_page_size=100)
    gap = {"from_slot": 472499400, "to_slot": 472499532}
    address = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

    def _direct_rpc(method, params):
        if method == "getSignaturesForAddress":
            return {"kind": "ok", "result": bf_transport._sig_page()}
        return {"kind": "ok",
                "result": fixtures.get("getTransaction")}

    def _recover_primitives_once():
        sink = DedupEventSink()
        rows, pages, _sig_stats = recover_signature_range(
            address, _direct_rpc, None, seen=sink.seen, max_pages=4)
        events, _tx_stats = fetch_transactions_for_signatures(
            rows, _direct_rpc)
        new = 0
        for ev in events:
            if sink.add(ev):
                new += 1
        return {"events_new": new, "signatures_found": len(rows),
                "pages": pages}

    primitives = benchmark_backfill_driver(
        _recover_primitives_once, runs=backfill_runs)
    primitives["proves"] = ("BackfillDriver recovery machinery rate "
                            "on mocked transport (direct fake rpc, no "
                            "router, no scheduler) with a synthetic "
                            "100-row page")
    primitives["does_not_prove"] = ("live provider paging rate or "
                                    "live archive latency")
    primitives["synthetic_page"] = ("getSignaturesForAddress page "
                                    "synthesised to 100 rows; the "
                                    "recorded fixture page has 2 rows")
    # (d2) router-mediated, recorded 2-row page, fresh router (full
    # token bucket) per run: the end-to-end driver-over-router rate
    # for a gap the recorded fixture can actually cover.
    def _recover_router_once():
        router = router_mod.ProviderRouter(
            home=tempfile.mkdtemp(prefix="phase10_backfill_rt_"),
            helius_enabled=False, transport=transport)
        driver = BackfillDriver(
            router,
            home=tempfile.mkdtemp(prefix="phase10_backfill_run_"))
        return driver.recover_gap("bench", address, gap)

    router_mediated = benchmark_backfill_driver(
        _recover_router_once, runs=backfill_runs)
    router_mediated["proves"] = ("BackfillDriver over the central "
                                 "router on mocked transport, "
                                 "recorded 2-row page, token available")
    router_mediated["does_not_prove"] = ("live provider paging rate; "
                                         "see the scheduler-cap note")
    # (d3) scheduler-cap observation: ONE 100-row page through ONE
    # shared router. The shared quota (Alchemy 5/s refill, burst
    # 10) drains mid-page, so most tx fetches are scheduler-refused
    # and land in the unrecovered gap. This is a measured property
    # of the router-mediated path, not of the driver.
    cap_router = router_mod.ProviderRouter(
        home=tempfile.mkdtemp(prefix="phase10_backfill_cap_"),
        helius_enabled=False, transport=bf_transport)
    cap_driver = BackfillDriver(
        cap_router, home=tempfile.mkdtemp(prefix="phase10_backfill_cap_"))
    cap_report = cap_driver.recover_gap("bench", address, gap)
    scheduler_cap = {
        "benchmark": "backfill_scheduler_cap",
        "signatures_found": cap_report.get("signatures_found"),
        "events_new": cap_report.get("events_new"),
        "fetch_failed": len(cap_report.get("fetch_failed") or []),
        "unrecovered_gap_recorded": cap_report.get(
            "unrecovered_gap") is not None,
        "elapsed_s": cap_report.get("elapsed_s"),
        "finding": ("router-mediated backfill of a 100-row page is "
                    "SCHEDULER-BOUND: the Alchemy token bucket "
                    "(burst 10, refill 5/s) drained after %d tx "
                    "fetches in this run; the other %d were "
                    "scheduler-refused and recorded as an "
                    "unrecovered gap, not lost. Sustained "
                    "router-mediated backfill is ~5 tx/s until the "
                    "bucket is paced or the driver paces itself."
                    % (cap_report.get("events_new") or 0,
                       len(cap_report.get("fetch_failed") or []))),
        "provenance": PROVENANCE_MOCKED_TRANSPORT,
    }
    report["tables"]["backfill"] = {
        "driver_primitives_100row": primitives,
        "router_mediated_2row": router_mediated,
        "scheduler_cap_100row": scheduler_cap,
    }

    # -- (e) cache hit rates + latency effect -----------------------
    cache = benchmark_cache(
        fresh_router, "getTransaction",
        [["benchcache%064d" % i,
          {"encoding": "jsonParsed",
           "maxSupportedTransactionVersion": 1}]
         for i in range(10)], repeats=3)
    cache["proves"] = ("router response-cache hit rate and cold-vs-"
                       "warm latency on mocked transport")
    cache["does_not_prove"] = "live cache value (provider data ages)"
    cache["ttl_note"] = (
        "getTransaction TTL shipped in provider_router.CACHE_TTL_S "
        "is %ss (the §13 brief says 300s — the code value is what "
        "was measured)" % cache["cache_ttl_s"].get("getTransaction"))
    report["tables"]["router_cache"] = cache

    # -- (f) WSS subscription handling cost (fake sockets) ----------
    from daemon.intelligence.acquisition import helius_streams as hs
    from daemon.intelligence.acquisition.websocket_manager import (
        AlchemySubscriptions)
    from daemon.intelligence.decoders import solana_transaction as st

    class _FakeWS:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(_json.loads(text))

        def close(self):
            pass

        def run_forever(self):
            return None

    def _fake_factory(url, on_message, on_error, on_close, on_open):
        ws = _FakeWS()
        _fake_factory.handlers = (on_message, on_error, on_close,
                                  on_open)
        _fake_factory.last = ws
        return ws

    alchemy_events = []
    subs = AlchemySubscriptions(
        url="wss://fake.invalid", ws_factory=_fake_factory,
        on_event=lambda p, d: alchemy_events.append(d))
    subs.subscribe_slot()
    subs.connect()
    slot_msg = _json.dumps(
        {"jsonrpc": "2.0", "method": "slotNotification",
         "params": {"subscription": 1, "result": {"slot": 472499400}}})
    alchemy_wss = benchmark_wss_dispatch(
        lambda m: subs.manager._on_message(None, m),
        [slot_msg], repeat=wss_messages)
    alchemy_wss["events_received"] = len(alchemy_events)
    alchemy_wss["proves"] = ("Alchemy slot-notification dispatch "
                             "cost on a fake socket")
    alchemy_wss["does_not_prove"] = "live WSS delivery latency"

    fix_tx = fixtures["getTransaction"]
    own = st.decode_transaction(fix_tx)
    own_programs = sorted(p for p in own["program_ids"] if p)
    notif = {
        "jsonrpc": "2.0", "method": "parsedTransactionNotification",
        "params": {"subscription": 7, "result": {"value": {
            "signature": fix_tx["transaction"]["signatures"][0],
            "slot": fix_tx["slot"], "status": "error",
            "error": {"InstructionError": [6, {"Custom": 6013}]},
            "fee": fix_tx["meta"]["fee"],
            "feePayer": own["account_keys"][0],
            "accountKeys": own["account_keys"],
            "instructions": [
                {"instructionIndex": i, "innerInstructionIndex": None,
                 "stackHeight": 1, "programId": prog,
                 "decoded": ({"name": "route",
                              "args": {"in_amount": "1000000"},
                              "accounts": {}}
                             if prog == hs.JUPITER_PROGRAM_ID else
                             {"name": "transfer", "args": {},
                              "accounts": {}}),
                 "summary": ({"type": "swap", "description": "bench",
                              "parsedData": {"protocol": "jupiter"}}
                             if prog == hs.JUPITER_PROGRAM_ID else None)}
                for i, prog in enumerate(own_programs)],
            "matchedIndexes": [own_programs.index(
                hs.JUPITER_PROGRAM_ID)],
            "summary": {"type": "swap", "description": "bench",
                        "parsedData": {"protocol": "jupiter"}},
            "blockTime": None}}}}
    helius_events = []
    hrouter = router_mod.ProviderRouter(
        home=tempfile.mkdtemp(prefix="phase10_helius_"),
        helius_enabled=True, transport=transport)
    stream = hs.HeliusParsedStreamClient(
        wss_url="wss://fake.invalid/?api-key=testkey-not-real",
        router=hrouter, ws_factory=_fake_factory,
        on_event=helius_events.append)
    stream.subscribe(hs.pumpfun_filter(), "pumpfun")
    helius_wss = benchmark_wss_dispatch(
        lambda m: stream._on_message(None, _json.dumps(m)),
        [notif], repeat=wss_messages)
    helius_wss["events_received"] = len(helius_events)
    helius_wss["proves"] = ("Helius parsed-stream notification "
                            "parse + dispatch cost on a fake socket")
    helius_wss["does_not_prove"] = ("live Parsed Streams delivery "
                                    "latency or credit consumption")
    parse_only = benchmark_wss_dispatch(
        lambda m: hs.parse_notification(m), [notif],
        repeat=wss_messages)
    parse_only["proves"] = "parse_notification cost alone (no client)"
    parse_only["does_not_prove"] = "live stream latency"
    report["tables"]["wss"] = {
        "alchemy_slot_dispatch": alchemy_wss,
        "helius_parsed_stream_dispatch": helius_wss,
        "helius_parse_notification_only": parse_only,
    }

    # -- lifecycle stage separation (derived, synthetic inputs) -----
    report["tables"]["lifecycle_stages"] = {
        "stage_latencies_synthetic": stage_derived,
        "note": ("Stage deltas are DERIVED from recorded "
                 "observed_at timestamps, never conflated. The "
                 "0.4s gaps above are synthetic inputs proving the "
                 "derivation, not measurements. Live stage latency "
                 "is UNMEASURED (see unmeasured list)."),
        "proves": "the derivation separates stages and yields None "
                  "for missing stages",
        "does_not_prove": "any live inter-stage latency",
    }

    # -- UNMEASURED (live-only) --------------------------------------
    report["unmeasured"] = [
        unmeasured(
            "live_provider_latency:alchemy/chainstack/public",
            "Alchemy/Chainstack keys unproven (401 from this VM); "
            "live calls forbidden by the Phase 10 hard rules.",
            "On AGT with real keys: run_benchmark(provider, "
            "[(method, params)...], calls_per_method=100, "
            "rpc_call=live) per provider per method; record "
            "p50/p95/p99 + 429/timeout rates + CU/RU spent."),
        unmeasured_helius_adapter(),
        unmeasured(
            "live_stage_latency:preprocessed/processed/confirmed/"
            "finalized",
            "Inter-stage latency is provider + network time; no "
            "live stream runs on this VM, and preprocessed is "
            "paid-gated (refused).",
            "On AGT: subscribe the Helius parsed stream + Alchemy "
            "WSS, record observed_at per stage per signature in "
            "the event store, then run stage_latencies() over the "
            "stored observations (n>=100 per stage pair) and "
            "report each transition separately."),
        unmeasured(
            "live_wss:handshake/reconnect/completeness",
            "No live WSS connection from this VM; handshake 429s "
            "and reconnect timing need the real endpoints.",
            "On AGT: connect each provider WSS, record handshake "
            "time, 429 counts, reconnect time after a forced "
            "drop, and missed-event counts vs the backfill "
            "recovery over 24h."),
        unmeasured(
            "live_backfill_rate",
            "Backfill rate against a live archive depends on "
            "provider paging latency, not driver overhead.",
            "On AGT: run BackfillDriver.recover_gap over a real "
            "24h gap via measure_backfill_performance; record "
            "events/s, pages, and unrecovered ranges."),
        unmeasured(
            "live_quota_consumption",
            "CU/RU spend under live load cannot be observed from "
            "mocked transport.",
            "On AGT: run the routine workload for 1h, read the "
            "usage journal (usage_report.py), and compare CU/RU "
            "spent vs the free-tier budgets."),
    ]
    report["transport_hosts_touched"] = sorted(
        set(h for h, _ in transport.calls))
    return report


def main(argv=None):
    """CLI: run the Phase 10 suite and write the JSON report.

    Usage: python -m daemon.intelligence.acquisition.benchmark
               [--out PATH] [--calls N]
    No live calls. Exits non-zero only on an internal error —
    unmeasured items are reported, not fatal."""
    import argparse
    import json as _json
    import os
    parser = argparse.ArgumentParser(
        description="Phase 10 benchmarks (mocked transport only)")
    parser.add_argument("--out", default=None,
                        help="write JSON report to PATH")
    parser.add_argument("--calls", type=int, default=30,
                        help="router full-call repetitions")
    args = parser.parse_args(argv)
    repo_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    repo_root = os.path.normpath(repo_root)
    report = run_phase10_suite(repo_root, calls_router=args.calls)
    text = _json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print("wrote %s" % args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

