"""Tracker-input availability only, not authenticated vendor or chain data."""
import hashlib
import json
import math
import uuid

FIELDS=('t','usd_market_cap','price_usd','vol_m5','buys_m5','sells_m5','usd_liquidity','holders')
COUNTS={'buys_m5','sells_m5','holders'}


def availability(value,present=True,count=False):
    if not present:return 'missing'
    if value is None:return 'null'
    if type(value) not in (int,float):return 'invalid-type'
    try:
        if not math.isfinite(value):return 'nonfinite'
        if value<0:return 'negative'
        if count and value!=int(value):return 'fractional-count'
    except (OverflowError,ValueError):return 'unrepresentable'
    return 'recorded'


def digest(mint,obs,identity,states,provider=None):
    body=json.dumps([mint,list(obs),identity,states],sort_keys=True,separators=(',',':'),allow_nan=True)
    if provider is not None:body=json.dumps([mint,list(obs),identity,states,provider],sort_keys=True,separators=(',',':'),allow_nan=True)
    return hashlib.sha256(body.encode()).hexdigest()


def envelope(mint,obs,stats,now,holders):
    states={key:availability(now if key=='t' else holders if key=='holders' else stats.get(key),
                            key in ('t','holders') or key in stats,key in COUNTS) for key in FIELDS}
    identity=uuid.uuid4().hex
    from daemon.launch_scanner import provider_inputs
    provider=stats.get(provider_inputs.KEY)
    result=dict(version=3 if provider is not None else 2,observation_id=identity,mint=mint,availability=states,
                binding=digest(mint,obs,identity,states,provider),scope='tracker-input-and-optional-adapter-consistency-not-authentication')
    if provider is not None:result['provider_normalization']=provider
    return result


class Observation(tuple):
    """Tuple numerical behavior unchanged; exact envelope travels with tuple."""
    def __new__(cls,values,evidence):
        obj=super().__new__(cls,values)
        obj.evidence_json=json.dumps(evidence,sort_keys=True)
        return obj


def validate(mint,obs,evidence):
    if not isinstance(evidence,dict) or type(evidence.get('version')) is not int or evidence.get('version') not in (2,3):
        return 'legacy-unrecorded' if evidence is None else 'unknown-version'
    if evidence['version']==3 and not isinstance(evidence.get('provider_normalization'),dict):return 'binding-invalid'
    identity=evidence.get('observation_id');states=evidence.get('availability')
    if (not isinstance(identity,str) or not identity or evidence.get('mint')!=mint
            or not isinstance(states,dict) or set(states)!=set(FIELDS)
            or evidence.get('binding')!=digest(mint,obs,identity,states,evidence.get('provider_normalization') if evidence['version']==3 else None)):
        return 'binding-invalid'
    if any(states[k]!='recorded' for k in FIELDS if k!='holders'):
        return 'required-input-unavailable'
    return 'recorded'


def window(mint,series):
    rows=[];ids=set();previous=None
    for obs in series or []:
        try:e=json.loads(obs.evidence_json)
        except (AttributeError,ValueError,TypeError):e=None
        state=validate(mint,obs,e)
        if state!='recorded':return dict(state=state,rows=rows)
        identity=e['observation_id']
        if identity in ids:return dict(state='duplicate-identity',rows=rows)
        if previous is not None and obs[0]<previous:return dict(state='chronology-invalid',rows=rows)
        ids.add(identity);previous=obs[0];rows.append(dict(obs=list(obs),evidence=e))
    return dict(state='recorded' if rows else 'empty',rows=rows)



def capture(mint,recorded_t,result):
    """Bind a typed recording boundary and ordered result, not source auth."""
    body=dict(result)
    body['capture']={'version':1,'mint':mint,'recorded_t':recorded_t}
    body['capture']['binding']=capture_digest(body)
    return body


def capture_digest(result):
    body={k:v for k,v in result.items() if k!='capture'}
    c=result.get('capture') or {}
    encoded=json.dumps([c.get('version'),c.get('mint'),c.get('recorded_t'),body],
                       sort_keys=True,separators=(',',':'),allow_nan=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def capture_time(mint,result):
    if not isinstance(result,dict):return None
    c=result.get('capture')
    if (not isinstance(c,dict) or type(c.get('version')) is not int or c['version']!=1
            or c.get('mint')!=mint or availability(c.get('recorded_t'))!='recorded'
            or c.get('binding')!=capture_digest(result)):return None
    return c['recorded_t']

def matches(mint,evidence,snapshots,until=None):
    """Strict envelope and exact ordered projection, without coercion/repair."""
    from .fingerprints import validate_recorded_series
    try:
        if not isinstance(evidence,dict) or evidence.get('state')!='recorded':return False
        boundary=capture_time(mint,evidence)
        if boundary is None or until is not None and (availability(until)!='recorded' or boundary!=until):return False
        rows=evidence.get('rows')
        if not isinstance(rows,list) or not rows:return False
        if any(not isinstance(r,dict) or set(r)!={'obs','evidence'} for r in rows):return False
        series=[r['obs'] for r in rows]
        if any(not isinstance(o,(list,tuple)) or len(o)!=8 for o in series):return False
        validate_recorded_series([list(o[:7])+[None] for o in series],until=boundary)
        if any(validate(mint,r['obs'],r['evidence'])!='recorded' for r in rows):return False
        if len({r['evidence']['observation_id'] for r in rows})!=len(rows):return False
        projected=[dict(t=o[0],mcap_usd=o[1],price_usd=o[2],vol5m_usd=o[3],
                        buys5m=int(o[4]),sells5m=int(o[5]),liq_usd=o[6]) for o in series]
        return projected==snapshots
    except (ValueError,TypeError,OverflowError,KeyError,IndexError):return False
