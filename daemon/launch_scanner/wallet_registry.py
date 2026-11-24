"""Wallet-map compatibility reader and atomic canonical learning store.

Legacy wallet map is imported under the same stable file lock. All readers and writers
must use this module before migration is enabled. No automatic recovery replay.
"""
import json
from pathlib import Path
try:
    from . import learning_effects as effects
except ImportError:
    import learning_effects as effects


def canonical(path):return Path(path)


def validate(wallets):
    effects._validate(wallets)
    if not isinstance(wallets,dict):raise ValueError('wallet registry is not a map')
    for wallet,entry in wallets.items():
        if not wallet or not isinstance(entry,dict):raise ValueError('wallet registry entry unavailable')
        for field in ('picks','wins'):
            if field in entry and (type(entry[field]) is not int or entry[field]<0):
                raise ValueError('wallet registry counter unavailable')
        if entry.get('wins',0)>entry.get('picks',0):raise ValueError('wallet registry counters conflict')
    return wallets


def _legacy(path):
    try:raw=Path(path).read_text()
    except FileNotFoundError:return {}
    return validate(json.loads(raw,object_pairs_hook=effects._pairs,parse_constant=effects._constant))


def read(path):
    try:doc=json.loads(Path(path).read_text(),object_pairs_hook=effects._pairs,parse_constant=effects._constant)
    except FileNotFoundError:return {}
    effects._validate(doc)
    if isinstance(doc,dict) and set(doc)=={'version','value','effects'}:
        if (type(doc['version']) is not int or doc['version']!=1 or not isinstance(doc['effects'],dict)
                or any(not isinstance(k,str) or not k or not isinstance(v,str) or len(v)!=64
                       or any(c not in '0123456789abcdef' for c in v) for k,v in doc['effects'].items())):
            raise ValueError('canonical wallet registry unavailable')
        return validate(doc['value'])
    return validate(doc)


def update(path,effect_id,payload,reducer,require_existing=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    def checked(value,data):return validate(reducer(validate(value),data))
    return effects.apply(path,effect_id,payload,checked,{},legacy_import=validate,require_existing=require_existing)


def replace(path,wallets):
    """Legacy bootstrap only. Refuse overwriting an existing canonical value."""
    import uuid
    def reducer(old,new):
        if old and old!=new:raise ValueError('registry replacement requires explicit reconciliation')
        return new
    return update(path,'bootstrap-'+uuid.uuid4().hex,validate(wallets),reducer)


def mute(path,changes,effect_id):
    def reducer(wallets,payload):
        for wallet,fields in payload.items():
            if wallet in wallets:wallets[wallet].update(fields)
        return wallets
    return update(path,effect_id,changes,reducer)
