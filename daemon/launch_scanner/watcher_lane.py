#!/usr/bin/env python3
"""Watcher paper lane — the smart-watcher convergence TRADING ARM
(built 2026-10-23 under the user's order: "make the arm that trade
to be so much more active and working in realtime" + "build
everything with full functionality").

Why this lane exists: the system SIGNALED coins that later ran
2x-40x while no trading arm traded them — the mirror arm was killed
on its verdict (-$24.84) and the CEO arm is killswitch-halted. In
the outcome data, the winners that were measured came overwhelmingly
from ONE source: smart-watcher convergence fires (>= 2 independent
tracked-wallet groups buying the same mint, fired by
scripts/smart-watcher off the logsSubscribe stream wake). That
source only ever fed Telegram cards. This lane is its dedicated
paper trading arm — a FRESH trial, not a resurrection of the killed
mirror (different source, different terms, own ledger/verdict).

Intake is EVENT-DRIVEN: scripts/watcher-trader watches the signal
bus (daemon/launch_scanner/signal_bus.py — the file smart-watcher
publishes every convergence fire to) on a ~1s loop and runs the
entry decision in the same cycle the fire lands. No scan timers,
no candidate polling: a fire is either traded within seconds or
journaled in the fire audit with the exact reason it was not.

Terms (user-ordered 2026-10-23):
- $1 paper shots, $100 paper bankroll, NO daily cap (the bankroll
  and the killswitch are the limits — "so much more active").
- MAX_OPEN = 50: high enough to never be the binding constraint in
  practice; the bankroll ($100 / $1) binds first.
- Honest fills INCLUDING venue fees via chain_costs.py (1.25%
  pump.fun / PumpSwap per swap) + the standard 25% round-trip
  cost gate, exactly as grad/mirror/momentum.
- USER LADDER exits: 50% @2x, 25% @3x, final 25% on a 30% trailing
  stop, -50% hard stop, 24h time stop. Exits evaluated by the
  daemon's own loop (~5s), with the F2 sustained-unquotability
  exit wired through the shared f2.check choke point.
- Safety gate fail-CLOSED (safety.check_token, the user's standing
  no-rug order): unknown = no entry. This is a vetted-signal lane,
  NOT a no-gate lane (that is grad_raw's registered design).
- Runner-rule aware: convergence coins are already-running by
  construction (smart-watcher applies its own runner gate before
  firing). At entry this lane re-checks FRESH tape and applies the
  $25k liquidity floor, fail-closed on unknown tape; mcap and
  liquidity are journaled on every entry.
- Dedupe-ever per mint (one entry per mint, like every lane).
- Killswitch lane "watcher" {window:100, min_trades:100,
  halt:True}; pre-registered verdict at 100 closes (expectancy
  <= 0 -> permanent halt, human-only re-enable), stamped in the
  ledger header; closes also append to the tamper-evident verdict
  ledger (grad.py's hook pattern). Results are to be reported raw
  AND ex-top-1 (grad_raw's survival rode one coin — the registry
  note says so; this lane's reporting must not repeat the sin).

DIVERGENCES from momentum_lane (stated, not hidden):
- No veto HARD gate and no vetoed-coin counterfactuals: the task
  terms for this arm name the fail-closed safety gate as the
  vetting; the fire audit (below) records every non-entry reason,
  which is this lane's accountability mechanism instead.
- No mcap floor at entry: the task terms mandate the $25k
  liquidity floor + journaling; the convergence signal itself is
  the entry thesis. (Smart-watcher fired only after its own
  runner gate passed.)
- No spec post-graduation filters (this source has no graduation
  semantics to apply them to).

FIRE AUDIT (the anti-"signal but no trade" mechanism): every
convergence fire the lane sees is journaled in the ledger's
"fire_audit" (bounded): {fire_ts, seen_ts, mint, outcome, reason}
— opened / blocked-final / pending-transient / dropped-stale.
Transient blocks (no tape yet, safety unknown, no quote) are
retried from the "pending" queue for FIRE_TTL_S seconds, then
dropped with their last reason. Nothing fires silently.

Paper only. NO Telegram cards from this lane (smart-watcher
already sends the card; this arm only trades). Nothing here
touches real money, keys, or the user's lanes.
Free-only: DexScreener/GeckoTerminal public endpoints + safety
via the existing modules.
"""
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.watcher_lane")

try:
    from daemon.launch_scanner import fills as _fills
    from daemon.launch_scanner import chain_costs as _costs
    from daemon.launch_scanner import killswitch as _ks
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import sources as _sources
    from daemon.launch_scanner import signal_bus as _bus
    _HAS_DEPS = True
except Exception:
    try:
        import fills as _fills
        import chain_costs as _costs
        import killswitch as _ks
        import safety as _safety
        import sources as _sources
        import signal_bus as _bus
        _HAS_DEPS = True
    except Exception:
        _HAS_DEPS = False

# Trial registry (fail-soft: registration must never break trading).
try:
    from daemon.launch_scanner import trials as _trials
except Exception:
    try:
        import trials as _trials  # type: ignore
    except Exception:
        _trials = None  # type: ignore

# F2 + reserves cross-probe + verdict ledger: fail-soft imports —
# a missing module leaves pre-F2 / pre-chain behavior identical.
try:
    from daemon.launch_scanner import f2 as _f2
    from daemon.launch_scanner import reserves as _reserves
    from daemon.launch_scanner import verdict_ledger as _verdict_ledger
except Exception:
    try:
        import f2 as _f2  # type: ignore
        import reserves as _reserves  # type: ignore
        import verdict_ledger as _verdict_ledger  # type: ignore
    except Exception:
        _f2 = None  # type: ignore
        _reserves = None  # type: ignore
        _verdict_ledger = None  # type: ignore

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "watcher_ledger.json")
MAX_CLOSES = 500
MAX_FIRE_AUDIT = 500
MAX_SEEN_KEYS = 2000

BANKROLL_START = 100.00
NOTIONAL_USD = 1.00       # the user's $1/shot rule
MAX_OPEN = 50              # bankroll binds first by design (see header)
LANE = "watcher"
DEFAULT_CHAIN = "solana"
FIRE_SOURCE = "smart-watcher"

# Runner-rule liquidity floor (standing, 2026-10-18), re-checked at
# entry from FRESH tape, fail-closed on unknown (task terms).
RUNNER_MIN_LIQ_USD = 25_000.0

# A fire whose entry is transiently blocked is retried for this
# long, then dropped (journaled). Convergence itself lives in a
# 30-minute window upstream; 10 minutes of retries keeps the arm
# honest without chasing stale signals.
FIRE_TTL_S = 600.0

# The user's ladder — identical constants to grad/momentum/mirror.
LEG_A_FRAC, LEG_A_TARGET = 0.50, 2.0   # sell half at 2x
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # half the remainder at 3x
LEG_C_FRAC, LEG_C_TRAIL = 0.25, 0.30   # final quarter on 30% trailing stop
HARD_STOP_MULT = 0.50                  # hard exit at -50%
TIME_STOP_H = 24                       # hygiene backstop

# Pre-registered verdict sample (see header + ledger stamp).
VERDICT_SAMPLE_CLOSES = 100

DS_CHAIN = {"solana": "solana", "base": "base", "ethereum": "ethereum"}


def _verdict_event(lane, kind, payload):
    """Append one verdict-ledger record (grad.py's pattern). Never
    raises. The chain path derives from this instance's LEDGER_PATH
    directory (== HOME in production; a temp ledger in tests
    isolates the chain with it)."""
    try:
        if _verdict_ledger is not None:
            _p = os.path.join(
                os.path.dirname(os.path.abspath(LEDGER_PATH)),
                "verdict_ledger", "events.jsonl")
            _verdict_ledger.append(lane, kind, payload, path=_p)
    except Exception:
        pass


# ---- ledger -------------------------------------------------------------
# Same inter-process locking pattern as grad.py/momentum_lane.py:
# every read-modify-write section holds an exclusive flock on a lock
# file next to the ledger; writes are atomic tmp+rename. The lock
# path is derived from LEDGER_PATH at call time so tests isolate it.

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


def _preregistration():
    """The verdict terms, stamped into the ledger header at creation
    so they cannot be quietly changed later."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'watcher'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only. Reporting: raw AND ex-top-1 expectancy."
                 % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,  # none — bankroll-bound by design
            "candidate_source": "smart-watcher convergence fires on "
                                "the signal bus (source "
                                "'smart-watcher', >= 2 independent "
                                "wallet groups), consumed "
                                "event-driven (~1s bus watch)",
            "safety_gate": "fail-closed (safety.check_token)",
            "runner_rule": {"min_liq_usd": RUNNER_MIN_LIQ_USD,
                            "fail_closed_on_unknown_tape": True},
            "veto": "not applied (task terms; the fail-closed "
                    "safety gate is this lane's vetting — divergence "
                    "from momentum_lane, stated in the header)",
            "fire_audit": "every fire journaled with its outcome; "
                          "transient blocks retried for %ds"
                          % int(FIRE_TTL_S),
            "cards": "NONE (smart-watcher already sends the card)",
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
            "f2": "active (shared f2.check choke point)",
        },
        "registered_ts": time.time(),
    }


def _blank():
    return {
        "bankroll_start": BANKROLL_START,
        "bankroll": BANKROLL_START,
        "positions": {},
        "closes": [],
        "created_ts": time.time(),
        "state": "active",
        "preregistered_verdict": _preregistration(),
        "verdict": None,
        "dead_mints": {},     # mint -> {"ts":, "reason":} final skips
        "fire_audit": [],     # every convergence fire + its outcome
        "pending": {},        # mint -> transiently blocked fire
        "bus_seen": [],       # fire keys already consumed (bounded)
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
            if not isinstance(d.get("dead_mints"), dict):
                d["dead_mints"] = {}
            if not isinstance(d.get("fire_audit"), list):
                d["fire_audit"] = []
            if not isinstance(d.get("pending"), dict):
                d["pending"] = {}
            if not isinstance(d.get("bus_seen"), list):
                d["bus_seen"] = []
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
    """Create the ledger on first run (stamps the pre-registered
    verdict terms into its header). The bus floor is the creation
    time: fires published before this lane existed are history, not
    signals for it."""
    with _ledger_lock():
        if os.path.exists(LEDGER_PATH):
            return _read()
        d = _blank()
        _write(d)
        logger.warning("watcher ledger created: bankroll $%.2f, "
                       "verdict pre-registered at %d closes",
                       d["bankroll"], VERDICT_SAMPLE_CLOSES)
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
            "n_pending": len(d.get("pending") or {}),
            "n_fires_audited": len(d.get("fire_audit") or []),
            "verdict": d.get("verdict"),
            "created_ts": d["created_ts"],
        }
    except Exception:
        return {}


def account_cash():
    try:
        return float(_load()["bankroll"])
    except Exception:
        return 0.0


def register_trial():
    """Register this lane in the trial registry (idempotent).
    Never raises."""
    try:
        if _trials is None:
            return None
        pre = _preregistration()
        return _trials.register(
            LANE, "lane", pre["terms"],
            note="watcher lane (2026-10-23 trading-arm build); "
                 "verdict: %s" % pre["rule"])
    except Exception:
        return None


# ---- price feeds ----------------------------------------------------------

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


_LAST_TAPE_SOURCE = ""


def _fresh_tape(chain, mint):
    """Fresh tape stats dict (DexScreener, GeckoTerminal fallback per
    sources.tape_batch_stats). None = unknown. Never raises."""
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


QUOTE_RETRY_GAP_S = 2.0
_LAST_QUOTE_DETAIL = ""


def _ds_pairs_price(pairs):
    """(price, note): price of the highest-liquidity pair that carries
    one — the same selection rule as grad.py/momentum_lane.py."""
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


def _quote_detail(chain, mint):
    """(price, trail): best-pair pairs fetch -> tape fallback -> one
    bounded retry. Trail is plain-language, for skip logs."""
    global _LAST_QUOTE_DETAIL
    ds = DS_CHAIN.get((chain or "").lower())
    if not ds:
        _LAST_QUOTE_DETAIL = "no dexscreener chain for %s" % chain
        return 0.0, _LAST_QUOTE_DETAIL
    url = "https://api.dexscreener.com/tokens/v1/%s/%s" % (ds, mint)
    trail = []
    px, note = _ds_pairs_price(_http_json(url))
    trail.append("pairs fetch: " + note)
    if px <= 0:
        st = _fresh_tape(chain, mint)
        tpx = float((st or {}).get("price_usd") or 0) if st else 0.0
        if tpx > 0:
            trail.append("tape fallback: priced via %s"
                         % ((st or {}).get("tape_source") or "unknown"))
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
    """Live quoted price in USD. 0.0 = no source can price the mint."""
    try:
        return _quote_detail(chain or DEFAULT_CHAIN, mint)[0]
    except Exception:
        return 0.0


# ---- convergence fires ----------------------------------------------------

def _convergence(sig):
    """Extract the convergence facts from a bus signal's evidence.
    Returns (n_groups, wallets, ok): ok only when the evidence
    proves >= 2 independent wallet groups converged — the fire
    definition. Missing evidence is NOT convergence (fail-closed:
    the lane trades the proven signal, not the source label)."""
    ev = sig.get("evidence") or {}
    if not isinstance(ev, dict):
        return 0, [], False
    comp = ev.get("components") or {}
    n = 0
    try:
        n = int(comp.get("convergence_groups") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        groups = ev.get("wallet_groups") or []
        if isinstance(groups, list) and groups:
            n = len([g for g in groups if g])
    if n <= 0:
        try:
            n = int(ev.get("smart_wallets") or 0)
        except (TypeError, ValueError):
            n = 0
    wallets = ev.get("wallets") or []
    if not isinstance(wallets, list):
        wallets = []
    return n, [str(w) for w in wallets], n >= 2


def _is_fire(sig):
    """A bus record is a tradeable convergence fire for this lane
    when: schema-valid, our source, Solana, and convergence proven.
    (Tier 'converged' is what smart-watcher stamps; the evidence
    check is the real gate so a mislabeled record cannot trade.)"""
    if not isinstance(sig, dict):
        return False
    if (sig.get("source") or "") != FIRE_SOURCE:
        return False
    if (sig.get("chain") or "") != DEFAULT_CHAIN:
        return False
    if not sig.get("mint"):
        return False
    _n, _w, ok = _convergence(sig)
    return ok


def _fire_key(sig):
    return "%.3f:%s" % (float(sig.get("ts") or 0), sig.get("mint") or "")


def _audit(d, sig, outcome, reason, now):
    """Journal one fire's outcome. Lock held by caller (d is the
    ledger dict being mutated)."""
    d["fire_audit"].append({
        "fire_ts": float(sig.get("ts") or 0),
        "seen_ts": now,
        "mint": sig.get("mint") or "",
        "symbol": str(sig.get("symbol") or "?")[:32],
        "outcome": outcome,
        "reason": str(reason)[:200],
    })
    d["fire_audit"] = d["fire_audit"][-MAX_FIRE_AUDIT:]


def _read_bus_signals():
    """All signals currently on the bus (oldest first). The bus is
    rewritten (bounded, atomic) by publishers, so consumers re-read
    it whole and dedupe by fire key — offsets are unsafe here.
    Never raises."""
    try:
        path = _bus.BUS_PATH
        with open(path) as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    out = []
    for line in lines:
        try:
            sig = json.loads(line)
        except Exception:
            continue
        if isinstance(sig, dict):
            out.append(sig)
    return out


# ---- entry gates ----------------------------------------------------------

def _liq_gate(chain, mint):
    """(ok, why, tape). $25k liquidity floor from FRESH tape,
    fail-closed on unknown tape."""
    st = _fresh_tape(chain, mint)
    if not st:
        return False, "liq gate: no live tape", None
    liq = float(st.get("usd_liquidity") or 0)
    if liq < RUNNER_MIN_LIQ_USD:
        return False, "liq gate: liq $%.0f < $25k" % liq, st
    return True, "", st


def _safety_ok(chain, mint):
    """(ok, why, report, transient). Fail-closed via
    safety.check_token — unknown = no entry (transient), hard fail
    is final."""
    try:
        rep = _safety.check_token(chain, mint, timeout=10)
    except Exception as e:
        return False, "safety error (%s)" % e, None, True
    if not isinstance(rep, dict) or rep.get("unknown"):
        return False, "safety unknown", rep, True
    if not rep.get("passed"):
        return False, "; ".join(rep.get("fails", []))[:160], rep, False
    return True, "", rep, False


def _mark_dead(mint, reason):
    try:
        with _ledger_lock():
            d = _read()
            d["dead_mints"][mint] = {"ts": time.time(),
                                     "reason": str(reason)[:160]}
            _write(d)
    except Exception:
        pass


def _skip(mint, why, final):
    logger.warning("watcher SKIP %s: %s%s", (mint or "?")[:16], why,
                   "" if final else " (transient — fire stays pending)")
    return False, why, final


def consider_fire(sig, now=None):
    """Decide one convergence fire (a signal-bus dict). Returns
    (opened, reason, final): final=True means never reconsider this
    mint (entered, hard-gated, or already traded); final=False is a
    transient block (the fire stays pending until FIRE_TTL_S).
    Never raises."""
    try:
        return _consider_fire(sig, now)
    except Exception as e:
        logger.warning("watcher consider_fire failed: %s", e)
        return False, "watcher error: %s" % e, False


def _consider_fire(sig, now):
    if not _HAS_DEPS:
        return _skip(str(sig.get("mint") or ""), "watcher deps missing",
                     False)
    now = time.time() if now is None else now
    mint = str(sig.get("mint") or "")
    symbol = str(sig.get("symbol") or "?")[:32]
    chain = DEFAULT_CHAIN
    if not mint:
        return _skip(mint, "no mint", True)
    n_groups, wallets, converged = _convergence(sig)
    if not converged:
        return _skip(mint, "no proven convergence in fire evidence", True)

    # HIGH-2 (2026-11-21): gate() is fail-closed on ANY state
    # error — the old is_halted() escaped malformed state as a
    # KeyError the except: pass swallowed, so entry proceeded.
    if not _ks.gate(LANE):
        return _skip(mint, "watcher lane halted by killswitch", True)

    with _ledger_lock():
        d = _read()
        if float(d["bankroll"]) < NOTIONAL_USD:
            return _skip(mint, "bankroll exhausted (cash $%.2f)"
                         % float(d["bankroll"]), True)
        if mint in d["positions"]:
            return _skip(mint, "already open", True)
        if any(c.get("mint") == mint for c in d["closes"]):
            return _skip(mint, "already traded (one entry per mint "
                               "ever)", True)
        if mint in d["dead_mints"]:
            return _skip(mint, "dead: %s"
                         % d["dead_mints"][mint].get("reason", ""), True)
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d)" % MAX_OPEN,
                         False)

    # Pre-registered cost gate (chain_costs, same as every paper lane).
    blocked, why = _costs.cost_gate(chain, NOTIONAL_USD)
    if blocked:
        logger.warning("watcher %s: %s", mint, why)
        return False, why, True

    # $25k liquidity floor from fresh tape (fail-closed).
    ok_gate, why_gate, st = _liq_gate(chain, mint)
    if not ok_gate:
        return _skip(mint, why_gate, False)

    quoted = quote_price(chain, mint)
    if quoted <= 0:
        why = "no live quote"
        if _LAST_QUOTE_DETAIL:
            why = "no live quote (%s)" % _LAST_QUOTE_DETAIL
        return _skip(mint, why, False)
    liquidity = float((st or {}).get("usd_liquidity") or 0) or \
        float(sig.get("liquidity") or 0)
    mcap = float((st or {}).get("usd_market_cap") or 0) or \
        float(sig.get("mcap") or 0)
    tape_source = (st or {}).get("tape_source") or ""

    # Safety gate — fail-closed, hard fail is final.
    ok, why, rep, transient = _safety_ok(chain, mint)
    if not ok:
        if not transient:
            _mark_dead(mint, "safety: " + why)
        return _skip(mint, "safety: " + why, not transient)

    gas_in = _costs.tx_cost_usd(chain, NOTIONAL_USD)
    eff_notional = NOTIONAL_USD - gas_in
    if eff_notional <= 0:
        return _skip(mint, "costs exceed notional", True)
    try:
        filled = _fills.honest_entry(quoted, eff_notional,
                                     liquidity or None)
    except Exception:
        filled = quoted
    if filled <= 0:
        return _skip(mint, "bad fill price", False)
    tokens = eff_notional / filled

    ev = sig.get("evidence") or {}
    comp = (ev.get("components") or {}) if isinstance(ev, dict) else {}
    fire_ts = float(sig.get("ts") or 0)
    pos = {
        "mint": mint, "symbol": symbol,
        "chain": chain, "source": FIRE_SOURCE, "lane": LANE,
        "score": 0.0, "tier": str(sig.get("tier") or "converged"),
        "entry_ts": time.time(), "quoted_price": quoted,
        "entry_price": filled, "notional_usd": NOTIONAL_USD,
        "peak_price": filled,
        "peak_ts": time.time(),
        "tokens": tokens, "gas_in_usd": round(gas_in, 6),
        "legs": {
            "a": {"frac": LEG_A_FRAC, "target": LEG_A_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "b": {"frac": LEG_B_FRAC, "target": LEG_B_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "c": {"frac": LEG_C_FRAC, "trail": LEG_C_TRAIL,
                  "peak": filled, "done": False, "exit_price": 0,
                  "realized_usd": 0},
        },
        "realized_usd": 0.0,
        "status": "open",
        "spot_index": None,
        "pair": "",
        "entry_snapshot": {
            "score": 0.0, "tier": str(sig.get("tier") or "converged"),
            "mcap": mcap, "liquidity": liquidity,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
            "safety": {
                "score": (rep or {}).get("score", 0),
                "flags": list((rep or {}).get("flags", []))[:6],
            },
        },
        "entry_features": {
            "chain": chain, "source": "smart-watcher-convergence",
            "fire_ts": fire_ts,
            "detection_lag_s": round(now - fire_ts, 3)
            if fire_ts else None,
            "convergence_groups": n_groups,
            "wallets": wallets[:12],
            "sol_spent_total": comp.get("sol_spent_total"),
            "sightings": comp.get("sightings"),
            "safety_score": comp.get("safety_score"),
            "mcap": mcap, "liquidity": liquidity,
            "tape_source": tape_source,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
        },
    }

    # Authoritative section: dedupe + caps + bankroll re-checked
    # under the same lock that debits cash and inserts the position.
    with _ledger_lock():
        d = _read()
        if float(d["bankroll"]) < NOTIONAL_USD:
            if d["state"] != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)
            return _skip(mint, "bankroll exhausted (cash $%.2f)"
                         % float(d["bankroll"]), True)
        if mint in d["positions"]:
            return _skip(mint, "already open (race)", True)
        if any(c.get("mint") == mint for c in d["closes"]):
            return _skip(mint, "already traded (race)", True)
        if mint in d["dead_mints"]:
            return _skip(mint, "dead (race)", True)
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d) (race)"
                         % MAX_OPEN, False)
        d["bankroll"] = round(float(d["bankroll"]) - NOTIONAL_USD, 6)
        d["positions"][mint] = pos
        _write(d)
    logger.warning("watcher OPEN %s %s @ %s ($1, %d groups, liq $%.0f, "
                   "mcap $%.0f, lag %.1fs, cash now $%.2f)",
                   symbol, mint[:12], filled, n_groups, liquidity,
                   mcap, (now - fire_ts) if fire_ts else -1,
                   account_cash())
    return True, "opened", True


# ---- bus consumption (the event-driven intake) ----------------------------

def process_new_fires(now=None, signals=None):
    """One intake cycle: read the bus, decide every convergence
    fire not yet consumed, retry pending transient blocks, drop
    stale ones — journaling EVERY outcome in the fire audit.
    `signals` overrides the bus read (tests). Returns counters
    {fires, opened, blocked, pending, dropped}. Never raises."""
    out = {"fires": 0, "opened": 0, "blocked": 0, "pending": 0,
           "dropped": 0}
    try:
        return _process_new_fires(out, now, signals)
    except Exception as e:
        logger.warning("watcher process_new_fires failed: %s", e)
        return out


def _process_new_fires(out, now, signals):
    now = time.time() if now is None else now
    ensure_ledger()
    with _ledger_lock():
        d0 = _read()
        floor_ts = float(d0.get("created_ts") or now)
        seen = set(d0.get("bus_seen") or [])
        pending0 = dict(d0.get("pending") or {})
    if signals is None:
        signals = _read_bus_signals()

    # Decide new fires (oldest first — the bus file is chronological).
    fresh = []
    for sig in signals:
        if not _is_fire(sig):
            continue
        if float(sig.get("ts") or 0) < floor_ts:
            continue  # predates this lane — history, not a signal
        key = _fire_key(sig)
        if key in seen or sig.get("mint") in pending0:
            continue
        fresh.append((key, sig))

    for key, sig in fresh:
        opened, why, final = consider_fire(sig, now)
        out["fires"] += 1
        with _ledger_lock():
            d = _read()
            d["bus_seen"].append(key)
            d["bus_seen"] = d["bus_seen"][-MAX_SEEN_KEYS:]
            if opened:
                _audit(d, sig, "opened", why, now)
                out["opened"] += 1
            elif final:
                _audit(d, sig, "blocked", why, now)
                out["blocked"] += 1
            else:
                d["pending"][sig["mint"]] = {
                    "fire": sig, "first_ts": now, "last_try": now,
                    "tries": 1, "last_reason": why}
                _audit(d, sig, "pending", why, now)
                out["pending"] += 1
            _write(d)

    # Retry / expire pending fires.
    with _ledger_lock():
        pend = dict(_read().get("pending") or {})
    for mint, rec in pend.items():
        sig = rec.get("fire") or {}
        age = now - float(rec.get("first_ts") or now)
        if age > FIRE_TTL_S:
            with _ledger_lock():
                d = _read()
                if mint in d["pending"]:
                    del d["pending"][mint]
                    _audit(d, sig, "dropped_stale",
                           "still blocked after %ds: %s"
                           % (int(FIRE_TTL_S),
                              rec.get("last_reason", "")), now)
                    _write(d)
            out["dropped"] += 1
            continue
        opened, why, final = consider_fire(sig, now)
        with _ledger_lock():
            d = _read()
            if mint not in d["pending"]:
                continue
            if opened:
                del d["pending"][mint]
                _audit(d, sig, "opened", "opened on retry", now)
                out["opened"] += 1
            elif final:
                del d["pending"][mint]
                _audit(d, sig, "blocked", why, now)
                out["blocked"] += 1
            else:
                d["pending"][mint]["last_try"] = now
                d["pending"][mint]["tries"] = \
                    int(d["pending"][mint].get("tries") or 1) + 1
                d["pending"][mint]["last_reason"] = why
            _write(d)
    return out


# ---- exits (the user's ladder; F2 wired via the shared choke point) -------

def _close_leg(pos, leg_key, exit_price, reason):
    """Close one leg at an honest exit. Returns realized USD."""
    leg = pos["legs"][leg_key]
    if leg["done"]:
        return 0.0
    leg_tokens = pos["tokens"] * leg["frac"]
    try:
        leg_usd = leg_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd,
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = leg_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(pos["chain"], gross)
    net = gross - gas_out
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _close_remaining(pos, exit_price, reason):
    """Close all remaining legs as ONE combined exit (one tx cost) —
    same rule as grad.py/momentum_lane.py."""
    open_legs = [k for k in ("a", "b", "c") if not pos["legs"][k]["done"]]
    if not open_legs:
        return 0.0
    total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"]
                       for k in open_legs)
    try:
        leg_usd = total_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd,
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = total_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(pos["chain"], gross)
    net = gross - gas_out
    for key in open_legs:
        leg = pos["legs"][key]
        share = (pos["tokens"] * leg["frac"]) / total_tokens \
            if total_tokens else 0
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net * share, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _peak_fields(pos):
    """Peak price/multiple for the close record, from RECORDED prices
    only — never fabricated (grad/mirror semantics)."""
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
            "peak_ts": pos.get("peak_ts"),
            "peak_source": ("legacy_lower_bound"
                            if pos.get("peak_partial") else "tracked")}


def _verdict_check(d):
    """The PRE-REGISTERED verdict, evaluated in-ledger at the moment
    the sample completes (grad.py semantics). Mutates d (lock held
    by caller); returns True when the lane must halt. Runs once.
    The record carries raw AND ex-top-1 expectancy (task terms)."""
    if d.get("verdict"):
        return False
    closes = d.get("closes") or []
    if len(closes) < VERDICT_SAMPLE_CLOSES:
        return False
    pnls = [float(c.get("pnl_usd") or 0) for c in closes]
    exp = sum(pnls) / len(pnls)
    top = max(pnls) if pnls else 0.0
    rest = [p for p in pnls if p != top] or pnls[:-1]
    exp_ex_top1 = (sum(rest) / len(rest)) if rest else exp
    halt = exp <= 0
    d["verdict"] = {
        "ts": time.time(),
        "n_closes": len(closes),
        "expectancy": round(exp, 6),
        "expectancy_ex_top1": round(exp_ex_top1, 6),
        "net_usd": round(sum(pnls), 4),
        "action": ("HALT — sample expectancy <= 0" if halt else
                   "SURVIVED — sample expectancy > 0"),
        "sample_closes": VERDICT_SAMPLE_CLOSES,
    }
    logger.warning("watcher VERDICT at %d closes: expectancy %.4f "
                   "(ex-top-1 %.4f) -> %s", len(closes), exp,
                   exp_ex_top1, d["verdict"]["action"])
    return halt


def _finalize(d, mint, pos, closed_reason):
    """Move an open position to closes, settle cash, record the lane
    trade, evaluate the pre-registered verdict, append to the
    tamper-evident verdict ledger. Lock held by caller."""
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
        "quote_journal": list(pos.get("quote_journal") or [])[-10:],
        "exit_liquidity": pos.get("last_liquidity"),
    }
    rec.update(_peak_fields(pos))
    # F2 stamp (2026-10-23): pos["_f2"] is set ONLY by the F2 exit
    # branch — its presence is the unmistakable marker, and it is
    # nested into the verdict-ledger close payload as well.
    if pos.get("_f2"):
        rec["f2"] = dict(pos["_f2"])
    d["closes"].append(rec)
    d["closes"] = d["closes"][-MAX_CLOSES:]
    del d["positions"][mint]
    had_verdict = bool(d.get("verdict"))
    halt_verdict = _verdict_check(d)
    _write(d)  # lock held by caller
    try:
        _ks.record_trade(LANE, pnl)
        _ks.check_lane(LANE)
        if halt_verdict:
            _ks.halt_lane(
                LANE,
                "pre-registered verdict: expectancy %.4f <= 0 over "
                "%d closes" % (d["verdict"]["expectancy"],
                               d["verdict"]["n_closes"]))
    except Exception:
        pass
    _vp = {"mint": mint, "pnl_usd": pnl,
           "realized_mult": rec["realized_mult"],
           "closed_reason": closed_reason,
           "closes_count": len(d["closes"])}
    if rec.get("f2"):
        _vp["f2"] = rec["f2"]
    _verdict_event(LANE, "close", _vp)
    if not had_verdict and d.get("verdict"):
        _verdict_event(LANE, "verdict", {
            "closes": d["verdict"].get("n_closes"),
            "expectancy": d["verdict"].get("expectancy"),
            "expectancy_ex_top1": d["verdict"].get(
                "expectancy_ex_top1"),
            "decision": d["verdict"].get("action")})
    logger.warning("watcher CLOSE %s %s pnl=$%.2f realized=%.3fx "
                   "(%s) cash=$%.2f", pos["chain"], pos["symbol"], pnl,
                   rec["realized_mult"], closed_reason, d["bankroll"])
    return rec


def poll_exits(price_fn=None):
    """Evaluate exits for all open watcher positions. price_fn(chain,
    mint, spot_index) -> float override for tests. Returns close
    records. Never raises."""
    try:
        return _poll_exits(price_fn)
    except Exception as e:
        logger.warning("watcher poll_exits failed: %s", e)
        return []


def _poll_exits(price_fn):
    with _ledger_lock():
        snapshot = {m: p for m, p in _read()["positions"].items()}
    if not snapshot:
        return []
    quotes = {}
    liqs = {}
    for mint, pos in snapshot.items():
        try:
            if price_fn:
                px = float(price_fn(pos["chain"], mint,
                                    pos.get("spot_index")) or 0)
            else:
                px = quote_price(pos["chain"], mint)
                try:
                    st = _fresh_tape(pos["chain"], mint)
                    if st and float(st.get("usd_liquidity") or 0) > 0:
                        liqs[mint] = float(st["usd_liquidity"])
                except Exception:
                    pass
        except Exception:
            px = 0.0
        # F2 cross-probe (2026-10-23): aggregator dark on an ARMED
        # Solana position — the direct pool reserves get their say
        # before anyone may call it unquotable. Watcher positions
        # carry no pair (the bus signal has none), so the probe
        # resolves what it can from the position and fails soft.
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
    with _ledger_lock():
        d = _read()
        closes = []
        now = time.time()
        for mint, pos in list(d["positions"].items()):
            try:
                px = float(quotes.get(mint, 0) or 0)
                if px <= 0:
                    # F2 (2026-10-23): an ARMED position dark across
                    # every source for a sustained stretch exits at
                    # its last real observed price, stamped — instead
                    # of waiting for the first post-rug dust print.
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
                    if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                        _close_remaining(pos, 0.0,
                                         "time_stop_24h_unquotable")
                        closes.append(_finalize(
                            d, mint, pos, "time_stop_24h_unquotable"))
                    continue
                entry = pos["entry_price"]
                mult = px / entry if entry > 0 else 0
                if "peak_price" not in pos:
                    pos["peak_price"] = 0.0
                    pos["peak_partial"] = True
                if px > float(pos.get("peak_price") or 0):
                    pos["peak_price"] = px
                    pos["peak_ts"] = now
                if mint in liqs:
                    pos["last_liquidity"] = liqs[mint]
                jq = pos.setdefault("quote_journal", [])
                jq.append({"ts": now, "px": px, "src": "poll"})
                del jq[:-40]
                # Hard stop first: -50% closes everything immediately.
                if mult <= HARD_STOP_MULT:
                    _close_remaining(pos, px, "hard_stop_-50pct")
                    closes.append(_finalize(d, mint, pos,
                                            "hard_stop_-50pct"))
                    continue
                # Time stop: 24h hygiene backstop.
                if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                    _close_remaining(pos, px, "time_stop_24h")
                    closes.append(_finalize(d, mint, pos,
                                            "time_stop_24h"))
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
                if all(pos["legs"][k]["done"] for k in ("a", "b", "c")):
                    closes.append(_finalize(d, mint, pos,
                                            "ladder_complete"))
                else:
                    _write(d)
            except Exception as e:
                logger.warning("watcher poll_exits %s failed: %s",
                               mint, e)
                continue
        return closes


def open_positions():
    """{mint: position} currently open. Never raises."""
    try:
        return _load()["positions"]
    except Exception:
        return {}
