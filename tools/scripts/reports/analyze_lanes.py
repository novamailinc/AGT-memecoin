#!/usr/bin/env python3
"""Lane-damage research: read-only analysis of paper memecoin bot lanes.
Outputs structured findings to stdout. Run from anywhere; paths absolute.
"""
import json, sqlite3, collections, statistics
from datetime import datetime, timezone

H = '/home/hatch/.forex-agent'
LANES = ['grad_select', 'tranche', 'tranche_protect', 'grad_raw', 'momentum']
DAYS = {  # WAT day-start epochs
    'Nov 5': 1793833200, 'Nov 6': 1793919600, 'Nov 7': 1794006000,
    'Nov 8': 1794092400, 'Nov 9': 1794178800, 'Nov 10': 1794265200,
    'Nov 11': 1794351600,
}
W0, W1 = DAYS['Nov 5'], DAYS['Nov 11']  # Nov 5-10 window (close_ts)

def daylabel(ts):
    for d in ['Nov 10', 'Nov 9', 'Nov 8', 'Nov 7', 'Nov 6', 'Nov 5']:
        if ts >= DAYS[d]:
            return d
    return 'before Nov 5'

def pnl(c):
    return float(c.get('pnl_usd') if c.get('pnl_usd') is not None else c.get('pnl') or 0)

def load_lane(lane):
    with open(f'{H}/{lane}_ledger.json') as f:
        return json.load(f)

# ---------- verdict chain ----------
chain = collections.defaultdict(list)  # lane -> [(ts, pnl, mult, reason)]
for line in open(f'{H}/verdict_ledger/events.jsonl'):
    e = json.loads(line)
    if e.get('kind') != 'close':
        continue
    p = e.get('payload') or {}
    chain[e.get('lane')].append((float(e.get('ts') or 0), float(p.get('pnl_usd') or 0),
                                 float(p.get('realized_mult') or 0), p.get('closed_reason')))

print('=' * 100)
print('A. VERDICT CHAIN — authoritative close counts (chain genesis Oct 23)')
print('=' * 100)
for lane in LANES:
    evs = sorted(chain.get(lane, []))
    if not evs:
        print(f'{lane}: NO chain events (ledger-only lane)')
        continue
    allp = [x[1] for x in evs]
    win = [x for x in evs if x[1] > 0]
    w = [x for x in evs if W0 <= x[0] < W1]
    wp = [x[1] for x in w]
    wwin = [x for x in w if x[1] > 0]
    print(f'{lane}: ALL-TIME(chain) n={len(evs)} wins={len(win)} ({100*len(win)/len(evs):.1f}%) '
          f'net=${sum(allp):+.2f} exp=${sum(allp)/len(allp):+.2f}/close | '
          f'Nov5-10 n={len(w)} wins={len(wwin)} ({100*len(wwin)/max(len(w),1):.1f}%) net=${sum(wp):+.2f}')
    per = collections.defaultdict(lambda: [0, 0, 0.0])
    for ts, p, m, r in evs:
        if W0 <= ts < W1:
            d = daylabel(ts)
            per[d][0] += 1
            per[d][1] += 1 if p > 0 else 0
            per[d][2] += p
    for d in ['Nov 5', 'Nov 6', 'Nov 7', 'Nov 8', 'Nov 9', 'Nov 10']:
        n, wn, s = per[d]
        print(f'    {d}: {n} closes, {wn} wins, net ${s:+.2f}')

# ---------- ledger detail ----------
print()
print('=' * 100)
print('B. LEDGER DETAIL — most recent 500 closes per lane (date range stated)')
print('=' * 100)
ledgers = {}
for lane in LANES:
    l = load_lane(lane)
    ledgers[lane] = l
    cs = sorted(l.get('closes') or [], key=lambda c: c.get('close_ts') or 0)
    if not cs:
        print(f'{lane}: no closes in ledger'); continue
    ps = [pnl(c) for c in cs]
    wins = [p for p in ps if p > 0]
    losses = [p for p in ps if p <= 0]
    rng = (datetime.fromtimestamp(cs[0]['close_ts'], tz=timezone.utc).strftime('%m-%d'),
           datetime.fromtimestamp(cs[-1]['close_ts'], tz=timezone.utc).strftime('%m-%d %H:%M'))
    print(f'\n--- {lane}: n={len(cs)} closes {rng[0]}..{rng[1]} UTC, bankroll_start={l.get("bankroll_start")} bankroll_now={round(l.get("bankroll") or 0, 2)}')
    print(f'    win rate {100*len(wins)/len(ps):.1f}% ({len(wins)}W/{len(losses)}L) net ${sum(ps):+.2f} '
          f'exp ${sum(ps)/len(ps):+.2f}/close | avg win ${statistics.mean(wins):+.2f} | avg loss ${statistics.mean(losses):+.2f} '
          f'| median pnl ${statistics.median(ps):+.2f}')
    # full-stake losses: realized ~0 (pnl <= -24.5 on $25 lanes; momentum $1 lanes: pnl <= -0.95)
    stake = 25.0 if lane != 'momentum' else 1.0
    full = [c for c in cs if pnl(c) <= -(stake - 0.5)]
    z = [c for c in cs if (c.get('realized_mult') or 0) == 0]
    print(f'    FULL-STAKE losses (pnl <= ${-(stake-0.5):.2f}): {len(full)} ({100*len(full)/len(cs):.1f}% of closes) = ${sum(pnl(c) for c in full):+.2f}')
    print(f'    realized_mult == 0.0x closes: {len(z)} = ${sum(pnl(c) for c in z):+.2f}')
    # tail
    top = sorted(cs, key=lambda c: pnl(c), reverse=True)
    net = sum(ps)
    t1 = pnl(top[0]); t5 = sum(pnl(c) for c in top[:5])
    print(f'    TAIL: top1 ${t1:+.2f} = {100*t1/net if net else 0:.1f}% of net | top5 ${t5:+.2f} = {100*t5/net if net else 0:.1f}% of net')
    print(f'    ex-top1 net ${net-t1:+.2f} exp ${(net-t1)/len(ps):+.2f} | ex-top5 net ${net-t5:+.2f} exp ${(net-t5)/len(ps):+.2f}')
    print(f'    biggest win: {top[0].get("mint","")[:10]} {top[0].get("realized_mult")}x ${t1:+.2f} | biggest loss: {top[-1].get("mint","")[:10]} ${pnl(top[-1]):+.2f} ({top[-1].get("closed_reason")})')
    # exit reasons (close-level)
    byr = collections.defaultdict(lambda: [0, 0.0])
    for c in cs:
        byr[c.get('closed_reason') or 'unknown'][0] += 1
        byr[c.get('closed_reason') or 'unknown'][1] += pnl(c)
    print('    EXIT REASONS (close-level):')
    for r, (n, s) in sorted(byr.items(), key=lambda kv: -kv[1][1]):
        print(f'      {r}: {n} closes ({100*n/len(cs):.1f}%), net ${s:+.2f}')
    # leg-level reasons
    legr = collections.defaultdict(lambda: [0, 0.0])
    for c in cs:
        for leg in (c.get('legs') or {}).values():
            if leg.get('done'):
                legr[leg.get('exit_reason') or 'unknown'][0] += 1
                legr[leg.get('exit_reason') or 'unknown'][1] += float(leg.get('realized_usd') or 0)
    print('    LEG-LEVEL exit reasons (realized $ per leg):')
    for r, (n, s) in sorted(legr.items(), key=lambda kv: -kv[1][0]):
        print(f'      {r}: {n} legs, realized ${s:+.2f}')
    # hold times
    holds = [(c.get('close_ts') or 0) - (c.get('entry_ts') or 0) for c in cs if c.get('close_ts') and c.get('entry_ts')]
    over24 = [h for h in holds if h > 86400]
    print(f'    HOLD: median {statistics.median(holds)/3600:.1f}h mean {statistics.mean(holds)/3600:.1f}h | closes held >24h: {len(over24)} ({100*len(over24)/len(holds):.1f}%)')
    pos = l.get('positions') or {}
    now = max(c.get('close_ts') or 0 for c in cs)
    old_open = [m for m, p in pos.items() if p and (now - (p.get('entry_ts') or 0)) > 86400]
    print(f'    OPEN now: {len(pos)} positions, {len(old_open)} open >24h')
    # entry_dq split
    dq = [c for c in cs if (c.get('entry_features') or {}).get('entry_dq')]
    ndq = [c for c in cs if not (c.get('entry_features') or {}).get('entry_dq')]
    def _s(lst):
        if not lst: return 'n=0'
        pp = [pnl(c) for c in lst]
        return f'n={len(lst)} wins={sum(1 for p in pp if p>0)} ({100*sum(1 for p in pp if p>0)/len(pp):.1f}%) net=${sum(pp):+.2f} exp=${sum(pp)/len(pp):+.2f}'
    print(f'    ENTRY_DQ (entry price failed live verification): dq {_s(dq)} | clean {_s(ndq)}')
    # fees: modeled ~$0.63 round-trip per $25 close, ~$0.004 per $1 close; already deducted from pnl
    fee_each = 0.63 if lane != 'momentum' else 0.004
    fees = fee_each * len(cs)
    print(f'    FEES (modeled, already deducted from pnl): ~${fee_each:.2f} x {len(cs)} closes = ~${fees:.2f} | gross-of-fees net would be ${net+fees:+.2f}')

# ---------- entries per day ----------
print()
print('=' * 100)
print('C. ENTRIES PER DAY Nov 5-10 (from closes entry_ts + open positions; ledger cap caveat: 500 most recent closes)')
print('=' * 100)
for lane in LANES:
    l = ledgers[lane]
    ent = collections.Counter()
    for c in (l.get('closes') or []):
        if c.get('entry_ts'):
            ent[daylabel(c['entry_ts'])] += 1
    for m, p in (l.get('positions') or {}).items():
        if p and p.get('entry_ts'):
            ent[daylabel(p['entry_ts'])] += 1
    row = ' | '.join(f'{d}: {ent.get(d,0)}' for d in ['Nov 5','Nov 6','Nov 7','Nov 8','Nov 9','Nov 10'])
    print(f'{lane}: {row}')
    print(f'    entries_day field (today): {l.get("entries_day")}')

# ---------- halt window reconstruction ----------
print()
print('=' * 100)
print('D. HALT WINDOW RECONSTRUCTION (100 closes before each halt/reenable, from verdict chain)')
print('=' * 100)
s = json.load(open(f'{H}/killswitch/state.json'))
halts = [(e['ts'], e['lane'], e['action'], e['reason']) for e in s.get('events', [])]
for ts, lane, action, reason in halts:
    if lane not in LANES:
        continue
    evs = sorted(chain.get(lane, []))
    prior = [x for x in evs if x[0] < ts][-100:]
    if not prior:
        print(f'{datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")} UTC {lane} {action}: no prior chain closes'); continue
    pp = [x[1] for x in prior]
    wn = sum(1 for p in pp if p > 0)
    print(f'{datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M")} UTC {lane} {action}: '
          f'prior-100 mean ${sum(pp)/len(pp):+.2f} wins {wn}/100 ({wn}%) | reason: {reason[:100]}')

# ---------- Nov 10 corruption block ----------
print()
print('=' * 100)
print('E. NOV 10 STATE-CORRUPTION BLOCK (11:26-11:35 WAT = 10:26-10:35 UTC)')
print('=' * 100)
blk0, blk1 = 1794306360, 1794306900
for lane in LANES:
    l = ledgers[lane]
    inblk = [c for c in (l.get('closes') or []) if c.get('entry_ts') and blk0 <= c['entry_ts'] < blk1]
    inblk_pos = [m for m, p in (l.get('positions') or {}).items() if p and p.get('entry_ts') and blk0 <= p['entry_ts'] < blk1]
    # entries in the 2h before the block, for rate
    before = [c for c in (l.get('closes') or []) if c.get('entry_ts') and blk0-7200 <= c['entry_ts'] < blk0]
    before += [m for m, p in (l.get('positions') or {}).items() if p and p.get('entry_ts') and blk0-7200 <= p['entry_ts'] < blk0]
    print(f'{lane}: entries during 9-min block: {len(inblk)+len(inblk_pos)} | entries in prior 2h: {len(before)} '
          f'(~{len(before)/2:.1f}/h -> expected ~{len(before)/2*0.15:.1f} in 9 min)')

# ---------- Live10 ----------
print()
print('=' * 100)
print('F. LIVE10 (funded demo account)')
print('=' * 100)
l10 = json.load(open(f'{H}/live10_demo_ledger.json'))
print('lanes dict (all-time funded):')
for lane, v in l10.get('lanes', {}).items():
    print(f'  {lane}: balance ${v.get("balance"):.2f} funded_closes={v.get("funded_closes")} wins={v.get("wins")} '
          f'({100*v.get("wins",0)/max(v.get("funded_closes",1),1):.1f}%) net ${v.get("net"):+.2f}')
print('combined:', l10.get('combined'))
mir = l10.get('mirrored') or []
print(f'mirrored entries held: {len(mir)} (capped 500)')
for lane in ['grad_select', 'tranche', 'tranche_protect', 'grad_raw']:
    w = [m for m in mir if m.get('lane') == lane and W0 <= (m.get('close_ts') or 0) < W1]
    fund = [m for m in w if m.get('funded')]
    unf = [m for m in w if not m.get('funded')]
    print(f'  {lane} Nov5-10 mirrored: {len(w)} total, {len(fund)} funded '
          f'({sum(1 for m in fund if (m.get("scaled_pnl") or 0) > 0)} wins, net ${sum(m.get("scaled_pnl") or 0 for m in fund):+.2f}) | '
          f'{len(unf)} UNFUNDED (paper net ${sum(m.get("paper_pnl") or 0 for m in unf):+.2f} -> at 0.4 scale ${sum(m.get("paper_pnl") or 0 for m in unf)*0.4:+.2f} missed)')
# Nov 9 incident verification: lane closes in backup->restore window vs mirrored
print()
print('Nov 9 Live10 incident check (backup 20:29 WAT=1794252540 -> restore 21:41 WAT=1794256860):')
for lane in ['grad_select', 'tranche']:
    l = ledgers[lane]
    window_closes = [c for c in (l.get('closes') or []) if 1794252540 <= (c.get('close_ts') or 0) < 1794256860]
    mints = {(m.get('mint'), m.get('lane')) for m in mir}
    missing = [c for c in window_closes if (c.get('mint'), lane) not in mints]
    print(f'  {lane}: lane closes in window: {len(window_closes)}, missing from Live10 mirrored: {len(missing)}')
    for c in missing:
        print(f'    MISSING: {c.get("mint","")[:12]} close_ts={c.get("close_ts")} pnl=${pnl(c):+.2f}')
    # also closes in the truncation->restore gap after backup (mirror was down 21:21-21:41)
    gap = [c for c in (l.get('closes') or []) if 1794255660 <= (c.get('close_ts') or 0) < 1794256860]
    print(f'    closes during mirror-down gap 21:21-21:41 WAT: {len(gap)}, all present in mirrored: {all((c.get("mint"), lane) in mints for c in gap)}')

# ---------- momentum day counts ----------
print()
print('=' * 100)
print('G. MOMENTUM day counts (ledger-only lane, 81 closes total)')
print('=' * 100)
l = ledgers['momentum']
cs = sorted(l.get('closes') or [], key=lambda c: c.get('close_ts') or 0)
per = collections.Counter(daylabel(c['close_ts']) for c in cs if c.get('close_ts'))
print('closes by WAT day:', dict(sorted(per.items())))
print('total closes in ledger:', len(cs), '| bankroll:', l.get('bankroll'), '| start:', l.get('bankroll_start'))
print('corrections entries:', len(l.get('corrections') or []), json.dumps(l.get('corrections'))[:400])
