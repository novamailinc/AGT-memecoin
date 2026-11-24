import pytest
from unittest.mock import patch
from daemon.intelligence.acquisition.usage_tracker import UsageTracker
from daemon.intelligence.acquisition import provider_usage as usage
from daemon.intelligence.acquisition.request_scheduler import PRIORITY_BACKGROUND,PRIORITY_CARD,PRIORITY_CRITICAL,PRIORITY_DEPLOYER
from daemon.intelligence.acquisition.cost_management import CostManager
from daemon.intelligence.acquisition import routine
from daemon.intelligence.acquisition.provider_router import ProviderRouter,ROUTING_MODE_REVISED

@pytest.mark.parametrize('priority',[PRIORITY_BACKGROUND,PRIORITY_DEPLOYER,PRIORITY_CARD,PRIORITY_CRITICAL])
@pytest.mark.parametrize('over',[0,1])
@pytest.mark.parametrize('scope',['monthly','daily','feature'])
def test_known_exhaustion_refuses_every_priority(tmp_path,monkeypatch,priority,over,scope):
    tracker=UsageTracker(home=str(tmp_path));limit=1000000
    if scope=='daily':monkeypatch.setitem(usage.BUDGET_OVERRIDES,'helius_main',{'daily_amount':limit//2});limit//=2
    if scope=='feature':monkeypatch.setitem(usage.FEATURE_BUDGETS,('helius_main','standard_rpc'),{'amount':limit//3});limit//=3
    tracker.record('helius_main','helius','getBalance','routine_account',PRIORITY_CARD,cost=limit+over)
    allowed,reason=tracker.nonessential_allowed('helius_main',priority,workload='routine_account',provider='helius')
    assert not allowed and 'exhausted' in reason
    cost=CostManager(home=str(tmp_path),tracker=tracker)
    assert not cost.pre_call_check('helius_main',priority,workload='routine_account')[0]
    if scope!='feature':assert cost.suppression_status('helius_main')['essential_suppressed']
    fresh=UsageTracker(home=str(tmp_path))
    assert not fresh.nonessential_allowed('helius_main',priority,workload='routine_account',provider='helius')[0]


def test_below_exhaustion_retains_essential_priority(tmp_path):
    tracker=UsageTracker(home=str(tmp_path));tracker.record('helius_main','helius','getBalance','routine_account',PRIORITY_CARD,cost=950000)
    assert tracker.nonessential_allowed('helius_main',PRIORITY_CRITICAL)[0]
    assert not tracker.nonessential_allowed('helius_main',PRIORITY_BACKGROUND)[0]

@pytest.mark.parametrize('outcome',['ok','transport','rate_limited'])
def test_helius_wss_handshake_observed_once_without_invented_cost(tmp_path,monkeypatch,outcome):
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED)
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    before=r.usage.store.get('helius_main')
    error=None if outcome=='ok' else RuntimeError('429 fixture' if outcome=='rate_limited' else 'transport fixture')
    with patch('daemon.launch_scanner.helius_ratelimit.pacer'):
        routine.websocket_result('wss://mainnet.helius-rpc.com/?api-key=fixture',error)
    fresh=UsageTracker(home=str(tmp_path)).store.get('helius_main')
    assert fresh['calls']==before['calls']+1 and fresh['cost_spent']==before['cost_spent']
    assert fresh['by_method']['websocket/handshake']['calls']==1
    import json
    journal=[json.loads(line) for line in open(tmp_path/'acquisition_provider_usage.jsonl')]
    assert journal[-1]['kind']==outcome and journal[-1]['cost'] is None
    assert journal[-1]['account']=='helius_main' and journal[-1]['provider']=='helius'
    assert all('fixture' not in str(v) for v in journal[-1].values())
    if outcome=='ok':assert r.health.snapshot()['helius']['last_success'] is not None
    if outcome=='rate_limited':assert r.health.snapshot()['helius']['wss_handshake_429s']==1

@pytest.mark.parametrize('priority',[PRIORITY_CARD,PRIORITY_CRITICAL])
def test_actual_router_and_ws_refuse_exhausted_before_transport(tmp_path,monkeypatch,priority):
    for i in (1,2,3):monkeypatch.setenv('HELIUS_API_KEY_%d'%i,'fixture-%d'%i)
    calls=[]
    def transport(*a,**kw):calls.append(a);raise AssertionError('exhausted provider transport')
    r=ProviderRouter(home=str(tmp_path),helius_enabled=True,mode=ROUTING_MODE_REVISED,transport=transport)
    r.usage.record('helius_main','helius','getBalance','routine_account',PRIORITY_CARD,cost=1000000)
    result=r.call('getBalance',['fixture'],priority=priority,max_attempts=1)
    assert not calls
    assert result['kind']=='rate_limited'
    assert result['attempts'][0]['note']=='budget_exhausted'
    monkeypatch.setattr(routine._local,'router',r,raising=False)
    with pytest.raises(routine.WebsocketAdmissionError):routine.websocket_admit(routine.websocket_url(),critical=True)
