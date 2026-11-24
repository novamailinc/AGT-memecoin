#!/usr/bin/env python3
"""Extend a symbol's cached history FURTHER BACK in time (Binance).

fetch_data.backfill() walks backward from *now*, so on a DB that already
holds the most recent N candles it just re-fetches the cached range.
This helper instead starts from the oldest cached candle and walks back
from there until target_candles is reached. Additive only — fetch_data.py
itself is untouched.
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data


def extend_back(symbol, interval="1h", target_candles=35064, progress=None):
    con = fetch_data.connect()
    have = con.execute(
        "SELECT COUNT(*) FROM klines WHERE symbol=? AND interval=?",
        (symbol, interval)).fetchone()[0]
    oldest = con.execute(
        "SELECT MIN(open_ms) FROM klines WHERE symbol=? AND interval=?",
        (symbol, interval)).fetchone()[0]
    end_ms = oldest - 1 if oldest else None
    while have < target_candles:
        rows = fetch_data.fetch_batch(symbol, interval, end_ms=end_ms)
        if not rows:
            break
        new = fetch_data.store_batch(con, symbol, interval, rows)
        have += new
        end_ms = int(rows[0][0]) - 1
        if progress:
            progress(symbol, have, target_candles)
        if len(rows) < 1000:
            break  # beginning of exchange history
        time.sleep(0.25)
    con.close()
    return have


def main(argv):
    symbol, interval, years = "BTCUSDT", "1h", 4.0
    for a in argv:
        if a.startswith("--symbol="):
            symbol = a.split("=", 1)[1]
        elif a.startswith("--interval="):
            interval = a.split("=", 1)[1]
        elif a.startswith("--years="):
            years = float(a.split("=", 1)[1])
    per_year = 365.25 * 24 * (4 if interval == "15m" else 1)
    target = int(years * per_year)

    def progress(sym, have, want):
        print(f"  {sym} {interval}: {have}/{want} candles", flush=True)

    have = extend_back(symbol, interval, target, progress)
    print(f"done: {have} {interval} candles for {symbol}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
