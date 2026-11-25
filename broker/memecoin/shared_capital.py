"""Pinned USD operations for native SOL lots. Offline building block only.

No activation switch or runtime caller yet. The caller must durably persist
entry/exit intent BEFORE invoking these effects, and hold its native state lock.
No compensating cash or uncertain-commit rollback is permitted.
"""
import math
import time


class CapitalConflict(ValueError):
    pass


def validate_fx(evidence, now=None, max_age=60.):
    """Strict observed quote. Never refresh a cached timestamp or invent source."""
    now = time.time() if now is None else now
    if not isinstance(evidence, dict):
        raise CapitalConflict('missing SOL/USD evidence')
    px, observed, source = (evidence.get('usd_per_sol'),
                            evidence.get('observed_at'), evidence.get('source'))
    if any(isinstance(v, bool) or not isinstance(v, (int,float))
           or not math.isfinite(v) for v in (px,observed)):
        raise CapitalConflict('invalid SOL/USD evidence')
    if px <= 0 or not 0 <= now-observed <= max_age or not isinstance(source,str) or not source:
        raise CapitalConflict('unavailable or stale SOL/USD evidence')
    return dict(evidence)


def entry_plan(lane, mint, ticket, tokens, cost_sol, fx, op_id, now=None,
               hold_op_id=None):
    fx = validate_fx(fx, now)
    if (type(ticket) is not int or ticket <= 0 or not lane or not mint or not op_id
            or any(isinstance(v,bool) or not isinstance(v,(int,float))
                   or not math.isfinite(v) or v <= 0 for v in (tokens,cost_sol))):
        raise CapitalConflict('invalid native entry economics')
    stake = round(cost_sol*fx['usd_per_sol'],6)
    if stake <= 0:
        raise CapitalConflict('native entry below shared currency precision')
    return {'kind':'entry','lane':lane,'mint':mint,'ticket':ticket,
            'qty':tokens,'cost_sol':cost_sol,'stake':stake,'fx':fx,'op_id':op_id,
            **({'hold_op_id': hold_op_id} if hold_op_id else {})}


def apply_entry(plan, account):
    """Return exact shared receipt, or explicit refusal with no native admission.

    Do not re-validate FX age on recovery: it is already pinned evidence of the
    original entry price. Requoting would silently change the operation.
    """
    expected = {'event':'debit','lane':plan['lane'],'mint':plan['mint'],
                'stake':plan['stake'],'qty':plan['qty'],'instance':True}
    rec, error = account.operation_receipt(plan['op_id'])
    if error:
        raise CapitalConflict(error)
    if rec is not None and rec.get('event') == 'debit_refused':
        expected_refusal = dict(expected,event='debit_refused')
        if any(rec.get(k)!=v for k,v in expected_refusal.items()):
            raise CapitalConflict('refused entry receipt parameter conflict')
        return None, rec['reason']
    if rec is not None and plan.get('hold_op_id'):
        # Entry already applied: finish a crash-split consume (the
        # hold's remainder may still be reserved). Idempotent no-op
        # when the pair completed.
        ok2, why2, _ = account.consume_hold_for_entry(
            plan['hold_op_id'],plan['lane'],plan['mint'],
            plan['stake'],plan['qty'],plan['op_id'])
        if not ok2:
            raise CapitalConflict(why2)
    if rec is None:
        if plan.get('hold_op_id'):
            # Seal-time admission: the hold already passed every gate and
            # reserved the cash account-wide. Adoption recovers recorded
            # truth; it is never a fresh admission that could be refused.
            ok, why, _ = account.consume_hold_for_entry(
                plan['hold_op_id'],plan['lane'],plan['mint'],
                plan['stake'],plan['qty'],plan['op_id'])
        else:
            ok, why, _ = account.try_debit_instance(plan['lane'],plan['mint'],
                                                   plan['stake'],plan['qty'],plan['op_id'])
        if not ok:
            # Failed fsync can throw, never masquerade as a refusal. A plain
            # refusal is rechecked against the authoritative durable outcome.
            rec,error = account.operation_receipt(plan['op_id'])
            if error:
                raise CapitalConflict(error)
            if rec is None:
                return None, why
        else:
            rec,error = account.operation_receipt(plan['op_id'])
            if error:
                raise CapitalConflict(error)
    if isinstance(rec,dict) and rec.get('event')=='debit_refused':
        return None, rec['reason']
    if (not isinstance(rec,dict) or not rec.get('pos_id')
            or any(rec.get(k)!=v for k,v in expected.items())):
        raise CapitalConflict('shared entry receipt conflicts with native intent')
    return rec, None


def exit_plan(lane, mint, allocation, pos_id, fx, op_id, now=None):
    """One native lot allocation, independently settled by its exact shared ID.

    fx=None means a priced native risk-reducing exit with USD release pending.
    Its native proceeds and basis remain pinned; there is no fabricated cash.
    """
    qty, proceeds = allocation['tokens'], allocation['proceeds_sol']
    if (not pos_id or not op_id or not lane or not mint
            or type(allocation.get('ticket')) is not int or allocation['ticket'] <= 0
            or not isinstance(allocation.get('basis_sol'), (int,float))
            or isinstance(allocation['basis_sol'],bool)
            or not math.isfinite(allocation['basis_sol']) or allocation['basis_sol'] < 0
            or any(isinstance(v,bool) or not isinstance(v,(int,float))
                or not math.isfinite(v) or v < 0 for v in (qty,proceeds)) or qty<=0):
        raise CapitalConflict('invalid native exit allocation')
    evidence = validate_fx(fx,now) if fx is not None else None
    return {'kind':'exit','lane':lane,'mint':mint,'pos_id':pos_id,
            'ticket':allocation['ticket'],'qty':qty,
            'basis_sol':allocation['basis_sol'],'proceeds_sol':proceeds,
            'fx':evidence,'proceeds':round(proceeds*evidence['usd_per_sol'],6)
            if evidence else None, 'op_id':op_id}


def apply_exit(plan, account):
    """One pinned release. A multi-lot exit needs a durable enclosing intent.

    Partial success is not rolled back. Replay each stored release in order,
    keeping the enclosing intent until every exact receipt is verified.
    """
    if plan['fx'] is None or plan['proceeds'] is None:
        return None, 'USD settlement pending FX'
    kind = 'current_price_simulated_fill'
    expected = {'event':'credit','lane':plan['lane'],'mint':plan['mint'],
                'pos_id':plan['pos_id'],'qty':plan['qty'],'proceeds':plan['proceeds'],
                'fee':0.,'price_kind':kind,'instance':True}
    rec,error = account.operation_receipt(plan['op_id'])
    if error:
        raise CapitalConflict(error)
    if rec is None:
        ok,why = account.credit(plan['lane'],plan['mint'],plan['proceeds'],plan['op_id'],
                                 pos_id=plan['pos_id'],qty=plan['qty'],fee=0.,price_kind=kind,
                                 expected_account_id=plan.get('expected_account_id'),
                                 entry_op_id=plan.get('entry_op_id'))
        rec,error = account.operation_receipt(plan['op_id'])
        if error:
            raise CapitalConflict(error)
        if rec is None:
            raise CapitalConflict('shared exit not applied: '+why)
    if any(rec.get(k)!=v for k,v in expected.items()):
        raise CapitalConflict('shared exit receipt conflicts with native intent')
    return rec,None
