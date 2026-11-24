"""Each accumulating hybrid check must fail pytest without leaking state."""
import ast
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
MODULES=('curve_cache','curve_cache_empty','helius_429_watch','helius_no_bypass',
         'helius_quiet','helius_vault','intelligence_decoders_pump',
         'intelligence_events','intelligence_events_store','intelligence_failure_matrix',
         'pidfile_rollout','reserves_curl','reserves_fallback','security_fixes')

@pytest.mark.parametrize('name',MODULES)
def test_each_hybrid_guard_propagates_and_isolates(name,tmp_path):
    source=(ROOT/'tests'/('test_'+name+'.py')).read_text()
    tree=ast.parse(source)
    check=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='check')
    guard=next((n for n in tree.body if isinstance(n,ast.If) and
        any(isinstance(x,ast.ImportFrom) and x.module=='tests.recorded_checks' for x in ast.walk(n))),None)
    # Without the source's wiring, copied checks falsely pass in pytest.
    code='FAILS=[]\nFAILURES=[]\nPASSES=[0]\nTOTAL=[0]\nMATRIX=[]\n'+ast.get_source_segment(source,check)+'\n'
    if guard is not None:code+=ast.get_source_segment(source,guard)+'\n'
    code+='''
def test_first():
    check('CAUSAL_FAILURE',False)
    check('ACCUMULATED_FAILURE',False)
def test_second():
    assert FAILS==[] and FAILURES==[] and PASSES==[0] and TOTAL==[0] and MATRIX==[]
    check('POSITIVE',True)
'''
    probe=tmp_path/('test_probe_'+name+'.py');probe.write_text(code)
    result=subprocess.run([sys.executable,'-m','pytest','-q',str(probe)],env=dict(os.environ,PYTHONPATH=str(ROOT)),capture_output=True,text=True)
    assert result.returncode==1,result.stdout+result.stderr
    assert '2 passed, 1 error' in result.stdout,result.stdout
    assert 'CAUSAL_FAILURE' in result.stdout and 'ACCUMULATED_FAILURE' in result.stdout
