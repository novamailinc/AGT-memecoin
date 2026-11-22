#!/usr/bin/env python3
"""Fetch hourly BTC/USD klines from Coinbase Exchange public API (no key).

Cross-venue check for Tier 2: does anything found on Binance replicate on
an independent exchange's data? Stored in the same klines.db as
symbol=COINBASE_BTCUSD, interval=1h.

Coinbase serves max 300 candles per request but accepts arbitrary
start/end, so we walk forward from (now - target span) to the present.
Response rows are [time, low, high, open, close, volume], newest first.
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data

BASE = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
SYMBOL = "COINBASE_BTCUSD"
PAGE = 300  # max candles per request


def _get(start_dt, end_dt):
    params = {"granularity": 3600,
              "start": start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
              "end": end_dt.strftime("%Y-%m-%dT%H:%M:%S")}
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "temporal-edge-validator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def backfill(target_candles=17520, progress=None):
    now = datetime.datetime.now(datetime.timezone.utc).replace(minute=0,
                                                              second=0,
                                                              microsecond=0)
    start = now - datetime.timedelta(hours=target_candles + 6)
    all_rows = []
    cursor = start
    while cursor < now:
        chunk_end = min(cursor + datetime.timedelta(hours=PAGE), now)
        batch = _get(cursor, chunk_end)
        # rows: [time, low, high, open, close, volume], newest first
        for r in batch:
            ts = int(r[0])
            if ts < now.timestamp() - 3600:  # drop still-forming candle
                all_rows.append((ts * 1000, float(r[3]), float(r[2]),
                                 float(r[1]), float(r[4]), float(r[5])))
        cursor = chunk_end
        if progress:
            progress(len(all_rows), target_candles)
        if not batch:
            break
        time.sleep(0.3)  # be polite to the public endpoint
    # de-dup by open_ms, oldest first
    seen = set()
    uniq = []
    for r in sorted(all_rows):
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
    con = fetch_data.connect()
    n = fetch_data.store_batch(con, SYMBOL, "1h", uniq)
    con.close()
    return n


def main(argv):
    years = 2.0
    for a in argv:
        if a.startswith("--years="):
            years = float(a.split("=", 1)[1])
    target = int(years * 365.25 * 24)

    def progress(have, want):
        print(f"  {SYMBOL}: {have}/{want} candles", flush=True)

    n = backfill(target, progress)
    print(f"done: {n} Coinbase hourly candles stored", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
