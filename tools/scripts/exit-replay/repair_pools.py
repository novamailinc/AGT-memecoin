#!/usr/bin/env python3
import json, subprocess, glob, os
HERE = os.path.dirname(os.path.abspath(__file__))
subset = json.load(open(os.path.join(HERE, 'subset.json')))
t0s = {}
for r in subset:
    t0s.setdefault(r['mint'], []).append(r['t0'])

def curl(url):
    out = subprocess.run(['curl', '-s', '--compressed', '--max-time', '40',
                          '-H', 'User-Agent: exit-replay/1.0', url],
                         capture_output=True, text=True)
    return json.loads(out.stdout)

def fetch_candles(pool, t0):
    before = int(t0 + 8 * 3600 + 3600)
    gt = curl('https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/minute?aggregate=1&limit=1000&before=%d' % (pool, before))
    rows = ((gt.get('data') or {}).get('attributes') or {}).get('ohlcv_list') or []
    return sorted(([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])] for x in rows), key=lambda c: c[0])

def covers(candles, t0):
    return bool(candles) and candles[0][0] <= t0 + 120 and candles[-1][0] >= t0 + 300

for f in sorted(glob.glob(os.path.join(HERE, 'cache', '*.json'))):
    d = json.load(open(f))
    mint = d['mint']
    cs = d.get('candles')
    t0 = min(t0s[mint])
    if cs and covers(cs, t0):
        continue
    print(mint[:12], 'coverage bad -> trying all pools', flush=True)
    try:
        gt = curl('https://api.geckoterminal.com/api/v2/networks/solana/tokens/%s/pools' % mint)
        pools = gt.get('data') or []
        print('   pools found:', len(pools), flush=True)
        fixed = False
        for p in pools[:10]:
            pool = (p.get('attributes') or {}).get('address')
            if not pool:
                continue
            cand = fetch_candles(pool, t0)
            print('   pool', pool[:10], 'candles:', len(cand), flush=True)
            if covers(cand, t0):
                d['pool'] = pool
                d['candles'] = cand
                d['error'] = None
                d['source'] = 'gecko-multipool'
                json.dump(d, open(f, 'w'))
                print('   FIXED', flush=True)
                fixed = True
                break
        if not fixed:
            print('   no pool covers entry window', flush=True)
    except Exception as e:
        print('   repair failed:', type(e).__name__, str(e)[:80], flush=True)
print('REPAIR DONE', flush=True)
