#!/usr/bin/env python3
"""Declared vendor fallback chains (2026-11-21, advanced build).

A pure declaration module: for each data need the bot has, the
explicit ordered fallback chain of vendors, with the cost class,
the per-attempt timeout, and the fail contract of every step.
Stdlib only, never raises, no network, no live state — the parent
wires these chains into the callers; this module only declares
them and stamps which vendor answered into journal values.

Needs and their documented fail contracts. These chains are the
LIVE order, read from the code 2026-11-21 (grad.py, grad_select.py)
and reconciled against the first declaration, which had four of
them aspirational or reversed:

  curve_reads       [curve_cache, helius_rpc, public_rpc]
                    fail CLOSED: an unreadable curve rejects as
                    unknown_curve (grad_select CurveStatsCheck).
                    The cache step itself fails OPEN to a live
                    call (a cache error is never a verdict); the
                    CHAIN fails closed when every step is dark.
                    On a non-429 failure the Helius step tries
                    the next key before going public — folded
                    into the single helius_rpc step.
  quotes            [dex_screener, tape_fallback, dex_screener_retry]
                    ENTRY quotes only (grad._quote_detail): a
                    DexScreener pairs fetch, then a tape
                    fallback, then one bounded DS retry; a
                    priced print with no backing pool liquidity
                    is treated as no quote (F6 phantom guard).
                    Fail OPEN at quote time: no quote is a
                    retryable skip, never a phantom fill
                    (quote_price() returns 0.0; _poll_exits
                    skips the cycle). Fail CLOSED at ENTRY: the
                    entry gate treats "no live quote" as "no
                    entry" — that closed gate lives in the
                    caller, not in the quote function, so the
                    chain's own contract recorded here is "open".
                    Jupiter v3 / Helius reserves are EXIT-only
                    and live in the exit_prices chain, NOT here.
  exit_prices       [caller_price_fn, jupiter_v3, helius_reserves,
                     ds_gt, f2_last_real_price]
                    fail CLOSED at the hard stop: an unquotable
                    position must not be assumed alive — past the
                    hold the existing unquotable backstop
                    finalizes it at 0 (grad._poll_exits). While
                    young, a dark cycle is a skip, not a close;
                    the CLOSED part is the backstop, and it stands.
                    caller_price_fn folds the rt_sync tape AND
                    the armed-watch pushed prices (it wins
                    whenever it prices); helius_reserves is
                    tried only for ARMED positions (peak >= 1.5x);
                    ds_gt is quote_price() (DexScreener + tape —
                    GeckoTerminal never prices exits).
  copycat_tally     [local_recorder, dex_screener]
                    fail CLOSED: an unknown tally rejects as
                    unknown_copycat (grad_select CopycatCheck).
                    The local saturation tally is FIRST (fresh
                    <= 1800s); DexScreener count_24h only when
                    the tally is stale or missing.
  canonical_pool    [migration_event, recorder_migration_match,
                     chain_fallback]
                    fail CLOSED: an unverifiable pool rejects as
                    unknown_canonical (grad_select
                    CanonicalPoolCheck). GeckoTerminal supplies
                    the pool_address being verified — it NEVER
                    verifies canonicity, so it is not a step.
  deployer_launches [local_recorder, curve_oldest_tx_creator]
                    fail CLOSED: an unknown deployer rejects as
                    unknown_serial (grad_select serial checks).
                    The curve fallback runs only when the birth
                    record has no trader AND curve stats are
                    exhausted/uncapped; the launch count itself
                    always comes from the local recorder index
                    even in the fallback path.

Cost classes: "free" = unmetered (no key, no quota — public
endpoints, local files, cached/pushed data); "rpc" = metered
Helius quota (pacer 1 token/20s/key, public-RPC leg under its
own 1-call/2s budget — see reserves.py). timeout_s 0 means the
step makes no outbound request (local file/cache, pushed data,
or a value already in hand).

DECLARED vs LIVE: reconciled 2026-11-21 — the chains above ARE
the live order (verified against grad.py _quote_detail /
_poll_exits and grad_select.py _curve_stats / CopycatCheck /
CanonicalPoolCheck / SerialDeployer checks). The first draft had
copycat reversed, quotes as a combined/aspirational Jupiter-first
chain, exit_prices starting at the armed-watch push instead of
the caller price_fn, and GeckoTerminal as a canonical verifier.
Those are corrected here; the test asserts the live order.

Paper only. Free-only.
"""

import copy

# ---------------------------------------------------------------------
# Documented fail contract per need: the verdict when every step
# in the chain is dark. "closed" = reject / no entry / finalize;
# "open" = skip this cycle and retry (quotes only — see the
# module docstring for the entry-time closed gate).
# ---------------------------------------------------------------------
NEED_FAIL = {
    "curve_reads": "closed",
    "quotes": "open",
    "exit_prices": "closed",
    "copycat_tally": "closed",
    "canonical_pool": "closed",
    "deployer_launches": "closed",
}

# ---------------------------------------------------------------------
# The chains. Each step: {"source", "cost", "timeout_s", "fail"}.
# "fail" on a step repeats the need's contract: a step that
# cannot answer hands over to the next step; only the exhaustion
# of the whole chain triggers the contract.
# ---------------------------------------------------------------------
CHAINS = {
    "curve_reads": [
        {"source": "curve_cache", "cost": "free", "timeout_s": 0,
         "fail": "closed"},
        {"source": "helius_rpc", "cost": "rpc", "timeout_s": 12,
         "fail": "closed"},
        {"source": "public_rpc", "cost": "free", "timeout_s": 12,
         "fail": "closed"},
    ],
    "quotes": [
        {"source": "dex_screener", "cost": "free", "timeout_s": 12,
         "fail": "open"},
        {"source": "tape_fallback", "cost": "free", "timeout_s": 12,
         "fail": "open"},
        {"source": "dex_screener_retry", "cost": "free",
         "timeout_s": 12, "fail": "open"},
    ],
    "exit_prices": [
        {"source": "caller_price_fn", "cost": "free", "timeout_s": 0,
         "fail": "closed"},
        {"source": "jupiter_v3", "cost": "free", "timeout_s": 6,
         "fail": "closed"},
        {"source": "helius_reserves", "cost": "rpc", "timeout_s": 12,
         "fail": "closed"},
        {"source": "ds_gt", "cost": "free", "timeout_s": 12,
         "fail": "closed"},
        {"source": "f2_last_real_price", "cost": "free", "timeout_s": 0,
         "fail": "closed"},
    ],
    "copycat_tally": [
        {"source": "local_recorder", "cost": "free", "timeout_s": 0,
         "fail": "closed"},
        {"source": "dex_screener", "cost": "free", "timeout_s": 5,
         "fail": "closed"},
    ],
    "canonical_pool": [
        {"source": "migration_event", "cost": "free", "timeout_s": 0,
         "fail": "closed"},
        {"source": "recorder_migration_match", "cost": "free",
         "timeout_s": 0, "fail": "closed"},
        {"source": "chain_fallback", "cost": "rpc", "timeout_s": 12,
         "fail": "closed"},
    ],
    "deployer_launches": [
        {"source": "local_recorder", "cost": "free", "timeout_s": 0,
         "fail": "closed"},
        {"source": "curve_oldest_tx_creator", "cost": "rpc",
         "timeout_s": 12, "fail": "closed"},
    ],
}


def chain_for(need):
    """The declared chain for `need`: a list of step dicts.

    Returns a deep copy — mutating the returned list (or its
    step dicts) never touches CHAINS. [] for an unknown need.
    Never raises."""
    try:
        chain = CHAINS.get(need)
        if not isinstance(chain, list):
            return []
        return copy.deepcopy(chain)
    except Exception:
        return []


def fail_contract(need):
    """The documented fail contract for `need`: "closed" or "open".

    Unknown needs fail CLOSED (the safe default). Never raises."""
    try:
        return NEED_FAIL.get(need, "closed")
    except Exception:
        return "closed"


def chain_names(need):
    """Source names of the declared chain for `need`, in order.
    [] for an unknown need. Never raises."""
    try:
        return [str(s.get("source") or "")
                for s in chain_for(need)]
    except Exception:
        return []


def annotate(values, need, source):
    """Stamp which vendor answered into `values`.

    Sets values["vendor_chain"][need] = {"chain": [source names
    in declared order], "used": source}. Keyed by need so the
    four select needs (curve, copycat, canonical, deployer) can
    coexist in one candidate's values dict — a single flat stamp
    would lose all but the last. Never overwrites an existing
    stamp for the SAME need (a first stamp wins — re-annotation
    must not rewrite the record of who actually answered
    first). Never raises; None / non-dict `values` is returned
    unchanged.
    """
    try:
        if not isinstance(values, dict):
            return values
        existing = values.get("vendor_chain")
        if not isinstance(existing, dict):
            existing = {}
            values["vendor_chain"] = existing
        if need in existing:
            return values
        existing[need] = {
            "chain": chain_names(need),
            "used": source,
        }
        return values
    except Exception:
        try:
            return values
        except Exception:
            return None


def validate(chains=None):
    """Problems with `chains` (default: the shipped CHAINS).

    Checks every chain is non-empty, every source name is unique
    within its chain, and every step's fail contract matches its
    need's documented contract — plus that every step is a
    well-formed dict with a known cost class and a non-negative
    timeout. Returns a list of problem strings; [] means valid.
    Never raises."""
    try:
        target = CHAINS if chains is None else chains
        problems = []
        if not isinstance(target, dict):
            return ["chains is not a dict"]
        for need, chain in target.items():
            if need not in NEED_FAIL:
                problems.append("unknown need: %r" % (need,))
                continue
            if not isinstance(chain, list) or not chain:
                problems.append("%s: chain is empty" % (need,))
                continue
            seen = set()
            for step in chain:
                if not isinstance(step, dict):
                    problems.append("%s: step is not a dict: %r"
                                    % (need, step))
                    continue
                src = step.get("source")
                if not src or not isinstance(src, str):
                    problems.append("%s: step has no source name: %r"
                                    % (need, step))
                    continue
                if src in seen:
                    problems.append(
                        "%s: duplicate source %r" % (need, src))
                seen.add(src)
                if step.get("cost") not in ("free", "rpc"):
                    problems.append(
                        "%s: %s has unknown cost %r"
                        % (need, src, step.get("cost")))
                timeout = step.get("timeout_s")
                if not isinstance(timeout, (int, float)) \
                        or isinstance(timeout, bool) or timeout < 0:
                    problems.append(
                        "%s: %s has bad timeout_s %r"
                        % (need, src, timeout))
                if step.get("fail") != NEED_FAIL[need]:
                    problems.append(
                        "%s: %s fail %r != documented contract %r"
                        % (need, src, step.get("fail"),
                           NEED_FAIL[need]))
        return problems
    except Exception as e:
        return ["validate error: %s" % type(e).__name__]
