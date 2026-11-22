#!/usr/bin/env python3
"""Tests for daemon/launch_scanner/tx_guards.py.

Covers the two phantom-entry guards: meta.err gating (a
CreateEvent can survive in a reverted tx's logs) and whole-line
log matching (substring also matches CreateTokenAccount /
CreatePool). No network, no disk. Pure in-process checks.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import os
import sys

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import tx_guards as TG

FAILURES = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


# 1. tx_succeeded: True only on a dict meta with err None.
check("succeeded: err None",
      TG.tx_succeeded({"meta": {"err": None}}) is True)
check("succeeded: err key absent (meta without err)",
      TG.tx_succeeded({"meta": {}}) is True)
check("succeeded: bare meta dict",
      TG.tx_succeeded({"err": None}) is True)
check("reverted: dict err",
      TG.tx_succeeded(
          {"meta": {"err": {"InstructionError": [0, {}]}}}) is False)
check("reverted: string err",
      TG.tx_succeeded({"meta": {"err": "oops"}}) is False)
check("reverted: bare meta with err",
      TG.tx_succeeded({"err": {"x": 1}}) is False)
check("unknown: no meta",
      TG.tx_succeeded({}) is False)
check("unknown: meta not a dict",
      TG.tx_succeeded({"meta": "nope"}) is False)
check("unknown: None tx",
      TG.tx_succeeded(None) is False)
check("unknown: non-dict tx",
      TG.tx_succeeded("nope") is False)

# 2. tx_reverted_reason: stable keys, None on success.
check("reason: reverted",
      TG.tx_reverted_reason({"meta": {"err": {}}})
      == "tx_reverted")
check("reason: unknown on missing meta",
      TG.tx_reverted_reason({}) == "tx_unknown")
check("reason: unknown on None",
      TG.tx_reverted_reason(None) == "tx_unknown")
check("reason: None on success",
      TG.tx_reverted_reason({"meta": {"err": None}}) is None)

# 3. whole_line_match: equality per line, never substring.
LOGS = [
    "Program log: Instruction: Create",
    "Program log: Instruction: CreateTokenAccount",
    "Program log: Instruction: CreatePool",
    "Program log: hello",
]
check("whole-line: exact Create matches",
      TG.whole_line_match(
          LOGS, "Program log: Instruction: Create") is True)
check("whole-line: Create does NOT match CreateTokenAccount",
      TG.whole_line_match(
          ["Program log: Instruction: CreateTokenAccount"],
          "Program log: Instruction: Create") is False)
check("whole-line: Create does NOT match CreatePool",
      TG.whole_line_match(
          ["Program log: Instruction: CreatePool"],
          "Program log: Instruction: Create") is False)
check("whole-line: no match at all",
      TG.whole_line_match(
          LOGS, "Program log: Instruction: Sell") is False)
check("whole-line: None logs",
      TG.whole_line_match(
          None, "Program log: Instruction: Create") is False)
check("whole-line: non-list logs",
      TG.whole_line_match(
          "Program log: Instruction: Create",
          "Program log: Instruction: Create") is False)
check("whole-line: empty logs",
      TG.whole_line_match(
          [], "Program log: Instruction: Create") is False)

# 4. find_log_line returns the line or None.
check("find: returns the whole line",
      TG.find_log_line(
          LOGS, "Program log: Instruction: Create")
      == "Program log: Instruction: Create")
check("find: None when absent",
      TG.find_log_line(LOGS, "Program log: Instruction: Sell")
      is None)
check("find: None on bad input",
      TG.find_log_line(None, "x") is None)

# 5. log_messages extracts meta.logMessages only.
check("log_messages: extracts list",
      TG.log_messages({"meta": {"logMessages": ["a", "b"]}})
      == ["a", "b"])
check("log_messages: missing -> []",
      TG.log_messages({"meta": {}}) == [])
check("log_messages: no meta -> []",
      TG.log_messages({}) == [])
check("log_messages: non-list -> []",
      TG.log_messages({"meta": {"logMessages": "a"}}) == [])
check("log_messages: None tx -> []",
      TG.log_messages(None) == [])

# 6. The CreateEvent discriminator: a reverted tx's logs still
# carry the Create line, so the line match alone is NOT enough
# — the err gate must refuse it. Both guards together decide.
REVERTED_TX = {
    "meta": {
        "err": {"InstructionError": [0, {"Custom": 6000}]},
        "logMessages": ["Program log: Instruction: Create"],
    }
}
check("both guards: reverted tx with Create line is refused",
      TG.whole_line_match(
          TG.log_messages(REVERTED_TX),
          TG.CREATE_LOG_LINE) is True
      and TG.tx_succeeded(REVERTED_TX) is False)

print()
if FAILURES:
    print("FAILURES: %d" % len(FAILURES))
    for f in FAILURES:
        print("  - " + f)
    sys.exit(1)
print("ALL TX GUARD TESTS PASSED")
