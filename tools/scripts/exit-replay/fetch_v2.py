#!/usr/bin/env python3
"""Fetch v2: correct GeckoTerminal OHLCV params (after_timestamp /
before_timestamp). For each mint, try its pools (reserve desc) and keep the
first whose 1m candles genuinely cover the entry window."""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, 'cache2')
os.makedirs(CACHE, exist_ok=True)

def curl(url, tries=3):
    last = None
    for i in range(tries):
        try:
            out = subprocess.run(['curl', '-s', '--compressed', '--max-time', '45',
                                  '-H', 'User-Agent: exit-replay/1.0', url],
                                 capture_output=True, text=True)
            return json.loads(out.stdout)
        except Exception as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last

def ohlcv(pool, t0):
    after = int(t0 - 600)
    before = int(t0 + 8 * 3600)
    gt = curl('https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/minute'
              '?aggregate=1&limit=1000&after_timestamp=%d&before_timestamp=%d' % (pool, after, before))
    rows = ((gt.get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []
    return sorted(([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in rows),
                  key=lambda c: c[0])

def usable(candles, t0):
    if len(candles) < 3:
        return False
    after = [c for c in candles if c[0] + 60 >= t0]
    if not after:
        return False
    first = after[0]
    if first[0] - t0 > 1800:  # first trade >30m after entry: wrong/stale pool
        return False
    return True

def main():
    subset = json.load(open(os.path.join(HERE, 'subset.json')))
    t0s = {}
    for r in subset:
        t0s.setdefault(r['mint'], []).append(r['t0'])
    for mint, ts in sorted(t0s.items()):
        t0 = min(ts)
        cpath = os.path.join(CACHE, mint + '.json')
        if os.path.exists(cpath):
            print(mint[:10], 'cached', flush=True)
            continue
        entry = {'mint': mint, 'pool': None, 'candles': None, 'error': None}
        try:
            gt = curl('https://api.geckoterminal.com/api/v2/networks/solana/tokens/%s/pools' % mint)
            pools = gt.get('data') or []
            def res(p):
                try: return float((p.get('attributes') or {}).get('reserve_in_usd') or 0)
                except Exception: return 0.0
            pools.sort(key=res, reverse=True)
            tried = []
            for p in pools[:6]:
                pool = (p.get('attributes') or {}).get('address')
                if not pool:
                    continue
                cand = ohlcv(pool, t0)
                tried.append((pool, len(cand)))
                time.sleep(0.45)
                if usable(cand, t0):
                    entry['pool'] = pool
                    entry['candles'] = cand
                    break
            if not entry['candles']:
                # keep the largest candle set as a last resort for the record
                if tried:
                    pool, _ = max(tried, key=lambda x: x[1])
                    cand = ohlcv(pool, t0)
                    if cand:
                        entry['pool'] = pool
                        entry['candles'] = cand
                        entry['error'] = 'window-not-covered'
                    else:
                        entry['error'] = 'no candles any pool'
                else:
                    entry['error'] = 'no pools'
        except Exception as e:
            entry['error'] = '%s: %s' % (type(e).__name__, e)
        with open(cpath, 'w') as f:
            json.dump(entry, f)
        print(mint[:10], entry['error'] or ('ok %d candles pool %s' % (len(entry['candles']), entry['pool'][:8])), flush=True)
        time.sleep(0.45)
    print('FETCH2 DONE', flush=True)

if __name__ == '__main__':
    main()
