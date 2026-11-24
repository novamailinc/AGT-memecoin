"""Invalid timing evidence cannot become an early epoch through lenient math."""
import pytest
from tests.test_backtest_timestamp import _hist,_engine,backtest

BAD=['broken','180',float('nan'),float('inf'),-float('inf'),True,False,-1,{},[]]

@pytest.mark.parametrize('bad',BAD)
def test_invalid_safety_floor_blocks_v2_without_changing_v1(bad):
    h=_hist(safety_eval_t=bad)
    current=_engine(backtest.REPLAY_MODEL_V2).run([h])
    assert current['n_trades']==0
    assert current['per_gate_blocked'].get('safety-timing-unknown')==1
    assert _engine(backtest.REPLAY_MODEL_V1).run([h])['n_trades']==1

@pytest.mark.parametrize('field',['recorded_t','end_t'])
@pytest.mark.parametrize('bad',BAD)
def test_invalid_rug_availability_never_filters_later_outcome(field,bad):
    h=_hist(safety_eval_t=0.,rug=True);h['outcome'][field]=bad
    current=_engine(backtest.REPLAY_MODEL_V2).run([h])
    assert current['n_trades']==1
    assert current['n_rug_future_label_ignored']==1
    assert _engine(backtest.REPLAY_MODEL_V1).run([h])['n_trades']==0


def test_malformed_explicit_rug_time_cannot_fallback_to_other_time():
    h=_hist(safety_eval_t=0.,rug=True,outcome_recorded_t='broken',outcome_end_t=0.)
    current=_engine(backtest.REPLAY_MODEL_V2).run([h])
    assert current['n_trades']==1 and current['n_rug_future_label_ignored']==1


@pytest.mark.parametrize('bad',BAD+[None])
def test_invalid_snapshot_time_cannot_become_epoch(bad):
    h=_hist(safety_eval_t=0.);h['snapshots'][0]['t']=bad
    current=_engine(backtest.REPLAY_MODEL_V2).run([h])
    assert current['n_trades']==0 and current['per_gate_blocked'].get('snapshot-timing-invalid')==1


def test_unordered_series_refused_without_sorting_or_mutation():
    import copy
    h=_hist(safety_eval_t=0.)
    for snap,t in zip(h['snapshots'],[180.,0.,120.,60.,240.,300.]):snap['t']=t
    before=copy.deepcopy(h)
    current=_engine(backtest.REPLAY_MODEL_V2).run([h])
    assert current['n_trades']==0 and current['per_gate_blocked'].get('snapshot-timing-invalid')==1
    assert h==before
    assert _engine(backtest.REPLAY_MODEL_V1).run([h])['n_trades']==1


def test_equal_snapshot_times_preserved_as_recorded_not_deduplicated():
    h=_hist(safety_eval_t=0.);h['snapshots'][1]['t']=0.
    assert _engine(backtest.REPLAY_MODEL_V2).run([h])['n_trades']==1


def test_holdout_first_zero_is_not_replaced_by_later_positive_snapshot():
    early=_hist(safety_eval_t=0.);early['mint']='EARLY'
    later=_hist(safety_eval_t=30.);later['mint']='LATER'
    for snap in later['snapshots']:snap['t']+=30.
    split=_engine(backtest.REPLAY_MODEL_V2).run_with_holdout([later,early],.5)
    assert split['dev']['trades'][0]['mint']=='EARLY'
    assert split['eval']['trades'][0]['mint']=='LATER'


def test_holdout_invalid_series_counted_unavailable_not_trade():
    early=_hist(safety_eval_t=0.);early['mint']='VALID'
    invalid=_hist(safety_eval_t=0.);invalid['snapshots'][0]['t']=float('nan')
    split=_engine(backtest.REPLAY_MODEL_V2).run_with_holdout([invalid,early],.5)
    assert split['dev']['n_trades']+split['eval']['n_trades']==1
    assert split['rejected']['n_trades']==0
    assert split['rejected']['per_gate_blocked'].get('snapshot-timing-invalid')==1
    assert split['cohort']['rejected_count']==1


def test_holdout_overlapping_evidence_windows_reported_not_hidden():
    early=_hist(safety_eval_t=0.);late=_hist(safety_eval_t=30.)
    for snap in late['snapshots']:snap['t']+=30.
    split=_engine(backtest.REPLAY_MODEL_V2).run_with_holdout([late,early],.5)
    audit=split['snapshot_boundary']
    assert audit['status']=='overlap'
    assert audit['dev_latest_snapshot_t']==300.
    assert audit['eval_earliest_snapshot_t']==30.
    assert not audit['snapshot_windows_separated'] and not audit['stage_a_qualified']
    assert split['dev']['n_histories']+split['eval']['n_histories']==2


def test_holdout_separated_snapshot_windows_not_stage_a_qualification():
    early=_hist(safety_eval_t=0.);late=_hist(safety_eval_t=400.)
    for snap in late['snapshots']:snap['t']+=400.
    audit=_engine(backtest.REPLAY_MODEL_V2).run_with_holdout([late,early],.5)['snapshot_boundary']
    assert audit['status']=='separated' and audit['snapshot_windows_separated']
    assert not audit['stage_a_qualified']


@pytest.mark.parametrize('kind',['invalid','empty','touching'])
def test_holdout_unknown_empty_and_touching_not_separated(kind):
    early=_hist(safety_eval_t=0.);late=_hist(safety_eval_t=300.)
    for snap in late['snapshots']:snap['t']+=300.
    histories=[early,late]
    if kind=='invalid':late['snapshots'][0]['t']=float('nan')
    if kind=='empty':histories=[]
    audit=_engine(backtest.REPLAY_MODEL_V2).run_with_holdout(histories,.5)['snapshot_boundary']
    assert audit['status']=={'invalid':'unknown-timing','empty':'empty-segment','touching':'overlap'}[kind]
    assert not audit['snapshot_windows_separated'] and not audit['stage_a_qualified']


def test_invalid_cohort_cannot_move_valid_rows_across_holdout_boundary():
    import copy
    histories=[]
    for i in range(4):
        h=_hist(safety_eval_t=i*400.);h['mint']=str(i)
        for snap in h['snapshots']:snap['t']+=i*400.
        histories.append(h)
    invalid=_hist(safety_eval_t=0.);invalid['snapshots'][0]['t']='broken'
    before=copy.deepcopy(histories)
    baseline=_engine(backtest.REPLAY_MODEL_V2).run_with_holdout(histories,.5)
    augmented=_engine(backtest.REPLAY_MODEL_V2).run_with_holdout([invalid]*5+histories,.5)
    assert augmented['dev']==baseline['dev'] and augmented['eval']==baseline['eval']
    assert augmented['split_index']==baseline['split_index']==2
    assert augmented['cohort']==dict(input_count=9,admitted_count=4,rejected_count=5,
        rejection_reason='unknown snapshot chronology; excluded from split, retained in rejected report')
    assert augmented['rejected']['n_histories']==5
    assert histories==before


@pytest.mark.parametrize('field',['safety','snapshot','rug','holdout'])
def test_oversized_integer_timing_is_unavailable_not_replay_crash(field):
    h=_hist(safety_eval_t=0.,rug=field=='rug');huge=10**1000
    if field=='safety':h['safety_eval_t']=huge
    if field in ('snapshot','holdout'):h['snapshots'][0]['t']=huge
    if field=='rug':h['outcome']['recorded_t']=huge
    engine=_engine(backtest.REPLAY_MODEL_V2)
    if field=='holdout':
        result=engine.run_with_holdout([h],.5)
        assert result['cohort']['rejected_count']==1
    else:
        result=engine.run([h])
        assert result['n_trades']==(1 if field=='rug' else 0)
