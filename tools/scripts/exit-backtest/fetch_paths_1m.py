"""Fetch 1-min candle paths for every paper close. Cached, retried, resumable, parallel."""
import json, os, time, http.client, urllib.request
from concurrent.futures import ThreadPoolExecutor

DATA = '/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl'
CACHE = '/tmp/bt_paths_1m'
os.makedirs(CACHE, exist_ok=True)

def fetch(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
            return json.loads(urllib.request.urlopen(req, timeout=60).read())
        except http.client.IncompleteRead as e:
            try:
                return json.loads(e.partial)
            except Exception:
                pass
        except Exception:
            time.sleep(2 + i)
    return None

# mint -> pool cache (reuse from 15m run)
POOL = {}
import glob
for f in glob.glob('/tmp/bt_paths/*.json'):
    if os.path.basename(f).startswith('_'):
        continue
    try:
        d = json.load(open(f))
        POOL[d['mint']] = d['pool']
    except Exception:
        pass
print('pools cached:', len(POOL), flush=True)

def fetch_one(r):
    mint = r['mint']
    cp = os.path.join(CACHE, mint + '.json')
    if os.path.exists(cp):
        return ('cached', None)
    pool = POOL.get(mint)
    if not pool:
        d = fetch('https://api.geckoterminal.com/api/v2/networks/solana/tokens/%s/pools?page=1' % mint)
        pools = d.get('data', []) if d else []
        if not pools:
            return ('failed', (mint, 'no pools'))
        pool = pools[0]['id'].split('_', 1)[1]
    t0, t1 = r['t_entry'] - 900, r['t_close'] + 900
    url = ('https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/minute'
           '?aggregate=1&limit=200&before_timestamp=%d' % (pool, int(t1)))
    d = fetch(url)
    cl = d.get('data', {}).get('attributes', {}).get('ohlcv_list', []) if d else []
    cl = sorted([c for c in cl if c[0] >= t0], key=lambda c: c[0])
    if len(cl) < 5:
        return ('failed', (mint, 'only %d candles' % len(cl)))
    json.dump({'mint': mint, 'symbol': r.get('symbol'), 'pool': pool,
               't_entry': r['t_entry'], 't_close': r['t_close'],
               'realized_mult': r.get('realized_mult'), 'exit_reason': r.get('exit_reason'),
               'peak_mult': r.get('peak_mult'), 'candles': cl}, open(cp, 'w'))
    return ('ok', None)

def main():
    closes = []
    for line in open(DATA):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('realized_mult') is not None and r.get('t_entry') and r.get('t_close'):
            closes.append(r)
    print('closes: %d' % len(closes), flush=True)
    total = len(closes)
    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, res in enumerate(ex.map(fetch_one, closes)):
            status, info = res
            if status == 'failed':
                failed.append(info)
            else:
                done += 1
            if i % 20 == 0:
                print('progress %d/%d done=%d failed=%d' % (i, total, done, len(failed)), flush=True)
    print('FINAL done=%d failed=%d' % (done, len(failed)), flush=True)
    json.dump(failed, open(os.path.join(CACHE, '_failed.json'), 'w'))

if __name__ == '__main__':
    main()
