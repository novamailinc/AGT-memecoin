#!/usr/bin/env python3
"""CEO account (2026-10-20): a $100 paper bankroll with real-money discipline.

The user handed the bot a $100 account and CEO authority for two months:
trade the fired-card bus like it's real money, because this ledger is the
evidence base for any future live-mode decision. So the discipline is
real even though the money is paper:

- Bankroll starts at exactly $100.00. NO resets, NO top-ups, ever.
- Every entry is exactly $1.00 of bankroll (the user's $1/shot rule),
  debited from cash at entry; leg proceeds return to cash only when the
  position fully closes (cash settles at finalize).
- If cash < $1.00 the account stops entering: state "bankroll_exhausted".
  Losses are final — the record is never rewritten.
- Honest fills (fills.py) and per-chain tx costs (chain_costs.py) are
  charged exactly like the mirror, including the pre-registered 25%
  cost gate — Ethereum is COST-BLOCKED at $1 notional, and that is a
  finding of this account, not a bug.
- Exits are the user's ladder, identical constants to mirror.py:
  50% @ 2.0x, 25% @ 3.0x, final 25% on a 30% trailing stop, -50% hard
  stop, 24h time stop (unquotable past 24h finalizes at 0).

Entries come from the signal bus (signal_bus.py schema v1): every card
fired by any feed publishes there. The daemon tails the bus from a
byte-offset watermark persisted in the ledger, plus a bounded set of
processed line hashes so a bus rotation (the bus is bounded and
rewritten by publish()) can never replay an old card into a new entry.

Killswitch lane "ceo": pre-registered halt — negative rolling expectancy
after 15 closes halts entries (same rule as the mirror lanes). A halt
stops entries only; open positions keep exiting and the bus is untouched.

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: stdlib HTTP (DexScreener, Hyperliquid public APIs).
"""
import hashlib
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.ceo")

try:
    from daemon.launch_scanner import fills as _fills
    from daemon.launch_scanner import chain_costs as _costs
    from daemon.launch_scanner import killswitch as _ks
    from daemon.launch_scanner import veto as _veto
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import signal_bus as _bus
    from daemon.launch_scanner import sources as _sources
    _HAS_DEPS = True
except Exception:
    try:
        import fills as _fills
        import chain_costs as _costs
        import killswitch as _ks
        import veto as _veto
        import safety as _safety
        import signal_bus as _bus
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

# Jupiter Price v3 for exit pricing (2026-11-17): fail-soft import —
# a missing module leaves the pre-Jupiter behavior byte-identical.
# See jup_price.py: free, no key, batchable; graduated tokens only.
try:
    from daemon.launch_scanner import jup_price as _jup
except Exception:
    try:
        import jup_price as _jup  # type: ignore
    except Exception:
        _jup = None  # type: ignore

# Decision memory (2026-11-17): fail-soft import — a missing
# module changes nothing. Lessons are advisory context for CEO
# auto-tune only: ``lessons_context(lane)`` returns the top
# lessons as a text block. They NEVER gate entries or exits.
try:
    from daemon.launch_scanner import lessons as _lessons
except Exception:
    try:
        import lessons as _lessons  # type: ignore
    except Exception:
        _lessons = None  # type: ignore


def lessons_context(lane="grad_select", n=5):
    """Top decision-memory lessons for ``lane`` as a text block,
    for CEO auto-tune to read. Advisory only — never a filter,
    never a gate. Never raises."""
    try:
        if _lessons is not None:
            return _lessons.context_block(lane, n=n)
    except Exception:
        pass
    return "Decision-memory lessons for %s: unavailable." % lane


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
LEDGER_PATH = os.path.join(HOME, "ceo_ledger.json")
BUS_PATH = os.path.join(HOME, "launch_scanner", "signal_bus.jsonl")
MAX_CLOSES = 500
SEEN_TTL_S = 72 * 3600  # processed-line hashes kept 72h (bus holds ~days)

BANKROLL_START = 100.00
NOTIONAL_USD = 1.00       # the user's $1/shot rule


def _paper_account():
    """The shared cross-lane paper account (2026-11-22), or None
    when disabled. Same opt-in and fail-open-import semantics as
    grad's: PAPER_ACCOUNT_ENABLED=1 makes the shared account the
    authoritative capital gate for entries and the settlement
    target for exits; default OFF preserves historical behavior.
    A defensive import failure disables the gate for the call
    rather than breaking the lane."""
    if os.environ.get("PAPER_ACCOUNT_ENABLED") != "1":
        return None
    try:
        from . import paper_account
        return paper_account
    except Exception:
        logger.exception("ceo: paper account unavailable; shared "
                         "gate disabled for this call")
        return None


def _pa_settle(pos, qty, gross, fee, suffix):
    """Credit the shared account for one settlement tranche, when
    this position was opened through it. `qty` units sold for
    `gross` paper dollars with `fee` in fees; the account books
    net proceeds immediately and allocates basis average-cost, so
    partial legs shrink the position's remaining units and basis
    while their receipts land in shared cash (never held outside
    the account, never double-counted at finalize). The op_id is
    stable per tranche (pinned position instance + suffix), so a
    retried close after an uncertain response cannot double-credit.
    Positions without a pinned pa_pos_id (opened with the flag
    off) are skipped: their stake never left the account, and a
    lane+mint match could settle a LATER reopen of the same mint
    after the 24h cooldown. Failures are journaled by the account
    module and never block the lane's own settle."""
    pa_pos_id = pos.get("pa_pos_id")
    if not pa_pos_id:
        return
    _pa = _paper_account()
    if _pa is None:
        return
    mint = pos["mint"]
    _op = "ceo:%s:%s:%s" % (suffix, mint, pa_pos_id)
    _ok, _why = _pa.credit(LANE, mint, gross, _op, pos_id=pa_pos_id,
                           qty=qty, fee=fee)
    if not _ok:
        logger.warning("ceo: shared paper account credit for %s "
                       "(%s) failed: %s (journaled)", mint, suffix,
                       _why)
MAX_OPEN = 10
COOLDOWN_S = 24 * 3600    # a closed mint rests 24h before re-entry
LANE = "ceo"

# The user's ladder — identical constants to mirror.py (post-revert).
LEG_A_FRAC, LEG_A_TARGET = 0.50, 2.0   # sell half at 2x
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # half the remainder at 3x
LEG_C_FRAC, LEG_C_TRAIL = 0.25, 0.30   # final quarter on 30% trailing stop
HARD_STOP_MULT = 0.50                  # hard exit at -50%
TIME_STOP_H = 24                       # hygiene backstop

CHAINS = ("solana", "base", "ethereum", "hyperliquid")
DS_CHAIN = {"solana": "solana", "base": "base", "ethereum": "ethereum"}
HL_INFO = "https://api.hyperliquid.xyz/info"

# Farmed-pack identification (CEO review 2026-10-21): the 6-wallet
# single-operator pack in the smart-wallet registry (labels
# grad-sniper-1/2/3, pack-sniper-1/2/3) sourced 13 of the CEO account's
# first 17 entries, and every one of those coins died 10-30 minutes
# after the card fired (registry record for all six wallets: 0 wins).
# This began as a hard exclusion; the user's order later the same day
# ("trade the highest signal") superseded it — KIMCHI was a pack card
# AND the day's top signal. The pack set is now used for LOGGING only:
# the entry decision belongs to the runner gate (_runner_gate), which
# judges the live tape instead of the wallet name.
_PACK_PREFIXES = frozenset(
    {"Fn9n3f", "Fs9RN3", "27HFmP", "3KcPSJ", "5zWxvq", "7HVGmP"})


def _is_pack_convergence(sig):
    """True when a smart-watcher convergence card names a pack wallet.

    smart-watcher publishes evidence.wallet_names as a comma-joined string
    of 6-char wallet prefixes (scripts/smart-watcher). Never raises.
    """
    try:
        ev = sig.get("evidence") or {}
        names = str(ev.get("wallet_names") or "")
        parts = {p.strip() for p in names.replace(",", " ").split()}
        return bool(parts & _PACK_PREFIXES)
    except Exception:
        return False


# Runner gate at entry (user order 2026-10-21, supersedes the pack
# exclusion above as the ENTRY rule): the CEO account's day-1 bleed came
# from entering convergence cards at birth size — the coins rugged in
# minutes, before any exit rung could matter. The pack exclusion tried
# to fix that by wallet name, but it also blocks the pack's genuine
# runners (KIMCHI: pack-bought at graduation, carded at $6.05M mcap /
# ~$320k liq, +2.3x after the card) — and the user's standing order is
# to trade the highest signals. The gate judges the TAPE, not the name:
# a Solana card is entered only when the LIVE tape at entry moment is
# runner-grade per the standing rule ($25k liq / $50k mcap / rising
# 5m). Pack cards that reach runner grade are admitted and logged, so
# the ledger can judge pack-at-grade on its own record.
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
# read blocks exactly like a failed first read.
RUNNER_CONFIRM_READS = 2
RUNNER_CONFIRM_GAP_S = 5.0


def _runner_gate_check(mint):
    """One runner-grade read of the live tape. Returns the bare failure
    reason ("" when the tape is runner-grade right now)."""
    global _LAST_GATE_TAPE
    st = _fresh_tape("solana", mint)
    # Journaling only: retain the tape this gate read so an entry can
    # stamp the REAL entry-time tape values onto its position. Some
    # card sources publish placeholder entry fields (smart-watcher
    # cards carry a constant score and no buys/sells/age at all), so
    # the tape the lane itself measured is the only real record.
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
    """(ok, why). Fail-closed on unknown tape. Solana only: the bleed
    was Solana birth-size entries; other chains keep their existing
    gates (feeds enforce the rule at fire time, ETH is cost-blocked).

    Tradeoff, stated plainly: the confirmation read BLOCKS this caller
    for ~RUNNER_CONFIRM_GAP_S on the pass path. ceo-trader consumes
    sparse bus cards serially (~100/day), so the queue cost is nil and
    the +5s re-verification is the entire point. (A deferred/async
    confirm queue would restructure the bus loop for no gain here.)"""
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
    logger.warning("ceo runner gate confirmed x%d %s%s",
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
# Same inter-process locking pattern as mirror.py (audit 2026-10-18):
# every read-modify-write section holds an exclusive flock on a lock file
# next to the ledger, and writes are atomic tmp+rename. The lock path is
# derived from LEDGER_PATH at call time so tests can isolate the ledger.

import fcntl


def _lock_path():
    return LEDGER_PATH + ".lock"


class _ledger_lock:
    """Exclusive inter-process lock spanning a read-modify-write section."""

    def __enter__(self):
        self._lf = open(_lock_path(), "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
        finally:
            self._lf.close()
        return False


def _blank():
    return {
        "bankroll_start": BANKROLL_START,
        "bankroll": BANKROLL_START,
        "positions": {},
        "closes": [],
        "created_ts": time.time(),
        "state": "active",
        "watermark_offset": 0,
        "seen_cards": {},
    }


def _read():
    """Bare ledger read. Hold _ledger_lock if you will modify+write."""
    try:
        with open(LEDGER_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict):
            b = _blank()
            for k, v in b.items():
                d.setdefault(k, v)
            if not isinstance(d.get("positions"), dict):
                d["positions"] = {}
            if not isinstance(d.get("closes"), list):
                d["closes"] = []
            if not isinstance(d.get("seen_cards"), dict):
                d["seen_cards"] = {}
            return d
    except Exception:
        pass
    return _blank()


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


def ensure_ledger():
    """Create the ledger on first run. The watermark starts at the
    CURRENT bus end: the CEO account trades cards fired from now on,
    never the historical backlog. Returns the ledger dict."""
    with _ledger_lock():
        if os.path.exists(LEDGER_PATH):
            return _read()
        d = _blank()
        try:
            d["watermark_offset"] = os.path.getsize(BUS_PATH)
        except Exception:
            d["watermark_offset"] = 0
        _write(d)
        logger.warning("ceo ledger created: bankroll $%.2f, bus watermark "
                       "at byte %d", d["bankroll"], d["watermark_offset"])
        return d


def account_state():
    """Read-only summary for reports/tests. Never raises."""
    try:
        d = _load()
        return {
            "bankroll": d["bankroll"],
            "bankroll_start": d["bankroll_start"],
            "state": d["state"],
            "n_open": len(d["positions"]),
            "n_closes": len(d["closes"]),
            "created_ts": d["created_ts"],
        }
    except Exception:
        return {}


# ---- price feeds (same routes as mirror.py) ------------------------------

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
# skip names what was tried and what each lookup returned.
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
    # tokens/v1/<chain>/<mint> — the /latest/dex/tokens/ route drops
    # through this VM's egress proxy (same note as mirror.py).
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
            logger.warning("ceo quote via tape fallback %s",
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


def _is_graduated_solana(pos):
    """True only for a Solana position known to be a graduated token.

    Jupiter Price v3 lists graduated (Raydium/PumpSwap) tokens; a
    pre-graduation bonding-curve token has no v3 entry, so v3 must
    never be tried for it — the Helius curve/reserves path stays
    the fallback there. Markers, in order: an explicit graduated
    flag, a graduation block in the entry snapshot (the grad lanes'
    shape), or a graduation source. Anything else is NOT treated
    as graduated and keeps the pre-Jupiter path byte-identical.
    """
    try:
        if (pos.get("chain") or "solana") != "solana":
            return False
        if pos.get("graduated") is True:
            return True
        snap = pos.get("entry_snapshot") or {}
        if isinstance(snap, dict) and snap.get("graduation"):
            return True
        return (pos.get("source") or "") == "graduation"
    except Exception:
        return False


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


# ---- entry ---------------------------------------------------------------

def _veto_ok(chain, mint):
    """Re-verify hard vetoes at entry (same semantics as mirror.py):
    fail-open on exception (vetoes only on positive evidence),
    fail-CLOSED on unknown EVM safety."""
    try:
        if chain == "solana":
            # run_bundle=False: the card pipeline ran the full safety
            # suite seconds ago; re-running the RPC walk here would stall
            # on a slow (not failing) RPC (audit 2026-10-18).
            rep = _veto.veto_check(mint, "solana", run_bundle=False,
                                   lane="ceo")
            return bool(rep.get("passed")), "; ".join(rep.get("vetoes", []))
        if chain in ("base", "ethereum"):
            rep = _safety.check_token(chain, mint, timeout=10)
            if rep.get("unknown"):
                return False, "safety unknown at ceo entry"
            if not rep.get("passed"):
                return False, "; ".join(rep.get("fails", []))[:120]
            return True, ""
        if chain == "hyperliquid":
            return True, ""  # exchange-settled; no contract veto applies
        return False, "unsupported chain"
    except Exception as e:
        return True, "veto unavailable (%s)" % e


def _skip(mint, why):
    logger.warning("ceo SKIP %s: %s", (mint or "?")[:16], why)
    return False, why


def consider_card(sig):
    """Decide one bus card event. Opens a $1 position from the bankroll
    or logs the skip reason. Returns (opened, reason). Never raises."""
    try:
        return _consider_card(sig)
    except Exception as e:
        logger.warning("ceo consider_card failed: %s", e)
        return False, "ceo error: %s" % e


def _consider_card(sig):
    if not _HAS_DEPS:
        return _skip(sig.get("mint"), "ceo deps missing")
    chain = (sig.get("chain") or "").lower()
    mint = sig.get("mint") or ""
    source = sig.get("source") or "unknown"
    if chain not in CHAINS:
        return _skip(mint, "unsupported chain %s" % chain)
    if not mint:
        return _skip(mint, "no mint")

    # Killswitch halt stops entries only — never the bus, never exits.
    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state error.
    if not _ks.gate(LANE):
        return _skip(mint, "ceo lane halted by killswitch")

    # Runner gate (2026-10-21): Solana entries must be runner-grade on
    # the live tape RIGHT NOW — this replaces the farmed-pack exclusion
    # as the entry rule (see _runner_gate). Pack cards at runner grade
    # are admitted and logged for their own ledger record.
    ok_gate, why_gate = _runner_gate(chain, mint)
    if not ok_gate:
        return _skip(mint, why_gate)
    if _is_pack_convergence(sig):
        logger.warning("ceo pack card admitted at runner grade: %s %s",
                       sig.get("symbol"), mint)

    # Fast dedupe/state checks on a locked snapshot (authoritative
    # re-check happens under the write lock below).
    with _ledger_lock():
        d = _read()
        state = d["state"]
        cash = float(d["bankroll"])
        if state == "bankroll_exhausted" or cash < NOTIONAL_USD:
            if state != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)  # lock held here; the flip is final, like losses
            return _skip(mint, "bankroll exhausted (cash $%.2f)" % cash)
        if mint in d["positions"]:
            return _skip(mint, "already open")
        for c in d["closes"]:
            if c.get("mint") == mint and \
                    time.time() - float(c.get("close_ts") or 0) < COOLDOWN_S:
                return _skip(mint, "closed within 24h cooldown")
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d)" % MAX_OPEN)

    # Pre-registered cost gate (the productive ETH answer).
    blocked, why = _costs.cost_gate(chain, NOTIONAL_USD)
    if blocked:
        logger.warning("ceo %s: %s", mint, why)
        return False, why

    extra = sig.get("extra") or {}
    spot_index = extra.get("spot_index")
    quoted = quote_price(chain, mint, spot_index)
    if quoted <= 0:
        # HL listings carry their mark price on the card event.
        quoted = float(extra.get("mark_px") or 0)
    if quoted <= 0:
        why = "no live quote"
        if _LAST_QUOTE_DETAIL:
            why = "no live quote (%s)" % _LAST_QUOTE_DETAIL
        return _skip(mint, why)

    ok, why = _veto_ok(chain, mint)
    if not ok:
        return _skip(mint, "veto at entry: %s" % why)

    liquidity = float(sig.get("liquidity") or 0)
    gas_in = _costs.tx_cost_usd(chain, NOTIONAL_USD)
    eff_notional = NOTIONAL_USD - gas_in
    if eff_notional <= 0:
        return _skip(mint, "costs exceed notional")
    try:
        filled = _fills.honest_entry(quoted, eff_notional, liquidity or None)
    except Exception:
        filled = quoted
    if filled <= 0:
        return _skip(mint, "bad fill price")
    tokens = eff_notional / filled

    evidence = sig.get("evidence") or {}
    # Journaling only: the gate's confirming tape read (real
    # entry-time buys/sells/liquidity/mcap) rides into
    # entry_features — for sources whose cards publish placeholder
    # fields it is the only real entry record the close will carry.
    gate_tape = _gate_tape_stamp()
    pos = {
        "mint": mint, "symbol": (sig.get("symbol") or "?")[:32],
        "chain": chain, "source": (source or "unknown")[:32], "lane": LANE,
        "score": round(float(sig.get("score") or 0), 1),
        "tier": (sig.get("tier") or "")[:32],
        "entry_ts": time.time(), "quoted_price": quoted,
        "entry_price": filled, "notional_usd": NOTIONAL_USD,
        # Position-level peak over poll-observed prices, seeded with
        # the entry fill. Persisted on closes as peak_mult so
        # round-tripper leakage is measurable (added 2026-10-20,
        # mirroring mirror.py — closes previously carried no peak).
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
            "score": round(float(sig.get("score") or 0), 1),
            "tier": (sig.get("tier") or "")[:32],
            "mcap": float(sig.get("mcap") or 0),
            "liquidity": liquidity,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
            "evidence": dict(evidence),
        },
        "entry_features": {
            "chain": chain, "source": (source or "unknown")[:32],
            "score": round(float(sig.get("score") or 0), 1),
            "tier": (sig.get("tier") or "")[:32],
            "mcap": float(sig.get("mcap") or 0),
            "liquidity": liquidity,
            "card_ts": float(sig.get("ts") or 0),
            "quoted_price": quoted, "filled_price": filled,
            "age_min": float(sig.get("age_min") or 0),
            "buys": int(sig.get("buys") or 0),
            "sells": int(sig.get("sells") or 0),
            "gate_tape": gate_tape,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
        },
    }

    # Authoritative section: dedupe + bankroll + cap re-checked under
    # the same lock that debits cash and inserts the position — a crash
    # or a racing cycle can never double-enter or overdraw.
    with _ledger_lock():
        d = _read()
        if d["state"] == "bankroll_exhausted" or \
                float(d["bankroll"]) < NOTIONAL_USD:
            if float(d["bankroll"]) < NOTIONAL_USD and \
                    d["state"] != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)
            return _skip(mint, "bankroll exhausted (cash $%.2f)"
                         % float(d["bankroll"]))
        if mint in d["positions"]:
            return _skip(mint, "already open (race)")
        for c in d["closes"]:
            if c.get("mint") == mint and \
                    time.time() - float(c.get("close_ts") or 0) < COOLDOWN_S:
                return _skip(mint, "closed within 24h cooldown (race)")
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d) (race)" % MAX_OPEN)
        # Shared cross-lane paper account (2026-11-22): when enabled
        # it is the AUTHORITATIVE capital gate — the entry happens
        # only if the shared cash covers the stake, so two lanes can
        # never spend the same dollars. Lock order is lane-ledger ->
        # account (the account never calls back), same as grad; the
        # debit lands immediately before the position insert, which
        # cannot fail. Fail-closed and transient: cash freed by a
        # close re-enables entries. The op_id keys on the pool's
        # durable identity (mint + entry ts) so a retry of an
        # uncertain response returns the original outcome instead of
        # double-spending; a legitimate REOPEN after the 24h cooldown
        # is a new instance with a new entry ts.
        _pa = _paper_account()
        if _pa is not None:
            _op = "ceo:entry:%s:%d" % (mint,
                                       int(pos["entry_ts"] * 1000))
            _ok, _why, _pid = _pa.try_debit(LANE, mint, NOTIONAL_USD,
                                            tokens, _op)
            if not _ok:
                return _skip(mint, "paper account: " + _why)
            # Pin the open instance so no close can settle a later
            # reopen of the same mint.
            pos["pa_pos_id"] = _pid
        d["bankroll"] = round(float(d["bankroll"]) - NOTIONAL_USD, 6)
        d["positions"][mint] = pos
        _write(d)
    logger.warning("ceo OPEN %s %s %s @ %s ($1, cash now $%.2f)",
                   chain, pos["symbol"], mint[:12], filled,
                   account_cash())
    return True, "opened"


def account_cash():
    try:
        return float(_load()["bankroll"])
    except Exception:
        return 0.0


# ---- bus tail --------------------------------------------------------------

def _line_key(line):
    return hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:16]


def process_bus():
    """Consume new bus lines since the persisted watermark.

    Returns {"lines": n_new, "opened": n, "skipped": n}. The watermark
    (byte offset) advances past every consumed line; processed line
    hashes are kept 72h so a bus rewrite/rotation replays nothing.
    Never raises."""
    out = {"lines": 0, "opened": 0, "skipped": 0}
    try:
        return _process_bus(out)
    except Exception as e:
        logger.warning("ceo process_bus failed: %s", e)
        return out


def _process_bus(out):
    ensure_ledger()
    with _ledger_lock():
        d = _read()
        offset = int(d.get("watermark_offset") or 0)
        seen = dict(d.get("seen_cards") or {})
    try:
        size = os.path.getsize(BUS_PATH)
    except Exception:
        return out
    if size < offset:
        # Bus was bounded/rewritten smaller — rescan from 0; the seen
        # hashes + position/cooldown dedupe make replays harmless.
        logger.warning("ceo bus shrank (%d < %d); rescanning from 0",
                       size, offset)
        offset = 0
    if size == offset:
        return out
    with open(BUS_PATH, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    text = chunk.decode("utf-8", "replace")
    lines = text.split("\n")
    # Last element is "" when the chunk ends with a newline (complete);
    # otherwise it is a partial line — hold it back for the next cycle.
    complete = lines[:-1]
    consumed_bytes = 0
    new_seen = {}
    for line in complete:
        consumed_bytes += len(line.encode("utf-8", "replace")) + 1
        if not line.strip():
            continue
        out["lines"] += 1
        key = _line_key(line)
        if key in seen or key in new_seen:
            out["skipped"] += 1
            continue
        new_seen[key] = time.time()
        try:
            sig = json.loads(line)
        except Exception:
            out["skipped"] += 1
            continue
        if _HAS_DEPS:
            ok_v, errs = _bus.validate(sig)
            if not ok_v:
                logger.warning("ceo SKIP bus line (invalid: %s)",
                               ",".join(errs))
                out["skipped"] += 1
                continue
        opened, _why = consider_card(sig)
        if opened:
            out["opened"] += 1
        else:
            out["skipped"] += 1
    # Persist watermark + seen (pruned) even when nothing opened.
    with _ledger_lock():
        d = _read()
        d["watermark_offset"] = offset + consumed_bytes
        merged = dict(d.get("seen_cards") or {})
        merged.update(new_seen)
        cutoff = time.time() - SEEN_TTL_S
        d["seen_cards"] = {k: v for k, v in merged.items() if v >= cutoff}
        _write(d)
    return out


# ---- exits (the user's ladder, mirror-identical constants) -----------------

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
            exit_price, leg_usd,
            # F5 (2026-10-23): exit-time liquidity beats the entry
            # snapshot — pools drain between entry and exit.
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = leg_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(chain, gross)
    net = gross - gas_out
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    _pa_settle(pos, leg_tokens, gross, gas_out, "leg:%s" % leg_key)
    return net


def _close_remaining(pos, exit_price, reason):
    """Close all remaining legs as ONE combined exit (one tx cost) —
    same rule as mirror.py (audit 2026-10-18)."""
    chain = pos["chain"]
    open_legs = [k for k in ("a", "b", "c") if not pos["legs"][k]["done"]]
    if not open_legs:
        return 0.0
    total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"]
                       for k in open_legs)
    try:
        leg_usd = total_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd,
            # F5 (2026-10-23): exit-time liquidity beats the entry
            # snapshot — pools drain between entry and exit.
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = total_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(chain, gross)  # ONE tx for the combined exit
    net = gross - gas_out
    for key in open_legs:
        leg = pos["legs"][key]
        share = (pos["tokens"] * leg["frac"]) / total_tokens \
            if total_tokens else 0
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net * share, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    _pa_settle(pos, total_tokens, gross, gas_out,
               "remaining:%s" % "".join(open_legs))
    return net


def _peak_fields(pos):
    """Peak price/multiple for the close record, from RECORDED prices
    only — never fabricated. Identical semantics to mirror.py: tracked
    positions report their running poll-observed peak
    (peak_source="tracked"); positions opened before tracking existed
    report the max of their recorded prices (leg C peak + realized
    exit prices) as an honest lower bound
    (peak_source="legacy_lower_bound"); no recorded price at all ->
    nulls with peak_unknown=True.
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
    """Move an open position to closes, settle cash, record the lane
    trade. Must be called with _ledger_lock held; writes via _write."""
    realized = round(float(pos["realized_usd"]), 6)
    pnl = round(realized - NOTIONAL_USD, 4)
    d["bankroll"] = round(float(d["bankroll"]) + realized, 6)
    if d["bankroll"] < NOTIONAL_USD:
        d["state"] = "bankroll_exhausted"
    rec = {
        "mint": mint, "symbol": pos["symbol"], "chain": pos["chain"],
        "source": pos["source"], "lane": LANE,
        "entry_ts": pos["entry_ts"], "close_ts": time.time(),
        "entry_price": pos["entry_price"],
        "realized_usd": realized,
        "realized_mult": round(realized / NOTIONAL_USD, 4),
        "pnl_usd": pnl,
        "closed_reason": closed_reason,
        "legs": {k: {"done": v["done"],
                     "exit_price": v.get("exit_price", 0),
                     "realized_usd": v.get("realized_usd", 0),
                     "exit_reason": v.get("exit_reason", "")}
                 for k, v in pos["legs"].items()},
        "entry_snapshot": pos["entry_snapshot"],
        "entry_features": pos.get("entry_features", {}),
        # F3/P1 (2026-10-23): quote journal tail + exit liquidity.
        "quote_journal": list(pos.get("quote_journal") or [])[-10:],
        "exit_liquidity": pos.get("last_liquidity"),
        # Jupiter v3 audit (2026-11-17): which source priced the
        # exit that closed this position ("jup-v3", "external",
        # "ds/gt", "hl-mark", "f2-last-real-price", "unquotable",
        # or "" for legacy closes).
        "price_source": pos.get("last_price_source") or "",
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
    _write(d)  # lock held by caller
    try:
        _ks.record_trade(LANE, pnl)
        _ks.check_lane(LANE)
    except Exception:
        pass
    logger.warning("ceo CLOSE %s %s pnl=$%.2f realized=%.3fx (%s) "
                   "cash=$%.2f", pos["chain"], pos["symbol"], pnl,
                   rec["realized_mult"], closed_reason, d["bankroll"])
    _vp = {"mint": mint, "pnl_usd": pnl,
           "realized_mult": rec["realized_mult"],
           "closed_reason": closed_reason,
           "closes_count": len(d["closes"])}
    if rec.get("f2"):
        _vp["f2"] = rec["f2"]
    _verdict_event(LANE, "close", _vp)
    return rec


def poll_exits(price_fn=None):
    """Evaluate exits for all open CEO positions. price_fn(chain, mint,
    spot_index) -> float override for tests. Returns close records.
    Never raises."""
    try:
        return _poll_exits(price_fn)
    except Exception as e:
        logger.warning("ceo poll_exits failed: %s", e)
        return []


def _poll_exits(price_fn):
    # Phase 1: snapshot (locked read). Quotes happen outside the lock.
    with _ledger_lock():
        snapshot = {m: p for m, p in _read()["positions"].items()}
    if not snapshot:
        return []
    # Phase 2: fetch quotes (no lock). HL prices reuse mirror's path:
    # one spotMetaAndAssetCtxs fetch per cycle, keyed by spot index.
    # Jupiter v3 (2026-11-17) is the first exit source for GRADUATED
    # Solana tokens only — one batched call for the whole open set,
    # 12s cache in jup_price, no Helius. A caller price_fn still
    # wins when it prices the mint; a mint v3 does not know (a
    # pre-graduation curve token) falls through to the pre-Jupiter
    # path unchanged, including the F2 reserves probe below.
    hl_marks = {}
    if any(p["chain"] == "hyperliquid" for p in snapshot.values()):
        hl_marks = _hl_all_marks()
    jup_prices = {}
    if _jup is not None:
        try:
            jup_prices = _jup.get_prices(
                [m for m, p in snapshot.items()
                 if _is_graduated_solana(p)]) or {}
        except Exception:
            jup_prices = {}
    quotes = {}
    quote_src = {}
    liqs = {}
    for mint, pos in snapshot.items():
        try:
            src = ""
            if price_fn:
                px = float(price_fn(pos["chain"], mint,
                                    pos.get("spot_index")) or 0)
                src = "external"
                if px <= 0 and mint in jup_prices:
                    px = jup_prices[mint]
                    src = _jup.SOURCE
            elif pos["chain"] == "hyperliquid":
                px = float(hl_marks.get(str(pos.get("spot_index")), 0) or 0)
                src = "hl-mark"
            elif mint in jup_prices:
                px = jup_prices[mint]
                src = _jup.SOURCE
            else:
                px = quote_price(pos["chain"], mint)
                src = "ds/gt"
                try:  # F5: fresh liquidity for the exit fill model
                    st = _fresh_tape(pos["chain"], mint)
                    if st and float(st.get("usd_liquidity") or 0) > 0:
                        liqs[mint] = float(st["usd_liquidity"])
                except Exception:
                    pass
            quote_src[mint] = src
        except Exception:
            px = 0.0
            quote_src[mint] = ""
        # F2 cross-probe (2026-10-23): aggregator dark on an ARMED
        # Solana position — before anyone may call it unquotable,
        # the direct pool reserves get their say (the same source
        # grad's phase 2 and rt_sync's pushed pricing already use).
        # A live reserve price makes the position quotable and is
        # evaluated normally; both sources dark is what phase 3's
        # F2 check requires. (With a caller price_fn the probe is
        # unnecessary: rt_sync's price_fn already folds the tape
        # and the pushed reserve price into its 0.)
        if px <= 0 and not price_fn and _f2 is not None \
                and _reserves is not None \
                and (pos.get("chain") or "solana") == "solana" \
                and _f2.is_candidate(pos):
            try:
                rpx, _rsrc = _reserves.price_for_position(
                    dict(pos, mint=mint))
                if rpx > 0:
                    px = rpx
            except Exception:
                pass
        quotes[mint] = px
    # Phase 3: mutate under lock.
    with _ledger_lock():
        d = _read()
        closes = []
        now = time.time()
        for mint, pos in list(d["positions"].items()):
            try:
                px = quotes.get(mint, 0)
                if px <= 0:
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
                        pos["last_price_source"] = "f2-last-real-price"
                        _close_remaining(
                            pos, _f2_stamp["last_real_price"],
                            _f2.F2_REASON)
                        closes.append(_finalize(
                            d, mint, pos, _f2.F2_REASON))
                        continue
                    # Unknown price: skip this cycle — but past the 24h
                    # backstop an unquotable token is dead, so finalize
                    # at 0 (same rule mirror.py adopted 2026-10-19).
                    if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                        pos["last_price_source"] = "unquotable"
                        _close_remaining(pos, 0.0,
                                         "time_stop_24h_unquotable")
                        closes.append(_finalize(
                            d, mint, pos, "time_stop_24h_unquotable"))
                    continue
                entry = pos["entry_price"]
                mult = px / entry if entry > 0 else 0

                # Position-level peak tracking (2026-10-20, same as
                # mirror.py): fold the quote into the running peak
                # BEFORE exits are evaluated; pre-tracking positions
                # are seeded mid-life and flagged partial (their close
                # reports a lower bound, never an invention).
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
                jq.append({"ts": now, "px": px,
                           "src": quote_src.get(mint) or "poll"})
                del jq[:-40]
                pos["last_price_source"] = quote_src.get(mint) or ""

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
                logger.warning("ceo poll_exits %s failed: %s", mint, e)
                continue
        return closes


def open_positions():
    """{mint: position} currently open. Never raises."""
    try:
        return _load()["positions"]
    except Exception:
        return {}
