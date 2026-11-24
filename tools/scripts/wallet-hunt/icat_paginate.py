"""Paginate iCat mint signatures toward oldest (public RPC, paced).
Saves progress incrementally so a 429 stop still leaves partial data.
Hard stop on persistent 429. Max 60 pages x 1000 sigs.
"""
import json, urllib.request, time, sys

RPC = 'https://api.mainnet-beta.solana.com'
MINT = 'FHHxyowUnk6oVKF9FV1U9zyt9YFEr2vu68gbpkKcpump'
OUT = '/home/hatch/workspace/wallet-hunt/icat_signatures.json'

def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({'jsonrpc': '2.0', 'id': 1,
                         'method': method, 'params': params}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

all_sigs = []
before = None
for page in range(60):
    opts = {'limit': 1000}
    if before:
        opts['before'] = before
    try:
        res = rpc('getSignaturesForAddress', [MINT, opts])
    except Exception as e:
        print('page %d request failed: %s' % (page, e), flush=True)
        break
    if 'error' in res:
        print('page %d RPC error: %s' % (page, res['error']), flush=True)
        break
    sigs = res.get('result') or []
    print('page %d: %d sigs' % (page, len(sigs)), flush=True)
    if not sigs:
        break
    all_sigs.extend(sigs)
    before = sigs[-1]['signature']
    with open(OUT, 'w') as f:
        json.dump(all_sigs, f)
    if len(sigs) < 1000:
        break
    time.sleep(6)

print('TOTAL sigs saved:', len(all_sigs), flush=True)
if all_sigs:
    print('newest blockTime:', all_sigs[0].get('blockTime'), flush=True)
    print('oldest blockTime:', all_sigs[-1].get('blockTime'), flush=True)
