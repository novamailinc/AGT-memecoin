"""Offline combined learner recovery. Not full discovery/decision qualification."""
import json
import pytest
from tests.test_scanner_close_outbox import setup
from tests.test_paper_account_instances import account
from daemon.launch_scanner import (LaunchScanner,close_outbox,narratives,smartmoney,
    wallet_registry,exit_liquidity_clock,exit_copy,deployers,deployer_registry,scoreboard)
from daemon.common import get_daemon_state


def test_all_six_learning_sinks_recover_one_native_close(account,tmp_path,monkeypatch):
    monkeypatch.setenv('FOREX_AGENT_HOME',str(tmp_path/'runtime'))
    heat=tmp_path/'heat.json';wallet=tmp_path/'wallet.json';clock=tmp_path/'clock.json'
    copy=tmp_path/'copy.json';deployer=tmp_path/'deployer.json';history=tmp_path/'history.jsonl'
    monkeypatch.setattr(narratives,'default_heat_path',lambda:heat)
    monkeypatch.setattr(smartmoney,'_REGISTRY_FILE',wallet)
    monkeypatch.setattr(exit_liquidity_clock,'_resolve_path',lambda p:str(clock) if p is None else str(p))
    monkeypatch.setattr(exit_copy,'_resolve_path',lambda p:str(copy) if p is None else str(p))
    monkeypatch.setattr(deployers,'_REGISTRY_FILE',deployer)
    smartmoney.save_registry({'wallet':{'picks':0,'wins':0}})
    a,store,state,scanner,cfg=setup(tmp_path)
    cfg.degen.backtest_enabled=True;cfg.degen.backtest_dataset_path=str(history)
    mint=state['degen_positions']['M']['mint']
    state['degen_positions']['M']['deployer']='deployer'
    state['smart_activity']={mint:['wallet']};state['narrative_tags']={mint:['dog']}
    original=a.close_position
    def interrupted(ticket):
        original(ticket)
        raise SystemExit('native close before scanner event')
    monkeypatch.setattr(a,'close_position',interrupted)
    with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
    assert len(account.snapshot()['pending_sol'])==1
    for path in (heat,wallet,clock,copy,deployer,history):path.write_text('{bad')
    fresh=LaunchScanner.__new__(LaunchScanner);fresh._close_store=store
    restored=get_daemon_state(store,'launch_scanner')
    fresh._manage_exits(cfg,a,restored)
    assert not restored['degen_positions']
    assert len(scoreboard.load_degen_closes(str(store.path)))==1
    kinds=('scanner_narrative_projected','scanner_wallet_learning_projected',
           'scanner_exit_clock_projected','scanner_exit_copy_projected',
           'scanner_deployer_projected','scanner_backtest_projected')
    for kind in kinds:assert not store.journal_query(kind=kind),kind
    for path in (heat,wallet,clock,copy,deployer,history):assert path.read_text()=='{bad'
    for path in (heat,wallet,clock,copy,deployer,history):path.unlink()
    smartmoney.save_registry({'wallet':{'picks':0,'wins':0}})
    for _ in range(2):fresh._manage_exits(cfg,a,get_daemon_state(store,'launch_scanner'))
    for kind in kinds:assert len(store.journal_query(kind=kind))==1,kind
    assert len(narratives.read_heat(heat)['outcomes'])==1
    assert wallet_registry.read(wallet)['wallet']['picks']==1
    assert len(exit_liquidity_clock._all_records(str(clock)))==1
    assert len(exit_copy._all_closes(str(copy)))==1
    assert len(deployer_registry.read(deployer))==1
    assert len(history.read_text().splitlines())==1
    sinks=(heat,wallet,clock,copy,deployer,history)
    sink_bytes={p:p.read_bytes() for p in sinks}
    before=account.snapshot()
    for kind in kinds:store._conn.execute('DELETE FROM journal WHERE kind=?',(kind,))
    store._conn.commit()
    for _ in range(2):fresh._manage_exits(cfg,a,get_daemon_state(store,'launch_scanner'))
    for kind in kinds:assert len(store.journal_query(kind=kind))==1,kind
    assert account.snapshot()==before
    assert {p:p.read_bytes() for p in sinks}==sink_bytes
    assert len(exit_liquidity_clock._all_records(str(clock)))==1
    assert len(exit_copy._all_closes(str(copy)))==1
    registry=deployer_registry.read(deployer)
    assert len(registry)==1
    stored=json.loads(deployer.read_text())
    assert len(stored['value']['grade_claims'])==1
    assert sum(sum(v.get(k,0) for k in ('rugs','hits_2x','dead')) for v in registry.values())==1
    assert len(history.read_text().splitlines())==1
    assert wallet_registry.read(wallet)['wallet']['picks']==1
    assert len(narratives.read_heat(heat)['outcomes'])==1
