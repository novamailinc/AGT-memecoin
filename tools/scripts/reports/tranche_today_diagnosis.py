"""Why is tranche losing today (Oct 31)? Fresh read: today-only closes per
lane, exit mix, leg rates, peak leakage, open positions, plus the two
test lanes and grad_select on the same entries for comparison."""
import json, collections, time

FA = '/home/hatch/.forex-agent/'
OCT31 = 1793401200  # Oct 31 00:00 WAT
AFTERNOON = 1793456400  # Oct 31 ~15:00 WAT (test lanes started 15:30)

def lane_today(path, label):
    try:
        d = json.load(open(FA + path))
    except Exception as e:
        print(f'{label}: ledger unreadable: {e}'); return []
    closes = [c for c in (d.get('closes') or []) if c.get('close_ts', 0) >= OCT31]
    closes.sort(key=lambda c: c['close_ts'])
    n = len(closes)
    if not n:
        print(f'{label}: 0 closes today'); return closes
    wins = sum(1 for c in closes if c['pnl_usd'] > 0)
    net = sum(c['pnl_usd'] for c in closes)
    print(f'{label}: {n} closes, {wins} wins ({100*wins/n:.1f}%), net ${net:+.2f}, exp ${net/n:+.2f}/close')
    mix = collections.Counter(c.get('closed_reason', '?') for c in closes)
    pnl = collections.defaultdict(float)
    for c in closes: pnl[c.get('closed_reason', '?')] += c['pnl_usd']
    print(f'  exit mix:', {k: f"{v}c/${pnl[k]:+.0f}" for k, v in mix.most_common()})
    top = sorted(closes, key=lambda c: c['pnl_usd'], reverse=True)[:3]
    bot = sorted(closes, key=lambda c: c['pnl_usd'])[:3]
    print('  top:', [(c['mint'][:10], round(c['pnl_usd'], 2)) for c in top])
    print('  bottom:', [(c['mint'][:10], round(c['pnl_usd'], 2)) for c in bot])
    # leg banking (tranche-family ledgers carry legs)
    for leg in ('a', 'b', 'c'):
        reasons = collections.Counter()
        for c in closes:
            lv = (c.get('legs') or {}).get(leg)
            if isinstance(lv, dict): reasons[lv.get('exit_reason', '?')] += 1
        if reasons: print(f'  leg {leg.upper()}:', dict(reasons))
    # peak leakage
    leaked = [c for c in closes if (c.get('peak_mult') or 0) >= 1.5 and c['pnl_usd'] <= 0]
    if leaked: print(f'  peaked >=1.5x but lost: {len(leaked)} (${sum(c["pnl_usd"] for c in leaked):+.2f})')
    pos = d.get('positions') or {}
    now = time.time()
    ages = sorted(((now - p.get('entry_ts', now)) / 3600 for p in pos.values()), reverse=True)
    print(f'  open now: {len(pos)}' + (f', oldest {ages[0]:.1f}h' if ages else ''))
    return closes

print('=== TODAY Oct 31 (WAT) ===')
tr = lane_today('tranche_ledger.json', 'TRANCHE live')
print()
lane_today('grad_select_ledger.json', 'GRAD_SELECT (same entries, select exits)')
print()
lane_today('tranche_protect_ledger.json', 'TRANCHE_PROTECT test (breakeven after A)')
print()
lane_today('raw_tight_ledger.json', 'RAW_TIGHT test (-25% stop, spray)')
print()
# tranche morning vs afternoon split
if tr:
    am = [c for c in tr if c['close_ts'] < AFTERNOON]
    pm = [c for c in tr if c['close_ts'] >= AFTERNOON]
    for label, cs in (('tranche morning (<15:00)', am), ('tranche afternoon (>=15:00)', pm)):
        n = len(cs)
        if n:
            w = sum(1 for c in cs if c['pnl_usd'] > 0); net = sum(c['pnl_usd'] for c in cs)
            print(f'{label}: {n}c {w}w net ${net:+.2f} exp ${net/n:+.2f}')
