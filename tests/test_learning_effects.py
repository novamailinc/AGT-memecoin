"""Primitive tests only, no actual scanner learning store wired yet."""
import json
import os
import pytest
from daemon.launch_scanner import learning_effects as le


def reducer(value,payload):
    value['picks']+=payload['picks'];return value


def test_exact_retry_and_conflict(tmp_path):
    p=tmp_path/'learning.json'
    assert le.apply(p,'close1',{'picks':1},reducer,{'picks':0})==(True,{'picks':1})
    assert le.apply(p,'close1',{'picks':1},lambda *a:pytest.fail('replayed'))==(False,{'picks':1})
    with pytest.raises(ValueError,match='conflict'):le.apply(p,'close1',{'picks':2},reducer)
    assert json.loads(p.read_text())['value']['picks']==1
    assert p.stat().st_mode&0o777==0o600


def test_visible_replace_then_directory_fsync_failure_retry(tmp_path,monkeypatch):
    p=tmp_path/'learning.json';original=le.os.fsync;calls=[]
    def fail_directory(fd):
        calls.append(fd)
        if p.exists():raise OSError('directory fault')
        return original(fd)
    monkeypatch.setattr(le.os,'fsync',fail_directory)
    with pytest.raises(OSError):le.apply(p,'close1',{'picks':1},reducer,{'picks':0})
    assert json.loads(p.read_text())['value']['picks']==1
    monkeypatch.setattr(le.os,'fsync',original)
    assert le.apply(p,'close1',{'picks':1},reducer)==(False,{'picks':1})


def test_reducer_failure_and_corrupt_or_legacy_store_refused(tmp_path):
    p=tmp_path/'learning.json'
    with pytest.raises(RuntimeError):le.apply(p,'x',{},lambda *a:(_ for _ in ()).throw(RuntimeError()),{})
    assert not p.exists()
    for bad in ['{','{}','{"wallet":{"picks":7}}']:
        p.write_text(bad)
        with pytest.raises((ValueError,json.JSONDecodeError)):le.apply(p,'x',{},reducer)
        assert p.read_text()==bad


def test_parallel_processes_increment_once_each(tmp_path):
    import subprocess,sys
    p=tmp_path/'learning.json'
    script='''from daemon.launch_scanner.learning_effects import apply
import sys
def reduce(v,p):return v+1
for i in range(12):apply(sys.argv[1],str(i),{'n':i},reduce,0)
'''
    jobs=[subprocess.Popen([sys.executable,'-c',script,str(p)]) for _ in range(3)]
    assert all(j.wait(timeout=10)==0 for j in jobs)
    doc=json.loads(p.read_text());assert doc['value']==12 and len(doc['effects'])==12

def test_duplicate_keys_recursively_refused(tmp_path):
    p=tmp_path/'learning.json'
    for raw in ['{"version":1,"value":0,"effects":{"x":"'+'0'*64+'"},"effects":{}}',
                '{"version":1,"value":{"a":1,"a":2},"effects":{}}']:
        p.write_text(raw)
        with pytest.raises(ValueError,match='duplicate'):le.apply(p,'x',{},lambda v,p:v)
        assert p.read_text()==raw

@pytest.mark.parametrize('payload',[{1:'a'},('a',),{None:1},{'x':{True:1}}])
def test_python_json_coercions_refused(tmp_path,payload):
    with pytest.raises(ValueError,match='exact JSON'):le.apply(tmp_path/'l','x',payload,lambda v,p:v,0)
    assert not (tmp_path/'l').exists()


def test_parent_must_exist_and_fchmod_failure_closes_descriptor(tmp_path,monkeypatch):
    with pytest.raises(ValueError,match='parent'):le.apply(tmp_path/'absent'/'l','x',{},lambda v,p:v,0)
    real=le.os.close;closed=[]
    def close(fd):closed.append(fd);return real(fd)
    monkeypatch.setattr(le.os,'close',close)
    monkeypatch.setattr(le.os,'fchmod',lambda *a:(_ for _ in ()).throw(OSError('mode')))
    with pytest.raises(OSError):le.apply(tmp_path/'l','x',{},lambda v,p:v,0)
    assert len(closed)>=len((tmp_path,*tmp_path.parents))+1
    assert not list(tmp_path.glob('.learning-effect-*'))

def test_reducer_receives_canonical_object_order(tmp_path):
    p=tmp_path/'l'
    assert le.apply(p,'x',{'z':1,'a':2},lambda v,p:list(p),[])==(True,['a','z'])
    assert le.apply(p,'x',{'a':2,'z':1},lambda *a:pytest.fail('replayed'))==(False,['a','z'])

@pytest.mark.parametrize('bad',['NaN','Infinity','-Infinity','1e999'])
def test_nonfinite_stored_value_refused_before_reducer(tmp_path,bad):
    p=tmp_path/'l';raw='{"version":1,"value":{"pnl":'+bad+'},"effects":{}}';p.write_text(raw)
    with pytest.raises(ValueError):le.apply(p,'x',{},lambda *a:pytest.fail('corrupt value reached reducer'))
    assert p.read_text()==raw

@pytest.mark.parametrize('initial',[float('nan'),{'pnl':float('inf')},{1:'coerced'}])
def test_bad_initial_refused_before_reducer(tmp_path,initial):
    with pytest.raises(ValueError):le.apply(tmp_path/'l','x',{},lambda *a:pytest.fail('bad initial reached reducer'),initial)
    assert not (tmp_path/'l').exists()
