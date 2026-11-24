"""Actual shared/native scanner recovery, wallet learner only, no network."""
import json
import pytest
from tests.test_scanner_close_outbox import setup
from tests.test_paper_account_instances import account
from daemon.launch_scanner import LaunchScanner,smartmoney,close_outbox,wallet_registry
from daemon.common import save_daemon_state,get_daemon_state


def seed(tmp_path,monkeypatch):
    path=tmp_path/'wallets.json';monkeypatch.setattr(smartmoney,'_REGISTRY_FILE',path)
    smartmoney.save_registry({'wallet':{'picks':0,'wins':0}})
    a,store,state,scanner,cfg=setup(tmp_path)
    mint=state['degen_positions']['M']['mint'];state['smart_activity']={mint:['wallet']}
    save_daemon_state(store,'launch_scanner',state)
    return path,a,store,state,scanner,cfg


def test_native_close_crash_recovers_exact_wallet_effect_with_pinned_path(account,tmp_path,monkeypatch):
    path,a,store,state,scanner,cfg=seed(tmp_path,monkeypatch)
    original=a.close_position
    def crash(ticket):original(ticket);raise SystemExit('after native close')
    monkeypatch.setattr(a,'close_position',crash)
    with pytest.raises(SystemExit):scanner._manage_exits(cfg,a,state)
    assert wallet_registry.read(path)['wallet']['picks']==0
    monkeypatch.setattr(smartmoney,'_REGISTRY_FILE',tmp_path/'wrong.json')
    fresh=LaunchScanner.__new__(LaunchScanner);fresh._close_store=store
    fresh._manage_exits(cfg,a,get_daemon_state(store,'launch_scanner'))
    assert wallet_registry.read(path)['wallet']['picks']==1
    assert not (tmp_path/'wrong.json').exists()
    # Lost downstream ACK and stale scanner snapshot cannot duplicate counter.
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_wallet_learning_projected'");store._conn.commit()
    fresh._manage_exits(cfg,a,get_daemon_state(store,'launch_scanner'))
    assert wallet_registry.read(path)['wallet']['picks']==1


def test_wallet_write_then_ack_crash_and_retry(account,tmp_path,monkeypatch):
    path,a,store,state,scanner,cfg=seed(tmp_path,monkeypatch)
    original=store.journal_add
    def fail_ack(row):
        if row.get('kind')=='scanner_wallet_learning_projected':raise OSError('ACK fault')
        return original(row)
    monkeypatch.setattr(store,'journal_add',fail_ack)
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions']
    assert wallet_registry.read(path)['wallet']['picks']==1
    assert not store.journal_query(kind='scanner_wallet_learning_projected')
    monkeypatch.setattr(store,'journal_add',original)
    close_outbox.project_wallet_learning(store)
    assert wallet_registry.read(path)['wallet']['picks']==1
    assert len(store.journal_query(kind='scanner_wallet_learning_projected'))==1


def test_corrupt_wallet_store_does_not_block_economic_close_then_retries(account,tmp_path,monkeypatch):
    path,a,store,state,scanner,cfg=seed(tmp_path,monkeypatch)
    saved=path.read_text();path.write_text('{')
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and len(store.journal_query(kind='scanner_close_committed'))==1
    assert path.read_text()=='{' and not store.journal_query(kind='scanner_wallet_learning_projected')
    path.write_text(saved);close_outbox.project_wallet_learning(store)
    assert wallet_registry.read(path)['wallet']['picks']==1


def test_absent_activity_is_not_recorded_empty(account,tmp_path,monkeypatch):
    path,a,store,state,scanner,cfg=seed(tmp_path,monkeypatch)
    state.pop('smart_activity')
    scanner._manage_exits(cfg,a,state)
    context=store.journal_query(kind='scanner_close_committed')[0]['detail']['context']
    assert context['wallet_learning_spec']['state']=='unrecorded'
    assert context['wallet_learning_spec']['wallets'] is None
    assert not store.journal_query(kind='scanner_wallet_learning_projected')
    assert wallet_registry.read(path)['wallet']['picks']==0


def test_missing_target_stays_pending_until_restored(account,tmp_path,monkeypatch):
    path,a,store,state,scanner,cfg=seed(tmp_path,monkeypatch)
    saved=path.read_text();path.unlink()
    scanner._manage_exits(cfg,a,state)
    assert not path.exists() and not store.journal_query(kind='scanner_wallet_learning_projected')
    path.write_text(saved);close_outbox.project_wallet_learning(store)
    assert wallet_registry.read(path)['wallet']['picks']==1


@pytest.mark.parametrize('bad_path',[None,'','relative-wallet.json'])
def test_invalid_pinned_target_never_falls_back(account,tmp_path,monkeypatch,bad_path):
    import copy
    path,a,store,state,scanner,cfg=seed(tmp_path,monkeypatch)
    original=smartmoney.record_outcome
    monkeypatch.setattr(smartmoney,'record_outcome',lambda *a,**k: (_ for _ in ()).throw(OSError('defer')))
    scanner._manage_exits(cfg,a,state)
    monkeypatch.setattr(smartmoney,'record_outcome',original)
    rows=copy.deepcopy(store.journal_query(kind='scanner_close_committed'))
    rows[0]['detail']['context']['wallet_learning_spec']['path']=bad_path
    query=store.journal_query
    monkeypatch.setattr(store,'journal_query',lambda **kw:rows if kw.get('kind')=='scanner_close_committed' else query(**kw))
    close_outbox.project_wallet_learning(store)
    assert wallet_registry.read(path)['wallet']['picks']==0
    assert not query(kind='scanner_wallet_learning_projected')


@pytest.mark.parametrize('mutation',['nan','mint','symbol','ticket','allocation','rowid','missing-wallet'])
def test_bad_source_or_missing_wallet_stays_pending(account,tmp_path,monkeypatch,mutation):
    import copy
    path,a,store,state,scanner,cfg=seed(tmp_path,monkeypatch)
    original=smartmoney.record_outcome
    monkeypatch.setattr(smartmoney,'record_outcome',lambda *a,**k: (_ for _ in ()).throw(OSError('defer')))
    scanner._manage_exits(cfg,a,state)
    monkeypatch.setattr(smartmoney,'record_outcome',original)
    rows=copy.deepcopy(store.journal_query(kind='scanner_close_committed'));row=rows[0];e=row['detail']
    if mutation=='nan':e['native_receipt']['realized_pnl_sol']=float('nan')
    elif mutation=='mint':e['context']['wallet_learning_spec']['mint']='wrong'
    elif mutation=='symbol':e['native_receipt']['symbol']='wrong'
    elif mutation=='ticket':e['native_receipt']['ticket']=-9
    elif mutation=='allocation':e['native_receipt']['allocations'][0]['ticket']=-9
    elif mutation=='rowid':row['symbol']='wrong'
    else:e['context']['wallet_learning_spec']['wallets']=['missing']
    query=store.journal_query
    monkeypatch.setattr(store,'journal_query',lambda **kw:rows if kw.get('kind')=='scanner_close_committed' else query(**kw))
    close_outbox.project_wallet_learning(store)
    assert wallet_registry.read(path)['wallet']['picks']==0
    assert not query(kind='scanner_wallet_learning_projected')
