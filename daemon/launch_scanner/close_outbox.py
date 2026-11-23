"""Shared scanner close evidence. No quote, policy or qualification decisions.

Context precedes economic execution. Exact native CLOSE evidence completes the
atomic SQLite event projection after restart. Legacy learning effects are not
replayed: their independent file writers do not yet support exact effect IDs.
The committed evidence explicitly retains that unresolved projection boundary.
"""
import copy
import hashlib
import json
import math


def key(identity):
    return 'scanner-close-' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def load(store, identity):
    rows = store.journal_query(kind='scanner_close_intent', symbol=key(identity), limit=2)
    if not rows:
        return None
    if len(rows) != 1 or rows[0]['detail']['identity'] != identity:
        raise ValueError('scanner close context identity conflict')
    return rows[0]['detail']


def prepare(store, identity, pos, position_key, reason, peak, now, state, default_volume):
    prior = load(store, identity)
    if prior is not None:
        return prior
    context = copy.deepcopy({'identity':identity, 'position':pos, 'position_key':position_key,
        'reason':reason, 'peak':peak, 'decision_ts':now, 'default_volume':default_volume,
        'smart_activity':(state.get('smart_activity') or {}).get(pos.get('mint') or position_key, []),
        'narrative_tags':(state.get('narrative_tags') or {}).get(pos.get('mint') or position_key, ['other'])})
    context = store.scanner_close_context(key(identity), context)
    if load(store, identity) != context:
        raise OSError('scanner close context not durably readable')
    return context


def commit(store, context, receipt):
    from agent.events import validate_event
    from daemon.launch_scanner import EXIT_POLICY_VERSION
    pos = context['position']
    if receipt['ticket'] != context['identity']['ticket'] or receipt['symbol'] != pos['symbol']:
        raise ValueError('scanner close receipt position conflict')
    close_px = float(receipt['price_sol'])
    allocations = receipt['allocations']
    if len(allocations) != 1 or allocations[0]['ticket'] != receipt['ticket']:
        raise ValueError('scanner close requires exact native ticket allocation')
    allocation = allocations[0]
    basis = float(allocation['basis_sol'])
    proceeds = float(allocation['proceeds_sol'])
    tokens = float(allocation['tokens'])
    pnl = float(receipt['realized_pnl_sol'])
    if (not all(math.isfinite(x) for x in (basis,proceeds,tokens,pnl,close_px))
            or min(basis,proceeds,tokens,close_px) < 0
            or not math.isclose(proceeds, tokens*close_px, rel_tol=1e-10, abs_tol=1e-12)
            or not math.isclose(pnl, proceeds-basis, rel_tol=1e-10, abs_tol=1e-12)):
        raise ValueError('scanner close native allocation economics conflict')
    event = validate_event({'event':'position.closed', 'event_id':key(context['identity']),
        'severity':'INFO', 'ts':receipt['ts'], 'ticket':receipt['ticket'], 'symbol':pos['symbol'],
        'reason':'degen_'+context['reason'], 'degen_tier':pos.get('tier') or '?',
        'label':pos.get('label') or '', 'leg':pos.get('leg') or 'A',
        'close_price':close_px, 'profit':round(pnl,6), 'exit_version':EXIT_POLICY_VERSION})
    created = store.scanner_close_event(event, {'context':context, 'native_receipt':receipt,
        'learning_projection':'unverified-independent-writers'})
    if created:
        from agent.events.bus import notify_committed
        notify_committed(event)
    return event
