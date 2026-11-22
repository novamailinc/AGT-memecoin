"""Build the weekly paper P&L PDF for Oct 26-30, 2026. Paper USD lanes only.
Sources: verdict chain (grad_select, grad_raw) + lane ledgers. Phantoms excluded.
hl_mirror chain closes are counted ONLY when their mint is absent from the mirror
ledger (the chain hl closes otherwise duplicate the mirror ledger). Raw all-time
includes its 42 pre-chain checkpoint closes (net +$1.53, wins unknown).
Balances use realized closes only; open positions are listed, not valued.
"""
import json, time, datetime, os
from fpdf import FPDF

WEEK_START = 1792969200   # Mon Oct 26 00:00 WAT
TRAIL_START = 1792796400  # Fri Oct 24 00:00 WAT
NOW = time.time()
DAYB = [(1792969200, 'Mon Oct 26'), (1793055600, 'Tue Oct 27'),
        (1793142000, 'Wed Oct 28'), (1793228400, 'Thu Oct 29'),
        (1793314800, 'Fri Oct 30')]
PHANT = ('274rXqtAaGAp9P', 'BVY5qMsi9G5f')

def wat(ts):
    return datetime.datetime.fromtimestamp(ts + 3600, datetime.timezone.utc).strftime('%d %b %Y, %H:%M WAT')

lanes = {}
def add(lane, ts, pnl, mint='', mult=0.0):
    d = lanes.setdefault(lane, {'week_c': 0, 'week_w': 0, 'week_net': 0.0,
                                'trail_c': 0, 'trail_w': 0, 'trail_net': 0.0,
                                'all_c': 0, 'all_w': 0, 'all_net': 0.0,
                                'days': [[0, 0, 0.0] for _ in range(5)],
                                'week_wins': [], 'week_losses': []})
    d['all_c'] += 1; d['all_w'] += (1 if pnl > 0 else 0); d['all_net'] += pnl
    if ts >= TRAIL_START:
        d['trail_c'] += 1; d['trail_w'] += (1 if pnl > 0 else 0); d['trail_net'] += pnl
    if ts >= WEEK_START:
        d['week_c'] += 1; d['week_w'] += (1 if pnl > 0 else 0); d['week_net'] += pnl
        di = 4
        for i, (b, _) in enumerate(DAYB):
            if ts < b:
                di = i - 1; break
        if 0 <= di <= 4:
            r = d['days'][di]; r[0] += 1; r[1] += (1 if pnl > 0 else 0); r[2] += pnl
        if pnl > 0: d['week_wins'].append((pnl, mint, mult))
        elif pnl < 0: d['week_losses'].append((pnl, mint, mult))

def pnl_of(c): return float(c.get('pnl_usd', c.get('pnl', 0)) or 0)
def ts_of(c): return float(c.get('close_ts', c.get('exit_ts', c.get('ts', 0))) or 0)

LED = [('tranche_ledger.json', 'tranche'), ('momentum_ledger.json', 'momentum'),
       ('moon_ledger.json', 'moon'), ('degen_mirror_ledger.json', 'mirror'),
       ('steady_ledger.json', 'steady'), ('ceo_ledger.json', 'ceo'),
       ('watcher_ledger.json', 'watcher'), ('watcher_v2_ledger.json', 'watcher_v2'),
       ('grad_ledger.json', 'grad'), ('degen_v2_ledger.json', 'degen_v2'),
       ('smart_select_ledger.json', 'smart_select'), ('stacker_ledger.json', 'stacker')]
bank = {}
mirror_mints = set()
for fn, lane in LED:
    fp = '/home/hatch/.forex-agent/' + fn
    if not os.path.exists(fp):
        continue
    d = json.load(open(fp))
    for c in (d.get('closes') or d.get('closed') or []):
        if lane == 'mirror':
            mirror_mints.add(str(c.get('mint') or ''))
        add(lane, ts_of(c), pnl_of(c), str(c.get('mint') or '')[:12],
            float(c.get('realized_mult', c.get('mult', 0)) or 0))
    pos = d.get('positions') or {}
    bank[lane] = {'start': d.get('bankroll_start'), 'now': d.get('bankroll'),
                  'open': len(pos) if isinstance(pos, dict) else 0}
for fn, lane in [('grad_select_ledger.json', 'grad_select'), ('grad_raw_ledger.json', 'grad_raw')]:
    d = json.load(open('/home/hatch/.forex-agent/' + fn))
    pos = d.get('positions') or {}
    bank[lane] = {'start': d.get('bankroll_start'), 'now': d.get('bankroll'),
                  'open': len(pos) if isinstance(pos, dict) else 0}

hl_resid = 0
for line in open('/home/hatch/.forex-agent/verdict_ledger/events.jsonl'):
    try:
        e = json.loads(line)
    except Exception:
        continue
    if e.get('kind') != 'close':
        continue
    pay = e.get('payload') or {}
    if str(pay.get('mint') or '').startswith(PHANT):
        continue
    lane = e.get('lane')
    if lane in ('grad_select', 'grad_raw'):
        add(lane, float(e.get('ts') or 0), float(pay.get('pnl_usd') or 0),
            str(pay.get('mint') or '')[:12], float(pay.get('realized_mult') or 0))
    elif lane == 'hl_mirror' and str(pay.get('mint') or '') not in mirror_mints:
        hl_resid += 1
        add('hl_residual', float(e.get('ts') or 0), float(pay.get('pnl_usd') or 0),
            str(pay.get('mint') or '')[:12], float(pay.get('realized_mult') or 0))

# Raw pre-chain checkpoint: 42 closes, net +$1.53, wins unknown (additive, pre-week).
lanes['grad_raw']['all_c'] += 42
lanes['grad_raw']['all_net'] += 1.53

wk_c = sum(d['week_c'] for d in lanes.values())
wk_w = sum(d['week_w'] for d in lanes.values())
wk_n = sum(d['week_net'] for d in lanes.values())
tr_c = sum(d['trail_c'] for d in lanes.values())
tr_w = sum(d['trail_w'] for d in lanes.values())
tr_n = sum(d['trail_net'] for d in lanes.values())
all_n = sum(d['all_net'] for d in lanes.values())
bal_order = ['grad_select', 'grad_raw', 'tranche', 'moon', 'momentum', 'ceo', 'grad', 'watcher', 'watcher_v2']
bal_lanes = [l for l in bal_order if bank.get(l, {}).get('start') is not None]
bal_start = sum(bank[l]['start'] for l in bal_lanes)
bal_real = sum(lanes[l]['all_net'] for l in bal_lanes if l in lanes)
bal_now = bal_start + bal_real
fri = [sum(d['days'][4][0] for d in lanes.values()), sum(d['days'][4][1] for d in lanes.values()),
       sum(d['days'][4][2] for d in lanes.values())]
print(f"hl residual closes kept: {hl_resid}")
print(f"WEEK: {wk_c} closes {wk_w} wins ${wk_n:.2f}")
print(f"TRAIL7: {tr_c} closes {tr_w} wins ${tr_n:.2f}")
print(f"ALL realized: ${all_n:.2f}")
print(f"BALANCE: start ${bal_start:.2f} + realized ${bal_real:.2f} = ${bal_now:.2f}")
print(f"TODAY Fri: {fri[0]} closes {fri[1]} wins ${fri[2]:.2f}")
for l in bal_lanes:
    lb = bank[l]['now']; rb = bank[l]['start'] + lanes[l]['all_net']
    print(f"  {l}: ledger ${lb} vs realized ${rb:.2f} gap {round(lb - rb, 2) if lb is not None else None} open {bank[l]['open']}")

# ---------------- PDF ----------------
def money(x):
    return ('-$' if x < 0 else '$') + f"{abs(x):,.2f}"
def pct(w, c):
    return f"{(100.0 * w / c if c else 0):.1f}%"

pdf = FPDF()
pdf.set_margins(15, 15, 15)
pdf.set_auto_page_break(auto=True, margin=15)
pdf.add_page()
pdf.set_font('Helvetica', 'B', 18)
pdf.cell(0, 10, 'Paper Trading P&L - This Week', new_x='LMARGIN', new_y='NEXT')
pdf.set_font('Helvetica', '', 11)
pdf.cell(0, 6, 'Monday 26 October 2026 00:00 WAT to ' + wat(NOW), new_x='LMARGIN', new_y='NEXT')
pdf.set_font('Helvetica', 'B', 12)
pdf.set_text_color(180, 0, 0)
pdf.cell(0, 8, 'PAPER ONLY - real money earned: $0.00. Nothing here is real profit.', new_x='LMARGIN', new_y='NEXT')
pdf.set_text_color(0, 0, 0)
pdf.ln(2)

def section(t):
    pdf.set_font('Helvetica', 'B', 13)
    pdf.set_fill_color(30, 30, 30); pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 8, '  ' + t, new_x='LMARGIN', new_y='NEXT', fill=True)
    pdf.set_text_color(0, 0, 0); pdf.ln(2)

def header_row(cols):
    pdf.set_font('Helvetica', 'B', 10); pdf.set_fill_color(220, 220, 220)
    for text, w in cols:
        pdf.cell(w, 7, text, border=1, fill=True)
    pdf.ln()

def data_row(cells, bold=False):
    pdf.set_font('Helvetica', 'B' if bold else '', 10)
    for text, w, al in cells:
        pdf.cell(w, 6, text, border=1, align=al)
    pdf.ln()

section('Headline numbers')
pdf.set_font('Helvetica', '', 11)
for line in [
    f"Started with {money(bal_start)} across {len(bal_lanes)} lanes with a recorded starting bankroll.",
    f"This week profit: {money(wk_n)} on {wk_c} closes, {wk_w} wins ({pct(wk_w, wk_c)} win rate).",
    f"Today (Fri Oct 30) so far: {money(fri[2])} on {fri[0]} closes, {fri[1]} wins.",
    f"Trailing 7 days (Oct 24-30): {money(tr_n)} on {tr_c} closes, {tr_w} wins.",
    f"All-time realized profit (all paper USD lanes): {money(all_n)}.",
    f"Realized balance now (9 bankroll lanes): {money(bal_now)}.",
]:
    pdf.cell(0, 6, line, new_x='LMARGIN', new_y='NEXT')
pdf.ln(2)

section('Balance - lanes with a recorded starting bankroll')
pdf.set_font('Helvetica', '', 10)
pdf.cell(0, 6, 'Balance = started + realized profit from closes. Open positions are listed but not valued.', new_x='LMARGIN', new_y='NEXT')
pdf.ln(1)
header_row([('Lane', 42), ('Started', 28), ('Realized P&L', 32), ('Balance now', 32), ('Open', 18)])
for l in bal_lanes:
    d = lanes[l]; b = bank[l]
    data_row([(l, 42, 'L'), (money(b['start']), 28, 'R'), (money(d['all_net']), 32, 'R'),
              (money(b['start'] + d['all_net']), 32, 'R'), (str(b['open']), 18, 'R')])
data_row([('TOTAL', 42, 'L'), (money(bal_start), 28, 'R'), (money(bal_real), 32, 'R'),
          (money(bal_now), 32, 'R'), (str(sum(bank[l]['open'] for l in bal_lanes)), 18, 'R')], bold=True)
pdf.ln(2)

section('This week by lane (Mon Oct 26 to now)')
header_row([('Lane', 42), ('Closes', 22), ('Wins', 22), ('Win rate', 26), ('Net', 32)])
for l, d in sorted(lanes.items(), key=lambda kv: -kv[1]['week_net']):
    if d['week_c'] == 0:
        continue
    data_row([(l, 42, 'L'), (str(d['week_c']), 22, 'R'), (str(d['week_w']), 22, 'R'),
              (pct(d['week_w'], d['week_c']), 26, 'R'), (money(d['week_net']), 32, 'R')])
data_row([('TOTAL', 42, 'L'), (str(wk_c), 22, 'R'), (str(wk_w), 22, 'R'),
          (pct(wk_w, wk_c), 26, 'R'), (money(wk_n), 32, 'R')], bold=True)
pdf.set_font('Helvetica', '', 10)
pdf.cell(0, 6, 'All other lanes: 0 closes this week.', new_x='LMARGIN', new_y='NEXT')
pdf.ln(2)

section('Daily breakdown - all paper USD lanes')
header_row([('Day', 42), ('Closes', 22), ('Wins', 22), ('Net', 32)])
for i, (_, nm) in enumerate(DAYB):
    c = sum(d['days'][i][0] for d in lanes.values())
    w = sum(d['days'][i][1] for d in lanes.values())
    n = sum(d['days'][i][2] for d in lanes.values())
    data_row([(nm, 42, 'L'), (str(c), 22, 'R'), (str(w), 22, 'R'), (money(n), 32, 'R')])
data_row([('WEEK TOTAL', 42, 'L'), (str(wk_c), 22, 'R'), (str(wk_w), 22, 'R'), (money(wk_n), 32, 'R')], bold=True)
pdf.ln(2)

section('Best and worst closes this week')
allwins = []
allloss = []
for lane, d in lanes.items():
    for pnl, mint, mult in d['week_wins']:
        allwins.append((pnl, lane, mint, mult))
    for pnl, mint, mult in d['week_losses']:
        allloss.append((pnl, lane, mint, mult))
header_row([('P&L', 28), ('Lane', 32), ('Coin', 42), ('Multiple', 26)])
for pnl, lane, mint, mult in sorted(allwins, reverse=True)[:5]:
    data_row([(money(pnl), 28, 'R'), (lane, 32, 'L'), (mint, 42, 'L'), (f"{mult:.2f}x", 26, 'R')])
for pnl, lane, mint, mult in sorted(allloss)[:3]:
    data_row([(money(pnl), 28, 'R'), (lane, 32, 'L'), (mint, 42, 'L'), (f"{mult:.2f}x", 26, 'R')])
pdf.ln(2)

section('Notes - read this before quoting any number')
pdf.set_font('Helvetica', '', 10)
notes = [
    'Paper only. The bot has never traded real funds, so real money earned is $0.00. Paper profits are not spendable.',
    'Window: Monday 26 October 2026 00:00 WAT to ' + wat(NOW) + '. Trailing 7 days covers Oct 24-30.',
    'Scope: paper USD lanes only. Two voided phantom closes are excluded everywhere (274rXqtA +$60,749.25 and BVY5qMsi9G5f +$16,059,490.26 - glitch quotes, never in the live ledgers).',
    'Old SOL book excluded: frozen since Oct 15, SOL-denominated, separate from this week.',
    'grad_raw all-time includes its 42 pre-chain checkpoint closes (net +$1.53, wins unknown). grad_select checkpoint is an anchor already inside its chain total and is not added again.',
    'Mirror book counted once: the verdict chain hl_mirror closes are the same trades as the mirror ledger (identical coins, prices, times) and are not double-counted. Any chain hl close absent from the mirror ledger is kept separately as hl_residual.',
    'Balances use realized closes only. Ledger-reported bankroll differs on two lanes with no open positions: grad_raw ledger shows ' + money(bank['grad_raw']['now']) + ' vs ' + money(100 + lanes['grad_raw']['all_net']) + ' realized (gap ' + money(bank['grad_raw']['now'] - (100 + lanes['grad_raw']['all_net'])) + ', under investigation); grad ledger shows ' + money(bank['grad']['now']) + ' vs ' + money(100 + lanes['grad']['all_net']) + ' realized (gap ' + money(bank['grad']['now'] - (100 + lanes['grad']['all_net'])) + '). Open positions are counted but not valued.',
    'Degen cards were permanently killed Oct 30 (user order). They carried no paper P&L this week beyond the mirror lane shown above.',
    'A win means a close with profit above $0. Stakes: grad_select and grad_raw $25 per coin, tranche $25, all other lanes $1.',
]
for n in notes:
    pdf.multi_cell(0, 5, '- ' + n, new_x='LMARGIN', new_y='NEXT')
    pdf.ln(1)

out = '/home/hatch/workspace/your_files/weekly-pnl-2026-10-30.pdf'
pdf.output(out)
print('PDF written:', out, os.path.getsize(out), 'bytes')
