"""A check()-recorded failure must become a pytest failure, without leakage."""
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'tests/test_intelligence_failure_matrix.py'


def test_recorded_failure_fails_own_pytest_and_next_test_is_clean(tmp_path):
    source=SOURCE.read_text()
    source=source.replace('DATA = os.path.join(os.path.dirname(__file__), "data", "intelligence")',
                          'DATA = '+repr(str(ROOT/'tests/data/intelligence')))
    probe=tmp_path/'test_harness_probe.py'
    probe.write_text(source+'''

def test_harness_probe_failure():
    check('CAUSAL_SENTINEL', False)
    check('STILL_ACCUMULATES', False)

def test_harness_probe_next_clean():
    assert FAILS == [] and PASSES == [0] and MATRIX == []
    check('POSITIVE_CONTROL', True)
''')
    result=subprocess.run([sys.executable,'-m','pytest','-q',str(probe)+'::test_harness_probe_failure',str(probe)+'::test_harness_probe_next_clean'],
        env=dict(os.environ,PYTHONPATH=str(ROOT)),text=True,capture_output=True)
    assert result.returncode==1, result.stdout+result.stderr
    assert '2 passed, 1 error' in result.stdout, result.stdout
    assert 'CAUSAL_SENTINEL' in result.stdout and 'STILL_ACCUMULATES' in result.stdout


def test_direct_main_retains_full_failure_accumulation(tmp_path):
    probe=tmp_path/'direct_probe.py'
    probe.write_text('''
import importlib.util
import sys
spec=importlib.util.spec_from_file_location('matrix_probe',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
names=['test_outage_matrix','test_helius_429_storm','test_wss_failures','test_event_pipeline_failures','test_cache_correctness','test_cost_under_failure']
for name in names:
    setattr(m,name,lambda name=name:m.check(name,False))
assert m.main()==1
assert len(m.FAILS)==6
print('DIRECT_ACCUMULATION_PRESERVED')
''')
    result=subprocess.run([sys.executable,str(probe),str(SOURCE)],env=dict(os.environ,PYTHONPATH=str(ROOT)),text=True,capture_output=True)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'DIRECT_ACCUMULATION_PRESERVED' in result.stdout
