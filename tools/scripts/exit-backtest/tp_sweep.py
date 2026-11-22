"""Leg A take-profit sweep: which TP level (+50/+75/+100/+150/+200%)
maximizes expectancy on real paper-trade price paths?

Half A variants (tick order mirrors the live engine: stop -> TP ->
max_hold -> trail, exits at market price on the trigger tick):
  classic: stop 30%, TP at X, 4h hold, single trail (arm +50%, 30% back)
  v2:      same + profit lock (arm +20%, 15% back) — what actually runs now
Half B fixed at the proven best: no fixed TP, 30% trail, 24h hold.
two_tier = 0.5*A + 0.5*B. Trustworthy subset via exit-reason agreement,
same as simulate.py validation.
"""
import json, os, sys, glob, time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.expanduser('~/workspace/exit-backtest'))
from simulate import load_entries, merged_series, simulate_M

CACHE = os.path.expanduser('~/workspace/exit-backtest/cache')
DATA = '/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl'


def median_sol(eps, cache):
    """Implied SOL/USD per day from candle closes vs engine entry prices."""
    day_imp = defaultdict(list)
    for f in glob.glob(os.path.join(cache, '*.json')):
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


def simulate_A(series, t_entry, tp_mult, stop_pct=0.30, max_hold_h=4.0,
               profit_lock=True, t1=0.20, d1=0.15, t2=0.50, d2=0.30):
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
            return ('stop_loss', price / entry, ts)
        if price >= entry * tp_mult:
            return ('take_profit', price / entry, ts)
        if age_h >= max_hold_h:
            return ('max_hold', price / entry, ts)
        gain = peak / entry - 1.0
        if profit_lock:
            if gain >= t2 and price <= peak * (1 - d2):
                return ('trail_stop', price / entry, ts)
            if gain >= t1 and price <= peak * (1 - d1):
                return ('profit_lock', price / entry, ts)
        else:
            if gain >= t2 and price <= peak * (1 - d2):
                return ('trail_stop', price / entry, ts)
    return ('open', series[-1][1] / entry, series[-1][0])


def main():
    eps = load_entries()
    sol = median_sol(eps, CACHE)
    print('SOL anchor: $%.0f' % sol, flush=True)
    paths = []
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
        cur = simulate_M(M1, t_entry, use_tp=True, trail_dd=0.30,
                         max_hold_h=4.0)
        paths.append({'mint': d['mint'], 'symbol': d.get('symbol'),
                      'M': M, 't_entry': t_entry,
                      'rec_mult': r['realized_mult'],
                      'rec_exit': r.get('exit_reason'),
                      'agree': cur[0] == r.get('exit_reason')})
    agree = [p for p in paths if p['agree']]
    print('paths=%d trustworthy=%d (exit-reason agreement)' % (
        len(paths), len(agree)), flush=True)

    tps = [1.5, 1.75, 2.0, 2.5, 3.0]
    out = {'n_paths': len(paths), 'n_trustworthy': len(agree),
           'sol_anchor': sol, 'levels': []}
    base_rec = sum(p['rec_mult'] for p in agree) / len(agree)
    print('recorded baseline (v1 exits): %.3fx  (exp %+.3f)' % (
        base_rec, base_rec - 1), flush=True)
    for tp in tps:
        for variant, pl in (('classic', False), ('v2', True)):
            mults, reasons = [], Counter()
            for p in agree:
                a = simulate_A(p['M'], p['t_entry'], tp, profit_lock=pl)
                b = simulate_M(p['M'], p['t_entry'], use_tp=False,
                               trail_dd=0.30, max_hold_h=24.0)
                two = 0.5 * a[1] + 0.5 * b[1]
                mults.append(two)
                reasons[a[0]] += 1
            n = len(mults)
            avg = sum(mults) / n
            wins = [m for m in mults if m > 1.0]
            losses = [m for m in mults if m <= 1.0]
            row = {'tp': tp, 'variant': variant, 'n': n,
                   'avg_mult': round(avg, 4),
                   'expectancy': round(avg - 1, 4),
                   'win_rate': round(len(wins) / n, 3),
                   'avg_win': round(sum(wins) / len(wins), 3) if wins else None,
                   'avg_loss': round(sum(losses) / len(losses), 3) if losses else None,
                   'a_reasons': dict(reasons)}
            out['levels'].append(row)
            print('TP %+.0f%% %-7s avg %.3fx  exp %+.3fx  win %5.1f%%  '
                  'A-exits %s' % (
                      (tp - 1) * 100, variant, avg, avg - 1,
                      100 * len(wins) / n,
                      dict(reasons)), flush=True)
    json.dump(out, open(os.path.expanduser(
        '~/workspace/exit-backtest/tp_sweep_results.json'), 'w'), indent=1)
    print('wrote tp_sweep_results.json', flush=True)


if __name__ == '__main__':
    main()
