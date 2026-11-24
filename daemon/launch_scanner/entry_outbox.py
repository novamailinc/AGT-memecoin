"""Source-pinned shared native entry recovery, never submits an order."""
import copy
import hashlib
import json
import logging
from datetime import datetime
logger=logging.getLogger(__name__)


def key(book,request):
    return 'scanner-entry-'+hashlib.sha256(json.dumps([book,request['idempotency_key']],sort_keys=True).encode()).hexdigest()


def validate(context):
    source=copy.deepcopy(context);digest=source.pop('sha256')
    if hashlib.sha256(json.dumps(source,sort_keys=True,allow_nan=False).encode()).hexdigest()!=digest:
        raise ValueError('scanner entry pinned source digest conflict')
    if (source.get('version')!=1 or source['template']['mint']!=source['request']['mint']
            or source['template']['symbol']!=source['request']['symbol']
            or source['template']['volume_sol']!=source['request']['volume']
            or source['request']['direction']!='BUY'):
        raise ValueError('scanner entry source identity conflict')
    return context


def conflicted(state,position_key=None,mint=None,ticket=None):
    for pk,conflict in (state.get('entry_position_conflicts') or {}).items():
        if position_key==pk or mint==pk:return True
        for name in ('active','durable'):
            proposal=conflict.get(name) or {}
            if ticket is not None and proposal.get('ticket')==ticket:return True
            if mint and proposal.get('mint')==mint:return True
    return False


def prepare(scanner,store,adapter,req,degen,state,card,intel,mint,symbol,wave,tag,leg,position_key,tp):
    from dataclasses import asdict
    book=adapter.risk_state_identity()
    if book is None:return None
    request=asdict(req);request['mint']=mint
    event_id=key(book,request)
    rows=store.journal_query(kind='scanner_entry_intent',symbol=event_id,limit=2)
    if rows:
        if len(rows)!=1:raise ValueError('duplicate scanner entry intent')
        prior=rows[0]['detail']
        if prior['book']!=book or prior['request']!=request or prior['position_key']!=position_key:
            raise ValueError('scanner entry retry request conflict')
        return validate(prior)
    template=scanner._leg_pos_dict(ticket=0,symbol=symbol,mint=mint,wave=wave,tag=tag,
        entry_price=0.,tier=card.get('tier') or '?',sl=req.stop_loss,tp=tp,volume_sol=req.volume,
        max_hold_hours=float(getattr(degen,'tier_b_max_hold_hours',24.) if leg=='B' else degen.max_hold_hours),
        leg=leg,card=copy.deepcopy(card),intel=copy.deepcopy(intel),saf=copy.deepcopy(intel.get('safety') or {}),
        deployer=((state.get('deployer_cache') or {}).get(mint) or {}).get('deployer'),
        entry_features=scanner._entry_features(card))
    sources={}
    for name in ('narrative_tags','smart_activity'):
        mapping=state.get(name)
        sources[name]={'state':'recorded','value':copy.deepcopy(mapping[mint])} if isinstance(mapping,dict) and mint in mapping else {'state':'unrecorded'}
    context={'version':1,'book':book,'request':request,'position_key':position_key,'template':template,'sources':sources}
    context['sha256']=hashlib.sha256(json.dumps(context,sort_keys=True,allow_nan=False).encode()).hexdigest()
    return store.scanner_entry_context(event_id,context)


def project_one(store,adapter,context,state):
    validate(context)
    event_id=key(context['book'],context['request'])
    if conflicted(state,position_key=context['position_key'],mint=context['request']['mint']):return
    def publish(evidence):
        fill=evidence['fill']
        if conflicted(state,position_key=context['position_key'],mint=fill['shared_mint'],ticket=fill['ticket']):return
        pos=copy.deepcopy(context['template'])
        pos.update(ticket=fill['ticket'],entry_price_sol=fill['price_sol'],peak_price_sol=fill['price_sol'],
            volume_sol=fill['volume_sol'],entry_ts=datetime.fromisoformat(fill['ts']).timestamp())
        if pos['mint']!=fill['shared_mint'] or pos['symbol']!=fill['symbol']:
            raise ValueError('scanner entry position source mismatch')
        positions=state.setdefault('degen_positions',{})
        prior=positions.get(context['position_key'])
        if prior is not None and prior.get('ticket')!=pos['ticket']:raise ValueError('scanner entry live ticket conflict')
        persisted=store.scanner_entry_publish(event_id,context['position_key'],pos,context['sources'],active_state=state,closed_tickets=evidence['closed_tickets'])
        state.clear();state.update(copy.deepcopy(persisted))
    # Native lock spans exact-open evidence, SQLite merge and in-memory publication.
    # A close cannot commit between the evidence check and this publication.
    adapter.scanner_entry_evidence(context['request'],context['book'],publish=publish)


def recover(store,adapter,state):
    try:
        book=adapter.risk_state_identity()
        if book is None:return
        rows=store.journal_query(kind='scanner_entry_intent',limit=-1)
    except Exception:
        logger.exception('entry recovery source unavailable');return
    for row in reversed(rows):
        try:
            context=row['detail']
            if context['book']!=book:continue
            if row['symbol']!=key(book,context['request']):raise ValueError('scanner entry intent identity conflict')
            project_one(store,adapter,context,state)
        except Exception:
            from .close_outbox import _capture_projection_failure
            _capture_projection_failure('entry_recovery_pending')
            logger.exception('entry projection pending; no restart submission')
