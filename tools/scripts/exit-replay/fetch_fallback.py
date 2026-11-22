#!/usr/bin/env python3
"""Fallback fetch: GeckoTerminal token->pools primary (DexScreener dropped
most of these dead tokens), then 1m OHLCV. Fills cache entries lacking candles."""
import json, os, time, gzip, io
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'cache')
UA = {'User-Agent': 'exit-replay/1.0 (paper research)', 'Accept-Encoding': 'gzip'}

def http_get(url, tries=4):
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
                wait = 12.0 * (i + 1)
            time.sleep(wait)
    raise last

def main():
    subset = json.load(open(os.path.join(HERE, 'subset.json')))
    mints = {}
    for r in subset:
        mints.setdefault(r['mint'], []).append(r)
    done = ok = 0
    for mint, recs in mints.items():
        cpath = os.path.join(CACHE, mint + '.json')
        cur = json.load(open(cpath)) if os.path.exists(cpath) else {}
        if cur.get('candles'):
            continue
        done += 1
        t0 = min(r['t0'] for r in recs)
        entry = {'mint': mint, 'pool': cur.get('pool'), 'candles': None, 'error': None, 'source': 'gecko-pools'}
        try:
            if not entry['pool']:
                gt = http_get('https://api.geckoterminal.com/api/v2/networks/solana/tokens/%s/pools' % mint)
                pools = gt.get('data') or []
                if not pools:
                    entry['error'] = 'no pools on geckoterminal'
                else:
                    def res(p):
                        try: return float((p.get('attributes') or {}).get('reserve_in_usd') or 0)
                        except Exception: return 0.0
                    best = max(pools, key=res)
                    entry['pool'] = (best.get('attributes') or {}).get('address')
                time.sleep(0.5)
            if entry['pool'] and not entry['error']:
                before = int(t0 + 8 * 3600 + 3600)
                url = ('https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/minute'
                       '?aggregate=1&limit=1000&before=%d' % (entry['pool'], before))
                gt = http_get(url)
                rows = ((gt.get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []
                candles = sorted(([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in rows),
                                 key=lambda c: c[0])
                entry['candles'] = candles
                if not candles:
                    entry['error'] = 'no candles from geckoterminal'
                else:
                    entry['error'] = None
                    ok += 1
            time.sleep(0.5)
        except Exception as e:
            entry['error'] = '%s: %s' % (type(e).__name__, e)
        with open(cpath, 'w') as f:
            json.dump(entry, f)
        print(mint[:10], entry['error'] or ('ok %d candles' % len(entry['candles'])), flush=True)
    print('FALLBACK DONE ok=%d of %d retried' % (ok, done))

if __name__ == '__main__':
    main()
