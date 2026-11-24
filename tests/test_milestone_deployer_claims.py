import pytest
from types import SimpleNamespace
from daemon.launch_scanner import milestones,deployers as d
from tests.test_deployer_registry_effects import registry


def test_failed_expiry_keeps_exact_proposal_for_retry(registry,monkeypatch):
    rec={'card_ts':1,'peak_mult':3};state={'deployer_cache':{'m':{'deployer':'dep'}}}
    real=d.claim_outcome
    with monkeypatch.context() as m:
        m.setattr(d,'claim_outcome',lambda *a,**kw:(_ for _ in ()).throw(OSError('disk')))
        assert milestones._grade_deployer_outcome(state,SimpleNamespace(),'m',rec) is False
    proposal=dict(rec['deployer_grade_proposal'])
    assert milestones._grade_deployer_outcome(state,SimpleNamespace(),'m',rec) is True
    assert rec['deployer_grade_proposal']==proposal
    assert d.load_registry()['dep']['hits_2x']==1
    fresh={'deployer_cache':{'m':{'deployer':'dep'}}}
    assert milestones._grade_deployer_outcome(fresh,SimpleNamespace(),'m',{'card_ts':1,'peak_mult':1}) is True
    assert fresh['deployer_outcome_recorded']['m']['outcome']=='hit_2x'
    assert d.load_registry()['dep']['dead']==0


def test_committed_close_claim_wins_expiry_different_grade(registry):
    winner=d.claim_outcome('m','dep','rug',1,'close')
    state={'deployer_cache':{'m':{'deployer':'dep'}}}
    assert milestones._grade_deployer_outcome(state,SimpleNamespace(),'m',{'card_ts':1,'peak_mult':3})
    assert state['deployer_outcome_recorded']['m']=={'ts':winner['observed_at'],'outcome':'rug'}
    assert d.load_registry()['dep']['rugs']==1 and d.load_registry()['dep']['hits_2x']==0


def test_poll_removes_previously_recorded_and_freezes_failed_expiry(registry,monkeypatch):
    # Use production watch key, not a fake grading-only path.
    state={};watch=milestones._watch(state)
    watch['old']={'card_ts':1,'peak_mult':2}
    state['deployer_outcome_recorded']={'old':{'outcome':'dead','ts':2}}
    milestones.poll(state,SimpleNamespace(),100000,4)
    assert 'old' not in watch
    watch['m']={'card_ts':1,'peak_mult':2,'chain':'solana'}
    state['deployer_cache']={'m':{'deployer':'dep'}}
    monkeypatch.setattr(d,'claim_outcome',lambda *a,**kw:(_ for _ in ()).throw(OSError('disk')))
    monkeypatch.setattr(milestones,'_batch_stats',lambda *a:(_ for _ in ()).throw(AssertionError('expired pending must not fetch/update market')))
    milestones.poll(state,SimpleNamespace(),100000,4)
    assert watch['m']['peak_mult']==2
    assert watch['m']['deployer_grade_proposal']['peak_mult']==2


def test_pinned_expiry_retry_ignores_removed_or_changed_cache_and_path(registry,tmp_path,monkeypatch):
    rec={'card_ts':1,'peak_mult':3};state={'deployer_cache':{'m':{'deployer':'dep'}}}
    with monkeypatch.context() as m:
        m.setattr(d,'claim_outcome',lambda *a,**kw:(_ for _ in ()).throw(OSError('disk')))
        assert not milestones._grade_deployer_outcome(state,SimpleNamespace(),'m',rec)
    state['deployer_cache']={'m':{'deployer':'wrong'}}
    monkeypatch.setattr(d,'_REGISTRY_FILE',tmp_path/'wrong.json')
    assert milestones._grade_deployer_outcome(state,SimpleNamespace(),'m',rec)
    from daemon.launch_scanner.deployer_registry import read
    assert set(read(registry))=={'dep'} and not (tmp_path/'wrong.json').exists()


def test_persisted_poll_failed_claim_then_restart_and_state_save_failure(registry,tmp_path,monkeypatch):
    import json
    from storage.store import Store
    from daemon.common import save_daemon_state,get_daemon_state
    store=Store(tmp_path/'state.db');state={'deployer_cache':{'m':{'deployer':'dep'}}}
    milestones._watch(state)['m']={'card_ts':1,'peak_mult':3,'chain':'solana'}
    save_daemon_state(store,'launch_scanner',state)
    with monkeypatch.context() as m:
        m.setattr(d,'claim_outcome',lambda *a,**kw:(_ for _ in ()).throw(OSError('store unavailable')))
        milestones.poll(state,SimpleNamespace(),100000,4)
    assert 'm' in milestones._watch(state)
    proposal=dict(milestones._watch(state)['m']['deployer_grade_proposal'])
    save_daemon_state(store,'launch_scanner',state);store.close()
    store=Store(tmp_path/'state.db');fresh=get_daemon_state(store,'launch_scanner');fresh['deployer_cache']={}
    milestones.poll(fresh,SimpleNamespace(),100000,4)
    assert 'm' not in milestones._watch(fresh)
    # Simulate state save failure by discarding fresh mutations: persisted
    # old pending watch replays even though canonical grade already committed.
    again=get_daemon_state(store,'launch_scanner');again['deployer_cache']={}
    milestones.poll(again,SimpleNamespace(),100000,4)
    assert 'm' not in milestones._watch(again)
    assert d.load_registry()['dep']['hits_2x']==1
    assert d._registry_doc()['value']['grade_claims']['m']['observed_at']==proposal['observed_at']


@pytest.mark.parametrize('card',[None,0,-1,'1',float('nan'),float('inf')])
def test_bad_card_time_or_preexpiry_proposal_cannot_grade(registry,card):
    state={'deployer_cache':{'m':{'deployer':'dep'}}};rec={'card_ts':card,'peak_mult':3}
    assert not milestones._grade_deployer_outcome(state,SimpleNamespace(),'m',rec)
    assert not registry.exists()


def test_persisted_preexpiry_proposal_refused(registry):
    state={'deployer_cache':{'m':{'deployer':'dep'}}}
    rec={'card_ts':100000,'peak_mult':3,'deployer_grade_proposal':{
        'mint':'m','deployer':'dep','outcome':'hit_2x','observed_at':100001,
        'card_ts':100000,'peak_mult':3,'path':str(registry)}}
    assert not milestones._grade_deployer_outcome(state,SimpleNamespace(),'m',rec)
    assert not registry.exists()
