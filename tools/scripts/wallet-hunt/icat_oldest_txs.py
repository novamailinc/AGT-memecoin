"""Fetch the OLDEST iCat mint transactions (first buyers window).
Reads ~/workspace/wallet-hunt/icat_signatures.json (newest-first),
fetches the last N (oldest), paced 7s, hard stop on 429, saves
incrementally to icat_oldest_txs.json.
"""
import json, urllib.request, time

RPC = 'https://api.mainnet-beta.solana.com'
N = 60

def rpc(method, params):
    req = urllib.request.Request(
        RPC,
        data=json.dumps({'jsonrpc': '2.0', 'id': 1,
                         'method': method, 'params': params}).encode(),
        headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

sigs = json.load(open('/home/hatch/workspace/wallet-hunt/icat_signatures.json'))
# Reverse so index 0 is the ABSOLUTE oldest (list is newest-first).
oldest = list(reversed(sigs[-N:]))
print('total sigs:', len(sigs), '| fetching oldest:', len(oldest), flush=True)
# Resume-aware: skip signatures already saved from a prior partial run.
out = []
try:
    out = json.load(open('/home/hatch/workspace/wallet-hunt/icat_oldest_txs.json'))
    done = {o['sig'] for o in out}
    oldest = [s for s in oldest if s['signature'] not in done]
    print('resuming: %d already saved, %d left' % (len(out), len(oldest)), flush=True)
except Exception:
    pass
for i, s in enumerate(oldest):
    sig = s['signature']
    try:
        tx = rpc('getTransaction', [sig, {'maxSupportedTransactionVersion': 1}])
        out.append({'sig': sig, 'blockTime': s.get('blockTime'), 'tx': (tx.get('result') or {})})
        print(f'fetched {i+1}/{len(oldest)} bt={s.get("blockTime")}', flush=True)
    except Exception as e:
        print(f'fetch {i} failed (stopping): {e}', flush=True)
        break
    with open('/home/hatch/workspace/wallet-hunt/icat_oldest_txs.json', 'w') as f:
        json.dump(out, f)
    time.sleep(7)
print('oldest txs saved:', len(out), flush=True)
