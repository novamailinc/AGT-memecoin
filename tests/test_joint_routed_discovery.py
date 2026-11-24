"""Synthetic offline transport to actual routed durable discovery/held candidate.

No sockets, provider entitlements, successful trade or full run_once is proved.
"""
import json
from types import SimpleNamespace
from tests.test_paper_account_instances import account
from daemon.intelligence.acquisition import routine,chainstack_workload
from daemon.intelligence.acquisition.provider_router import ProviderRouter,ROUTING_MODE_REVISED
from daemon.launch_scanner import LaunchScanner,logstream,smartmoney
from config.config import DegenConfig
from storage.store import Store


def test_three_routed_transports_to_durable_held_candidate(account,tmp_path,monkeypatch):
    import daemon.launch_scanner as module
    home=tmp_path/'runtime';monkeypatch.setenv('FOREX_AGENT_HOME',str(home))
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-alchemy')
    monkeypatch.setenv('HELIUS_API_KEY_1','fixture-helius')
    monkeypatch.setenv('CHAINSTACK_ENDPOINT','https://solana-mainnet.core.chainstack.com/fixture-chainstack')
    mint='SyntheticMint111111111111111111111111111111'
    sig='synthetic-signature'
    tx={'slot':77,'blockTime':1795470000,'version':'legacy',
        'transaction':{'signatures':[sig],'message':{'accountKeys':['payer'],
            'instructions':[{'programId':'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                'parsed':{'type':'initializeMint2','info':{'mint':mint}}}]}},
        'meta':{'err':None,'fee':5000,'preBalances':[10000],'postBalances':[5000],
                'preTokenBalances':[],'postTokenBalances':[],'innerInstructions':[]}}
    calls=[]
    class Response:
        status=200
        def read(self):return json.dumps({'jsonrpc':'2.0','id':1,'result':tx}).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    def transport(req,timeout=None):
        calls.append((req.full_url,json.loads(req.data)['method']))
        return Response()
    r=ProviderRouter(home=str(home),transport=transport,helius_enabled=True,mode=ROUTING_MODE_REVISED)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    ls=logstream.LogStream(intake_home=home)
    ls._handle_message(json.dumps({'method':'logsNotification','params':{'result':{'value':{'err':None,'signature':sig}}}}))
    found=ls.drain();assert len(found)==1 and found[0]['mint']==mint
    assert found[0]['acquisition_evidence']['provider']=='alchemy'
    assert smartmoney._rpc('getTransaction',[sig])==tx
    assert routine.evidence_since()[-1]['provider']=='helius'
    verification=chainstack_workload.verify_transaction(r,sig,tx,recent=True)
    assert verification['status']=='match' and verification['provider']=='chainstack'
    assert ['alchemy.com' in calls[0][0],'helius-rpc.com' in calls[1][0],'chainstack.com' in calls[2][0]]==[True]*3
    # New stream object has no in-memory message but durable intake reappears.
    ls=logstream.LogStream(intake_home=home)
    assert ls.drain()==found
    store=Store(tmp_path/'scanner.db');monkeypatch.setattr(module,'get_store',lambda:store)
    scanner=LaunchScanner.__new__(LaunchScanner);scanner._logstream=ls
    state={'seen':{},'degen_positions':{}}
    d=DegenConfig();d.degen_lane_enabled=False;d.shadow_enabled=False;d.early_warn_enabled=False
    scanner._drain_logstream(SimpleNamespace(degen=d),None,state)
    assert mint in state['candidates'] and not state['degen_positions']
    assert ls.drain()==[]
    rows=store.journal_query(kind='daemon_state',symbol=scanner.name)
    assert mint in rows[0]['detail']['candidates']
    assert rows[0]['detail']['logstream_committed_refs']==[sig]
    assert account.snapshot()['cash']==10.
    scanner._drain_logstream(SimpleNamespace(degen=d),None,state)
    assert len(state['candidates'])==1 and account.snapshot()['cash']==10.
    # Continue the exact durable routed candidate, not a separately seeded one.
    from daemon.launch_scanner import sources,safety
    cand=state['candidates'][mint]
    d.fingerprints_enabled=False;d.sentiment_enabled=False;d.lowcap_enabled=False
    d.backtest_enabled=False;d.confirm_unvetted_alerts=False
    screens=[]
    def unsafe_payload(url,timeout):
        screens.append(url)
        return {'rugged':False,'mintAuthority':'unsafe-authority','score_normalised':90}
    monkeypatch.setattr(safety,'_get_json',unsafe_payload)
    adapter=SimpleNamespace(submit_order=lambda *a,**kw:(_ for _ in ()).throw(AssertionError('unsafe submit')))
    for cap in (12000,16000,22000,30000):
        stats={'usd_market_cap':cap,'price_usd':cap/1000000.,'usd_liquidity':25000.,
            'vol_m5':8000.,'buys_m5':30,'sells_m5':6,'symbol':'SYN','name':'Synthetic'}
        cand['stats_ts']=0
        monkeypatch.setattr(sources,'dexscreener_batch_stats',lambda *a,stats=stats,**kw:{mint:stats})
        monkeypatch.setattr(sources,'dexscreener_token_stats',lambda *a,stats=stats,**kw:stats)
        scanner._judge(SimpleNamespace(degen=d),adapter,state)
    assert screens and mint not in state['candidates']
    assert not state['degen_positions'] and account.snapshot()['cash']==10.


def test_actual_judge_and_safety_transport_hardfail_cannot_enter(account,tmp_path,monkeypatch):
    from daemon.launch_scanner import runner,sources,safety
    import time
    monkeypatch.setenv('FOREX_AGENT_HOME',str(tmp_path/'runtime'))
    d=DegenConfig();d.degen_lane_enabled=False;d.shadow_enabled=False;d.early_warn_enabled=False
    d.fingerprints_enabled=False;d.sentiment_enabled=False;d.lowcap_enabled=False
    d.backtest_enabled=False;d.confirm_unvetted_alerts=False
    mint='SyntheticDangerMint';now=time.time()
    cand=runner.new_candidate(mint,mint,'DANGER','Danger','solana','pumpfun-log','logstream','u',now-120)
    state={'seen':{mint:{'first_ts':now-120,'alerted':False,'symbol':'DANGER'}},
        'candidates':{mint:cand},'degen_positions':{}}
    base={'usd_liquidity':25000.,'vol_m5':8000.,'buys_m5':30,'sells_m5':6,'symbol':'DANGER','name':'Danger'}
    seen=[]
    def report(url,timeout):
        seen.append(url)
        return {'rugged':False,'mintAuthority':'unsafe-authority','score_normalised':90}
    monkeypatch.setattr(safety,'_get_json',report)
    def forbidden(*a,**kw):raise AssertionError('hardfail must never submit')
    adapter=SimpleNamespace(submit_order=forbidden)
    scanner=LaunchScanner.__new__(LaunchScanner)
    for i,cap in enumerate((12000,16000,22000,30000)):
        stats=dict(base,usd_market_cap=cap,price_usd=cap/1000000.)
        cand['stats_ts']=0
        monkeypatch.setattr(sources,'dexscreener_batch_stats',lambda *a,stats=stats,**kw:{mint:stats})
        monkeypatch.setattr(sources,'dexscreener_token_stats',lambda *a,stats=stats,**kw:stats)
        scanner._judge(SimpleNamespace(degen=d),adapter,state)
    assert seen and mint not in state['candidates']
    assert not state['degen_positions'] and account.snapshot()['cash']==10.
