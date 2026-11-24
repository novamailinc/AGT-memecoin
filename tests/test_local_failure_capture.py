from tests.test_paper_account_instances import account
import json,sqlite3
from daemon.failure_reports import capture_local,FailureOutbox


def test_local_capture_redacted_pending_and_no_transport(tmp_path,monkeypatch):
    def forbidden(*a,**kw):raise AssertionError('no remote transport allowed')
    monkeypatch.setattr(FailureOutbox,'deliver_one',forbidden)
    path=tmp_path/'diag.sqlite'
    try:raise OSError('credential secret must never be retained')
    except OSError as exc:rid=capture_local('scanner.learning','test.pending',exc,{'operation':'persist','secret':'credential'},path=path)
    assert rid and FailureOutbox(path).status(rid)['state']=='pending'
    with sqlite3.connect(path) as db:body=db.execute('SELECT body FROM reports').fetchone()[0]
    assert 'credential' not in body and 'secret' not in body
    assert json.loads(body)['exception']['exception_type']=='OSError'


def test_local_capture_unavailable_cannot_raise(tmp_path):
    path=tmp_path/'not-directory';path.write_text('x')
    assert capture_local('scanner.learning','test.pending',path=path/'diag.sqlite') is None


def test_actual_projection_error_records_local_report_without_blocking(account,tmp_path,monkeypatch):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import narratives
    monkeypatch.setenv('FOREX_AGENT_HOME',str(tmp_path/'home'))
    path=tmp_path/'heat.json';path.write_text('{bad')
    monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    a,store,state,scanner,cfg=setup(tmp_path)
    scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and path.read_text()=='{bad'
    dbpath=tmp_path/'home'/'diagnostics'/'failure_reports.sqlite'
    with sqlite3.connect(dbpath) as db:bodies=[json.loads(row[0]) for row in db.execute('SELECT body FROM reports')]
    assert any(body['code']=='narrative_projection_pending' for body in bodies)


def test_duplicate_pending_capture_coalesces_with_count(tmp_path):
    path=tmp_path/'diag.sqlite'
    ids=[capture_local('scanner.learning','test.pending',OSError('private'),path=path) for _ in range(10)]
    assert len(set(ids))==1
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0]==1
        assert db.execute('SELECT count FROM coalesced').fetchone()[0]==10


def test_helper_failure_does_not_escape_actual_projection(account,tmp_path,monkeypatch):
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import narratives
    from daemon import failure_reports
    path=tmp_path/'heat.json';path.write_text('{bad')
    monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    monkeypatch.setattr(failure_reports,'capture_local',lambda *a,**kw:(_ for _ in ()).throw(ImportError('capture module unavailable')))
    a,store,state,scanner,cfg=setup(tmp_path);scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and path.read_text()=='{bad'


def test_import_failure_at_projection_capture_boundary(account,tmp_path,monkeypatch):
    import builtins
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import narratives
    path=tmp_path/'heat.json';path.write_text('{bad')
    monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    original=builtins.__import__
    def fail(name,*a,**kw):
        if name=='daemon.failure_reports':raise ImportError('module initialization unavailable')
        return original(name,*a,**kw)
    monkeypatch.setattr(builtins,'__import__',fail)
    a,store,state,scanner,cfg=setup(tmp_path);scanner._manage_exits(cfg,a,state)
    assert not state['degen_positions'] and path.read_text()=='{bad'


def test_explicit_queue_capacity_preserves_pending_and_duplicate_count(tmp_path):
    import pytest
    q=FailureOutbox(tmp_path/'diag.sqlite')
    first=q.record('scanner','a',coalesce_key='a',max_pending=1)
    assert q.record('scanner','a',coalesce_key='a',max_pending=1)==first
    with pytest.raises(ValueError):q.record('scanner','b',coalesce_key='b',max_pending=1)
    assert q.status(first)['state']=='pending'
    with sqlite3.connect(q.path) as db:assert db.execute('SELECT count(*) FROM reports').fetchone()[0]==1


def test_real_lock_contention_declines_local_capture_promptly(tmp_path):
    import time
    path=tmp_path/'diag.sqlite';q=FailureOutbox(path)
    with sqlite3.connect(path) as held:
        held.execute('BEGIN EXCLUSIVE')
        start=time.monotonic()
        assert all(capture_local('scanner.learning','locked',path=path) is None for _ in range(25))
        elapsed=time.monotonic()-start
        assert elapsed<1.0
    assert capture_local('scanner.learning','locked',path=path) is not None


def test_real_diagnostics_lock_at_actual_projection(account,tmp_path,monkeypatch):
    import time
    from tests.test_scanner_close_outbox import setup
    from daemon.launch_scanner import narratives
    home=tmp_path/'home';monkeypatch.setenv('FOREX_AGENT_HOME',str(home))
    diag=home/'diagnostics'/'failure_reports.sqlite';FailureOutbox(diag)
    path=tmp_path/'heat.json';path.write_text('{bad')
    monkeypatch.setattr(narratives,'default_heat_path',lambda:path)
    a,store,state,scanner,cfg=setup(tmp_path)
    with sqlite3.connect(diag) as held:
        held.execute('BEGIN EXCLUSIVE')
        start=time.monotonic();scanner._manage_exits(cfg,a,state)
        assert time.monotonic()-start<1.0
    assert not state['degen_positions'] and path.read_text()=='{bad'


def test_multiprocess_coalescing_and_shared_capacity(tmp_path):
    import subprocess,sys
    path=tmp_path/'diag.sqlite';FailureOutbox(path)
    code='''
import sys
from daemon.failure_reports import FailureOutbox
q=FailureOutbox(sys.argv[1])
for _ in range(20):
    for key in ('a','b','c'):
        try:q.record('scanner',key,coalesce_key=key,max_pending=2)
        except ValueError:pass
'''
    workers=[subprocess.Popen([sys.executable,'-c',code,str(path)]) for _ in range(6)]
    for worker in workers:assert worker.wait(timeout=20)==0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM reports').fetchone()[0]==2
        rows=db.execute('SELECT count FROM coalesced').fetchall()
        assert len(rows)==2 and all(row[0]==120 for row in rows)
