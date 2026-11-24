"""One isolated scanner cycle for the connected synthetic lifecycle test.

Peripheral discovery lists are empty and sockets forbidden. Real run_once,
scan, judge, safety parser, veto, native/shared economics and learners execute.
"""
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace


def main(root,cap):
    root=Path(root)
    import threading
    before={t.ident for t in threading.enumerate()}
    now=float(os.environ['JOINT_CYCLE_NOW'])
    time.time=lambda:now
    attempts=[]
    def denied(*a,**kw):
        attempts.append('network attempt')
        raise AssertionError('offline cycle')
    for name in ('create_connection','getaddrinfo'):setattr(socket,name,denied)
    for name in ('connect','connect_ex'):setattr(socket.socket,name,denied)
    from daemon.launch_scanner import (LaunchScanner,sources,safety,bundle,deployers,
        killswitch,paper_account,smartmoney,narratives,exit_liquidity_clock,exit_copy)
    import daemon.launch_scanner as module
    from daemon.launch_scanner.logstream import LogStream
    from daemon.launch_scanner import social_radar
    social_radar._polite_get=lambda *a,**kw:None
    from daemon.telegram import watchlist
    from tests.test_broker_memecoin import _adapter,FakeMarket
    from storage.store import Store
    from agent import events
    d=SimpleNamespace(**json.loads((root/'cycle-config.json').read_text()))
    store=Store(root/'scanner.db')
    module.get_store=lambda:store
    module.load_config=lambda:SimpleNamespace(degen=d)
    folder=root/'paper_account'
    paper_account.ACCOUNT_DIR=str(folder)
    paper_account.LEDGER_PATH=str(folder/'ledger.json')
    paper_account.JOURNAL_PATH=str(folder/'events.jsonl')
    paper_account.LOCK_FILE=str(folder/'.lock')
    for name in ('STATE_FILE','SHADOW_FILE','LOCK_FILE'):
        setattr(killswitch,name,str(root/'killswitch'/name))
    bundle._rpc=lambda *a,**kw:None
    deployers._rpc=lambda *a,**kw:None
    smartmoney._rpc=lambda *a,**kw:None
    exit_copy._rpc=lambda *a,**kw:None
    safety._get_json=lambda *a,**kw:{'rugged':False,'score_normalised':90}
    sources._get_json=lambda *a,**kw:None
    for name in ('pumpfun_new','dexscreener_boosts','hyperliquid_listings','geckoterminal_usdc_pools'):
        setattr(sources,name,lambda *a,**kw:[])
    watchlist._defaults_path=lambda:str(root/'watch-defaults.yaml')
    events.configure(store=store,webhook_url='')
    a=_adapter(str(root/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=paper_account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    from datetime import datetime,timezone
    original_quote=a._market.get_quote
    def clock_quote(symbol):
        quote=original_quote(symbol)
        if quote:quote['ts']=datetime.fromtimestamp(now-.01,timezone.utc)
        return quote
    a._market.get_quote=clock_quote
    a.connect({});mint=a._market.mint_for('BONK')
    stats={'usd_market_cap':cap,'price_usd':cap/1000000.,'usd_liquidity':25000.,
        'vol_m5':8000.,'buys_m5':30,'sells_m5':6,'symbol':'BONK','name':'Synthetic'}
    sources.dexscreener_batch_stats=lambda *a,**kw:{mint:stats}
    sources.dexscreener_token_stats=lambda *a,**kw:stats
    narratives.default_heat_path=lambda:root/'heat.json'
    smartmoney._REGISTRY_FILE=root/'wallet.json'
    deployers._REGISTRY_FILE=root/'deployer.json'
    exit_liquidity_clock._resolve_path=lambda p:str(root/'clock.json') if p is None else str(p)
    exit_copy._resolve_path=lambda p:str(root/'copy.json') if p is None else str(p)
    LaunchScanner._fresh_adapter=staticmethod(lambda:a)
    scanner=LaunchScanner(interval=.01)
    scanner._logstream=LogStream(intake_home=root/'intake')
    if os.environ.get('JOINT_FOREVER'):
        # Real daemon PID/signal loop. Input observations are fixture-clocked.
        import signal
        import daemon.common as common
        common.get_store=lambda:store
        d.poll_interval_seconds=.01
        prices=iter((12000.,16000.,22000.,30000.))
        count=[0]
        original_price=sources.dexscreener_batch_stats
        def cycle_prices(*args,**kwargs):
            stats['usd_market_cap']=next(prices,30000.)
            stats['price_usd']=stats['usd_market_cap']/1000000.
            return original_price(*args,**kwargs)
        sources.dexscreener_batch_stats=cycle_prices
        original_prune=scanner._prune_seen
        def checkpoint(state,degen,stamp):
            nonlocal now
            now+=1.
            count[0]+=1
            for cand in (state.get('candidates') or {}).values():cand['stats_ts']=0
            if count[0]>=4:
                if os.environ.get('JOINT_CRASH'):
                    assert attempts==[]
                    os._exit(44)
                os.kill(os.getpid(),signal.SIGTERM)
            return original_prune(state,degen,stamp)
        scanner._prune_seen=checkpoint
        assert scanner.run_forever()==0
        assert count[0]>=4
        assert not Path(common.pid_file(scanner.name)).exists()
    else:scanner.run_once()
    assert attempts==[],attempts
    assert {t.ident for t in threading.enumerate()}<=before
    print(json.dumps({'pid':os.getpid(),'cash':paper_account.snapshot()['cash']}))


if __name__=='__main__':main(sys.argv[1],float(sys.argv[2]))
