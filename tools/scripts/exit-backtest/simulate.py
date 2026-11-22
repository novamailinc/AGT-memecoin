"""Two-tier exit backtest, anchored to the engine's own entry_price_sol."""
import json, os, glob, time
from collections import defaultdict

CACHE = '/tmp/bt_paths_1m'
DATA = '/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl'

def load_entries():
    eps = {}
    for line in open(DATA):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get('realized_mult') is not None and r.get('entry_price_sol'):
            eps[r['mint']] = r
    return eps

def median_sol(eps):
    day_imp = defaultdict(list)
    for f in glob.glob(os.path.join(CACHE, '*.json')):
        if os.path.basename(f).startswith('_'):
            continue
        d = json.load(open(f))
        r = eps.get(d['mint'])
        if not r:
            continue
        cl = sorted(d['candles'], key=lambda c: c[0])
        t_entry = r['t_entry']
        ec = next((c for c in reversed(cl) if c[0] <= t_entry), None)
        if not ec:
            continue
        day = time.strftime('%m-%d', time.gmtime(t_entry))
        day_imp[day].append(ec[4] / r['entry_price_sol'])
    meds = []
    for day in sorted(day_imp):
        v = sorted(day_imp[day])
        meds.append(v[len(v) // 2])
    meds.sort()
    return meds[len(meds) // 2]

def merged_series(d, entry_price_sol, sol):
    """Merge 1m candles with the 5m tail (prefer 1m where they overlap).
    Returns [(ts, M)] in true-multiple space."""
    E = entry_price_sol * sol
    cl1 = sorted(d.get('candles', []), key=lambda c: c[0])
    cl5 = sorted(d.get('candles_5m', []), key=lambda c: c[0])
    last1 = cl1[-1][0] if cl1 else 0
    pts = [(c[0], c[4] / E) for c in cl1]
    pts += [(c[0], c[4] / E) for c in cl5 if c[0] > last1]
    pts = sorted(pts, key=lambda x: x[0])
    # linearly interpolate to 30s steps so fast crashes pass through
    # intermediate levels (the engine polls every 30s)
    out = []
    for i in range(len(pts) - 1):
        t0, p0 = pts[i]
        t1, p1 = pts[i + 1]
        out.append((t0, p0))
        t = t0 + 30
        while t < t1:
            f = (t - t0) / (t1 - t0)
            out.append((t, p0 + f * (p1 - p0)))
            t += 30
    out.append(pts[-1])
    return out

def simulate_M(series, t_entry, use_tp=True, trail_dd=0.30, max_hold_h=4.0,
               trail_trigger=0.50, stop_pct=0.30):
    epts = [(ts, m) for ts, m in series if ts <= t_entry]
    if not epts:
        return ('no_entry', 0.0, 0)
    t0, entry = epts[-1]
    peak = entry
    for ts, price in series:
        if ts <= t0:
            continue
        age_h = (ts - t0) / 3600.0
        if price > peak:
            peak = price
        if price <= entry * (1 - stop_pct):
            return ('stop_loss', 1 - stop_pct, ts)
        if use_tp and price >= entry * 3.0:
            return ('take_profit', 3.0, ts)
        if age_h >= max_hold_h:
            return ('max_hold', price / entry, ts)
        if peak >= entry * (1 + trail_trigger) and price <= peak * (1 - trail_dd):
            return ('trail_stop', peak * (1 - trail_dd) / entry, ts)
    return ('open', series[-1][1] / entry, series[-1][0])

def main():
    eps = load_entries()
    sol = median_sol(eps)
    print('SOL anchor: $%.0f' % sol)
    rows = []
    n_open_b = 0
    for f in sorted(glob.glob(os.path.join(CACHE, '*.json'))):
        if os.path.basename(f).startswith('_'):
            continue
        d = json.load(open(f))
        r = eps.get(d['mint'])
        if not r:
            continue
        cl = sorted(d['candles'], key=lambda c: c[0])
        t_entry = r['t_entry']
        M = merged_series(d, r['entry_price_sol'], sol)
        M1 = [(c[0], c[4] / (r['entry_price_sol'] * sol)) for c in cl]
        rec_mult = r['realized_mult']
        cur = simulate_M(M1, t_entry, use_tp=True, trail_dd=0.30, max_hold_h=4.0)
        halfB = simulate_M(M, t_entry, use_tp=False, trail_dd=0.40, max_hold_h=24.0)
        b_truncated = 'candles_5m' not in d
        if halfB[0] == 'open':
            n_open_b += 1
        two = 0.5 * rec_mult + 0.5 * halfB[1]
        rows.append({'mint': d['mint'], 'symbol': d.get('symbol'),
                     'rec_mult': rec_mult, 'rec_exit': r.get('exit_reason'),
                     'sim_cur': cur[1], 'sim_cur_exit': cur[0],
                     'sim_B': halfB[1], 'sim_B_exit': halfB[0], 'two_tier': two,
                     'b_truncated': b_truncated,
                     'peak_mult': r.get('peak_mult')})
    json.dump(rows, open('/tmp/bt_paths/_sim.json', 'w'), indent=1)
    n = len(rows)
    errs = sorted(abs(x['sim_cur'] - x['rec_mult']) for x in rows)
    print('n=%d  VALIDATION sim-current vs recorded: |err| p50=%.3f p90=%.3f' % (n, errs[n//2], errs[int(n*0.9)]))
    agree = sum(1 for x in rows if x['sim_cur_exit'] == x['rec_exit'])
    print('exit-reason agreement: %d/%d (%.0f%%)' % (agree, n, 100*agree/n))
    def avg(key):
        vs = [x[key] for x in rows]
        return sum(vs)/len(vs)
    print('avg recorded (current exits): %.3fx' % avg('rec_mult'))
    print('avg two-tier:                 %.3fx' % avg('two_tier'))
    print('expectancy/trade: current %+.3fx  vs  two-tier %+.3fx' % (avg('rec_mult')-1, avg('two_tier')-1))
    print('half-B still open at window end: %d/%d' % (n_open_b, n))
    print('half-B on truncated window (no 5m tail): %d/%d' % (sum(1 for x in rows if x['b_truncated']), n))
    big = [x for x in rows if (x['peak_mult'] or 0) >= 3]
    print('trades with recorded peak>=3x: %d' % len(big))
    for x in sorted(big, key=lambda z: -(z['peak_mult'] or 0))[:10]:
        print('  %-10s peak %4.1f rec %5.2f (%s) two-tier %5.2f (B: %5.2f %s)' % (
            (x['symbol'] or '?')[:10], x['peak_mult'] or 0, x['rec_mult'], x['rec_exit'],
            x['two_tier'], x['sim_B'], x['sim_B_exit']))

if __name__ == '__main__':
    main()
