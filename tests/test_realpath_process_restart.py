"""External-process continuation of the production realpath fixture.

First interpreter acquires fixture transactions, opens and partially
settles an actual watcher position, writes a checkpoint, and exits.
A different interpreter loads persisted state, refuses duplicate intake,
closes the remaining position, and retrieves the real outcome memory.
Third interpreter checks the completed result survived another exit.
No module-reload claim, no live network/provider, no AGT reboot claim.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]

CONTINUE = r'''
import json,os,sys
from pathlib import Path
home=Path(os.environ['FOREX_AGENT_HOME'])
c=json.loads((home/'fixture-checkpoint.json').read_text())
from daemon.launch_scanner import watcher_lane as w, paper_account as pa, dossiers as d
w.LEDGER_PATH=c['ledger']
pos=w.open_positions()[c['mint']]
assert pos['pa_pos_id']==c['pos_id']
assert abs(pa.snapshot()['cash']-c['cash'])<1e-9
assert w.process_new_fires()['opened']==0
assert len(w.open_positions())==1
w.poll_exits(price_fn=lambda *a: c['entry_price']*0.1)
assert c['mint'] not in w.open_positions()
outs=d.outcome_records(mint=c['mint'])
assert len(outs)==1 and outs[0]['provenance']=='simulated'
assert outs[0]['qualified'] is False
mem=d.launch_memory(c['mint'])
assert mem and 'watcher' in mem['resolved_lanes']
(home/'fixture-completed.json').write_text(json.dumps({'cash':pa.snapshot()['cash']}))
print('FRESH PROCESS CLOSE AND MEMORY PASSED')
'''
VERIFY = r'''
import json,os
from pathlib import Path
home=Path(os.environ['FOREX_AGENT_HOME'])
c=json.loads((home/'fixture-checkpoint.json').read_text())
end=json.loads((home/'fixture-completed.json').read_text())
from daemon.launch_scanner import watcher_lane as w,paper_account as pa,dossiers as d
w.LEDGER_PATH=c['ledger']
assert not w.open_positions()
assert abs(pa.snapshot()['cash']-end['cash'])<1e-9
assert len(d.outcome_records(mint=c['mint']))==1
assert w.process_new_fires()['opened']==0
assert len(d.outcome_records(mint=c['mint']))==1
print('THIRD PROCESS RESTART AND DEDUP PASSED')
'''


class ExternalProcessAcceptance(unittest.TestCase):
    def test_acquisition_account_exit_memory_across_interpreters(self):
        with tempfile.TemporaryDirectory(prefix='external-realpath-') as home:
            env = dict(os.environ, FOREX_AGENT_HOME=home,
                       AGT_REALPATH_TEST_HOME=home,
                       AGT_REALPATH_CHECKPOINT='1',
                       PYTHONPATH=str(REPO), PAPER_ACCOUNT_ENABLED='1',
                       PAPER_ACCOUNT_START='10')
            commands = [[sys.executable, str(REPO/'tests/test_realpath_e2e.py')],
                        [sys.executable, '-c', CONTINUE],
                        [sys.executable, '-c', VERIFY]]
            for command in commands:
                result = subprocess.run(command, cwd=REPO, env=env,
                                        text=True, capture_output=True,
                                        timeout=45)
                self.assertEqual(result.returncode, 0,
                                 result.stdout + result.stderr)
            checkpoint = json.loads((Path(home)/'fixture-checkpoint.json').read_text())
            completed = json.loads((Path(home)/'fixture-completed.json').read_text())
            self.assertNotEqual(checkpoint['cash'], completed['cash'])


if __name__ == '__main__':
    unittest.main()
