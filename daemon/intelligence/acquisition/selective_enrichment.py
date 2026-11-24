"""Selective scanner diagnostics. Parsed facts never replace raw transaction truth.

Only unsuccessful mint decoding enters this path. A finite durable request cap
bounds spend even across restart; claimed requests are never automatically retried
because a timeout/crash cannot prove the provider did not bill them.
"""
import queue
import threading
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from .helius_parsed_events import HeliusParsedEventsClient
from .helius_streams import verify_with_own_decoder
from .usage_tracker import UsageTracker


class SelectiveEnrichment:
    def __init__(self, home=None, client=None, max_requests=20):
        self.home=Path(home or os.environ.get('FOREX_AGENT_HOME',Path.home()/'.forex-agent'))
        self.home.mkdir(parents=True,exist_ok=True)
        self.path=self.home/'selective_enrichment.sqlite3'
        self.max_requests=max_requests
        self._jobs=queue.Queue(maxsize=8)
        self._worker=None
        self._worker_lock=threading.Lock()
        self.usage=UsageTracker(home=str(self.home))
        self.client=client
        # Sources are separate credentials, NOT separate allowances. A stable
        # shard chooses once, never failover/rotate on denial or throttling.
        self.clients=[]
        if client is None:
            for i in (1,2,3):
                key=os.environ.get('HELIUS_API_KEY_%d'%i)
                if i==1:key=key or os.environ.get('HELIUS_API_KEY')
                self.clients.append(('key%d'%i,HeliusParsedEventsClient(api_key=key,usage_store=self.usage.store,http_timeout=5) if key else None))
        else:self.clients=[('injected',client)]
        for _,candidate in self.clients:
            if isinstance(candidate,HeliusParsedEventsClient):candidate.account_home=str(self.home)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS admission (id INTEGER PRIMARY KEY, next_at REAL NOT NULL)')
            db.execute('INSERT OR IGNORE INTO admission VALUES (1,0)')
            db.execute('CREATE TABLE IF NOT EXISTS investigations (signature TEXT PRIMARY KEY, state TEXT NOT NULL, evidence TEXT, created REAL NOT NULL)')

    def _db(self):
        db=sqlite3.connect(str(self.path),timeout=.1)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def submit(self, signature, raw_tx):
        """Bounded optional background diagnostics, never block stream intake."""
        import copy
        with self._worker_lock:
            try:self._jobs.put_nowait((signature,copy.deepcopy(raw_tx)))
            except queue.Full:return {"status":"queue_full","decision_usable":False}
            if self._worker is None or not self._worker.is_alive():
                self._worker=threading.Thread(target=self._drain,daemon=True)
                self._worker.start()
        return {"status":"queued","decision_usable":False}

    def _drain(self):
        while True:
            try:signature,raw=self._jobs.get(timeout=1.)
            except queue.Empty:
                with self._worker_lock:
                    if self._jobs.empty():
                        self._worker=None
                        return
                continue
            try:self.investigate(signature,raw)
            except Exception:pass # durable claims remain unresolved, never rebilled
            finally:self._jobs.task_done()

    def investigate(self, signature, raw_tx):
        """One selective diagnostic, durably capped/deduplicated. Never a buy signal."""
        if not self.clients:return {'status':'unconfigured','decision_usable':False}
        label,client=self.clients[int.from_bytes(hashlib.sha256(signature.encode()).digest()[:8],'big')%len(self.clients)]
        if client is None or not client.configured():return {'status':'unconfigured','decision_usable':False}
        signatures=(raw_tx.get('transaction') or {}).get('signatures')
        if not isinstance(signatures,list) or not signatures or signatures[0] != signature:
            return {'status':'identity_mismatch','decision_usable':False}
        allowed,reason=self.usage.nonessential_allowed('helius_main','background',workload='complex_tx_parse',provider='helius')
        if not allowed:return {'status':'budget_refused','decision_usable':False,'reason':reason}
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT state,evidence FROM investigations WHERE signature=?',(signature,)).fetchone()
            if row:return json.loads(row[1]) if row[1] else {'status':'unresolved_claim','decision_usable':False}
            n=db.execute('SELECT count(*) FROM investigations').fetchone()[0]
            if n>=self.max_requests:return {'status':'hard_cap','decision_usable':False}
            next_at=db.execute('SELECT next_at FROM admission WHERE id=1').fetchone()[0]
            if time.time()<next_at:return {'status':'shared_cooldown','decision_usable':False}
            db.execute('UPDATE admission SET next_at=? WHERE id=1',(time.time()+1.,))
            db.execute('INSERT INTO investigations VALUES (?,?,?,?)',(signature,'claimed',None,time.time()))
        result=client.parse_transactions([signature],'decoder_unconfident')
        if result.get('kind')=='rate_limited':
            with self._db() as db:
                db.execute('UPDATE admission SET next_at=max(next_at,?) WHERE id=1',(time.time()+300.,))
        events=result.get('result') or []
        matches=[e for e in events if e.get('signature')==signature] if isinstance(events,list) else []
        verdict=verify_with_own_decoder(matches[0],raw_tx) if len(matches)==1 else None
        evidence={'status':'observed' if verdict else result.get('kind','unavailable'),
                  'signature':signature,'provider':'helius','credential_source':label,'billing_account':'helius_main','reason':'decoder_unconfident',
                  'decision_usable':False,'verification':verdict,
                  'kind':result.get('kind'),'credits':result.get('credits'),
                  'observed_at':time.time()}
        with self._db() as db:
            db.execute('UPDATE investigations SET state=?,evidence=? WHERE signature=?',('observed',json.dumps(evidence),signature))
        return evidence
