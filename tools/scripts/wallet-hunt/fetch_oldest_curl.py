"""Fetch the N oldest transactions for a mint via curl (urllib truncates
large bodies through this egress proxy; curl does not).
Usage: fetch_oldest_curl.py <sigfile> <outfile> [count=100]
Oldest-first, resume-aware (skips sigs already in outfile).
Pace 7s; HARD STOP on 429 — never switches endpoints.
Tx file is a dict keyed by signature. Atomic saves.
"""
import json, subprocess, time, sys, os

RPC = 'https://api.mainnet-beta.solana.com'
SIGFILE, OUT = sys.argv[1], sys.argv[2]
COUNT = int(sys.argv[3]) if len(sys.argv) > 3 else 100

def rpc(method, params):
    payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
    r = subprocess.run(['curl', '-s', '--max-time', '90', '-X', 'POST', RPC,
                        '-H', 'Content-Type: application/json', '-d', payload],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError('curl rc=%d %s' % (r.returncode, r.stderr[:200]))
    return json.loads(r.stdout)

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
            print('HARD STOP 429', flush=True)
            break
        done[sig] = {'error': str(e)}
        continue
    if 'error' in res:
        print('RPC error %s: %s' % (sig[:12], res['error']), flush=True)
        if '429' in str(res['error']):
            print('HARD STOP 429', flush=True)
            break
        done[sig] = {'error': str(res['error'])}
    else:
        done[sig] = res.get('result')
    with open(OUT + '.tmp', 'w') as f:
        json.dump(done, f)
    os.replace(OUT + '.tmp', OUT)
    if (i + 1) % 10 == 0:
        print('fetched %d/%d' % (i + 1, len(oldest)), flush=True)
    time.sleep(7)
print('DONE have %d/%d' % (len(done), len(oldest)), flush=True)
