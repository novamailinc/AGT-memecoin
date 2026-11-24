"""Strict legacy/envelope deployer reader shared by primary consumers."""
import json
from pathlib import Path
try:
    from . import learning_effects as effects
except ImportError:
    import learning_effects as effects

def validate(value):
    effects._validate(value)
    if not isinstance(value,dict) or any(not k or not isinstance(v,dict) for k,v in value.items()):
        raise ValueError('deployer registry unavailable')
    import math
    for entry in value.values():
        for name in ('launches_seen','hits_2x','rugs','dead'):
            if name in entry and (type(entry[name]) is not int or entry[name]<0):raise ValueError('deployer counters invalid')
        for name in ('first_seen','last_seen'):
            if name in entry and entry[name] is not None and (type(entry[name]) not in (int,float) or not math.isfinite(entry[name])):raise ValueError('deployer timestamp invalid')
    return value


def canonical(value):
    markers={'schema_version','registry','grade_claims'}
    if isinstance(value,dict) and markers.intersection(value) and set(value)!=markers:
        raise ValueError('deployer claims schema damaged')
    if isinstance(value,dict) and set(value)==markers:
        if type(value['schema_version']) is not int or value['schema_version']!=2 or not isinstance(value['grade_claims'],dict):raise ValueError('deployer claims schema invalid')
        validate(value['registry'])
        for mint,claim in value['grade_claims'].items():
            import math
            if (not mint or not isinstance(claim,dict) or set(claim)!={'mint','deployer','outcome','observed_at','source'}
                    or claim['mint']!=mint or not isinstance(claim['deployer'],str) or not claim['deployer']
                    or claim['outcome'] not in ('hit_2x','rug','dead') or not isinstance(claim['source'],str) or not claim['source']
                    or type(claim['observed_at']) not in (int,float) or not math.isfinite(claim['observed_at'])):
                raise ValueError('deployer grade claim invalid')
        effects._validate(value)
        return value
    return {'schema_version':2,'registry':validate(value),'grade_claims':{}}


def read_document(path,*,require_existing=False):
    try:doc=json.loads(Path(path).read_text(),object_pairs_hook=effects._pairs,parse_constant=effects._constant)
    except FileNotFoundError:
        if require_existing:raise ValueError('deployer source missing')
        return {'version':1,'value':canonical({}),'effects':{}}
    if isinstance(doc,dict) and set(doc)=={'version','value','effects'}:
        if (type(doc['version']) is not int or doc['version']!=1 or not isinstance(doc['effects'],dict)
                or any(not isinstance(k,str) or not k or not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for k,v in doc['effects'].items())):
            raise ValueError('deployer receipt envelope invalid')
        doc['value']=canonical(doc['value']);return doc
    return {'version':1,'value':canonical(doc),'effects':{}}


def read(path,*,require_existing=False):return read_document(path,require_existing=require_existing)['value']['registry']
