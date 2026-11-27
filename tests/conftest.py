"""pytest path bootstrap: runtime sets PYTHONPATH to forex-agent/;
tests do the same here so they never rely on the working directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Script-style tests are excluded from pytest collection.
#
# About half of tests/ are plain scripts: module-level checks ending in
# sys.exit(...). Importing them at collection time runs the whole script
# and the sys.exit kills the pytest session (INTERNALERROR). They are run
# for real by scripts/run_script_tests.py (see build.yml), each as a
# subprocess with its own pass/fail. Detection matches that runner: a
# file with no test_* functions and no Test* classes is script-style.
# ---------------------------------------------------------------------------
import ast as _ast
import glob as _glob
import os

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from test_discovery import is_script_test
# Recursive: script-style tests also live in subdirectories (e.g.
# tests/realstack/*-SOURCE-ONLY.py). A top-level-only glob let pytest
# import those at collection time; their module-level block_network()
# then installed the network block for the whole session and every
# loopback-HTTP test (local API / SSE) failed with "network blocked in
# tests". Same detection as the runner; subdirectories included.
collect_ignore=[p for p in _glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), '**', 'test_*.py'), recursive=True) if is_script_test(p)]
