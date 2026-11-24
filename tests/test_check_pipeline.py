#!/usr/bin/env python3
"""Tests for daemon/launch_scanner/check_pipeline.py.

Covers the reference pipeline semantics (pumpfun-bonkfun-bot
src/trading/checks/pipeline.py): one class per gate, first skip
wins, split by reads_chain, per-check budget, required vs
best-effort on unreadable data, exception text never surfaces.
No network, no disk. Pure in-process checks.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import os
import sys
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import check_pipeline as CP

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


class _Pass(CP.PreBuyCheck):
    name = "passer"
    reads_chain = False

    def check(self, cand, ctx):
        return None


class _Skip(CP.PreBuyCheck):
    name = "skipper"
    reads_chain = False

    def check(self, cand, ctx):
        return "nope"


class _ChainSkip(CP.PreBuyCheck):
    name = "chain_skipper"
    reads_chain = True

    def check(self, cand, ctx):
        return "chain_nope"


class _Slow(CP.PreBuyCheck):
    name = "slow"
    reads_chain = False
    unavailable_reason = "unknown_slow"

    def check(self, cand, ctx):
        time.sleep(0.3)
        return None


class _Unavailable(CP.PreBuyCheck):
    name = "unavail"
    reads_chain = False
    unavailable_reason = "unknown_unavail"

    def check(self, cand, ctx):
        raise CP.CheckUnavailable("the data is gone")


class _Boom(CP.PreBuyCheck):
    name = "boomer"
    reads_chain = False
    unavailable_reason = "unknown_boomer"

    def check(self, cand, ctx):
        raise RuntimeError("secret exploded detail must not leak")


class _BadReturn(CP.PreBuyCheck):
    name = "badret"
    reads_chain = False
    unavailable_reason = "unknown_badret"

    def check(self, cand, ctx):
        return 42


CAND = {"mint": "x"}


def _pipe(*configured):
    return CP.CheckPipeline(list(configured), ctx=None)


# 1. First skip wins, in order.
p = _pipe(CP.ConfiguredCheck(_Pass(), named=False),
          CP.ConfiguredCheck(_Skip(), named=False),
          CP.ConfiguredCheck(_Skip(), named=False))
check("first skip wins",
      p.reason(CAND, reads_chain=False) == "nope")
check("run_phases: free skip wins over chain",
      _pipe(CP.ConfiguredCheck(_Skip(), named=False),
            CP.ConfiguredCheck(_ChainSkip(), named=False)
            ).run_phases(CAND) == "nope")

# 2. Split by reads_chain: each phase runs only its own kind.
p = _pipe(CP.ConfiguredCheck(_Skip(), named=False),
          CP.ConfiguredCheck(_ChainSkip(), named=False))
check("free phase ignores chain checks",
      p.reason(CAND, reads_chain=False) == "nope")
check("chain phase ignores free checks",
      p.reason(CAND, reads_chain=True) == "chain_nope")
check("run_phases: chain skip after free pass",
      _pipe(CP.ConfiguredCheck(_Pass(), named=False),
            CP.ConfiguredCheck(_ChainSkip(), named=False)
            ).run_phases(CAND) == "chain_nope")
check("has() reports each kind",
      p.has(reads_chain=False) and p.has(reads_chain=True))
check("has() false when absent",
      not _pipe(CP.ConfiguredCheck(_Pass())
                ).has(reads_chain=True))

# 3. All pass -> None.
check("all pass -> None",
      _pipe(CP.ConfiguredCheck(_Pass(), named=False),
            CP.ConfiguredCheck(_Pass(), named=False)
            ).run_phases(CAND) is None)

# 4. Budget: a slow check past its budget fails; required
# refuses with its stable unavailable reason.
p = _pipe(CP.ConfiguredCheck(_Slow(), budget=0.1, named=False))
t0 = time.monotonic()
r = p.reason(CAND, reads_chain=False)
dt = time.monotonic() - t0
check("slow check past budget -> unavailable reason",
      r == "unknown_slow")
check("slow check does not stall past its budget",
      dt < 0.25)

# 5. Best-effort slow check proceeds.
p = _pipe(CP.ConfiguredCheck(_Slow(), budget=0.1, required=False,
                             named=False))
check("best-effort slow check proceeds",
      p.reason(CAND, reads_chain=False) is None)

# 6. No budget (None) lets a slow check finish.
p = _pipe(CP.ConfiguredCheck(_Slow(), budget=None, named=False))
check("budget None: slow check finishes and passes",
      p.reason(CAND, reads_chain=False) is None)

# 7. CheckUnavailable: required refuses, best-effort proceeds.
p = _pipe(CP.ConfiguredCheck(_Unavailable(), named=False))
check("unavailable required -> unavailable reason",
      p.reason(CAND, reads_chain=False) == "unknown_unavail")
p = _pipe(CP.ConfiguredCheck(_Unavailable(), required=False,
                             named=False))
check("unavailable best-effort -> proceeds",
      p.reason(CAND, reads_chain=False) is None)

# 8. Exception: failure kind only — the exception text never
# surfaces in the reason.
p = _pipe(CP.ConfiguredCheck(_Boom(), named=False))
r = p.reason(CAND, reads_chain=False)
check("exception -> unavailable reason, text never leaks",
      r == "unknown_boomer" and "secret" not in (r or ""))

# 9. Bad return (not None/str) counts as cannot-decide.
p = _pipe(CP.ConfiguredCheck(_BadReturn(), named=False))
check("bad return -> unavailable reason",
      p.reason(CAND, reads_chain=False) == "unknown_badret")

# 10. named=True prefixes "<name>: <reason>".
p = _pipe(CP.ConfiguredCheck(_Skip(), named=True))
check("named prefixes the check name",
      p.reason(CAND, reads_chain=False) == "skipper: nope")

# 11. A check that returns a string after its budget still
# fails the budget (late answer is not an answer).
class _LateSkip(CP.PreBuyCheck):
    name = "late"
    reads_chain = False
    unavailable_reason = "unknown_late"

    def check(self, cand, ctx):
        time.sleep(0.3)
        return "too_late"


p = _pipe(CP.ConfiguredCheck(_LateSkip(), budget=0.1, named=False))
check("late skip -> budget failure, not the skip reason",
      p.reason(CAND, reads_chain=False) == "unknown_late")

# 12. required_by_default is honoured by ConfiguredCheck
# construction only via explicit required; the pipeline never
# raises on an empty check list.
check("empty pipeline passes",
      _pipe().run_phases(CAND) is None)

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECK PIPELINE TESTS PASSED")
