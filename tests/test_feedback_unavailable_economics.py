import copy,json,time
import pytest
from types import SimpleNamespace
from daemon.launch_scanner import feedback,winner_model
from daemon.telegram.alerts import _tuning_weekly_review


def row(peak=None,realized=None):
    return dict(outcome_kind='paper',peak_mult=peak,realized_mult=realized,rug=None,
                exit_reason='stop_loss',hold_min=90.,t_close=time.time(),sniper_score=60.)


def test_unknown_is_not_breakeven_or_negative_runner():
    r=row();assert feedback._is_runner(r) is None
    s=feedback._stats([r]);assert s['n']==1 and s['unavailable_count']==1
    assert s['runner_rate'] is None and s['avg_realized'] is None


def test_exact_twenty_unknown_stoploss_cannot_generate_false_dead_bleed():
    rows=[row() for _ in range(20)];before=copy.deepcopy(rows)
    d=SimpleNamespace(stop_loss_pct=20)
    assert feedback.analyze_exits(d,rows,time.time(),'2026-11-23',20)==[]
    assert rows==before


@pytest.mark.parametrize('analyzer',['analyze_ranking','analyze_entry','analyze_exits'])
def test_mixed_unknown_and_known_loss_does_not_use_selected_subset(analyzer):
    rows=[row(0,0) for _ in range(20)]+[row()]
    assert getattr(feedback,analyzer)(SimpleNamespace(),rows,time.time(),'2026-11-23',20)==[]
    s=feedback._stats(rows);assert s['n']==21 and s['avg_realized'] is None


def test_actual_dataset_full_scan_blocks_unknown_cohort(tmp_path):
    path=tmp_path/'data.jsonl';path.write_text(''.join(json.dumps(row())+'\n' for _ in range(20)))
    d=SimpleNamespace(backtest_dataset_path=str(path),feedback_min_outcomes=20,stop_loss_pct=20)
    assert len(feedback.load_outcomes(path))==20
    assert feedback.analyze(d,time.time(),[])==[]


def test_zero_observed_economics_valid_not_unknown():
    s=feedback._stats([dict(row(0,0),rug=True)])
    assert s['n']==1 and s['avg_realized']==0 and s['runner_rate']==0
    assert feedback._is_runner(dict(row(0,0),rug=True)) is False


def test_weekly_unavailable_not_rendered_as_zero():
    text=_tuning_weekly_review(dict(stats=dict(n=20,runner_rate=None,avg_realized=None,unavailable_count=20)))
    assert 'unavailable' in text and '0% runners' not in text and 'x0.00' not in text


def test_unknown_labels_do_not_train_negative_winner_model():
    rows=[dict(row(12,2),rug=False),row()];before=copy.deepcopy(rows);m=winner_model.train(rows)
    assert m['n_total']==0 and m['unavailable_count']==1 and m['input_count']==2
    assert m['features']=={} and rows==before


def test_unknown_followup_not_graded_helped_or_hurt(tmp_path):
    path=tmp_path/'props.json';now=time.time()
    feedback.save_proposals([dict(id='X',status='applied',decided_ts=now-1,followup='pending',
        baseline=dict(n=20,runner_rate=.5,avg_realized=1))],path)
    assert feedback.grade_applied(path,[dict(row(),t_close=now) for _ in range(20)],now)==1
    p=feedback.load_proposals(path)[0]
    assert p['followup']=='unclear' and p['followup_evidence_state']=='unavailable'


def test_unknown_rug_label_with_known_peak_not_positive_winner():
    m=winner_model.train([row(12,None)])
    assert m['n_total']==0 and m['unavailable_count']==1


@pytest.mark.parametrize('bad',[None,0,1,'false',{},[]])
def test_rug_must_be_explicit_bool_not_truthiness(bad):
    r=dict(row(12,1),rug=bad)
    assert feedback._is_runner(r) is None
    assert feedback._stats([r])['runner_rate'] is None
    assert feedback.analyze_exits(SimpleNamespace(stop_loss_pct=20),[r]*20,time.time(),'2026-11-23',20)==[]
    assert winner_model.is_xxx(r) is None
    assert winner_model.train([r])['n_total']==0
    assert winner_model.suggest_threshold({},[r]*20) is None


@pytest.mark.parametrize('rug',[False,True])
def test_valid_bool_rug_labels_preserved(rug):
    r=dict(row(12,1),rug=rug)
    assert feedback._is_runner(r) is (not rug)
    assert winner_model.is_xxx(r) is (not rug)
    assert winner_model.train([r])['n_total']==1
