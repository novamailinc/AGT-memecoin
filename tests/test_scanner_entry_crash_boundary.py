"""Actual native-entry/scanner-position recovery with pinned original source."""
from types import SimpleNamespace
import pytest
from tests.test_broker_memecoin import _adapter,FakeMarket
from tests.test_paper_account_instances import account
from daemon.launch_scanner import LaunchScanner,killswitch,veto
from daemon.common import save_daemon_state,get_daemon_state
from storage.store import Store


def test_committed_entry_before_scanner_position_recovers_without_submit(account,tmp_path,monkeypatch):
    monkeypatch.setattr(killswitch,'gate',lambda lane:True)
    monkeypatch.setattr(veto,'veto_check',lambda *a,**kw:{'passed':True,'vetoes':[]})
    import time
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(tmp_path/'scanner.db')
    state={'degen_positions':{}};save_daemon_state(store,'launch_scanner',state)
    scanner=LaunchScanner.__new__(LaunchScanner);scanner._close_store=store
    d=SimpleNamespace(entry_sol=.02,stop_loss_pct=30.,take_profit_pct=200.,
        max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=False,speed_game_min_age_min=0.)
    card={'tier':'early','extra':{'complete':True},'note':''}
    original=a.submit_order
    def crash(req):original(req);raise SystemExit('native entry committed before scanner position')
    monkeypatch.setattr(a,'submit_order',crash)
    with pytest.raises(SystemExit):scanner._paper_enter(None,a,state,card,d,a._market.mint_for('BONK'),'BONK')
    assert account.snapshot()['cash']==8.
    assert len(a.positions())==1
    assert get_daemon_state(store,'launch_scanner')['degen_positions']=={}
    assert len(store.journal_query(kind='scanner_entry_intent'))==1
    from daemon.launch_scanner import entry_outbox
    from daemon import common
    monkeypatch.setattr(a,'submit_order',lambda *a:(_ for _ in ()).throw(AssertionError('no restart submit')))
    a._fx_observer=lambda:(_ for _ in ()).throw(AssertionError('no restart FX'))
    restored=get_daemon_state(store,'launch_scanner')
    entry_outbox.recover(store,a,restored)
    assert len(restored['degen_positions'])==1
    pos=next(iter(restored['degen_positions'].values()))
    assert pos['ticket']==1 and pos['volume_sol']==.02 and pos['entry_price_sol']==.000001
    assert len(store.journal_query(kind='scanner_entry_projected'))==1
    assert get_daemon_state(store,'launch_scanner')['degen_positions']==restored['degen_positions']
    assert account.snapshot()['cash']==8.



@pytest.mark.parametrize('boundary',['before-submit','after-A','after-B','state-write'])
def test_committed_legs_only_recover_with_original_sources(account,tmp_path,monkeypatch,boundary):
    import time
    from daemon.launch_scanner import entry_outbox
    monkeypatch.setattr(killswitch,'gate',lambda lane:True)
    monkeypatch.setattr(veto,'veto_check',lambda *a,**kw:{'passed':True,'vetoes':[]})
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(tmp_path/'scanner.db');scanner=LaunchScanner.__new__(LaunchScanner);scanner._close_store=store
    mint=a._market.mint_for('BONK')
    state={'degen_positions':{},'narrative_tags':{mint:['dog']},'smart_activity':{mint:['wallet']}}
    save_daemon_state(store,'launch_scanner',state)
    d=SimpleNamespace(entry_sol=.04,stop_loss_pct=30.,take_profit_pct=200.,max_hold_hours=4.,
        tier_b_max_hold_hours=24.,daily_max_loss_sol=1.,two_tier_enabled=True,speed_game_min_age_min=0.)
    card={'tier':'early','extra':{'complete':True},'note':'','narratives':['dog']}
    original=a.submit_order;publish=store.scanner_entry_publish;calls=[]
    def submit(req):
        calls.append(req)
        if boundary=='before-submit':raise SystemExit('before economic entry')
        result=original(req)
        if boundary=='after-A' or boundary=='after-B' and len(calls)==2:raise SystemExit('committed leg')
        return result
    monkeypatch.setattr(a,'submit_order',submit)
    if boundary=='state-write':monkeypatch.setattr(store,'scanner_entry_publish',lambda *a,**kw:(_ for _ in ()).throw(SystemExit('state fail')))
    with pytest.raises(SystemExit):scanner._paper_enter(None,a,state,card,d,mint,'BONK',intel={'safety':{'score':90}})
    monkeypatch.setattr(store,'scanner_entry_publish',publish)
    monkeypatch.setattr(a,'submit_order',lambda *a:(_ for _ in ()).throw(AssertionError('no restart submit')))
    a._fx_observer=lambda:(_ for _ in ()).throw(AssertionError('no restart FX'))
    restored=get_daemon_state(store,'launch_scanner')
    for _ in range(2):entry_outbox.recover(store,a,restored)
    count=0 if boundary=='before-submit' else 2 if boundary=='after-B' else 1
    assert len(restored['degen_positions'])==count
    assert account.snapshot()['cash']==10.-2.*count
    assert len(store.journal_query(kind='scanner_entry_projected'))==count
    for pos in restored['degen_positions'].values():
        assert pos['safety_score']==90 and pos['narratives']==['dog']
        assert pos['entry_price_sol']==.000001
    assert restored['narrative_tags'][mint]==['dog'] and restored['smart_activity'][mint]==['wallet']


def test_entry_child_native_commit_then_fresh_recovery(account,tmp_path,monkeypatch):
    import os,subprocess,sys
    source='''
import os,sys,time
from types import SimpleNamespace
from tests.test_broker_memecoin import _adapter,FakeMarket
from daemon.launch_scanner import LaunchScanner,paper_account as pa,killswitch,veto,entry_outbox
from daemon.common import get_daemon_state
from storage.store import Store
folder,native,db,mode=sys.argv[1:]
pa.ACCOUNT_DIR=folder;pa.LEDGER_PATH=folder+'/ledger.json';pa.JOURNAL_PATH=folder+'/events.jsonl';pa.LOCK_FILE=folder+'/.lock'
a=_adapter(native,FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=pa,fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
a.connect({});store=Store(db);scanner=LaunchScanner.__new__(LaunchScanner);scanner._close_store=store
state=get_daemon_state(store,'launch_scanner');state.setdefault('degen_positions',{})
if mode=='crash':
 killswitch.gate=lambda lane:True
 veto.veto_check=lambda *a,**kw:{'passed':True,'vetoes':[]}
 original=a.submit_order
 def submit(req):
  original(req);os._exit(43)
 a.submit_order=submit
 d=SimpleNamespace(entry_sol=.02,stop_loss_pct=30.,take_profit_pct=200.,max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=False,speed_game_min_age_min=0.)
 scanner._paper_enter(None,a,state,{'tier':'early','extra':{'complete':True},'note':''},d,a._market.mint_for('BONK'),'BONK')
else:
 a.submit_order=lambda *a:(_ for _ in ()).throw(AssertionError('no submit'))
 a._fx_observer=lambda:(_ for _ in ()).throw(AssertionError('no FX'))
 entry_outbox.recover(store,a,state)
 assert len(state['degen_positions'])==1
 assert len(store.journal_query(kind='scanner_entry_projected'))==1
 assert pa.snapshot()['cash']==8.
 assert next(iter(state['degen_positions'].values()))['ticket']==1
'''
    args=[sys.executable,'-c',source,account.ACCOUNT_DIR,str(tmp_path/'native'),str(tmp_path/'scanner.db')]
    env=dict(os.environ,FOREX_AGENT_TESTING='1')
    first=subprocess.run(args+['crash'],capture_output=True,text=True,env=env)
    assert first.returncode==43,first.stderr
    workers=[subprocess.Popen(args+['recover'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env) for _ in range(6)]
    for worker in workers:
        out,err=worker.communicate(timeout=20)
        assert worker.returncode==0,err
    for _ in range(2):
        replay=subprocess.run(args+['recover'],capture_output=True,text=True,env=env)
        assert replay.returncode==0,replay.stderr


def test_malformed_entry_plan_isolated_from_other_entry_recovery(account,tmp_path,monkeypatch):
    from daemon.launch_scanner import entry_outbox
    import time
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(tmp_path/'scanner.db')
    store.journal_add({'kind':'scanner_entry_intent','symbol':'broken','detail':{'book':a.risk_state_identity(),'request':None}})
    restored={'degen_positions':{}}
    entry_outbox.recover(store,a,restored)
    assert restored['degen_positions']=={} and account.snapshot()['cash']==10.
    assert len(store.journal_query(kind='scanner_entry_intent'))==1


def test_corrupted_pinned_source_cannot_rehydrate(account,tmp_path,monkeypatch):
    import json,time
    from daemon.launch_scanner import entry_outbox
    monkeypatch.setattr(killswitch,'gate',lambda lane:True)
    monkeypatch.setattr(veto,'veto_check',lambda *a,**kw:{'passed':True,'vetoes':[]})
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(tmp_path/'scanner.db');sc=LaunchScanner.__new__(LaunchScanner);sc._close_store=store
    d=SimpleNamespace(entry_sol=.02,stop_loss_pct=30.,take_profit_pct=200.,max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=False,speed_game_min_age_min=0.)
    original=a.submit_order
    def crash(req):original(req);raise SystemExit('committed')
    monkeypatch.setattr(a,'submit_order',crash)
    with pytest.raises(SystemExit):sc._paper_enter(None,a,{'degen_positions':{}},{'tier':'early','extra':{'complete':True}},d,a._market.mint_for('BONK'),'BONK')
    row=store._conn.execute("SELECT id,payload FROM journal WHERE kind='scanner_entry_intent'").fetchone()
    payload=json.loads(row['payload']);payload['detail']['template']['narratives']=['invented']
    store._conn.execute('UPDATE journal SET payload=? WHERE id=?',(json.dumps(payload),row['id']));store._conn.commit()
    empty={'degen_positions':{}};entry_outbox.recover(store,a,empty)
    assert not empty['degen_positions'] and not store.journal_query(kind='scanner_entry_projected')
    assert len(a.positions())==1 and account.snapshot()['cash']==8.


def test_postsubmit_mutable_intel_cannot_replace_pinned_source(account,tmp_path,monkeypatch):
    import time
    monkeypatch.setattr(killswitch,'gate',lambda lane:True)
    monkeypatch.setattr(veto,'veto_check',lambda *a,**kw:{'passed':True,'vetoes':[]})
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(tmp_path/'scanner.db');sc=LaunchScanner.__new__(LaunchScanner);sc._close_store=store
    d=SimpleNamespace(entry_sol=.02,stop_loss_pct=30.,take_profit_pct=200.,max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=False,speed_game_min_age_min=0.)
    card={'tier':'early','extra':{'complete':True},'narratives':['dog']};intel={'safety':{'score':90}}
    original=a.submit_order
    def mutate(req):
        result=original(req);card['narratives']=['wrong'];intel['safety']['score']=0
        return result
    monkeypatch.setattr(a,'submit_order',mutate)
    state={'degen_positions':{}}
    assert sc._paper_enter(None,a,state,card,d,a._market.mint_for('BONK'),'BONK',intel=intel)
    pos=next(iter(state['degen_positions'].values()))
    assert pos['narratives']==['dog'] and pos['safety_score']==90


def committed_plan(account,tmp_path,monkeypatch):
    import time
    from daemon.launch_scanner import entry_outbox
    monkeypatch.setattr(killswitch,'gate',lambda lane:True)
    monkeypatch.setattr(veto,'veto_check',lambda *a,**kw:{'passed':True,'vetoes':[]})
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(tmp_path/'scanner.db');sc=LaunchScanner.__new__(LaunchScanner);sc._close_store=store
    d=SimpleNamespace(entry_sol=.02,stop_loss_pct=30.,take_profit_pct=200.,max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=False,speed_game_min_age_min=0.)
    original=a.submit_order
    def crash(req):original(req);raise SystemExit('committed')
    monkeypatch.setattr(a,'submit_order',crash)
    with pytest.raises(SystemExit):sc._paper_enter(None,a,{'degen_positions':{}},{'tier':'early','extra':{'complete':True}},d,a._market.mint_for('BONK'),'BONK')
    return a,store,entry_outbox


def test_actual_entry_ack_loss_and_closed_buy_replay(account,tmp_path,monkeypatch):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    state={'degen_positions':{}};outbox.recover(store,a,state)
    expected=state['degen_positions'].copy()
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_entry_projected'");store._conn.commit()
    state={'degen_positions':{}};outbox.recover(store,a,state)
    assert state['degen_positions']==expected and len(store.journal_query(kind='scanner_entry_projected'))==1
    a.close_position(1)
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_entry_projected'");store._conn.commit()
    state={'degen_positions':{}};outbox.recover(store,a,state)
    assert state['degen_positions']=={} and store.journal_query(kind='scanner_entry_projected')==[]
    assert account.snapshot()['cash']==8. and len(account.snapshot()['pending_sol'])==1


def test_current_state_merge_conflict_and_bad_plus_valid_isolation(account,tmp_path,monkeypatch):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    store.journal_add({'kind':'scanner_entry_intent','symbol':'bad','detail':{'book':a.risk_state_identity(),'request':None}})
    save_daemon_state(store,'launch_scanner',{'degen_positions':{'unrelated':{'ticket':777}},'sentinel':'preserve'})
    state={'degen_positions':{}};outbox.recover(store,a,state)
    persisted=get_daemon_state(store,'launch_scanner')
    assert persisted['sentinel']=='preserve' and persisted['degen_positions']['unrelated']=={'ticket':777}
    assert len(state['degen_positions'])==2 and len(store.journal_query(kind='scanner_entry_projected'))==1
    save_daemon_state(store,'launch_scanner',state)
    after=get_daemon_state(store,'launch_scanner')
    assert after['sentinel']=='preserve' and after['degen_positions']['unrelated']=={'ticket':777}
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_entry_projected'");store._conn.commit()
    key=next(k for k in state['degen_positions'] if k!='unrelated')
    save_daemon_state(store,'launch_scanner',{'degen_positions':{key:{'ticket':999}}})
    state={'degen_positions':{}};outbox.recover(store,a,state)
    assert state['degen_positions']=={} and store.journal_query(kind='scanner_entry_projected')==[]
    assert get_daemon_state(store,'launch_scanner')['degen_positions'][key]['ticket']==999


def test_concurrent_close_cannot_commit_between_open_read_and_publish(account,tmp_path,monkeypatch):
    import threading,time
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    # Separate actual adapter/lock owner, sharing the same native book/account.
    b=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    b.connect({});entered=threading.Event();attempted=threading.Event();closed=threading.Event();errors=[]
    original=store.scanner_entry_publish
    def publish(*args,**kw):
        entered.set();assert attempted.wait(2)
        assert not closed.wait(.1)
        return original(*args,**kw)
    monkeypatch.setattr(store,'scanner_entry_publish',publish)
    def close():
        try:
            assert entered.wait(2);attempted.set();b.close_position(1);closed.set()
        except Exception as exc:errors.append(exc)
    worker=threading.Thread(target=close);worker.start()
    state={'degen_positions':{}}
    outbox.recover(store,a,state);worker.join(timeout=3)
    assert not worker.is_alive() and not errors and closed.is_set()
    assert len(state['degen_positions'])==1 # published before close, never after it
    assert len(account.snapshot()['pending_sol'])==1
    # Any fresh retry observes closed evidence and cannot republish.
    store._conn.execute("DELETE FROM journal WHERE kind='scanner_entry_projected'");store._conn.commit()
    fresh={'degen_positions':{}};outbox.recover(store,a,fresh)
    assert fresh['degen_positions']=={} and not store.journal_query(kind='scanner_entry_projected')


@pytest.mark.parametrize('field,value',[('event','credit'),('mint','wrong'),('qty',1),('stake',999),('instance',False),('lane','wrong')])
def test_wrong_shared_debit_receipt_cannot_rehydrate(account,tmp_path,monkeypatch,field,value):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    original=account.operation_receipt
    def wrong(op):
        receipt,error=original(op)
        if receipt and receipt.get('event')=='debit':receipt=dict(receipt,**{field:value})
        return receipt,error
    monkeypatch.setattr(account,'operation_receipt',wrong)
    state={'degen_positions':{}};outbox.recover(store,a,state)
    assert state['degen_positions']=={} and store.journal_query(kind='scanner_entry_projected')==[]


def test_wrong_shared_operation_contract_refused(account,tmp_path,monkeypatch):
    import json
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    path=a._fills_path();rows=[json.loads(x) for x in path.read_text().splitlines()]
    rows[0]['shared_entry_op_id']='native-entry:otherbook:otherkey'
    path.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
    state={'degen_positions':{}};outbox.recover(store,a,state)
    assert not state['degen_positions'] and not store.journal_query(kind='scanner_entry_projected')


def test_entry_intent_storage_failure_has_no_native_effect(account,tmp_path,monkeypatch):
    import time
    monkeypatch.setattr(killswitch,'gate',lambda lane:True)
    monkeypatch.setattr(veto,'veto_check',lambda *a,**kw:{'passed':True,'vetoes':[]})
    a=_adapter(str(tmp_path/'native'),FakeMarket({'SOL':100.,'BONK':.0001}),shared_account=account,
        fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'})
    a.connect({});store=Store(tmp_path/'scanner.db');sc=LaunchScanner.__new__(LaunchScanner);sc._close_store=store
    monkeypatch.setattr(store,'scanner_entry_context',lambda *a:(_ for _ in ()).throw(OSError('intent unavailable')))
    d=SimpleNamespace(entry_sol=.02,stop_loss_pct=30.,take_profit_pct=200.,max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=False,speed_game_min_age_min=0.)
    with pytest.raises(OSError):sc._paper_enter(None,a,{'degen_positions':{}},{'tier':'early','extra':{'complete':True}},d,a._market.mint_for('BONK'),'BONK')
    assert not a.positions() and account.snapshot()['cash']==10.


def test_ack_present_retry_carries_nested_sources_without_resetting_peak(account,tmp_path,monkeypatch):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    state={'degen_positions':{}};outbox.recover(store,a,state)
    key=next(iter(state['degen_positions']))
    persisted=get_daemon_state(store,'launch_scanner')
    persisted['narrative_tags']={'other':['cat']};persisted['smart_activity']={'other':['wallet2']}
    persisted['degen_positions']['other']={'ticket':777};persisted['sentinel']='preserve'
    save_daemon_state(store,'launch_scanner',persisted)
    stale={'degen_positions':{key:dict(state['degen_positions'][key],peak_price_sol=.000009)},
           'narrative_tags':{},'smart_activity':{}}
    outbox.recover(store,a,stale)
    assert stale['degen_positions'][key]['peak_price_sol']==.000009
    save_daemon_state(store,'launch_scanner',stale)
    after=get_daemon_state(store,'launch_scanner')
    assert after['narrative_tags']['other']==['cat'] and after['smart_activity']['other']==['wallet2']
    assert after['degen_positions']['other']['ticket']==777 and after['sentinel']=='preserve'
    assert len(store.journal_query(kind='scanner_entry_projected'))==1


def test_active_durable_ticket_conflict_has_no_entry_ack(account,tmp_path,monkeypatch):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    save_daemon_state(store,'launch_scanner',{'degen_positions':{'other':{'ticket':777}}})
    state={'degen_positions':{'other':{'ticket':888}}}
    before=get_daemon_state(store,'launch_scanner')
    outbox.recover(store,a,state)
    assert state['entry_position_conflicts']['other']['durable']=={'ticket':777}
    assert state['entry_position_conflicts']['other']['active']=={'ticket':888}
    save_daemon_state(store,'launch_scanner',state)
    assert get_daemon_state(store,'launch_scanner')==state
    assert not store.journal_query(kind='scanner_entry_projected')


def test_closed_stale_durable_lot_not_carried_into_new_entry(account,tmp_path,monkeypatch):
    from tests.test_broker_memecoin import _req
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    state={'degen_positions':{}};outbox.recover(store,a,state)
    old_key=next(iter(state['degen_positions']));old_pos=state['degen_positions'][old_key]
    a.close_position(1)
    # New actual entry plan uses a different wave/key in same unsaved cycle.
    monkeypatch.setattr(a,'submit_order',a.__class__.submit_order.__get__(a))
    sc=LaunchScanner.__new__(LaunchScanner);sc._close_store=store
    d=SimpleNamespace(entry_sol=.02,stop_loss_pct=30.,take_profit_pct=200.,max_hold_hours=4.,daily_max_loss_sol=1.,two_tier_enabled=False,speed_game_min_age_min=0.)
    active={'degen_positions':{},'current_cycle':'keep'}
    assert sc._paper_enter(None,a,active,{'tier':'early','extra':{'complete':True}},d,a._market.mint_for('BONK'),'BONK',wave=2)
    assert old_key not in active['degen_positions']
    assert {p['ticket'] for p in active['degen_positions'].values()}=={2}
    save_daemon_state(store,'launch_scanner',active)
    assert {p['ticket'] for p in get_daemon_state(store,'launch_scanner')['degen_positions'].values()}=={2}


def test_conflicting_key_isolated_without_losing_independent_native_work(account,tmp_path,monkeypatch):
    from tests.test_broker_memecoin import _req
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    monkeypatch.setattr(a,'submit_order',a.__class__.submit_order.__get__(a))
    a.submit_order(_req('BONK','BUY',.01,'independent'))
    save_daemon_state(store,'launch_scanner',{'degen_positions':{'conflict':{'ticket':777}}})
    active={'degen_positions':{'conflict':{'ticket':888},'independent':{'ticket':2}},
        'current_cycle':'keep','narrative_tags':{'new':['dog']}}
    outbox.recover(store,a,active)
    assert active['degen_positions']['conflict']=={'ticket':888}
    assert active['entry_position_conflicts']['conflict']['durable']=={'ticket':777}
    assert active['degen_positions']['independent']=={'ticket':2}
    assert active['current_cycle']=='keep' and active['narrative_tags']=={'new':['dog']}
    save_daemon_state(store,'launch_scanner',active)
    assert get_daemon_state(store,'launch_scanner')==active
    assert len(a.positions())==2 and account.snapshot()['cash']==7.


@pytest.mark.parametrize('mutation',['duplicate','economics','symbol','allocation'])
def test_bad_close_cannot_prune_scanner_lot(account,tmp_path,monkeypatch,mutation):
    import json
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    state={'degen_positions':{}};outbox.recover(store,a,state)
    a.close_position(1)
    path=a._fills_path();rows=[json.loads(x) for x in path.read_text().splitlines()]
    close=next(e for e in rows if e['kind']=='CLOSE')
    if mutation=='duplicate':rows.append(dict(close))
    elif mutation=='economics':close['allocations'][0]['proceeds_sol']=999
    elif mutation=='symbol':close['symbol']='WRONG'
    else:close['allocations'][0]['ticket']=999
    path.write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
    # Strict shared close evidence itself must reject, not derive pruning authority.
    with pytest.raises(ValueError):a.scanner_close_receipt(a.scanner_close_identity(1))
    assert len(get_daemon_state(store,'launch_scanner')['degen_positions'])==1


def test_actual_live_active_ticket_is_preserved_with_conflicting_durable_proposal(account,tmp_path,monkeypatch):
    from tests.test_broker_memecoin import _req
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    monkeypatch.setattr(a,'submit_order',a.__class__.submit_order.__get__(a))
    a.submit_order(_req('BONK','BUY',.01,'independent'))
    save_daemon_state(store,'launch_scanner',{'degen_positions':{'conflict':{'ticket':777,'context':'old'}}})
    active={'degen_positions':{'conflict':{'ticket':2,'context':'actual-live'}}}
    outbox.recover(store,a,active)
    assert active['degen_positions']['conflict']=={'ticket':2,'context':'actual-live'}
    conflict=active['entry_position_conflicts']['conflict']
    assert conflict['active']['ticket']==2 and conflict['durable']['ticket']==777
    save_daemon_state(store,'launch_scanner',active)
    assert get_daemon_state(store,'launch_scanner')==active
    assert {p.ticket for p in a.positions()}=={1,2}


def test_quarantine_blocks_affected_management_and_entry_not_independent_exit(account,tmp_path,monkeypatch):
    from tests.test_scanner_close_outbox import setup
    from tests.test_broker_memecoin import _req
    a,store,state,sc,cfg=setup(tmp_path)
    a.submit_order(_req('BONK','BUY',.02,'independent'))
    state['degen_positions']['independent']=dict(state['degen_positions']['M'],ticket=2)
    state['entry_position_conflicts']={'M':{'active':dict(state['degen_positions']['M']),
        'durable':{'ticket':999},'state':'unavailable-conflict'}}
    sc._manage_exits(cfg,a,state)
    assert {p.ticket for p in a.positions()}=={1}
    assert len(account.snapshot()['pending_sol'])==1
    assert 'M' in state['degen_positions'] and 'independent' not in state['degen_positions']
    monkeypatch.setattr(a,'submit_order',lambda *a:(_ for _ in ()).throw(AssertionError('no conflict submit')))
    assert sc._paper_enter(None,a,state,{},cfg.degen,'M','BONK') is False


def test_self_consistent_close_allocation_cannot_exceed_original_buy(account,tmp_path,monkeypatch):
    import json
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch);a.close_position(1)
    rows=[json.loads(x) for x in a._fills_path().read_text().splitlines()]
    close=next(e for e in rows if e['kind']=='CLOSE')
    close['allocations'][0]['tokens']*=2;close['allocations'][0]['basis_sol']*=2
    close['allocations'][0]['proceeds_sol']*=2;close['realized_pnl_sol']*=2
    a._fills_path().write_text('\n'.join(json.dumps(x) for x in rows)+'\n')
    with pytest.raises(ValueError,match='economics'):a.scanner_close_receipt(a.scanner_close_identity(1))


def test_settlement_bound_final_close_accepts_valid_partial_reduction(account,tmp_path,monkeypatch):
    from tests.test_broker_memecoin import _req
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    monkeypatch.setattr(a,'submit_order',a.__class__.submit_order.__get__(a))
    a.submit_order(_req('BONK','SELL',.01,'partial'))
    a.close_position(1)
    receipt=a.scanner_close_receipt(a.scanner_close_identity(1))
    assert receipt['allocations'][0]['tokens']==10000.
    assert receipt['allocations'][0]['basis_sol']==.01
    assert len(account.snapshot()['pending_sol'])==2


def test_ticket_alias_cannot_restore_or_ack_quarantined_entry(account,tmp_path,monkeypatch):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    state={'degen_positions':{},'entry_position_conflicts':{'different-key':{
        'active':{'ticket':1},'durable':{'ticket':999},'state':'unavailable-conflict'}}}
    outbox.recover(store,a,state)
    assert state['degen_positions']=={} and not store.journal_query(kind='scanner_entry_projected')
    assert len(a.positions())==1 and account.snapshot()['cash']==8.


@pytest.mark.parametrize('proposal',[{'active':{'ticket':1},'durable':{'ticket':999}},
    {'active':{'mint':'DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263'},'durable':{}}])
def test_latest_durable_quarantine_refuses_stale_caller_publication(account,tmp_path,monkeypatch,proposal):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    save_daemon_state(store,'launch_scanner',{'degen_positions':{},'entry_position_conflicts':{'alias':proposal}})
    stale={'degen_positions':{}}
    outbox.recover(store,a,stale)
    assert not stale['degen_positions'] and not store.journal_query(kind='scanner_entry_projected')
    assert stale['entry_position_conflicts']['alias']==proposal
    save_daemon_state(store,'launch_scanner',stale)
    assert get_daemon_state(store,'launch_scanner')['entry_position_conflicts']['alias']==proposal


@pytest.mark.parametrize('key',['candidates','pending_entry','sniper_pending','early_digest',
    'early_window','sustained_watch','milestone_watch','lowcap','fingerprints','sentiment',
    'winner_held','smart_fasttracked'])
@pytest.mark.parametrize('removed',[False,True])
def test_consumed_workflow_not_resurrected_by_entry_publish(account,tmp_path,monkeypatch,key,removed):
    a,store,outbox=committed_plan(account,tmp_path,monkeypatch)
    save_daemon_state(store,'launch_scanner',{'degen_positions':{},key:{'consumed':{'value':1}}})
    active={'degen_positions':{}}
    if not removed:active[key]={}
    outbox.recover(store,a,active)
    assert key not in active if removed else active[key]=={}
    save_daemon_state(store,'launch_scanner',active)
    reopened=Store(store.path)
    durable=get_daemon_state(reopened,'launch_scanner')
    assert key not in durable if removed else durable[key]=={}
    assert len(a.positions())==1 and account.snapshot()['cash']==8.
