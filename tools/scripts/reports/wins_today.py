#!/usr/bin/env python3
"""Wins-so-far-today (WAT day) for paper lanes + devnet demo.
Usage: wins_today.py [day_start_epoch]
Reads verdict chain + lane ledgers + devnet demo trading ledger.
Prints per-lane closes/wins/net + totals. Never raises per file."""
import json
import os
import sys

HOME = "/home/hatch/.forex-agent"
DAY = int(sys.argv[1]) if len(sys.argv) > 1 else 1793487600

# ---- verdict chain ----
chain = {}
kinds = {}
try:
    with open(os.path.join(HOME, "verdict_ledger/events.jsonl")) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ts = ev.get("ts") or 0
            if ts < DAY:
                continue
            kinds[ev.get("kind")] = kinds.get(ev.get("kind"), 0) + 1
            p = ev.get("payload") or {}
            if "pnl_usd" not in p:
                continue
            lane = ev.get("lane") or "?"
            c = chain.setdefault(lane, {"closes": 0, "wins": 0, "net": 0.0})
            c["closes"] += 1
            c["net"] += float(p["pnl_usd"])
            if float(p["pnl_usd"]) > 0:
                c["wins"] += 1
except FileNotFoundError:
    pass
print("== PAPER, verdict chain, today ==")
print("kinds seen today:", kinds)
tot_c = tot_w = 0
tot_n = 0.0
for lane in sorted(chain):
    c = chain[lane]
    print(f"  {lane}: {c['closes']} closes, {c['wins']} wins, "
          f"net ${c['net']:+,.2f}")
    tot_c += c["closes"]
    tot_w += c["wins"]
    tot_n += c["net"]
print(f"  TOTAL chain: {tot_c} closes, {tot_w} wins, net ${tot_n:+,.2f}")

# ---- lane ledgers ----
print("== PAPER, lane ledgers, today ==")
LEDGERS = {
    "grad_select": "grad_select_ledger.json",
    "grad_raw": "grad_raw_ledger.json",
    "tranche": "tranche_ledger.json",
    "tranche_protect": "tranche_protect_ledger.json",
    "raw_tight": "raw_tight_ledger.json",
    "mimic": "mimic_ledger.json",
    "moon": "moon_ledger.json",
    "momentum": "momentum_ledger.json",
}
tot_c = tot_w = 0
tot_n = 0.0
for lane, fn in LEDGERS.items():
    try:
        d = json.load(open(os.path.join(HOME, fn)))
    except Exception as e:
        print(f"  {lane}: UNREADABLE ({e})")
        continue
    closes = d.get("closes") or d.get("closed") or []
    if isinstance(closes, dict):
        closes = list(closes.values())
    c = w = 0
    n = 0.0
    for cl in closes:
        if not isinstance(cl, dict):
            continue
        ts = cl.get("close_ts") or cl.get("exit_ts") or cl.get("ts") or 0
        if ts < DAY:
            continue
        pnl = cl.get("pnl_usd")
        if pnl is None:
            pnl = cl.get("pnl", 0)
        c += 1
        n += float(pnl)
        if float(pnl) > 0:
            w += 1
    print(f"  {lane}: {c} closes, {w} wins, net ${n:+,.2f}")
    tot_c += c
    tot_w += w
    tot_n += n
print(f"  TOTAL ledgers: {tot_c} closes, {tot_w} wins, net ${tot_n:+,.2f}")

# ---- devnet demo trading ledger ----
print("== DEMO (devnet, modeled SOL), today ==")
try:
    d = json.load(open(os.path.join(HOME, "devnet_demo_trading_ledger.json")))
    print("  top-level keys:", sorted(d.keys()))
    print("  lanes keys:", sorted((d.get("lanes") or {}).keys()))
    buckets = {}
    top = d.get("closed_positions") or []
    if top:
        buckets["grad_select"] = top
    for lane, ld in (d.get("lanes") or {}).items():
        if isinstance(ld, dict):
            buckets[lane] = ld.get("closed_positions") or []
    tot_c = tot_w = 0
    tot_n = 0.0
    for lane in sorted(buckets):
        c = w = 0
        n = 0.0
        for cl in buckets[lane]:
            if not isinstance(cl, dict):
                continue
            ts = cl.get("close_ts") or cl.get("ts") or 0
            if ts < DAY:
                continue
            pnl = cl.get("demo_pnl_sol")
            if pnl is None:
                continue
            c += 1
            n += float(pnl)
            if float(pnl) > 0:
                w += 1
        print(f"  {lane}: {c} closes, {w} wins, net {n:+.4f} SOL")
        tot_c += c
        tot_w += w
        tot_n += n
    print(f"  TOTAL demo: {tot_c} closes, {tot_w} wins, "
          f"net {tot_n:+.4f} SOL (modeled, not settled on-chain)")
except Exception as e:
    print(f"  UNREADABLE ({e})")
