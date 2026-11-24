import json
import pytest
from daemon.launch_scanner import wallet_registry as wr,smartmoney


def test_legacy_import_and_exact_outcome_preserve_raw_read_contract(tmp_path,monkeypatch):
    p=tmp_path/'wallets.json';p.write_text(json.dumps({'wallet':{'picks':2,'wins':1,'label':'known'}}))
    monkeypatch.setattr(smartmoney,'_REGISTRY_FILE',p)
    assert smartmoney.record_outcome('mint',True,['wallet'],effect_id='close1')[0]
    assert not smartmoney.record_outcome('mint',True,['wallet'],effect_id='close1')[0]
    assert wr.read(p)=={'wallet':{'picks':3,'wins':2,'label':'known'}}
    wr.mute(p,{'wallet':{'muted':True}},'rotation1')
    assert not smartmoney.record_outcome('mint',True,['wallet'],effect_id='close1')[0]
    assert wr.read(p)['wallet']['picks']==3 and wr.read(p)['wallet']['muted']
    with pytest.raises(ValueError,match='conflict'):smartmoney.record_outcome('mint',False,['wallet'],effect_id='close1')


def test_corrupt_legacy_or_canonical_refused_no_rewrite(tmp_path):
    p=tmp_path/'wallets.json'
    for raw in ['{','{"wallet":{"picks":-1}}','{"version":1,"value":{},"effects":{"x":"bad"}}']:
        p.write_text(raw)
        with pytest.raises(ValueError):wr.update(p,'id',{},lambda v,p:v)
        assert p.read_text()==raw


def test_all_actual_script_registry_readers_use_compatibility_reader():
    from pathlib import Path
    root=Path(__file__).parents[1]
    for name in ['smart-watcher','wallet-monitor','nightly-grader','intel-registries-refresh','smart-discover','smart-registry-rotate']:
        text=(root/'scripts'/name).read_text()
        assert 'from wallet_registry import read' in text
    rotate=(root/'scripts/smart-registry-rotate').read_text()
    assert 'mute(REGISTRY,changes' in rotate and 'os.replace(tmp, REGISTRY)' not in rotate

def test_actual_script_compatibility_readers_see_wallets_not_envelope(tmp_path,monkeypatch):
    import runpy,sys
    from pathlib import Path
    root=Path(__file__).parents[1];home=tmp_path
    path=home/'launch_scanner/smart_wallets.json'
    wr.update(path,'seed',{'wallet':{'picks':1,'wins':1}},lambda old,p:p)
    monkeypatch.setenv('FOREX_AGENT_HOME',str(home))
    watcher=runpy.run_path(str(root/'scripts/smart-watcher'),run_name='reader_fixture')
    assert watcher['load_registry']()=={'wallet':{'picks':1,'wins':1}}
    monitor=runpy.run_path(str(root/'scripts/wallet-monitor'),run_name='reader_fixture')
    monkeypatch.setitem(sys.modules,'wallet_grader',None)
    assert monitor['get_active_wallets']()==['wallet']
    refresh=runpy.run_path(str(root/'scripts/intel-registries-refresh'),run_name='reader_fixture')
    monkeypatch.setattr(refresh['_collect_smart_wallets'].__globals__['FC'],'is_plausible_wallet',lambda w:w=='wallet')
    candidates={};refresh['_collect_smart_wallets'](candidates)
    assert set(candidates)=={'wallet'}


def test_bootstrap_cannot_replace_existing_learned_counters(tmp_path):
    p=tmp_path/'l';wr.replace(p,{'wallet':{'picks':0,'wins':0}})
    wr.update(p,'close',{},lambda v,p:{'wallet':{'picks':1,'wins':1}})
    with pytest.raises(ValueError,match='reconciliation'):wr.replace(p,{'wallet':{'picks':0,'wins':0}})
    assert wr.read(p)['wallet']['picks']==1
