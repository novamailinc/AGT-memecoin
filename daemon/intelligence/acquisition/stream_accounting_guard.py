"""Fail-closed write-ahead accounting markers, shared across stream workers.

A failed outcome/credit write leaves a durable pending marker. New workers
must refuse admission until that evidence is reconciled, never erase it on
restart. Markers contain no key or event payload. No automatic reset.
"""
import contextlib
import json
import time
import os
from pathlib import Path
import uuid

_PROCESS_BLOCKED=set()


def root(home):
    return Path(home or os.environ.get('FOREX_AGENT_HOME') or Path.home()/'.forex-agent')/'helius_accounting_pending'


def allowed(home):
    p=root(home)
    if str(p) in _PROCESS_BLOCKED:return False
    try:return not p.exists() or not any(p.iterdir())
    except OSError:return False


@contextlib.contextmanager
def recording(home, evidence=None):
    p=root(home); f=p/(uuid.uuid4().hex+'.pending')
    try:
        p.mkdir(parents=True,exist_ok=True)
        parent=os.open(p.parent,os.O_RDONLY)
        try:os.fsync(parent)
        finally:os.close(parent)
        fd=os.open(f,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:os.write(fd,json.dumps({'id':f.stem,'created':time.time(),'state':'unresolved','evidence':evidence or {}},allow_nan=False).encode());os.fsync(fd)
        finally:os.close(fd)
        directory=os.open(p,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
        yield
        f.unlink()
        directory=os.open(p,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    except Exception:
        _PROCESS_BLOCKED.add(str(p))
        raise
