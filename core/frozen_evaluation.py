"""Offline artifact reproducibility/refusal runner. Never a qualification gate."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import tempfile

MAX_BYTES=16*1024*1024
ENTRY={'min_safety_score','require_known_safety','max_entry_mcap_usd'}
EXIT={'tp_ladder','trailing_stop_pct','time_stop_h'}
COST={'fee_bps','slippage_bps','price_impact_bps','fail_prob_entry','fail_prob_exit','trade_notional_usd','token_account_cost_usd'}


def sha(data):return hashlib.sha256(data).hexdigest()


def strict_json(data):
    text=data.decode('utf-8')
    # Bound structure before the JSON decoder can recurse. Quoted delimiters
    # and escaped quotes are data, not nesting.
    depth=0;quoted=False;escaped=False;containers=0
    for char in text:
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '[{':
            depth+=1;containers+=1
            if depth>64 or containers>100000:raise ValueError('JSON structure limit exceeded')
        elif char in ']}':depth-=1
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('duplicate JSON key: '+k)
            out[k]=v
        return out
    def constant(v):raise ValueError('nonfinite JSON literal: '+v)
    try:result=json.loads(text,object_pairs_hook=pairs,parse_constant=constant)
    except RecursionError:raise ValueError('JSON recursion limit exceeded')
    pending=[result];nodes=0
    while pending:
        value=pending.pop();nodes+=1
        if nodes>200000:raise ValueError('JSON node limit exceeded')
        if type(value) is float and not math.isfinite(value):raise ValueError('nonfinite JSON number')
        if isinstance(value,dict):pending.extend(value.values())
        elif isinstance(value,list):pending.extend(value)
    return result



def local_path(root,relative):
    if not isinstance(relative,str) or not relative or Path(relative).is_absolute():raise ValueError('relative local artifact path required')
    parts=Path(relative).parts
    if any(p in ('.','..') for p in parts):raise ValueError('artifact traversal refused')
    p=root
    for part in parts:
        p=p/part
        if p.is_symlink():raise ValueError('artifact symlink refused')
    if root not in p.parents:raise ValueError('artifact outside root')
    return p


def read_once(root,relative):
    """Descriptor traversal rejects symlink races in every path component."""
    local_path(root,relative)
    parts=Path(relative).parts;fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        file_fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            before=os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size>MAX_BYTES:raise ValueError('artifact regular-file/size requirement failed')
            data=b''
            while True:
                block=os.read(file_fd,min(65536,MAX_BYTES+1-len(data)))
                if not block:break
                data+=block
                if len(data)>MAX_BYTES:raise ValueError('artifact grew beyond limit')
            after=os.fstat(file_fd)
            if ((before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)
                !=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns) or len(data)!=before.st_size):
                raise ValueError('artifact changed while reading')
            return data
        finally:os.close(file_fd)
    finally:os.close(fd)


def exact(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):raise ValueError(label+' exact schema required')


def numeric(value):
    if type(value) not in (int,float):return False
    try:return math.isfinite(value) and value>=0
    except (ValueError,OverflowError):return False


def effective_config(config):
    from daemon.launch_scanner.backtest import BacktestEngine,REPLAY_MODEL_V2
    exact(config,{'schema_version','replay_model','require_provider_inputs','seed','entry_rules','exit_rules','costs'},'config')
    if type(config['schema_version']) is not int or config['schema_version']!=1:raise ValueError('unknown config version')
    if config['replay_model']!=REPLAY_MODEL_V2 or config['require_provider_inputs'] is not True:raise ValueError('frozen runner requires explicit v2/provider evidence')
    if type(config['seed']) is not int:raise ValueError('integer seed required')
    e=config['entry_rules'];x=config['exit_rules'];exact(e,ENTRY,'entry');exact(x,EXIT,'exit');exact(config['costs'],COST,'costs')
    if type(e['require_known_safety']) is not bool or not e['require_known_safety']:raise ValueError('known safety required')
    if not numeric(e['min_safety_score']) or e['min_safety_score']>100 or not numeric(e['max_entry_mcap_usd']):raise ValueError('invalid entry assumption')
    if not numeric(x['trailing_stop_pct']) or x['trailing_stop_pct']>100 or not numeric(x['time_stop_h']):raise ValueError('invalid exit assumption')
    if not isinstance(x['tp_ladder'],list):raise ValueError('explicit TP list required')
    previous=1.
    total=0.
    for rung in x['tp_ladder']:
        if not isinstance(rung,list) or len(rung)!=2 or not all(numeric(v) for v in rung):raise ValueError('invalid TP rung')
        mult,fraction=rung
        if mult<=previous or not 0<fraction<=1:raise ValueError('TP ladder ordered multipliers/fractions required')
        total+=fraction;previous=mult
    if total>1:raise ValueError('TP fractions exceed original position')
    return BacktestEngine(entry_rules=e,exit_rules=x,costs=config['costs'],seed=config['seed'],
                          replay_model=config['replay_model'],require_provider_inputs=True)


def window(history):
    snaps=history.get('snapshots')
    if not isinstance(snaps,list) or not snaps:raise ValueError('empty or unknown retained window')
    times=[]
    for snap in snaps:
        if not isinstance(snap,dict) or not numeric(snap.get('t')):raise ValueError('invalid retained time')
        times.append(snap['t'])
    if any(b<a for a,b in zip(times,times[1:])):raise ValueError('reversed retained window')
    outcome=history.get('outcome')
    if not isinstance(outcome,dict) or not numeric(outcome.get('recorded_t')):raise ValueError('outcome availability unknown')
    times.append(outcome['recorded_t'])
    for key in ('end_t','peak_t'):
        if not numeric(outcome.get(key)):raise ValueError('outcome interval unknown')
        times.append(outcome[key])
    if isinstance(history.get('safety'),dict) and history['safety'].get('score') is not None:
        if not numeric(history.get('safety_eval_t')):raise ValueError('safety availability unknown')
        times.append(history['safety_eval_t'])
    from daemon.launch_scanner.input_availability import capture_time
    capture=capture_time(history.get('mint'),history.get('snapshot_input_evidence'))
    if capture is None:raise ValueError('capture availability unknown')
    times.append(capture)
    return min(times),max(times)


def evaluate(root,manifest_path):
    """Deterministic report core, with retained bytes and explicit refusal."""
    root=Path(root)
    if not root.is_absolute() or root.is_symlink() or root.resolve()!=root:raise ValueError('canonical absolute local root required')
    report={'schema_version':1,'status':'refused','stage_a_qualified':False,'measured_qualification':'unavailable',
            'strategy_execution_binding':'unverified-artifact-source-not-executed','tuning_isolation':'unverified',
            'scope':'local artifact consistency and deterministic replay, not authenticity/freshness/qualification',
            'artifacts':{},'errors':[],'dataset':{'line_count':0,'records':[],'economic_totals_complete':False}}
    try:
        raw=read_once(root,manifest_path);report['manifest_sha256']=sha(raw);report['manifest_retained_base64']=base64.b64encode(raw).decode();m=strict_json(raw)
        exact(m,{'schema_version','artifacts','partition','calibration_model_id'},'manifest')
        if type(m['schema_version']) is not int or m['schema_version']!=1:raise ValueError('unknown manifest version')
        if not isinstance(m['artifacts'],list):raise ValueError('artifact inventory required')
        blobs={};paths=set()
        for a in m['artifacts']:
            exact(a,{'role','path','sha256','byte_count'},'artifact')
            role=a['role'];path=a['path']
            if not isinstance(role,str) or not role or role in blobs or path in paths:raise ValueError('duplicate artifact role/path')
            if role not in ('dataset','config','calibration','strategy-source') and not role.startswith('calibration-source-'):raise ValueError('unknown artifact role')
            if path==manifest_path:raise ValueError('manifest cannot reference itself')
            data=read_once(root,path)
            report['artifacts'][role]={'path':path,'sha256':sha(data),'byte_count':len(data),'retained_base64':base64.b64encode(data).decode()}
            if type(a['byte_count']) is not int or a['byte_count']!=len(data) or a['sha256']!=sha(data):raise ValueError('artifact size/digest mismatch: '+role)
            blobs[role]=data;paths.add(path)
        if not {'dataset','config','calibration','strategy-source'}<=set(blobs):raise ValueError('required artifact missing')
        config=strict_json(blobs['config']);cal=strict_json(blobs['calibration']);engine=effective_config(config)
        exact(cal,{'schema_version','model_id','evidence_kind','source_roles','limitations','config_sha256'},'calibration')
        if type(cal['schema_version']) is not int or cal['schema_version']!=1 or not isinstance(cal['model_id'],str) or not cal['model_id'] or cal['model_id']!=m['calibration_model_id'] or cal['config_sha256']!=sha(blobs['config']):raise ValueError('calibration model linkage mismatch')
        if cal['evidence_kind'] not in ('scenario-only','claimed-measured-unverified'):raise ValueError('unsupported calibration evidence kind')
        if (not isinstance(cal['source_roles'],list) or not cal['source_roles'] or len(set(cal['source_roles']))!=len(cal['source_roles'])
            or any(not isinstance(r,str) or not r.startswith('calibration-source-') or r not in blobs for r in cal['source_roles'])
            or not isinstance(cal['limitations'],str) or not cal['limitations']):raise ValueError('calibration source/limitations unavailable')
        # All calibration references here are strict JSON evidence, never code.
        for role in cal['source_roles']:strict_json(blobs[role])
        report['calibration']={'model_id':cal['model_id'],'evidence_kind':cal['evidence_kind'],'measurement_authenticity':'unverified'}
        histories=[];identities=set();bad=False
        for line_no,line in enumerate(blobs['dataset'].splitlines(),1):
            report['dataset']['line_count']=line_no
            if not line.strip():
                report['dataset']['records'].append({'line':line_no,'disposition':'blank'});continue
            try:
                h=strict_json(line)
                if not isinstance(h,dict):raise ValueError('history must be object')
                identity=h.get('history_id') if h.get('outcome_kind')!='paper' else h.get('outcome_id')
                if not isinstance(identity,str) or not identity:raise ValueError('stable explicit history/outcome identity required')
                if identity in identities:raise ValueError('duplicate stable identity')
                identities.add(identity);histories.append(h)
                report['dataset']['records'].append({'line':line_no,'identity':identity,'sha256':sha(line),'disposition':'parsed'})
            except (ValueError,TypeError,OverflowError,RecursionError) as exc:
                bad=True;report['dataset']['records'].append({'line':line_no,'disposition':'invalid','error':str(exc)})
        report['dataset']['records_parse_complete']=not bad
        report['dataset']['economic_totals_complete']=False
        report['dataset']['economic_scope']='reported paper numbers only, native receipt/basis/fees/FX completeness not independently verified'
        if bad:
            report['dataset']['known_records_accounting']=engine.run([h for h in histories if h.get('outcome_kind')=='paper'])['paper']
            raise ValueError('dataset parsing/identity failure; no selected-subset performance')
        report['all_paper_accounting']=engine.run([h for h in histories if h.get('outcome_kind')=='paper'])['paper']
        part=m['partition'];exact(part,{'cutoff','embargo_s','dev','eval'},'partition')
        if not numeric(part['cutoff']) or not numeric(part['embargo_s']) or not math.isfinite(part['cutoff']+part['embargo_s']):raise ValueError('typed usable cutoff/embargo required')
        if (not isinstance(part['dev'],list) or not isinstance(part['eval'],list) or not part['dev'] or not part['eval']
            or any(not isinstance(v,str) for v in part['dev']+part['eval'])
            or len(set(part['dev']+part['eval']))!=len(part['dev']+part['eval'])
            or set(part['dev']+part['eval'])!=identities):raise ValueError('exact nonempty disjoint complete membership required')
        report['whole_cohort']=engine._provider_qualification(histories,True)
        report['all_paper_accounting']=engine.run([h for h in histories if h.get('outcome_kind')=='paper'])['paper']
        by_id={h.get('history_id') if h.get('outcome_kind')!='paper' else h['outcome_id']:h for h in histories}
        for group in ('dev','eval'):
            for identity in part[group]:
                lo,hi=window(by_id[identity])
                if group=='dev' and hi>=part['cutoff'] or group=='eval' and lo<=part['cutoff']+part['embargo_s']:raise ValueError('retained availability window touches/crosses cutoff or embargo')
        report['partition']={'membership':part,'embargo_policy':'caller-assumption-not-reviewed','availability_scope':'stored snapshots/safety/outcome/capture only; upstream acquisition completeness unverified'}
        report['execution_source_hashes']={name:sha(Path(__file__).resolve().parents[1].joinpath(name).read_bytes()) for name in
            ('core/frozen_evaluation.py','daemon/launch_scanner/backtest.py','daemon/launch_scanner/input_availability.py','daemon/launch_scanner/provider_inputs.py','daemon/launch_scanner/fingerprints.py')}
        report['dev']=engine._run([by_id[i] for i in part['dev']],report['whole_cohort'])
        # Separate seeded engine prevents development draws consuming eval RNG.
        config_eval=dict(config,seed=config['seed']+1);eval_engine=effective_config(config_eval)
        report['eval']=eval_engine._run([by_id[i] for i in part['eval']],report['whole_cohort'])
        report['status']='complete-unqualified'
    except (ValueError,TypeError,KeyError,OSError,OverflowError,RecursionError) as exc:report['errors'].append(str(exc))
    return finite_report(report)


def finite_report(value):
    if type(value) is float and not math.isfinite(value):return {'availability':'nonfinite-metric','sign':'positive' if value>0 else 'negative' if value<0 else 'undefined'}
    if isinstance(value,dict):return {k:finite_report(v) for k,v in value.items()}
    if isinstance(value,list):return [finite_report(v) for v in value]
    return value


def publish(root,output,report,input_paths):
    """Atomic, no-clobber single-file publication. Never follows output link."""
    root=Path(root);p=local_path(root,output)
    if output in input_paths:raise ValueError('output cannot overwrite input')
    # Existing parent required. Open its descriptor without following symlinks.
    relative=Path(output);fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    tmp=None
    try:
        for part in relative.parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
        # tempfile beneath descriptor-resolved parent; link uses same descriptor.
        name='.frozen-result-'+os.urandom(12).hex();tmp=name
        out=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        with os.fdopen(out,'w') as f:
            json.dump({'publication':'complete','result':report},f,sort_keys=True,allow_nan=False);f.write('\n');f.flush();os.fsync(f.fileno())
        os.link(name,relative.name,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False);os.fsync(fd)
    finally:
        if tmp is not None:
            try:os.unlink(tmp,dir_fd=fd)
            except FileNotFoundError:pass
        os.close(fd)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',required=True);parser.add_argument('--manifest',required=True);parser.add_argument('--output',required=True);args=parser.parse_args(argv)
    try:
        report=evaluate(args.root,args.manifest)
        inputs={args.manifest}|{a['path'] for a in report['artifacts'].values()}
        publish(args.root,args.output,report,inputs)
        print(json.dumps({'status':report['status'],'stage_a_qualified':False,'errors':report['errors']},sort_keys=True))
        return 0 if report['status']=='complete-unqualified' else 2
    except (ValueError,OSError) as exc:
        print(json.dumps({'status':'unpublished-refusal','stage_a_qualified':False,'error':str(exc)}));return 2


if __name__=='__main__':raise SystemExit(main())
