#!/usr/bin/env python3
"""Bounded trial supervisor (2026-11-22, approved 10-minute phone trial).

Runs ONE lane process under a hard wall clock and a hard outbound
request ceiling, then exits. It never restarts the child.

Enforcement (all inside this supervisor + its shims, not a separate
watcher job):

  * Wall: TRIAL_SECONDS (default 600). On expiry the child's whole
    process group is SIGTERM'd, then SIGKILL'd after 5s.
  * Request ceiling: TRIAL_MAX_REQUESTS (default 900) counted across
    EVERY outbound HTTP(S) call the lane makes, whichever transport:
      - urllib: a sitecustomize shim prepended on PYTHONPATH wraps
        urllib.request.urlopen, counts each call, and trips on any
        HTTP 429 response.
      - curl subprocess (reserves.py RPC transport, safety.py
        curl-first): a `curl` shim earlier on PATH counts each call,
        appends a status capture, and trips on HTTP 429.
    Once the counter exceeds the ceiling, further requests are refused
    (shims fail before touching the network) and the supervisor stops
    the trial.
  * Rate-limit stop: any 429 seen by either shim writes the trip file;
    the supervisor polls it every second and kills the trial.
  * No restart: the supervisor exits after the child stops. The phone
    launch must invoke this script once (detached or one-shot job);
    nothing here re-launches anything.

Ledger/journal state lives under FOREX_AGENT_HOME as usual, so the
trial's accounting survives for inspection after exit.

Usage:
  FOREX_AGENT_HOME=<dir> PAPER_ACCOUNT_ENABLED=1 \
      python3 scripts/trial_run.py [--seconds N] [--max-requests N] \
      -- python3 scripts/grad-trader
"""

import os
import signal
import subprocess
import sys
import tempfile
import time

CURL_SHIM = r"""#!/bin/sh
# trial_run curl shim: count every call, refuse past the ceiling,
# trip on HTTP 429. Fails closed: any shim error refuses the request.
COUNT="$TRIAL_COUNT_FILE"; TRIP="$TRIAL_TRIP_FILE"
MAX="$TRIAL_MAX_REQUESTS"
if [ -f "$TRIP" ]; then echo "trial: rate-limit trip active" >&2; exit 22; fi
(
  flock -x 9
  n=$(cat "$COUNT" 2>/dev/null || echo 0)
  n=$((n + 1))
  echo "$n" > "$COUNT"
  if [ "$n" -gt "$MAX" ]; then touch "$TRIP"; echo "trial: request ceiling $MAX exceeded" >&2; exit 23; fi
) 9>"$COUNT.lock" || { echo "trial: counter error" >&2; exit 23; }
body=$(mktemp); code_file=$(mktemp)
"$TRIAL_REAL_CURL" -sS -o "$body" -w '%{http_code}' "$@" >"$code_file" 2>&2
rc=$?
code=$(cat "$code_file" 2>/dev/null | tail -c 3)
cat "$body"; rm -f "$body" "$code_file"
if [ "$code" = "429" ]; then touch "$TRIP"; echo "trial: HTTP 429 from endpoint" >&2; exit 22; fi
exit $rc
"""

SITECUSTOMIZE = r'''"""trial_run urllib shim: count every urlopen, trip on HTTP 429."""
import os
import urllib.request

_COUNT = os.environ.get("TRIAL_COUNT_FILE")
_TRIP = os.environ.get("TRIAL_TRIP_FILE")
_MAX = int(os.environ.get("TRIAL_MAX_REQUESTS", "900"))
_real_urlopen = urllib.request.urlopen


def _bump():
    if os.path.exists(_TRIP):
        raise OSError("trial: rate-limit trip active")
    try:
        import fcntl
        with open(_COUNT + ".lock", "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with open(_COUNT) as f:
                    n = int(f.read() or 0)
            except FileNotFoundError:
                n = 0
            n += 1
            with open(_COUNT, "w") as f:
                f.write(str(n))
            if n > _MAX:
                open(_TRIP, "a").close()
                raise OSError("trial: request ceiling %d exceeded" % _MAX)
    except OSError:
        raise
    except Exception:
        raise OSError("trial: counter error")  # fail closed


def urlopen_trial(url, *a, **kw):
    _bump()
    try:
        return _real_urlopen(url, *a, **kw)
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) == 429:
            open(_TRIP, "a").close()
        raise


urllib.request.urlopen = urlopen_trial
'''


def main(argv):
    seconds = int(os.environ.get("TRIAL_SECONDS", "600"))
    max_req = os.environ.get("TRIAL_MAX_REQUESTS", "900")
    args = list(argv[1:])
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--":
            break
        val = args.pop(0)
        if flag == "--seconds":
            seconds = int(val)
        elif flag == "--max-requests":
            max_req = int(val)
    if not args:
        print("trial_run: no child command given", file=sys.stderr)
        return 2

    home = os.environ.get("FOREX_AGENT_HOME") or os.path.abspath("fahome")
    os.makedirs(home, exist_ok=True)
    count_file = os.path.join(home, "trial_request_count")
    trip_file = os.path.join(home, "trial_trip")
    for p in (count_file, trip_file, count_file + ".lock"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

    shimdir = tempfile.mkdtemp(prefix="trial-shims-")
    real_curl = "/usr/bin/curl"
    for cand in ("/usr/bin/curl", "/bin/curl", "/data/data/com.agt/files/usr/bin/curl"):
        if os.path.exists(cand):
            real_curl = cand
            break
    with open(os.path.join(shimdir, "curl"), "w") as f:
        f.write(CURL_SHIM)
    os.chmod(os.path.join(shimdir, "curl"), 0o755)
    with open(os.path.join(shimdir, "sitecustomize.py"), "w") as f:
        f.write(SITECUSTOMIZE)

    env = dict(os.environ)
    env["PATH"] = shimdir + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = shimdir + os.pathsep + env.get("PYTHONPATH", "")
    env["TRIAL_COUNT_FILE"] = count_file
    env["TRIAL_TRIP_FILE"] = trip_file
    env["TRIAL_MAX_REQUESTS"] = str(max_req)
    env["TRIAL_REAL_CURL"] = real_curl

    child = subprocess.Popen(args, env=env, start_new_session=True)
    deadline = time.monotonic() + seconds
    outcome = "completed"
    while True:
        rc = child.poll()
        if rc is not None:
            break
        if os.path.exists(trip_file):
            outcome = "stopped: rate-limit or request-ceiling trip"
            break
        if time.monotonic() >= deadline:
            outcome = "stopped: %ds wall reached" % seconds
            break
        time.sleep(1.0)
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
    try:
        with open(count_file) as f:
            total = f.read().strip()
    except FileNotFoundError:
        total = "0"
    print("trial_run: outcome=%s wall=%ds requests=%s/%s child_rc=%s"
          % (outcome, seconds, total, max_req, child.returncode))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
