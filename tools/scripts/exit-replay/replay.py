#!/usr/bin/env python3
"""Exit-ladder replay over fetched 1m OHLCV paths.

Entry: first candle close at/after record t0; exits evaluated from the NEXT
candle. Within a candle, order is conservative: hard stop (vs low) first,
then TP fills (vs high), then peak update, then trailing-stop check (vs low).
Gap-through: if a candle opens below a stop/trail level, fill at the open.
Time caps / end of window: unsold remainder liquidated at that candle close.
A flat 4% round-trip cost multiplies the final realized multiple.
"""
import json, os, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
COST = 0.96  # 4% round-trip

def load_path(mint):
    p = os.path.join(HERE, 'cache2', mint + '.json')
    if not os.path.exists(p):
        return None
    d = json.load(open(p))
    return d.get('candles') or None

def entry_index(candles, t0):
    for i, c in enumerate(candles):
        if c[0] + 60 > t0:  # candle covers t0 or starts after it
            if c[0] >= t0 or True:
                # first candle whose close time is at/after t0
                if c[0] + 60 >= t0:
                    return i
    return None

def generic_ladder(candles, ei, tranches, stop_mult, cap_h):
    """tranches: [{frac, tp|None, trail|None, arm|None}] ; returns gross realized mult."""
    entry_ts, entry_px = candles[ei][0], candles[ei][4]
    if entry_px <= 0:
        return None
    cap_ts = entry_ts + cap_h * 3600
    stop_level = entry_px * stop_mult
    peak = entry_px
    sold = []  # (frac, mult)
    live = [dict(t, done=False) for t in tranches]
    last_close = entry_px
    for c in candles[ei + 1:]:
        ts, o, h, l, cl = c
        if ts > cap_ts:
            break
        last_close = cl
        # 1) hard stop on everything unsold
        if l <= stop_level:
            px = o if o < stop_level else stop_level
            for t in live:
                if not t['done']:
                    sold.append((t['frac'], px / entry_px))
                    t['done'] = True
            break
        # 2) TP fills
        for t in live:
            if not t['done'] and t.get('tp') is not None and h >= entry_px * t['tp']:
                sold.append((t['frac'], t['tp']))
                t['done'] = True
        # 3) peak update
        if h > peak:
            peak = h
        # 4) trailing exits
        for t in live:
            if not t['done'] and t.get('trail') is not None and peak >= entry_px * t['arm']:
                lvl = peak * (1 - t['trail'])
                if l <= lvl:
                    px = o if o < lvl else lvl
                    sold.append((t['frac'], px / entry_px))
                    t['done'] = True
        if all(t['done'] for t in live):
            break
    rem = sum(t['frac'] for t in live if not t['done'])
    if rem > 0:
        sold.append((rem, last_close / entry_px))
    return sum(f * m for f, m in sold)

def v0(candles, ei):
    return generic_ladder(candles, ei,
        [{'frac': 0.50, 'tp': 2.0},
         {'frac': 0.25, 'tp': 3.0},
         {'frac': 0.25, 'trail': 0.30, 'arm': 3.0}],
        stop_mult=0.5, cap_h=8)  # 24h rule truncated by 8h data window

def v1(candles, ei):
    return generic_ladder(candles, ei,
        [{'frac': 0.50, 'tp': 1.5},
         {'frac': 0.25, 'tp': 2.0},
         {'frac': 0.25, 'trail': 0.20, 'arm': 1.5}],
        stop_mult=0.6, cap_h=4)

def v2(candles, ei):
    return generic_ladder(candles, ei,
        [{'frac': 0.50, 'tp': 1.5},
         {'frac': 0.25, 'tp': 2.5},
         {'frac': 0.25, 'trail': 0.15, 'arm': 1.5}],
        stop_mult=0.6, cap_h=4)

def v4(candles, ei):
    return generic_ladder(candles, ei,
        [{'frac': 0.50, 'tp': 1.5},
         {'frac': 0.50, 'trail': 0.30, 'arm': 1.0}],
        stop_mult=0.5, cap_h=6)

def v3(candles, ei):
    entry_ts, entry_px = candles[ei][0], candles[ei][4]
    if entry_px <= 0:
        return None
    def leg(cap_h, tp, lock_arm, lock_drop, trail, stop_mult=0.7):
        stop_level = entry_px * stop_mult
        peak = entry_px
        last_close = entry_px
        cap_ts = entry_ts + cap_h * 3600
        for c in candles[ei + 1:]:
            ts, o, h, l, cl = c
            if ts > cap_ts:
                break
            last_close = cl
            if l <= stop_level:
                return (o if o < stop_level else stop_level) / entry_px
            if tp is not None and h >= entry_px * tp:
                return tp
            if h > peak:
                peak = h
            # profit-lock / trail exit
            if lock_arm is not None and peak >= entry_px * lock_arm:
                lvl = peak * (1 - lock_drop)
                if l <= lvl:
                    return (o if o < lvl else lvl) / entry_px
            elif trail is not None:
                lvl = peak * (1 - trail)
                if l <= lvl:
                    return (o if o < lvl else lvl) / entry_px
        return last_close / entry_px
    a = leg(4, tp=3.0, lock_arm=1.2, lock_drop=0.15, trail=None)
    b = leg(6, tp=None, lock_arm=None, lock_drop=None, trail=0.30)
    return 0.5 * a + 0.5 * b

VARIANTS = {'V0_user_ladder': v0, 'V1_fast_harvest': v1, 'V2_fast_tight_trail': v2,
            'V3_two_tier_current': v3, 'V4_bank_half_free': v4}

def main():
    subset = json.load(open(os.path.join(HERE, 'subset.json')))
    per_record = []   # one row per dataset record (legs kept separate)
    ttp = []  # time-to-peak minutes
    missing = []
    seen_entries = set()
    for r in subset:
        mint = r['mint']
        candles = load_path(mint)
        if not candles:
            missing.append(mint)
            continue
        ei = entry_index(candles, r['t0'])
        if ei is None or ei + 1 >= len(candles):
            missing.append(mint)
            continue
        entry_px = candles[ei][4]
        if entry_px <= 0:
            missing.append(mint)
            continue
        seg = candles[ei:]
        pk = max(seg, key=lambda c: c[2])
        ttp.append((pk[0] - candles[ei][0]) / 60.0)
        seen_entries.add((mint, int(r['t0'])))
        row = {'mint': mint, 'symbol': r.get('symbol'), 't0': r['t0'],
               'dataset_peak_mult': r['peak_mult'], 'dataset_realized': r['realized_mult'],
               'round_tripper': bool(r['round_tripper']), 'variants': {}}
        for name, fn in VARIANTS.items():
            g = fn(candles, ei)
            row['variants'][name] = round(g * COST, 4) if g is not None else None
        per_record.append(row)
    agg = {}
    rt_rows = [v for v in per_record if v['round_tripper']]
    for name in VARIANTS:
        vals = [v['variants'][name] for v in per_record if v['variants'][name] is not None]
        rt_vals = [v['variants'][name] for v in rt_rows if v['variants'][name] is not None]
        agg[name] = {
            'n': len(vals),
            'mean_realized': round(statistics.mean(vals), 4),
            'median_realized': round(statistics.median(vals), 4),
            'total_realized': round(sum(vals), 3),
            'win_rate_ge1x': round(sum(1 for v in vals if v >= 1.0) / len(vals), 4),
            'worst_case': round(min(vals), 4),
            'round_trippers_n': len(rt_vals),
            'round_trippers_total': round(sum(rt_vals), 3),
            'round_trippers_mean': round(statistics.mean(rt_vals), 4) if rt_vals else None,
        }
    # dataset's own realized outcomes over the same replayed records, for scale
    act = [v['dataset_realized'] for v in per_record]
    act_rt = [v['dataset_realized'] for v in rt_rows]
    agg['DATASET_ACTUAL'] = {
        'n': len(act), 'mean_realized': round(statistics.mean(act), 4),
        'total_realized': round(sum(act), 3),
        'win_rate_ge1x': round(sum(1 for v in act if v >= 1.0) / len(act), 4),
        'worst_case': round(min(act), 4),
        'round_trippers_n': len(act_rt), 'round_trippers_total': round(sum(act_rt), 3),
        'round_trippers_mean': round(statistics.mean(act_rt), 4) if act_rt else None,
    }
    out = {
        'coverage': {'records_in_subset': len(subset), 'records_replayed': len(per_record),
                     'unique_entry_events_replayed': len(seen_entries),
                     'missing_records': len(subset) - len(per_record),
                     'unique_mints_missing': sorted(set(missing))},
        'median_time_to_peak_min': round(statistics.median(ttp), 1) if ttp else None,
        'mean_time_to_peak_min': round(statistics.mean(ttp), 1) if ttp else None,
        'aggregates': agg,
        'per_record': per_record,
    }
    json.dump(out, open(os.path.join(HERE, 'results.json'), 'w'), indent=1)
    print(json.dumps({'coverage': out['coverage'], 'median_ttp_min': out['median_time_to_peak_min'],
                      'mean_ttp_min': out['mean_time_to_peak_min']}, indent=1))
    for name, a in sorted(agg.items(), key=lambda kv: -kv[1]['mean_realized']):
        print(name, a)

if __name__ == '__main__':
    main()
