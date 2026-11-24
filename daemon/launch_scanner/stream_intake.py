"""Durable received log signatures and resolved candidate handoff.

Retained ACK IDs are bounded, pending work is never evicted. This cannot
recover stream messages never received while disconnected.
"""
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import tempfile
import time


class StreamIntake:
    def __init__(self,home,filename='logstream_intake.json'):
        self.path=Path(home)/'launch_scanner'/filename

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with open(str(self.path)+'.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            try:yield
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    def _read(self):
        try:data=json.loads(self.path.read_text())
        except FileNotFoundError:return {'v':1,'pending':{},'accepted':[]}
        except (OSError,ValueError) as exc:raise ValueError('stream intake unavailable') from exc
        if (not isinstance(data,dict) or type(data.get('v')) is not int or data['v']!=1
                or not isinstance(data.get('pending'),dict) or not isinstance(data.get('accepted'),list)
                or any(not isinstance(s,str) or not s for s in data['accepted'])
                or len(set(data['accepted']))!=len(data['accepted'])):
            raise ValueError('stream intake schema unavailable')
        for sig,rec in data['pending'].items():
            if (not isinstance(sig,str) or not sig or sig in data['accepted']
                    or not isinstance(rec,dict) or type(rec.get('ts')) not in (int,float)
                    or not math.isfinite(rec['ts']) or rec['ts']<=0
                    or ('mint' in rec and (not isinstance(rec['mint'],str) or not rec['mint']))):
                raise ValueError('stream intake record unavailable')
        return data

    def _save(self,data):
        fd,name=tempfile.mkstemp(dir=self.path.parent,prefix='.logstream-intake-')
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w') as out:
                json.dump(data,out,allow_nan=False);out.flush();os.fsync(out.fileno())
            os.replace(name,self.path)
            directory=os.open(self.path.parent,os.O_RDONLY)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)

    def receive(self,sig):
        if not isinstance(sig,str) or not sig:raise ValueError('invalid signature')
        with self._locked():
            data=self._read()
            if sig in data['accepted'] or sig in data['pending']:
                self._save(data)  # reconfirm an earlier post-replace fsync failure
                return False
            data['pending'][sig]={'ts':time.time()};self._save(data);return True

    def pending(self):
        with self._locked():return self._read()['pending']

    def resolve(self,sig,mint):
        if not isinstance(mint,str) or not mint:raise ValueError('invalid mint')
        with self._locked():
            data=self._read();rec=data['pending'][sig]
            if rec.get('mint') not in (None,mint):raise ValueError('conflicting mint')
            rec['mint']=mint;self._save(data)

    def defer(self,sig):
        with self._locked():
            data=self._read()
            if sig not in data['pending']:return
            rec=data['pending'].pop(sig);data['pending'][sig]=rec;self._save(data)

    def accept(self,signatures):
        with self._locked():
            data=self._read()
            for sig in signatures:
                rec=data['pending'].get(sig)
                if not rec or not rec.get('mint'):continue
                del data['pending'][sig]
                if sig not in data['accepted']:data['accepted'].append(sig)
            data['accepted']=data['accepted'][-5000:]
            self._save(data)
