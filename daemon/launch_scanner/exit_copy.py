"""Exit-copying — MEASUREMENT ONLY (safe batch 2026-10-09).

For coins the bot tracks, record when a curated smart wallet EXITS vs
when our paper position exited. The weekly review answers: "would
copying their exits have helped?" — with exit ORDERING and our own
realized pnl. Counterfactual profits are never fabricated: we report
who left first and what we actually made, nothing more.

How it works:
  - record_close(...): at close time, stores our exit (mint, entry/
    close ts, close multiple, reason, pnl). One additive call at the
    existing close site.
  - daily_pass(state, degen): once per WAT calendar day, for recent
    closes lacking wallet-exit data, checks the wallets that BOUGHT
    this mint (state["smart_activity"][mint]) for sells after our
    entry. Helius calls go through the shared helius_ratelimit guard,
    are budget-capped (default 40 tx parses/day), and fail soft.

NO BEHAVIOR CHANGE: nothing reads these records except the weekly
review. There is no auto-copying, no signal, no gate — the module
cannot influence entries, exits, or alerts.

Dataset: runtime state ``<home>/launch_scanner/exit_copy.json``
(0600, locked atomic effect writes), bounded (500 closes), 7-day source
retention watermark. Exact receipts retained separately and unbounded. Reads
apply current-wallclock seven-day filtering. New closes have distinct identities
and mint fields; legacy mint-keyed rows remain readable. Old wallet prefixes
are preserved without inventing missing full addresses.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

# Transaction-outcome guards (2026-11-17, Batch 3): meta.err
# gating (tx_guards.py). A reverted tx's balances never moved —
# its token delta is fiction, never a sale.
try:
    import tx_guards as _txg
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import tx_guards as _txg
    except Exception:
        raise

logger = logging.getLogger("exit_copy")

WAT = timezone(timedelta(hours=1))

_SUBDIR = "launch_scanner"
_FILENAME = "exit_copy.json"
_TTL_S = 7 * 24 * 3600
_MAX_CLOSES = 500
_SIG_LIMIT = 20
_TIMEOUT = 12.0

_STATE_DAY_KEY = "exit_copy_last_pass_day"


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def _resolve_path(path: Optional[str]) -> Optional[str]:
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        from daemon.common import get_home  # noqa: PLC0415
        return os.path.join(get_home(), _SUBDIR, _FILENAME)
    except Exception:
        logger.exception("exit_copy: could not resolve home")
        return None


def _validate_closes(closes):
    import math
    from . import learning_effects as effects
    effects._validate(closes)
    if not isinstance(closes,dict):raise ValueError('exit-copy store unavailable')
    for identity,rec in closes.items():
        if not identity or not isinstance(rec,dict):raise ValueError('exit-copy close invalid')
        if any(type(rec.get(k)) not in (int,float) or not math.isfinite(rec[k]) for k in ('entry_ts','close_ts','close_mult','pnl_sol')):
            raise ValueError('exit-copy economics invalid')
        if rec['close_mult']<0 or rec['close_ts']<rec['entry_ts'] or not isinstance(rec.get('reason'),str):raise ValueError('exit-copy close source invalid')
        if not isinstance(rec.get('wallet_exits'),dict) or not isinstance(rec.get('scanned_day'),str):raise ValueError('exit-copy enrichment invalid')
        day=rec['scanned_day']
        if day:
            from datetime import date
            if date.fromisoformat(day).isoformat()!=day:raise ValueError('exit-copy scan day invalid')
        if 'mint' in rec and (not isinstance(rec['mint'],str) or not rec['mint']):raise ValueError('exit-copy mint invalid')
        for wallet,info in rec['wallet_exits'].items():
            if (not wallet or not isinstance(info,dict) or any(type(info.get(k)) not in (int,float) or not math.isfinite(info[k]) for k in ('sold_ts','after_our_close_h'))):
                raise ValueError('exit-copy wallet evidence invalid')
            if 'complete' in info and type(info['complete']) is not bool:raise ValueError('exit-copy wallet completeness invalid')
            if info['sold_ts']<rec['entry_ts'] or info['after_our_close_h']!=round((info['sold_ts']-rec['close_ts'])/3600.,2):
                raise ValueError('exit-copy wallet timing conflict')
    return closes


def _all_closes(path):
    from . import learning_effects as effects
    p=_resolve_path(path)
    if not p:return {}
    try:
        with open(p,'r',encoding='utf-8') as fh:doc=json.load(fh,object_pairs_hook=effects._pairs,parse_constant=effects._constant)
    except FileNotFoundError:return {}
    if isinstance(doc,dict) and set(doc)=={'version','value','effects'}:
        if type(doc['version']) is not int or doc['version']!=1 or not isinstance(doc['effects'],dict) or any(not isinstance(k,str) or not k or not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for k,v in doc['effects'].items()):
            raise ValueError('exit-copy receipts invalid')
        doc=doc['value']
    return _validate_closes(doc)


def _load(path):
    cutoff=time.time()-_TTL_S
    return {identity:rec for identity,rec in _all_closes(path).items() if rec['close_ts']>=cutoff}


def _immutable(rec):
    return {k:v for k,v in rec.items() if k not in ('wallet_exits','scanned_day')}


def append_close(path,record,effect_id):
    from . import learning_effects as effects
    from pathlib import Path
    p=Path(path)
    if not p.is_absolute():raise ValueError('exit-copy path must be absolute')
    _validate_closes({effect_id:record});p.parent.mkdir(parents=True,exist_ok=True)
    def reducer(state,payload):
        _validate_closes(state)
        if effect_id in state and state[effect_id]!=payload:raise ValueError('exit-copy close identity conflict')
        state[effect_id]=payload
        watermark=max(r['close_ts'] for r in state.values())
        rows=[(k,r) for k,r in state.items() if r['close_ts']>=watermark-_TTL_S]
        rows.sort(key=lambda pair:(pair[1]['close_ts'],pair[0]))
        return _validate_closes(dict(rows[-_MAX_CLOSES:]))
    return effects.apply(p,effect_id,record,reducer,{},legacy_import=_validate_closes)


def merge_enrichment(path,identity,original,exits,day):
    """Merge only this source close's patch into the current locked store."""
    from . import learning_effects as effects
    import hashlib
    from pathlib import Path
    p=Path(path)
    if not p.is_absolute():raise ValueError('exit-copy path must be absolute')
    payload={'identity':identity,'source':_immutable(original),'wallet_exits':exits,'scanned_day':day}
    _validate_closes({identity:dict(payload['source'],wallet_exits=exits,scanned_day=day)})
    effect_id='enrichment-'+hashlib.sha256(effects._encoded(payload).encode()).hexdigest()
    def reducer(state,payload):
        _validate_closes(state)
        current=state.get(identity)
        if current is None or _immutable(current)!=payload['source']:raise ValueError('exit-copy enrichment stale or missing close')
        for wallet,info in payload['wallet_exits'].items():
            prior=current['wallet_exits'].get(wallet)
            if prior is not None and prior!=info:
                # An incomplete bounded search can improve to an earlier sale;
                # a complete result cannot silently regress or contradict.
                if prior.get('complete') is True or info['sold_ts']>prior['sold_ts']:
                    raise ValueError('exit-copy wallet evidence conflict')
            current['wallet_exits'][wallet]=info
        current['scanned_day']=max(current['scanned_day'],payload['scanned_day'])
        return _validate_closes(state)
    return effects.apply(p,effect_id,payload,reducer,{},legacy_import=_validate_closes,require_existing=True)


def _rpc(method: str, params: List[Any]) -> Any:
    """Routine exit-history investigation follows central section 9 routing."""
    from daemon.intelligence.acquisition.routine import read
    return read(method, params, timeout=_TIMEOUT, background=True)



def _token_delta(tx: Dict[str, Any], wallet: str,
                 mint: str) -> float:
    """Net token-balance change for (wallet, mint) in one transaction.
    Negative = the wallet sold (partially or fully)."""
    try:
        meta = tx.get("meta") or {}

        def _amts(key: str) -> Dict[Tuple[str, str], float]:
            out: Dict[Tuple[str, str], float] = {}
            for b in meta.get(key) or []:
                if not isinstance(b, dict):
                    continue
                amt = _f((b.get("uiTokenAmount") or {}).get("uiAmount"))
                out[(str(b.get("owner")), str(b.get("mint")))] = amt
            return out

        pre = _amts("preTokenBalances")
        post = _amts("postTokenBalances")
        k = (str(wallet), str(mint))
        return post.get(k, 0.0) - pre.get(k, 0.0)
    except Exception:
        return 0.0


def _wallet_sold_after(wallet: str, mint: str, after_ts: float,
                       budget: List[int], *, detailed=False, reserve=None, progress_path=None):
    """Earliest blockTime at/after after_ts where the wallet's balance
    of mint decreased. None when no sell found (or on any failure).
    budget[0] caps getTransaction parses; decremented per parse."""
    if progress_path is not None:
        return _wallet_progress(wallet,mint,after_ts,budget,reserve,progress_path)
    try:
        if reserve is not None:reserve()
        sigs = _rpc("getSignaturesForAddress",[wallet, {"limit": _SIG_LIMIT}])
        if not isinstance(sigs,list):raise ValueError('wallet signature history unavailable')
        covered=len(sigs)<_SIG_LIMIT
        complete=True
        earliest: Optional[float] = None
        for s in sigs:
            if not isinstance(s, dict):
                complete=False;continue
            bt = _f(s.get("blockTime"))
            sig = s.get("signature")
            if not sig or bt<=0:
                complete=False;continue
            if bt < after_ts:
                covered=True;continue
            if budget[0] <= 0:
                complete=False;break
            if reserve is not None:reserve()
            budget[0] -= 1
            tx = _rpc("getTransaction",
                      [sig, {"encoding": "jsonParsed",
                             "maxSupportedTransactionVersion": 0}]) or {}
            # meta.err gate (2026-11-17, Batch 3): a reverted
            # tx's balances never moved — skip it, don't count
            # its delta as a sale.
            if not _txg.tx_succeeded(tx):
                if _txg.tx_reverted_reason(tx)=='tx_unknown':complete=False
                continue
            if _token_delta(tx, wallet, mint) < 0:
                if earliest is None or bt < earliest:
                    earliest = bt
            time.sleep(0.15)
        return {'sold_ts':earliest,'complete':complete and covered} if detailed else earliest
    except Exception:
        logger.exception("exit_copy wallet scan failed for %s", wallet[:8])
        return {'sold_ts':None,'complete':False} if detailed else None


def record_close(degen: Any, mint: str, entry_ts: float, close_ts: float,
                 close_mult: float, reason: str, pnl_sol: float,
                 path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Store our exit for later wallet-exit comparison.

    Measurement only. Returns the record, or None when disabled.
    Never raises.
    """
    try:
        if not getattr(degen, "exit_copy_enabled", True):
            return None
        if not mint:
            return None
        mint = str(mint)
        import uuid
        p=_resolve_path(path)
        if not p:return None
        identity='exit-copy-'+uuid.uuid4().hex
        rec={
            'mint':mint,'close_ts':float(close_ts),'entry_ts':float(entry_ts),
            'close_mult':round(float(close_mult),4),'reason':str(reason or ''),
            'pnl_sol':round(float(pnl_sol),6),'wallet_exits':{},'scanned_day':''}
        append_close(p,rec,identity)
        return rec
    except Exception:
        logger.exception("exit_copy record_close failed for %s", mint)
        return None


def daily_pass(state: Dict[str, Any], degen: Any,
               now: Optional[float] = None,
               path: Optional[str] = None) -> int:
    """Once per WAT calendar day: look for smart-wallet exits on our
    recent closes. Returns closes enriched. Never raises.

    Only wallets that BOUGHT the mint (state["smart_activity"][mint])
    are checked — bounded, throttled, fail-soft.
    """
    try:
        if not getattr(degen, "exit_copy_enabled", True):
            return 0
        now = time.time() if now is None else float(now)
        day = datetime.fromtimestamp(now, WAT).date().isoformat()
        if state.get(_STATE_DAY_KEY) == day:
            return 0
        max_tx = int(getattr(degen, "exit_copy_max_tx_per_day", 40) or 40)
        budget = [max_tx]
        closes = _load(path)
        if not closes:
            state[_STATE_DAY_KEY] = day
            return 0
        activity = state.get("smart_activity") or {}
        enriched = 0
        all_complete=True
        def reserve():
            reserve_daily_call(_resolve_path(path),day,max_tx)
        for identity, rec in closes.items():
            mint=rec.get('mint') or identity
            if not isinstance(rec, dict):
                continue
            if rec.get("scanned_day") == day:
                continue
            buyers = activity.get(mint) or []
            if not buyers:
                merge_enrichment(_resolve_path(path),identity,rec,{},day)
                continue
            entry_ts = _f(rec.get("entry_ts"))
            complete=True
            exits = dict(rec.get("wallet_exits") or {})
            for wallet in buyers:
                if not isinstance(wallet,str) or not wallet:
                    complete=False;continue
                if (exits.get(wallet) or {}).get('complete') is True:
                    continue
                if budget[0] <= 0:
                    complete=False;break
                result = _wallet_sold_after(wallet,mint,entry_ts,budget,detailed=True,reserve=reserve,progress_path=_resolve_path(path))
                sold_ts=result['sold_ts']
                complete=complete and result['complete']
                if not sold_ts and wallet in exits:
                    # A newly empty history contradicts retained positive
                    # evidence; do not upgrade it to a complete negative.
                    complete=False
                if sold_ts:
                    exits[wallet] = {"sold_ts": sold_ts,"complete":result['complete'],
                                          "after_our_close_h": round(
                                              (sold_ts - _f(rec.get("close_ts")))
                                              / 3600.0, 2)}
            merge_enrichment(_resolve_path(path),identity,rec,exits,day if complete else rec['scanned_day'])
            if complete:enriched += 1
            else:all_complete=False
        if all_complete:state[_STATE_DAY_KEY] = day
        if enriched:
            logger.warning("exit_copy daily pass: %d closes scanned, "
                           "%d tx budget left", enriched, budget[0])
        return enriched
    except Exception:
        logger.exception("exit_copy daily_pass failed")
        return 0


def weekly_section(path: Optional[str] = None) -> List[str]:
    """Weekly review: who exited first — us or the smart wallets?

    Honest measurement: ordering + our realized pnl. No fabricated
    counterfactual profits.
    """
    out = ["== EXIT-COPYING (last 7 days) =="]
    try:
        closes = _load(path)
    except Exception:
        out.append("exit-copy data unavailable (read failed).")
        return out
    with_exits = [(m, r) for m, r in closes.items()
                  if isinstance(r, dict) and r.get("wallet_exits")]
    if not with_exits:
        out.append("no smart-wallet exits observed on our closes yet.")
        return out
    n = len(with_exits)
    earlier = later = 0
    deltas: List[float] = []
    pnls: List[float] = []
    for _m, r in with_exits:
        exits = r.get("wallet_exits") or {}
        ours = _f(r.get("close_ts"))
        first = min((_f(e.get("sold_ts")) for e in exits.values()
                     if isinstance(e, dict) and _f(e.get("sold_ts")) > 0),
                    default=0.0)
        if first <= 0:
            continue
        dh = (first - ours) / 3600.0
        deltas.append(dh)
        pnls.append(_f(r.get("pnl_sol")))
        if dh < 0:
            earlier += 1
        else:
            later += 1
    out.append("closes with a tracked wallet exit: %d" % n)
    if deltas:
        out.append("wallet exited BEFORE us: %d | AFTER us: %d"
                   % (earlier, later))
        out.append("avg wallet-exit delta: %+.1fh (negative = they left first)"
                   % (sum(deltas) / len(deltas)))
        out.append("our avg pnl on those closes: %+.6f SOL"
                   % (sum(pnls) / len(pnls)))
        out.append("reading: when wallets leave first and we still "
                   "profit, our exits are fine; when they leave first "
                   "and we round-trip, their exits were the tell.")
    return out


def reserve_daily_call(path,day,limit):
    """Durably reserve before every investigation request, even failed calls.

    Uses the configured daily transaction limit as a conservative total RPC
    request ceiling, including signature lookup. Retry cannot reset this file.
    """
    import uuid
    from pathlib import Path
    from . import learning_effects as effects
    p=Path(path).absolute().with_name(Path(path).name+'.budget.json')
    p.parent.mkdir(parents=True,exist_ok=True)
    def reducer(state,payload):
        if not isinstance(state,dict) or any(not isinstance(k,str) or type(v) is not int or v<0 for k,v in state.items()):raise ValueError('exit-copy budget unavailable')
        used=state.get(day,0)
        if used>=limit:raise ValueError('exit-copy daily request budget exhausted')
        state[day]=used+1
        return state
    effects.apply(p,'request-'+uuid.uuid4().hex,{'day':day,'limit':limit},reducer,{})


def _wallet_progress(wallet,mint,after_ts,budget,reserve,path):
    """Durable bounded signature queue and pagination, scoped to source entry.

    Progress never claims a negative on missing tx/status/balances. A failed
    parse stays at queue head; successful/reverted work advances across days.
    """
    from . import learning_effects as effects
    from pathlib import Path
    import hashlib,uuid,copy,math
    p=Path(path).absolute().with_name(Path(path).name+'.progress.json')
    identity=hashlib.sha256(effects._encoded([wallet,mint,after_ts]).encode()).hexdigest()
    initial={'before':None,'queue':[],'done':False,'earliest':None,'completed_day':''}
    def validate_state(state,boundary=after_ts):
        from datetime import date
        if (not isinstance(state,dict) or set(state)!=set(initial) or type(state['done']) is not bool
                or not isinstance(state['queue'],list) or len(state['queue'])>_SIG_LIMIT
                or state['before'] is not None and (not isinstance(state['before'],str) or not state['before'])
                or not isinstance(state['completed_day'],str)):
            raise ValueError('exit-copy progress invalid')
        if state['completed_day']:
            if date.fromisoformat(state['completed_day']).isoformat()!=state['completed_day'] or not state['done'] or state['queue']:
                raise ValueError('exit-copy progress completion invalid')
        if state['earliest'] is not None and (type(state['earliest']) not in (int,float) or not math.isfinite(state['earliest']) or state['earliest']<boundary):
            raise ValueError('exit-copy progress earliest invalid')
        seen=set()
        for item in state['queue']:
            if (not isinstance(item,dict) or set(item)!={'signature','blockTime'} or not isinstance(item['signature'],str) or not item['signature']
                    or item['signature'] in seen or type(item['blockTime']) not in (int,float) or not math.isfinite(item['blockTime']) or item['blockTime']<boundary):
                raise ValueError('exit-copy progress queue invalid')
            seen.add(item['signature'])
        return state
    def read():
        try:doc=json.loads(p.read_text(),object_pairs_hook=effects._pairs,parse_constant=effects._constant)
        except FileNotFoundError:return copy.deepcopy(initial)
        if (not isinstance(doc,dict) or set(doc)!={'version','value','effects'} or type(doc['version']) is not int or doc['version']!=1
                or not isinstance(doc['value'],dict) or not isinstance(doc['effects'],dict)
                or any(not isinstance(k,str) or not k or not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for k,v in doc['effects'].items())):
            raise ValueError('exit-copy progress unavailable')
        # Every cached row has its source tuple, not merely an opaque key.
        for source_id,row in doc['value'].items():
            if not isinstance(row,dict) or set(row)!={'source','state'}:raise ValueError('exit-copy progress source unavailable')
            source=row['source']
            if (not isinstance(source,list) or len(source)!=3 or not all(isinstance(v,str) and v for v in source[:2])
                    or type(source[2]) not in (int,float) or not math.isfinite(source[2])
                    or hashlib.sha256(effects._encoded(source).encode()).hexdigest()!=source_id):raise ValueError('exit-copy progress source conflict')
            # Validate each row against its own entry boundary.
            validate_state(row['state'],source[2])
        row=doc['value'].get(identity)
        return copy.deepcopy(row['state'] if row else initial)
    def save(old,new):
        def reducer(state,payload):
            expected={'source':[wallet,mint,after_ts],'state':payload['old']}
            if not isinstance(state,dict) or state.get(identity,{'source':[wallet,mint,after_ts],'state':initial})!=expected:raise ValueError('exit-copy concurrent progress changed')
            validate_state(payload['new'])
            state[identity]={'source':[wallet,mint,after_ts],'state':payload['new']};return state
        effects.apply(p,'progress-'+uuid.uuid4().hex,{'old':old,'new':new},reducer,{})
    state=None
    try:
        state=read()
        day=datetime.fromtimestamp(time.time(),WAT).date().isoformat()
        if state['done'] and not state['queue'] and state['earliest'] is None and state['completed_day']!=day:
            new=copy.deepcopy(initial);save(state,new);state=new
        while True:
            if not state['queue']:
                if state['done']:
                    if state['completed_day']!=day:
                        new=copy.deepcopy(state);new['completed_day']=day;save(state,new);state=new
                    return {'sold_ts':state['earliest'],'complete':True}
                if reserve is not None:reserve()
                options={'limit':_SIG_LIMIT}
                if state['before']:options['before']=state['before']
                sigs=_rpc('getSignaturesForAddress',[wallet,options])
                if not isinstance(sigs,list):raise ValueError('signature page unavailable')
                new=copy.deepcopy(state);queue=[];done=len(sigs)<_SIG_LIMIT
                seen=set()
                for item in sigs:
                    if (not isinstance(item,dict) or not isinstance(item.get('signature'),str) or not item['signature']
                            or item['signature'] in seen or type(item.get('blockTime')) not in (int,float)
                            or not math.isfinite(item['blockTime'])):raise ValueError('signature page malformed')
                    seen.add(item['signature'])
                    if item['blockTime']<after_ts:done=True
                    else:queue.append({'signature':item['signature'],'blockTime':item['blockTime']})
                before=sigs[-1]['signature'] if sigs else state['before']
                if sigs and before==state['before']:raise ValueError('signature pagination did not advance')
                new.update(queue=queue,before=before,done=done)
                save(state,new);state=new
                if not queue:continue
            if budget[0]<=0:return {'sold_ts':state['earliest'],'complete':False}
            item=state['queue'][0]
            if reserve is not None:reserve()
            budget[0]-=1
            tx=_rpc('getTransaction',[item['signature'],{'encoding':'jsonParsed','maxSupportedTransactionVersion':0}])
            _verify_transaction_source(tx,item)
            status=_txg.tx_reverted_reason(tx)
            if status=='tx_unknown':raise ValueError('transaction execution unknown')
            new=copy.deepcopy(state)
            if status is None:
                if _verified_token_delta(tx,item,wallet,mint)<0:
                    new['earliest']=min(new['earliest'],item['blockTime']) if new['earliest'] is not None else item['blockTime']
            new['queue']=new['queue'][1:];save(state,new);state=new
            time.sleep(.15)
    except Exception:
        logger.warning('exit-copy wallet history partial/unavailable',exc_info=True)
        return {'sold_ts':state['earliest'] if state else None,'complete':False}


def _verify_transaction_source(tx,item):
    import math
    if not isinstance(tx,dict):raise ValueError('transaction unavailable')
    signatures=(tx.get('transaction') or {}).get('signatures')
    ts=tx.get('blockTime')
    if (not isinstance(signatures,list) or not signatures or signatures[0]!=item['signature']
            or type(ts) not in (int,float) or not math.isfinite(ts) or ts!=item['blockTime']):
        raise ValueError('transaction signature/time source conflict')


def _verified_token_delta(tx,item,wallet,mint):
    """Exact raw-unit change; UI floats never determine a sale."""
    _verify_transaction_source(tx,item)
    meta=tx.get('meta') or {}
    identities={};relevant_decimals=set()
    def balances(name):
        rows=meta.get(name)
        if not isinstance(rows,list):raise ValueError('transaction balances unavailable')
        found={}
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('owner'),str) or not row['owner'] or not isinstance(row.get('mint'),str) or not row['mint']:
                raise ValueError('transaction balance identity unresolved')
            index=row.get('accountIndex');token=row.get('uiTokenAmount')
            if type(index) is not int or index<0 or index in found or not isinstance(token,dict):raise ValueError('transaction token account unresolved')
            raw=token.get('amount');decimals=token.get('decimals')
            if (not isinstance(raw,str) or not raw or any(c not in '0123456789' for c in raw)
                    or type(decimals) is not int or not 0<=decimals<=255):raise ValueError('transaction raw amount unavailable')
            identity=(row['owner'],row['mint'],decimals)
            if index in identities and identities[index]!=identity:raise ValueError('transaction account identity/decimals conflict')
            identities[index]=identity;found[index]=(identity,int(raw))
            if row['owner']==wallet and row['mint']==mint:relevant_decimals.add(decimals)
        return found
    pre=balances('preTokenBalances');post=balances('postTokenBalances')
    if len(relevant_decimals)>1:raise ValueError('transaction mint decimals conflict')
    def total(rows):return sum(raw for identity,raw in rows.values() if identity[:2]==(wallet,mint))
    # Fully validated token balance lists with no matching owner/mint establish
    # that this transaction did not change the requested token balance.
    return total(post)-total(pre)
