import json
import pytest
from daemon.launch_scanner import deployers as d


@pytest.fixture
def registry(tmp_path,monkeypatch):
    for name,file in [('_REGISTRY_FILE','history.json'),('_ARCHIVE_FILE','archive.jsonl'),('_ARCHIVE_LOCK_FILE','archive.lock'),('_ARCHIVE_META_FILE','meta.json')]:monkeypatch.setattr(d,name,tmp_path/file)
    return tmp_path/'history.json'


def test_exact_outcome_retry_and_conflict(registry):
    assert d.record_outcome('m','dep','rug',effect_id='one',observed_at=10)
    assert d.record_outcome('m','dep','rug',effect_id='one',observed_at=10)
    assert not d.record_outcome('m','dep','dead',effect_id='one',observed_at=10)
    assert d.load_registry()['dep']['rugs']==1


def test_stale_snapshot_cannot_erase_update(registry):
    d.record_launch('dep',effect_id='a',observed_at=1);stale=d.load_registry()
    d.record_launch('dep',effect_id='b',observed_at=2)
    stale['dep']['launches_seen']=99
    assert not d.save_registry(stale)
    assert d.load_registry()['dep']['launches_seen']==2


def test_visible_replace_failure_exact_retry(registry,monkeypatch):
    from daemon.launch_scanner import learning_effects as e
    real=e.os.replace
    def fault(a,b):real(a,b);raise OSError('visible failure')
    with monkeypatch.context() as m:
        m.setattr(e.os,'replace',fault)
        assert not d.record_outcome('m','dep','rug',effect_id='one',observed_at=10)
    assert d.record_outcome('m','dep','rug',effect_id='one',observed_at=10)
    assert d.load_registry()['dep']['rugs']==1


def test_legacy_and_corruption_refusal(registry):
    registry.write_text(json.dumps({'dep':{'launches_seen':1,'rugs':0,'first_seen':1}}))
    assert d.record_outcome('m','dep','rug',effect_id='one',observed_at=10)
    assert d.load_registry()['dep']['rugs']==1
    registry.write_text('{bad')
    assert not d.record_outcome('m','dep','rug',effect_id='two',observed_at=10)
    with pytest.raises(ValueError):d.load_registry()
    assert registry.read_text()=='{bad'


def test_archive_eviction_outside_pure_reducer_exact_retry(registry,monkeypatch):
    monkeypatch.setattr(d,'_MAX_REGISTRY',1)
    d.record_launch('old',effect_id='old',observed_at=1)
    d.record_launch('new',effect_id='new',observed_at=2)
    before=d._ARCHIVE_FILE.read_text()
    d.record_launch('new',effect_id='new',observed_at=2)
    assert d._ARCHIVE_FILE.read_text()==before
    assert set(d.load_registry())=={'new'}


def test_concurrent_process_outcomes_never_lose_counters(registry):
    import subprocess,sys,os
    code='''
import sys
from pathlib import Path
from daemon.launch_scanner import deployers as d
d._REGISTRY_FILE=Path(sys.argv[1])
for i in range(5):assert d.record_outcome('m','dep','rug',effect_id=sys.argv[2]+str(i),observed_at=10)
'''
    jobs=[subprocess.Popen([sys.executable,'-c',code,str(registry),str(i)],env=dict(os.environ,FOREX_AGENT_TESTING='1'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(3)]
    for job in jobs:
        _,err=job.communicate(timeout=20);assert job.returncode==0,err
    assert d.load_registry()['dep']['rugs']==15


def test_archive_then_registry_fault_retains_live_and_retries_segment(registry,monkeypatch):
    from daemon.launch_scanner import learning_effects as e
    monkeypatch.setattr(d,'_MAX_REGISTRY',1)
    d.record_launch('old',effect_id='old',observed_at=1)
    real=e.os.replace
    def fail_registry(a,b):
        if str(b)==str(registry):raise OSError('registry replacement unavailable')
        real(a,b)
    with monkeypatch.context() as m:
        m.setattr(e.os,'replace',fail_registry)
        with pytest.raises(OSError):d.record_launch('new',effect_id='new',observed_at=2)
    assert set(d.load_registry())=={'old'}
    assert d._ARCHIVE_FILE.exists()
    d.record_launch('new',effect_id='new',observed_at=2)
    assert set(d.load_registry())=={'new'}
    archive=d.deployer_archive('old')
    assert archive['records'][0]['launches_seen']==1


def test_primary_reader_compatibility_and_invalid_counter(registry):
    from daemon.launch_scanner.deployer_registry import read
    from pathlib import Path
    import runpy
    d.record_launch('dep',effect_id='launch',observed_at=1)
    assert set(read(registry))=={'dep'}
    radar=runpy.run_path('scripts/launch-radar')
    fn=radar['load_qualifying_deployers'];fn.__globals__['DEPLOYER_FILE']=str(registry)
    assert fn()=={}
    assert set(read(registry))=={'dep'}
    registry.write_text(json.dumps({'dep':{'rugs':-1}}))
    with pytest.raises(ValueError):read(registry)


def test_archive_directory_sync_failure_retains_live(registry,tmp_path,monkeypatch):
    from daemon.launch_scanner import learning_effects as e
    monkeypatch.setattr(d,'_MAX_REGISTRY',1)
    archive=tmp_path/'separate'/'archive.jsonl';monkeypatch.setattr(d,'_ARCHIVE_FILE',archive)
    d.record_launch('old',effect_id='old',observed_at=1)
    real=e._sync_directory
    def fail(path):
        if str(path)==str(archive.parent):raise OSError('archive directory sync unavailable')
        return real(path)
    with monkeypatch.context() as m:
        m.setattr(e,'_sync_directory',fail)
        d.record_launch('new',effect_id='new',observed_at=2)
    assert set(d.load_registry())=={'old','new'}


def test_separate_meta_sync_failure_blocks_archive_success(registry,tmp_path,monkeypatch):
    from daemon.launch_scanner import learning_effects as e
    meta=tmp_path/'separate-meta'/'meta.json';monkeypatch.setattr(d,'_ARCHIVE_META_FILE',meta)
    monkeypatch.setattr(d,'_MAX_REGISTRY',1)
    d.record_launch('old',effect_id='old',observed_at=1)
    real=e._sync_directory
    def fail(path):
        if str(path)==str(meta.parent):raise OSError('meta directory unavailable')
        return real(path)
    with monkeypatch.context() as m:
        m.setattr(e,'_sync_directory',fail)
        d.record_launch('new',effect_id='new',observed_at=2)
    assert set(d.load_registry())=={'old','new'}


def test_compaction_diagnostics_persist_before_source_drop(registry,tmp_path,monkeypatch):
    monkeypatch.setattr(d,'_ARCHIVE_MAX_BYTES',1)
    d._ARCHIVE_FILE.write_text('{bad}\n')
    original=d._ARCHIVE_FILE.read_text()
    monkeypatch.setattr(d,'_meta_write',lambda meta:(_ for _ in ()).throw(OSError('meta unavailable')))
    assert not d._archive_deployer('dep',{'first_seen':1,'launches_seen':1},2)
    assert d._ARCHIVE_FILE.read_text().startswith(original)


def test_first_winner_claim_survives_retry_competition_and_eviction(registry,monkeypatch):
    first=d.claim_outcome('m','old','rug',1,'close-one')
    assert d.claim_outcome('m','old','hit_2x',2,'expiry')==first
    assert d.claim_outcome('m','old','rug',1,'close-one')==first
    assert d.load_registry()['old']['rugs']==1
    monkeypatch.setattr(d,'_MAX_REGISTRY',1)
    d.record_launch('new',effect_id='new',observed_at=3)
    assert 'old' not in d.load_registry()
    assert d.claim_outcome('m','old','dead',4,'later')==first
    assert 'old' not in d.load_registry()


def test_claim_visible_failure_and_cas_snapshot_keeps_claim(registry,monkeypatch):
    from daemon.launch_scanner import learning_effects as e
    real=e.os.replace
    def fault(a,b):real(a,b);raise OSError('visible failure')
    with monkeypatch.context() as m:
        m.setattr(e.os,'replace',fault)
        with pytest.raises(OSError):d.claim_outcome('m','dep','rug',1,'close')
    claim=d.claim_outcome('m','dep','rug',1,'close')
    snap=d.load_registry();snap['dep']['launches_seen']=2
    assert d.save_registry(snap)
    assert d.claim_outcome('m','dep','dead',3,'expiry')==claim
    assert d.load_registry()['dep']['rugs']==1


def test_old_envelope_receipt_retry_imports_without_loss(registry):
    import hashlib
    from daemon.launch_scanner.learning_effects import _encoded
    payload={'kind':'outcome','mint':'m','deployer':'dep','outcome':'rug','ts':1}
    registry.write_text(json.dumps({'version':1,'value':{'dep':{'launches_seen':1,'rugs':1}},
        'effects':{'old':hashlib.sha256(_encoded(payload).encode()).hexdigest()}}))
    assert d.record_outcome('m','dep','rug',effect_id='old',observed_at=1)
    assert d.load_registry()['dep']['rugs']==1
    d.claim_outcome('n','dep','dead',2,'expiry')
    assert d.load_registry()['dep']['rugs']==1


def test_competing_child_claims_choose_single_durable_winner(registry):
    import subprocess,sys,os
    source='''
import sys
from pathlib import Path
from daemon.launch_scanner import deployers as d
d._REGISTRY_FILE=Path(sys.argv[1])
d.claim_outcome('m','dep',sys.argv[2],1,sys.argv[2])
'''
    jobs=[subprocess.Popen([sys.executable,'-c',source,str(registry),outcome],env=dict(os.environ,FOREX_AGENT_TESTING='1'),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for outcome in ('rug','hit_2x','dead')]
    for job in jobs:
        _,err=job.communicate(timeout=20);assert job.returncode==0,err
    entry=d.load_registry()['dep']
    assert sum(entry.get(k,0) for k in ('rugs','hits_2x','dead'))==1
    winner=d._registry_doc()['value']['grade_claims']['m']
    assert winner['outcome'] in ('rug','hit_2x','dead')


@pytest.mark.parametrize('missing',['schema_version','registry','grade_claims'])
def test_damaged_claim_schema_never_reimports_as_legacy(registry,missing):
    d.claim_outcome('m','dep','rug',1,'close')
    doc=json.loads(registry.read_text());doc['value'].pop(missing)
    registry.write_text(json.dumps(doc));before=registry.read_bytes()
    with pytest.raises(ValueError):d.load_registry()
    with pytest.raises(ValueError):d.claim_outcome('m','dep','dead',2,'expiry')
    assert registry.read_bytes()==before
