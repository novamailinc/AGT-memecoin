#!/usr/bin/env python3
"""Verify daily recap figures from live ledgers. Windows (epoch, UTC):
Nov 21 Lagos day = 1795215600 -> 1795302000
Nov 22 partial   = 1795302000 -> now
"""
import json, sys, time

HOME = "/home/hatch/.forex-agent"
LANES = ["grad_select", "grad_raw", "tranche", "tranche_protect", "momentum"]

def load(name):
    with open(f"{HOME}/{name}") as f:
        return json.load(f)

def window_stats(closes, lo, hi):
    sel = [c for c in closes if lo <= c.get("close_ts", 0) < hi]
    n = len(sel)
    wins = sum(1 for c in sel if c.get("pnl_usd", 0) > 0)
    net = sum(c.get("pnl_usd", 0) for c in sel)
    best = max(sel, key=lambda c: c.get("pnl_usd", 0)) if sel else None
    return n, wins, net, best

def report(lo, hi, label):
    print(f"=== {label} [{lo} -> {hi}] ===")
    total_n = total_w = 0
    total_net = 0.0
    for lane in LANES:
        led = load(f"{lane}_ledger.json")
        n, w, net, best = window_stats(led.get("closes", []), lo, hi)
        opens = len(led.get("positions", {}))
        total_n += n; total_w += w; total_net += net
        b = f" | best {best.get('symbol') or best.get('mint','?')[:8]} {best.get('realized_mult')}x pnl {best.get('pnl_usd'):.2f} ({best.get('closed_reason')})" if best else ""
        print(f"  {lane}: {n} closes, {w} wins, net ${net:.2f}, open {opens}{b}")
    print(f"  PAPER TOTAL: {total_n} closes, {total_w} wins, net ${total_net:.2f}")
    # Live10
    l10 = load("live10_demo_ledger.json")
    lanes = l10.get("lanes", {})
    bal = {k: v.get("balance") for k, v in lanes.items() if isinstance(v, dict)}
    print(f"  Live10 balances: {bal} total ${sum(bal.values()):.2f}")
    mir = l10.get("mirrored", [])
    f = [m for m in mir if lo <= m.get("close_ts", 0) < hi and m.get("funded")]
    u = [m for m in mir if lo <= m.get("close_ts", 0) < hi and not m.get("funded")]
    fw = sum(1 for m in f if m.get("scaled_pnl", 0) > 0)
    fnet = sum(m.get("scaled_pnl", 0) for m in f)
    unet_paper = sum(m.get("paper_pnl", 0) for m in u)
    unet_scaled = sum(m.get("scaled_pnl", 0) for m in u)
    print(f"  Live10 funded: {len(f)} closes, {fw} wins, net ${fnet:.2f}")
    print(f"  Live10 unfunded: {len(u)} closes, paper ${unet_paper:.2f}, scaled ${unet_scaled:.2f}")
    for m in u:
        print(f"    unfunded: {m.get('lane')} {m.get('mint','?')[:8]} paper {m.get('paper_pnl')} scaled {m.get('scaled_pnl')}")

now = int(time.time())
report(1795215600, 1795302000, "Nov 21 Lagos FULL day")
report(1795302000, now, "Nov 22 Lagos partial (to now)")

# Killswitch windows
ks = load("killswitch/state.json")
print("=== Killswitch ===")
for lane, st in ks.get("lanes", {}).items():
    tr = st.get("trades", [])
    net = sum(t.get("pnl", 0) for t in tr)
    wins = sum(1 for t in tr if t.get("pnl", 0) > 0)
    print(f"  {lane}: halted={st.get('halted')} n={len(tr)} net ${net:.2f} wins {wins} per-trade ${net/len(tr):.2f}" if tr else f"  {lane}: halted={st.get('halted')} n=0")
