"""Durable wallet acquisition cursors and pending transaction signatures.

Cursor advances only in the same atomic snapshot as queued signatures.
Acknowledgement is explicit after downstream processing. Delivery is at
least once across a crash after processing but before acknowledgement.
Never silently discards pending work to satisfy a size bound.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time


class WalletIntake:
    def __init__(self, home):
        self.path = Path(home)/'launch_scanner'/'wallet_intake.json'

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path)+'.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try: yield
            finally: fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self):
        try: d=json.loads(self.path.read_text())
        except FileNotFoundError: return {'v':1,'wallets':{}}
        except (OSError,ValueError) as exc: raise ValueError('intake unavailable') from exc
        if not isinstance(d,dict) or type(d.get('v')) is not int or d['v']!=1 or not isinstance(d.get('wallets'),dict):
            raise ValueError('intake schema unavailable')
        for wallet, rec in d['wallets'].items():
            if (not isinstance(wallet,str) or not isinstance(rec,dict)
                    or not isinstance(rec.get('cursor'),str) or not rec['cursor']
                    or not isinstance(rec.get('pending'),list)
                    or any(not isinstance(s,str) or not s for s in rec['pending'])
                    or len(set(rec['pending']))!=len(rec['pending'])):
                raise ValueError('intake record unavailable')
        return d

    def _save(self,d):
        fd,name=tempfile.mkstemp(dir=self.path.parent,prefix='.wallet-intake-')
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w') as f:
                json.dump(d,f,allow_nan=False);f.flush();os.fsync(f.fileno())
            os.replace(name,self.path)
            directory=os.open(self.path.parent,os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(name):os.unlink(name)

    def wallet(self,wallet):
        with self._locked():
            return self._read()['wallets'].get(wallet)

    def stage(self,wallet,expected_cursor,new_cursor,signatures,gap=None):
        """Compare-and-stage new signatures oldest first, then advance.
        expected_cursor=None only bootstraps a genuinely missing wallet.
        gap is a diagnostic coverage limit, never a claim of completeness.
        """
        if (not isinstance(wallet,str) or not wallet or not isinstance(new_cursor,str)
                or not new_cursor or not isinstance(signatures,list)
                or any(not isinstance(s,str) or not s for s in signatures)):
            raise ValueError('invalid acquisition identity')
        with self._locked():
            d=self._read();old=d['wallets'].get(wallet)
            if (old or {}).get('cursor')!=expected_cursor:
                raise ValueError('cursor changed during page fetch')
            pending=list((old or {}).get('pending') or [])
            for sig in signatures:
                if sig not in pending:pending.append(sig)
            rec=dict(old or {},cursor=new_cursor,pending=pending)
            if gap:
                rec['coverage_gap']={'reason':str(gap),'ts':time.time()}
            d['wallets'][wallet]=rec;self._save(d)

    def acknowledge(self,wallet,signature):
        with self._locked():
            d=self._read();rec=d['wallets'].get(wallet)
            if not rec or signature not in rec['pending']:return False
            rec['pending'].remove(signature)
            self._save(d);return True

    def defer(self,wallet,signature):
        """Rotate a failed attempt so poison details cannot starve later work."""
        with self._locked():
            d=self._read();rec=d['wallets'].get(wallet)
            if not rec or signature not in rec['pending']:return
            rec['pending'].remove(signature);rec['pending'].append(signature)
            self._save(d)
