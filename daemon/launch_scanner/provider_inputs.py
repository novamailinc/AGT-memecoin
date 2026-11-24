"""Local adapter evidence. No provider authentication or freshness claim."""
import copy
import hashlib
import json
import math
import re
import uuid
from decimal import Decimal, InvalidOperation

KEY='_provider_normalization'
FIELDS=('usd_market_cap','price_usd','vol_m5','buys_m5','sells_m5','usd_liquidity')
# No whitespace, leading plus, exponent or leading-zero shorthand. Minus is
# parsed then rejected when negative; -0 remains a distinct raw string.
DECIMAL=re.compile(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z')
# Non-count decimals use Python's correctly rounded binary64 projection,
# compared exactly with adapter output; original decimal remains retained.
# Counts must project exactly. Nonzero underflow and overflow are refused.
POLICY='finite-base10-no-whitespace-plus-exponent-binary64-count-exact-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=True).encode()).hexdigest()


def sealed(body):
    body=copy.deepcopy(body);body.pop('binding',None);body['binding']=digest(body);return body


def valid_binding(body):
    return isinstance(body,dict) and body.get('binding')==digest({k:v for k,v in body.items() if k!='binding'})


def chain_name(chain):
    return {'eth':'ethereum','ethereum':'ethereum','solana':'solana','base':'base'}.get(chain)


def same_token(a,b,chain):
    if not isinstance(a,str) or not isinstance(b,str) or not a or not b:return False
    return a==b if chain_name(chain)=='solana' else a.lower()==b.lower()


def raw_field(payload,path):
    value=payload
    for part in path.split('.'):
        if not isinstance(value,dict):return {'path':path,'state':'container-invalid'}
        if part not in value:return {'path':path,'state':'missing'}
        value=value[part]
    return {'path':path,'state':'present','value':copy.deepcopy(value)}


def parsed(field,count=False):
    if field.get('state')!='present':return None,field.get('state','invalid')
    value=field.get('value')
    if value is None:return None,'null'
    if type(value) is bool:return None,'invalid-type'
    if type(value) not in (int,float,str):return None,'invalid-type'
    if isinstance(value,str) and not DECIMAL.fullmatch(value):return None,'invalid-decimal-string'
    try:
        dec=Decimal(value) if type(value) is int or isinstance(value,str) else Decimal.from_float(value)
        if not dec.is_finite():return None,'nonfinite'
        if dec<0:return None,'negative'
        if count and dec!=dec.to_integral_value():return None,'fractional-count'
        number=float(dec)
        if not math.isfinite(number):return None,'overflow'
        if dec!=0 and number==0:return None,'underflow'
        if count and Decimal.from_float(number)!=dec:return None,'count-precision-loss'
        return number,'parsed-decimal-string' if isinstance(value,str) else 'numeric'
    except (InvalidOperation,ValueError,OverflowError):return None,'unrepresentable'


def begin(provider,payload):
    if provider=='dexscreener':
        paths={'usd_market_cap':'marketCap','price_usd':'priceUsd','vol_m5':'volume.m5',
               'buys_m5':'txns.m5.buys','sells_m5':'txns.m5.sells','usd_liquidity':'liquidity.usd'}
        identity={k:raw_field(payload,p) for k,p in {'mint':'baseToken.address','quote':'quoteToken.address',
                    'chain':'chainId','pair':'pairAddress'}.items()}
        proxy=not bool(payload.get('marketCap'))
        if proxy:paths['usd_market_cap']='fdv'
    elif provider=='geckoterminal':
        paths={'usd_market_cap':'attributes.market_cap_usd','price_usd':'attributes.price_usd',
               'vol_m5':'unsupported.volume.m5','buys_m5':'unsupported.txns.m5.buys',
               'sells_m5':'unsupported.txns.m5.sells','usd_liquidity':'attributes.total_reserve_in_usd'}
        attrs=payload.get('attributes') or {};proxy=not bool(attrs.get('market_cap_usd'))
        if proxy:paths['usd_market_cap']='attributes.fdv_usd'
        identity={'mint':raw_field(payload,'attributes.address'),'resource':raw_field(payload,'id'),
                  'chain':{'path':'resource-id-network-prefix','state':'unrecorded'}}
    else:raise ValueError('unknown adapter')
    return {'version':1,'provider':provider,'policy':POLICY,'source_id':uuid.uuid4().hex,
            'fields':{k:raw_field(payload,p) for k,p in paths.items()},'payload_identity':identity,
            'market_cap_semantics':'fdv-proxy' if proxy else 'observed-market-cap',
            'market_cap_primary':raw_field(payload,'marketCap' if provider=='dexscreener' else 'attributes.market_cap_usd')}


def finish(body,normalized):
    body=copy.deepcopy(body);body['normalized']={k:normalized.get(k) for k in FIELDS}
    return sealed(body)


def resolved(stats,mint,chain,selection):
    result=copy.deepcopy(stats);body=result.get(KEY)
    if not isinstance(body,dict):return result
    # Requested identity is separate from observed payload identity, never
    # copied into payload identity when the latter is absent.
    body.pop('binding',None);body['request']={'mint':mint,'chain':chain,'selection':selection}
    result[KEY]=sealed(body);return result


def validate(mint,normalized,body):
    if body is None:return 'provider-unrecorded'
    try:
        if (not valid_binding(body) or type(body.get('version')) is not int or body['version']!=1
                or body.get('policy')!=POLICY or body.get('provider') not in ('dexscreener','geckoterminal')
                or not isinstance(body.get('source_id'),str) or not body['source_id']):return 'provider-invalid'
        if set(body['fields'])!=set(FIELDS) or set(body['normalized'])!=set(FIELDS):return 'provider-invalid'
        if {k:normalized.get(k) for k in FIELDS}!=body['normalized']:return 'provider-output-mismatch'
        request=body.get('request') or {};chain=chain_name(request.get('chain'))
        if chain is None or request.get('mint')!=mint:return 'provider-request-unbound'
        allowed=('highest-liquidity-base-token-pair','highest-liquidity-single-token-pair') if body['provider']=='dexscreener' else ('token-resource-no-pair-selection',)
        if request.get('selection') not in allowed:return 'provider-selection-unrecorded'
        identity=body['payload_identity'];observed=identity['mint']
        if observed.get('state')!='present' or not isinstance(observed.get('value'),str):return 'provider-identity-unrecorded'
        if not same_token(mint,observed['value'],chain):return 'provider-identity-conflict'
        if body['provider']=='dexscreener':
            payload_chain=identity['chain']
            if payload_chain.get('state')!='present':return 'provider-identity-unrecorded'
            if chain_name(payload_chain.get('value'))!=chain:return 'provider-identity-conflict'
            if identity['pair'].get('state')!='present' or not identity['pair'].get('value'):return 'provider-pair-unrecorded'
            expected={'usd_market_cap':'marketCap' if body['market_cap_semantics']=='observed-market-cap' else 'fdv',
                      'price_usd':'priceUsd','vol_m5':'volume.m5','buys_m5':'txns.m5.buys','sells_m5':'txns.m5.sells','usd_liquidity':'liquidity.usd'}
        else:
            resource=identity['resource']
            # Resource id can corroborate provider network and token. Request
            # network alone never supplies missing observed chain identity.
            prefix={'solana':'solana','base':'base','ethereum':'eth'}[chain]
            if resource.get('state')!='present':return 'provider-identity-unrecorded'
            if resource.get('value')!=prefix+'_'+observed['value']:return 'provider-identity-conflict'
            expected={'usd_market_cap':'attributes.market_cap_usd' if body['market_cap_semantics']=='observed-market-cap' else 'attributes.fdv_usd',
                      'price_usd':'attributes.price_usd','vol_m5':'unsupported.volume.m5','buys_m5':'unsupported.txns.m5.buys',
                      'sells_m5':'unsupported.txns.m5.sells','usd_liquidity':'attributes.total_reserve_in_usd'}
        if body['market_cap_semantics'] not in ('observed-market-cap','fdv-proxy'):return 'provider-invalid'
        if body['market_cap_semantics']=='fdv-proxy':return 'provider-market-cap-proxy'
        if body.get('market_cap_primary')!=body['fields']['usd_market_cap']:return 'provider-selection-conflict'
        for k,field in body['fields'].items():
            if field.get('path')!=expected[k]:return 'provider-path-invalid'
            number,state=parsed(field,count=k in ('buys_m5','sells_m5'))
            if state not in ('numeric','parsed-decimal-string'):return 'provider-field-unavailable'
            value=body['normalized'][k]
            if type(value) not in (int,float) or number!=value:return 'provider-output-mismatch'
        return 'provider-recorded'
    except (KeyError,TypeError,ValueError,OverflowError):return 'provider-invalid'


def history_status(mint,evidence,chain=None,snapshots=None):
    """Whole history qualifies only if every ordered input has provider scope."""
    from .input_availability import capture_time,validate as tracker_validate
    if not isinstance(evidence,dict) or evidence.get('state')!='recorded':return 'tracker-unavailable'
    if capture_time(mint,evidence) is None:return 'provider-invalid'
    rows=evidence.get('rows')
    if not isinstance(rows,list) or not rows:return 'provider-unrecorded'
    from .input_availability import matches
    try:
        projected=[dict(t=r['obs'][0],mcap_usd=r['obs'][1],price_usd=r['obs'][2],vol5m_usd=r['obs'][3],
                        buys5m=int(r['obs'][4]),sells5m=int(r['obs'][5]),liq_usd=r['obs'][6]) for r in rows]
        if not matches(mint,evidence,projected if snapshots is None else snapshots):return 'provider-invalid'
    except (KeyError,TypeError,IndexError,ValueError,OverflowError):return 'provider-invalid'
    statuses=[]
    for row in rows:
        try:
            o=row['obs'];e=row['evidence']
            if tracker_validate(mint,o,e)!='recorded':return 'provider-invalid'
            normalized=dict(zip(FIELDS,o[1:7]))
            if chain is not None and e.get('version')==3:
                request=((e.get('provider_normalization') or {}).get('request') or {})
                if chain_name(chain) is None or chain_name(request.get('chain'))!=chain_name(chain):return 'provider-identity-conflict'
            statuses.append(validate(mint,normalized,e.get('provider_normalization')) if e.get('version')==3 else 'provider-unrecorded')
        except (KeyError,TypeError,IndexError):return 'provider-invalid'
    return 'provider-recorded' if all(s=='provider-recorded' for s in statuses) else next(s for s in statuses if s!='provider-recorded')



def record_status(history):
    """Validate the stored recording boundary independently of replay model."""
    from .input_availability import availability,capture_time
    if not isinstance(history,dict):return 'provider-invalid'
    mint=history.get('mint');evidence=history.get('snapshot_input_evidence')
    if history.get('outcome_kind')=='paper':
        boundary=history.get('snapshot_recorded_t')
    else:
        outcome=history.get('outcome')
        boundary=outcome.get('recorded_t') if isinstance(outcome,dict) else None
    if availability(boundary)!='recorded' or capture_time(mint,evidence)!=boundary:return 'provider-record-boundary-invalid'
    return history_status(mint,evidence,history.get('chain','unrecorded'),history.get('snapshots',[]))
