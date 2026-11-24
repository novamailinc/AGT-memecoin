import json
from pathlib import Path
from daemon.intelligence.acquisition.helius_streams import normalize_parsed_transaction,verify_with_own_decoder

def fixture():
    return json.loads((Path(__file__).parent/'fixtures/provider_joint/finalized-pump-transaction.json').read_text())

def test_actual_current_parsed_events_envelope():
    data=fixture();event=normalize_parsed_transaction(data['helius'][0])
    assert event['signature']==data['raw']['transaction']['signatures'][0]
    assert event['slot']==data['raw']['slot']
    assert event['status']=='error' # real reverted transaction must not become ok
    assert event['verification_status']=='parsed_unverified'
    assert event['fee']==data['raw']['meta']['fee']
    verdict=verify_with_own_decoder(event,data['raw'])
    assert verdict['verification_status']=='verified'
    assert verdict['status']=='error' # agreement never repairs a reverted execution

def test_item_error_not_normalized_as_decoded():
    assert normalize_parsed_transaction({'signature':'sig','parserStatus':'ERROR','parserError':{'code':'transaction_not_found'}}) is None

def test_unknown_execution_status_stays_unknown():
    x=fixture()['helius'][0];x['parsed']['transactionStatus']='FUTURE_STATUS'
    e=normalize_parsed_transaction(x)
    assert e['status']=='unknown'
    assert not e['execution_status_known']

class Client:
    def __init__(self,event):self.event=event;self.calls=0
    def configured(self):return True
    def parse_transactions(self,sigs,reason):
        assert reason=='decoder_unconfident'
        self.calls+=1
        return {'kind':'ok','result':[self.event],'credits':10}

def test_durable_dedup_and_cap(tmp_path):
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    d=fixture();e=normalize_parsed_transaction(d['helius'][0]);c=Client(e)
    s=SelectiveEnrichment(tmp_path,client=c,max_requests=1)
    sig=e['signature'];a=s.investigate(sig,d['raw'])
    assert not a['decision_usable'] and a['verification']['status']=='error'
    assert s.investigate(sig,d['raw'])==a
    assert SelectiveEnrichment(tmp_path,client=c,max_requests=1).investigate(sig,d['raw'])==a
    assert c.calls==1
    import copy
    raw=copy.deepcopy(d['raw']);raw['transaction']['signatures']=['another']
    assert s.investigate('another',raw)['status']=='hard_cap'
    assert c.calls==1

def test_actual_logstream_consumer_never_promotes_enrichment(tmp_path,monkeypatch):
    from daemon.launch_scanner.logstream import LogStream
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    import copy
    d=fixture();raw=copy.deepcopy(d['raw']);raw['meta']['err']=None
    raw['transaction']['message']['instructions']=[];raw['meta']['innerInstructions']=[]
    raw['meta']['postTokenBalances']=[]
    event=normalize_parsed_transaction(d['helius'][0]);c=Client(event)
    ls=LogStream(intake_home=tmp_path);ls._http_rpc=lambda *args:raw
    ls._selective_enricher=SelectiveEnrichment(tmp_path,client=c)
    assert ls._mint_for_signature(event['signature']) is None
    ls._selective_enricher._jobs.join()
    assert c.calls==1
    assert ls._last_enrichment['status']=='queued'
    assert ls._last_enrichment['decision_usable'] is False

def test_reverted_raw_never_calls_enrichment(tmp_path):
    from daemon.launch_scanner.logstream import LogStream
    ls=LogStream(intake_home=tmp_path);d=fixture();ls._http_rpc=lambda *args:d['raw']
    class Bomb:
        def investigate(self,*a):raise AssertionError('must not enrich failed execution')
    ls._selective_enricher=Bomb()
    assert ls._mint_for_signature(d['helius'][0]['signature']) is None

def test_three_keys_one_account_cooldown_no_rotation(tmp_path,monkeypatch):
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    import copy
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-key-%d'%i)
    s=SelectiveEnrichment(tmp_path)
    seen=[]
    for label,client in s.clients:
        def response(*a,label=label):
            seen.append(label);return {'kind':'rate_limited','result':None,'credits':None}
        client.parse_transactions=response
    d=fixture();sig=d['raw']['transaction']['signatures'][0]
    first=s.investigate(sig,d['raw'])
    assert first['kind']=='rate_limited' and len(seen)==1
    assert first['billing_account']=='helius_main'
    raw=copy.deepcopy(d['raw']);raw['transaction']['signatures']=['different']
    assert s.investigate('different',raw)['status']=='shared_cooldown'
    assert len(seen)==1
    assert s.investigate(sig,d['raw'])==first
    assert len(seen)==1

def test_unresolved_claim_never_automatically_rebilled(tmp_path):
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    d=fixture();c=Client(normalize_parsed_transaction(d['helius'][0]));s=SelectiveEnrichment(tmp_path,client=c)
    sig=d['raw']['transaction']['signatures'][0]
    with s._db() as db:db.execute('INSERT INTO investigations VALUES (?,?,?,?)',(sig,'claimed',None,1))
    assert s.investigate(sig,d['raw'])['status']=='unresolved_claim'
    assert c.calls==0


def test_sparse_actual_event_cannot_claim_verification():
    d=fixture();e=normalize_parsed_transaction(d['helius'][0])
    for k in ('slot','fee','instructions'):e.pop(k)
    assert verify_with_own_decoder(e,d['raw'])['verification_status']=='unverifiable'

def test_slow_optional_transport_does_not_stall_submit(tmp_path):
    import threading,time
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    entered=threading.Event();release=threading.Event()
    class Slow(Client):
        def parse_transactions(self,*args):
            entered.set();release.wait(2)
            return super().parse_transactions(*args)
    d=fixture();s=SelectiveEnrichment(tmp_path,client=Slow(normalize_parsed_transaction(d['helius'][0])))
    sig=d['raw']['transaction']['signatures'][0]
    try:
        start=time.monotonic();assert s.submit(sig,d['raw'])['status']=='queued'
        assert time.monotonic()-start<.25
        assert entered.wait(1)
        for i in range(8):assert s.submit(sig,d['raw'])['status']=='queued'
        assert s.submit(sig,d['raw'])['status']=='queue_full'
    finally:release.set();s._jobs.join()

def test_actual_stream_wrapper_retains_top_level_instructions():
    from daemon.intelligence.acquisition.helius_streams import parse_notification
    p=Path(__file__).parent/'fixtures/provider_joint/confirmed-pump-stream-event.json'
    msg=json.loads(p.read_text());kind,e=parse_notification(msg)
    assert kind=='notification'
    assert e['signature']==msg['params']['result']['value']['transaction']['signature']
    assert e['instructions']==msg['params']['result']['value']['instructions']
    assert len(e['instructions'])>0
    assert e['verification_status']=='parsed_unverified'


def test_malformed_instruction_program_identity_unverifiable():
    d=fixture();e=normalize_parsed_transaction(d['helius'][0])
    for instructions in ([{}],[None],[{'programId':''}],[{'programId':3}]):
        e['instructions']=instructions
        assert verify_with_own_decoder(e,d['raw'])['verification_status']=='unverifiable'

def test_worker_exits_then_restarts_without_orphaned_jobs(tmp_path):
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    import time
    d=fixture();c=Client(normalize_parsed_transaction(d['helius'][0]));s=SelectiveEnrichment(tmp_path,client=c)
    sig=d['raw']['transaction']['signatures'][0]
    s.submit(sig,d['raw']);s._jobs.join()
    first=s._worker
    first.join(timeout=2)
    assert not first.is_alive()
    assert s.submit(sig,d['raw'])['status']=='queued'
    s._jobs.join()
    assert c.calls==1 # durable dedup survives worker lifetime
    assert s._jobs.qsize()==0

def test_scanner_unresolved_intake_recovers_preclaim_queue_loss(tmp_path):
    from daemon.launch_scanner.logstream import LogStream
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    import copy
    d=fixture();raw=copy.deepcopy(d['raw']);raw['meta']['err']=None
    raw['transaction']['message']['instructions']=[];raw['meta']['innerInstructions']=[];raw['meta']['postTokenBalances']=[]
    sig=raw['transaction']['signatures'][0]
    first=LogStream(intake_home=tmp_path)
    assert first._intake.receive(sig)
    class LostBeforeClaim:
        def submit(self,*a):return {'status':'queued','decision_usable':False}
    first._selective_enricher=LostBeforeClaim();first._http_rpc=lambda *a:raw
    assert first._mint_for_signature(sig) is None
    # Rebuild objects as after process loss. Only durable intake survives.
    second=LogStream(intake_home=tmp_path)
    client=Client(normalize_parsed_transaction(d['helius'][0]))
    second._selective_enricher=SelectiveEnrichment(tmp_path,client=client)
    second._http_rpc=lambda *a:raw
    second._retry_pending();second._selective_enricher._jobs.join()
    assert client.calls==1
    assert sig in second._intake.pending() # unresolved, never autoaccepted
    assert second.drain()==[] # no fabricated candidate from enrichment
    second._last_retry_ts=0
    second._last_rpc_ts=0
    second._retry_pending();second._selective_enricher._jobs.join()
    assert client.calls==1 # observed diagnostics durable, no repeat billing
