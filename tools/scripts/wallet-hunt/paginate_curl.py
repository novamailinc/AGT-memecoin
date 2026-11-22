"""Paginate one mint's signatures toward oldest via curl (urllib truncates
large bodies through this egress proxy; curl does not).
Usage: paginate_curl.py <mint> <outfile> [limit=200] [maxpages=2000]
Public RPC only, paced >=6s between pages. HARD STOP on 429.
Atomic saves. Resumes from outfile's oldest signature.
"""
import json, subprocess, time, sys, os

RPC = 'https://api.mainnet-beta.solana.com'
MINT, OUT = sys.argv[1], sys.argv[2]
LIMIT = int(sys.argv[3]) if len(sys.argv) > 3 else 200
MAXPAGES = int(sys.argv[4]) if len(sys.argv) > 4 else 2000

def rpc(method, params):
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    r = subprocess.run(['curl', '-s', '--max-time', '60', '-X', 'POST', RPC,
                        '-H', 'Content-Type: application/json', '-d', payload],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        raise RuntimeError('curl rc=%d %s' % (r.returncode, r.stderr[:200]))
    return json.loads(r.stdout)

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
    try:
        res = rpc('getSignaturesForAddress', [MINT, opts])
    except Exception as e:
        print('%s page %d failed: %s' % (MINT[:8], page, e), flush=True)
        if '429' in str(e):
            print('HARD STOP 429', flush=True)
        break
    if 'error' in res:
        print('%s page %d RPC error: %s' % (MINT[:8], page, res['error']), flush=True)
        if '429' in str(res['error']):
            print('HARD STOP 429', flush=True)
        break
    sigs = res.get('result') or []
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
