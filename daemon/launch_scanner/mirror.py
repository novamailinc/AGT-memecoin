#!/usr/bin/env python3
"""Degen-card paper mirror (2026-10-17): card -> paper parity, all chains.

The blueprint's load-bearing rule: "mirror every card to paper with
honest fills, no exceptions." Every fired degen-lane card opens a $1
paper position (the user's own $1/shot rule) so the ledger — not the
designer — judges card quality. This feeds the live-mode gates
("Top Picks winning more than losing on paper").

Per-chain honesty (the "perfect and productive" requirement):
- Solana: fills.py adverse fills (liquidity-scaled slippage, 0.5% floor).
- Base/Ethereum: fills.py price impact + chain_costs gas per tx.
- Hyperliquid: 0.035% taker fee per tx, no gas, mark-price quoting.
- COST GATE (pre-registered): if modeled worst-case round-trip tx costs
  exceed 25% of the $1 notional, the mirror does NOT open — it logs
  COST-BLOCKED with the math. On Ethereum ($5 gas), this correctly
  proves $1 strategies can't survive; that IS the productive result.
- Exits follow the USER's ladder (the mirror measures what the user
  does): 50% @ 2x, 25% @ 3x, 25% on a 30% trailing stop, −50% hard stop
  on the remainder, 24h time-stop hygiene.
  (2026-10-20 correction: a retune to a faster "V4" ladder was applied
  and then REVERTED the same morning — the replay behind it had been
  run on corrupted price paths (wrong GeckoTerminal paging parameter).
  The audited replay ranks the user's ladder FIRST on the peaked>=2x
  subset; see ~/workspace/exit-replay/report.md.)

Killswitch: one lane per chain (degen_mirror, base_mirror, eth_mirror,
hl_mirror), pre-registered halt on negative rolling expectancy after
15 closes. A halted mirror stops opening paper positions ONLY — cards
keep firing; the user's lane is never touched by the mirror's halt.

No paper trading happens anywhere except through this module, and only
for fired cards. Nothing here touches real money, keys, or wallets.

Free-only: stdlib HTTP (DexScreener, Hyperliquid public APIs).
"""
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.mirror")

try:
    from daemon.launch_scanner import fills as _fills
    from daemon.launch_scanner import chain_costs as _costs
    from daemon.launch_scanner import killswitch as _ks
    from daemon.launch_scanner import veto as _veto
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import sources as _sources
    _HAS_DEPS = True
except Exception:
    try:
        import fills as _fills
        import chain_costs as _costs
        import killswitch as _ks
        import veto as _veto
        import safety as _safety
        import sources as _sources
        _HAS_DEPS = True
    except Exception:
        _HAS_DEPS = False

# Tamper-evident verdict ledger (2026-10-23): fail-soft import —
# journaling must never break trading.
try:
    from daemon.launch_scanner import verdict_ledger as _verdict_ledger
except Exception:
    try:
        import verdict_ledger as _verdict_ledger  # type: ignore
    except Exception:
        _verdict_ledger = None  # type: ignore

# F2 sustained-unquotability exit (2026-10-23, user-approved) +
# the direct-reserve cross-probe it needs: fail-soft imports — a
# missing module leaves the pre-F2 behavior byte-identical.
try:
    from daemon.launch_scanner import f2 as _f2
    from daemon.launch_scanner import reserves as _reserves
except Exception:
    try:
        import f2 as _f2  # type: ignore
        import reserves as _reserves  # type: ignore
    except Exception:
        _f2 = None  # type: ignore
        _reserves = None  # type: ignore


def _verdict_event(lane, kind, payload):
    """Append one verdict-ledger record. Never raises. The chain path
    is derived from this module's LEDGER_PATH directory (== HOME in
    production; a temp ledger in tests isolates the chain with it)."""
    try:
        if _verdict_ledger is not None:
            _p = os.path.join(
                os.path.dirname(os.path.abspath(LEDGER_PATH)),
                "verdict_ledger", "events.jsonl")
            _verdict_ledger.append(lane, kind, payload, path=_p)
    except Exception:
        pass


HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "degen_mirror_ledger.json")
_LOCK_PATH = os.path.join(HOME, "degen_mirror_ledger.lock")
MAX_CLOSES = 500

# The user's own $1/shot rule — the mirror measures what the user does.
NOTIONAL_USD = 1.0

# Pre-registered exit plan (the user's ladder, stated 2026-10-17):
LEG_A_FRAC, LEG_A_TARGET = 0.50, 2.0   # sell half at 2x
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # half the remainder at 3x
LEG_C_FRAC, LEG_C_TRAIL = 0.25, 0.30   # final quarter on 30% trailing stop
HARD_STOP_MULT = 0.50                  # hard exit at -50%
TIME_STOP_H = 24                       # hygiene backstop

LANE_BY_CHAIN = {
    "solana": "degen_mirror",
    "base": "base_mirror",
    "ethereum": "eth_mirror",
    "hyperliquid": "hl_mirror",
}

DS_CHAIN = {"solana": "solana", "base": "base", "ethereum": "ethereum"}
HL_INFO = "https://api.hyperliquid.xyz/info"

# Runner gate at entry (2026-10-21, completing the ladder on the user's
# order): the degen mirror's 37-close record (-$16.28, halted 2026-10-19)
# came from mirroring cards at BIRTH size — the same bleed the CEO lane
# had. A Solana card is now mirrored only when the LIVE tape at entry
# moment is runner-grade per the standing rule ($25k liq / $50k mcap /
# rising 5m), verified fresh — the card's own fields are not trusted.
# Identical contract to ceo.py's _runner_gate. Fail-closed on unknown
# tape. Other chains unchanged (their feeds enforce the rule at fire
# time; ETH stays cost-blocked; HL has no DexScreener tape).
RUNNER_MIN_LIQ_USD = 25_000.0
RUNNER_MIN_MCAP_USD = 50_000.0


_LAST_TAPE_SOURCE = ""  # venue of the last _fresh_tape read, for logs
_LAST_GATE_TAPE = None  # last runner-gate tape read (journaling only)


def _fresh_tape(chain, mint):
    """Fresh tape stats dict for the entry gate (DexScreener, with a
    GeckoTerminal fallback per mint when DS is dark). None = unknown.
    The returned dict carries ``tape_source``; the last source is also
    journaled in _LAST_TAPE_SOURCE for skip/open log lines."""
    global _LAST_TAPE_SOURCE
    try:
        stats = _sources.tape_batch_stats([mint],
                                          chain=chain or "solana")
        st = (stats or {}).get(mint) or None
        _LAST_TAPE_SOURCE = str((st or {}).get("tape_source") or "")
        return st
    except Exception:
        _LAST_TAPE_SOURCE = ""
        return None


# Consecutive confirmation (2026-10-21, warp-id pattern adopted from
# the GitHub ecosystem sweep): a single DexScreener print can be a
# stale or spike read — the pool that looks runner-grade on one fetch
# is mid-rug on the next. The gate therefore requires the tape to pass
# on RUNNER_CONFIRM_READS consecutive fresh reads, one rt_sync cadence
# apart. Fail-closed throughout: a degraded or unknown confirmation
# read blocks exactly like a failed first read. Identical contract to
# ceo.py.
RUNNER_CONFIRM_READS = 2
RUNNER_CONFIRM_GAP_S = 5.0


def _runner_gate_check(mint):
    """One runner-grade read of the live tape. Returns the bare failure
    reason ("" when the tape is runner-grade right now)."""
    global _LAST_GATE_TAPE
    st = _fresh_tape("solana", mint)
    # Journaling only: retain the tape this gate read so an entry can
    # stamp the REAL entry-time tape values onto its position (the
    # card's own fields are not trusted and, for some sources, are
    # unpopulated placeholders).
    _LAST_GATE_TAPE = st if isinstance(st, dict) else None
    if not st:
        return "no live tape"
    liq = float(st.get("usd_liquidity") or 0)
    mcap = float(st.get("usd_market_cap") or 0)
    chg = float(st.get("chg_m5") or 0)
    if liq < RUNNER_MIN_LIQ_USD:
        return "liq $%.0f < $25k" % liq
    if mcap < RUNNER_MIN_MCAP_USD:
        return "mcap $%.0f < $50k" % mcap
    if chg <= 0:
        return "not rising (chg_m5 %.1f%%)" % chg
    return ""


def _runner_gate(chain, mint):
    """(ok, why). Solana only; fail-closed on unknown tape.

    Tradeoff, stated plainly: the confirmation read BLOCKS the calling
    feed process for ~RUNNER_CONFIRM_GAP_S, and only on the pass path
    (a card whose first read already failed returns instantly). The
    card itself is sent BEFORE mirror_card is called, so the user never
    waits on this; the cost is a few seconds' delay to that feed's
    next poll cycle, on the handful of runner-grade Solana cards per
    day. (A deferred/async confirm queue would restructure every
    feed's fire site for no gain here.)"""
    global _LAST_GATE_TAPE
    _LAST_GATE_TAPE = None  # journaling: only THIS card's gate reads count
    if chain != "solana":
        return True, ""
    why = _runner_gate_check(mint)
    if why:
        return False, "runner gate: " + why
    for _ in range(RUNNER_CONFIRM_READS - 1):
        time.sleep(RUNNER_CONFIRM_GAP_S)
        why = _runner_gate_check(mint)
        if why:
            via = (" [via %s]" % _LAST_TAPE_SOURCE) if _LAST_TAPE_SOURCE \
                else ""
            return False, "runner gate unconfirmed: " + why + via
    via = (" via %s" % _LAST_TAPE_SOURCE) if _LAST_TAPE_SOURCE else ""
    logger.warning("mirror runner gate confirmed x%d %s%s",
                   RUNNER_CONFIRM_READS, (mint or "?")[:16], via)
    return True, ""


def _gate_tape_stamp():
    """Journaling-only: the confirming runner-gate tape read as a
    compact entry_features sub-record — the REAL entry-time tape
    values (m5 buys/sells/volume, momentum, liquidity, mcap, venue)
    this lane itself measured when it decided to enter. Only fields
    the tape actually carries are stamped: the GeckoTerminal
    fallback rows carry None for the txn windows, and those are
    omitted, never zero-filled. None when no gate tape was read for
    this entry (non-Solana chains skip the gate)."""
    st = _LAST_GATE_TAPE
    if not isinstance(st, dict):
        return None
    out = {}
    for key, src in (("tape_source", "tape_source"),
                     ("liquidity", "usd_liquidity"),
                     ("mcap", "usd_market_cap"),
                     ("chg_m5", "chg_m5"),
                     ("buys_m5", "buys_m5"),
                     ("sells_m5", "sells_m5"),
                     ("vol_m5", "vol_m5")):
        v = st.get(src)
        if v is not None:
            out[key] = v
    return out or None


# ---- ledger -------------------------------------------------------------
# Inter-process locking (audit 2026-10-18): mirror_card runs in 5 separate
# feed processes and mirror-exits polls every 120s. Lock-free
# read-modify-write allowed last-writer-wins clobbering (lost leg closes
# -> double-counted P&L, or dropped new positions). _read/_write are the
# bare file ops; every read-modify-write section must hold _ledger_lock.

import fcntl


class _ledger_lock:
    """Exclusive inter-process lock spanning a read-modify-write section."""

    def __enter__(self):
        self._lf = open(_LOCK_PATH, "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
        finally:
            self._lf.close()
        return False


def _read():
    """Bare ledger read. Hold _ledger_lock if you will modify+write."""
    try:
        with open(LEDGER_PATH) as f:
            d = json.load(f)
            if isinstance(d, dict):
                d.setdefault("positions", {})
                d.setdefault("closes", [])
                return d
    except Exception:
        pass
    return {"positions": {}, "closes": []}


def _write(d):
    """Bare ledger write (atomic tmp+rename, fsync'd). Hold _ledger_lock.

    Flush+fsync the tmp file before the rename and fsync the
    directory after it (best-effort): this VM hard-resets, and an
    un-fsync'd rename can come back hollow/stale (2026-10-24
    grad_select ledger loss)."""
    try:
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, LEDGER_PATH)
        try:
            dfd = os.open(os.path.dirname(LEDGER_PATH) or ".",
                          os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        return True
    except Exception:
        return False


def _load():
    """Locked single read (for read-only callers)."""
    with _ledger_lock():
        return _read()


def _save(d):
    """Locked single write (for single-op callers)."""
    with _ledger_lock():
        return _write(d)


def lane_stats():
    """{lane: {n_closes, wins, win_rate, total_pnl_usd}} from the ledger."""
    d = _load()
    out = {}
    for c in d.get("closes", []):
        lane = c.get("lane", "?")
        s = out.setdefault(lane, {"n_closes": 0, "wins": 0,
                                  "total_pnl_usd": 0.0})
        s["n_closes"] += 1
        if (c.get("pnl_usd") or 0) > 0:
            s["wins"] += 1
        s["total_pnl_usd"] = round(
            s["total_pnl_usd"] + float(c.get("pnl_usd") or 0), 4)
    for s in out.values():
        n = s["n_closes"]
        s["win_rate"] = round(s["wins"] / n, 3) if n else 0.0
    return out


# ---- price feeds --------------------------------------------------------

def _http_json(url, timeout=12, payload=None):
    try:
        data = None
        headers = {"User-Agent": "forex-agent/1.0"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


# Entry-quote repair (2026-10-21 autopsy): cards were passing the
# consecutive-confirmation runner gate and then skipping "no live
# quote" ~0.4s later (Grok in BOTH lanes, Speed in mirror — Speed's
# quote then worked 60s later in ceo). Two defects vs the gate's tape
# read (sources.dexscreener_batch_stats):
#   1. quote_price read pairs[0] blindly, while the gate selects the
#      HIGHEST-LIQUIDITY pair — a fresh graduation's first-listed pair
#      can be the stale/unpriced one.
#   2. _http_json is a single bare-urllib attempt (no retry, no curl),
#      while the gate's fetcher is curl-first with retries/backoff — a
#      transient 429/proxy drop was treated as a final "no quote".
# The quote path now selects pairs exactly like the gate, falls back
# to the gate's own tape read, and retries the pairs fetch once after
# a short gap. Every attempt is journaled into _LAST_QUOTE_DETAIL so a
# skip names what was tried and what each lookup returned. Identical
# contract to ceo.py.
QUOTE_RETRY_GAP_S = 2.0
_LAST_QUOTE_DETAIL = ""


def _ds_pairs_price(pairs):
    """(price, note): price of the highest-liquidity pair that carries
    one — the same selection rule as the gate's tape (sources.py)."""
    if not isinstance(pairs, list) or not pairs:
        return 0.0, "no pairs returned"
    best_px, best_liq, priced = 0.0, -1.0, 0
    for p in pairs:
        if not isinstance(p, dict):
            continue
        try:
            px = float(p.get("priceUsd") or 0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            priced += 1
            if liq > best_liq:
                best_px, best_liq = px, liq
    if best_px > 0:
        return best_px, "%d pair(s), %d priced" % (len(pairs), priced)
    return 0.0, "%d pair(s), none priced" % len(pairs)


def _quote_detail(chain, mint, spot_index=None):
    """(price, trail): quote via, in order — best-pair selection on the
    tokens/v1 fetch, the gate's tape read as fallback, one bounded
    retry of the pairs fetch. Trail is plain-language, for skip logs."""
    global _LAST_QUOTE_DETAIL
    chain = (chain or "").lower()
    if chain == "hyperliquid":
        px = _hl_mark_price(spot_index)
        _LAST_QUOTE_DETAIL = "hl mark price: %s" % (
            "ok" if px > 0 else "unavailable")
        return px, _LAST_QUOTE_DETAIL
    ds = DS_CHAIN.get(chain)
    if not ds:
        _LAST_QUOTE_DETAIL = "no dexscreener chain for %s" % chain
        return 0.0, _LAST_QUOTE_DETAIL
    url = "https://api.dexscreener.com/tokens/v1/%s/%s" % (ds, mint)
    trail = []
    px, note = _ds_pairs_price(_http_json(url))
    trail.append("pairs fetch: " + note)
    if px <= 0:
        # Tape fallback: the runner gate just priced this mint through
        # sources.py (curl + retries, max-liquidity pair). If the tape
        # carries a price, the entry quote uses it — fills.py still
        # applies adverse slippage vs it, nothing is fabricated.
        st = _fresh_tape(chain, mint)
        tpx = float((st or {}).get("price_usd") or 0) if st else 0.0
        if tpx > 0:
            trail.append("tape fallback: priced via %s"
                         % ((st or {}).get("tape_source") or "unknown"))
            logger.warning("mirror quote via tape fallback %s",
                           (mint or "?")[:16])
            px = tpx
        else:
            trail.append("tape fallback: "
                         + ("no tape" if not st else "tape unpriced"))
    if px <= 0:
        time.sleep(QUOTE_RETRY_GAP_S)
        px2, note2 = _ds_pairs_price(_http_json(url))
        trail.append("pairs retry: " + note2)
        if px2 > 0:
            px = px2
    _LAST_QUOTE_DETAIL = "; ".join(trail)
    return px, _LAST_QUOTE_DETAIL


def quote_price(chain, mint, spot_index=None):
    """Live quoted price in USD. 0.0 only when NO source can price the
    mint (caller skips). Attempts are journaled in _LAST_QUOTE_DETAIL.
    Also used by exit polling: 0 still means 'unknown — skip this
    cycle', never a fabricated price."""
    try:
        return _quote_detail(chain, mint, spot_index)[0]
    except Exception:
        return 0.0


def _hl_mark_price(spot_index):
    try:
        d = _http_json(HL_INFO, payload={"type": "spotMetaAndAssetCtxs"})
        if not isinstance(d, list) or len(d) < 2:
            return 0.0
        universe = (d[0] or {}).get("universe") or []
        ctxs = d[1] or []
        for u, c in zip(universe, ctxs):
            try:
                if int(u.get("index")) == int(spot_index):
                    return float((c or {}).get("markPx") or 0)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return 0.0


# ---- entry --------------------------------------------------------------

def _veto_ok(chain, mint, liquidity):
    """Re-verify hard vetoes at mirror entry. Fail-open on exception
    (vetoes only on positive evidence); fail-CLOSED on unknown EVM
    safety (same rule as the cards)."""
    try:
        if chain == "solana":
            # run_bundle=False: the card pipeline just ran the full safety
            # suite seconds ago; re-running the expensive RPC walk here
            # would stall the feed's card loop on a slow (not failing) RPC
            # (audit 2026-10-18). Vetoes still apply on positive evidence.
            rep = _veto.veto_check(mint, "solana", run_bundle=False,
                                   lane="mirror")
            return bool(rep.get("passed")), "; ".join(rep.get("vetoes", []))
        if chain in ("base", "ethereum"):
            rep = _safety.check_token(chain, mint, timeout=10)
            if rep.get("unknown"):
                return False, "safety unknown at mirror entry"
            if not rep.get("passed"):
                return False, "; ".join(rep.get("fails", []))[:120]
            return True, ""
        if chain == "hyperliquid":
            return True, ""  # exchange-settled; no contract veto applies
        return False, "unsupported chain"
    except Exception as e:
        return True, "veto unavailable (%s)" % e


def mirror_card(chain, source, mint, symbol="", score=0.0, tier="",
                mcap=0.0, liquidity=0.0, evidence=None, extra=None):
    """Open a $1 paper position mirroring a fired card.

    Called at the card-fire site, right after the card sends. Returns
    (opened: bool, reason: str). Never raises. Cards are never affected
    by the mirror's decisions.
    """
    try:
        return _mirror_card(chain, source, mint, symbol, score, tier,
                            mcap, liquidity, evidence, extra)
    except Exception as e:
        logger.warning("mirror_card failed for %s: %s", mint, e)
        return False, "mirror error: %s" % e


def _mirror_card(chain, source, mint, symbol, score, tier,
                 mcap, liquidity, evidence, extra):
    if not _HAS_DEPS:
        return False, "mirror deps missing"
    chain = (chain or "").lower()
    lane = LANE_BY_CHAIN.get(chain)
    if not lane:
        return False, "chain %s not mirrored" % chain
    if not mint:
        return False, "no mint"

    # The mirror's halt never touches cards — it only stops new paper.
    try:
        if _ks.is_halted(lane):
            return False, "mirror lane %s halted" % lane
    except Exception:
        pass

    # Runner gate (2026-10-21): Solana mirrors must be runner-grade on
    # the live tape at entry — see _runner_gate.
    ok_gate, why_gate = _runner_gate(chain, mint)
    if not ok_gate:
        return False, why_gate

    # Locked read-modify-write: two feeds firing the same mint at once
    # must not double-open (audit 2026-10-18).
    with _ledger_lock():
        d = _read()
        if mint in d["positions"]:
            return False, "already mirrored (open)"
        if any(c.get("mint") == mint for c in d["closes"]):
            return False, "already mirrored (closed)"

    # Pre-registered cost gate: the productive ETH answer.
    blocked, why = _costs.cost_gate(chain, NOTIONAL_USD)
    if blocked:
        logger.warning("mirror %s: %s", mint, why)
        return False, why

    extra = extra or {}
    spot_index = extra.get("spot_index")
    quoted = quote_price(chain, mint, spot_index)
    if quoted <= 0:
        # HL listings carry their mark price from the watcher.
        quoted = float(extra.get("mark_px") or 0)
    if quoted <= 0:
        why = "no live quote"
        if _LAST_QUOTE_DETAIL:
            why = "no live quote (%s)" % _LAST_QUOTE_DETAIL
        return False, why

    ok, why = _veto_ok(chain, mint, liquidity)
    if not ok:
        return False, "veto at entry: %s" % why

    # Honest entry: adverse fill vs quoted (fills.py) on the $1 notional
    # minus the chain's per-tx cost.
    gas_in = _costs.tx_cost_usd(chain, NOTIONAL_USD)
    eff_notional = NOTIONAL_USD - gas_in
    if eff_notional <= 0:
        return False, "costs exceed notional"
    try:
        filled = _fills.honest_entry(quoted, eff_notional, liquidity or None)
    except Exception:
        filled = quoted
    if filled <= 0:
        return False, "bad fill price"
    tokens = eff_notional / filled

    # Journaling only: the gate's confirming tape read (real
    # entry-time buys/sells/liquidity/mcap), stamped below into the
    # position's entry_features so every close carries it.
    gate_tape = _gate_tape_stamp()
    pos = {
        "mint": mint, "symbol": (symbol or "?")[:32], "chain": chain,
        "source": (source or "unknown")[:32], "lane": lane,
        "score": round(float(score or 0), 1), "tier": (tier or "")[:32],
        "entry_ts": time.time(), "quoted_price": quoted,
        "entry_price": filled, "notional_usd": NOTIONAL_USD,
        # Position-level peak over poll-observed prices, seeded with
        # the entry fill (the first genuinely observed price). Updated
        # in _poll_exits; persisted on closes as peak_mult so
        # round-tripper leakage (peaked >=2x, closed <1x) is measurable
        # (added 2026-10-20 — closes previously carried no peak at all).
        "peak_price": filled,
        "tokens": tokens, "gas_in_usd": round(gas_in, 6),
        "legs": {
            "a": {"frac": LEG_A_FRAC, "target": LEG_A_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "b": {"frac": LEG_B_FRAC, "target": LEG_B_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "c": {"frac": LEG_C_FRAC, "trail": LEG_C_TRAIL, "peak": filled,
                  "done": False, "exit_price": 0, "realized_usd": 0},
        },
        "realized_usd": 0.0,
        "status": "open",
        "spot_index": spot_index, "pair": extra.get("pair", ""),
        "entry_snapshot": {
            "score": round(float(score or 0), 1), "tier": (tier or "")[:32],
            "mcap": float(mcap or 0), "liquidity": float(liquidity or 0),
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
            "evidence": dict(evidence or {}),
        },
        # Uniform decision-time record (2026-10-23, journaling):
        # ceo/grad positions have always carried entry_features and
        # their closes inherit it; mirror positions had none, so the
        # card's smart-money evidence lived only inside the snapshot
        # and the gate's real tape read was discarded. _finalize now
        # copies this into every close — on BOTH exit paths, since
        # scripts/mirror-exits and rt_sync both drive this module's
        # poll_exits/_finalize.
        "entry_features": {
            "chain": chain, "source": (source or "unknown")[:32],
            "score": round(float(score or 0), 1),
            "tier": (tier or "")[:32],
            "mcap": float(mcap or 0),
            "liquidity": float(liquidity or 0),
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
            "evidence": dict(evidence or {}),
            "gate_tape": gate_tape,
        },
    }
    d["positions"][mint] = pos
    with _ledger_lock():
        # Re-check under lock: another feed may have opened it while we
        # were quoting/vetoing (idempotency must hold under concurrency).
        d = _read()
        if mint in d["positions"] or any(
                c.get("mint") == mint for c in d["closes"]):
            return False, "already mirrored (race)"
        d["positions"][mint] = pos
        _write(d)
    logger.warning("mirror OPEN %s %s %s @ %s ($1, %s)",
                   chain, pos["symbol"], mint[:12], filled, lane)
    return True, "opened"


# ---- exits --------------------------------------------------------------

def _close_leg(pos, leg_key, exit_price, reason):
    """Close one leg at an honest exit. Returns realized USD for the leg."""
    chain = pos["chain"]
    leg = pos["legs"][leg_key]
    if leg["done"]:
        return 0.0
    leg_tokens = pos["tokens"] * leg["frac"]
    try:
        leg_usd = leg_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd, (pos.get("last_liquidity")
              or pos["entry_snapshot"].get("liquidity")) or None)
    except Exception:
        filled_exit = exit_price
    gross = leg_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(chain, gross)
    net = gross - gas_out
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _close_remaining(pos, exit_price, reason):
    """Close all remaining legs as ONE combined exit (one tx cost).

    Audit 2026-10-18: looping _close_leg charged one gas per leg (3x on
    Base) while the cost model prices the stop as a single tx. The
    remainder exits together, so it pays once; net is split pro-rata
    across legs for the ledger.
    """
    chain = pos["chain"]
    open_legs = [k for k in ("a", "b", "c") if not pos["legs"][k]["done"]]
    if not open_legs:
        return 0.0
    total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"]
                       for k in open_legs)
    try:
        leg_usd = total_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd, (pos.get("last_liquidity")
              or pos["entry_snapshot"].get("liquidity")) or None)
    except Exception:
        filled_exit = exit_price
    gross = total_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(chain, gross)  # ONE tx for the combined exit
    net = gross - gas_out
    for key in open_legs:
        leg = pos["legs"][key]
        share = (pos["tokens"] * leg["frac"]) / total_tokens if total_tokens else 0
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net * share, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _peak_fields(pos):
    """Peak price/multiple for the close record, from RECORDED prices
    only — never fabricated.

    Positions opened with peak tracking carry pos["peak_price"], a
    running peak over poll-observed prices seeded with the entry fill
    (peak_source="tracked"). Positions opened before tracking existed
    have no peak_price at entry; polls since the upgrade seed it
    mid-life and flag pos["peak_partial"]. For those, the max of every
    recorded price (tracked peak, leg C's peak — seeded at the fill,
    updated while leg C was open — and the legs' realized exit prices)
    is an honest LOWER bound on the true peak
    (peak_source="legacy_lower_bound"). If no recorded price exists
    at all, return nulls with peak_unknown=True.
    """
    entry = float(pos.get("entry_price") or 0)
    candidates = []
    tracked = pos.get("peak_price")
    if tracked:
        candidates.append(float(tracked))
    legs = pos.get("legs") or {}
    lc = legs.get("c") or {}
    if lc.get("peak"):
        candidates.append(float(lc["peak"]))
    for v in legs.values():
        if v.get("done") and v.get("exit_price"):
            candidates.append(float(v["exit_price"]))
    if entry <= 0 or not candidates:
        return {"peak_price": None, "peak_mult": None,
                "peak_unknown": True, "peak_ts": None}
    peak_price = max(candidates)
    return {"peak_price": peak_price,
            "peak_mult": round(peak_price / entry, 4),
            # F3 (2026-10-23): when the peak happened.
            "peak_ts": pos.get("peak_ts"),
            "peak_source": ("legacy_lower_bound"
                            if pos.get("peak_partial") else "tracked")}


def _finalize(d, mint, pos, closed_reason):
    """Move an open position to closes, record the lane trade.

    Must be called with _ledger_lock held; writes via _write (no re-lock,
    which would deadlock).
    """
    pnl = round(pos["realized_usd"] - NOTIONAL_USD, 4)
    rec = {
        "mint": mint, "symbol": pos["symbol"], "chain": pos["chain"],
        "source": pos["source"], "lane": pos["lane"],
        "entry_ts": pos["entry_ts"], "close_ts": time.time(),
        "entry_price": pos["entry_price"],
        "pnl_usd": pnl,
        "closed_reason": closed_reason,
        "legs": {k: {"done": v["done"],
                      "exit_price": v.get("exit_price", 0),
                      "realized_usd": v.get("realized_usd", 0),
                      "exit_reason": v.get("exit_reason", "")}
                 for k, v in pos["legs"].items()},
        "entry_snapshot": pos["entry_snapshot"],
        # Journaling (2026-10-23): the position's entry-time record
        # (card evidence + gate tape) rides into every close.
        "entry_features": pos.get("entry_features", {}),
        # F3/P1 (2026-10-23): quote journal tail + exit liquidity.
        "quote_journal": list(pos.get("quote_journal") or [])[-10:],
        "exit_liquidity": pos.get("last_liquidity"),
        "transitions": list(pos.get("transitions") or []),
    }
    rec.update(_peak_fields(pos))
    # F2 stamp (2026-10-23): pos["_f2"] is set ONLY by the F2 exit
    # branch — its presence is the unmistakable marker, copied into
    # the record and the verdict-ledger payload below.
    if pos.get("_f2"):
        rec["f2"] = dict(pos["_f2"])
    d["closes"].append(rec)
    d["closes"] = d["closes"][-MAX_CLOSES:]
    del d["positions"][mint]
    _write(d)  # lock held by caller (_poll_exits)
    try:
        _ks.record_trade(pos["lane"], pnl)
        _ks.check_lane(pos["lane"])
    except Exception:
        pass
    logger.warning("mirror CLOSE %s %s pnl=$%.2f (%s)",
                   pos["chain"], pos["symbol"], pnl, closed_reason)
    _vp = {"mint": mint, "pnl_usd": pnl,
           "realized_mult": round(float(pos["realized_usd"])
                                  / NOTIONAL_USD, 4),
           "closed_reason": closed_reason,
           "closes_count": len(d["closes"])}
    if rec.get("f2"):
        _vp["f2"] = rec["f2"]
    _verdict_event(pos["lane"], "close", _vp)
    return rec


def poll_exits(price_fn=None):
    """Evaluate exits for all open mirror positions. Called every ~2 min
    by scripts/mirror-exits. price_fn(chain, mint, spot_index) -> float
    override for tests. Returns list of close records. Never raises."""
    try:
        return _poll_exits(price_fn)
    except Exception as e:
        logger.warning("poll_exits failed: %s", e)
        return []


def _poll_exits(price_fn):
    # Phase 1: snapshot open positions (locked read). Network I/O for
    # price quotes happens OUTSIDE the lock in phase 2, so a slow feed
    # never blocks mirror_card entries.
    with _ledger_lock():
        snapshot = {m: p for m, p in _read()["positions"].items()}
    if not snapshot:
        return []
    # Phase 2: fetch quotes (no lock).
    hl_marks = {}
    if any(p["chain"] == "hyperliquid" for p in snapshot.values()):
        hl_marks = _hl_all_marks()  # one HL context fetch per cycle
    quotes = {}
    refusals = {}
    liqs = {}
    for mint, pos in snapshot.items():
        try:
            if price_fn:
                px = float(price_fn(pos["chain"], mint,
                                    pos.get("spot_index")) or 0)
            elif pos["chain"] == "hyperliquid":
                px = float(hl_marks.get(str(pos.get("spot_index")), 0) or 0)
            else:
                px = quote_price(pos["chain"], mint)
                try:  # F5: fresh liquidity for the exit fill model
                    st = _fresh_tape(pos["chain"], mint)
                    if st and float(st.get("usd_liquidity") or 0) > 0:
                        liqs[mint] = float(st["usd_liquidity"])
                except Exception:
                    pass
        except Exception:
            px = 0.0
        # F2 cross-probe (2026-10-23): aggregator dark on an ARMED
        # Solana position — the direct pool reserves get their say
        # before anyone may call it unquotable (same source grad's
        # phase 2 and rt_sync's pushed pricing already use). A live
        # reserve price makes the position quotable and is evaluated
        # normally; both dark is what phase 3's F2 check requires.
        # (With a caller price_fn, rt_sync's 0 already folds the
        # tape + the pushed reserve price, so no probe is needed.)
        if px <= 0 and not price_fn and _f2 is not None \
                and _reserves is not None \
                and (pos.get("chain") or "solana") == "solana" \
                and _f2.is_candidate(pos):
            try:
                rpx, _rsrc = _reserves.price_for_position(
                    dict(pos, mint=mint))
                if rpx > 0:
                    px = rpx
                elif _rsrc:
                    refusals[mint] = _rsrc
            except Exception:
                pass
        quotes[mint] = px
    # Phase 3: mutate under lock (fresh read; positions opened between
    # phase 1 and 3 simply wait for the next cycle).
    with _ledger_lock():
        d = _read()
        closes = []
        now = time.time()
        for mint, pos in list(d["positions"].items()):
            try:
                px = quotes.get(mint, 0)
                if px <= 0:
                    # Reserves refusal provenance (2026-11-22): the
                    # refusal DETAIL is journalled so a schema-gate
                    # refusal is distinguishable from ordinary
                    # darkness in the close record.
                    _ref = refusals.get(mint)
                    if _ref:
                        _jq = pos.setdefault("quote_journal", [])
                        _jq.append({"ts": now, "px": 0,
                                    "src": "reserves_refused",
                                    "detail": _ref})
                        del _jq[:-40]
                        # Dark positions `continue` below without the
                        # normal per-position write — persist the
                        # provenance now (lock held).
                        _write(d)
                    # F2 (2026-10-23): an ARMED position dark across
                    # every source for a sustained stretch exits at
                    # its last real observed price, stamped — instead
                    # of waiting for the first post-rug dust print.
                    # Never-armed positions keep the exact pre-F2
                    # behavior below (incl. the 24h backstop).
                    _f2_stamp = _f2.check(pos, now) \
                        if _f2 is not None else None
                    if _f2_stamp is not None:
                        pos["_f2"] = _f2_stamp
                        _close_remaining(
                            pos, _f2_stamp["last_real_price"],
                            _f2.F2_REASON)
                        closes.append(_finalize(
                            d, mint, pos, _f2.F2_REASON))
                        continue
                    # Unknown price: a transient feed blip skips this cycle,
                    # never force-close on missing data. But if the position
                    # is ALSO past the 24h backstop, the token is almost
                    # certainly dead/delisted (no DexScreener pair after
                    # 24h+ is a rug, not a blip) — finalize it at 0 so the
                    # ledger judges a full loss instead of holding it open
                    # forever. (Fixed 2026-10-19: 12 positions sat open 42h+
                    # because the skip made the time stop unreachable.)
                    if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                        _close_remaining(pos, 0.0,
                                         "time_stop_24h_unquotable")
                        closes.append(_finalize(
                            d, mint, pos, "time_stop_24h_unquotable"))
                    continue
                entry = pos["entry_price"]
                mult = px / entry if entry > 0 else 0

                # Position-level peak tracking (2026-10-20): fold every
                # observed quote into the running peak BEFORE exits are
                # evaluated, so the close record's peak_mult includes
                # the final cycle. Positions opened before tracking
                # existed are seeded mid-life and flagged partial —
                # their closes report a lower bound, never an invention.
                if "peak_price" not in pos:
                    pos["peak_price"] = 0.0
                    pos["peak_partial"] = True
                if px > float(pos.get("peak_price") or 0):
                    pos["peak_price"] = px
                    pos["peak_ts"] = now  # F3 (2026-10-23)
                if mint in liqs:
                    pos["last_liquidity"] = liqs[mint]  # F5
                # F3: per-eval quote journal (compact, capped).
                jq = pos.setdefault("quote_journal", [])
                jq.append({"ts": now, "px": px, "src": "poll"})
                del jq[:-40]

                # Hard stop first: -50% closes everything immediately.
                if mult <= HARD_STOP_MULT:
                    _close_remaining(pos, px, "hard_stop_-50pct")
                    closes.append(_finalize(d, mint, pos, "hard_stop_-50pct"))
                    continue
                # Time stop: 24h hygiene backstop.
                if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                    _close_remaining(pos, px, "time_stop_24h")
                    closes.append(_finalize(d, mint, pos, "time_stop_24h"))
                    continue
                # Leg A: 50% at 2x.
                la = pos["legs"]["a"]
                if not la["done"] and mult >= LEG_A_TARGET:
                    _close_leg(pos, "a", px, "leg_a_2x")
                # Leg B: 25% at 3x.
                lb = pos["legs"]["b"]
                if not lb["done"] and mult >= LEG_B_TARGET:
                    _close_leg(pos, "b", px, "leg_b_3x")
                # Leg C: 25% on 30% trailing stop from peak.
                lc = pos["legs"]["c"]
                if not lc["done"]:
                    if px > lc["peak"]:
                        lc["peak"] = px
                    elif px <= lc["peak"] * (1 - LEG_C_TRAIL):
                        _close_leg(pos, "c", px, "leg_c_trail_30pct")
                # All legs done -> finalize.
                if all(pos["legs"][k]["done"] for k in ("a", "b", "c")):
                    closes.append(_finalize(d, mint, pos, "ladder_complete"))
                else:
                    _write(d)
            except Exception as e:
                logger.warning("poll_exits %s failed: %s", mint, e)
                continue
        return closes


def _hl_all_marks():
    """{spot_index: markPx} for one poll cycle. Best effort."""
    out = {}
    try:
        d = _http_json(HL_INFO, payload={"type": "spotMetaAndAssetCtxs"})
        if not isinstance(d, list) or len(d) < 2:
            return out
        for u, c in zip((d[0] or {}).get("universe") or [], d[1] or []):
            try:
                out[str(int(u.get("index")))] = float(
                    (c or {}).get("markPx") or 0)
            except (TypeError, ValueError):
                continue
    except Exception:
        pass
    return out


def open_positions():
    """{mint: position} currently open. Never raises."""
    try:
        return _load()["positions"]
    except Exception:
        return {}
