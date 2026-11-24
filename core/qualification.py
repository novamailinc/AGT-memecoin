"""Qualification evidence bookkeeping, original request sections 12/13/22.

Not an execution gate or authorization store. All transitions are explicit.
Mainnet states are named but cannot be entered by this offline registry.
Evidence is caller-recorded, never independently verified by a boolean.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

STATES = ('DEVELOPMENT', 'HISTORICAL_VALIDATION', 'DEMO_QUALIFICATION',
          'DEMO_QUALIFIED', 'MAINNET_READY_PENDING_AUTHORIZATION',
          'MAINNET_CANARY', 'MAINNET_RESTRICTED', 'MAINNET_EXPANDED',
          'PAUSED', 'EMERGENCY_STOP')
DEMO_CRITERIA = ('trade_sample', 'observation_period', 'net_expectancy',
                 'profit_factor', 'drawdown', 'out_of_sample',
                 'data_integrity', 'provider_routing', 'entry_exit',
                 'execution_realism', 'restart_recovery', 'risk_controls',
                 'frozen_strategy')
HISTORY_CRITERIA = ('timestamp_correct', 'realistic_costs',
                    'chronological_holdout', 'multiple_regimes',
                    'reproducible_advantage')
BINDING = ('strategy_version', 'config_hash', 'data_period')
MAINNET = set(STATES[4:8])


class Registry:
    def __init__(self, home=None):
        self.path = Path(home or os.environ.get('FOREX_AGENT_HOME',
                         str(Path.home()/'.forex-agent'))) / 'qualification.json'

    @contextlib.contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path)+'.lock', 'a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self):
        try:
            d = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {'availability': 'missing', 'qualified': False}
        except (OSError, ValueError):
            return {'availability': 'corrupt_or_unreadable', 'qualified': False}
        if (not isinstance(d, dict) or type(d.get('v')) is not int or d.get('v') != 1
                or d.get('state') not in STATES
                or not isinstance(d.get('evidence'), dict)
                or not isinstance(d.get('audit'), list)
                or not isinstance(d.get('binding'), dict)
                or any(not isinstance(d['binding'].get(k), str)
                       or not d['binding'][k].strip() for k in BINDING)):
            return {'availability': 'corrupt_or_unreadable', 'qualified': False}
        return dict(d, availability='ok')

    def _write(self, d):
        d = {k: v for k, v in d.items() if k != 'availability'}
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.qualification-')
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(d, f, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def initialize(self, binding):
        if (set(binding) != set(BINDING) or any(
                not isinstance(v, str) or not v.strip() for v in binding.values())):
            raise ValueError('exact strategy/config/data-period binding required')
        with self._lock():
            if self._read()['availability'] != 'missing':
                raise ValueError('existing state cannot be reset or overwritten')
            self._write({'v': 1, 'state': 'DEVELOPMENT', 'binding': binding,
                         'evidence': {}, 'audit': [{'action': 'initialize',
                                                   'ts': time.time()}]})
        return self.status()

    @staticmethod
    def _missing(d, criteria):
        missing = []
        for key in criteria:
            e = d['evidence'].get(key)
            if (not isinstance(e, dict) or e.get('passed') is not True
                    or e.get('binding') != d['binding']
                    or not isinstance(e.get('source'), str) or not e['source'].strip()
                    or not isinstance(e.get('justification'), str)
                    or not e['justification'].strip()):
                missing.append(key)
        return missing

    def status(self):
        # One locked snapshot, so readiness and state describe the same version.
        with self._lock():
            d = self._read()
        if d['availability'] != 'ok':
            return d
        d['missing_historical'] = self._missing(d, HISTORY_CRITERIA)
        d['missing_demo'] = self._missing(d, DEMO_CRITERIA)
        d['qualified'] = (d['state'] == 'DEMO_QUALIFIED'
                          and not d['missing_historical'] and not d['missing_demo'])
        d['execution_authorized'] = False
        d['qualification_scope'] = 'recorded_evidence_bookkeeping_only'
        d['runtime_gate_connected'] = False
        d['evidence_provenance'] = 'caller_recorded_not_independently_verified'
        return d

    def record(self, criterion, passed, source, justification, binding):
        if criterion not in HISTORY_CRITERIA + DEMO_CRITERIA or type(passed) is not bool:
            raise ValueError('known criterion and exact boolean required')
        if not all(isinstance(v, str) and v.strip() for v in (source, justification)):
            raise ValueError('source and justification required, no invented defaults')
        with self._lock():
            d = self._read()
            if d['availability'] != 'ok' or binding != d['binding']:
                raise ValueError('unavailable registry or mismatched evidence identity')
            d['evidence'][criterion] = dict(passed=passed, source=source,
                                            justification=justification,
                                            binding=dict(binding), ts=time.time())
            d['audit'].append(dict(action='record', criterion=criterion,
                                   passed=passed, source=source, ts=time.time()))
            # Evidence revocation cannot leave a qualified status displayed.
            if not passed and d['state'] == 'DEMO_QUALIFIED':
                d['state'] = 'PAUSED'
                d['audit'].append(dict(action='qualification_revoked', ts=time.time()))
            self._write(d)
        return self.status()

    def transition(self, target, reason):
        if target not in STATES or not isinstance(reason, str) or not reason.strip():
            raise ValueError('named target and reason required')
        if target in MAINNET:
            raise ValueError('mainnet transitions unavailable: separate verified authority required')
        with self._lock():
            d = self._read()
            if d['availability'] != 'ok':
                raise ValueError('registry unavailable')
            current = d['state']
            allowed = {'DEVELOPMENT': {'HISTORICAL_VALIDATION'},
                       'HISTORICAL_VALIDATION': {'DEMO_QUALIFICATION'},
                       'DEMO_QUALIFICATION': {'DEMO_QUALIFIED'},
                       'PAUSED': {'HISTORICAL_VALIDATION'},
                       'EMERGENCY_STOP': {'HISTORICAL_VALIDATION'}}
            if target not in ('PAUSED', 'EMERGENCY_STOP') and target not in allowed.get(current, set()):
                raise ValueError('transition not allowed')
            required = HISTORY_CRITERIA if target in ('DEMO_QUALIFICATION', 'DEMO_QUALIFIED') else ()
            if target == 'DEMO_QUALIFIED':
                required += DEMO_CRITERIA
            missing = self._missing(d, required)
            if missing:
                raise ValueError('unmet evidence: ' + ','.join(missing))
            d['state'] = target
            d['audit'].append(dict(action='transition', previous=current,
                                   target=target, reason=reason, ts=time.time()))
            self._write(d)
        return self.status()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--home')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status')
    init = sub.add_parser('initialize')
    for key in BINDING:
        init.add_argument('--'+key.replace('_', '-'), required=True)
    trans = sub.add_parser('transition')
    trans.add_argument('target', choices=STATES)
    trans.add_argument('--reason', required=True)
    rec = sub.add_parser('record')
    rec.add_argument('evidence_file', help='JSON with criterion, passed, source, justification, binding')
    args = vars(p.parse_args())
    registry = Registry(args.pop('home'))
    cmd = args.pop('command')
    try:
        if cmd == 'initialize':
            out = registry.initialize(args)
        elif cmd == 'transition':
            out = registry.transition(**args)
        elif cmd == 'record':
            out = registry.record(**json.loads(Path(args['evidence_file']).read_text()))
        else:
            out = registry.status()
        print(json.dumps(out, indent=2))
    except (ValueError, OSError, TypeError) as exc:
        p.exit(2, str(exc)+'\n')


if __name__ == '__main__':
    main()
