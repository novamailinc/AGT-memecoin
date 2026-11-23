#!/usr/bin/env python3
"""Generate daemon/launch_scanner/data/pump_fee_schedule_captured.json.

Rebuilds the committed fee-schedule artifact from the captured on-chain
fee accounts in tests/fixtures/fee_program/ (see its capture_meta.json
for provenance). The artifact is what chain_costs.py loads at runtime;
it is derived data, never hand-edited. Re-run after any re-capture:

    python3 scripts/gen_pump_fee_schedule.py            # rewrite
    python3 scripts/gen_pump_fee_schedule.py --check    # CI: verify

The capture itself requires an approved on-chain read route; this script
only reads the committed fixture files.
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from daemon.launch_scanner import pump_fee_schedule as pfs  # noqa: E402

FIXTURE_DIR = os.path.join(REPO, "tests", "fixtures", "fee_program")
ARTIFACT = os.path.join(REPO, "daemon", "launch_scanner", "data",
                        "pump_fee_schedule_captured.json")


def main():
    check = "--check" in sys.argv[1:]
    bundle = pfs.load_fixture_set(FIXTURE_DIR)
    schedule = pfs.build_schedule(bundle)
    rendered = json.dumps(schedule, indent=1, sort_keys=True) + "\n"
    if check:
        try:
            with open(ARTIFACT, "r", encoding="utf-8") as fh:
                current = fh.read()
        except OSError:
            print("FAIL: %s missing - run gen_pump_fee_schedule.py"
                  % os.path.relpath(ARTIFACT, REPO))
            return 1
        if current != rendered:
            print("FAIL: %s is stale - run gen_pump_fee_schedule.py"
                  % os.path.relpath(ARTIFACT, REPO))
            return 1
        print("ok: %s matches the captured fixtures"
              % os.path.relpath(ARTIFACT, REPO))
        return 0
    os.makedirs(os.path.dirname(ARTIFACT), exist_ok=True)
    with open(ARTIFACT, "w", encoding="utf-8") as fh:
        fh.write(rendered)
    print("wrote %s (captured %s)"
          % (os.path.relpath(ARTIFACT, REPO),
             schedule["source"]["accounts"]["fee_config_pump"]
             ["captured_utc"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
