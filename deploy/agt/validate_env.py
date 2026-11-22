"""Validate the Python environment on the AGT prefix.

Phase 13 (AGT packaging skeleton). The AGT bootstrap prefix ships
Python 3.12 with the stdlib and no apt / Debian packages: pure-Python
+ vendored wheels only. This script reports, per module, whether it
imports here — it never installs anything and never fails the deploy
on its own; the report tells the finalize pass what to vendor.

Classification per module:
  * stdlib        — must import; a failure is a hard environment bug.
  * pure-python   — vendored wheel expected; missing = vendor it.
  * compiled      — needs a native extension (solders, numpy, pandas,
                    scikit-learn, xgboost). The AGT prefix cannot
                    build these; they work only as prebuilt
                    arm64 wheels vendored into the deployment, which
                    is UNPROVEN. A missing compiled module is reported
                    honestly with its consequence, never papered over.

Consequences recorded here (verified against the repo):
  * solders missing  -> devnet-demo-trader cannot start (signing
    needs it). Paper lanes do not need it.
  * numpy/pandas/sklearn/xgboost missing -> intelligence ML trainer
    only; the paper lanes and the acquisition router do not need
    them.
  * pyyaml missing   -> optional config/defaults.yaml support only.
  * pytest missing   -> the daily test-suite loop cannot run on AGT
    until pytest is vendored.

Output: JSON report on stdout + a human summary. Exit 0 when the
report was produced (even with missing modules); exit 2 only when
the Python itself is too old (< 3.12) to run the bot at all.
"""

from __future__ import annotations

import importlib.util
import json
import sys

MIN_PYTHON = (3, 12)

MODULES = (
    # (name, kind, consequence when missing)
    ("json", "stdlib", "core: cannot run anything without it"),
    ("sqlite3", "stdlib", "core: local.db event journal + state"),
    ("http.server", "stdlib", "core: status dashboard"),
    ("zoneinfo", "stdlib", "scheduler Lagos wall time (fallback is a "
                           "fixed UTC+1 offset, so this is advisory)"),
    ("yaml", "pure-python", "optional config/defaults.yaml support"),
    ("solders", "compiled",
     "devnet-demo-trader cannot start (Solana signing); paper lanes "
     "unaffected"),
    ("numpy", "compiled", "intelligence ML trainer only"),
    ("pandas", "compiled", "intelligence ML trainer only"),
    ("sklearn", "compiled", "intelligence ML trainer only"),
    ("xgboost", "compiled", "intelligence ML trainer only"),
    ("pytest", "pure-python",
     "daily test-suite loop cannot run on AGT until vendored"),
)


def check_module(name):
    spec = importlib.util.find_spec(name)
    if spec is None:
        return {"present": False, "origin": None}
    origin = spec.origin or ""
    return {"present": True, "origin": origin}


def validate():
    report = {
        "python": "%d.%d.%d" % sys.version_info[:3],
        "python_ok": sys.version_info[:2] >= MIN_PYTHON,
        "executable": sys.executable,
        "modules": {},
    }
    for name, kind, consequence in MODULES:
        entry = check_module(name)
        entry["kind"] = kind
        entry["consequence_if_missing"] = consequence
        report["modules"][name] = entry
    missing_stdlib = [n for n, m in report["modules"].items()
                      if m["kind"] == "stdlib" and not m["present"]]
    missing_compiled = [n for n, m in report["modules"].items()
                        if m["kind"] == "compiled" and not m["present"]]
    report["missing_stdlib"] = missing_stdlib
    report["missing_compiled"] = missing_compiled
    report["summary"] = (
        "python %s (%s); stdlib missing: %s; compiled missing: %s"
        % (report["python"],
           "ok" if report["python_ok"] else "TOO OLD",
           ", ".join(missing_stdlib) or "none",
           ", ".join(missing_compiled) or "none"))
    return report


def main(argv=None):
    report = validate()
    print(json.dumps(report, indent=2))
    print(report["summary"])
    if not report["python_ok"]:
        print("FATAL: Python < 3.12 cannot run this deployment.")
        return 2
    if report["missing_stdlib"]:
        print("FATAL: missing stdlib modules: %s"
              % ", ".join(report["missing_stdlib"]))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
