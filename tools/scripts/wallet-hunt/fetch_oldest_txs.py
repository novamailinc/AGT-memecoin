"""Fetch the N oldest transactions for a mint (public RPC, paced).
Usage: fetch_oldest_txs.py <sigfile> <outfile> [count=100]
Oldest-first, resume-aware (skips sigs already in outfile).
Pace 7s; HARD STOP on 429 (rate limit) — never switches endpoints.
"""
import json, urllib.request, time, sys, os

RPC = 'https://api.mainnet-beta.solana.com'
SIGFILE, OUT = sys.argv[1], sys.argv[2]
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 100

def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({'jsonrpc': '2.0', 'id': 1,
                         'method': method, 'params': params}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

sigs = json.load(open(SIGFILE))
oldest = [s['signature'] for s in reversed(sigs[-COUNT:])]
done = {}
if os.path.exists(OUT):
    try:
        done = json.load(open(OUT))
    except Exception:
        done = {}
print('target %d oldest, already have %d' % (len(oldest), len(done)), flush=True)
for i, sig in enumerate(oldest):
    if sig in done:
        continue
    try:
        res = rpc('getTransaction', [sig, {'encoding': 'jsonParsed',
                                           'maxSupportedTransactionVersion': 1}])
    except Exception as e:
        print('fetch failed %s: %s' % (sig[:12], e), flush=True)
        if '429' in str(e):
            break
        done[sig] = {'error': str(e)}
        continue
    if 'error' in res:
        print('RPC error %s: %s' % (sig[:12], res['error']), flush=True)
        if '429' in str(res['error']):
            break
        done[sig] = {'error': str(res['error'])}
    else:
        done[sig] = res.get('result')
    # Atomic save: a runtime restart mid-write must never tear the file.
    with open(OUT + '.tmp', 'w') as f:
        json.dump(done, f)
    os.replace(OUT + '.tmp', OUT)
    if (i + 1) % 10 == 0:
        print('fetched %d/%d' % (i + 1, len(oldest)), flush=True)
    time.sleep(7)
print('DONE have %d/%d' % (len(done), len(oldest)), flush=True)
