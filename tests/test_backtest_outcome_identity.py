"""Exact opt-in history writer tests, independent of scanner projection."""
import json
from unittest.mock import patch
from daemon.launch_scanner import backtest


class TestBacktestOutcomeIdentity:
    def test_exact_retry_conflict_and_legacy_retained(self,tmp_path):
        path=str(tmp_path/'history.jsonl')
        legacy={'mint':'M','pnl_sol':-.2}
        rec={'outcome_id':'book-ticket-1','mint':'M','pnl_sol':-.01}
        assert backtest.record_history(path,legacy)
        assert backtest.record_history(path,rec)
        assert backtest.record_history(path,rec)
        assert not backtest.record_history(path,dict(rec,pnl_sol=1.))
        assert [json.loads(line) for line in open(path)]==[legacy,rec]

    def test_visible_replace_failure_retry_is_exact(self,tmp_path):
        path=str(tmp_path/'history.jsonl');rec={'outcome_id':'book-ticket-1','pnl_sol':-.01}
        from daemon.launch_scanner import backtest
        real=backtest.os.replace
        def visible(a,b):
            real(a,b);raise OSError('visible but response failed')
        with patch.object(backtest.os,'replace',side_effect=visible):
            assert not backtest.record_history(path,rec)
        assert backtest.record_history(path,rec)
        assert [json.loads(line) for line in open(path)]==[rec]

    def test_corrupt_history_never_replaced(self,tmp_path):
        p=tmp_path/'history.jsonl';p.write_text('{corrupt\\n')
        assert not backtest.record_history(str(p),{'outcome_id':'one'})
        assert p.read_text()=='{corrupt\\n'

    def test_two_process_same_id_and_legacy_append(self,tmp_path):
        import os,subprocess,sys
        path=str(tmp_path/'history.jsonl')
        code='from daemon.launch_scanner.backtest import record_history; import sys; assert record_history(sys.argv[1],{"outcome_id":"one","pnl_sol":-.01}); assert record_history(sys.argv[1],{"legacy":sys.argv[2]})'
        env=dict(os.environ,FOREX_AGENT_TESTING='1')
        jobs=[subprocess.Popen([sys.executable,'-c',code,path,str(i)],env=env) for i in range(2)]
        assert [p.wait() for p in jobs]==[0,0]
        rows=[json.loads(line) for line in open(path)]
        assert len(rows)==3 and sum(r.get('outcome_id')=='one' for r in rows)==1

    def test_unrelated_history_identity_conflicts_block_new_and_retry(self,tmp_path):
        import pytest
        cases=[
            [{'outcome_id':'old','pnl_sol':-.2},{'outcome_id':'old','pnl_sol':1.}],
            [{'outcome_id':'old','pnl_sol':-.2},{'outcome_id':'old','pnl_sol':-.2}],
            [{'outcome_id':''}], [{'outcome_id':None}], [{'outcome_id':1}],
        ]
        for i,existing in enumerate(cases):
            target={'outcome_id':'target','pnl_sol':-.01}
            for target_exists in (False,True):
                path=tmp_path/('history-%s-%s.jsonl'%(i,target_exists))
                path.write_text(''.join(json.dumps(r)+'\n' for r in existing+([target] if target_exists else [])))
                before=path.read_bytes()
                assert not backtest.record_history(str(path),target)
                assert path.read_bytes()==before
