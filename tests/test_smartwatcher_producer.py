#!/usr/bin/env python3
"""Smart-watcher producer-constants regression tests (2026-10-23).

scripts/smart-watcher used to publish a hardcoded score=85 to the
signal bus, the paper mirror and degen_outcomes.json, and never
published buys/sells/age or the hitting wallets — so 30/36 CEO closes
carried fake entry features (score=85.0/buys=0/age=0.0). The watcher
measures NO total signal score; it fires on convergence (>=2
independent wallet groups) plus the runner-gate tape. These tests
prove the published records now carry the REAL measured values
(m5 buys/sells from the enrich txns, pair age from pairCreatedAt,
full hitting-wallet addresses, convergence/tape components, the
safety report's own score), that the unmeasured total score is
OMITTED (never zero/constant-filled), and that two different tokens
publish different values (non-constant). No network: send_card and
mirror_card are stubbed at the module seams fire_card uses, and the
bus/outcome files live in a temp FOREX_AGENT_HOME.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT
TMPD = tempfile.mkdtemp(prefix="smartwatcher-producer-")
os.environ["FOREX_AGENT_HOME"] = TMPD
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

_loader = importlib.machinery.SourceFileLoader(
    "smart_watcher", os.path.join(REPO, "scripts", "smart-watcher"))
_spec = importlib.util.spec_from_loader("smart_watcher", _loader)
SW = importlib.util.module_from_spec(_spec)
_loader.exec_module(SW)

# Top-level imports on purpose: fire_card does `from mirror import
# mirror_card` / `from signal_bus import ...` against the
# daemon/launch_scanner sys.path entry, so the stub must patch THIS
# module object, not the daemon.launch_scanner package instance.
import mirror as MIRROR

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
W1 = "WalletAAA1111111111111111111111111111111111"
W2 = "WalletBBB2222222222222222222222222222222222"


def fixture_info(buys=137, sells=52, age_min=12.0):
    return {
        "symbol": "FIX",
        "price": 0.0064,
        "mcap": 640_000.0,
        "liq": 120_000.0,
        "vol": 88_000.0,
        "chg_m5": 14.0,
        "chg_h1": 40.0,
        "has_mom": True,
        "txns": {"m5": {"buys": buys, "sells": sells},
                 "h1": {"buys": 900, "sells": 410},
                 "h24": {"buys": 5000, "sells": 2600}},
        "volume": {"h24": 88_000.0},
        "pairCreatedAt": int((NOW - age_min * 60) * 1000),
    }


def fixture_rec():
    return {
        "sightings": [
            {"wallet": W1, "group": "pack-sniper", "buy_ts": NOW - 120,
             "buy_price_sol": 0.001, "sol_spent": 1.5},
            {"wallet": W2, "group": "smart-flow", "buy_ts": NOW - 60,
             "buy_price_sol": 0.0011, "sol_spent": 0.5},
        ],
        "runup_mult": 1.32,
        "anchor_price_usd": 0.0048,
    }


# --- helpers: measured vs absent -------------------------------------------
check("window: present m5 returns measured counts",
      SW._window_txns(fixture_info(), "m5") == (137, 52))
check("window: measured 0 buys is a real 0, not absence",
      SW._window_txns({"txns": {"m5": {"buys": 0, "sells": 3}}},
                      "m5") == (0, 3))
check("window: absent window is (None, None)",
      SW._window_txns({"txns": {}}, "m5") == (None, None))
age = SW._pair_age_min(fixture_info(), now_ts=NOW)
check("age: pairCreatedAt -> ~12 min", age is not None
      and abs(age - 12.0) < 0.1)
check("age: missing pairCreatedAt -> None",
      SW._pair_age_min({}) is None)
check("age: future pairCreatedAt -> None",
      SW._pair_age_min({"pairCreatedAt": int((NOW + 600) * 1000)},
                       now_ts=NOW) is None)
check("safety score: RugCheck score_normalised",
      SW._safety_score({"score_normalised": 42}) == 42.0)
check("safety score: instant-safety score",
      SW._safety_score({"passed": True, "score": 77}) == 77.0)
check("safety score: no score carried -> None",
      SW._safety_score({"risks": []}) is None
      and SW._safety_score(None) is None)

feat = SW._measured_features(fixture_info(), rec=fixture_rec(),
                             n_groups=2,
                             report={"score_normalised": 42})
check("features: m5 buys/sells measured",
      feat.get("buys") == 137 and feat.get("sells") == 52)
check("features: age measured", abs(feat.get("age_min", 0) - 12.0) < 0.1)
check("features: hitting wallets are the full sighting addresses",
      feat.get("wallets") == [W1, W2])
comp = feat.get("components") or {}
check("features: components carry convergence + tape + runup",
      comp.get("convergence_groups") == 2
      and comp.get("sightings") == 2
      and comp.get("buys_m5") == 137 and comp.get("sells_h1") == 410
      and comp.get("buys_h24") == 5000
      and comp.get("chg_m5") == 14.0
      and comp.get("runup_mult") == 1.32
      and comp.get("sol_spent_total") == 2.0
      and comp.get("safety_score") == 42.0)
sparse = SW._measured_features({"symbol": "X", "txns": {},
                                "pairCreatedAt": 0})
check("features: unmeasured buys/sells/age are ABSENT, not zero-filled",
      "buys" not in sparse and "sells" not in sparse
      and "age_min" not in sparse
      and "buys_m5" not in (sparse.get("components") or {}))

# --- end-to-end publish path (fire_card) -----------------------------------
sent_cards = []
SW.send_card = lambda text, mint="", chain="solana", source="": (
    sent_cards.append(text), True)[1]
SW._HAS_LPLOCK = False
SW._HAS_SATURATION = False
# Exercise the card path: shipped code kills cards (_CARDS_ENABLED
# False, suppressing Telegram cards while still publishing the bus
# signal). These checks target card construction, so force it on.
SW._CARDS_ENABLED = True
mirror_calls = []
MIRROR.mirror_card = lambda *a, **kw: (mirror_calls.append((a, kw)),
                                       (True, "opened"))[1]

MINT = "FixMint111111111111111111111111111111111pump"
ok = SW.fire_card(MINT, fixture_info(), "ok", 2,
                  "%s, %s" % (W1[:6], W2[:6]),
                  rec=fixture_rec(), report={"score_normalised": 42})
check("fire: card sent", ok and len(sent_cards) == 1)
check("fire: card text carries the measured tape (137b/52s, 12m old)",
      bool(sent_cards) and "137b/52s" in sent_cards[0]
      and "12m old" in sent_cards[0])

bus = [json.loads(l) for l in
       open(os.path.join(TMPD, "launch_scanner",
                         "signal_bus.jsonl")).read().splitlines()]
sig = [s for s in bus if s.get("mint") == MINT]
check("bus: smart-watcher record published", len(sig) == 1)
if sig:
    s0 = sig[0]
    check("bus: NO constant score key (unmeasured total omitted)",
          "score" not in s0)
    check("bus: real buys/sells/age on the record",
          s0.get("buys") == 137 and s0.get("sells") == 52
          and abs(s0.get("age_min", 0) - 12.0) < 0.1)
    ev = s0.get("evidence") or {}
    check("bus: evidence names the hitting wallets (full + prefixes)",
          ev.get("wallets") == [W1, W2]
          and ev.get("wallet_names") == "%s, %s" % (W1[:6], W2[:6])
          and ev.get("smart_wallets") == 2)
    check("bus: evidence components are the measured values",
          (ev.get("components") or {}).get("buys_m5") == 137
          and (ev.get("components") or {}).get("runup_mult") == 1.32
          and ev.get("safety_score") == 42.0)

check("mirror: called once", len(mirror_calls) == 1)
if mirror_calls:
    _a, kw = mirror_calls[0]
    check("mirror: no score=85 (no score kwarg at all)",
          "score" not in kw)
    mev = kw.get("evidence") or {}
    check("mirror: evidence carries wallets + components + age",
          mev.get("wallets") == [W1, W2]
          and (mev.get("components") or {}).get("buys_m5") == 137
          and abs(mev.get("age_min", 0) - 12.0) < 0.1)

outcomes = json.load(open(os.path.join(TMPD, "degen_outcomes.json")))
rec_out = outcomes.get(MINT) or {}
check("outcomes: record carries real buys/sells/age/wallets/components",
      rec_out.get("buys") == 137 and rec_out.get("sells") == 52
      and abs(rec_out.get("age_min", 0) - 12.0) < 0.1
      and rec_out.get("wallets") == [W1, W2]
      and (rec_out.get("components") or {}).get("convergence_groups") == 2
      and rec_out.get("safety_score") == 42.0)
check("outcomes: no constant score recorded", "score" not in rec_out)

# --- non-constant proof: a second token publishes ITS values ---------------
MINT2 = "FixMint222222222222222222222222222222222pump"
ok2 = SW.fire_card(MINT2, fixture_info(buys=9, sells=31, age_min=47.0),
                   "ok", 3, "%s, %s" % (W1[:6], W2[:6]),
                   rec=fixture_rec(), report={"score_normalised": 42})
bus = [json.loads(l) for l in
       open(os.path.join(TMPD, "launch_scanner",
                         "signal_bus.jsonl")).read().splitlines()]
sig2 = [s for s in bus if s.get("mint") == MINT2]
check("bus: second token carries its own (different) measured values",
      ok2 and len(sig2) == 1 and sig2[0].get("buys") == 9
      and sig2[0].get("sells") == 31
      and abs(sig2[0].get("age_min", 0) - 47.0) < 0.1)

# --- omission proof: sparse enrich publishes no invented fields ------------
MINT3 = "FixMint333333333333333333333333333333333pump"
sparse_info = fixture_info()
sparse_info["txns"] = {}
sparse_info["pairCreatedAt"] = 0
ok3 = SW.fire_card(MINT3, sparse_info, "ok", 2,
                   "%s, %s" % (W1[:6], W2[:6]), rec=fixture_rec())
bus = [json.loads(l) for l in
       open(os.path.join(TMPD, "launch_scanner",
                         "signal_bus.jsonl")).read().splitlines()]
sig3 = [s for s in bus if s.get("mint") == MINT3]
check("bus: unmeasured buys/sells/age keys omitted, not zero-filled",
      ok3 and len(sig3) == 1 and "buys" not in sig3[0]
      and "sells" not in sig3[0] and "age_min" not in sig3[0]
      and "score" not in sig3[0])

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
