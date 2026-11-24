#!/usr/bin/env python3
"""Graduation test lane tests (pure logic + injected feeds).

Covers grad.py: the graduation-age/venue filter, instant-safety
fail-closed semantics (hard fail final, unknown retried), veto final,
dedupe (one entry per mint ever), MAX_OPEN + daily caps, the user's
exit ladder, peak journaling, and the PRE-REGISTERED 100-close
verdict (kill at expectancy <= 0, survive at > 0). Plus the new
sources.geckoterminal_new_pools normalizer and the killswitch 'grad'
registration / halt_lane API. No network: every feed is stubbed.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad as G
from daemon.launch_scanner import killswitch as KS
from daemon.launch_scanner import sources as SRC

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- killswitch registration ----------------
check("killswitch: grad lane registered (window/min 100, halt)",
      "grad" in KS.CFG and KS.CFG["grad"]["window"] == 100
      and KS.CFG["grad"]["min_trades"] == 100
      and KS.CFG["grad"]["halt"] is True)
check("grad: verdict sample constant is 100",
      G.VERDICT_SAMPLE_CLOSES == 100)

# halt_lane API on an isolated killswitch state file
_ks_tmp = tempfile.mkdtemp(prefix="grad-ks-")
KS.STATE_DIR = _ks_tmp
KS.STATE_FILE = os.path.join(_ks_tmp, "state.json")
KS.SHADOW_FILE = os.path.join(_ks_tmp, "shadow.json")
KS.LOCK_FILE = os.path.join(_ks_tmp, "state.lock")
r = KS.halt_lane("grad", "unit test halt")
check("killswitch: halt_lane halts + records reason",
      r["action"] == "HALTED" and KS.is_halted("grad")
      and KS._load()["lanes"]["grad"]["halt_reason"] == "unit test halt")
r2 = KS.halt_lane("grad", "second halt ignored")
check("killswitch: halt_lane idempotent",
      r2["action"] == "halted"
      and KS._load()["lanes"]["grad"]["halt_reason"] == "unit test halt")
KS.reenable("grad", "unit test reset")
check("killswitch: grad not halted after human reenable",
      not KS.is_halted("grad"))


# DISCLOSED TEST EDIT (stale vs the mandatory entry guard, baseline red): entries are refused unless the shared account is
# enabled. TEST-FIXTURE limits (not owner values); no guard waived or patched. Also isolates ALL killswitch paths.
import importlib as _il, tempfile as _tf
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
import tests._guard_fixture as _GF
os.environ["FOREX_AGENT_HOME"] = _tf.mkdtemp(prefix="shared-acct-home-")
os.environ["PAPER_ACCOUNT_START"] = "1000"
os.environ.pop("PAPER_ACCOUNT_LANE_CAP", None)
os.environ.pop("PAPER_ACCOUNT_TRADE_SIZE_USD", None)
import daemon.launch_scanner.paper_account as _pa
_pa = _il.reload(_pa)
_GF.install(_pa)
os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"

# ---------------- harness ----------------
TMPD = tempfile.mkdtemp(prefix="grad-test-")
G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")


class FakeKS:
    """Killswitch semantics for tests: halt flags + verdict halts."""

    def __init__(self):
        self.halted = set()
        self.recorded = []
        self.halt_reasons = {}

    def is_halted(self, lane):
        return lane in self.halted

    def gate(self, lane):
        return not self.is_halted(lane)

    def record_trade(self, lane, pnl, ts=None):
        self.recorded.append(pnl)

    def check_lane(self, lane):
        return {"lane": lane}

    def halt_lane(self, lane, reason=""):
        if lane not in self.halted:
            self.halted.add(lane)
            self.halt_reasons[lane] = reason
        return {"lane": lane, "action": "HALTED"}


FAKE_KS = FakeKS()

SAFETY_OK = {"passed": True, "unknown": False, "score": 7,
             "flags": [], "fails": [], "source": "rugcheck"}
TAPE_OK = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 8_000.0, "usd_market_cap": 69_000.0,
           "chg_m5": 3.0, "price_usd": 0.00007,
           "tape_source": "dexscreener"}


def _patch_all(price=0.00007):
    p1 = patch.object(G, "_veto")
    p2 = patch.object(G, "_safety")
    p3 = patch.object(G, "_ks", FAKE_KS)
    p4 = patch.object(G, "quote_price", return_value=price)
    p5 = patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK))
    p6 = patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)
    # DISCLOSED: the shared-account guard fixture installs an at-cost price feed under jup_price, which G._jup.get_prices reads;
    # with price_fn returning 0 ("unquotable") that feed answered at cost and the case closed as time_stop_24h instead of
    # time_stop_24h_unquotable. These cases model "Jupiter has no price", so the lane's Jupiter read returns nothing here.
    p7 = patch.object(G._jup, "get_prices", return_value={}) if getattr(G, "_jup", None) is not None else None
    m_veto, m_safety = p1.start(), p2.start()
    for p in [x for x in (p3, p4, p5, p6, p7) if x is not None]:
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(SAFETY_OK)
    return tuple(x for x in (p1, p2, p3, p4, p5, p6, p7) if x is not None), m_veto, m_safety


def _unpatch(ps):
    for p in ps:
        p.stop()


def _fresh(price=0.00007):
    # DISCLOSED: each case gets a clean shared account (the account persists across cases otherwise, so earlier open
    # positions make later entries refuse). TEST-FIXTURE values.
    global _pa
    os.environ["FOREX_AGENT_HOME"] = _tf.mkdtemp(prefix="shared-acct-home-")
    _pa = _il.reload(_pa)
    _GF.install(_pa)
    os.environ["PAPER_ACCOUNT_MAX_DATA_AGE_S"] = "100000"
    G._RECOVERY_DONE = False
    G._ENTRY_BLOCKED = False
    if os.path.exists(G.LEDGER_PATH):
        os.remove(G.LEDGER_PATH)
    FAKE_KS.halted = set()
    FAKE_KS.recorded = []
    FAKE_KS.halt_reasons = {}
    ps, m_veto, m_safety = _patch_all(price=price)
    G.ensure_ledger()
    return ps, m_veto, m_safety


def _cand(mint="AAA111pump", age_s=60.0, dex="pumpswap", **kw):
    c = {"mint": mint, "symbol": "TKN", "name": "TKN / SOL",
         "chain": "solana", "dex": dex,
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6_000.0,
         "fdv_usd": 69_000.0, "price_usd": 0.00007,
         "source": "gecko-new-pool"}
    c.update(kw)
    return c


# ---------------- graduation filter ----------------
now = time.time()
check("filter: fresh pumpswap pump-mint qualifies",
      G.is_graduation_candidate(_cand(age_s=30), now) is not None)
check("filter: raydium pool qualifies",
      G.is_graduation_candidate(_cand(dex="raydium", age_s=300),
                                now) is not None)
check("filter: pool 11 min old rejected",
      G.is_graduation_candidate(_cand(age_s=660), now) is None)
check("filter: bonding curve (pump-fun dex) rejected",
      G.is_graduation_candidate(_cand(dex="pump-fun"), now) is None)
check("filter: non-pump mint rejected",
      G.is_graduation_candidate(_cand(mint="So111111111111"), now)
      is None)
check("filter: missing timestamp rejected",
      G.is_graduation_candidate(_cand(created_ts=0), now) is None)
check("filter: exactly 600s boundary qualifies",
      G.is_graduation_candidate(_cand(age_s=600), now) is not None)


# ---------------- sources normalizer ----------------
def _gt_pool(mint, dex, age_s=45.0):
    import datetime as _dt
    ts = _dt.datetime.fromtimestamp(
        time.time() - age_s, tz=_dt.timezone.utc).isoformat()
    return {"attributes": {"name": "TKN / SOL", "address": "POOLX",
                           "pool_created_at": ts,
                           "reserve_in_usd": "5000.5",
                           "fdv_usd": "69000",
                           "base_token_price_usd": "0.00007"},
            "relationships": {
                "base_token": {"data": {"id": "solana_" + mint}},
                "dex": {"data": {"id": dex}}}}


with patch.object(SRC, "_get_json",
                  return_value={"data": [_gt_pool("BBB222pump",
                                                  "pumpswap"),
                                         _gt_pool("CCC333pump",
                                                  "pump-fun")]}):
    pools = SRC.geckoterminal_new_pools("solana", pages=1)
check("sources: new_pools normalizes mint/dex/age/liq",
      len(pools) == 2 and pools[0]["mint"] == "BBB222pump"
      and pools[0]["dex"] == "pumpswap"
      and abs(pools[0]["reserve_usd"] - 5000.5) < 0.01
      and 0 < time.time() - pools[0]["created_ts"] < 120)
check("sources: normalized pools feed the grad filter",
      G.is_graduation_candidate(pools[0]) is not None
      and G.is_graduation_candidate(pools[1]) is None)


# ---------------- ledger header pre-registration ----------------
ps, _mv, _ms = _fresh()
d = G._load()
pre = d.get("preregistered_verdict") or {}
check("ledger: verdict pre-registered in header",
      pre.get("sample_closes") == 100
      and pre.get("killswitch_cfg", {}).get("min_trades") == 100
      and pre.get("terms", {}).get("runner_rule_applies") is False
      and pre.get("registered_ts", 0) > 0)
check("ledger: bankroll starts at $100, state active",
      d["bankroll"] == 100.0 and d["state"] == "active"
      and d["verdict"] is None)
_unpatch(ps)


# ---------------- entry: happy path + audit fields ----------------
ps, _mv, _ms = _fresh()
opened, why, final = G.consider_candidate(_cand())
check("entry: fresh graduation opens", opened and final)
d = G._load()
pos = d["positions"].get("AAA111pump") or {}
snap = pos.get("entry_snapshot") or {}
grad_blk = snap.get("graduation") or {}
check("entry: $1 debited, position recorded",
      abs(d["bankroll"] - 99.0) < 1e-9 and bool(pos))
check("entry: audit fields logged (age/pool/dex/liq/mcap)",
      0 < grad_blk.get("grad_age_s", 0) <= 600
      and grad_blk.get("pool_address") == "POOL1"
      and grad_blk.get("dex") == "pumpswap"
      and snap.get("liquidity") == 8_000.0
      and snap.get("mcap") == 69_000.0)
check("entry: honest fill is adverse (entry >= quoted)",
      pos.get("entry_price", 0) >= pos.get("quoted_price", 0) > 0)
check("entry: daily counter incremented",
      d["entries_day"].get("count") == 1)
_unpatch(ps)


# ---------------- dedupe: one entry per mint ever ----------------
ps, _mv, _ms = _fresh()
ok1, _w, _f = G.consider_candidate(_cand())
ok2, why2, fin2 = G.consider_candidate(_cand())
check("dedupe: second attempt on open mint blocked final",
      ok1 and not ok2 and fin2 and "already open" in why2)
# close it via hard stop, then try again -> still blocked (ever)
G.poll_exits(lambda c, m, s: 0.00007 * 0.4)
ok3, why3, fin3 = G.consider_candidate(_cand())
check("dedupe: closed mint can never re-enter",
      not ok3 and fin3 and "already traded" in why3)
check("dedupe: exactly one close recorded", len(G._load()["closes"]) == 1)
_unpatch(ps)


# ---------------- safety fail-closed ----------------
ps, _mv, m_safety = _fresh()
m_safety.check_token.return_value = {
    "passed": False, "unknown": False, "fails": ["mint authority on"]}
opened, why, final = G.consider_candidate(_cand(mint="BAD111pump"))
d = G._load()
check("safety: hard fail blocks, final, mint marked dead",
      not opened and final and "safety" in why
      and "BAD111pump" in d["dead_mints"])
opened2, _w2, _f2 = G.consider_candidate(_cand(mint="BAD111pump"))
check("safety: dead mint never reconsidered",
      not opened2 and len(d["positions"]) == 0)
_unpatch(ps)

ps, _mv, m_safety = _fresh()
m_safety.check_token.return_value = {
    "passed": True, "unknown": True, "fails": []}
opened, why, final = G.consider_candidate(_cand(mint="UNK111pump"))
d = G._load()
check("safety: unknown blocks but is NOT final (retry in window)",
      not opened and not final and "UNK111pump" not in d["dead_mints"])
m_safety.check_token.return_value = dict(SAFETY_OK)
opened, _w, _f = G.consider_candidate(_cand(mint="UNK111pump"))
check("safety: unknown-then-known candidate enters on retry",
      opened and "UNK111pump" in G._load()["positions"])
_unpatch(ps)


# ---------------- veto is final ----------------
ps, m_veto, _ms = _fresh()
m_veto.veto_check.return_value = {
    "passed": False, "vetoes": ["repeat-rug deployer"]}
opened, why, final = G.consider_candidate(_cand(mint="VETO11pump"))
check("veto: vetoed mint blocked final + dead",
      not opened and final and "veto" in why
      and "VETO11pump" in G._load()["dead_mints"])
_unpatch(ps)


# ---------------- caps ----------------
ps, _mv, _ms = _fresh()
with patch.object(G, "MAX_OPEN", 1):
    ok1, _w, _f = G.consider_candidate(_cand(mint="CAP111pump"))
    ok2, why2, fin2 = G.consider_candidate(_cand(mint="CAP222pump"))
check("caps: MAX_OPEN blocks non-final (retryable)",
      ok1 and not ok2 and not fin2 and "max open" in why2)
_unpatch(ps)

ps, _mv, _ms = _fresh()
with patch.object(G, "DAILY_ENTRY_CAP", 1):
    ok1, _w, _f = G.consider_candidate(_cand(mint="DAY111pump"))
    ok2, why2, fin2 = G.consider_candidate(_cand(mint="DAY222pump"))
check("caps: daily entry cap blocks non-final",
      ok1 and not ok2 and not fin2 and "daily entry cap" in why2)
_unpatch(ps)


# ---------------- killswitch halt blocks entries ----------------
ps, _mv, _ms = _fresh()
FAKE_KS.halted.add("grad")
opened, why, final = G.consider_candidate(_cand(mint="HALT11pump"))
check("killswitch: halted lane opens nothing",
      not opened and "halted" in why
      and len(G._load()["positions"]) == 0)
_unpatch(ps)


# ---------------- no quote = retry, not death ----------------
ps, _mv, _ms = _fresh()
with patch.object(G, "quote_price", return_value=0.0):
    opened, why, final = G.consider_candidate(_cand(mint="NQ1111pump"))
check("quote: no live quote blocks non-final, mint not dead",
      not opened and not final
      and "NQ1111pump" not in G._load()["dead_mints"])
_unpatch(ps)


# ---------------- ladder exits ----------------
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="LAD111pump"))
entry = G._load()["positions"]["LAD111pump"]["entry_price"]
# 2.05x: leg A fires, position stays open
closes = G.poll_exits(lambda c, m, s: entry * 2.05)
d = G._load()
pos = d["positions"].get("LAD111pump") or {}
check("ladder: leg A (50% @2x) fires, still open",
      closes == [] and pos.get("legs", {}).get("a", {}).get("done"))
# 3.05x: leg B fires
G.poll_exits(lambda c, m, s: entry * 3.05)
pos = G._load()["positions"].get("LAD111pump") or {}
check("ladder: leg B (25% @3x) fires",
      pos.get("legs", {}).get("b", {}).get("done"))
# drop 35% from peak: leg C trailing stop completes the ladder
closes = G.poll_exits(lambda c, m, s: entry * 3.05 * 0.65)
d = G._load()
rec = (d["closes"] or [{}])[-1]
check("ladder: leg C trail completes -> close recorded, profit",
      len(closes) == 1 and rec.get("closed_reason") == "ladder_complete"
      and rec.get("pnl_usd", 0) > 0)
check("ladder: peak journaled on close (mult >= 3)",
      (rec.get("peak_mult") or 0) >= 3.0
      and rec.get("peak_source") == "tracked")
check("ladder: cash settled back above $100 after a win",
      d["bankroll"] > 100.0)
_unpatch(ps)

# hard stop
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="STOP11pump"))
entry = G._load()["positions"]["STOP11pump"]["entry_price"]
closes = G.poll_exits(lambda c, m, s: entry * 0.49)
rec = (G._load()["closes"] or [{}])[-1]
check("ladder: -50% hard stop closes all, loss < $1 (stop works)",
      len(closes) == 1
      and rec.get("closed_reason") == "hard_stop_-50pct"
      and -1.0 < rec.get("pnl_usd", 0) < -0.3)
_unpatch(ps)

# time stop: quoted but 25h old
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="TIME11pump"))
with G._ledger_lock():
    d = G._read()
    d["positions"]["TIME11pump"]["entry_ts"] -= 25 * 3600
    G._write(d)
closes = G.poll_exits(lambda c, m, s: 0.00007)
rec = (G._load()["closes"] or [{}])[-1]
check("ladder: 24h time stop closes",
      len(closes) == 1 and rec.get("closed_reason") == "time_stop_24h")
_unpatch(ps)

# unquotable past 24h finalizes at 0
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="DEAD11pump"))
with G._ledger_lock():
    d = G._read()
    d["positions"]["DEAD11pump"]["entry_ts"] -= 25 * 3600
    G._write(d)
closes = G.poll_exits(lambda c, m, s: 0.0)
rec = (G._load()["closes"] or [{}])[-1]
check("ladder: unquotable past 24h finalizes at 0 (full loss)",
      len(closes) == 1
      and rec.get("closed_reason") == "time_stop_24h_unquotable"
      and rec.get("pnl_usd", 0) <= -1.0
      and rec.get("realized_usd", 1) <= 0.0)
_unpatch(ps)

# absurd print past 24h finalizes like unquotable (2026-11-08):
# two grad_select positions sat open 56-71h because the
# phantom-print guard `continue`d past the 24h time stop.
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="ABSURD11pump"))
_entry = G._load()["positions"]["ABSURD11pump"]["entry_price"]
with G._ledger_lock():
    d = G._read()
    d["positions"]["ABSURD11pump"]["entry_ts"] -= 25 * 3600
    G._write(d)
closes = G.poll_exits(lambda c, m, s, e=_entry: e * 100.0)
rec = (G._load()["closes"] or [{}])[-1]
check("ladder: absurd print past 24h finalizes at 0 (full loss)",
      len(closes) == 1
      and rec.get("closed_reason") == "time_stop_24h_unquotable"
      and rec.get("pnl_usd", 0) <= -1.0
      and rec.get("realized_usd", 1) <= 0.0)
_unpatch(ps)

# absurd print BEFORE the backstop still skips (no close, no legs)
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="ABSURD22pump"))
_entry = G._load()["positions"]["ABSURD22pump"]["entry_price"]
closes = G.poll_exits(lambda c, m, s, e=_entry: e * 100.0)
_p = G._load()["positions"].get("ABSURD22pump")
check("ladder: absurd print before 24h skips the cycle (no close)",
      closes == [] and _p is not None
      and all(not v["done"] for v in _p["legs"].values())
      and any(j.get("skipped")
              for j in (_p.get("quote_journal") or [])))
_unpatch(ps)


# ---------------- pre-registered verdict ----------------
# Mechanism tested with the sample patched to 3 (the production
# constant 100 is asserted above and in the ledger header).
ps, _mv, _ms = _fresh()
with patch.object(G, "VERDICT_SAMPLE_CLOSES", 3):
    for i, mint in enumerate(("V11111pump", "V22222pump", "V33333pump")):
        ok, _w, _f = G.consider_candidate(_cand(mint=mint))
        assert ok
        e = G._load()["positions"][mint]["entry_price"]
        G.poll_exits(lambda c, m, s, e=e: e * 0.4)  # hard stop, loss
    d = G._load()
    v = d.get("verdict") or {}
check("verdict: 100-close rule fires at sample (patched=3), "
      "negative expectancy halts lane",
      v.get("n_closes") == 3 and v.get("expectancy", 0) <= 0
      and "HALT" in str(v.get("action"))
      and "grad" in FAKE_KS.halted
      and "pre-registered" in FAKE_KS.halt_reasons.get("grad", ""))
_unpatch(ps)

ps, _mv, _ms = _fresh()
with patch.object(G, "VERDICT_SAMPLE_CLOSES", 2):
    for mint in ("W11111pump", "W22222pump"):
        ok, _w, _f = G.consider_candidate(_cand(mint=mint))
        assert ok
        e = G._load()["positions"][mint]["entry_price"]
        # run to 3.05x then trail out: a profitable ladder close
        G.poll_exits(lambda c, m, s, e=e: e * 3.05)
        G.poll_exits(lambda c, m, s, e=e: e * 3.05 * 0.6)
    d = G._load()
    v = d.get("verdict") or {}
check("verdict: positive expectancy at sample -> SURVIVED, no halt",
      v.get("n_closes") == 2 and v.get("expectancy", 0) > 0
      and "SURVIVED" in str(v.get("action"))
      and "grad" not in FAKE_KS.halted)
_unpatch(ps)


# ---------------- scan_once: watchlist + window expiry ----------------
ps, _mv, _ms = _fresh()
pools = [_cand(mint="SCAN11pump", age_s=120),
         _cand(mint="SCAN22pump", age_s=120, dex="pump-fun"),
         _cand(mint="SCAN33pump", age_s=1200)]
with patch.object(G, "fetch_candidates", return_value=pools):
    res = G.scan_once()
d = G._load()
check("scan: only the fresh graduation entered; curve + stale ignored",
      res["opened"] == 1 and "SCAN11pump" in d["positions"]
      and "SCAN22pump" not in d["positions"]
      and "SCAN33pump" not in d["positions"])
with patch.object(G, "fetch_candidates", return_value=[]):
    res2 = G.scan_once(now=time.time() + 3600)
d2 = G._load()
check("scan: repeat scan does not duplicate the entry",
      res2["opened"] == 0 and len(d2["positions"]) == 1)
_unpatch(ps)


# ---------------- F6 phantom-fill guards (2026-10-23) ----------------
# The phantom (mint 274rXqtA) booked +$60,749: entered on a row with
# an empty pool address, quoted via a $0-liquidity DS print, then
# "filled" exits at a 60,750x print. Three guards, three tests.

ps, _mv, _ms = _fresh()
with patch.object(G, "_http_json",
                  return_value=[{"priceUsd": "2.7e-05",
                                 "liquidity": {"usd": 0},
                                 "pairAddress": "PAIRX"}]):
    with patch.object(G, "_fresh_tape", return_value=None):
        px, trail = G._quote_detail("ZZZPHANTOMpump")
check("phantom guard: zero-liquidity DS print is not a quote",
      px == 0.0 and "zero-liquidity" in trail)
with patch.object(G, "_http_json",
                  return_value=[{"priceUsd": "0.00007",
                                 "liquidity": {"usd": 5000},
                                 "pairAddress": "PAIRX"}]):
    with patch.object(G, "_fresh_tape", return_value=None):
        px2, _t2 = G._quote_detail("ZZZPHANTOMpump")
check("phantom guard: backed print still quotes",
      px2 > 0)
_unpatch(ps)

ps, _mv, _ms = _fresh()
opened, why, final = G.consider_candidate(
    _cand(mint="NOPOOLpump", pool_address=""))
check("phantom guard: empty pool address refuses entry (retryable)",
      not opened and "pool address" in why and final is False)
_unpatch(ps)

ps, _mv, _ms = _fresh()
opened, _w, _f = G.consider_candidate(_cand(mint="PHANT1pump"))
check("phantom guard: candidate WITH pool address still enters",
      opened)
with G._ledger_lock():
    d = G._read()
    d["positions"]["PHANT1pump"]["peak_price"] = \
        2.0 * d["positions"]["PHANT1pump"]["entry_price"]
    G._write(d)


class _CurveReserves:
    @staticmethod
    def price_for_position(pos):
        return 999.0, "reserves:curve"


with patch.object(G, "_reserves", _CurveReserves()):
    closes = G.poll_exits()
d = G._load()
_p = d["positions"].get("PHANT1pump")
check("phantom guard: curve print on graduated token banks no legs",
      closes == [] and _p is not None
      and all(not v["done"] for v in _p["legs"].values()))
check("phantom guard: skipped curve print is journaled",
      _p is not None and any(j.get("skipped")
                             for j in (_p.get("quote_journal") or [])))
_unpatch(ps)


class _CurveReservesModest:
    """A curve print at a sane price (2x entry): below the absurd
    guard's 50x bound, so only the F6 branch fires. (The 999.0
    stub above trips the absurd guard first, so it cannot pin
    the F6 branch.)"""
    @staticmethod
    def price_for_position(pos):
        return float(pos["entry_price"]) * 2.0, "reserves:curve"


# curve print past 24h finalizes like unquotable (2026-11-08):
# same stuck-forever shape as the absurd-print branch — the F6
# guard `continue`d past the 24h time stop.
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="CURVE11pump"))
with G._ledger_lock():
    d = G._read()
    d["positions"]["CURVE11pump"]["entry_ts"] -= 25 * 3600
    d["positions"]["CURVE11pump"]["peak_price"] = \
        2.0 * d["positions"]["CURVE11pump"]["entry_price"]
    G._write(d)
with patch.object(G, "_reserves", _CurveReservesModest()):
    closes = G.poll_exits()
rec = (G._load()["closes"] or [{}])[-1]
check("phantom guard: curve print past 24h finalizes at 0 (full loss)",
      len(closes) == 1
      and rec.get("closed_reason") == "time_stop_24h_unquotable"
      and rec.get("pnl_usd", 0) <= -1.0
      and rec.get("realized_usd", 1) <= 0.0)
_unpatch(ps)

# curve print BEFORE the backstop still skips (no close, no legs)
ps, _mv, _ms = _fresh()
G.consider_candidate(_cand(mint="CURVE22pump"))
with G._ledger_lock():
    d = G._read()
    d["positions"]["CURVE22pump"]["peak_price"] = \
        2.0 * d["positions"]["CURVE22pump"]["entry_price"]
    G._write(d)
with patch.object(G, "_reserves", _CurveReservesModest()):
    closes = G.poll_exits()
_p = G._load()["positions"].get("CURVE22pump")
check("phantom guard: curve print before 24h skips the cycle (no close)",
      closes == [] and _p is not None
      and all(not v["done"] for v in _p["legs"].values())
      and any("curve print" in (j.get("skipped") or "")
              for j in (_p.get("quote_journal") or [])))
_unpatch(ps)


print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all grad tests passed")
