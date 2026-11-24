import pytest
from types import SimpleNamespace
from daemon.launch_scanner import LaunchScanner,close_outbox
from tests.test_paper_account_instances import account


def build(tmp_path,entry=1.,close=0.,pnl=-1.):
    cfg=SimpleNamespace(degen=SimpleNamespace(backtest_enabled=True,backtest_dataset_path=str(tmp_path/'dataset.jsonl')))
    return LaunchScanner.__new__(LaunchScanner)._backtest_record_paper_close(
        cfg,{},'M','M',{'entry_ts':1.,'peak_price_sol':entry},entry,close,
        'test',pnl,2.,record_only=True,snapshots_override=[])['record']


def test_legacy_missing_basis_not_breakeven(tmp_path):
    r=build(tmp_path,entry=0.)
    assert r['realized_mult'] is None and r['peak_mult'] is None and r['rug'] is None
    assert r['pnl_sol']==-1 and r['native_allocation_state']=='unrecorded-legacy-source'


@pytest.mark.parametrize('bad',[None,'1',True,float('nan'),float('inf'),10**1000])
def test_legacy_invalid_source_economics_never_filled(tmp_path,bad):
    r=build(tmp_path,entry=bad,pnl=bad)
    assert r['realized_mult'] is None and r['pnl_sol'] is None


def test_legacy_explicit_zero_close_is_recorded_loss(tmp_path):
    r=build(tmp_path)
    assert r['realized_mult']==0 and r['pnl_sol']==-1 and r['rug'] is True


def receipt(basis=2.,proceeds=1.):
    return dict(ticket=1,realized_pnl_sol=proceeds-basis,
        allocations=[dict(ticket=1,basis_sol=basis,proceeds_sol=proceeds,tokens=4.)])


def test_native_basis_preserved_without_price_ratio_inference():
    r=receipt();e=close_outbox.native_history_economics(r)
    assert e['basis_sol']==2 and e['proceeds_sol']==1 and e['realized_pnl_sol']==-1
    assert e['net_proceeds_over_basis']==.5 and e['net_multiple_state']=='recorded'
    assert r==receipt()


def test_native_zero_basis_return_unavailable_not_one():
    e=close_outbox.native_history_economics(receipt(0,1))
    assert e['net_proceeds_over_basis'] is None and e['basis_sol']==0


def test_native_zero_proceeds_exact_total_loss():
    e=close_outbox.native_history_economics(receipt(2,0))
    assert e['net_proceeds_over_basis']==0 and e['realized_pnl_sol']==-2


def test_native_contradictory_pnl_refused():
    r=receipt();r['realized_pnl_sol']=99
    with pytest.raises(ValueError):close_outbox.native_history_economics(r)


def test_old_committed_evidence_replay_not_rewritten(account,tmp_path):
    from tests.test_scanner_close_outbox import setup
    import copy,time,json
    a,store,state,scanner,cfg=setup(tmp_path)
    pos=state['degen_positions']['M'];identity=a.scanner_close_identity(1)
    context=close_outbox.prepare(store,identity,pos,'M','max_hold',.000001,time.time(),state,.02,
        backtest_spec={'path':str(tmp_path/'data.jsonl'),'record':{}})
    a.close_position(1);r=a.scanner_close_receipt(identity)
    close_outbox.commit(store,context,r)
    old=store.journal_query(kind='scanner_close_committed')[0]['detail']
    for field in ('native_allocation','native_allocation_state','realized_mult_metric'):
        old['backtest_spec']['record'].pop(field)
    old['legacy_reporting_schema']='preserved'
    payload=dict(kind='scanner_close_committed',symbol=close_outbox.key(identity),detail=old)
    store._conn.execute("UPDATE journal SET payload=? WHERE kind='scanner_close_committed'",(json.dumps(payload),));store._conn.commit()
    before=copy.deepcopy(old)
    close_outbox.commit(store,context,r)
    assert store.journal_query(kind='scanner_close_committed')[0]['detail']==before
    bad=copy.deepcopy(r);bad['new_conflicting_source']='not exact'
    with pytest.raises(ValueError,match='source conflict'):close_outbox.commit(store,context,bad)
