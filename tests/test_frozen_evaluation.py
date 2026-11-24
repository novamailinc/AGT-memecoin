import json,hashlib,subprocess,sys,copy
from pathlib import Path
import pytest
from core import frozen_evaluation as F
from daemon.launch_scanner import sources as S,provider_inputs as P,LaunchScanner
from daemon.launch_scanner.fingerprints import FingerprintTracker
from types import SimpleNamespace


def fixture(root):
    config={'schema_version':1,'replay_model':'v2-timestamp-correct','require_provider_inputs':True,'seed':0,
            'entry_rules':{'min_safety_score':50.,'require_known_safety':True,'max_entry_mcap_usd':500000.},
            'exit_rules':{'tp_ladder':[],'trailing_stop_pct':30.,'time_stop_h':6.},
            'costs':{k:0. for k in F.COST}}
    rows=[]
    for mint,t in (('A',100.),('B',300.)):
        payload=dict(baseToken={'address':mint},chainId='solana',pairAddress='PAIR',marketCap='100',priceUsd='1',liquidity={'usd':'10'},volume={'m5':'5'},txns={'m5':{'buys':1,'sells':0}})
        stats=P.resolved(S._pair_to_stats(payload),mint,'solana','highest-liquidity-base-token-pair')
        cfg=SimpleNamespace(fp_series_journal_path=str(root/(mint+'series')),backtest_dataset_path=str(root/(mint+'history')))
        tr=FingerprintTracker(cfg);tr.observe(mint,stats,t);sc=LaunchScanner.__new__(LaunchScanner);sc._fp_tracker=tr
        sc._backtest_record(SimpleNamespace(degen=cfg),{},dict(mint=mint,chain='solana',safety={'score':80},safety_eval_t=t),'confirm',t+10)
        h=json.loads((root/(mint+'history')).read_text());h['history_id']=mint;rows.append(h)
    contents={'dataset':'\n'.join(json.dumps(h) for h in rows)+'\n','config':json.dumps(config),
              'calibration':json.dumps({'schema_version':1,'model_id':'fixture-v1','evidence_kind':'scenario-only','source_roles':['calibration-source-fixture'],'limitations':'synthetic uncalibrated','config_sha256':F.sha(json.dumps(config).encode())}),
              'calibration-source-fixture':json.dumps({'synthetic':True}),'strategy-source':'not executed\n'}
    artifacts=[]
    for role,text in contents.items():
        path=role+'.json';(root/path).write_text(text);data=(root/path).read_bytes();artifacts.append({'role':role,'path':path,'sha256':F.sha(data),'byte_count':len(data)})
    m={'schema_version':1,'artifacts':artifacts,'partition':{'cutoff':200.,'embargo_s':10.,'dev':['A'],'eval':['B']},'calibration_model_id':'fixture-v1'}
    (root/'manifest.json').write_text(json.dumps(m));return m


def rewrite(root,m,role,text):
    a=next(a for a in m['artifacts'] if a['role']==role);(root/a['path']).write_text(text);data=(root/a['path']).read_bytes();a.update(sha256=F.sha(data),byte_count=len(data));(root/'manifest.json').write_text(json.dumps(m))


def test_actual_cli_existing_engine_deterministic_unqualified(tmp_path):
    fixture(tmp_path)
    first=F.evaluate(tmp_path,'manifest.json');second=F.evaluate(tmp_path,'manifest.json')
    assert first==second and first['status']=='complete-unqualified' and not first['stage_a_qualified']
    assert first['strategy_execution_binding'].startswith('unverified')
    r=subprocess.run([sys.executable,'-m','core.frozen_evaluation','--root',str(tmp_path),'--manifest','manifest.json','--output','report.json'],capture_output=True,text=True)
    assert r.returncode==0,r.stdout+r.stderr
    result=json.loads((tmp_path/'report.json').read_text());assert result['publication']=='complete' and result['result']==first
    again=subprocess.run([sys.executable,'-m','core.frozen_evaluation','--root',str(tmp_path),'--manifest','manifest.json','--output','report.json'],capture_output=True,text=True)
    assert again.returncode==2 and 'unpublished-refusal' in again.stdout


@pytest.mark.parametrize('role',['config','calibration','calibration-source-fixture','dataset'])
@pytest.mark.parametrize('bad',['{"duplicate":1,"duplicate":2}','{"bad":NaN}','{"bad":1e999}','\udcff'])
def test_strict_parsing_every_json_artifact(tmp_path,role,bad):
    m=fixture(tmp_path)
    if '\udcff' in bad:
        a=next(a for a in m['artifacts'] if a['role']==role);data=b'\xff';(tmp_path/a['path']).write_bytes(data);a.update(sha256=F.sha(data),byte_count=1);(tmp_path/'manifest.json').write_text(json.dumps(m))
    else:rewrite(tmp_path,m,role,bad)
    result=F.evaluate(tmp_path,'manifest.json');assert result['status']=='refused' and not result['stage_a_qualified']


@pytest.mark.parametrize('mutation',['hash','missing','symlink','membership','touch','embargo','empty','partialconfig','duplicateidentity','output'])
def test_artifact_and_partition_refusals(tmp_path,mutation):
    m=fixture(tmp_path)
    if mutation=='hash':(tmp_path/'config.json').write_text('{}')
    elif mutation=='missing':(tmp_path/'config.json').unlink()
    elif mutation=='symlink':(tmp_path/'config.json').unlink();(tmp_path/'config.json').symlink_to('/etc/passwd')
    elif mutation=='membership':m['partition']['eval']=['A']
    elif mutation=='touch':m['partition']['cutoff']=110.
    elif mutation=='embargo':m['partition']['embargo_s']=100.
    elif mutation=='empty':m['partition']['eval']=[]
    elif mutation=='partialconfig':rewrite(tmp_path,m,'config','{}')
    elif mutation=='duplicateidentity':
        text=(tmp_path/'dataset.json').read_text();rewrite(tmp_path,m,'dataset',text+text)
    else:
        with pytest.raises((ValueError,OSError)):F.publish(tmp_path,'manifest.json',{}, {'manifest.json'})
        return
    (tmp_path/'manifest.json').write_text(json.dumps(m))
    assert F.evaluate(tmp_path,'manifest.json')['status']=='refused'


def test_global_bad_line_keeps_raw_and_unknown_totals_not_subset_performance(tmp_path):
    m=fixture(tmp_path);rewrite(tmp_path,m,'dataset',(tmp_path/'dataset.json').read_text()+'{\n')
    r=F.evaluate(tmp_path,'manifest.json')
    assert r['status']=='refused' and not r['dataset']['economic_totals_complete'] and r['dataset']['line_count']==3
    assert r['dataset']['records'][-1]['line']==3 and 'dev' not in r
    assert r['artifacts']['dataset']['retained_base64']


def test_unknown_provider_cohort_blocks_every_segment(tmp_path):
    m=fixture(tmp_path);rows=[json.loads(l) for l in (tmp_path/'dataset.json').read_text().splitlines()];rows[1]['snapshot_input_evidence']['rows'][0]['evidence']['version']=2
    # Capture is intentionally stale. Refuse unknown availability window rather
    # than repairing a changed input just to manufacture an evaluable cohort.
    rewrite(tmp_path,m,'dataset','\n'.join(json.dumps(r) for r in rows))
    r=F.evaluate(tmp_path,'manifest.json');assert not r['whole_cohort']['cohort_qualified'] and r['status']=='refused'


def test_safe_publication_symlink_and_input_never_overwritten(tmp_path):
    fixture(tmp_path);original=(tmp_path/'config.json').read_bytes();(tmp_path/'link').symlink_to(tmp_path/'config.json')
    with pytest.raises(ValueError):F.publish(tmp_path,'link',{},set())
    with pytest.raises(FileExistsError):F.publish(tmp_path,'config.json',{},set())
    assert (tmp_path/'config.json').read_bytes()==original
    assert not list(tmp_path.glob('.frozen-result-*'))


def test_late_label_and_safety_availability_refuse_false_window_separation(tmp_path):
    m=fixture(tmp_path);rows=[json.loads(l) for l in (tmp_path/'dataset.json').read_text().splitlines()]
    rows[0]['outcome']['recorded_t']=250.
    rewrite(tmp_path,m,'dataset','\n'.join(json.dumps(r) for r in rows))
    r=F.evaluate(tmp_path,'manifest.json');assert r['status']=='refused' and 'touches/crosses' in r['errors'][0]


def test_calibration_model_and_exact_config_link_refuse(tmp_path):
    m=fixture(tmp_path);c=json.loads((tmp_path/'calibration.json').read_text());c['config_sha256']='bad'
    rewrite(tmp_path,m,'calibration',json.dumps(c))
    assert F.evaluate(tmp_path,'manifest.json')['status']=='refused'


def test_consistent_legacy_scope_keeps_original_cohort_refusal_and_paper_loss(tmp_path):
    from daemon.launch_scanner import input_availability as I
    m=fixture(tmp_path);rows=[json.loads(l) for l in (tmp_path/'dataset.json').read_text().splitlines()]
    h=rows[1];h.update(outcome_kind='paper',outcome_id='PAPER',pnl_sol=-7.,realized_mult=0.,snapshot_recorded_t=310.)
    e=h['snapshot_input_evidence'];item=e['rows'][0];item['evidence'].pop('provider_normalization');item['evidence']['version']=2
    ee=item['evidence'];ee['binding']=I.digest('B',item['obs'],ee['observation_id'],ee['availability'])
    h['snapshot_input_evidence']=I.capture('B',310.,{k:v for k,v in e.items() if k!='capture'})
    m['partition']['eval']=['PAPER'];rewrite(tmp_path,m,'dataset','\n'.join(json.dumps(r) for r in rows));(tmp_path/'manifest.json').write_text(json.dumps(m))
    r=F.evaluate(tmp_path,'manifest.json');assert r['status']=='complete-unqualified'
    assert not r['whole_cohort']['cohort_qualified']
    assert r['dev']['n_trades']==0 and r['eval']['paper']['total_pnl_sol']==-7. and r['all_paper_accounting']['total_pnl_sol']==-7.
    assert not r['dataset']['economic_totals_complete']


def test_descriptor_detects_mutation_while_reading(tmp_path,monkeypatch):
    fixture(tmp_path);real=F.os.read;done=False
    def changed(fd,size):
        nonlocal done
        data=real(fd,size)
        if not done:
            done=True
            with open(tmp_path/'config.json','ab') as f:f.write(b' ')
        return data
    monkeypatch.setattr(F.os,'read',changed)
    with pytest.raises(ValueError):F.read_once(tmp_path,'config.json')


@pytest.mark.parametrize('role',['config','calibration','calibration-source-fixture','dataset'])
def test_nested_artifact_actual_cli_publishes_refusal_no_traceback(tmp_path,role):
    m=fixture(tmp_path);rewrite(tmp_path,m,role,'['*2000+'0'+']'*2000)
    r=subprocess.run([sys.executable,'-m','core.frozen_evaluation','--root',str(tmp_path),'--manifest','manifest.json','--output','refusal.json'],capture_output=True,text=True)
    assert r.returncode==2 and 'Traceback' not in r.stderr
    report=json.loads((tmp_path/'refusal.json').read_text())
    assert report['publication']=='complete' and report['result']['status']=='refused'


def test_json_structure_limit_respects_quoted_brackets():
    assert F.strict_json(json.dumps({'text':'['*2000+'"escaped"'}).encode())['text'].startswith('[')
    with pytest.raises(ValueError):F.strict_json(('['*65+'0'+']'*65).encode())


def test_bad_membership_retains_fully_parsed_paper_loss(tmp_path):
    m=fixture(tmp_path);rows=[json.loads(l) for l in (tmp_path/'dataset.json').read_text().splitlines()]
    rows[1].update(outcome_kind='paper',outcome_id='LOSS',pnl_sol=-7.,realized_mult=0.)
    rewrite(tmp_path,m,'dataset','\n'.join(json.dumps(r) for r in rows))
    r=F.evaluate(tmp_path,'manifest.json')
    assert r['status']=='refused' and r['all_paper_accounting']['trades']==1 and r['all_paper_accounting']['total_pnl_sol']==-7.
    assert not r['dataset']['economic_totals_complete'] and r['dataset']['records_parse_complete']


def test_nested_manifest_actual_cli_safe_refusal_publication(tmp_path):
    (tmp_path/'manifest.json').write_text('['*2000+'0'+']'*2000)
    r=subprocess.run([sys.executable,'-m','core.frozen_evaluation','--root',str(tmp_path),'--manifest','manifest.json','--output','refusal.json'],capture_output=True,text=True)
    assert r.returncode==2 and 'Traceback' not in r.stderr
    report=json.loads((tmp_path/'refusal.json').read_text())
    assert report['publication']=='complete' and report['result']['status']=='refused'
    assert 'structure limit' in report['result']['errors'][0]
