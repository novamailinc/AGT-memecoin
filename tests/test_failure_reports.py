import json
import sqlite3
import pytest
from daemon.failure_reports import FailureOutbox,exception_details


class TestFailureOutbox:
    def test_restart_retry_requires_authenticated_bound_receipt(self,tmp_path):
        clock=[100.];path=tmp_path/'reports.sqlite';q=FailureOutbox(path,now=lambda:clock[0])
        rid=q.record('scanner','provider.unavailable',context={'api_key':'sensitive','url':'https://example.invalid/key','safe':'account unavailable'})
        sent=[]
        def transport(env):
            sent.append(env)
            return {'report_id':rid,'sha256':env['sha256'],'receiver':'instinct-candidate','state':'received'}
        assert not q.deliver_one(transport,lambda r:False,'instinct-candidate')['received']
        assert q.status(rid)['state']=='pending'
        q=FailureOutbox(path,now=lambda:clock[0]);clock[0]+=5
        assert q.deliver_one(transport,lambda r:True,'instinct-candidate')['received']
        assert sent[0]==sent[1] and q.status(rid)['attempts']==2
        assert q.deliver_one(transport,lambda r:True,'instinct-candidate') is None
        assert sent[0]['report']['context']=={}


    @pytest.mark.parametrize('field,value',[('report_id','other'),('sha256','wrong'),('receiver','other'),('state','queued')])
    def test_wrong_ack_never_counts_as_received(self,tmp_path,field,value):
        q=FailureOutbox(tmp_path/'r.sqlite');rid=q.record('scanner','test.failure')
        def transport(env):
            r={'report_id':rid,'sha256':env['sha256'],'receiver':'target','state':'received'};r[field]=value;return r
        assert not q.deliver_one(transport,lambda r:True,'target')['received']
        assert q.status(rid)['state']=='pending'


    def test_receiver_failure_is_durable_and_exception_payload_is_not_copied(self,tmp_path):
        q=FailureOutbox(tmp_path/'r.sqlite')
        try:raise ValueError('unlabelled secret material')
        except ValueError as exc:rid=q.record('scanner','test.failure',exc=exc)
        def fail(env):raise OSError('remote sensitive response')
        assert not q.deliver_one(fail,lambda r:True,'target')['received']
        with sqlite3.connect(q.path) as db:body=db.execute('SELECT body FROM reports').fetchone()[0]
        assert 'unlabelled secret' not in body and 'remote sensitive' not in body
        detail=json.loads(body)['exception']
        assert detail['exception_type']=='ValueError' and detail['frames'][-1]['line']>0
        assert q.status(rid)['state']=='pending'


    def test_abrupt_sender_claim_expires_and_replays_same_id(self,tmp_path):
        clock=[100.];q=FailureOutbox(tmp_path/'r.sqlite',now=lambda:clock[0]);rid=q.record('scanner','test.failure')
        def die(env):raise KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):q.deliver_one(die,lambda r:True,'target')
        assert q.status(rid)['state']=='inflight'
        q=FailureOutbox(q.path,now=lambda:clock[0])
        assert q.deliver_one(lambda e:None,lambda r:True,'target') is None
        clock[0]+=61
        def ok(env):return {'report_id':rid,'sha256':env['sha256'],'receiver':'target','state':'received','instructions':'not retained'}
        assert q.deliver_one(ok,lambda r:True,'target')['received']
        assert 'instructions' not in q.status(rid)['receipt']


    @pytest.mark.parametrize('newer_state',['failed','inflight'])
    def test_late_verified_ack_is_monotonic_after_newer_attempt(self,tmp_path,newer_state):
        clock=[100.];q=FailureOutbox(tmp_path/'r.sqlite',now=lambda:clock[0]);rid=q.record('scanner','test.failure')
        newer=FailureOutbox(q.path,now=lambda:clock[0])
        def old_transport(env):
            clock[0]+=61
            def new_transport(_):
                if newer_state=='inflight':raise KeyboardInterrupt()
                return None
            if newer_state=='inflight':
                with pytest.raises(KeyboardInterrupt):newer.deliver_one(new_transport,lambda r:True,'target')
            else:newer.deliver_one(new_transport,lambda r:True,'target')
            return {'report_id':rid,'sha256':env['sha256'],'receiver':'target','state':'received'}
        result=q.deliver_one(old_transport,lambda r:True,'target')
        assert result['received'] is True
        assert q.status(rid)['state']=='received' and q.status(rid)['receipt']


    def test_arbitrary_context_text_is_omitted_not_guessed_safe(self,tmp_path):
        q=FailureOutbox(tmp_path/'r.sqlite')
        rid=q.record('scanner','test.failure',context={'note':'Bearer sensitive','authorization':'value','safe':'mnemonic material','private':'private key material','provider':'alchemy','attempt':2,'slot':'uncontrolled text'})
        with sqlite3.connect(q.path) as db:body=json.loads(db.execute('SELECT body FROM reports').fetchone()[0])
        assert body['context']=={'provider':'alchemy','attempt':2}


    @pytest.mark.parametrize('value',[float('inf'),float('-inf'),float('nan'),-1,10**100])
    def test_invalid_optional_numbers_do_not_lose_core_report(self,tmp_path,value):
        q=FailureOutbox(tmp_path/'r.sqlite')
        rid=q.record('scanner','test.failure',context={'gap_seconds':value,'attempt':value,'http_status':value,'slot':value,'rpc_code':value})
        assert q.status(rid)['state']=='pending'
        with sqlite3.connect(q.path) as db:body=json.loads(db.execute('SELECT body FROM reports').fetchone()[0])
        # Negative RPC codes are valid but other invalid diagnostics are omitted.
        assert set(body['context']).issubset({'rpc_code'})


