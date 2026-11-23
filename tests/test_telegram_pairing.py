#!/usr/bin/env python3
"""Telegram pairing gate tests (2026-11-22).

The control room must be claimable ONLY via a verified allowlist or a
single-use, expiring pairing code; with neither configured every claim
is denied (no first-sender pairing). The code is never echoed or
logged, and a consumed code cannot rebind after replay or restart.

Plain script - run directly (scripts/run_script_tests.py).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "daemon"))

from daemon.telegram import security as sec_mod  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " " + name)
    if not cond:
        fails.append(name)


def fresh():
    return sec_mod.SecurityContext({})


for var in ("TELEGRAM_ALLOWED_CHAT_ID", "TELEGRAM_ALLOWED_USER_ID",
            "TELEGRAM_PAIR_CODE", "TELEGRAM_PAIR_CODE_EXPIRES"):
    os.environ.pop(var, None)

# No code and no allowlist: every claim is denied, none pairs.
s = fresh()
ok, msg = s.try_pair("111", "222")
check("no config: first /start denied", not ok and "denied" in msg)
check("no config: nothing paired", not s.is_paired())
ok, msg = s.try_pair("111", "222", code="anything")
check("no config: code-bearing claim also denied", not ok)

# Allowlist path still works without any code.
os.environ["TELEGRAM_ALLOWED_CHAT_ID"] = "111"
os.environ["TELEGRAM_ALLOWED_USER_ID"] = "222"
s = fresh()
ok, msg = s.try_pair("999", "999")
check("allowlist: stranger denied", not ok and not s.is_paired())
ok, msg = s.try_pair("111", "222")
check("allowlist: exact owner pairs", ok and s.is_paired())
ok, msg = s.try_pair("111", "222")
check("allowlist: second pairing denied", not ok and "already paired" in msg)
del os.environ["TELEGRAM_ALLOWED_CHAT_ID"]
del os.environ["TELEGRAM_ALLOWED_USER_ID"]

# Pairing code path.
os.environ["TELEGRAM_PAIR_CODE"] = "deadbeef-secret"
s = fresh()
ok, msg = s.try_pair("111", "222")
check("code gate: missing code denied", not ok and not s.is_paired())
ok, msg = s.try_pair("111", "222", code="wrong")
check("code gate: wrong code denied", not ok and not s.is_paired())
ok, msg = s.try_pair("111", "222", code="deadbeef-secret")
check("code gate: right code pairs", ok and s.is_paired())
check("code gate: code marked consumed", s.state.get("pair_code_consumed") is True)
# Replay: a fresh daemon over the SAME state cannot re-pair with the code.
s2 = sec_mod.SecurityContext(s.state)
ok, msg = s2.try_pair("333", "333", code="deadbeef-secret")
check("code gate: replay after restart cannot rebind", not ok)
check("code gate: original pairing survives restart",
      s2.is_authorized("111", "222"))
# /repair stays allowlist-only: with no allowlist it is denied and the
# pairing is untouched (handler-level gate; try_pair would deny anyway).
os.environ.pop("TELEGRAM_PAIR_CODE")

# Expiry.
os.environ["TELEGRAM_PAIR_CODE"] = "deadbeef-secret"
os.environ["TELEGRAM_PAIR_CODE_EXPIRES"] = "1"  # long past
s = fresh()
ok, msg = s.try_pair("111", "222", code="deadbeef-secret")
check("code gate: expired code denied", not ok and not s.is_paired())
os.environ.pop("TELEGRAM_PAIR_CODE")
os.environ.pop("TELEGRAM_PAIR_CODE_EXPIRES")

print("FAILURES: %d" % len(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
