"""Synthetic market/safety transports, real judge/safety/gate/veto/native entry.

No live market/strategy advantage, full run_once or AGT qualification.
"""
import time
import pytest
from types import SimpleNamespace
from tests.test_paper_account_instances import account
from tests.test_broker_memecoin import _adapter,FakeMarket
from daemon.launch_scanner import LaunchScanner,runner,sources,safety,killswitch,bundle,deployers
from storage.store import Store
from config.config import DegenConfig


@pytest.mark.parametrize('cycle_mode',['segments','run_once','fresh_process','run_forever','abrupt_entry'])
def test_actual_judge_safe_payload_to_shared_native_entry(account,tmp_path,monkeypatch,cycle_mode):
    import socket,threading
    threads_before={t.ident for t in threading.enumerate()}
    attempts=[]
    def denied(*a,**kw):
        attempts.append('network attempt')
        raise AssertionError('fixture must not use live network')
    for name in ('create_connection','getaddrinfo'):
        monkeypatch.setattr(socket,name,denied)
    for name in ('connect','connect_ex'):
        monkeypatch.setattr(socket.socket,name,denied)
    from daemon.launch_scanner import social_radar
    monkeypatch.setattr(social_radar,'_polite_get',lambda *a,**kw:None)
    monkeypatch.setenv('FOREX_AGENT_HOME',str(tmp_path/'runtime'))
    (tmp_path/'runtime'/'launch_scanner').mkdir(parents=True)
    for name in ('STATE_FILE','SHADOW_FILE','LOCK_FILE'):
        monkeypatch.setattr(killswitch,name,str(tmp_path/'killswitch'/name))
    # Explicit unavailable-RPC seams, no gate/verdict/veto replacements.
    monkeypatch.setattr(bundle,'_rpc',lambda *a,**kw:None)
    monkeypatch.setattr(deployers,'_rpc',lambda *a,**kw:None)
    monkeypatch.setattr(safety,'_get_json',lambda *a,**kw:{'rugged':False,'score_normalised':90})
    from daemon.telegram import watchlist
    defaults=tmp_path/'watch-defaults.yaml'
    defaults.write_text('symbols:\n  - SOL\nmemecoin:\n  symbols:\n    SOL: "So11111111111111111111111111111111111111112"\n')
    monkeypatch.setattr(watchlist,'_defaults_path',lambda:str(defaults))
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});mint=a._market.mint_for('BONK');now=time.time()
    store=Store(tmp_path/'scanner.db');sc=LaunchScanner.__new__(LaunchScanner);sc._close_store=store
    d=DegenConfig();d.entry_sol=.02;d.two_tier_enabled=False;d.speed_game_min_age_min=0.
    d.fingerprints_enabled=False;d.sentiment_enabled=False;d.lowcap_enabled=False;d.backtest_enabled=False
    d.fp_series_journal_path=str(tmp_path/'fp-series.jsonl')
    d.degen_lane_enabled=False;d.shadow_enabled=False;d.early_warn_enabled=False
    # Actual routed intake hands this exact candidate to the judge below.
    import json
    import daemon.launch_scanner as module
    from daemon.launch_scanner.logstream import LogStream
    from daemon.intelligence.acquisition import routine
    from daemon.intelligence.acquisition.provider_router import ProviderRouter,ROUTING_MODE_REVISED
    monkeypatch.setenv('ALCHEMY_API_KEY','fixture-alchemy')
    tx={'slot':77,'transaction':{'signatures':['synthetic-success'],
        'message':{'instructions':[{'parsed':{'type':'initializeMint2','info':{'mint':mint}}}]}},
        'meta':{'err':None,'innerInstructions':[]}}
    class Response:
        status=200
        def read(self):return json.dumps({'jsonrpc':'2.0','id':1,'result':tx}).encode()
        def __enter__(self):return self
        def __exit__(self,*a):pass
    (tmp_path/'provider').mkdir()
    r=ProviderRouter(home=str(tmp_path/'provider'),transport=lambda *a,**kw:Response(),helius_enabled=True,mode=ROUTING_MODE_REVISED)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    ls=LogStream(intake_home=tmp_path/'intake')
    ls._handle_message(json.dumps({'method':'logsNotification','params':{'result':{'value':{'err':None,'signature':'synthetic-success'}}}}))
    assert ls.drain()[0]['acquisition_evidence']['provider']=='alchemy'
    sc._logstream=ls;monkeypatch.setattr(module,'get_store',lambda:store)
    state={'seen':{},'degen_positions':{}}
    if cycle_mode!='segments':
        from daemon.common import save_daemon_state,get_daemon_state
        from agent import events
        events.configure(store=store,webhook_url='')
        monkeypatch.setattr(module,'load_config',lambda:SimpleNamespace(degen=d))
        d.pumpportal_enabled=False;d.momentum_enabled=False;d.state_save_interval_seconds=.000001
        for flag in ('feedback_enabled','recap_enabled','review_enabled','sustained_enabled',
            'second_wave_enabled','wavefront_enabled','toppicks_enabled',
            'intraday_top3_enabled','insider_track_enabled','exit_copy_enabled'):
            setattr(d,flag,False)
        monkeypatch.setattr(sources,'_get_json',lambda *a,**kw:None)
        for source in ('pumpfun_new','dexscreener_boosts','hyperliquid_listings','geckoterminal_usdc_pools'):
            monkeypatch.setattr(sources,source,lambda *a,**kw:[])
        monkeypatch.setattr(module.smartmoney,'_rpc',lambda *a,**kw:None)
        monkeypatch.setattr(LaunchScanner,'_fresh_adapter',staticmethod(lambda:a))
        save_daemon_state(store,'launch_scanner',state)
        sc.run_once()
        state=get_daemon_state(store,'launch_scanner')
    else:sc._drain_logstream(SimpleNamespace(degen=d),a,state)
    assert ls.drain()==[] and mint in state['candidates']
    cand=state['candidates'][mint]
    # Synthetic migration travels through the real parser and drain/annotation.
    from daemon.launch_scanner.pumpportal import PumpPortalFeed
    pp=PumpPortalFeed()
    pp._handle_message(json.dumps({'txType':'migrate','mint':mint,'pool':'pump-amm',
        'signature':'synthetic-migration','marketId':'fixture-pool'}))
    sc._pumpportal=pp
    sc._drain_pumpportal(SimpleNamespace(degen=d),a,state)
    assert state['seen'][mint]['extra']['migration']['pool']=='pumpswap'
    cand['symbol']='BONK';cand['name']='Synthetic'
    from agent import events
    events.configure(store=store,webhook_url='')
    if cycle_mode!='segments':
        monkeypatch.setattr(module,'load_config',lambda:SimpleNamespace(degen=d))
        d.pumpportal_enabled=False
        d.momentum_enabled=False
        d.state_save_interval_seconds=.000001
        for flag in ('feedback_enabled','recap_enabled','review_enabled','sustained_enabled',
            'second_wave_enabled','wavefront_enabled','toppicks_enabled',
            'intraday_top3_enabled','insider_track_enabled','exit_copy_enabled'):
            setattr(d,flag,False)
        monkeypatch.setattr(sources,'_get_json',lambda *a,**kw:None)
        for source in ('pumpfun_new','dexscreener_boosts','hyperliquid_listings','geckoterminal_usdc_pools'):
            monkeypatch.setattr(sources,source,lambda *a,**kw:[])
        monkeypatch.setattr(module.smartmoney,'_rpc',lambda *a,**kw:None)
        monkeypatch.setattr(LaunchScanner,'_fresh_adapter',staticmethod(lambda:a))
    def child_cycle(cap,forever=False,crash=False):
        import subprocess,sys,os
        (tmp_path/'cycle-config.json').write_text(json.dumps(vars(d)))
        result=subprocess.run([sys.executable,'-m','tests.joint_cycle_child',str(tmp_path),str(cap)],
            capture_output=True,text=True,env=dict(os.environ,JOINT_CYCLE_NOW=str(time.time()),JOINT_FOREVER='1' if forever else '',JOINT_CRASH='1' if crash else ''))
        assert result.returncode==(44 if crash else 0),result.stdout+result.stderr
        a._load_state()
    for cap in ((12000,) if cycle_mode in ('run_forever','abrupt_entry') else (12000,16000,22000,30000)):
        stats={'usd_market_cap':cap,'price_usd':cap/1000000.,'usd_liquidity':25000.,
            'vol_m5':8000.,'buys_m5':30,'sells_m5':6,'symbol':'BONK','name':'Synthetic'}
        cand['stats_ts']=0
        monkeypatch.setattr(sources,'dexscreener_batch_stats',lambda *a,stats=stats,**kw:{mint:stats})
        monkeypatch.setattr(sources,'dexscreener_token_stats',lambda *a,stats=stats,**kw:stats)
        if cycle_mode!='segments':
            from daemon.common import save_daemon_state,get_daemon_state
            save_daemon_state(store,'launch_scanner',state)
            if cycle_mode in ('run_forever','abrupt_entry'):child_cycle(cap,forever=True,crash=cycle_mode=='abrupt_entry')
            elif cycle_mode=='fresh_process':child_cycle(cap)
            else:sc.run_once()
            state=get_daemon_state(store,'launch_scanner')
            cand=(state.get('candidates') or {}).get(mint,{})
        else:sc._judge(SimpleNamespace(degen=d),a,state)
    assert mint not in state['candidates']
    assert len(state['degen_positions'])==1 and len(a.positions())==1
    assert account.snapshot()['cash']==8.
    pos=state['degen_positions'][mint]
    assert pos['safety_score']==90 and pos['ticket']==1
    assert watchlist.current_watchlist()[1]['BONK']==mint
    launch_events=[e for e in store.event_journal_latest(100) if e['event']=='launch.detected']
    assert len(launch_events)==1 and launch_events[0]['mint']==mint
    assert launch_events[0]['safety_score']==90
    assert len(store.journal_query(kind='scanner_entry_projected'))==1
    if cycle_mode=='abrupt_entry':
        import os
        pidpath=tmp_path/'runtime'/'run'/'launch_scanner.pid'
        stale_pid=int(pidpath.read_text())
        with pytest.raises(ProcessLookupError):os.kill(stale_pid,0)
        # Start a real new daemon loop, which must reclaim the stale pidfile.
        before_restart=account.snapshot()
        child_cycle(30000,forever=True)
        assert not pidpath.exists() and account.snapshot()==before_restart
        assert len(store.journal_query(kind='scanner_entry_projected'))==1
    # Extend this same successful native entry into all six real learner sinks.
    from daemon.launch_scanner import (narratives,smartmoney,exit_liquidity_clock,
        exit_copy,deployer_registry,wallet_registry)
    heat=tmp_path/'heat.json';wallet=tmp_path/'wallet.json';clock=tmp_path/'clock.json'
    copy=tmp_path/'copy.json';dep=tmp_path/'deployer.json';history=tmp_path/'history.jsonl'
    monkeypatch.setattr(narratives,'default_heat_path',lambda:heat)
    monkeypatch.setattr(smartmoney,'_REGISTRY_FILE',wallet)
    monkeypatch.setattr(exit_liquidity_clock,'_resolve_path',lambda p:str(clock) if p is None else str(p))
    monkeypatch.setattr(exit_copy,'_resolve_path',lambda p:str(copy) if p is None else str(p))
    monkeypatch.setattr(deployers,'_REGISTRY_FILE',dep)
    smartmoney.save_registry({'wallet':{'picks':0,'wins':0}})
    d.backtest_enabled=True;d.backtest_dataset_path=str(history);d.exit_copy_enabled=True
    state['smart_activity']={mint:['wallet']};state['narrative_tags']={mint:['dog']}
    state['degen_positions'][mint]['deployer']='fixture-deployer'
    # Advance clock, do not mutate source-pinned economic entry timestamp.
    close_now=time.time()+30*3600
    monkeypatch.setattr(time,'time',lambda:close_now)
    from datetime import datetime,timezone
    get_quote=a._market.get_quote
    def clock_quote(symbol):
        quote=get_quote(symbol)
        if quote:quote['ts']=datetime.fromtimestamp(close_now-.01,timezone.utc)
        return quote
    monkeypatch.setattr(a._market,'get_quote',clock_quote)
    if cycle_mode!='segments':
        save_daemon_state(store,'launch_scanner',state)
        if cycle_mode in ('fresh_process','run_forever','abrupt_entry'):child_cycle(30000)
        else:sc.run_once()
        state=get_daemon_state(store,'launch_scanner')
    else:sc._manage_exits(SimpleNamespace(degen=d),a,state)
    assert not state['degen_positions'] and not a.positions()
    assert len(account.snapshot()['pending_sol'])==(0 if cycle_mode!='segments' else 1)
    assert len(narratives.read_heat(heat)['outcomes'])==1
    assert wallet_registry.read(wallet)['wallet']['picks']==1
    assert len(exit_liquidity_clock._all_records(str(clock)))==1
    assert len(exit_copy._all_closes(str(copy)))==1
    assert len(deployer_registry.read(dep))==1
    assert len(history.read_text().splitlines())==1
    from daemon.launch_scanner import scoreboard
    kinds=('scanner_narrative_projected','scanner_wallet_learning_projected',
           'scanner_exit_clock_projected','scanner_exit_copy_projected',
           'scanner_deployer_projected','scanner_backtest_projected')
    for kind in kinds:assert len(store.journal_query(kind=kind))==1,kind
    rows=scoreboard.load_degen_closes(str(store.path))
    assert len(rows)==1
    event=rows[0]['event_id']
    assert rows[0]['ticket']==1 and rows[0]['profit']==0. and rows[0]['symbol']=='BONK'
    assert narratives.read_heat(heat)['outcomes'][0][1:]==[['dog'],1.0,False]
    assert wallet_registry.read(wallet)=={'wallet':{'picks':1,'wins':0}}
    clock_row=exit_liquidity_clock._all_records(str(clock))[0]
    assert (clock_row['mint'],clock_row['pnl_sol'],clock_row['close_mult'])==(mint,0.,1.)
    copy_row=exit_copy._all_closes(str(copy))[event+':exit-copy-v1']
    assert (copy_row['mint'],copy_row['pnl_sol'],copy_row['close_mult'])==(mint,0.,1.)
    assert deployer_registry.read(dep)['fixture-deployer']['dead']==1
    record=json.loads(history.read_text())
    assert (record['mint'],record['outcome_id'],record['pnl_sol'],record['safety']['score'])==(mint,event,0.,90)
    assert record['entry_features']['gain_mult']==2.5 and len(record['snapshots'])==4
    # Reopened durable state and a subsequent judge/drain must not replay entry/card.
    from daemon.common import get_daemon_state,save_daemon_state
    save_daemon_state(store,'launch_scanner',state)
    reopened=Store(store.path);restored=get_daemon_state(reopened,'launch_scanner')
    before=account.snapshot();acks=len(store.journal_query(kind='scanner_entry_projected'))
    monkeypatch.setattr(watchlist,'add_to_watchlist',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('no repeated card/watch')))
    sc._drain_logstream(SimpleNamespace(degen=d),a,restored)
    sc._judge(SimpleNamespace(degen=d),a,restored)
    assert mint not in restored['candidates'] and account.snapshot()==before
    assert len(store.journal_query(kind='scanner_entry_projected'))==acks==1
    assert len([e for e in store.event_journal_latest(100) if e['event']=='launch.detected'])==1
    assert all(x['ok'] for x in a.retry_pending_usd())
    assert account.snapshot()['cash']==10. and account.snapshot()['pending_sol']==[]


    assert attempts==[]
    assert {t.ident for t in threading.enumerate()}<=threads_before
