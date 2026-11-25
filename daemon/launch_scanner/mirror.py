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
import copy
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.mirror")

try:
    from daemon.launch_scanner import fills as _fills
    from daemon.launch_scanner import risk_controls as _risk
    from daemon.launch_scanner import chain_costs as _costs
    from daemon.launch_scanner import killswitch as _ks
    from daemon.launch_scanner import veto as _veto
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import sources as _sources
    _HAS_DEPS = True
except Exception:
    try:
        import fills as _fills
        import risk_controls as _risk
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


HOME = (os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent"))
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


_LAST_QUOTE_STAMP = None  # request-start/receipt of the last priced quote
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
        if _PENDING_CLOSE_INTENTS:
            _ciw = _close_intents()
            if _ciw is not None:
                _keep = []
                for _lane, _iid, _pid, _mint, _suffix in \
                        _PENDING_CLOSE_INTENTS:
                    if _ciw.tranche_lane_done(d, _pid, _mint, _suffix):
                        _ciw.mark_lane_applied(_lane, _iid)
                    else:
                        _keep.append((_lane, _iid, _pid, _mint, _suffix))
                _PENDING_CLOSE_INTENTS[:] = _keep
            else:
                _PENDING_CLOSE_INTENTS.clear()
        return True
    except Exception:
        return False

# ---- shared paper account routing (mirror lanes) ------------------------
# Mirror lanes (degen/base/eth/hl_mirror) reserve cash from the ONE shared
# paper account like the other lanes (same pattern as ceo.py): the entry
# debits it under the mandatory guard and gates, every exit TRANCHE credits
# net proceeds with a stable op_id, and a durable close intent covers the
# account/ledger write gap. The account lane name is pos["lane"].
def _paper_account():
    if os.environ.get("PAPER_ACCOUNT_ENABLED") != "1":
        return None
    try:
        try:
            from daemon.launch_scanner import paper_account
        except ImportError:
            import paper_account
        return paper_account
    except Exception:
        logger.exception("mirror: paper account unavailable")
        return None


def _close_intents():
    if _paper_account() is None:
        return None
    try:
        try:
            from daemon.launch_scanner import close_intents
        except ImportError:
            import close_intents
        return close_intents
    except Exception:
        logger.exception("mirror: close-intent journal unavailable")
        return None


# (lane, intent_id, pos_id, mint, suffix) whose account side is durable but
# whose lane-ledger write is not yet confirmed; flushed by _write only when
# the persist actually carries the tranche/close.
_PENDING_CLOSE_INTENTS = []
_RECOVERY_DONE = False
_ENTRY_BLOCKED = False


def _recover_close_intents():
    """Re-drive incomplete close intents once per process for every mirror
    lane before the first entry/close. A frozen intent (account and ledger
    diverge) blocks ALL entries until an operator resolves it."""
    global _RECOVERY_DONE, _ENTRY_BLOCKED
    if _RECOVERY_DONE:
        return _ENTRY_BLOCKED
    _RECOVERY_DONE = True
    _ci = _close_intents()
    if _ci is None:
        return False
    for lane in sorted(set(LANE_BY_CHAIN.values())):
        def account_apply(op, lane=lane):
            _pa = _paper_account()
            if _pa is None:
                return False, "paper account disabled"
            if op.get("kind") == "write_down":
                ok, why = _pa.write_down(
                    lane, op.get("mint"), op.get("op_id"),
                    pos_id=op.get("pos_id"), qty=op.get("qty"),
                    reason=op.get("reason") or "recovered")
            else:
                ok, why = _pa.credit(
                    lane, op.get("mint"), op.get("amount"),
                    op.get("op_id"), pos_id=op.get("pos_id"),
                    qty=op.get("qty"), fee=op.get("fee") or 0.0,
                    price_kind=op.get("price_kind"))
            if not ok and _pa.op_outcome(op.get("op_id")) == "orphan" \
                    and not _pa.pos_ever_existed(
                        pos_id=op.get("pos_id"), lane=lane,
                        mint=op.get("mint")):
                return True, "orphan (converged, epoch-proven)"
            return ok, why

        def lane_has_close(intent):
            info = intent.get("close_info") or {}
            return _ci.tranche_lane_done(_read(), intent.get("pos_id"),
                                         intent.get("mint"),
                                         info.get("leg_suffix"))

        def lane_apply(intent):
            d = _read()
            pos = (d.get("positions") or {}).get(intent.get("mint"))
            if pos is not None and (
                    not intent.get("pos_id")
                    or pos.get("pa_pos_id") == intent.get("pos_id")):
                return None
            return False

        frozen = _ci.recover_lane(lane, account_apply, lane_has_close,
                                  lane_apply)
        if frozen:
            _ENTRY_BLOCKED = True
            logger.error("mirror %s: %d close intent(s) FROZEN - ledgers "
                         "diverge, ENTRIES BLOCKED until operator "
                         "resolution: %s", lane, len(frozen),
                         ",".join(frozen))
    return _ENTRY_BLOCKED


def _intent_gate(pos, key):
    """FAIL CLOSED before anything is sold or booked: when the shared account is on for this position and the close-intent journal cannot be
    written, the leg/close is NOT executed (no sale, no lane close/bankroll/verdict, no account op). The position shows pending_exit
    close_intent_not_durable and the exit is retried on later polls, once storage returns."""
    if not pos.get("pa_pos_id"):
        return True
    _ci = _close_intents()
    if _ci is None:
        return True
    try:
        _ok = _ci.journal_writable(pos["lane"])
    except Exception:
        _ok = False
    if _ok:
        return True
    logger.error("mirror: close intent journal NOT writable: exit %s not executed (pending_exit close_intent_not_durable), retried next poll", key)
    _hold_not_durable(pos, key)
    return False


def _pa_apply(pos, suffix, kind_op, apply_fn):
    """Shared body of settle/write-down: durable intent BEFORE the account
    op, orphan convergence, pending flush registration. Failures are
    journaled by the account and never block the lane's own settle."""
    pid = pos.get("pa_pos_id")
    if not pid:
        return
    _pa = _paper_account()
    if _pa is None:
        return
    lane, mint = pos["lane"], pos["mint"]
    _pend = None
    _ci = _close_intents()
    if _ci is not None:
        iid = _ci.begin(lane, mint, pid, [kind_op],
                        {"realized_usd": kind_op.get("amount") or 0.0,
                         "closed_reason": suffix, "close_ts": time.time(),
                         "leg_suffix": suffix})
        _pend = (lane, iid, pid, mint, suffix)
    _ok, _why = apply_fn(_pa)
    if not _ok and _pa.op_outcome(kind_op["op_id"]) == "orphan" \
            and not _pa.pos_ever_existed(pos_id=pid, lane=lane, mint=mint):
        _ok, _why = True, ""
    if _ok:
        if _pend is not None:
            _ci.mark_account_applied(lane, _pend[1])
            _PENDING_CLOSE_INTENTS.append(_pend)
    else:
        logger.warning("mirror: shared paper account %s for %s (%s) failed: "
                       "%s (journaled, intent open for recovery)",
                       kind_op["kind"], mint, suffix, _why)


def _pa_settle(pos, qty, gross, fee, suffix, price_kind=None):
    pid = pos.get("pa_pos_id")
    if not pid:
        return
    lane, mint = pos["lane"], pos["mint"]
    op = "%s:%s:%s:%s" % (lane, suffix, mint, pid)
    _pa_apply(pos, suffix,
              {"kind": "credit", "op_id": op, "mint": mint, "amount": gross,
               "qty": qty, "fee": fee, "price_kind": price_kind,
               "pos_id": pid},
              lambda _pa: _pa.credit(lane, mint, gross, op, pos_id=pid,
                                     qty=qty, fee=fee,
                                     price_kind=price_kind))


def _pa_write_down(pos, reason):
    pid = pos.get("pa_pos_id")
    if not pid:
        return
    lane, mint = pos["lane"], pos["mint"]
    op = "%s:writedown:%s:%s" % (lane, mint, pid)
    _pa_apply(pos, "writedown",
              {"kind": "write_down", "op_id": op, "mint": mint,
               "pos_id": pid, "reason": reason},
              lambda _pa: _pa.write_down(lane, mint, op, pos_id=pid,
                                         reason=reason))


try:
    from daemon.launch_scanner import demo_fill as _demo
except Exception:
    try:
        import demo_fill as _demo  # type: ignore
    except Exception:
        _demo = None  # type: ignore


def _demo_filler():
    return _demo.default_filler() if _demo is not None else None


def _demo_mode():
    return bool(_demo is not None and _demo.mode_enabled())


# EXIT POLICY (open owner decision, NOT chosen here): the configured owner impact cap
# (max_price_impact_pct) ALSO gates exits when set; only an unset/invalid cap falls back to
# this value so a missing config cannot strand a position. A separate exit cap or unbounded
# exit is an owner decision; the effective cap is recorded in each exit's provenance.
_EXIT_IMPACT_CAP_PCT = 100.0


def _entry_notional():
    """Exact stake: the owner-configured trade size when set (the account
    refuses a conflicting/invalid one), else this lane's legacy $1."""
    _pa = _paper_account()
    if _pa is None:
        return NOTIONAL_USD
    try:
        v = _pa.configured_trade_size()
    except Exception:
        v = None
    return NOTIONAL_USD if v is None else float(v)



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


def _tape_quote_stamp(st):
    """Stamp for a quote priced from a tape row: only the row's own
    stamps (source_asof/fetched_at + received_at). Missing -> None, so
    the entry age gate refuses; never invented."""
    if not isinstance(st, dict):
        return None
    out = {}
    for k in ("source_asof", "fetched_at", "received_at"):
        v = st.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    if "received_at" not in out or not (
            "source_asof" in out or "fetched_at" in out):
        return None
    return out


def _quote_detail(chain, mint, spot_index=None):
    """(price, trail): quote via, in order — best-pair selection on the
    tokens/v1 fetch, the gate's tape read as fallback, one bounded
    retry of the pairs fetch. Trail is plain-language, for skip logs."""
    global _LAST_QUOTE_DETAIL, _LAST_QUOTE_STAMP
    chain = (chain or "").lower()
    _qstart = time.time()
    _LAST_QUOTE_STAMP = None
    if chain == "hyperliquid":
        px = _hl_mark_price(spot_index)
        if px > 0:
            _LAST_QUOTE_STAMP = {"fetched_at": _qstart,
                                 "received_at": time.time()}
        _LAST_QUOTE_DETAIL = "hl mark price: %s" % (
            "ok" if px > 0 else "unavailable")
        return px, _LAST_QUOTE_DETAIL
    ds = DS_CHAIN.get(chain)
    if not ds:
        _LAST_QUOTE_DETAIL = "no dexscreener chain for %s" % chain
        return 0.0, _LAST_QUOTE_DETAIL
    url = "https://api.dexscreener.com/tokens/v1/%s/%s" % (ds, mint)
    trail = []
    _rs = time.time()
    px, note = _ds_pairs_price(_http_json(url))
    if px > 0:
        # Stamp only from the observed request start/receipt (DexScreener
        # gives no source time); no price -> no stamp -> age gate refuses.
        _LAST_QUOTE_STAMP = {"fetched_at": _rs, "received_at": time.time()}
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
            _LAST_QUOTE_STAMP = _tape_quote_stamp(st)
        else:
            trail.append("tape fallback: "
                         + ("no tape" if not st else "tape unpriced"))
    if px <= 0:
        time.sleep(QUOTE_RETRY_GAP_S)
        _rs2 = time.time()
        px2, note2 = _ds_pairs_price(_http_json(url))
        trail.append("pairs retry: " + note2)
        if px2 > 0:
            px = px2
            _LAST_QUOTE_STAMP = {"fetched_at": _rs2,
                                 "received_at": time.time()}
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
    global _LAST_QUOTE_STAMP
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

    # MANDATORY capital gate: the shared paper account's guard (account
    # enabled, gates on, every owner limit set) must pass before any entry.
    # The reservation itself happens below, under the ledger lock.
    try:
        try:
            from daemon.launch_scanner import entry_guard as _eg
        except ImportError:
            import entry_guard as _eg
        return_why = _eg.mandatory_entry_refusal(lane)
    except Exception:
        return_why = "mandatory entry guard unavailable; entries blocked fail-closed"
    if return_why:
        return False, return_why
    _pa = _paper_account()
    if _pa is not None:
        _recover_close_intents()
        if _ENTRY_BLOCKED:
            return False, ("close-intent divergence frozen: entries "
                           "blocked until operator resolution")
    _N = _entry_notional()

    # Locked read-modify-write: two feeds firing the same mint at once
    # must not double-open (audit 2026-10-18).
    with _ledger_lock():
        d = _read()
        if mint in d["positions"]:
            return False, "already mirrored (open)"
        if any(c.get("mint") == mint for c in d["closes"]):
            return False, "already mirrored (closed)"

    # Pre-registered cost gate: the productive ETH answer.
    blocked, why = _costs.cost_gate(chain, _N)
    if blocked:
        logger.warning("mirror %s: %s", mint, why)
        return False, why

    extra = extra or {}
    spot_index = extra.get("spot_index")
    _LAST_QUOTE_STAMP = None  # never reuse a previous entry's stamp
    quoted = quote_price(chain, mint, spot_index)
    if quoted <= 0:
        # HL listings carry their mark price from the watcher.
        quoted = float(extra.get("mark_px") or 0)
    # A quote without its own fetch stamp is only as fresh as the watcher's
    # mark stamp; no mark_asof means unknown age (the age gate refuses).
    _ma = extra.get("mark_asof")
    if _LAST_QUOTE_STAMP is None and isinstance(_ma, (int, float)):
        _LAST_QUOTE_STAMP = {"source_asof": float(_ma),
                             "fetched_at": float(_ma),
                             "received_at": float(_ma)}
    _quote_stamp_snap = dict(_LAST_QUOTE_STAMP) if _LAST_QUOTE_STAMP else None  # THIS entry's priced-quote stamp
    if quoted <= 0:
        why = "no live quote"
        if _LAST_QUOTE_DETAIL:
            why = "no live quote (%s)" % _LAST_QUOTE_DETAIL
        return False, why

    ok, why = _veto_ok(chain, mint, liquidity)
    if not ok:
        return False, "veto at entry: %s" % why

    # Venue fee grounding (2026-11-22 fee integration): solana entries
    # classify the venue from the fresh tape's dex label; hyperliquid
    # uses its own taker-fee model; any other chain or an unmodeled dex
    # REFUSES the entry rather than silently booking zero venue fees.
    # (mirror stays deliberately unwired; this keeps its cost model
    # consistent with the live lanes.)
    if (chain or "").lower() == "solana":
        # Classify from the runner gate's own confirmed read (the same
        # _LAST_GATE_TAPE the journaling stamp uses) — NO extra fetch:
        # the consecutive-confirmation gate is the lane's fresh tape.
        fee_venue, venue_why = _costs.lane_dex_venue(
            chain, (_LAST_GATE_TAPE or {}).get("dex"))
    else:
        fee_venue, venue_why = _costs.lane_dex_venue(chain, None)
    if fee_venue is None:
        return False, venue_why

    # Honest entry: adverse fill vs quoted (fills.py) on the $1 notional
    # minus the chain's per-tx cost.
    gas_in = _costs.tx_cost_usd(chain, _N,
                                venue=fee_venue or None)
    eff_notional = _N - gas_in
    if eff_notional <= 0:
        return False, "costs exceed notional"

    # The fill and the slippage gate use the lane's OWN gate-verified
    # tape liquidity, never the card's claim (2026-11-23, same fix as
    # ceo): a claimed value can be absent or inflated. The
    # consecutive-confirmation gate already refused no-tape cards.
    gate_liq = float((_LAST_GATE_TAPE or {}).get("usd_liquidity") or 0)
    liquidity = gate_liq or float(liquidity or 0)

    # Declared slippage cap (fills.MAX_SLIPPAGE_PCT, tighten-only via
    # AGT_RISK_MAX_SLIPPAGE_PCT): a fill the model can only price at
    # the cap is not an honest entry. Retryable - liquidity can
    # recover.
    _slip_ok, _slip_why, _ = _risk.check_slippage(
        eff_notional, liquidity or None)
    if not _slip_ok:
        return False, "risk: " + _slip_why
    try:
        filled = _fills.honest_entry(quoted, eff_notional, liquidity or None)
    except Exception:
        filled = quoted
    if filled <= 0:
        return False, "bad fill price"
    tokens = eff_notional / filled
    _exec = None
    _intent_op = None
    if _demo_mode():
        # DEMO execution: the fill is the Jupiter-quote simulation, NEVER the
        # model fill. If it cannot be obtained the entry REFUSES (no silent
        # fallback to modelled P&L).
        _df = _demo_filler()
        if _df is None or _pa is None:
            return False, "demo exec: quote-simulation filler unavailable; entry refused"
        _cap = _pa.max_price_impact_pct()
        try:
            _intent_op = _df.intent_id(lane, mint)   # durable intent; stale/unknown classified from the account
            _prior_fill = _df.get_fill(_intent_op)  # durable journal read BEFORE buy
            _committed = _prior_fill is not None and _df.op_committed(_intent_op) is True  # replay needs account debit evidence
            _fill = _df.buy(_intent_op, mint, _N, _cap)
        except _demo.DemoSkip as _e:
            return False, "demo exec: %s" % _e.reason
        from daemon.launch_scanner import entry_guard as _eg_pq
        _pq = _eg_pq.post_quote_refusal(_fill, _prior_fill, _committed, _pa.max_data_age_s(), time.time())
        if _pq:
            try:
                _df.release_intent(lane, mint)
            except Exception as _re:
                logger.warning("mirror: release_intent failed (%r); a retry may replay the original fill", _re)
            return False, "risk: " + _pq
        tokens = _fill["qty_tokens"]
        filled = _fill["price_usd"]
        gas_in = _fill["fee_usd"]
        _exec = {"source": _fill["execution_source"],
                 "entry_op": _fill["op_id"],
                 "signature": _fill["signature"],
                 "synthetic_signature": _fill["synthetic_signature"],
                 "provenance": _fill["provenance"]}

    # Journaling only: the gate's confirming tape read (real
    # entry-time buys/sells/liquidity/mcap), stamped below into the
    # position's entry_features so every close carries it.
    gate_tape = _gate_tape_stamp()
    if _pa is not None:
        from daemon.launch_scanner import entry_guard as _eg
        _row = (_LAST_GATE_TAPE if (chain or "").lower() == "solana"
                else _LAST_QUOTE_STAMP)
        _age_why = _eg.quote_age_refusal(
            _row, _pa.max_data_age_s(), time.time())
        if _age_why:
            return False, "risk: " + _age_why
        _qage_why = _eg.quote_age_refusal(
            _quote_stamp_snap, _pa.max_data_age_s(), time.time())
        if _qage_why:
            return False, "risk: priced quote: " + _qage_why
    pos = {
        "mint": mint, "symbol": (symbol or "?")[:32], "chain": chain,
        "source": (source or "unknown")[:32], "lane": lane,
        "score": round(float(score or 0), 1), "tier": (tier or "")[:32],
        "entry_ts": time.time(), "quoted_price": quoted,
        "entry_price": filled, "notional_usd": _N,
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
        "fee_venue": fee_venue,
        "spot_index": spot_index, "pair": extra.get("pair", ""),
        "entry_snapshot": {
            "score": round(float(score or 0), 1), "tier": (tier or "")[:32],
            "mcap": float(mcap or 0), "liquidity": float(liquidity or 0),
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, _N),
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
            "cost_summary": _costs.cost_summary(chain, _N),
            "evidence": dict(evidence or {}),
            "gate_tape": gate_tape,
        },
    }
    if _exec is not None:
        pos["execution"] = _exec
    d["positions"][mint] = pos
    with _ledger_lock():
        # Re-check under lock: another feed may have opened it while we
        # were quoting/vetoing (idempotency must hold under concurrency).
        d = _read()
        if mint in d["positions"] or any(
                c.get("mint") == mint for c in d["closes"]):
            return False, "already mirrored (race)"
        if _pa is not None:
            # STABLE per (lane, mint): a mint is mirrored at most once, so a
            # crash between the debit and the ledger persist is retried with
            # the SAME op id and gets the original reservation back (no
            # orphan, no second debit). A timestamp here made every retry a
            # new operation.
            _op = _intent_op or "%s:entry:%s" % (lane, mint)
            _ok, _why, _pid = _pa.try_debit(lane, mint, _N, tokens, _op,
                                            durable_refusal=_demo_mode())
            if not _ok:
                if _intent_op:   # refused before any debit: the next attempt takes a FRESH quote
                    try:
                        _demo_filler().release_intent(lane, mint)
                    except Exception as _re:
                        logger.warning("mirror: release_intent failed (%r); a retry may replay the original (stale) fill", _re)
                return False, "paper account: " + str(_why)
            pos["pa_pos_id"] = _pid
        d["positions"][mint] = pos
        _write(d)
        if _intent_op:
            try:
                _demo_filler().consume_intent(lane, mint)
            except Exception as _ce:
                logger.warning("mirror: consume_intent failed (%r); stale intent dropped on re-entry", _ce)
    logger.warning("mirror OPEN %s %s %s @ %s ($%.2f, %s)",
                   chain, pos["symbol"], mint[:12], filled, _N, lane)
    return True, "opened"


# ---- exits --------------------------------------------------------------

# Exit price provenance (2026-11-22): additive settlement labels.
try:
    from daemon.launch_scanner import exit_provenance as _pk
except Exception:
    try:
        import exit_provenance as _pk  # type: ignore
    except Exception:
        _pk = None  # type: ignore


def _pkind(reason, pkind=None):
    """Resolve the exit price kind for one settlement tranche."""
    if pkind:
        return pkind
    if _pk is None:
        return None
    return _pk.classify_reason(
        reason, getattr(_f2, "F2_REASON", None) if _f2 else None)

def _exit_fee_venue(pos):
    """Venue for the exit fee. The entry-stamped venue wins; a legacy
    position without one is charged the conservative pump maximum tier
    on solana (estimates may hurt, never help). None = the chain's own
    model (hyperliquid taker fee) or no venue kwarg."""
    v = pos.get("fee_venue")
    if v is None and (pos.get("chain") or "").lower() == "solana":
        return "pump"
    return v or None


def _close_leg_impl(pos, leg_key, exit_price, reason, pkind=None):
    """Close one leg at an honest exit. Returns realized USD for the leg."""
    chain = pos["chain"]
    leg = pos["legs"][leg_key]
    if leg["done"]:
        return 0.0
    if not _intent_gate(pos, leg_key):
        return 0.0
    leg_tokens = pos["tokens"] * leg["frac"]
    _kind = _pkind(reason, pkind)
    _sim = (pos.get("execution") or {}).get("source") == "jupiter_quote_sim"
    if _sim and _kind != "write_down_zero":
        # DEMO: the sale goes through the quote simulation. A failed quote leaves
        # the leg PENDING (no invented proceeds); the next poll retries.
        _df = _demo_filler()
        _pa_m = _paper_account()
        if _df is None or _pa_m is None:
            pos["pending_exit"] = {"leg": leg_key, "why": "filler unavailable", "ts": time.time()}
            return 0.0
        try:
            _cap = _pa_m.max_price_impact_pct()
        except Exception:
            _cap = None
        _cap_src = "owner_max_price_impact_pct"
        try:
            _capv = float(_cap) if _cap is not None else 0.0
        except (TypeError, ValueError):
            _capv = 0.0
        if not (_capv > 0):
            _capv, _cap_src = _EXIT_IMPACT_CAP_PCT, "fallback_unset_or_invalid"
        _cap = _capv
        try:
            _fill = _df.sell("%s:exit:%s:%s" % (pos["lane"], pos["mint"], leg_key),
                             pos["mint"], leg_tokens, _cap, meta=_sell_meta(pos))
        except _demo.DemoSkip as _e:
            _hold_not_durable(pos, leg_key, _e.reason, detail=_e.detail)
            logger.warning("mirror demo exit pending %s %s: %s", pos["mint"][:12], leg_key, _e.reason)
            return 0.0
        pos.pop("pending_exit", None)
        filled_exit = _fill["price_usd"]
        gross = _fill["gross_usd"]
        gas_out = _fill["fee_usd"]
        net = gross - gas_out
        _fv = _exit_fee_venue(pos)
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net, 6), exit_reason=reason,
                   exit_price_kind=_kind, fee_venue=_fv or "none",
                   exit_execution={"source": _fill["execution_source"],
                                   "signature": _fill["signature"],
                                   "synthetic_signature": True,
                                   "effective_impact_cap_pct": _cap,
                                   "impact_cap_source": _cap_src,
                                   "provenance": _fill["provenance"]})
        pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
        _pa_settle(pos, leg_tokens, gross, gas_out,
                   "leg:%s" % leg_key, price_kind=_kind)
        return net
    try:
        leg_usd = leg_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd, (pos.get("last_liquidity")
              or pos["entry_snapshot"].get("liquidity")) or None)
    except Exception:
        filled_exit = exit_price
    gross = leg_tokens * filled_exit
    _fv = _exit_fee_venue(pos)
    # A write-down is NOT a sale: no fee on units nobody bought.
    gas_out = (0.0 if _kind == "write_down_zero"
               else _costs.tx_cost_usd(chain, gross, venue=_fv))
    net = gross - gas_out
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason,
               exit_price_kind=_kind,
               fee_venue=_fv or "none")
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    if _kind == "write_down_zero":
        _pa_write_down(pos, reason)
    else:
        _pa_settle(pos, leg_tokens, gross, gas_out,
                   "leg:%s" % leg_key, price_kind=_kind)
    return net


def _close_remaining_impl(pos, exit_price, reason, pkind=None):
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
    if not _intent_gate(pos, "remaining:%s" % "".join(open_legs)):
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
    _kind = _pkind(reason, pkind)
    _fv = _exit_fee_venue(pos)
    # ONE tx for the combined exit; none for a write-down (nothing sold).
    gas_out = (0.0 if _kind == "write_down_zero"
               else _costs.tx_cost_usd(chain, gross, venue=_fv))
    net = gross - gas_out
    for key in open_legs:
        leg = pos["legs"][key]
        share = (pos["tokens"] * leg["frac"]) / total_tokens if total_tokens else 0
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net * share, 6), exit_reason=reason,
                   exit_price_kind=_kind)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    if _kind == "write_down_zero":
        _pa_write_down(pos, reason)
    else:
        _pa_settle(pos, total_tokens, gross, gas_out,
                   "remaining:%s" % "".join(open_legs), price_kind=_kind)
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



def _guard_close(impl, pos, key, *args, meta=None):
    """Run one leg/close implementation. If the close intent could not be made durable AFTER the sale fill (begin raised IntentNotDurable),
    restore the WHOLE position (leg dict objects in place, so poll-loop aliases stay valid) and hold the exit visibly: no account op ran (begin
    precedes it), the lane persists nothing. The journaled sale fill (deterministic op id "<lane>:exit:<mint>:<key>", carrying the exit
    reason/kind in its own record via sell(meta=)) is what the next poll's journal-derived resume settles; the sale is not repeated."""
    _snap = copy.deepcopy(pos); _live_legs = pos.get("legs"); _live_leg = {k: v for k, v in (_live_legs or {}).items()}
    pos["_exit_meta"] = dict(meta or {}, key=key)
    try:
        return impl(*args)
    except Exception as _e:
        _ci = _close_intents()
        if _ci is None or not isinstance(_e, _ci.IntentNotDurable):
            raise
        pos.clear(); pos.update(_snap)
        # keep the ORIGINAL leg dict objects (the poll loop may hold aliases such as la/lb/lc): restore them in place, then re-attach them
        for _k, _lv in _live_leg.items():
            _lv.clear(); _lv.update(pos["legs"][_k]); pos["legs"][_k] = _lv
        if _live_legs is not None:
            _live_legs.clear(); _live_legs.update(pos["legs"]); pos["legs"] = _live_legs
        pos["_exit_meta"] = dict(meta or {}, key=key)
        logger.error("close intent NOT DURABLE (%s): exit %s rolled back in memory (nothing persisted, no account op)", _e, key)
        _hold_not_durable(pos, key)
        return 0.0
    finally:
        pos.pop("_exit_meta", None)


def _close_leg(pos, leg_key, exit_price, reason, pkind=None):
    return _guard_close(_close_leg_impl, pos, leg_key, pos, leg_key, exit_price, reason, pkind,
                        meta={"reason": reason, "pkind": pkind})


def _close_remaining(pos, exit_price, reason, pkind=None):
    return _guard_close(_close_remaining_impl, pos, "remaining", pos, exit_price, reason, pkind,
                        meta={"reason": reason, "pkind": pkind})


def _sell_meta(pos):
    """Exit reason/kind recorded INSIDE the journaled fill (atomic with it) so a later resume knows what the sale was for."""
    _m = pos.get("_exit_meta") or {}
    return {"reason": _m.get("reason"), "pkind": _m.get("pkind"), "key": _m.get("key"), "ts": time.time()} if _m else None


def _hold_not_durable(pos, key, why="close_intent_not_durable", detail=None):
    """Visible, DURABLE hold (pending_exit is persisted by the poll's own write): keeps a pre-existing pending exit in full under "prior", counts the
    tries and the first-held time, flags OPERATOR ATTENTION from the 3rd consecutive try, and marks a RISK exit (stop / time stop / dark-position exit)
    as blocked: a risk exit held by storage failure is never silently waived and the log alone is not the escalation."""
    _p0 = pos.get("pending_exit"); _pe = dict(_p0) if isinstance(_p0, dict) else {}
    _was = _pe.get("why") == why
    if _p0 and not _was:
        _pe["prior"] = {k: v for k, v in _p0.items() if k != "prior"}
    _m = pos.get("_exit_meta") or {}
    _rs = _m.get("reason") or _pe.get("reason") or ""
    _tries = int(_pe.get("tries") or 0) + 1 if _was else 1
    _pe.update({"leg": key, "why": why, "ts": time.time(), "tries": _tries,
                "first_ts": _pe.get("first_ts") if _was else time.time(), "reason": _rs,
                "risk_exit_blocked": bool(any(t in str(_rs) for t in ("stop", "f2", "unquotable", "backstop"))),
                "operator_attention": _tries >= 3})
    if detail is not None:
        _pe["detail"] = detail
    if why == "close_intent_not_durable" or str(why).startswith("unresolved_"):
        # An exit held on UNRESOLVED evidence (storage or an unknown/inflight sale) excludes the position from any qualification, and says how it is
        # resolved: by an operator reconciling the sale record against the fill journal; observed records are never rewritten to clear it.
        _pe["qualification_excluded"] = True
        _pe["resolution"] = "operator: reconcile the sale record against the fill journal; observed evidence is not rewritten to clear this hold"
    pos["pending_exit"] = _pe
    if _pe["risk_exit_blocked"]:
        logger.error("RISK EXIT BLOCKED by close-intent storage failure: %s %s reason=%s tries=%d operator_attention=%s",
                     pos.get("mint"), key, _rs, _tries, _pe["operator_attention"])


def _resume_pending_exit(pos):
    """Poll-start journal reconciliation. A sale fill that is journaled under THIS position's deterministic exit op id while the leg is still open
    is a landed sale the lane never settled (a close intent that was not durable, or a crash between the fill and the lane/account writes). It is
    settled FIRST, before any price or trigger logic and whether or not the trigger still holds, from the journaled fill (its own record carries the
    exit reason/kind). A position with NO journaled fill is never touched here: an unsold gate refusal does not force a sale.
    None = nothing to resume; "held" = still not durable (or an inflight claim, never double-executed); "closed:<reason>" = all legs now done;
    "resumed" = progressed, legs remain."""
    if not pos.get("pa_pos_id") or (pos.get("execution") or {}).get("source") != "jupiter_quote_sim":
        return None
    _df = _demo_filler()
    if _df is None:
        return None
    _open = [k for k in ("a", "b", "c") if not (pos["legs"].get(k) or {}).get("done")]
    if not _open:
        return None
    _lane = pos.get("lane") or globals().get("LANE")
    _cands = [("remaining", "remaining:%s" % "".join(_open))] + [("leg", k) for k in _open]
    for _kind, _key in _cands:
        try:
            _rec = _df.get_fill("%s:exit:%s:%s" % (_lane, pos["mint"], _key))
        except Exception:
            return None
        if not _rec:
            continue
        if _rec.get("status") == "inflight":
            pos["_exit_meta"] = {"reason": "inflight_unresolved", "key": _key}
            try:
                _hold_not_durable(pos, _key, "unresolved_inflight")
            finally:
                pos.pop("_exit_meta", None)
            return "held"
        if _rec.get("status") != "filled":
            continue
        _it = _rec.get("intent") or {}
        _reason = _it.get("reason") or "journaled_fill_resume"
        _pk = _it.get("pkind")
        if _kind == "leg":
            _close_leg(pos, _key, float(_rec.get("price_usd") or 0.0), _reason, _pk)
        else:
            _close_remaining(pos, float(_rec.get("price_usd") or 0.0), _reason, _pk)
        _pe2 = pos.get("pending_exit")
        if isinstance(_pe2, dict) and _pe2.get("why") == "close_intent_not_durable":
            return "held"
        if all((v or {}).get("done") for v in pos["legs"].values()):
            return "closed:%s" % _reason
        return "resumed"
    return None


def _append_close(closes, rec):
    """_finalize returns None when a position is held open (legs not done); only real close records are collected."""
    if rec is not None:
        closes.append(rec)


def _finalize(d, mint, pos, closed_reason):
    """Move an open position to closes, record the lane trade.

    Must be called with _ledger_lock held; writes via _write (no re-lock,
    which would deadlock).
    """
    _recover_close_intents()
    if not all((v or {}).get("done") for v in pos["legs"].values()):
        # An exit leg is still PENDING (filler unavailable or skipped, or the close intent was not durable and the exit was rolled back): never
        # book a close, bankroll, trade record or verdict for a position that still holds unsold units. It stays open; the exit retries next poll.
        pos.setdefault("pending_exit", {"leg": "close", "why": "legs_not_done", "ts": time.time()})
        _write(d)
        return None
    _N = float(pos.get("notional_usd") or NOTIONAL_USD)  # the real entry stake
    pnl = round(pos["realized_usd"] - _N, 4)
    rec = {
        "pa_pos_id": pos.get("pa_pos_id"), "notional_usd": _N,
        "mint": mint, "symbol": pos["symbol"], "chain": pos["chain"],
        "source": pos["source"], "lane": pos["lane"],
        "entry_ts": pos["entry_ts"], "close_ts": time.time(),
        "entry_price": pos["entry_price"],
        "pnl_usd": pnl,
        "closed_reason": closed_reason,
        "legs": {k: {"done": v["done"],
                      "exit_price": v.get("exit_price", 0),
                      "realized_usd": v.get("realized_usd", 0),
                      "exit_reason": v.get("exit_reason", ""),
                      "exit_price_kind": v.get("exit_price_kind")}
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
    rec["exit_price_kind"] = (
        _pk.aggregate(v.get("exit_price_kind")
                      for v in pos["legs"].values() if v.get("done"))
        if _pk is not None else "unknown")
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
                                  / _N, 4),
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
                _rr = _resume_pending_exit(pos)
                if _rr == "held":
                    _write(d)
                    continue
                if _rr and _rr.startswith("closed:"):
                    _append_close(closes, _finalize(d, mint, pos, _rr[7:]))
                    continue
                px = quotes.get(mint, 0)
                if px <= 0:
                    # Reserves refusal provenance (2026-11-22): the
                    # refusal DETAIL is journalled so a schema-gate
                    # refusal is distinguishable from ordinary
                    # darkness in the close record.
                    _ref = refusals.get(mint)
                    if _ref:
                        _jq = pos.setdefault("quote_journal", [])
                        _jq.append({"ts": now, "px": None,
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
                        _append_close(closes, _finalize(
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
                        _append_close(closes, _finalize(
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
                    _append_close(closes, _finalize(d, mint, pos, "hard_stop_-50pct"))
                    continue
                # Time stop: 24h hygiene backstop.
                if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                    _close_remaining(pos, px, "time_stop_24h")
                    _append_close(closes, _finalize(d, mint, pos, "time_stop_24h"))
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
                    _append_close(closes, _finalize(d, mint, pos, "ladder_complete"))
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
