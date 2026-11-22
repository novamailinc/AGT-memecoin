#!/usr/bin/env python3
"""Switch wiring tests (advanced build, piece 10, 2026-11-21).

Covers the switches wiring:
(a) scripts/helius-429-watch now drives auto-quiet: at >=5
    429s/10min it engages quiet for 1h (bounded, not the manual
    12h) and journals the engage to switch_log.jsonl; below
    threshold it engages nothing. Tested end-to-end with real
    pacer 429 events in a temp home (no live state, no Telegram
    token — the ping fails gracefully, as designed).
(b) reserves._try_public journals the public-RPC fallback
    transition ONCE per outage (engage), and the next Helius
    success journals the restore — not once per call.

Plain script — run directly with the venv python, not pytest.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import subprocess
import sys
import tempfile
import time

REPO = _REPO_ROOT
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def _switch_log(home):
    p = os.path.join(home, "switch_log.jsonl")
    if not os.path.exists(p):
        return []
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


WATCH = os.path.join(REPO, "scripts", "helius-429-watch")
VENV = "/home/hatch/workspace/Forex-agent/.venv/bin/python"

# ---------------- (a) auto-quiet engages at >=5 429s --------------
home_a = tempfile.mkdtemp(prefix="switch-wire-a-")
env_a = dict(os.environ, FOREX_AGENT_HOME=home_a)
# Record 6 real 429 events through the pacer API, in-process,
# under the same temp home the watch subprocess will read. The
# env must be set BEFORE importing the module: the pacer
# singleton resolves its home at import.
os.environ["FOREX_AGENT_HOME"] = home_a
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
import helius_ratelimit as RL
for _ in range(6):
    RL.pacer().report("test-bucket", False)
check("setup: 6 recent 429s recorded",
      len(RL.pacer().recent_429_events(window_s=600)) >= 6)
r = subprocess.run([VENV, WATCH], env=env_a, capture_output=True,
                   text=True, timeout=60)
check("watch: exits 1 while a spike is active", r.returncode == 1)
check("watch: reports the spike", "429 SPIKE" in r.stderr)
log = _switch_log(home_a)
check("watch: auto-quiet engage journaled once",
      len([e for e in log if e.get("switch") == "helius_quiet"
           and e.get("action") == "engage"]) == 1)
# Quiet state is engaged for ~1h, not 12h.
import helius_ratelimit as RL2
q = RL2.quiet_status(home=home_a)
check("watch: quiet active after engage", q.get("active") is True)
check("watch: quiet duration is ~1h (bounded, not 12h)",
      3000 < (q.get("until", 0) - time.time()) < 4200)
# A second watch run while quiet is active must NOT re-engage.
r2 = subprocess.run([VENV, WATCH], env=env_a, capture_output=True,
                    text=True, timeout=60)
check("watch: second run still exits 1 (spike ongoing)",
      r2.returncode == 1)
log2 = _switch_log(home_a)
check("watch: no second engage while already quiet",
      len([e for e in log2 if e.get("switch") == "helius_quiet"
           and e.get("action") == "engage"]) == 1)

# ---------------- (a2) below threshold: nothing engages -----------
home_b = tempfile.mkdtemp(prefix="switch-wire-b-")
env_b = dict(os.environ, FOREX_AGENT_HOME=home_b)
r3 = subprocess.run([VENV, WATCH], env=env_b, capture_output=True,
                    text=True, timeout=60)
check("watch: exits 0 with no 429s", r3.returncode == 0)
check("watch: no quiet engaged below threshold",
      RL2.quiet_status(home=home_b).get("active") is not True)
check("watch: no switch log below threshold",
      _switch_log(home_b) == [])

# ---------------- (b) public-RPC fallback transition log -----------
home_c = tempfile.mkdtemp(prefix="switch-wire-c-")
import reserves as RES
RES.HOME = home_c
RES._PUBLIC_FALLBACK_ON[0] = False


class _Budget:
    def acquire(self):
        return True


_orig_budget = RES._public_budget
_orig_attempt = RES._rpc_attempt
RES._public_budget = lambda: _Budget()
RES._rpc_attempt = lambda url, body, method, host, timeout: \
    (None, "timeout", 0.0)
try:
    out = RES._try_public(b"{}", "getSignaturesForAddress",
                          ["https://api.mainnet-beta.solana.com"], 12)
    check("fallback: _try_public returns None on transport failure",
          out is None)
    log_c = _switch_log(home_c)
    check("fallback: engage journaled once",
          len([e for e in log_c
               if e.get("switch") == "public_rpc_fallback"
               and e.get("action") == "engage"]) == 1)
    # A second fallback call during the same outage: no new line.
    RES._try_public(b"{}", "getSignaturesForAddress",
                    ["https://api.mainnet-beta.solana.com"], 12)
    log_c2 = _switch_log(home_c)
    check("fallback: second call in same outage logs nothing new",
          len([e for e in log_c2
               if e.get("switch") == "public_rpc_fallback"]) == 1)
    # Helius success restores: one restore line, flag cleared.
    RES._note_helius_restored()
    log_c3 = _switch_log(home_c)
    check("fallback: restore journaled on Helius success",
          len([e for e in log_c3
               if e.get("switch") == "public_rpc_fallback"
               and e.get("action") == "restore"]) == 1)
    # Next outage engages again.
    RES._try_public(b"{}", "getSignaturesForAddress",
                    ["https://api.mainnet-beta.solana.com"], 12)
    log_c4 = _switch_log(home_c)
    check("fallback: re-engage journaled after restore",
          len([e for e in log_c4
               if e.get("switch") == "public_rpc_fallback"
               and e.get("action") == "engage"]) == 2)
finally:
    RES._public_budget = _orig_budget
    RES._rpc_attempt = _orig_attempt
    RES._PUBLIC_FALLBACK_ON[0] = False

print()
if fails:
    print("FAILURES: %d" % len(fails))
    for f in fails:
        print(" - " + f)
    sys.exit(1)
print("ALL SWITCHES-WIRE TESTS PASSED")
