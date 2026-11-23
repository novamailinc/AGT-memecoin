#!/usr/bin/env python3
"""P1/P2/P5/P8 build tests (2026-10-23): direct-reserve pricing,
armed push watcher, grad exit pack (F3/F5/graduation handler),
stream detection in the gated lane, latency stamps, dossier hook.

Plain script, no network: parsers run on synthetic account bytes in
the VERIFIED PumpSwap layout; the watcher runs on injected messages;
the grad engine runs on stubbed feeds exactly like test_grad.py.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import base64
import json
import os
import struct
import sys
import tempfile
import time
from unittest.mock import patch

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import reserves as R
from daemon.launch_scanner import armed_watch as AW
from daemon.launch_scanner import grad as G

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---------------- reserves: base58 + parsers ----------------
check("b58 roundtrip", R.b58decode(R.b58encode(b"\x01\x02\x03\x04"))
      == b"\x01\x02\x03\x04")
check("b58 WSOL stable",
      R.b58encode(R.b58decode(R.WSOL)) == R.WSOL)

MINT = "CoskJf4y4cbs9QWKgncZpPkpWWGMSFWnVRbU6atJpump"


def _pool_bytes():
    """Synthetic PumpSwap pool account in the verified layout."""
    data = bytearray(301)
    data[43:75] = R.b58decode(R.WSOL)          # quote_mint
    data[75:107] = R.b58decode(MINT)           # base_mint
    data[139:171] = R.b58decode("QuoteVault1111111111111111111111111111"
                                .ljust(32, "1")[:32].replace(" ", ""))
    return bytes(data)


def _token_acct(mint, amount):
    data = bytearray(165)
    data[0:32] = R.b58decode(mint)
    struct.pack_into("<Q", data, 64, amount)
    return bytes(data)


qv = "HqV1111111111111111111111111111111111111111"
bv = "BvV1111111111111111111111111111111111111111"
pool = bytearray(301)
pool[43:75] = R.b58decode(R.WSOL)
pool[75:107] = R.b58decode(MINT)
pool[139:171] = R.b58decode(qv)
pool[171:203] = R.b58decode(bv)
got_q, got_b = R.parse_pumpswap_vaults(bytes(pool))
check("pool offsets: quote vault @139, base vault @171",
      got_q == qv and got_b == bv)

accts = {qv: (R.TOKEN_PROGRAM, _token_acct(R.WSOL, 5_000_000_000)),
         bv: (R.TOKEN_PROGRAM, _token_acct(MINT, 100_000_000_000))}
mb, mq, mqm = R.match_vaults(accts, MINT)
check("vault mint-matching (order-proof)",
      mb == bv and mq == qv and mqm == R.WSOL)
va = R._vault_amounts(accts, bv, qv, mqm)
check("vault amounts parsed",
      va["base_amount"] == 100_000_000_000
      and va["quote_amount"] == 5_000_000_000
      and va["quote_mint"] == R.WSOL)

# PUMP-quoted pool (quote-control mints are a program feature):
# the quote vault holds PUMP, not WSOL — the old WSOL-only match
# failed closed (unreadable); now the quote mint comes from the
# quote vault's own token account.
PUMP_M = "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"
accts_p = {qv: (R.TOKEN_PROGRAM, _token_acct(PUMP_M, 5_000_000)),
           bv: (R.TOKEN_PROGRAM, _token_acct(MINT, 100_000_000_000))}
mb2, mq2, mqm2 = R.match_vaults(accts_p, MINT)
check("PUMP-quoted vaults match, quote mint = PUMP",
      mb2 == bv and mq2 == qv and mqm2 == PUMP_M)
# two different non-base mints = ambiguous -> refuse
accts_bad = dict(accts_p)
accts_bad["XvV1111111111111111111111111111111111111111"] = (
    R.TOKEN_PROGRAM, _token_acct(PUMP_M, 1))
mb3, mq3, mqm3 = R.match_vaults(accts_bad, MINT)
check("three-vault ambiguity refused", mb3 is None and mq3 is None)
with patch.object(R, "quote_usd_price", return_value=(0.00253, 1.0)), \
     patch.object(R, "quote_decimals", return_value=6):
    # 5 PUMP (dec 6) / 100_000 tokens (dec 6) x $0.00253
    px2 = R.amounts_price(100_000_000_000, 5_000_000, 6,
                          quote_mint=PUMP_M)
check("amounts_price PUMP-quoted math",
      px2 is not None
      and abs(px2 - (5.0 / 100_000.0) * 0.00253) < 1e-12)
with patch.object(R, "quote_decimals", return_value=None):
    check("amounts_price: unknown quote decimals -> None",
          R.amounts_price(100_000_000_000, 5_000_000, 6,
                          quote_mint=PUMP_M) is None)

with patch.object(R, "sol_usd_price", return_value=100.0):
    px = R.amounts_price(100_000_000_000, 5_000_000_000, 6)
# price = (5 SOL / 100_000 tokens) * $100 = $0.005
check("amounts_price math", px is not None and abs(px - 0.005) < 1e-9)
check("amounts_price: empty base -> None",
      R.amounts_price(0, 5, 6) is None)

# bonding curve parse + PDA
curve = bytearray(166)  # current quote-aware schema (>= 115)
curve[:8] = bytes.fromhex("17b7f83760d8ac60")
struct.pack_into("<Q", curve, 8, 1_000_000_000_000)   # v_token raw
struct.pack_into("<Q", curve, 16, 85_000_000_000)     # v_quote (SOL)
curve[48] = 0
curve[83:115] = bytes([6, 155, 136, 87, 254, 171, 129, 132, 251, 104,
                       127, 99, 70, 24, 192, 53, 218, 196, 57, 220,
                       26, 235, 59, 85, 152, 160, 240, 0, 0, 0, 0, 1])
c = R.parse_bonding_curve(bytes(curve))
check("curve parse", c["v_token"] == 1_000_000_000_000
      and c["v_sol"] == 85_000_000_000 and c["complete"] is False)
pda1, pda2 = R.derive_curve_pda(MINT), R.derive_curve_pda(MINT)
check("curve PDA deterministic + valid",
      pda1 is not None and pda1 == pda2
      and not R._is_on_curve(R.b58decode(pda1)))

# mint decimals parser
mdata = bytearray(82)
mdata[44] = 6
check("mint decimals @44", R.parse_mint_decimals(bytes(mdata)) == 6)

# ---------------- armed watcher ----------------
positions = {
    "A": {"entry_price": 1.0, "peak_price": 1.4},   # 1.4x: not armed
    "B": {"entry_price": 1.0, "peak_price": 1.5},   # exactly 1.5x: armed
    "C": {"entry_price": 2.0, "peak_price": 9.0},   # armed
    "D": {"entry_price": 0.0, "peak_price": 9.0},   # no entry: skipped
}
armed = AW.armed_positions(positions)
check("armed filter: >=1.5x peak only",
      set(armed) == {"B", "C"})

seen = []
w = AW.ArmedWatcher(on_price=lambda m, p, t: seen.append((m, p)),
                    sol_usd_fn=lambda now=None: 100.0)
w._want = {MINT: {"pool": "POOL", "decimals": 6,
                  "base_vault": bv, "quote_vault": qv}}
w._subs = {4242: (MINT, "base"), 4243: (MINT, "quote")}
w._amounts = {MINT: {"base": 100_000_000_000, "quote": 0, "ts": 0.0}}


def _notif(sub, mint, amount):
    return json.dumps({
        "method": "accountNotification",
        "params": {"subscription": sub, "result": {"value": {
            "data": [base64.b64encode(
                _token_acct(mint, amount)).decode(), "base64"]}}}})


w._handle(None, _notif(4243, R.WSOL, 5_000_000_000))
check("watcher: vault move -> pushed price",
      len(seen) == 1 and seen[0][0] == MINT
      and abs(seen[0][1] - 0.005) < 1e-9)
check("watcher: fresh price served", w.price(MINT) > 0)
w._amounts[MINT]["ts"] = time.time() - 100  # age the last update
check("watcher: stale price withheld",
      w.price(MINT, max_age_s=15.0) == 0.0)

# ---------------- grad engine: entry stamps + detection ----------------
TMPD = tempfile.mkdtemp(prefix="p1-grad-")
G.LEDGER_PATH = os.path.join(TMPD, "grad_ledger.json")


class _FakeKS:
    halted = set()
    recorded = []

    @classmethod
    def is_halted(cls, lane):
        return lane in cls.halted

    @classmethod
    def gate(cls, lane):
        return not cls.is_halted(lane)

    @classmethod
    def record_trade(cls, lane, pnl):
        cls.recorded.append((lane, pnl))

    @classmethod
    def check_lane(cls, lane):
        return {}

    @classmethod
    def halt_lane(cls, lane, reason):
        cls.halted.add(lane)


TAPE_OK = {"price_usd": 0.00007, "usd_liquidity": 6000.0,
           "usd_market_cap": 69000.0, "tape_source": "dexscreener"}
SAFETY_OK = {"passed": True, "fails": [], "flags": [], "score": 90,
             "unknown": False, "source": "test"}


def _patch_all(price=0.00007):
    ps = [patch.object(G, "_veto"), patch.object(G, "_safety"),
          patch.object(G, "_ks", _FakeKS),
          patch.object(G, "quote_price", return_value=price),
          patch.object(G, "_fresh_tape", return_value=dict(TAPE_OK)),
          patch.object(G, "QUOTE_RETRY_GAP_S", 0.0)]
    m_veto, m_safety = ps[0].start(), ps[1].start()
    for p in ps[2:]:
        p.start()
    m_veto.veto_check.return_value = {"passed": True, "vetoes": []}
    m_safety.check_token.return_value = dict(SAFETY_OK)
    return ps


def _cand(mint="AAA111pump", age_s=60.0, **kw):
    c = {"mint": mint, "symbol": "TKN", "dex": "pumpswap",
         "created_ts": time.time() - age_s,
         "pool_address": "POOL1", "reserve_usd": 6000.0,
         "fdv_usd": 69000.0, "source": "gecko-new-pool"}
    c.update(kw)
    return c


if os.path.exists(G.LEDGER_PATH):
    os.remove(G.LEDGER_PATH)
ps = _patch_all()
G.ensure_ledger()
opened, _why, _fin = G.consider_candidate(
    _cand(first_seen_ts=time.time() - 30))
check("entry opens (stamps setup)", opened)
pos = G.open_positions()["AAA111pump"]
check("P5 stamps: born/seen/decided present + ordered",
      0 < pos["born_ts"] <= pos["seen_ts"] <= pos["decided_ts"]
      <= pos["entry_ts"])
check("P2 stamp: detection recorded from candidate source",
      pos["entry_features"]["detection"] == "gecko-new-pool")
for p in ps:
    p.stop()

# ---------------- exits: F3 journal/peak_ts, F5, handler ----------------
prices = {"px": 0.00007 * 3.2}  # >3x the honest filled price: A+B fire
G.poll_exits(lambda c, m, s=None: prices["px"])
pos = G.open_positions()["AAA111pump"]
check("F3: peak_ts recorded on peak update",
      pos.get("peak_ts") is not None
      and pos["peak_ts"] >= pos["entry_ts"])
check("F3: quote journal appended",
      len(pos.get("quote_journal") or []) >= 1
      and pos["quote_journal"][-1]["px"] == prices["px"])
check("legs A+B fired at 3x (ladder unchanged)",
      pos["legs"]["a"]["done"] and pos["legs"]["b"]["done"])

# graduation handler + F5 via the default quote path
def _fake_quote(chain, mint, spot_index=None):
    G._LAST_QUOTE_PAIR = "NEWPOOL9"
    G._LAST_QUOTE_LIQ = 4321.0
    return 0.00007 * 0.4  # below hard stop: forces a full close

with patch.object(G, "quote_price", side_effect=_fake_quote):
    closes = G.poll_exits()
check("handler: close happened on hard stop", len(closes) == 1)
rec = closes[0]
check("handler: pair transition journaled into close",
      any(t.get("kind") == "pair_changed" and t.get("to") == "NEWPOOL9"
          for t in rec.get("transitions") or []))
check("F5: exit liquidity refreshed into close record",
      rec.get("exit_liquidity") == 4321.0)
check("F3: close carries peak_ts + quote journal",
      rec.get("peak_ts") is not None
      and len(rec.get("quote_journal") or []) >= 1)
check("P5: close carries latency stamps",
      rec.get("born_ts") and rec.get("seen_ts")
      and rec.get("decided_ts"))

# ---------------- P2: stream detection in scan ----------------
class _Feed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        out, self._items = self._items, []
        return out


if os.path.exists(G.LEDGER_PATH):
    os.remove(G.LEDGER_PATH)
_FakeKS.halted = set()
ps = _patch_all()
G.ensure_ledger()
item = {"source": "pumpportal-migration", "mint": "STR111pump",
        "symbol": "STR", "created_ts": time.time(),
        "extra": {"migration_pool": "pumpswap", "market_id": "POOL9"}}
out = G.scan_once(feed=_Feed([item]), fetch_gt=False)
check("P2: stream candidate counted + opened",
      out["stream"] == 1 and out["opened"] == 1)
pos = G.open_positions().get("STR111pump") or {}
check("P2: entry stamped pumpportal-migration",
      (pos.get("entry_features") or {}).get("detection")
      == "pumpportal-migration")
for p in ps:
    p.stop()

# ---------------- P8: dossier safety hook ----------------
class _Dossiers:
    rep = None

    @classmethod
    def read_safety(cls, mint):
        return cls.rep


if os.path.exists(G.LEDGER_PATH):
    os.remove(G.LEDGER_PATH)
ps = _patch_all()
G.ensure_ledger()
with patch.object(G, "_dossiers", _Dossiers):
    _Dossiers.rep = {"passed": False, "fails": ["dossier: rug"],
                     "flags": [], "score": 1, "unknown": False,
                     "source": "dossier:test"}
    opened, why, final = G.consider_candidate(_cand(mint="DOS111pump"))
    check("P8: conclusive dossier FAIL blocks without live check",
          not opened and final)
    _Dossiers.rep = dict(SAFETY_OK)
    opened, why, final = G.consider_candidate(_cand(mint="DOS222pump"))
    check("P8: conclusive dossier PASS opens", opened)
    _Dossiers.rep = {"unknown": True}
    opened, why, final = G.consider_candidate(_cand(mint="DOS333pump"))
    check("P8: unknown dossier falls through to live check",
          opened)
for p in ps:
    p.stop()

print()
if fails:
    print("FAILURES:", len(fails))
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL P1/P2/P5/P8 TESTS PASSED")
