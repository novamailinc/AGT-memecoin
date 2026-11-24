"""Inactive failure-report outbox. Local storage is not remote delivery.

No default receiver or sender is supplied. A deployment must inject a
transport and an authenticated receipt verifier before trying delivery.
Diagnostic strings are data, never executable repair instructions.
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import traceback
import uuid

# Only enumerated diagnostic fields are permitted. Free text, even beneath
# an innocent key, can contain persistent secrets and is never retained.
_NUMERIC_CONTEXT = frozenset({'http_status','rpc_code','retry_seconds','attempt','slot','gap_seconds'})
_ENUM_CONTEXT = {'provider':{'alchemy','chainstack','helius','public'},
                 'operation':{'read','connect','subscribe','reconcile','persist'}}


def redact(value):
    if not isinstance(value,dict):
        return {}
    out={}
    for key,item in value.items():
        if key in _NUMERIC_CONTEXT and isinstance(item,(int,float)) and not isinstance(item,bool):
            valid=math.isfinite(item) if isinstance(item,float) else True
            if key=='http_status':valid=valid and isinstance(item,int) and 100<=item<=599
            elif key=='rpc_code':valid=valid and isinstance(item,int) and -(2**31)<=item<2**31
            elif key in ('attempt','slot'):valid=valid and isinstance(item,int) and 0<=item<2**63
            else:valid=valid and 0<=item<=315360000
            if valid:out[key]=item
        elif key in _ENUM_CONTEXT and isinstance(item,str) and item in _ENUM_CONTEXT[key]:
            out[key]=item
    return out


def exception_details(exc):
    """Exact exception type and code locations, without source text or locals.

    Exception messages can contain arbitrary request bodies or credentials,
    so omit them rather than promising arbitrary-text secret detection.
    """
    return {'exception_type':type(exc).__name__,
            'frames':[{'file':os.path.basename(f.filename),'line':f.lineno,'function':f.name}
                      for f in traceback.extract_tb(exc.__traceback__)[-20:]],
            'message_omitted':True}


class FailureOutbox:
    def __init__(self,path,now=time.time,*,lock_timeout=5):
        self.path=str(path);self.now=now;self.lock_timeout=lock_timeout
        os.makedirs(os.path.dirname(os.path.abspath(self.path)),exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, next_at REAL NOT NULL, lease_until REAL NOT NULL, receipt TEXT)')
        os.chmod(self.path,0o600)

    def _db(self):
        db=sqlite3.connect(self.path,timeout=self.lock_timeout)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def record(self,component,code,exc=None,context=None,*,coalesce_key=None,max_pending=None):
        report_id='failure_'+uuid.uuid4().hex
        body={'schema':1,'report_id':report_id,'observed_at':self.now(),
              'component':component,'code':code,'diagnostics_untrusted':True,
              'context':redact(context or {})}
        if exc is not None:body['exception']=exception_details(exc)
        # Components/codes are developer-owned identifiers, not copied error text.
        for value in (component,code):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',value):
                raise ValueError('component and code require stable identifiers')
        encoded=json.dumps(body,sort_keys=True,separators=(',',':'),allow_nan=False)
        digest=hashlib.sha256(encoded.encode()).hexdigest()
        with self._db() as db:
            if coalesce_key is not None:
                db.execute('CREATE TABLE IF NOT EXISTS coalesced (key TEXT PRIMARY KEY, report_id TEXT NOT NULL, count INTEGER NOT NULL, last_at REAL NOT NULL)')
                db.execute('BEGIN IMMEDIATE')
                prior=db.execute('SELECT c.report_id FROM coalesced c JOIN reports r ON r.id=c.report_id WHERE c.key=? AND r.state!=?',(coalesce_key,'received')).fetchone()
                if prior:
                    db.execute('UPDATE coalesced SET count=count+1,last_at=? WHERE key=?',(self.now(),coalesce_key))
                    return prior[0]
            if max_pending is not None and db.execute("SELECT count(*) FROM reports WHERE state!='received'").fetchone()[0]>=max_pending:
                raise ValueError('failure queue capacity reached')
            db.execute('INSERT INTO reports VALUES (?,?,?,\'pending\',0,?,0,NULL)',(report_id,encoded,digest,self.now()))
            if coalesce_key is not None:
                db.execute('INSERT INTO coalesced VALUES (?,?,1,?) ON CONFLICT(key) DO UPDATE SET report_id=excluded.report_id,count=1,last_at=excluded.last_at',(coalesce_key,report_id,self.now()))
        return report_id

    def status(self,report_id):
        with self._db() as db:
            row=db.execute('SELECT state,attempts,next_at,receipt FROM reports WHERE id=?',(report_id,)).fetchone()
        if row is None:raise KeyError(report_id)
        return dict(zip(('state','attempts','next_at','receipt'),row))

    def deliver_one(self,transport,verify_receipt,receiver):
        """Attempt one due report. Neither HTTP200 nor queued email is proof.

        transport(envelope) returns a candidate receipt. verify_receipt must
        authenticate its expected receiver identity independently. Binding
        report_id/digest/receiver/state is checked here as well. The same ID
        is retried after a lost receipt, requiring receiver deduplication.
        """
        if not receiver or not callable(transport) or not callable(verify_receipt):
            raise ValueError('verified receiver, transport and receipt verifier required')
        now=self.now()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT id,body,digest,attempts FROM reports WHERE state!='received' AND next_at<=? AND lease_until<=? ORDER BY next_at,id LIMIT 1",(now,now)).fetchone()
            if row is None:return None
            rid,body,digest,attempts=row
            # Expiring claim survives abrupt sender death; no success before ACK.
            db.execute('UPDATE reports SET state=\'inflight\',lease_until=?,attempts=attempts+1 WHERE id=?',(now+60,rid))
        envelope={'report':json.loads(body),'sha256':digest,'receiver':receiver}
        valid=False;receipt=None
        try:
            receipt=transport(envelope)
            valid=isinstance(receipt,dict) and receipt.get('report_id')==rid and receipt.get('sha256')==digest and receipt.get('receiver')==receiver and receipt.get('state')=='received' and verify_receipt(receipt) is True
        except Exception:
            pass  # Do not persist untrusted response bodies or credential errors.
        with self._db() as db:
            if valid:
                # Persist only bound acknowledgement fields, never remote instructions.
                safe={k:receipt[k] for k in ('report_id','sha256','receiver','state')}
                db.execute("UPDATE reports SET state='received',receipt=?,lease_until=0 WHERE id=?",(json.dumps(safe,sort_keys=True),rid))
            else:
                delay=min(3600,5*2**min(attempts,10))
                db.execute("UPDATE reports SET state='pending',next_at=?,lease_until=0 WHERE id=? AND attempts=? AND state!='received'",(self.now()+delay,rid,attempts+1))
        return {'report_id':rid,'received':self.status(rid)['state']=='received'}



def capture_local(component,code,exc=None,context=None,*,path=None):
    """Persist redacted diagnostics locally only. Never attempts remote delivery.

    Returns a local report ID, not a delivery receipt. SQLite lock admission is nonblocking. Filesystem and sync latency are not
    bounded here. Failure returns None and never invokes logging handlers.
    """
    try:
        if path is None:
            from daemon.common import get_home
            path=os.path.join(get_home(),'diagnostics','failure_reports.sqlite')
        safe_context=redact(context or {})
        kind=type(exc).__name__ if exc is not None else None
        group=hashlib.sha256(json.dumps([component,code,kind,safe_context],sort_keys=True).encode()).hexdigest()
        return FailureOutbox(path,lock_timeout=0).record(component,code,exc,safe_context,coalesce_key=group,max_pending=1000)
    except Exception:
        return None
