"""Thorough diagnosis of the tranche and grad_raw lanes, from live ledgers + verdict chain.
Paper USD only. Phantoms excluded. WAT day boundary Oct 31 = 1793401200, Oct 29 08:24 WAT (raw halt) = 1793257440."""
import json, collections

FA = '/home/hatch/.forex-agent/'
OCT31 = 1793401200
RAW_HALT = 1793257440  # Oct 29 ~08:24 WAT

def stats(closes, label):
    n = len(closes)
    if not n:
        print(f'{label}: 0 closes'); return
    wins = sum(1 for c in closes if c['pnl'] > 0)
    net = sum(c['pnl'] for c in closes)
    top = sorted(closes, key=lambda c: c['pnl'], reverse=True)[:5]
    top_share = sum(c['pnl'] for c in top)
    print(f'{label}: {n} closes, {wins} wins ({100*wins/n:.1f}%), net ${net:+.2f}, exp ${net/n:+.2f}/close, top5 ${top_share:+.2f} ({100*top_share/net if net else 0:.0f}% of net)' if net else f'{label}: {n} closes, {wins} wins, net ${net:+.2f}')
    return net

def reason_mix(closes, label):
    mix = collections.Counter(c['reason'] for c in closes)
    pnl = collections.defaultdict(float)
    for c in closes: pnl[c['reason']] += c['pnl']
    print(f'  {label} exit mix:', {k: f"{v}c/${pnl[k]:+.0f}" for k, v in mix.most_common()})

# ---------------- TRANCHE (ledger, complete: 164 closes) ----------------
t = json.load(open(FA + 'tranche_ledger.json'))
tc = [{'pnl': c['pnl_usd'], 'mult': c.get('realized_mult', 0), 'ts': c['close_ts'],
       'reason': c.get('closed_reason', '?'), 'peak': c.get('peak_mult', 0),
       'legs': c.get('legs') or {}, 'buyback': 'buyback' in json.dumps(c).lower(),
       'mint': c['mint']} for c in t['closes']]
tc.sort(key=lambda c: c['ts'])
print('=== TRANCHE (ledger) ===')
stats(tc, 'all-time')
stats(tc[:100], 'verdict window (first 100)')
stats(tc[100:], 'post-verdict (101+)')
stats([c for c in tc if c['ts'] >= OCT31], 'today Oct 31')
reason_mix(tc, 'all-time')
reason_mix(tc[100:], 'post-verdict')
# leg banking rates
for leg, bank_marker in [('a', 'leg_a'), ('b', 'leg_b'), ('c', 'leg_c')]:
    reasons = collections.Counter()
    for c in tc:
        lv = c['legs'].get(leg)
        if isinstance(lv, dict): reasons[lv.get('exit_reason', '?')] += 1
    print(f'  leg {leg.upper()} reasons:', dict(reasons))
# peak leakage: peaked >=1.5x but still lost, peaked >=3x but still lost
for thr in (1.5, 3.0):
    peaked = [c for c in tc if (c['peak'] or 0) >= thr]
    lost = [c for c in peaked if c['pnl'] <= 0]
    print(f'  peaked >={thr}x: {len(peaked)}, of which lost money: {len(lost)} (${sum(c["pnl"] for c in lost):+.2f})')
bb = [c for c in tc if c['buyback']]
print(f'  buyback closes: {len(bb)}, wins {sum(1 for c in bb if c["pnl"]>0)}, net ${sum(c["pnl"] for c in bb):+.2f}')
pos = t.get('positions') or {}
print(f'  open now: {len(pos)}')
import time
now = time.time()
for m, p in pos.items():
    age_h = (now - p.get('entry_ts', now)) / 3600
    print(f'    open {m[:12]} age {age_h:.1f}h peak {p.get("peak_mult", 0):.2f}x')

# ---------------- GRAD_RAW (verdict chain, phantom excluded) ----------------
raw_chain = []
for line in open(FA + 'verdict_ledger/events.jsonl'):
    try: e = json.loads(line)
    except Exception: continue
    if e.get('lane') != 'grad_raw' or e.get('kind') != 'close': continue
    p = e['payload']
    if p['mint'].startswith('274rXqt'): continue  # voided phantom
    if abs(p['pnl_usd']) > 10000: continue
    raw_chain.append({'pnl': p['pnl_usd'], 'mult': p.get('realized_mult', 0), 'ts': e.get('ts', 0),
                      'reason': p.get('closed_reason', '?'), 'mint': p['mint']})
raw_chain.sort(key=lambda c: c['ts'])
print('\n=== GRAD_RAW (verdict chain, phantom excluded) ===')
print(f'chain closes: {len(raw_chain)}')
halt_idx = sum(1 for c in raw_chain if c['ts'] <= RAW_HALT)
print(f'closes at halt time (Oct 29 08:24 WAT): {halt_idx}')
stats(raw_chain, 'all-time chain')
stats(raw_chain[max(0, halt_idx-100):halt_idx], 'halt window (100 closes to halt)')
stats(raw_chain[max(0, halt_idx-200):halt_idx-100], 'prior 100 before halt window')
stats(raw_chain[halt_idx:], 'drain closes after halt')
stats(raw_chain[-100:], 'last 100 chain closes (incl drain)')
reason_mix(raw_chain[max(0, halt_idx-100):halt_idx], 'halt window')
reason_mix(raw_chain[halt_idx:], 'drain')
# checkpoint: 42 closes +$1.53 (wins unknown) -> all-time incl checkpoint
print(f'  all-time incl 42-close checkpoint: {len(raw_chain)+42} closes, net ${sum(c["pnl"] for c in raw_chain)+1.53:+.2f}')

# ---------------- killswitch ----------------
try:
    ks = json.load(open(FA + 'killswitch/state.json'))
    print('\n=== killswitch ===')
    print(json.dumps(ks, indent=1)[:1200])
except Exception as ex:
    print('killswitch read failed:', ex)
