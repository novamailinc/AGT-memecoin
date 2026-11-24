"""Analyze fetched oldest transactions for one mint (local files only).
Usage: analyze_oldest.py <txfile> <mint>
Prints: creation tx, burst timing, fail rates, first buys in time order,
repeat buyers, venues. Writes <txfile>.summary.json.
"""
import json, sys
from collections import Counter, defaultdict

TXFILE, MINT = sys.argv[1], sys.argv[2]
raw = json.load(open(TXFILE))
txs = []
for sig, tx in raw.items():
    if not isinstance(tx, dict) or 'blockTime' not in tx:
        continue
    txs.append((sig, tx))
txs.sort(key=lambda x: (x[1]['blockTime'], x[0]))
print('txs analyzed:', len(txs))
if not txs:
    sys.exit(0)
t0 = txs[0][1]['blockTime']
print('first tx bt:', t0, '| last tx bt:', txs[-1][1]['blockTime'],
      '| span s:', txs[-1][1]['blockTime'] - t0)

def token_delta(tx, mint):
    """Return {owner: delta_tokens_float} for this mint from balance changes."""
    meta = tx.get('meta') or {}
    pre = {(b.get('owner'), b.get('accountIndex')): b for b in
           (meta.get('preTokenBalances') or []) if b.get('mint') == mint}
    out = {}
    for b in (meta.get('postTokenBalances') or []):
        if b.get('mint') != mint:
            continue
        key = (b.get('owner'), b.get('accountIndex'))
        pre_amt = 0.0
        if key in pre:
            try:
                pre_amt = float(pre[key]['uiTokenAmount']['uiAmount'] or 0)
            except Exception:
                pre_amt = 0.0
        try:
            post_amt = float(b['uiTokenAmount']['uiAmount'] or 0)
        except Exception:
            post_amt = 0.0
        d = post_amt - pre_amt
        if abs(d) > 0:
            out[b.get('owner')] = out.get(b.get('owner'), 0.0) + d
    return out

def sol_spent(tx):
    meta = tx.get('meta') or {}
    pre, post = meta.get('preBalances') or [], meta.get('postBalances') or []
    if not pre or not post:
        return None
    return (pre[0] - post[0]) / 1e9  # lamports -> SOL (incl. fee)

fails = 0
progs = Counter()
buys = []  # (offset, sig, buyer, tokens, sol)
repeat = Counter()
for sig, tx in txs:
    meta = tx.get('meta') or {}
    if meta.get('err') is not None:
        fails += 1
    msg = (tx.get('transaction') or {}).get('message') or {}
    for ix in msg.get('instructions') or []:
        progs[ix.get('programId')] = progs.get(ix.get('programId'), 0) + 1
    d = token_delta(tx, MINT)
    for owner, delta in d.items():
        if delta > 0:
            buys.append((tx['blockTime'] - t0, sig, owner, delta,
                         sol_spent(tx)))
            repeat[owner] += 1
print('failed txs:', fails, '/', len(txs))
for win in (3, 10, 60, 300):
    w = [t for t in txs if t[1]['blockTime'] - t0 <= win]
    wf = sum(1 for _, t in w if (t.get('meta') or {}).get('err') is not None)
    print('first %ss: %d txs, %d failed (%.0f%%)' % (
        win, len(w), wf, 100.0 * wf / max(1, len(w))))
print('top programs:', progs.most_common(8))
print('--- first 25 buys (offset s, buyer, tokens, SOL spent incl fee) ---')
for off, sig, owner, tok, sol in buys[:25]:
    print('%4ds %s %s tokens=%.1f sol=%s' % (
        off, sig[:12], owner, tok,
        ('%.4f' % sol) if sol is not None else '?'))
print('--- repeat buyers in window ---')
for owner, n in repeat.most_common(10):
    if n > 1:
        tot = sum(t for _, _, o, t, _ in buys if o == owner)
        print('%s x%d buys, %.1f tokens total' % (owner, n, tot))
json.dump({'t0': t0, 'n': len(txs), 'fails': fails,
           'buys': buys, 'repeat': dict(repeat)},
          open(TXFILE + '.summary.json', 'w'))
