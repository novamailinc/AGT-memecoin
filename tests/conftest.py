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

def _collectable(_node):
    """pytest can only run a test whose params are all optional."""
    _args = _node.args
    _params = _args.posonlyargs + _args.args + _args.kwonlyargs
    _n_defaults = len(_args.defaults) + len(_args.kw_defaults or [])
    if _params and _params[0].arg == "self":
        _params = _params[1:]
    return len(_params) <= _n_defaults


collect_ignore = []
for _p in _glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "test_*.py")):
    try:
        _tree = _ast.parse(open(_p).read())
    except (SyntaxError, OSError):
        continue
    _has_pytest_tests = any(
        (isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
         and _n.name.startswith("test_") and _collectable(_n))
        or (isinstance(_n, _ast.ClassDef) and _n.name.startswith("Test"))
        for _n in _ast.walk(_tree))
    if not _has_pytest_tests:
        collect_ignore.append(_p)
