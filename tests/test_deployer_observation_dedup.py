from types import SimpleNamespace
from daemon.launch_scanner import LaunchScanner


def test_precomputed_negative_does_not_repeat_same_staged_observation(monkeypatch):
    scanner=LaunchScanner.__new__(LaunchScanner)
    monkeypatch.setattr(scanner,'_digest_has',lambda *a:False)
    calls=[]
    monkeypatch.setattr(scanner,'_deployer_card_line',lambda *a:calls.append(a))
    monkeypatch.setattr(scanner,'_deployer_reputation_line',lambda *a:None)
    monkeypatch.setattr(scanner,'_safe_batch_card_lines',lambda *a:{})
    monkeypatch.setattr(scanner,'_safe_batch2_card_lines',lambda *a:{})
    cfg=SimpleNamespace(degen=SimpleNamespace());state={}
    scanner._enqueue_early_digest(cfg,state,cfg.degen,{'mint':'m'}, {},1,'t1','reason',{}, {},1,dtrig=None,dtrig_evaluated=True)
    assert calls==[]
    scanner._enqueue_early_digest(cfg,{},cfg.degen,{'mint':'n'}, {},1,'t2','reason',{}, {},1)
    assert len(calls)==1


def test_actual_stage_flush_cap_fallback_preserves_negative(monkeypatch):
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace())
    state={};calls=[]
    def build(*a,**kw):
        calls.append('observed')
        return {'mint':'m','symbol':'M'},None,True
    monkeypatch.setattr(scanner,'_build_early_evt',build)
    monkeypatch.setattr(scanner,'_early_allowed',lambda *a:False)
    monkeypatch.setattr(scanner,'_digest_has',lambda *a:False)
    monkeypatch.setattr(scanner,'_deployer_card_line',lambda *a:(_ for _ in ()).throw(AssertionError('same staged negative processed twice')))
    monkeypatch.setattr(scanner,'_deployer_reputation_line',lambda *a:None)
    monkeypatch.setattr(scanner,'_safe_batch_card_lines',lambda *a:{})
    monkeypatch.setattr(scanner,'_safe_batch2_card_lines',lambda *a:{})
    scanner._sniper_stage(cfg,state,cfg.degen,{'mint':'m'}, {},{},1,'t1','reason',{}, {'score':1},1)
    scanner._flush_sniper_pending(cfg,state)
    assert calls==['observed'] and len(state['early_digest'])==1


def test_actual_stage_flush_retries_failed_evaluation(monkeypatch):
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace());state={};calls=[]
    monkeypatch.setattr(scanner,'_build_early_evt',lambda *a,**kw:({'mint':'m','symbol':'M'},None,False))
    monkeypatch.setattr(scanner,'_early_allowed',lambda *a:False)
    monkeypatch.setattr(scanner,'_digest_has',lambda *a:False)
    monkeypatch.setattr(scanner,'_deployer_card_line',lambda *a:calls.append('retry'))
    monkeypatch.setattr(scanner,'_deployer_reputation_line',lambda *a:None)
    monkeypatch.setattr(scanner,'_safe_batch_card_lines',lambda *a:{})
    monkeypatch.setattr(scanner,'_safe_batch2_card_lines',lambda *a:{})
    scanner._sniper_stage(cfg,state,cfg.degen,{'mint':'m'}, {},{},1,'t1','reason',{}, {'score':1},1)
    scanner._flush_sniper_pending(cfg,state)
    assert calls==['retry']


def test_actual_card_status_distinguishes_cold_from_store_failure(monkeypatch):
    from daemon.launch_scanner import deployers
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace())
    monkeypatch.setattr(deployers,'cached_deployer',lambda *a:'dep')
    monkeypatch.setattr(deployers,'evaluate_deployer_fasttrack',lambda *a,**kw:None)
    monkeypatch.setattr(deployers,'record_launch',lambda *a:{})
    assert scanner._deployer_card_line(cfg,{},'m','solana',{},True) is None
    assert scanner._deployer_line_evaluated is True
    monkeypatch.setattr(deployers,'record_launch',lambda *a:(_ for _ in ()).throw(OSError('disk')))
    assert scanner._deployer_card_line(cfg,{},'m','solana',{},True) is None
    assert scanner._deployer_line_evaluated is False


def test_partial_launch_success_retry_reuses_staged_operation(tmp_path,monkeypatch):
    from daemon.launch_scanner import deployers
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace())
    monkeypatch.setattr(deployers,'_REGISTRY_FILE',tmp_path/'registry.json')
    monkeypatch.setattr(deployers,'cached_deployer',lambda *a:'dep')
    calls=[]
    def evaluate(*a,**kw):
        calls.append(1)
        if len(calls)==1:raise OSError('evaluation after write failed')
        return None
    monkeypatch.setattr(deployers,'evaluate_deployer_fasttrack',evaluate)
    c={'mint':'m','_deployer_launch_operation':{'id':'workflow-one','observed_at':1}}
    assert scanner._deployer_card_line(cfg,{},'m','solana',c,True) is None
    assert not scanner._deployer_line_evaluated
    assert scanner._deployer_card_line(cfg,{},'m','solana',c,True) is None
    assert scanner._deployer_line_evaluated
    assert deployers.load_registry()['dep']['launches_seen']==1
    c['_deployer_launch_operation']={'id':'workflow-two','observed_at':2}
    scanner._deployer_card_line(cfg,{},'m','solana',c,True)
    assert deployers.load_registry()['dep']['launches_seen']==2


def test_visible_launch_commit_fault_reuses_exact_operation(tmp_path,monkeypatch):
    from daemon.launch_scanner import deployers,learning_effects
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace())
    monkeypatch.setattr(deployers,'_REGISTRY_FILE',tmp_path/'registry.json')
    monkeypatch.setattr(deployers,'cached_deployer',lambda *a:'dep')
    monkeypatch.setattr(deployers,'evaluate_deployer_fasttrack',lambda *a,**kw:None)
    c={'mint':'m','_deployer_launch_operation':{'id':'workflow-one','observed_at':1}}
    real=learning_effects.os.replace
    def fault(a,b):real(a,b);raise OSError('visible commit error')
    with monkeypatch.context() as m:
        m.setattr(learning_effects.os,'replace',fault)
        scanner._deployer_card_line(cfg,{},'m','solana',c,True)
        assert not scanner._deployer_line_evaluated
    scanner._deployer_card_line(cfg,{},'m','solana',c,True)
    assert scanner._deployer_line_evaluated
    assert deployers.load_registry()['dep']['launches_seen']==1


def test_actual_staged_payload_sqlite_reopen_uses_pinned_binding(tmp_path,monkeypatch):
    from daemon.launch_scanner import deployers
    from daemon.launch_scanner.deployer_registry import read
    from daemon.common import save_daemon_state,get_daemon_state
    from storage.store import Store
    original=tmp_path/'registry.json';monkeypatch.setattr(deployers,'_REGISTRY_FILE',original)
    monkeypatch.setattr(deployers,'cached_deployer',lambda *a:'dep')
    real_launch=deployers.record_launch
    def launch_then_fail(*a,**kw):real_launch(*a,**kw);raise OSError('after committed launch')
    monkeypatch.setattr(deployers,'record_launch',launch_then_fail)
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace());state={}
    def build(config,state,degen,c,*a,**kw):
        result=scanner._deployer_card_line(config,state,c['mint'],'solana',c,True)
        return {'mint':c['mint'],'symbol':'M'},result,scanner._deployer_line_evaluated
    monkeypatch.setattr(scanner,'_build_early_evt',build)
    scanner._sniper_stage(cfg,state,cfg.degen,{'mint':'m'}, {},{},1,'t1','reason',{}, {'score':1},1)
    store=Store(tmp_path/'state.db');save_daemon_state(store,'launch_scanner',state);store.close()
    monkeypatch.setattr(deployers,'_REGISTRY_FILE',tmp_path/'wrong.json')
    monkeypatch.setattr(deployers,'cached_deployer',lambda *a:(_ for _ in ()).throw(AssertionError('must use pinned deployer')))
    monkeypatch.setattr(deployers,'record_launch',real_launch)
    fresh=LaunchScanner.__new__(LaunchScanner)
    for name,fn in [('_early_allowed',lambda *a:False),('_digest_has',lambda *a:False),('_deployer_reputation_line',lambda *a:None),('_safe_batch_card_lines',lambda *a:{}),('_safe_batch2_card_lines',lambda *a:{})]:monkeypatch.setattr(fresh,name,fn)
    store=Store(tmp_path/'state.db');loaded=get_daemon_state(store,'launch_scanner')
    fresh._flush_sniper_pending(cfg,loaded)
    assert read(original)['dep']['launches_seen']==1
    assert not (tmp_path/'wrong.json').exists() and len(loaded['early_digest'])==1


def test_real_evaluator_pinned_read_and_corruption_not_cold(tmp_path,monkeypatch):
    from daemon.launch_scanner import deployers
    from daemon.launch_scanner.deployer_registry import read
    import json
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace())
    original=tmp_path/'original.json';wrong=tmp_path/'wrong.json'
    monkeypatch.setattr(deployers,'_REGISTRY_FILE',original)
    monkeypatch.setattr(deployers,'cached_deployer',lambda *a:'dep')
    c={'mint':'m','_deployer_launch_operation':{'id':'op','observed_at':1}}
    assert scanner._deployer_card_line(cfg,{},'m','solana',c,True) is None
    assert scanner._deployer_line_evaluated
    monkeypatch.setattr(deployers,'_REGISTRY_FILE',wrong)
    assert scanner._deployer_card_line(cfg,{},'m','solana',c,True) is None
    assert scanner._deployer_line_evaluated and not wrong.exists()
    # Corrupt only evaluator read after the write's exact retry has succeeded.
    real=deployers.record_launch
    def corrupt_after(*args,**kw):
        result=real(*args,**kw);original.write_text('{bad');return result
    monkeypatch.setattr(deployers,'record_launch',corrupt_after)
    assert scanner._deployer_card_line(cfg,{},'m','solana',c,True) is None
    assert not scanner._deployer_line_evaluated


def test_strict_evaluator_missing_source_after_write_is_unavailable(tmp_path,monkeypatch):
    from daemon.launch_scanner import deployers
    scanner=LaunchScanner.__new__(LaunchScanner);cfg=SimpleNamespace(degen=SimpleNamespace())
    path=tmp_path/'registry.json';monkeypatch.setattr(deployers,'_REGISTRY_FILE',path)
    monkeypatch.setattr(deployers,'cached_deployer',lambda *a:'dep')
    real=deployers.record_launch
    def remove_after(*a,**kw):
        result=real(*a,**kw);path.unlink();return result
    monkeypatch.setattr(deployers,'record_launch',remove_after)
    c={'mint':'m','_deployer_launch_operation':{'id':'op','observed_at':1}}
    assert scanner._deployer_card_line(cfg,{},'m','solana',c,True) is None
    assert not scanner._deployer_line_evaluated
