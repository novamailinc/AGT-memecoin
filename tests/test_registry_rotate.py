#!/usr/bin/env python3
"""Registry rotation rule tests (pure function, no disk)."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import importlib.util
import sys

spec = importlib.util.spec_from_file_location(
    "rot", _REPO_ROOT + "/scripts/smart-registry-rotate")
# import without running main()
_src = open(_REPO_ROOT + "/scripts/smart-registry-rotate").read()
_ns = {"__name__": "rot_test",
        "__file__": _REPO_ROOT + "/scripts/smart-registry-rotate"}
exec(compile(_src.split('if __name__ == "__main__":')[0], "rot", "exec"), _ns)
rotation_action = _ns["rotation_action"]

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# mute: enough data + clearly losing
check("proven loser muted", rotation_action(100, -0.44, 100, False) == "mute")
check("boundary n=10 muted", rotation_action(10, -0.25, 10, False) == "mute")
check("boundary avg=-0.20 muted", rotation_action(10, -0.20, 10, False) == "mute")
# keep: thin data even if bad avg
check("thin data kept", rotation_action(4, -0.50, 4, False) == "keep")
# keep: losing but above bar
check("mild loser kept", rotation_action(50, -0.10, 50, False) == "keep")
# keep: winner
check("winner kept", rotation_action(30, 0.15, 30, False) == "keep")
# stale: no buys at all
check("dormant stale", rotation_action(0, 0.0, 0, False) == "stale")
# already muted: left alone
check("muted left alone", rotation_action(100, -0.9, 100, True) == "keep")
# never raises
check("garbage -> keep", rotation_action(None, "x", None, False) == "keep")

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
