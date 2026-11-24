"""Atomic effect application in a JSON learning store, not an outbox ACK.

Receipts live in the same atomically replaced document as the learning value.
No side effects belong in the reducer. Callers must use this lock for every
writer of that file; this helper alone does not migrate or fence legacy writers.
"""
import contextlib
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def _validate(value):
    # Reject coercions before digesting: reducers see the same JSON identity.
    if value is None or type(value) in (str,bool,int):return
    if type(value) is float:
        import math
        if math.isfinite(value):return
    if type(value) is list:
        for item in value:_validate(item)
        return
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():_validate(item)
        return
    raise ValueError('learning value must use exact JSON types and string keys')


def _constant(value):
    raise ValueError('nonfinite learning JSON constant')


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate learning JSON key')
        result[key]=value
    return result


def _encoded(value):
    _validate(value)
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


def _sync_directory(path):
    directory=os.open(path,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)


def apply(path,effect_id,payload,reducer,initial=None,legacy_import=None,require_existing=False):
    """Apply a deterministic pure reducer once, or reconfirm exact retry durability.

    Envelope is new-format only: legacy stores require explicit validated import.
    Returns (created, value). An error after replace may already have committed;
    retry the same effect, never infer rollback from the exception.
    """
    if not isinstance(effect_id,str) or not effect_id:
        raise ValueError('learning effect needs exact identity')
    canonical_payload=_encoded(payload)
    digest=hashlib.sha256(canonical_payload.encode()).hexdigest()
    payload=json.loads(canonical_payload,object_pairs_hook=_pairs,parse_constant=_constant)
    _validate(initial)
    path=Path(path).absolute()
    # Parent provisioning is a separate responsibility. Require it to exist,
    # then sync every ancestor so first-write admission cannot rely on an
    # unflushed newly created parent entry.
    if not path.parent.is_dir():raise ValueError('learning parent must already exist')
    for ancestor in (path.parent,*path.parent.parents):_sync_directory(ancestor)
    with open(str(path)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        try:
            try:doc=json.loads(path.read_text(),object_pairs_hook=_pairs,parse_constant=_constant)
            except FileNotFoundError:
                if require_existing:raise ValueError('pinned learning store missing')
                doc={'version':1,'value':copy.deepcopy(initial),'effects':{}}
            if legacy_import is not None and not (isinstance(doc,dict) and set(doc)=={'version','value','effects'}):
                doc={'version':1,'value':legacy_import(copy.deepcopy(doc)),'effects':{}}
            if (not isinstance(doc,dict) or type(doc.get('version')) is not int
                    or doc['version']!=1 or 'value' not in doc
                    or not isinstance(doc.get('effects'),dict)
                    or any(not isinstance(k,str) or not k or not isinstance(v,str)
                           or len(v)!=64 or any(c not in '0123456789abcdef' for c in v)
                           for k,v in doc['effects'].items())):
                raise ValueError('learning store unavailable or needs explicit migration')
            _validate(doc)  # including old value, before reducer can overwrite corruption
            prior=doc['effects'].get(effect_id)
            if prior is not None and prior!=digest:
                raise ValueError('learning effect identity payload conflict')
            created=prior is None
            if created:
                doc['value']=reducer(copy.deepcopy(doc['value']),copy.deepcopy(payload))
                doc['effects'][effect_id]=digest
            # Even exact retry reestablishes file + directory durability after
            # an earlier failure between visible replace and successful fsync.
            encoded=_encoded(doc)
            fd,name=tempfile.mkstemp(dir=path.parent,prefix='.learning-effect-')
            owned_fd=fd
            try:
                os.fchmod(fd,0o600)
                out=os.fdopen(fd,'w')
                owned_fd=None
                with out:
                    out.write(encoded);out.flush();os.fsync(out.fileno())
                os.replace(name,path)
                _sync_directory(path.parent)
            finally:
                if owned_fd is not None:os.close(owned_fd)
                if os.path.exists(name):os.unlink(name)
            return created,copy.deepcopy(doc['value'])
        finally:fcntl.flock(lock,fcntl.LOCK_UN)
