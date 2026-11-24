#!/usr/bin/env python3
"""Fetch hourly BTC/USD klines from Kraken's public OHLC endpoint (no key).

Cross-venue check for Tier 2: does anything found on Binance replicate on
an independent exchange's data? Stored in the same klines.db as
symbol=KRAKEN_XBTUSD, interval=1h.

Kraken returns up to 720 candles per request; paginate with `since`.
The trailing (still-forming) candle is dropped on every page.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data

BASE = "https://api.kraken.com/0/public/OHLC"
SYMBOL = "KRAKEN_XBTUSD"
PAIR = "XBTUSD"
INTERVAL = 60  # minutes
PAGE = 720


def _get(params):
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "temporal-edge-validator/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def backfill(target_candles=17520, progress=None):
    """Page FORWARD from (now - target span) to the present.

    Kraken's `since` returns candles NEWER than the given time, so we start
    at the oldest wanted timestamp and walk forward. The still-forming
    trailing candle is dropped at the end.
    """
    import time as _time
    con = fetch_data.connect()
    now = int(_time.time())
    start = now - int(target_candles * 3600) - 7200  # small overlap margin
    all_rows = []
    since = start
    while True:
        d = _get({"pair": PAIR, "interval": INTERVAL, "since": since})
        if d.get("error"):
            raise RuntimeError(f"Kraken error: {d['error']}")
        key = next(k for k in d["result"] if k != "last")
        batch = d["result"][key]
        if not batch:
            break
        all_rows.extend(batch)
        since = batch[-1][0] + 1
        if progress:
            progress(len(all_rows), target_candles)
        if len(batch) < PAGE or batch[-1][0] >= now - 3600:
            break  # caught up to the present
        time.sleep(1.0)  # Kraken is strict about rate limits; be polite
    # drop the still-forming candle and anything outside the wanted span
    cutoff_oldest = now - int(target_candles * 3600)
    rows = [(int(r[0]) * 1000, float(r[1]), float(r[2]), float(r[3]),
             float(r[4]), float(r[6]))
            for r in all_rows
            if cutoff_oldest <= int(r[0]) < now - 3600]
    # de-dup (pages can overlap by one candle)
    seen = set()
    uniq = []
    for r in rows:
        if r[0] not in seen:
            seen.add(r[0])
            uniq.append(r)
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
    print(f"done: {n} Kraken hourly candles stored", flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
