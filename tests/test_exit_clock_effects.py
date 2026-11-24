import json
import pytest
from types import SimpleNamespace
from daemon.launch_scanner import exit_liquidity_clock as clock


def test_legacy_import_exact_effect_and_reader(tmp_path):
    path=tmp_path/'clock.json';path.write_text('[{"mint":"old","close_ts":1}]')
    record={'mint':'new','close_ts':2,'hold_h':1,'close_mult':1,'peak_mult':1,'pnl_sol':0}
    assert clock.append_record(str(path),record,'close1')[0]
    assert not clock.append_record(str(path),record,'close1')[0]
    assert clock._all_records(str(path))==[{'mint':'old','close_ts':1},record]
    with pytest.raises(ValueError):clock.append_record(str(path),dict(record,close_ts=3),'close1')


def test_build_does_not_consume_peak_or_write(tmp_path):
    clock._PEAKS.clear();clock.note_price('M',3,1100)
    path=tmp_path/'clock.json'
    rec=clock.record_close(SimpleNamespace(),'M',900,1,1300,2,3,'exit',.1,path=str(path),record_only=True)
    assert rec['time_to_peak_h']==round(200/3600,4)
    assert clock._PEAKS['M']==[3,1100] and not path.exists()


def test_corruption_never_overwritten(tmp_path):
    path=tmp_path/'clock.json';path.write_text('{bad')
    with pytest.raises(ValueError):clock.append_record(str(path),{'mint':'M'},'close')
    assert path.read_text()=='{bad'


@pytest.mark.parametrize('bad',[None,'bad',{}, {'mint':'M','close_ts':float('nan')}])
def test_bad_record_never_committed(tmp_path,bad):
    path=tmp_path/'clock.json'
    with pytest.raises(ValueError):clock.append_record(str(path),bad,'bad')
    assert not path.exists()


def test_ttl_pruned_on_append_and_invalid_builder_refused(tmp_path):
    path=tmp_path/'clock.json';path.write_text('[{"mint":"expired","close_ts":1}]')
    record={'mint':'new','close_ts':clock._TTL_S+2,'hold_h':1,'close_mult':1,'peak_mult':1,'pnl_sol':0}
    clock.append_record(str(path),record,'new')
    assert clock._all_records(str(path))==[record]
    assert clock.record_close(SimpleNamespace(),'M',1,1,2,float('inf'),2,'x',0,record_only=True) is None


def test_two_process_exact_append_and_retained_receipts(tmp_path):
    import os,subprocess,sys
    path=tmp_path/'clock.json'
    code='''import sys
from daemon.launch_scanner.exit_liquidity_clock import append_record
r={'mint':'M','close_ts':100,'hold_h':1,'close_mult':1,'peak_mult':1,'pnl_sol':0}
append_record(sys.argv[1],r,'same-close')
'''
    env=dict(os.environ,FOREX_AGENT_TESTING='1')
    jobs=[subprocess.Popen([sys.executable,'-c',code,str(path)],env=env) for _ in range(2)]
    assert [job.wait() for job in jobs]==[0,0]
    assert len(clock._all_records(str(path)))==1
    newer={'mint':'N','close_ts':clock._TTL_S+200,'hold_h':1,'close_mult':1,'peak_mult':1,'pnl_sol':0}
    clock.append_record(str(path),newer,'newer-close')
    old={'mint':'M','close_ts':100,'hold_h':1,'close_mult':1,'peak_mult':1,'pnl_sol':0}
    assert not clock.append_record(str(path),old,'same-close')[0]
    assert clock._all_records(str(path))==[newer]



def test_out_of_order_replay_cannot_evict_newer_or_bypass_ttl(tmp_path,monkeypatch):
    monkeypatch.setattr(clock,'_MAX_RECORDS',2)
    path=tmp_path/'clock.json'
    def rec(ts,mint):return {'mint':mint,'close_ts':ts,'hold_h':1,'close_mult':1,'peak_mult':1,'pnl_sol':0}
    a=rec(1000000,'A');b=rec(1000001,'B');old=rec(1,'OLD')
    clock.append_record(str(path),a,'a');clock.append_record(str(path),b,'b')
    clock.append_record(str(path),old,'old')
    assert clock._all_records(str(path))==[a,b]
    assert not clock.append_record(str(path),old,'old')[0]
    assert clock._all_records(str(path))==[a,b]
    # Same-time ties are deterministic, not arrival ordered.
    c=rec(1000001,'C');clock.append_record(str(path),c,'c')
    assert clock._all_records(str(path))==[b,c]
