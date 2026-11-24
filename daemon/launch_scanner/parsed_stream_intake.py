"""Parsed notifications -> raw-verified durable scanner detections.

A notification is a hint, never a substitute for getTransaction. Uses the
existing StreamIntake/LogStream drain-then-ACK contract; failed/conflicting
comparisons remain unresolved and never reach the candidate handoff.
"""
import copy
import os
from pathlib import Path
from .stream_intake import StreamIntake
from .logstream import extract_mint
from .tx_guards import tx_succeeded
from daemon.intelligence.acquisition import routine, helius_streams
from daemon.intelligence.acquisition.chainstack_workload import TX_FETCH_OPTIONS


class ParsedStreamIntake(StreamIntake):
    def __init__(self, home=None, reader=None):
        super().__init__(home or os.environ.get('FOREX_AGENT_HOME',Path.home()/'.forex-agent'),
                         filename='parsed_stream_intake.json')
        self.reader=reader or routine.call

    def receive_event(self,event):
        if not isinstance(event,dict) or event.get('status')!='ok':
            return False
        sig=event.get('signature')
        if not isinstance(sig,str) or not sig or type(event.get('slot')) is not int:
            return False
        with self._locked():
            data=self._read()
            if sig in data['accepted'] or sig in data['pending']:
                self._save(data)
                return False
            import time
            data['pending'][sig]={'ts':time.time(),'parsed_event':copy.deepcopy(event)}
            self._save(data)
        return True

    def _resolve_pending(self,sig,rec):
        event=rec.get('parsed_event')
        # Old/malformed pending records cannot turn into unverified candidates.
        if not isinstance(event,dict) or event.get('signature')!=sig:
            return
        result=self.reader('getTransaction',[sig,copy.deepcopy(TX_FETCH_OPTIONS)],
                           hints={'purpose':'normal_scanning'})
        if not isinstance(result,dict) or result.get('decision_usable') is not True:
            return
        raw=result.get('result')
        if not isinstance(raw,dict) or not tx_succeeded(raw):
            return
        signatures=(raw.get('transaction') or {}).get('signatures')
        if not isinstance(signatures,list) or not signatures or signatures[0]!=sig:
            return
        verified=helius_streams.verify_with_own_decoder(event,raw)
        if verified.get('verification_status')!='verified':
            return
        mint=extract_mint(raw)
        if not mint:
            return
        evidence={k:copy.deepcopy(result.get(k)) for k in (
            'provider','kind','attempts','fallback','fallback_difference','cached',
            'decision_usable','degraded','completeness','note','credential_source','account')}
        evidence.update(recorded=True,method='getTransaction',
                        parsed_stream={'signature':sig,'slot':event['slot'],
                                       'verification_status':'verified',
                                       'source':'helius_parsed_stream'})
        self.resolve(sig,mint,acquisition_evidence=evidence)

    def drain(self):
        # Same bounded per-pass pending-detail batch as LogStream._retry_pending.
        for sig,rec in [(s,r) for s,r in self.pending().items() if not r.get('mint')][:10]:
            self._resolve_pending(sig,rec)
        return [dict(rec,signature=sig) for sig,rec in self.pending().items() if rec.get('mint')]

    def acknowledge(self,signatures):
        self.accept(signatures)
