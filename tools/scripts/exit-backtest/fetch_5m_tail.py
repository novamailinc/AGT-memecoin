"""Fetch 5-min candles t_entry -> t_entry+25h for half-B's long tail."""
import json, os, time, http.client, urllib.request, glob
from concurrent.futures import ThreadPoolExecutor

DATA = '/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl'
CACHE = '/tmp/bt_paths_1m'

POOL = {}
for f in glob.glob(os.path.join(CACHE, '*.json')):
    if os.path.basename(f).startswith('_'):
        continue
    try:
        d = json.load(open(f))
        POOL[d['mint']] = (d['pool'], d['t_entry'])
    except Exception:
        pass

def fetch(url, tries=6):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept-Encoding': 'identity'})
            return json.loads(urllib.request.urlopen(req, timeout=90).read())
        except http.client.IncompleteRead as e:
            try:
                return json.loads(e.partial)
            except Exception:
                pass
        except Exception:
            time.sleep(2 + i)
    return None

def fetch_one(item):
    mint, (pool, t_entry) = item
    cp = os.path.join(CACHE, mint + '.json')
    try:
        d = json.load(open(cp))
    except Exception:
        return ('failed', (mint, 'no cache'))
    if 'candles_5m' in d:
        return ('cached', None)
    t1 = t_entry + 25 * 3600
    url = ('https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/minute'
           '?aggregate=5&limit=300&before_timestamp=%d' % (pool, int(t1)))
    r = fetch(url)
    cl = r.get('data', {}).get('attributes', {}).get('ohlcv_list', []) if r else []
    cl = sorted([c for c in cl if c[0] >= t_entry - 900], key=lambda c: c[0])
    if len(cl) < 10:
        return ('failed', (mint, 'only %d 5m candles' % len(cl)))
    d['candles_5m'] = cl
    json.dump(d, open(cp, 'w'))
    return ('ok', None)

def main():
    items = list(POOL.items())
    print('mints:', len(items), flush=True)
    done, failed = 0, []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, res in enumerate(ex.map(fetch_one, items)):
            s, info = res
            if s == 'failed':
                failed.append(info)
            else:
                done += 1
            if i % 20 == 0:
                print('progress %d/%d done=%d failed=%d' % (i, len(items), done, len(failed)), flush=True)
    print('FINAL done=%d failed=%d' % (done, len(failed)), flush=True)

if __name__ == '__main__':
    main()
