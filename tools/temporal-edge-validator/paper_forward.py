#!/usr/bin/env python3
"""Out-of-sample paper tracker (honest forward test, no real money).

Decisions (which windows qualify) are FROZEN at build time in
qualified_windows.json. Each hourly run settles the last fully-closed candle:
if its hour was a qualified window, log the hypothetical trade
(entry=open, exit=close, net of costs) and compound a paper equity curve.

Run hourly via cron. This is measurement, not trading.
"""
import datetime
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data
import validator

DB = os.path.join(HERE, "data", "paper_trades.db")
QUALIFIED = os.path.join(HERE, "qualified_windows.json")
START_EQUITY = 1000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    symbol    TEXT NOT NULL,
    open_ms   INTEGER NOT NULL,
    window    TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry     REAL NOT NULL,
    exit      REAL NOT NULL,
    net_ret   REAL NOT NULL,
    frac      REAL NOT NULL,
    equity_before REAL NOT NULL,
    equity_after  REAL NOT NULL,
    PRIMARY KEY (symbol, open_ms)
);
CREATE TABLE IF NOT EXISTS equity (
    symbol TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    trades INTEGER NOT NULL DEFAULT 0,
    wins   INTEGER NOT NULL DEFAULT 0
);
"""


def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    return con


def update_latest(symbol, interval="1h"):
    """Pull the newest candles; INSERT OR IGNORE keeps it idempotent."""
    con = fetch_data.connect()
    rows = fetch_data.fetch_batch(symbol, interval)
    n = fetch_data.store_batch(con, symbol, interval, rows)
    con.close()
    return n


def get_equity(con, symbol):
    row = con.execute("SELECT equity, trades, wins FROM equity WHERE symbol=?",
                      (symbol,)).fetchone()
    if row is None:
        con.execute("INSERT INTO equity (symbol, equity) VALUES (?,?)",
                    (symbol, START_EQUITY))
        con.commit()
        return START_EQUITY, 0, 0
    return row


def freeze_qualified(results_list, out_path=QUALIFIED):
    """Build the frozen decision file from validated backtest results."""
    frozen = {"frozen_at": datetime.datetime.now(
        datetime.timezone.utc).isoformat(), "symbols": {}}
    for res in results_list:
        sym = res["symbol"]
        wins = [v for v in res["validated_on_test"] if v.get("confirmed")]
        kelly = res.get("kelly") or {}
        half = kelly.get("half_kelly") or 0.0
        frac = max(0.005, min(0.05, half)) if half > 0 else 0.0
        frozen["symbols"][sym] = {
            "windows": [{"kind": v["kind"], "key": v["key"],
                         "direction": v["direction"]} for v in wins],
            "frac": frac,
            "vol_filter": res.get("vol_filter", False),
            "cost": res.get("round_trip_cost", 0.002),
        }
    with open(out_path, "w") as fh:
        json.dump(frozen, fh, indent=2)
    return frozen


def run_once(symbol):
    with open(QUALIFIED) as fh:
        frozen = json.load(fh)
    cfg = frozen["symbols"].get(symbol)
    if not cfg or not cfg["windows"]:
        return {"symbol": symbol, "action": "no qualified windows"}
    update_latest(symbol)
    rows = fetch_data.load_series(symbol, "1h")
    candles = validator.hourly_returns(rows)

    now_ms = int(time.time() * 1000)
    hour_ms = 3600_000
    last_closed_ms = (now_ms // hour_ms) * hour_ms - hour_ms
    candle = next((c for c in candles if c["ms"] == last_closed_ms), None)
    if candle is None:
        return {"symbol": symbol, "action": "latest candle not cached yet"}

    hit = next((w for w in cfg["windows"]
                if w["kind"] == "hourly" and w["key"] == candle["hour"]), None)
    if hit is None:
        return {"symbol": symbol, "action": "hour not qualified",
                "hour": candle["hour"]}

    if cfg["vol_filter"]:
        ok = validator.trailing_vol_filter(candles)
        idx = next(i for i, c in enumerate(candles) if c["ms"] == last_closed_ms)
        if not ok[idx]:
            return {"symbol": symbol, "action": "vol filter blocked",
                    "hour": candle["hour"]}

    r = candle["ret"]
    if hit["direction"] == "short":
        r = -r
    net = r - cfg["cost"]

    con = connect()
    equity, trades, wins = get_equity(con, symbol)
    frac = cfg["frac"]
    equity_after = equity * (1.0 + frac * net)
    try:
        con.execute(
            "INSERT INTO paper_trades (symbol, open_ms, window, direction, "
            "entry, exit, net_ret, frac, equity_before, equity_after) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (symbol, last_closed_ms, f"hour_{candle['hour']:02d}",
             hit["direction"], 0.0, 0.0, net, frac, equity, equity_after))
        con.execute("UPDATE equity SET equity=?, trades=trades+1, "
                    "wins=wins+? WHERE symbol=?",
                    (equity_after, 1 if net > 0 else 0, symbol))
        con.commit()
        logged = True
    except sqlite3.IntegrityError:
        logged = False  # already settled this candle
    con.close()
    return {"symbol": symbol, "action": "logged" if logged else "duplicate",
            "hour": candle["hour"], "direction": hit["direction"],
            "net_ret": net, "equity": equity_after if logged else equity}


def summary(symbol):
    con = connect()
    row = con.execute("SELECT equity, trades, wins FROM equity WHERE symbol=?",
                      (symbol,)).fetchone()
    con.close()
    if row is None:
        return f"{symbol}: no paper trades yet"
    eq, tr, w = row
    wr = (w / tr * 100) if tr else 0.0
    ret = (eq / START_EQUITY - 1.0) * 100
    return (f"{symbol}: {tr} paper trades, win rate {wr:.1f}%, "
            f"equity {eq:.2f} ({ret:+.1f}% from {START_EQUITY:.0f})")


def main(argv):
    if "--freeze" in argv:
        results = []
        for f in sorted(os.listdir(HERE)):
            if f.startswith("results_") and f.endswith(".json"):
                with open(os.path.join(HERE, f)) as fh:
                    results.append(json.load(fh))
        frozen = freeze_qualified(results)
        n = sum(len(s["windows"]) for s in frozen["symbols"].values())
        print(f"froze {n} qualified windows across "
              f"{len(frozen['symbols'])} symbols -> {QUALIFIED}")
        return
    if argv and argv[0].startswith("--summary"):
        syms = json.load(open(QUALIFIED))["symbols"] if os.path.exists(QUALIFIED) else {}
        for s in syms:
            print(summary(s))
        return
    symbols = ["BTCUSDT", "SOLUSDT", "ETHUSDT"]
    for a in argv:
        if a.startswith("--symbols="):
            symbols = a.split("=", 1)[1].split(",")
    for s in symbols:
        print(run_once(s), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
