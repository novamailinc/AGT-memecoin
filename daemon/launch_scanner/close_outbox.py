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
import logging
import sys
logger = logging.getLogger(__name__)
from datetime import datetime


def key(identity):
    return 'scanner-close-' + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def load(store, identity):
    rows = store.journal_query(kind='scanner_close_intent', symbol=key(identity), limit=2)
    if not rows:
        return None
    if len(rows) != 1 or rows[0]['detail']['identity'] != identity:
        raise ValueError('scanner close context identity conflict')
    return rows[0]['detail']


def prepare(store, identity, pos, position_key, reason, peak, now, state, default_volume,
            backtest_spec=None, backtest_state="unrecorded-legacy", clock_spec=None, exit_copy_spec=None, deployer_spec=None,backtest_source=None):
    prior = load(store, identity)
    if prior is not None:
        return prior
    context = copy.deepcopy({'identity':identity, 'position':pos, 'position_key':position_key,
        'reason':reason, 'peak':peak, 'decision_ts':now, 'default_volume':default_volume,
        'backtest_spec':backtest_spec, 'backtest_state':backtest_state,
        'backtest_source':backtest_source,
        'clock_spec':clock_spec,
        'exit_copy_spec':exit_copy_spec,
        'deployer_spec':deployer_spec,
        'narrative_spec':narrative_source(state,pos.get('mint') or position_key,default_volume),
        'smart_activity':(state.get('smart_activity') or {}).get(pos.get('mint') or position_key, []),
        'narrative_tags':(state.get('narrative_tags') or {}).get(pos.get('mint') or position_key, ['other'])})
    from . import smartmoney
    mint=pos.get('mint') or position_key
    activity=state.get('smart_activity')
    context['wallet_learning_spec']={
        'state':'recorded' if isinstance(activity,dict) and mint in activity else 'unrecorded',
        'path':str(smartmoney._REGISTRY_FILE.absolute()),
        'mint':mint,
        'wallets':copy.deepcopy(activity[mint]) if isinstance(activity,dict) and mint in activity else None}
    context = store.scanner_close_context(key(identity), context)
    if load(store, identity) != context:
        raise OSError('scanner close context not durably readable')
    return context


def prepare_orphan(store,identity,position,reason,now):
    """Only native economics are known. Never invent missing scanner inputs."""
    prior=load(store,identity)
    if prior is not None:return prior
    unavailable={'state':'unavailable-orphan-scanner-context'}
    context={'identity':identity,'position':copy.deepcopy(position),
        'position_key':'orphan:'+str(identity['ticket']),'reason':reason,
        'peak':None,'decision_ts':now,'default_volume':None,'orphan':True,
        'source_provenance':{'native_position':'recorded','scanner_position':'unavailable',
            'narrative_tags':'unavailable','peak':'unavailable','configured_basis':'unavailable'},
        'backtest_spec':None,'backtest_state':'unavailable-orphan-scanner-context',
        'backtest_source':copy.deepcopy(unavailable),
        'wallet_learning_spec':copy.deepcopy(unavailable),
        'clock_spec':copy.deepcopy(unavailable),'narrative_spec':copy.deepcopy(unavailable),
        'exit_copy_spec':copy.deepcopy(unavailable),'deployer_spec':copy.deepcopy(unavailable)}
    context=store.scanner_close_context(key(identity),context)
    if load(store,identity)!=context:raise OSError('orphan close context unreadable')
    return context


def recover_orphans(store,adapter):
    """Isolate unavailable orphan evidence from independent economic exits."""
    try:
        book=adapter.risk_state_identity()
        if book is None:return
        rows=store.journal_query(kind='scanner_close_intent',limit=-1)
    except Exception:
        _capture_projection_failure('orphan_recovery_source_pending')
        logger.exception('orphan recovery source unavailable; ordinary exits continue')
        return
    for row in reversed(rows):
        try:
            context=row['detail']
            if context.get('orphan') is not True:continue
            identity=context['identity']
            if {k:v for k,v in identity.items() if k!='ticket'}!=book:continue
            if row['symbol']!=key(identity):raise ValueError('orphan close intent identity conflict')
            receipt=adapter.scanner_close_receipt(identity)
            if receipt is not None:commit(store,context,receipt)
        except Exception:
            _capture_projection_failure('orphan_recovery_pending')
            logger.exception('orphan recovery pending; independent exits continue')


def native_history_economics(receipt):
    """Exact native allocated close evidence, never inferred from price ratio.

    This is SOL-native paper settlement, not USD/FX or real fill calibration.
    """
    allocations=receipt['allocations']
    if len(allocations)!=1 or allocations[0]['ticket']!=receipt['ticket']:
        raise ValueError('native history requires one exact ticket allocation')
    a=allocations[0]
    basis=a['basis_sol'];proceeds=a['proceeds_sol'];tokens=a['tokens']
    pnl=receipt['realized_pnl_sol']
    if (any(type(v) not in (int,float) or not math.isfinite(v) for v in (basis,proceeds,tokens,pnl))
            or min(basis,proceeds,tokens)<0
            or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
        raise ValueError('native history economics conflict')
    net=proceeds/basis if basis>0 else None
    if net is not None and not math.isfinite(net):net=None
    return {'schema':'native-allocated-close-v1','ticket':receipt['ticket'],
        'basis_sol':basis,'proceeds_sol':proceeds,'tokens':tokens,'realized_pnl_sol':pnl,
        'net_proceeds_over_basis':net,
        'net_multiple_state':'recorded' if net is not None else 'unavailable-nonpositive-basis-or-overflow',
        'scope':'native SOL paper allocation only; USD FX and real execution not verified'}


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
    if context.get('orphan') is True:event['orphan']=True
    # Immutable prior evidence wins over newer reporting schemas. Validate
    # both economic source and projections, never retrofit old close records.
    if hasattr(store,'journal_query'):
        prior=store.journal_query(kind='scanner_close_committed',symbol=key(context['identity']),limit=2)
        if prior:
            if (len(prior)!=1 or prior[0]['detail'].get('context')!=context
                    or prior[0]['detail'].get('native_receipt')!=receipt):
                raise ValueError('scanner close committed source conflict')
            store.scanner_close_event(event,prior[0]['detail'])
            return event
    spec = copy.deepcopy(context.get('backtest_spec'))
    if spec is not None:
        record = spec['record']
        entry_px = float(allocation['entry_price_sol'])
        close_ts = datetime.fromisoformat(receipt['ts']).timestamp()
        entry_ts = float(pos.get('entry_ts') or context['decision_ts'])
        realized_mult = close_px / entry_px if entry_px > 0 else None
        peak_px = float(context.get('peak') or entry_px or close_px)
        peak_mult = peak_px / entry_px if entry_px > 0 else None
        if realized_mult is not None and not math.isfinite(realized_mult):realized_mult=None
        if peak_mult is not None and not math.isfinite(peak_mult):peak_mult=None
        rug = realized_mult <= .10 if realized_mult is not None else None
        record['snapshot_input_evidence']=copy.deepcopy((context.get('backtest_source') or {}).get('input_evidence',record.get('snapshot_input_evidence',{'state':'legacy-unrecorded','rows':[]})))
        record['snapshot_recorded_t']=context['decision_ts']
        record['native_allocation']=native_history_economics(receipt)
        record['native_allocation_state']='recorded'
        record['realized_mult_metric']='close-price-over-entry-price-not-net-return'
        record.update(outcome_id=key(context['identity']), t_close=close_ts,
            entry_price_sol=entry_px, close_price_sol=close_px, peak_price_sol=peak_px,
            peak_mult=round(peak_mult,4) if peak_mult is not None else None,
            realized_mult=round(realized_mult,4) if realized_mult is not None else None,
            pnl_sol=round(pnl,6), rug=rug, hold_min=round(max(0.,(close_ts-entry_ts)/60.),1))
        record['outcome'] = {'peak_mult':round(peak_mult,4) if peak_mult is not None else None, 'peak_t':close_ts,
                            'rug':rug, 'end_t':close_ts, 'recorded_t':close_ts}
    clock_spec=copy.deepcopy(context.get('clock_spec'))
    if clock_spec and clock_spec.get('state')=='recorded':
        from . import exit_liquidity_clock
        from types import SimpleNamespace
        close_ts=datetime.fromisoformat(receipt['ts']).timestamp()
        entry_px=float(allocation['entry_price_sol'])
        clock_spec['record']=exit_liquidity_clock.record_close(SimpleNamespace(),
            pos.get('mint') or context['position_key'],float(pos.get('entry_ts') or context['decision_ts']),
            entry_px,close_ts,close_px,float(context.get('peak') or entry_px),context['reason'],pnl,
            pos.get('label') or '',record_only=True,pinned_peak=clock_spec.get('peak_info'),use_pinned_peak=True)
        if clock_spec['record'] is None:clock_spec['state']='invalid-source'
    created = store.scanner_close_event(event, {'context':context, 'native_receipt':receipt,
        'learning_projection':'unverified-independent-writers', 'backtest_spec':spec,'clock_spec':clock_spec,
        'narrative_spec':narrative_record(context,receipt),
        'exit_copy_spec':exit_copy_record(context,receipt),
        'deployer_spec':deployer_record(context,receipt)})
    if created:
        from agent.events.bus import notify_committed
        notify_committed(event)
    return event


def project_backtests(store):
    """Retry pinned history records even when scanner positions are already gone.

    Old evidence without a pinned spec is not retroactively reconstructed.
    Dataset failure leaves the source available and does not re-close a trade.
    """
    from . import backtest
    try:
        rows = store.journal_query(kind='scanner_close_committed', limit=-1)
    except Exception:
        logger.exception('backtest projection source unavailable; economic exits continue')
        return
    for row in reversed(rows):
        try:
            evidence = row['detail']
            spec = evidence.get('backtest_spec')
            if spec is None:
                if row['symbol']!=key(evidence['context']['identity']):raise ValueError('backtest source close conflict')
                spec=rebuild_backtest(evidence)
                if spec is None:continue
            event_id = row['symbol']
            if store.journal_query(kind='scanner_backtest_projected', symbol=event_id, limit=1):
                continue
            if not backtest.record_history(spec['path'], spec['record']):
                logger.error('backtest projection pending for %s', event_id)
                continue
            store.journal_add({'kind':'scanner_backtest_projected', 'symbol':event_id,
                               'detail':spec})
        except Exception:
            _capture_projection_failure('backtest_projection_pending')
            logger.exception('backtest projection still pending; economic exits continue')


def project_wallet_learning(store):
    """Replay only newly pinned wallet effects. No historical inference.

    Receipt and counters share the wallet document. The journal ACK is progress
    only: losing it cannot reapply the effect. Other learners remain unverified.
    """
    from . import smartmoney
    try:rows=store.journal_query(kind='scanner_close_committed',limit=-1)
    except Exception:
        logger.exception('wallet learning source unavailable');return
    for row in reversed(rows):
        try:
            evidence=row['detail'];context=evidence['context']
            spec=context.get('wallet_learning_spec')
            if not spec or spec.get('state')!='recorded':continue
            wallets=spec['wallets']
            if not isinstance(wallets,list) or any(not isinstance(w,str) or not w for w in wallets):
                raise ValueError('wallet learning source malformed')
            from pathlib import Path
            path=spec.get('path')
            if not isinstance(path,str) or not path or not Path(path).is_absolute():
                raise ValueError('pinned wallet path invalid')
            pos=context['position'];identity=context['identity'];receipt=evidence['native_receipt']
            if (row['symbol']!=key(identity) or spec.get('mint')!=(pos.get('mint') or context['position_key'])
                    or not isinstance(spec.get('mint'),str) or not spec['mint']
                    or receipt['ticket']!=identity['ticket'] or receipt['symbol']!=pos['symbol']):
                raise ValueError('wallet projection source identity conflict')
            allocations=receipt['allocations']
            if len(allocations)!=1 or allocations[0]['ticket']!=receipt['ticket']:
                raise ValueError('wallet projection allocation identity conflict')
            allocation=allocations[0]
            pnl=float(receipt['realized_pnl_sol']);price=float(receipt['price_sol'])
            basis=float(allocation['basis_sol']);proceeds=float(allocation['proceeds_sol']);tokens=float(allocation['tokens'])
            if (not all(math.isfinite(x) for x in (pnl,price,basis,proceeds,tokens))
                    or min(price,basis,proceeds,tokens)<0
                    or not math.isclose(proceeds,tokens*price,rel_tol=1e-10,abs_tol=1e-12)
                    or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
                raise ValueError('wallet projection receipt economics conflict')
            effect_id=row['symbol']+':wallet-outcome-v1'
            if store.journal_query(kind='scanner_wallet_learning_projected',symbol=effect_id,limit=1):continue
            receipt=evidence['native_receipt']
            smartmoney.record_outcome(spec['mint'],float(receipt['realized_pnl_sol'])>0,
                wallets,effect_id=effect_id,registry_path=path,require_existing=True)
            store.journal_add({'kind':'scanner_wallet_learning_projected','symbol':effect_id,
                'detail':{'source_close':row['symbol'],'scope':'wallet-only','spec':spec}})
        except Exception:
            _capture_projection_failure('wallet_projection_pending')
            logger.exception('wallet learning pending; economic exits continue')


def project_exit_clock(store):
    """Exact pinned observer records, independently retried after economic close."""
    from . import exit_liquidity_clock
    from pathlib import Path
    try:rows=store.journal_query(kind='scanner_close_committed',limit=-1)
    except Exception:
        logger.exception('exit clock source unavailable');return
    for row in reversed(rows):
        try:
            evidence=row['detail'];spec=evidence.get('clock_spec')
            if not spec or spec.get('state')!='recorded':continue
            path=spec.get('path')
            if not isinstance(path,str) or not path or not Path(path).is_absolute():raise ValueError('clock pinned path invalid')
            context=evidence['context'];receipt=evidence['native_receipt']
            pos=context['position'];identity=context['identity'];original=context['clock_spec']
            if row['symbol']!=key(identity) or receipt['ticket']!=identity['ticket'] or receipt['symbol']!=pos['symbol']:
                raise ValueError('clock close identity conflict')
            if original.get('state')!='recorded' or any(spec.get(k)!=original.get(k) for k in ('state','path','peak_info')):
                raise ValueError('clock pinned source conflict')
            allocations=receipt['allocations']
            if len(allocations)!=1 or allocations[0]['ticket']!=receipt['ticket']:raise ValueError('clock allocation conflict')
            allocation=allocations[0]
            basis=float(allocation['basis_sol']);proceeds=float(allocation['proceeds_sol']);tokens=float(allocation['tokens'])
            price=float(receipt['price_sol']);pnl=float(receipt['realized_pnl_sol']);entry=float(allocation['entry_price_sol'])
            if (not all(math.isfinite(x) for x in (basis,proceeds,tokens,price,pnl,entry)) or min(basis,proceeds,tokens,price,entry)<0
                    or not math.isclose(proceeds,tokens*price,rel_tol=1e-10,abs_tol=1e-12)
                    or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
                raise ValueError('clock native economics conflict')
            from types import SimpleNamespace
            expected=exit_liquidity_clock.record_close(SimpleNamespace(),pos.get('mint') or context['position_key'],
                float(pos.get('entry_ts') or context['decision_ts']),entry,datetime.fromisoformat(receipt['ts']).timestamp(),
                price,float(context.get('peak') or entry),context['reason'],pnl,pos.get('label') or '',record_only=True,
                pinned_peak=original.get('peak_info'),use_pinned_peak=True)
            if expected is None or spec.get('record')!=expected:raise ValueError('clock record source conflict')
            effect=row['symbol']+':exit-clock-v1'
            if store.journal_query(kind='scanner_exit_clock_projected',symbol=effect,limit=1):continue
            exit_liquidity_clock.append_record(path,spec['record'],effect)
            store.journal_add({'kind':'scanner_exit_clock_projected','symbol':effect,'detail':{'source_close':row['symbol'],'scope':'exit-clock-only'}})
        except Exception:
            _capture_projection_failure('clock_projection_pending')
            logger.exception('exit clock pending; economic exits continue')


def clock_source(degen,mint):
    from . import exit_liquidity_clock
    from pathlib import Path
    if not getattr(degen,'exit_clock_enabled',True):return {'state':'disabled'}
    path=exit_liquidity_clock._resolve_path(None)
    if not path:return {'state':'unavailable-path'}
    return {'state':'recorded','path':str(Path(path).absolute()),
            'peak_info':copy.deepcopy(exit_liquidity_clock._PEAKS.get(mint))}


def narrative_source(state,mint,configured_basis):
    """Pin deployed metric, not a replacement peak/native-basis strategy."""
    from . import narratives
    from pathlib import Path
    if type(configured_basis) not in (int,float) or not math.isfinite(configured_basis) or configured_basis<0:
        return {'state':'unavailable-basis'}
    tags=(state.get('narrative_tags') or {}).get(mint) or ['other']
    return {'state':'recorded','path':str(Path(narratives.default_heat_path()).absolute()),
        'metric':'legacy-realized-pnl-over-configured-entry-v1','configured_basis_sol':configured_basis,
        'tags':list(narratives.NarrativeHeat._norm_tags(tags)),'rug':False}


def narrative_record(context,receipt):
    from . import narratives
    spec=copy.deepcopy(context.get('narrative_spec'))
    if not spec or spec.get('state')!='recorded':return spec
    basis=spec['configured_basis_sol'];pnl=float(receipt['realized_pnl_sol'])
    if (type(basis) not in (int,float) or not math.isfinite(basis) or basis<0
            or basis!=context['default_volume'] or not math.isfinite(pnl)
            or spec.get('metric')!='legacy-realized-pnl-over-configured-entry-v1' or spec.get('rug') is not False):
        raise ValueError('narrative configured metric unavailable')
    tags=list(narratives.NarrativeHeat._norm_tags(context.get('narrative_tags') or ['other']))
    if tags!=spec.get('tags'):raise ValueError('narrative pinned tags conflict')
    ts=datetime.fromisoformat(receipt['ts']).timestamp()
    mult=max(0.,1.+pnl/basis) if basis>0 else 1.
    spec['outcome']=narratives.validate_heat_outcome([ts,tags,mult,False])
    return spec


def project_narratives(store):
    """Project preserved configured-basis metric once at native close time.

    Correct source time prevents retries from making old heat newly recent.
    Missing old specs remain unrecorded, never inferred from today's config.
    """
    from . import narratives
    from pathlib import Path
    applied=0
    try:rows=store.journal_query(kind='scanner_close_committed',limit=-1)
    except Exception:
        logger.exception('narrative source unavailable');return applied
    for row in reversed(rows):
        try:
            evidence=row['detail'];context=evidence['context'];spec=evidence.get('narrative_spec')
            if not spec or spec.get('state')!='recorded':continue
            receipt=evidence['native_receipt'];identity=context['identity'];pos=context['position']
            if row['symbol']!=key(identity) or receipt['ticket']!=identity['ticket'] or receipt['symbol']!=pos['symbol']:
                raise ValueError('narrative close identity conflict')
            allocations=receipt['allocations']
            if len(allocations)!=1 or allocations[0]['ticket']!=receipt['ticket']:raise ValueError('narrative allocation conflict')
            allocation=allocations[0]
            basis=float(allocation['basis_sol']);proceeds=float(allocation['proceeds_sol']);tokens=float(allocation['tokens'])
            price=float(receipt['price_sol']);pnl=float(receipt['realized_pnl_sol'])
            if (not all(math.isfinite(x) for x in (basis,proceeds,tokens,price,pnl)) or min(basis,proceeds,tokens,price)<0
                    or not math.isclose(proceeds,tokens*price,rel_tol=1e-10,abs_tol=1e-12)
                    or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
                raise ValueError('narrative native economics conflict')
            expected=narrative_record(context,receipt)
            if expected!=spec:raise ValueError('narrative record source conflict')
            path=spec.get('path')
            if not isinstance(path,str) or not path or not Path(path).is_absolute():raise ValueError('narrative path invalid')
            effect=row['symbol']+':narrative-v1'
            if store.journal_query(kind='scanner_narrative_projected',symbol=effect,limit=1):continue
            narratives.append_heat(path,spec['outcome'],effect)
            store.journal_add({'kind':'scanner_narrative_projected','symbol':effect,
                'detail':{'source_close':row['symbol'],'scope':'narrative-only','metric':spec['metric']}})
            applied+=1
        except Exception:
            _capture_projection_failure('narrative_projection_pending')
            logger.exception('narrative learning pending; economic exits continue')
    return applied


def exit_copy_source(degen):
    from . import exit_copy
    from pathlib import Path
    if not getattr(degen,'exit_copy_enabled',True):return {'state':'disabled'}
    path=exit_copy._resolve_path(None)
    if not path:return {'state':'unavailable-path'}
    return {'state':'recorded','path':str(Path(path).absolute())}


def exit_copy_record(context,receipt):
    from . import exit_copy
    spec=copy.deepcopy(context.get('exit_copy_spec'))
    if not spec or spec.get('state')!='recorded':return spec
    allocation=receipt['allocations'][0];entry=float(allocation['entry_price_sol'])
    pos=context['position'];mint=pos.get('mint') or context['position_key']
    record={'mint':mint,'entry_ts':float(pos.get('entry_ts') or context['decision_ts']),
        'close_ts':datetime.fromisoformat(receipt['ts']).timestamp(),
        'close_mult':round(float(receipt['price_sol'])/entry if entry>0 else 0.,4),
        'reason':str(context['reason'] or ''),'pnl_sol':round(float(receipt['realized_pnl_sol']),6),
        'wallet_exits':{},'scanned_day':''}
    exit_copy._validate_closes({'source':record})
    spec['record']=record
    return spec


def project_exit_copy(store):
    from . import exit_copy
    from pathlib import Path
    try:rows=store.journal_query(kind='scanner_close_committed',limit=-1)
    except Exception:
        logger.exception('exit-copy source unavailable');return
    for row in reversed(rows):
        try:
            e=row['detail'];context=e['context'];spec=e.get('exit_copy_spec')
            if not spec or spec.get('state')!='recorded':continue
            receipt=e['native_receipt'];identity=context['identity'];pos=context['position']
            if row['symbol']!=key(identity) or receipt['ticket']!=identity['ticket'] or receipt['symbol']!=pos['symbol']:
                raise ValueError('exit-copy identity conflict')
            allocations=receipt['allocations']
            if len(allocations)!=1 or allocations[0]['ticket']!=receipt['ticket']:raise ValueError('exit-copy allocation conflict')
            a=allocations[0];basis=float(a['basis_sol']);proceeds=float(a['proceeds_sol']);tokens=float(a['tokens'])
            price=float(receipt['price_sol']);pnl=float(receipt['realized_pnl_sol']);entry=float(a['entry_price_sol'])
            if (not all(math.isfinite(v) for v in (basis,proceeds,tokens,price,pnl,entry)) or min(basis,proceeds,tokens,price,entry)<0
                    or not math.isclose(proceeds,tokens*price,rel_tol=1e-10,abs_tol=1e-12)
                    or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
                raise ValueError('exit-copy economics conflict')
            if exit_copy_record(context,receipt)!=spec:raise ValueError('exit-copy record source conflict')
            path=spec.get('path')
            if not isinstance(path,str) or not path or not Path(path).is_absolute():raise ValueError('exit-copy pinned path invalid')
            effect=row['symbol']+':exit-copy-v1'
            if store.journal_query(kind='scanner_exit_copy_projected',symbol=effect,limit=1):continue
            exit_copy.append_close(path,spec['record'],effect)
            store.journal_add({'kind':'scanner_exit_copy_projected','symbol':effect,
                'detail':{'source_close':row['symbol'],'scope':'exit-copy-only'}})
        except Exception:
            _capture_projection_failure('exit_copy_projection_pending')
            logger.exception('exit-copy pending; economic exits continue')


def deployer_source(degen,state,pos,mint):
    from . import deployers
    if not getattr(degen,'deployer_tracking_enabled',True):return {'state':'disabled'}
    if (state.get('deployer_outcome_recorded') or {}).get(mint):return {'state':'recorded-legacy-skip'}
    deployer=pos.get('deployer') or ((state.get('deployer_cache') or {}).get(mint) or {}).get('deployer')
    if not isinstance(deployer,str) or not deployer:return {'state':'unavailable-deployer'}
    return {'state':'recorded','mint':mint,'deployer':deployer,'path':str(deployers._REGISTRY_FILE.absolute())}


def deployer_record(context,receipt):
    spec=copy.deepcopy(context.get('deployer_spec'))
    if not spec or spec.get('state')!='recorded':return spec
    a=receipt['allocations'][0];entry=float(a['entry_price_sol']);price=float(receipt['price_sol'])
    peak=float(context.get('peak') or entry)
    if not all(math.isfinite(v) and v>=0 for v in (entry,price,peak)):raise ValueError('deployer price source invalid')
    realized=price/entry if entry>0 else 1.;peak_mult=peak/entry if entry>0 else 1.
    spec['outcome']='rug' if realized<=.10 else 'hit_2x' if peak_mult>=2 else 'dead'
    spec['observed_at']=datetime.fromisoformat(receipt['ts']).timestamp()
    spec['source']=key(context['identity'])
    return spec


def project_deployers(store):
    from . import deployers
    from pathlib import Path
    try:rows=store.journal_query(kind='scanner_close_committed',limit=-1)
    except Exception:
        logger.exception('deployer source unavailable');return
    for row in reversed(rows):
        try:
            e=row['detail'];context=e['context'];spec=e.get('deployer_spec')
            if not spec or spec.get('state')!='recorded':continue
            receipt=e['native_receipt'];identity=context['identity'];pos=context['position']
            if row['symbol']!=key(identity) or receipt['ticket']!=identity['ticket'] or receipt['symbol']!=pos['symbol']:
                raise ValueError('deployer close identity conflict')
            if spec.get('mint')!=(pos.get('mint') or context['position_key']):raise ValueError('deployer mint conflict')
            allocations=receipt['allocations']
            if len(allocations)!=1 or allocations[0]['ticket']!=receipt['ticket']:raise ValueError('deployer allocation conflict')
            a=allocations[0];basis=float(a['basis_sol']);proceeds=float(a['proceeds_sol']);tokens=float(a['tokens'])
            price=float(receipt['price_sol']);pnl=float(receipt['realized_pnl_sol'])
            if (not all(math.isfinite(v) for v in (basis,proceeds,tokens,price,pnl)) or min(basis,proceeds,tokens,price)<0
                    or not math.isclose(proceeds,tokens*price,rel_tol=1e-10,abs_tol=1e-12)
                    or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
                raise ValueError('deployer economics conflict')
            if deployer_record(context,receipt)!=spec:raise ValueError('deployer pinned source conflict')
            path=spec.get('path')
            if not isinstance(path,str) or not path or not Path(path).is_absolute():raise ValueError('deployer path invalid')
            effect=row['symbol']+':deployer-v1'
            if store.journal_query(kind='scanner_deployer_projected',symbol=effect,limit=1):continue
            winner=deployers.claim_outcome(spec['mint'],spec['deployer'],spec['outcome'],spec['observed_at'],spec['source'],registry_path=path)
            store.journal_add({'kind':'scanner_deployer_projected','symbol':effect,
                'detail':{'source_close':row['symbol'],'scope':'deployer-only','winner':winner}})
        except Exception:
            _capture_projection_failure('deployer_projection_pending')
            logger.exception('deployer grade pending; economic exits continue')


def snapshot_series(scanner,degen,mint,now,since=None):
    """Strict recorded subset, never completeness or inferred zero values.

    Journal recovery needs an explicit episode start. Missing/truncated history
    remains a subset; malformed source is unavailable rather than repaired.
    """
    import json,math,os
    tracker=getattr(scanner,'_fp_tracker',None)
    path=getattr(degen,'fp_series_journal_path',None)
    if path and since is not None:
        if type(since) not in (int,float) or not math.isfinite(since) or not 0<since<=now:
            raise ValueError('snapshot episode unavailable')
        rows=[]
        try:
            with open(os.path.expanduser(path)) as f:
                for line in f:
                    rec=json.loads(line)
                    if not isinstance(rec,dict):raise ValueError('snapshot row invalid')
                    if rec.get('mint')!=mint:continue
                    obs=rec.get('obs')
                    if not isinstance(obs,list) or len(obs)!=8:raise ValueError('snapshot shape invalid')
                    if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in obs[:7]):
                        raise ValueError('snapshot numeric source invalid')
                    if any(v!=int(v) for v in obs[4:6]):raise ValueError('snapshot count invalid')
                    if since<=obs[0]<=now:rows.append(tuple(obs))
        except FileNotFoundError:return None
        return sorted(rows,key=lambda x:x[0]) or None
    rows=list((getattr(tracker,'_series',{}) or {}).get(mint) or []) if tracker is not None else []
    from .fingerprints import validate_recorded_series
    validate_recorded_series([list(o[:7])+[None] if isinstance(o,(list,tuple)) and len(o)==8 else o for o in rows],until=now)
    if since is not None:
        from .input_availability import availability
        if availability(since)!='recorded' or since>now:raise ValueError('snapshot episode unavailable')
        rows=[o for o in rows if o[0]>=since]
    return rows or None


def backtest_source(scanner,degen,state,mint,decision_ts):
    """Capture builder inputs independently; no current-state retry inference."""
    from pathlib import Path
    from daemon.launch_scanner import _skip_live_write
    if not getattr(degen,'backtest_enabled',True):return {'state':'disabled'}
    try:
        import os
        path=os.path.expanduser(str(getattr(degen,'backtest_dataset_path','~/.forex-agent/launch_scanner/backtest_dataset.jsonl')))
        if _skip_live_write(path):return {'state':'test-skipped'}
        if not Path(path).is_absolute():raise ValueError('backtest path unavailable')
        episode=((state.get('seen') or {}).get(mint) or {}).get('first_ts')
        snapshot_error=False
        try:series=snapshot_series(scanner,degen,mint,decision_ts,since=episode)
        except (ValueError,TypeError,OverflowError,OSError):
            series=None;snapshot_error=True
        snapshots=[]
        if series:
            for o in series:
                counts=[]
                for value in (o[4],o[5]):
                    if type(value) not in (int,float) or not math.isfinite(value) or value<0 or value!=int(value):
                        raise ValueError('backtest count unavailable')
                    counts.append(int(value))
                snapshots.append({'t':o[0],'mcap_usd':o[1],'price_usd':o[2],'vol5m_usd':o[3],
                    'buys5m':counts[0],'sells5m':counts[1],'liq_usd':o[6]})
        from .learning_effects import _validate
        _validate(snapshots)
        tags_map=state.get('narrative_tags')
        if tags_map is not None and not isinstance(tags_map,dict):raise ValueError('narrative source unavailable')
        tags=(tags_map or {}).get(mint,['other'])
        if not isinstance(tags,list) or not tags:raise ValueError('narrative tags unavailable')
        from .fingerprints import FingerprintTracker
        evidence_tracker=getattr(scanner,'_fp_tracker',None) or FingerprintTracker(degen)
        input_evidence=evidence_tracker.historical_inputs(mint,decision_ts,since=episode)
        from .input_availability import matches,capture
        if input_evidence.get('state')=='recorded' and not matches(mint,input_evidence,snapshots,until=decision_ts):
            snapshots=[];series=None;snapshot_error=True
            input_evidence=capture(mint,decision_ts,{'state':'source-snapshot-mismatch','rows':[]})
        result={'state':'recorded','path':path,'snapshots':copy.deepcopy(snapshots),
                'snapshots_state':'unavailable' if snapshot_error else 'recorded' if series else 'unrecorded',
                'input_evidence':input_evidence,'narrative_tags':copy.deepcopy(tags),
                'narrative_state':'recorded' if mint in (tags_map or {}) else 'unrecorded'}
        validate_backtest_source(result,decision_ts,mint)
        return result
    except Exception:
        logger.exception('backtest source unavailable before close')
        return {'state':'unavailable'}


def rebuild_backtest(evidence):
    """Retry only captured valid inputs; original native close supplies outcome."""
    from daemon.launch_scanner import LaunchScanner
    from types import SimpleNamespace
    context=evidence['context'];source=context.get('backtest_source')
    if not source or source.get('state')!='recorded':return None
    from pathlib import Path
    if not isinstance(source.get('path'),str) or not Path(source['path']).is_absolute():raise ValueError('backtest pinned path invalid')
    from .learning_effects import _validate
    validate_backtest_source(source,context['decision_ts'],context['position'].get('mint') or context['position_key'])
    receipt=evidence['native_receipt'];pos=context['position'];identity=context['identity']
    if receipt['ticket']!=identity['ticket'] or receipt['symbol']!=pos['symbol']:raise ValueError('backtest receipt identity conflict')
    a=receipt['allocations']
    if len(a)!=1 or a[0]['ticket']!=receipt['ticket']:raise ValueError('backtest allocation conflict')
    allocation=a[0];entry=float(allocation['entry_price_sol']);price=float(receipt['price_sol'])
    basis=float(allocation['basis_sol']);proceeds=float(allocation['proceeds_sol']);tokens=float(allocation['tokens']);pnl=float(receipt['realized_pnl_sol'])
    if (not all(math.isfinite(v) for v in (entry,price,basis,proceeds,tokens,pnl)) or min(entry,price,basis,proceeds,tokens)<0
            or not math.isclose(proceeds,tokens*price,rel_tol=1e-10,abs_tol=1e-12)
            or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
        raise ValueError('backtest native economics conflict')
    mint=pos.get('mint') or context['position_key'];position=copy.deepcopy(pos);position['peak_price_sol']=context['peak']
    position['entry_ts']=pos.get('entry_ts') or context['decision_ts']
    config=SimpleNamespace(degen=SimpleNamespace(backtest_enabled=True,backtest_dataset_path=source['path']))
    scanner=LaunchScanner.__new__(LaunchScanner)
    spec=scanner._backtest_record_paper_close(config,{'narrative_tags':{mint:source['narrative_tags']}},mint,pos['symbol'],
        position,entry,price,context['reason'],pnl,datetime.fromisoformat(receipt['ts']).timestamp(),record_only=True,
        snapshots_override=source['snapshots'])
    if not isinstance(spec,dict):return None
    spec['record']['outcome_id']=key(identity)
    spec['record']['snapshot_input_evidence']=copy.deepcopy(source.get('input_evidence',{'state':'legacy-unrecorded','rows':[]}))
    spec['record']['snapshot_recorded_t']=context['decision_ts']
    spec['record']['native_allocation']=native_history_economics(receipt)
    spec['record']['native_allocation_state']='recorded'
    spec['record']['realized_mult_metric']='close-price-over-entry-price-not-net-return'
    spec['record']['recovery_source_provenance']={'snapshots':source['snapshots_state'],'narrative_tags':source['narrative_state']}
    return spec


def validate_backtest_source(source,decision_ts,mint=None):
    from .learning_effects import _validate
    _validate(source)
    if (set(source)-{'input_evidence'}!={'state','path','snapshots','snapshots_state','narrative_tags','narrative_state'}
            or source['snapshots_state'] not in ('recorded','unrecorded','unavailable') or source['narrative_state'] not in ('recorded','unrecorded')
            or not isinstance(source['narrative_tags'],list) or not source['narrative_tags'] or any(not isinstance(t,str) or not t for t in source['narrative_tags'])
            or not isinstance(source['snapshots'],list) or source['snapshots_state'] in ('unrecorded','unavailable') and source['snapshots']):
        raise ValueError('backtest pinned inputs invalid')
    evidence=source.get('input_evidence')
    if isinstance(evidence,dict) and evidence.get('state')=='recorded':
        from .input_availability import matches
        if not matches(mint,evidence,source['snapshots'],until=decision_ts):
            raise ValueError('backtest input envelope conflict')
    previous=None
    for snapshot in source['snapshots']:
        if not isinstance(snapshot,dict) or set(snapshot)!={'t','mcap_usd','price_usd','vol5m_usd','buys5m','sells5m','liq_usd'}:raise ValueError('backtest snapshot shape invalid')
        if any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in snapshot.values()):raise ValueError('backtest snapshot values invalid')
        if snapshot['t']<=0 or previous is not None and snapshot['t']<previous or snapshot['t']>decision_ts:raise ValueError('backtest snapshot chronology invalid')
        if any(type(snapshot[k]) is not int for k in ('buys5m','sells5m')):raise ValueError('backtest snapshot counts invalid')
        previous=snapshot['t']
    return source


def _capture_projection_failure(code):
    try:
        from daemon.failure_reports import capture_local
        return capture_local('scanner.learning',code,sys.exc_info()[1],{'operation':'persist'})
    except Exception:
        return None
