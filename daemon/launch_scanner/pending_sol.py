"""Account-owned native sale proceeds awaiting USD settlement.

A sale removes token units and carries their original USD basis into frozen SOL
proceeds. No cash or realized USD PnL is invented before dated FX settlement.
Caller pins operation payload before effects. Lock order: native -> account;
account code never acquires a native lock or calls market data under its lock.
"""
import copy
import math
import time
from broker.memecoin.shared_capital import validate_fx, CapitalConflict

EVENTS = ('pending_sol', 'settle_sol')


def _number(value, positive=False):
    return (isinstance(value,(int,float)) and not isinstance(value,bool)
            and math.isfinite(value) and (value>0 if positive else value>=0))


def apply(led, event, pa):
    """Replay exact economic mutation. None on success, reason on conflict."""
    op, kind = event['op_id'], event['event']
    params = {k:v for k,v in event.items() if k not in ('seq','event_id','ts')}
    prior = led.setdefault('ops',{}).get(op)
    if prior is not None:
        return None if prior == params else 'pending SOL operation identity conflict'
    lane, mint, pid = event['lane'],event['mint'],event['pos_id']
    pending = led.setdefault('pending_sol',{})
    if kind=='pending_sol':
        qty, proceeds = event['qty'],event['proceeds_sol']
        if not _number(qty,True) or not _number(proceeds):
            return 'invalid pending SOL economics'
        st=pa._lane(led,lane)
        key=pa._position_key(st,mint,pid)
        pos=st['open'].get(key)
        if (not isinstance(pos,dict) or pos.get('instance') is not True
                or pos.get('mint')!=mint or pos.get('pos_id')!=pid):
            return 'pending SOL requires exact open instance'
        have=pa._pos_qty(pos)
        if have is None or qty>have+pa._EPS:
            return 'pending SOL quantity exceeds lot'
        basis=pa._pos_basis(pos)
        final=qty>=have-pa._EPS
        allocated=basis if final else min(basis,round(basis*qty/have,6))
        if final:
            del st['open'][key]
        else:
            pos['qty']=round(have-qty,12)
            pos['basis']=round(basis-allocated,6)
        pending[op]={'pending_id':op,'lane':lane,'mint':mint,'pos_id':pid,
                     'ticket':event['ticket'],'qty':qty,'proceeds_sol':proceeds,
                     'basis':allocated,'native_basis_sol':event['native_basis_sol'],
                     'sold_at':event['sold_at'],'final_lot':final,
                     'status':'sold_pending_usd_fx','fee_sol':event['fee_sol']}
        params['basis']=allocated
    elif kind=='settle_sol':
        item=pending.get(event['pending_id'])
        if (item is None or any(item.get(k)!=event.get(k) for k in ('lane','mint','pos_id'))):
            return 'pending SOL settlement ownership conflict'
        evidence=event['fx']
        if (not isinstance(evidence,dict) or not _number(evidence.get('usd_per_sol'),True)
                or not _number(evidence.get('observed_at'))
                or not isinstance(evidence.get('source'),str) or not evidence['source']
                or evidence['observed_at']<item['sold_at']):
            return 'invalid or pre-sale settlement FX'
        net=round(item['proceeds_sol']*evidence['usd_per_sol'],6)
        if event['proceeds']!=net:
            return 'pending SOL settlement amount conflict'
        realized=round(net-item['basis'],6)
        led['cash']=round(led['cash']+net,6)
        led['cash_spendable']=round(led['cash_spendable']+net,6)
        led['realized_pnl']=round(led['realized_pnl']+realized,6)
        led['day_realized_pnl']=round(led.get('day_realized_pnl',0.)+realized,6)
        st=pa._lane(led,lane)
        st['realized_pnl']=round(st['realized_pnl']+realized,6)
        if item['final_lot']:
            st['n_closes']+=1
        params.update(basis=item['basis'],realized=realized)
        del pending[event['pending_id']]
    else:
        return 'unsupported pending SOL event'
    led['ops'][op]=params
    return None


def _execute(pa, event):
    with pa._lock():
        led,healthy,degraded=pa._load_locked()
        if not healthy or led is None or degraded:
            return False,degraded or 'account ledger unreadable'
        old=led.get('ops',{}).get(event['op_id'])
        if old is not None:
            expected={k:v for k,v in event.items() if k not in ('seq','event_id','ts')}
            if any(old.get(k)!=v for k,v in expected.items()):
                return False,'pending SOL retry parameter conflict'
            return True,'already applied'
        # Freshness is checked only for a NEW admission. Replay uses the
        # pinned observation, never re-ages already accepted evidence.
        if event['event']=='settle_sol':
            try:
                validate_fx(event.get('fx'))
            except CapitalConflict as exc:
                return False,str(exc)
        # Validate on a copy before WAL or economic projection.
        planned=copy.deepcopy(led)
        reason=apply(planned,event,pa)
        if reason:
            return False,reason
        seq=int(led.get('seq') or 0)+1
        payload=dict(event,seq=seq,event_id='ev#%d'%seq)
        pa._journal(payload)
        planned['seq']=seq
        pa._save_locked(planned)
        return True,'ok'


def reserve(pa,lane,mint,pos_id,qty,proceeds_sol,ticket,native_basis_sol,
            sold_at,op_id,fee_sol=0.):
    if (not all(isinstance(v,str) and v for v in (lane,mint,pos_id,op_id))
            or not _number(sold_at) or sold_at > time.time() or not _number(native_basis_sol)
            or not _number(fee_sol) or type(ticket) is not int or ticket<=0):
        return False,'invalid pending SOL payload'
    return _execute(pa,{'event':'pending_sol','instance':True,'lane':lane,'mint':mint,
                        'pos_id':pos_id,'qty':qty,'proceeds_sol':proceeds_sol,
                        'ticket':ticket,'native_basis_sol':native_basis_sol,
                        'sold_at':sold_at,'fee_sol':fee_sol,'op_id':op_id})


def settle(pa,lane,mint,pos_id,pending_id,fx,proceeds,op_id):
    return _execute(pa,{'event':'settle_sol','instance':True,'lane':lane,'mint':mint,
                        'pos_id':pos_id,'pending_id':pending_id,'fx':fx,
                        'proceeds':proceeds,'op_id':op_id})
