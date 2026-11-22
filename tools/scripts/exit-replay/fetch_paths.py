#!/usr/bin/env python3
"""Fetch DexScreener pool + GeckoTerminal 1m OHLCV for the peaked>=2x subset.
Caches everything under cache/. Free APIs only. Polite pacing."""
import json, os, sys, time, gzip, io
import urllib.request, urllib.error

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache')
os.makedirs(CACHE, exist_ok=True)
UA = {'User-Agent': 'exit-replay/1.0 (paper research)', 'Accept-Encoding': 'gzip'}

def http_get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip' or raw[:2] == b'\x1f\x8b':
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return json.loads(raw.decode('utf-8'))
        except Exception as e:
            last = e
            wait = 2.0 * (i + 1)
            if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                wait = 10.0 * (i + 1)
            time.sleep(wait)
    raise last

def main():
    subset = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'subset.json')))
    mints = {}
    for r in subset:
        mints.setdefault(r['mint'], []).append(r)
    status = {}
    for mint, recs in mints.items():
        t0 = min(r['t0'] for r in recs)
        cpath = os.path.join(CACHE, mint + '.json')
        if os.path.exists(cpath):
            status[mint] = 'cached'
            continue
        entry = {'mint': mint, 'pool': None, 'candles': None, 'error': None}
        try:
            ds = http_get('https://api.dexscreener.com/tokens/v1/solana/' + mint)
            pairs = ds if isinstance(ds, list) else (ds.get('pairs') or [])
            pairs = [p for p in pairs if p.get('chainId') in (None, 'solana')]
            if not pairs:
                entry['error'] = 'no pairs on dexscreener'
            else:
                def liq(p):
                    try: return float((p.get('liquidity') or {}).get('usd') or 0)
                    except Exception: return 0.0
                best = max(pairs, key=liq)
                entry['pool'] = best.get('pairAddress')
                entry['dex'] = best.get('dexId')
            time.sleep(0.45)
            if entry['pool']:
                before = int(t0 + 8 * 3600 + 3600)  # cover entry -> entry+8h
                url = ('https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/minute'
                       '?aggregate=1&limit=1000&before=%d' % (entry['pool'], before))
                gt = http_get(url)
                rows = ((gt.get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []
                # rows: [ts, open, high, low, close, volume] (newest first)
                candles = sorted(([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in rows),
                                 key=lambda c: c[0])
                entry['candles'] = candles
                if not candles:
                    entry['error'] = 'no candles from geckoterminal'
            time.sleep(0.45)
        except Exception as e:
            entry['error'] = '%s: %s' % (type(e).__name__, e)
        with open(cpath, 'w') as f:
            json.dump(entry, f)
        status[mint] = entry['error'] or ('ok %d candles' % len(entry['candles'] or []))
        print(mint[:10], status[mint], flush=True)
    ok = sum(1 for v in status.values() if v.startswith('ok') or v == 'cached')
    print('DONE ok=%d/%d' % (ok, len(status)))

if __name__ == '__main__':
    main()
