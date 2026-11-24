"""Observed WS transport evidence, not provider billing or crash liveness.

Unclosed sessions are unresolved, never asserted active. Byte counters are
bytes returned/sent by the application socket, excluding TLS/IP overhead.
A process crash can lose the last uncommitted observation. No quota limit
or provider-billed bandwidth is inferred from these measurements.
"""
import os
import sqlite3
import time
import uuid


class TransportLedger:
    def __init__(self,home):
        home=os.path.abspath(os.path.expanduser(str(home or os.environ.get('FOREX_AGENT_HOME') or '~/.forex-agent')))
        self.path=os.path.join(home,'acquisition_ws_transport.sqlite')
        os.makedirs(home,exist_ok=True)
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY,provider TEXT NOT NULL,pid INTEGER NOT NULL,started REAL NOT NULL,updated REAL NOT NULL,stage TEXT NOT NULL,rx INTEGER NOT NULL,tx INTEGER NOT NULL,closed REAL)')
        os.chmod(self.path,0o600)

    def _db(self):
        db=sqlite3.connect(self.path,timeout=.1)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def begin(self,provider):
        sid='ws_'+uuid.uuid4().hex;now=time.time()
        with self._db() as db:
            db.execute('INSERT INTO sessions VALUES (?,?,?,?,?,\'attempt\',0,0,NULL)',(sid,provider,os.getpid(),now,now))
        return sid

    def observe(self,sid,rx=0,tx=0,stage=None,closed=False):
        if type(rx) is not int or type(tx) is not int or min(rx,tx)<0:
            raise ValueError('byte measurements require nonnegative integers')
        if stage is not None and stage not in ('transport_open','handshake_ok'):
            raise ValueError('unknown transport stage')
        now=time.time()
        with self._db() as db:
            if closed:
                db.execute('UPDATE sessions SET rx=rx+?,tx=tx+?,updated=?,closed=COALESCE(closed,?) WHERE id=?',(rx,tx,now,now,sid))
            elif stage:
                db.execute('UPDATE sessions SET rx=rx+?,tx=tx+?,updated=?,stage=? WHERE id=? AND closed IS NULL',(rx,tx,now,stage,sid))
            else:
                db.execute('UPDATE sessions SET rx=rx+?,tx=tx+?,updated=? WHERE id=? AND closed IS NULL',(rx,tx,now,sid))

    def summary(self):
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS observation_loss (id TEXT PRIMARY KEY,dropped INTEGER NOT NULL)')
            dropped=db.execute('SELECT COALESCE(sum(dropped),0) FROM observation_loss').fetchone()[0]
            rows=db.execute('SELECT provider,count(*),sum(rx),sum(tx),sum(closed IS NULL),sum(stage=\'handshake_ok\') FROM sessions GROUP BY provider').fetchall()
        return {'known_dropped_observations':dropped,'measurement':'application_socket_bytes_not_provider_billing','completeness':'best_effort_may_drop_queued_observations','unclosed_semantics':'unresolved_not_verified_active',
                'providers':{r[0]:{'attempts':r[1],'observed_rx_bytes':r[2],'observed_tx_bytes':r[3],'unresolved_sessions':r[4],'accepted_handshakes':r[5]} for r in rows}}


class AsyncTransportMeter:
    """Bounded best-effort observation queue. Never waits for disk/network.

    Queue overflow/storage failure drops measurements and is reported as such.
    Shutdown/crash may lose queued bytes; this is not billing evidence.
    """
    def __init__(self,home):
        import queue,threading
        self.home=os.path.abspath(os.path.expanduser(str(home or os.environ.get('FOREX_AGENT_HOME') or '~/.forex-agent')))
        self.queue=queue.Queue(maxsize=256)
        self.dropped=0
        self.stopping=False
        self.stop_event=threading.Event()
        self.sid='ws_'+uuid.uuid4().hex
        self.worker=threading.Thread(target=self._run,daemon=True,name='ws-transport-meter')
        self.worker.start()

    def begin(self,provider):
        self._enqueue(('begin',provider))
        return self.sid

    def _enqueue(self,item):
        import queue
        if self.stop_event.is_set():
            self.dropped+=1
            return
        try:self.queue.put_nowait(item)
        except queue.Full:self.dropped+=1

    def observe(self,sid,**kwargs):
        self._enqueue(('observe',kwargs))
        if kwargs.get('closed'):self.stop()

    def stop(self):
        self.stopping=True
        self.stop_event.set()

    def _run(self):
        import queue,logging
        ledger=None;actual=None
        while True:
            try:kind,value=self.queue.get(timeout=.1)
            except queue.Empty:
                if self.stop_event.is_set():break
                continue
            try:
                if kind=='begin':ledger=TransportLedger(self.home);actual=ledger.begin(value)
                elif ledger is not None and actual is not None:ledger.observe(actual,**value)
                else:self.dropped+=1
            except Exception:
                self.dropped+=1
                logging.getLogger(__name__).warning('WS transport observations lost; measurement incomplete')
            finally:self.queue.task_done()
            if self.stop_event.is_set() and self.queue.empty():break
        if ledger is not None:
            try:
                with ledger._db() as db:
                    db.execute('CREATE TABLE IF NOT EXISTS observation_loss (id TEXT PRIMARY KEY,dropped INTEGER NOT NULL)')
                    db.execute('INSERT OR REPLACE INTO observation_loss VALUES (?,?)',(self.sid,self.dropped))
            except Exception:
                logging.getLogger(__name__).warning('WS observation loss count not persisted; measurement incomplete')
