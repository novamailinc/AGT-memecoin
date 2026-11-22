#!/usr/bin/env python3
"""Fetch hourly klines from Binance's public market-data endpoint and cache in SQLite.

No API key needed. Uses the unrestricted public data host
https://data-api.binance.vision (plain market data, no geo block).
"""
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = "https://data-api.binance.vision"
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "klines.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    symbol   TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_ms  INTEGER NOT NULL,
    open     REAL NOT NULL,
    high     REAL NOT NULL,
    low      REAL NOT NULL,
    close    REAL NOT NULL,
    volume   REAL NOT NULL,
    PRIMARY KEY (symbol, interval, open_ms)
);
"""


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "temporal-edge-validator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_batch(symbol, interval, end_ms=None, limit=1000):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    url = BASE + "/api/v3/klines?" + urllib.parse.urlencode(params)
    return _get(url)


def connect():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute(SCHEMA)
    return con


def max_open_ms(con, symbol, interval):
    row = con.execute(
        "SELECT MAX(open_ms) FROM klines WHERE symbol=? AND interval=?",
        (symbol, interval),
    ).fetchone()
    return row[0]


def store_batch(con, symbol, interval, rows):
    data = [
        (symbol, interval, int(r[0]), float(r[1]), float(r[2]),
         float(r[3]), float(r[4]), float(r[5]))
        for r in rows
    ]
    con.executemany(
        "INSERT OR IGNORE INTO klines "
        "(symbol, interval, open_ms, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?,?)",
        data,
    )
    con.commit()
    return len(data)


def backfill(symbol, interval="1h", target_candles=17520, progress=None):
    """Walk backwards from now until we hold target_candles (default ~2y hourly)."""
    con = connect()
    have = con.execute(
        "SELECT COUNT(*) FROM klines WHERE symbol=? AND interval=?",
        (symbol, interval),
    ).fetchone()[0]
    end_ms = None
    batches = 0
    while have < target_candles:
        rows = fetch_batch(symbol, interval, end_ms=end_ms)
        if not rows:
            break
        store_batch(con, symbol, interval, rows)
        batches += 1
        have += len(rows)
        end_ms = int(rows[0][0]) - 1  # step before the oldest candle fetched
        if progress:
            progress(symbol, have, target_candles)
        if len(rows) < 1000:
            break  # reached the beginning of available history
        time.sleep(0.25)  # be polite to the public endpoint
    con.close()
    return have


def load_series(symbol, interval="1h"):
    """Return list of (open_ms, open, high, low, close, volume), oldest first."""
    con = connect()
    rows = con.execute(
        "SELECT open_ms, open, high, low, close, volume FROM klines "
        "WHERE symbol=? AND interval=? ORDER BY open_ms",
        (symbol, interval),
    ).fetchall()
    con.close()
    return rows


def main(argv):
    symbols = ["BTCUSDT", "SOLUSDT", "ETHUSDT"]
    years = 2.0
    for a in argv:
        if a.startswith("--symbols="):
            symbols = a.split("=", 1)[1].split(",")
        elif a.startswith("--years="):
            years = float(a.split("=", 1)[1])
    target = int(years * 365.25 * 24)

    def progress(sym, have, want):
        print(f"  {sym}: {have}/{want} candles", flush=True)

    for sym in symbols:
        print(f"Backfilling {sym} ...", flush=True)
        have = backfill(sym, "1h", target, progress)
        print(f"  done: {have} hourly candles cached", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
