"""Paginate one mint's signatures toward oldest, small pages + retries.
Usage: paginate_small.py <mint> <outfile> [limit=200] [maxpages=1000]
Public RPC only, paced >=6s between pages, retry on truncated reads
(up to 5 attempts per page). HARD STOP on 429. Atomic saves.
Resumes from outfile's oldest signature.
"""
import json, urllib.request, time, sys, os

RPC = 'https://api.mainnet-beta.solana.com'
MINT, OUT = sys.argv[1], sys.argv[2]
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 200
MAXPAGES = int(sys.argv[4]) if len(sys.argv) > 4 else 1000

def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({'jsonrpc': '2.0', 'id': 1,
                         'method': method, 'params': params}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

all_sigs, before = [], None
if os.path.exists(OUT):
    try:
        all_sigs = json.load(open(OUT))
        if all_sigs:
            before = all_sigs[-1]['signature']
        print('%s resuming from %d saved sigs' % (MINT[:8], len(all_sigs)), flush=True)
    except Exception:
        all_sigs, before = [], None
for page in range(MAXPAGES):
    opts = {'limit': LIMIT}
    if before:
        opts['before'] = before
    sigs = None
    for attempt in range(5):
        try:
            res = rpc('getSignaturesForAddress', [MINT, opts])
        except Exception as e:
            print('%s page %d attempt %d failed: %s' % (MINT[:8], page, attempt, e), flush=True)
            if '429' in str(e):
                print('HARD STOP 429', flush=True)
                sys.exit(0)
            time.sleep(7)
            continue
        if 'error' in res:
            print('%s page %d RPC error: %s' % (MINT[:8], page, res['error']), flush=True)
            if '429' in str(res['error']):
                print('HARD STOP 429', flush=True)
                sys.exit(0)
            break
        sigs = res.get('result') or []
        break
    if sigs is None:
        print('%s page %d: giving up after retries' % (MINT[:8], page), flush=True)
        break
    print('%s page %d: %d sigs (total %d)' % (MINT[:8], page, len(sigs), len(all_sigs) + len(sigs)), flush=True)
    if not sigs:
        break
    all_sigs.extend(sigs)
    before = sigs[-1]['signature']
    with open(OUT + '.tmp', 'w') as f:
        json.dump(all_sigs, f)
    os.replace(OUT + '.tmp', OUT)
    if len(sigs) < LIMIT:
        break
    time.sleep(6)
print('%s TOTAL: %d sigs, oldest bt=%s' % (
    MINT[:8], len(all_sigs),
    all_sigs[-1].get('blockTime') if all_sigs else None), flush=True)
