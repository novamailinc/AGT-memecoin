import json
from pathlib import Path
import pytest
from unittest.mock import patch
from daemon.intelligence.acquisition.usage_tracker import UsageTracker
from daemon.intelligence.acquisition.provider_router import ProviderRouter,ROUTING_MODE_REVISED
from daemon.intelligence.acquisition import routine,helius_account
from daemon.intelligence.acquisition.helius_parsed_events import HeliusParsedEventsClient

@pytest.mark.parametrize('payload',['broken','[]','null','{"helius_main":null}','{"helius_main":{}}'])
def test_corrupt_usage_refused_without_rewrite(tmp_path,payload):
    path=tmp_path/'acquisition_provider_usage.json';path.write_text(payload)
    tracker=UsageTracker(home=str(tmp_path))
    for priority in ('card','critical','background','deployer'):
        assert tracker.nonessential_allowed('helius_main',priority)==(False,'usage_state_unavailable')
    assert path.read_text()==payload
    assert not helius_account.allowed(str(tmp_path))

@pytest.mark.parametrize('field',['cost_spent','daily_cost_spent','calls','daily_calls','disabled','month','day','by_feature'])
@pytest.mark.parametrize('value',[None,-1,'1',True])
def test_invalid_usage_evidence_refused(tmp_path,field,value):
    u=UsageTracker(home=str(tmp_path));u.record('helius_main','helius','getSlot','routine_account','card',cost=1)
    p=tmp_path/'acquisition_provider_usage.json';d=json.loads(p.read_text());d['helius_main'][field]=value;p.write_text(json.dumps(d))
    if field=='disabled' and value is True:
        assert u.nonessential_allowed('helius_main','critical')==(False,'account_disabled')
        return
    assert u.nonessential_allowed('helius_main','critical')==(False,'usage_state_unavailable')


def test_lost_counters_with_retained_journal_not_fresh_zero(tmp_path):
    u=UsageTracker(home=str(tmp_path));u.record('helius_main','helius','getSlot','routine_account','card',cost=1)
    (tmp_path/'acquisition_provider_usage.json').unlink()
    assert u.nonessential_allowed('helius_main','card')==(False,'usage_state_unavailable')
    assert not (tmp_path/'acquisition_provider_usage.json').exists()


def test_new_empty_runtime_admissible_without_claiming_prior_usage(tmp_path):
    assert UsageTracker(home=str(tmp_path)).nonessential_allowed('helius_main','card')==(True,None)


def test_unreadable_counter_refuses_without_network_or_reset(tmp_path,monkeypatch):
    u=UsageTracker(home=str(tmp_path));p=Path(u.store._path)
    # Admission uses builtins.open, inject a deterministic permission error.
    import builtins
    original=builtins.open
    def denied(file,*a,**kw):
        if str(file)==str(p):raise PermissionError('fixture counter access')
        return original(file,*a,**kw)
    with patch('builtins.open',side_effect=denied):
        assert u.nonessential_allowed('helius_main','card')==(False,'usage_state_unavailable')
    assert not p.exists()


def test_actual_router_ws_and_selective_corrupt_state_no_transport(tmp_path,monkeypatch):
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    p=tmp_path/'acquisition_provider_usage.json';p.write_text('broken')
    calls=[]
    def transport(*a,**kw):calls.append(a);raise AssertionError('no transport')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=transport)
    result=r.call('getSlot',[],max_attempts=1)
    assert result['kind']!='ok' and not calls
    assert result['attempts'][0]['note']=='usage_state_unavailable'
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    with pytest.raises(routine.WebsocketAdmissionError):routine.websocket_admit(routine.websocket_url(),critical=True)
    c=HeliusParsedEventsClient(api_key='fixture',_opener=transport);c.account_home=str(tmp_path)
    assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='rate_limited'
    assert not calls and p.read_text()=='broken'

@pytest.mark.parametrize('kind',['missing_account','empty_root','corrupt_journal','audit_limit'])
def test_account_loss_or_unverifiable_history_refuses(tmp_path,kind):
    u=UsageTracker(home=str(tmp_path));u.record('helius_main','helius','getSlot','routine_account','card',cost=1)
    p=tmp_path/'acquisition_provider_usage.json';j=tmp_path/'acquisition_provider_usage.jsonl'
    p.write_text('{}')
    if kind=='corrupt_journal':j.write_text('broken')
    if kind=='audit_limit':j.write_text((json.dumps({'account':'other'})+'\n')*10001)
    before=p.read_text()
    assert u.nonessential_allowed('helius_main','card')==(False,'usage_state_unavailable')
    assert p.read_text()==before


def test_new_account_with_only_other_valid_history_not_false_loss(tmp_path):
    u=UsageTracker(home=str(tmp_path));u.record('alchemy','alchemy','getSlot','routine_account','card',cost=20)
    assert u.nonessential_allowed('helius_main','card')==(True,None)


def test_partial_feature_loss_cannot_bypass_exhausted_feature_budget(tmp_path,monkeypatch):
    from daemon.intelligence.acquisition import provider_usage
    u=UsageTracker(home=str(tmp_path));u.record('helius_main','helius','getSlot','routine_account','card',cost=10)
    monkeypatch.setitem(provider_usage.FEATURE_BUDGETS,('helius_main','standard_rpc'),{'amount':10})
    assert not u.nonessential_allowed('helius_main','card',workload='routine_account',provider='helius')[0]
    p=tmp_path/'acquisition_provider_usage.json';d=json.loads(p.read_text());d['helius_main']['by_feature']={};p.write_text(json.dumps(d))
    assert u.nonessential_allowed('helius_main','card',workload='routine_account',provider='helius')==(False,'usage_state_unavailable')

@pytest.mark.parametrize('row',[{}, {'account':None},{'account':''},{'account':'   '},[]])
def test_unattributable_journal_not_evidence_of_other_account(tmp_path,row):
    (tmp_path/'acquisition_provider_usage.json').write_text('{}')
    (tmp_path/'acquisition_provider_usage.jsonl').write_text(json.dumps(row)+'\n')
    assert UsageTracker(home=str(tmp_path)).nonessential_allowed('helius_main','card')==(False,'usage_state_unavailable')
