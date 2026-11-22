"""Re-fetch 1m + 5m candle paths for every paper close into a PERSISTENT
cache (~/workspace/exit-backtest/cache) so VM restarts stop wiping /tmp.
Same GeckoTerminal logic as fetch_paths_1m.py + fetch_5m_tail.py.
Resumable: skips mints already cached."""
import json, os, time, http.client, urllib.request, glob
from concurrent.futures import ThreadPoolExecutor

DATA = '/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl'
CACHE = os.path.expanduser('~/workspace/exit-backtest/cache')
os.makedirs(CACHE, exist_ok=True)


def fetch(url, tries=6, timeout=90):
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': 'Mozilla/5.0',
                              'Accept-Encoding': 'identity'})
            return json.loads(urllib.request.urlopen(req,
                                                    timeout=timeout).read())
        except http.client.IncompleteRead as e:
            try:
                return json.loads(e.partial)
            except Exception:
                pass
        except Exception:
            time.sleep(2 + i)
    return None


def fetch_1m(r):
    mint = r['mint']
    cp = os.path.join(CACHE, mint + '.json')
    if os.path.exists(cp):
        return ('cached', None)
    d = fetch('https://api.geckoterminal.com/api/v2/networks/solana/tokens/%s/pools?page=1' % mint,
              timeout=60)
    pools = d.get('data', []) if d else []
    if not pools:
        return ('failed', (mint, 'no pools'))
    pool = pools[0]['id'].split('_', 1)[1]
    t0, t1 = r['t_entry'] - 900, r['t_close'] + 900
    url = ('https://api.geckoterminal.com/api/v2/networks/solana/pools/%s/ohlcv/minute'
           '?aggregate=1&limit=200&before_timestamp=%d' % (pool, int(t1)))
    d = fetch(url, timeout=60)
    cl = d.get('data', {}).get('attributes', {}).get('ohlcv_list', []) if d else []
    cl = sorted([c for c in cl if c[0] >= t0], key=lambda c: c[0])
    if len(cl) < 5:
        return ('failed', (mint, 'only %d candles' % len(cl)))
    json.dump({'mint': mint, 'symbol': r.get('symbol'), 'pool': pool,
               't_entry': r['t_entry'], 't_close': r['t_close'],
               'realized_mult': r.get('realized_mult'),
               'exit_reason': r.get('exit_reason'),
               'peak_mult': r.get('peak_mult'), 'candles': cl},
              open(cp, 'w'))
    return ('ok', None)


def fetch_5m(mint):
    cp = os.path.join(CACHE, mint + '.json')
    try:
        d = json.load(open(cp))
    except Exception:
        return ('failed', (mint, 'no cache'))
    if 'candles_5m' in d:
        return ('cached', None)
    pool, t_entry = d['pool'], d['t_entry']
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
    closes = []
    for line in open(DATA):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('realized_mult') is not None and r.get('t_entry') and r.get('t_close'):
            closes.append(r)
    print('closes in dataset: %d' % len(closes), flush=True)
    done, failed = 0, 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (status, _) in enumerate(ex.map(fetch_1m, closes)):
            if status == 'failed':
                failed += 1
            else:
                done += 1
            if i % 20 == 0:
                print('1m progress %d/%d done=%d failed=%d' % (i, len(closes), done, failed), flush=True)
    print('1m FINAL done=%d failed=%d' % (done, failed), flush=True)
    mints = [r['mint'] for r in closes]
    done, failed = 0, 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (status, _) in enumerate(ex.map(fetch_5m, mints)):
            if status == 'failed':
                failed += 1
            else:
                done += 1
            if i % 20 == 0:
                print('5m progress %d/%d done=%d failed=%d' % (i, len(mints), done, failed), flush=True)
    print('5m FINAL done=%d failed=%d' % (done, failed), flush=True)
    n = len([f for f in glob.glob(os.path.join(CACHE, '*.json')) if not os.path.basename(f).startswith('_')])
    print('cached mints: %d' % n, flush=True)


if __name__ == '__main__':
    main()
