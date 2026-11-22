"""Paginate one mint's signatures toward oldest (public RPC, paced).
Usage: paginate_mint.py <mint> <outfile> [start_before]
Resumes: if outfile exists, loads it and continues from its oldest
signature (or from start_before when given). Saves incrementally;
hard stop on RPC error/429; max 200 pages x 1000 per run.
"""
import json, urllib.request, time, sys, os

RPC = 'https://api.mainnet-beta.solana.com'
MINT, OUT = sys.argv[1], sys.argv[2]
START = sys.argv[3] if len(sys.argv) > 3 else None
MAXPAGES = int(sys.argv[4]) if len(sys.argv) > 4 else 200

def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({'jsonrpc': '2.0', 'id': 1,
                         'method': method, 'params': params}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

all_sigs, before = [], START
if os.path.exists(OUT):
    try:
        all_sigs = json.load(open(OUT))
        if all_sigs and not before:
            before = all_sigs[-1]['signature']
        print('%s resuming from %d saved sigs' % (MINT[:8], len(all_sigs)), flush=True)
    except Exception:
        all_sigs, before = [], START
for page in range(MAXPAGES):
    opts = {'limit': 1000}
    if before:
        opts['before'] = before
    try:
        res = rpc('getSignaturesForAddress', [MINT, opts])
    except Exception as e:
        print('%s page %d failed: %s' % (MINT[:8], page, e), flush=True)
        break
    if 'error' in res:
        print('%s page %d RPC error: %s' % (MINT[:8], page, res['error']), flush=True)
        break
    sigs = res.get('result') or []
    print('%s page %d: %d sigs' % (MINT[:8], page, len(sigs)), flush=True)
    if not sigs:
        break
    all_sigs.extend(sigs)
    before = sigs[-1]['signature']
    # Atomic save: a runtime restart mid-write must never tear the file.
    with open(OUT + '.tmp', 'w') as f:
        json.dump(all_sigs, f)
    os.replace(OUT + '.tmp', OUT)
    if len(sigs) < 1000:
        break
    time.sleep(6)
print('%s TOTAL: %d sigs, oldest bt=%s' % (
    MINT[:8], len(all_sigs),
    all_sigs[-1].get('blockTime') if all_sigs else None), flush=True)
