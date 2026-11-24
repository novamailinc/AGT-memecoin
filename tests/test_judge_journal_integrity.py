import json,copy
from types import SimpleNamespace
import pytest
from daemon.launch_scanner.fingerprints import FingerprintTracker
from daemon.launch_scanner import LaunchScanner


def obs(t=100.):return [t,1.,1.,1.,1.,1.,1.,None]


@pytest.mark.parametrize('kind',['reversal','count','nan','bool','negative','shape','fraction','huge'])
def test_same_mint_bad_evidence_unavailable_not_sorted_or_zeroed(tmp_path,kind):
    rows=[obs(),obs(110.)]
    if kind=='reversal':rows[1][0]=90.
    if kind=='count':rows[1][4]='broken'
    if kind=='nan':rows[1][0]=float('nan')
    if kind=='bool':rows[1][2]=True
    if kind=='negative':rows[1][3]=-1
    if kind=='shape':rows[1]=[]
    if kind=='fraction':rows[1][5]=1.5
    if kind=='huge':rows[1][0]=10**1000
    path=tmp_path/'series.jsonl';path.write_text(''.join(json.dumps(dict(mint='M',obs=o))+'\n' for o in rows));before=path.read_bytes()
    cfg=SimpleNamespace(fp_series_journal_path=str(path));tr=FingerprintTracker(cfg)
    assert tr.journal_series('M') is None
    assert tr._journal_last_read_state=='invalid' and tr.journal_status()['read_failures']==1
    assert path.read_bytes()==before
    # A valid window cannot replace invalid journal evidence in the actual recorder.
    tr._series['M']=[tuple(obs())]
    scanner=LaunchScanner.__new__(LaunchScanner);scanner._fp_tracker=tr
    target=tmp_path/'history.jsonl';cfg.backtest_dataset_path=str(target)
    scanner._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',safety={'score':80}), 'confirm',200.)
    assert not target.exists()


def test_other_mint_invalid_does_not_disable_independent_valid_series(tmp_path):
    path=tmp_path/'series.jsonl';rows=[dict(mint='BAD',obs=obs(200)),dict(mint='M',obs=obs(100)),dict(mint='BAD',obs=[]),dict(mint='M',obs=obs(100))]
    path.write_text(''.join(json.dumps(r)+'\n' for r in rows));tr=FingerprintTracker(SimpleNamespace(fp_series_journal_path=str(path)))
    assert tr.journal_series('BAD') is None
    assert tr.journal_series('M')==[tuple(obs(100)),tuple(obs(100))]
    assert tr._journal_last_read_state=='recorded'


@pytest.mark.parametrize('kind',['fraction','future','reversal','bool','negative','shape','nan'])
def test_window_fallback_validated_before_integer_cast_or_record(tmp_path,kind):
    rows=[obs(),obs(110.)]
    if kind=='fraction':rows[1][4]=1.5
    if kind=='future':rows[1][0]=201.
    if kind=='reversal':rows[1][0]=90.
    if kind=='bool':rows[1][5]=True
    if kind=='negative':rows[1][6]=-1
    if kind=='shape':rows[1]=[]
    if kind=='nan':rows[1][0]=float('nan')
    cfg=SimpleNamespace(fp_series_journal_path=str(tmp_path/'missing'),backtest_dataset_path=str(tmp_path/'history'))
    tr=FingerprintTracker(cfg);tr._series['M']=rows
    sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',safety={'score':80}),'confirm',200.)
    assert not (tmp_path/'history').exists()


def test_valid_zero_equal_window_times_retained_and_flagged_truncated(tmp_path):
    cfg=SimpleNamespace(fp_series_journal_path=str(tmp_path/'missing'),backtest_dataset_path=str(tmp_path/'history'))
    tr=FingerprintTracker(cfg);tr._series['M']=[obs(0),obs(0)]
    sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
    sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint='M',safety={'score':80}),'confirm',200.)
    r=json.loads((tmp_path/'history').read_text())
    assert [o['t'] for o in r['snapshots']]==[0,0] and r['window_truncated'] is True
