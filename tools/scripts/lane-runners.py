"""Which card lane produces the most runners, per day.
Lane attribution via milestone.source_event (authoritative).
Hit rate = unique 2x+ runners / unique coins carded that day."""
import sqlite3, json
from collections import defaultdict

con = sqlite3.connect('/home/hatch/workspace/lc.db')

# 1. runners per day per source lane (unique mints, best multiple)
runners = defaultdict(dict)  # day -> source -> {mint: (mult, symbol)}
for pay, ts in con.execute(
        "select payload, ts from event_journal where event='launch.milestone'"):
    day = (ts or '')[:10]
    if not day:
        continue
    try:
        p = json.loads(pay)
    except Exception:
        continue
    m = p.get('multiple')
    mint = p.get('mint')
    if not m or not mint:
        continue
    m = float(m)
    src = p.get('source_event', '?')
    d = runners[day].setdefault(src, {})
    if mint not in d or m > d[mint][0]:
        d[mint] = (m, str(p.get('symbol', '?')))

# 2. coins carded per day per lane
carded = defaultdict(lambda: defaultdict(set))  # day -> lane -> {mints}
digest_coins = defaultdict(int)  # day -> total coins collapsed into digests
for ev, pay, ts in con.execute(
        "select event, payload, ts from event_journal where event in "
        "('launch.early_digest','momentum.alert','launch.detected','launch.early')"):
    day = (ts or '')[:10]
    if not day:
        continue
    try:
        p = json.loads(pay)
    except Exception:
        continue
    if ev == 'launch.early_digest':
        digest_coins[day] += int(p.get('count', 0) or 0)
        for it in p.get('items', []):
            if isinstance(it, dict) and it.get('mint'):
                carded[day]['digest_items'].add(it['mint'])
    else:
        lane = {'momentum.alert': 'momentum',
                'launch.detected': 'confirmed',
                'launch.early': 'early_instant'}[ev]
        if p.get('mint'):
            carded[day][lane].add(p['mint'])

LANES = [('digest', 'launch.early_digest'),
         ('momentum', 'momentum.alert'),
         ('confirmed', 'launch.detected'),
         ('early_instant', 'launch.early')]

print('%-10s | %-34s | %-34s | %-34s | %-34s' % (
    'day', 'DIGEST cov/run/hit/best', 'MOMENTUM card/run/hit/best',
    'CONFIRMED card/run/hit/best', 'EARLY card/run/hit/best'))
tot = defaultdict(lambda: [0, 0])  # lane -> [carded, runners]
for day in sorted(set(list(runners) + list(carded))):
    cells = []
    for lane, src in LANES:
        cov = digest_coins[day] if lane == 'digest' else len(carded[day].get(lane, ()))
        rd = runners[day].get(src, {})
        n = len(rd)
        hit = (100.0 * n / cov) if cov else 0.0
        best = max(rd.values(), default=(0, '?'))
        cells.append('%6d %4d %5.1f%% %7.1fx' % (cov, n, hit, best[0]))
        tot[lane][0] += cov
        tot[lane][1] += n
    print('%-10s | %s | %s | %s | %s' % (day, *cells))

print()
print('TOTALS: lane -> coins carded / runners 2x+ / hit rate')
for lane, (c, r) in tot.items():
    print('  %-12s %7d carded  %4d runners  %5.2f%% hit' % (
        lane, c, r, 100.0 * r / c if c else 0))
