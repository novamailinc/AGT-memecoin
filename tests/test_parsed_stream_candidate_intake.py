"""Offline parsed-event -> actual raw decoder -> durable candidate intake."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from daemon.launch_scanner.parsed_stream_intake import ParsedStreamIntake
from daemon.intelligence.acquisition import helius_streams as hs
from daemon.intelligence.decoders.solana_transaction import decode_transaction

class CandidateIntake(unittest.TestCase):
    def setUp(self):
        self.home=tempfile.TemporaryDirectory();self.addCleanup(self.home.cleanup)
        self.raw=json.load(open('tests/data/intelligence/acquisition/live_getTransaction_pump.json'))['result']
        # SYNTHETIC success variant of a captured FAILED transaction.
        # This is a fixture, never claimed as successful live evidence.
        self.raw['meta']['err']=None
        self.raw['meta']['postTokenBalances']=[{'mint':'FixtureMintForIntake'}]
        self.raw['meta']['preTokenBalances']=[]
        own=decode_transaction(self.raw)
        self.event={'signature':own['signature'],'slot':own['slot'],'fee':own['meta']['fee'],
                    'status':'ok','instructions':[{'programId':p} for p in own['program_ids']],
                    'verification_status':'parsed_unverified'}
        self.reader=MagicMock(return_value={'kind':'ok','provider':'alchemy','decision_usable':True,'result':self.raw})
        self.q=ParsedStreamIntake(self.home.name,reader=self.reader)
    def test_verified_raw_mint_durable_until_ack(self):
        self.assertEqual(hs.verify_with_own_decoder(self.event,self.raw)['verification_status'],'verified')
        self.assertTrue(self.q.receive_event(self.event))
        rows=self.q.drain();self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['acquisition_evidence']['parsed_stream']['verification_status'],'verified')
        self.assertEqual(self.reader.call_args.kwargs['hints'],{'purpose':'normal_scanning'})
        self.assertEqual(self.reader.call_args.args[1][1]['maxSupportedTransactionVersion'],1)
        q2=ParsedStreamIntake(self.home.name,reader=self.reader)
        self.assertEqual(len(q2.drain()),1)
        q2.acknowledge([self.event['signature']]);self.assertEqual(q2.drain(),[])
        self.assertFalse(q2.receive_event(self.event))
    def test_conflict_cannot_create_candidate_even_after_restart(self):
        self.event['fee']+=1;self.q.receive_event(self.event)
        self.assertEqual(self.q.drain(),[])
        self.assertEqual(ParsedStreamIntake(self.home.name,reader=self.reader).drain(),[])
        self.assertNotIn('mint',self.q.pending()[self.event['signature']])
    def test_failed_or_identity_mismatch_raw_cannot_create_candidate(self):
        self.q.receive_event(self.event)
        self.raw['meta']['err']={'InstructionError':[0,'fixture']}
        self.assertEqual(self.q.drain(),[])
        self.raw['meta']['err']=None;self.raw['transaction']['signatures'][0]='different'
        self.assertEqual(self.q.drain(),[])
    def test_error_then_restart_rechecks_parsed_evidence(self):
        self.q.receive_event(self.event);self.reader.return_value={'kind':'error','decision_usable':False}
        self.assertEqual(self.q.drain(),[])
        self.reader.return_value={'kind':'ok','provider':'alchemy','decision_usable':True,'result':self.raw}
        q2=ParsedStreamIntake(self.home.name,reader=self.reader)
        self.assertEqual(len(q2.drain()),1)
    def test_notification_failure_never_queued(self):
        self.event['status']='error';self.assertFalse(self.q.receive_event(self.event));self.reader.assert_not_called()

if __name__=='__main__':unittest.main()
