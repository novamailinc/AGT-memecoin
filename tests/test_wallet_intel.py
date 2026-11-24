#!/usr/bin/env python3
"""Tests for the wallet intelligence system (5 components).

1. wallet_grader.py — UCL gates, practitioner gates, wash-trading
2. packdetect_v2.py — funding graph, union-find, pack signals
3. deployer_tracker.py — deployer scoring, factory detection, top creators
4. fresh_sniffer.py — insider/sniper/organic classification
5. scripts/wallet-monitor — message handling, dedup, killswitch
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
import os
import time
import json
import tempfile

sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")

# Isolate state files to temp dir
_tmp = tempfile.mkdtemp()
os.environ["FOREX_AGENT_HOME"] = _tmp

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# ============================================================
# 1. WALLET GRADER
# ============================================================
print("\n=== wallet_grader ===")
from wallet_grader import (
    grade_wallet, t_statistic, horizon_returns, wash_trading_score,
    STATUS_ACTIVE, STATUS_WATCHING, STATUS_REMOVED,
)

NOW = time.time()

def make_trades(n, win_rate=0.6, avg_ret=0.05, bundle=False, interleave=True):
    """Generate n synthetic trades, oldest first.

    interleave=True spreads wins evenly so all horizons stay positive
    (a wallet whose RECENT trades are all losses should not be ACTIVE).
    """
    trades = []
    n_wins = int(n * win_rate)
    # Build win/loss pattern
    is_win_list = [False] * n
    if interleave and n_wins > 0:
        step = n / n_wins
        for k in range(n_wins):
            is_win_list[int(k * step) % n] = True
    else:
        for i in range(n_wins):
            is_win_list[i] = True
    for i in range(n):
        is_win = is_win_list[i]
        ret = avg_ret * 2 if is_win else -avg_ret
        trades.append({
            "ts": NOW - (n - i) * 86400,
            "pnl": 100 if is_win else -50,
            "return": ret,
            "bundle_bot": bundle,
            "sniper_bot": False,
        })
    # Ensure last trade is a win so UCL "last" horizon gate can pass
    # (a wallet whose most recent trade lost should not be ACTIVE)
    if trades and trades[-1]["return"] <= 0 and n_wins > 0:
        trades[-1]["pnl"] = 100
        trades[-1]["return"] = avg_ret * 2
    return trades

# Strong wallet: 60 trades, 65% WR, positive everywhere
trades = make_trades(60, win_rate=0.65, avg_ret=0.08)
m = grade_wallet("strong_wallet", trades, now=NOW)
check("strong wallet passes t-stat", m.gate_results["t_stat_ok"])
check("strong wallet passes WR", m.gate_results["wr_ok"])
check("strong wallet passes 50 trades", m.gate_results["trades_50_ok"])
check("strong wallet ACTIVE", m.status == STATUS_ACTIVE)

# Weak wallet: 20% WR → watching
trades = make_trades(60, win_rate=0.2, avg_ret=-0.05)
m = grade_wallet("weak_wallet", trades, now=NOW)
check("weak wallet not active", m.status != STATUS_ACTIVE)

# Bundle bot → REMOVED (hard requirement)
trades = make_trades(60, win_rate=0.7, avg_ret=0.08, bundle=True)
m = grade_wallet("bundle_wallet", trades, now=NOW)
check("bundle bot REMOVED", m.status == STATUS_REMOVED)

# Too few trades → not active
trades = make_trades(10, win_rate=0.9, avg_ret=0.1)
m = grade_wallet("small_wallet", trades, now=NOW)
check("10 trades not active (noise)", m.status != STATUS_ACTIVE)

# Single-trade dependence → not active
trades = make_trades(60, win_rate=0.5, avg_ret=0.02)
trades[0]["pnl"] = 10000  # one huge win dominates
trades[0]["return"] = 5.0
m = grade_wallet("lucky_wallet", trades, now=NOW)
check("single-trade dependence flagged",
      not m.gate_results["no_single_dependence"])

# t-statistic math
check("t-stat positive for positive returns",
      t_statistic([0.1, 0.2, 0.15, 0.05, 0.1]) > 1.645)
check("t-stat zero for empty", t_statistic([]) == 0.0)

# Horizons
hz = horizon_returns([0.1]*20)
check("horizons computed", "last" in hz and "h11_15" in hz)

# Wash trading: identical buy/sell pairs
wash_trades = [
    {"side": "buy", "amount": 10.0, "mint": "m1"},
    {"side": "sell", "amount": 10.0, "mint": "m1"},
    {"side": "buy", "amount": 10.0, "mint": "m1"},
    {"side": "sell", "amount": 10.0, "mint": "m1"},
]
ws = wash_trading_score(wash_trades)
check("wash trading detected", ws > 0)

# Clean trades: no wash
clean_trades = [
    {"side": "buy", "amount": 10.0, "mint": "m1"},
    {"side": "sell", "amount": 8.0, "mint": "m1"},
]
check("clean trades low wash", wash_trading_score(clean_trades) < ws)

# ============================================================
# 2. PACK DETECTOR V2
# ============================================================
print("\n=== packdetect_v2 ===")
from packdetect_v2 import (
    UnionFind, trace_funder, funding_clusters, cooccurrence_packs,
    record_buy, detect_packs, pack_signal,
)

# Union-find
uf = UnionFind()
uf.union("a", "b")
uf.union("b", "c")
uf.union("x", "y")
groups = uf.groups(2)
check("union-find groups", len(groups) == 2)
check("union-find transitive", any({"a","b","c"} <= g for g in groups))

# Funder tracing
def mock_funder(w):
    chain = {"w1": "f1", "f1": "p1", "p1": "g1",
             "w2": "f1",  # w2 shares funder f1 with w1
             "w3": "cex_hot"}
    return chain.get(w)

# Add CEX hot to the module's set for test
import packdetect_v2
packdetect_v2.CEX_HOT_ADDRESSES.add("cex_hot")

chain = trace_funder("w1", mock_funder)
check("funder chain 3 hops", chain == ["f1", "p1", "g1"])
chain = trace_funder("w3", mock_funder)
check("CEX stops chain", chain == ["cex_hot"])

# Funding clusters
clusters = funding_clusters({"w1": ["f1","p1"], "w2": ["f1","p2"], "w4": ["f9"]})
check("funding cluster found", "f1" in clusters and clusters["f1"] == {"w1","w2"})

# Co-occurrence packs
events = [
    ("w1", "m1", 1000), ("w2", "m1", 1005),  # co-buy 1
    ("w1", "m2", 2000), ("w2", "m2", 2003),  # co-buy 2 → pack
    ("w3", "m1", 1002),  # w3 only co-buys once with w1/w2
]
packs = cooccurrence_packs(events, window_s=600, min_cobuys=2)
check("co-occurrence pack detected", any({"w1","w2"} <= p for p in packs))
check("single co-buy not packed", not any("w3" in p and "w1" in p for p in packs))

# Record + detect + signal integration
record_buy("pw1", "test_mint_a", NOW)
record_buy("pw2", "test_mint_a", NOW + 5)
record_buy("pw1", "test_mint_b", NOW + 1000)
record_buy("pw2", "test_mint_b", NOW + 1005)
detect_packs()
sig = pack_signal("test_mint_c", ["pw1", "pw2", "other"])
check("pack signal fires", sig is not None and sig["strength"] > 0)
check("pack signal no false positive",
      pack_signal("test_mint_c", ["stranger1", "stranger2"]) is None)

# ============================================================
# 3. DEPLOYER TRACKER
# ============================================================
print("\n=== deployer_tracker ===")
from deployer_tracker import (
    record_launch, record_graduation, record_runner,
    deployer_score, compute_top_creators, is_top_creator,
    PUMP_MINT_AUTHORITY,
)

check("mint authority constant", PUMP_MINT_AUTHORITY == "TSLvdd1pWpHVjahSpsvCXUbgwsL3JAcvokwaKt1eokM")

# Good deployer: 3 runners
for i in range(5):
    record_launch("good_dev", f"mint_g{i}", NOW - i*86400, symbol=f"G{i}")
for i in range(3):
    record_runner("good_dev", f"mint_g{i}", 2_000_000)
score = deployer_score("good_dev")
check("good deployer scores high", score["score"] >= 40)
check("good deployer not factory", not score["is_factory"])

# Factory: mass launches
for i in range(2100):
    record_launch("factory_dev", f"mint_f{i}", NOW - (i % 30)*86400)
score = deployer_score("factory_dev")
check("factory flagged", score["is_factory"])
check("factory scores zero", score["score"] == 0)

# Unknown deployer
score = deployer_score("never_seen")
check("unknown deployer zero", score["score"] == 0)

# Graduation tracking
record_launch("grad_dev", "mint_grad1", NOW)
record_graduation("grad_dev", "mint_grad1")
from deployer_tracker import get_deployer_stats
stats = get_deployer_stats("grad_dev")
check("graduation recorded", stats["graduations"] == 1)

# Top creators
top = compute_top_creators()
check("top creators computed", isinstance(top, list))
check("good_dev in top", is_top_creator("good_dev"))

# ============================================================
# 4. FRESH SNIFFER
# ============================================================
print("\n=== fresh_sniffer ===")
from fresh_sniffer import (
    score_fresh_wallet, is_fresh, mark_seen,
    INSIDER_THRESHOLD, SNIPER_THRESHOLD,
)

# Clear insider: created 30min before launch, deployer-funded, top-3 buyer
result = score_fresh_wallet(
    wallet="insider1",
    wallet_age_s=1800,
    first_tx_ts=NOW - 3600,
    launch_ts=NOW - 1800,
    funder_chain=["deployer_funder_1", "parent_x"],
    deployer_clusters={"deployer_funder_1"},
    buy_rank=2,
    buy_size_sol=5.0,
    sibling_sizes=[5.0, 5.0, 5.0],
    funded_ts=NOW - 3*86400,  # funded 3d ago, dormant
)
check("insider classified", result["classification"] == "insider")
check("insider score high", result["score"] >= INSIDER_THRESHOLD)

# Organic: old wallet, no funder link, late buyer, varied size
result = score_fresh_wallet(
    wallet="organic1",
    wallet_age_s=90*86400,
    first_tx_ts=NOW - 90*86400,
    launch_ts=NOW - 3600,
    funder_chain=["some_cex"],
    deployer_clusters={"deployer_funder_1"},
    buy_rank=500,
    buy_size_sol=1.23,
    sibling_sizes=[5.0, 2.1, 0.7],
)
check("organic classified", result["classification"] == "organic")

# Sniper: fresh, early buyer, but no deployer link
result = score_fresh_wallet(
    wallet="sniper1",
    wallet_age_s=7200,
    first_tx_ts=NOW - 7200,
    launch_ts=NOW - 3600,
    funder_chain=["random_funder"],
    deployer_clusters={"deployer_funder_1"},
    buy_rank=5,
    buy_size_sol=3.0,
    sibling_sizes=[1.0, 2.5],
)
check("sniper or unknown (not organic/insider)",
      result["classification"] in ("sniper", "unknown"))

# Seen wallets
check("new wallet is fresh", is_fresh("brand_new_wallet_xyz"))
mark_seen("brand_new_wallet_xyz")
check("marked wallet not fresh", not is_fresh("brand_new_wallet_xyz"))

# ============================================================
# 5. WALLET MONITOR
# ============================================================
print("\n=== wallet-monitor ===")
sys.path.insert(0, _REPO_ROOT + "/scripts")
# Import as module (script has no .py extension — use SourceFileLoader)
import importlib.util
from importlib.machinery import SourceFileLoader
loader = SourceFileLoader(
    "wallet_monitor",
    _REPO_ROOT + "/scripts/wallet-monitor")
spec = importlib.util.spec_from_loader("wallet_monitor", loader)
wm = importlib.util.module_from_spec(spec)
loader.exec_module(wm)

# Dedup logic
wm._alerted.clear()
check("first alert allowed", wm.should_alert("w1", "m1"))
check("dup alert blocked", not wm.should_alert("w1", "m1"))
check("different mint allowed", wm.should_alert("w1", "m2"))
check("different wallet allowed", wm.should_alert("w2", "m1"))

# Trade handler: buy vs sell
handled = []
orig_send = wm.send_telegram
wm.send_telegram = lambda t: handled.append(t)
try:
    wm.handle_trade({"txType": "buy", "traderPublicKey": "testw",
                     "mint": "testm", "symbol": "TEST",
                     "solAmount": 1.5, "marketCapSol": 100})
    check("buy triggers signal", len(handled) == 1)
    check("signal mentions symbol", "TEST" in handled[0])
    check("signal is not auto-buy",
          "not auto-buy" in handled[0] or "Signal intelligence" in handled[0])

    wm.handle_trade({"txType": "sell", "traderPublicKey": "testw",
                     "mint": "testm2", "symbol": "TEST2"})
    check("sell does not trigger", len(handled) == 1)

    wm.handle_trade({"txType": "buy"})  # missing fields
    check("malformed handled gracefully", len(handled) == 1)
finally:
    wm.send_telegram = orig_send

# Killswitch gate exists
check("killswitch gate callable", callable(wm.killswitch_ok))

# ============================================================
print(f"\n{'='*50}")
print(f"{len(fails)} failures")
if fails:
    print("FAILED:", fails)
sys.exit(1 if fails else 0)
