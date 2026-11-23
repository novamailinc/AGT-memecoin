#!/usr/bin/env python3
"""Bounded trial supervisor (2026-11-22, approved 10-minute phone trial).

Runs ONE lane process under a hard wall clock and a hard outbound
request ceiling, then exits. It never restarts the child.

Enforcement (all inside this supervisor + its shims, not a separate
watcher job):

  * Wall: TRIAL_SECONDS (default 600). On expiry the child's whole
    process group is SIGTERM'd, then SIGKILL'd after 5s.
  * Request ceiling: TRIAL_MAX_REQUESTS (default 900) counted across
    EVERY outbound connection the trial makes, whichever transport:
      - Python sockets: a sitecustomize shim prepended on PYTHONPATH
        wraps socket.socket.connect, so urllib (including redirect and
        retry reconnects) and websocket-client (PumpPortal stream
        connects/reconnects) are each counted per TCP connect. WS
        messages flow on an already-counted connection; they are not
        separately limited - the 10-minute wall bounds stream time.
        An HTTP 429 response additionally trips the trial immediately
        (urllib HTTPError path).
      - curl subprocesses (reserves.py RPC transport, safety.py
        curl-first - both resolve `curl` via PATH/shutil.which): a
        `curl` shim earlier on PATH counts each invocation, appends a
        status capture, and trips on HTTP 429.
    Once the counter exceeds the ceiling, further connects are refused
    (shims fail before touching the network) and the trial stops.
  * Rate-limit stop: any 429 seen by either shim writes the trip file;
    the supervisor polls it every second and kills the trial.
  * No restart / one-shot: the supervisor exits after the child stops
    and stamps <FOREX_AGENT_HOME>/trial_consumed at start; a second
    run against the same state directory refuses unless the directory
    is wiped (a fresh deployment is a fresh trial). The phone launch
    must invoke this script once (detached or one-shot job); nothing
    here re-launches anything.

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
    # Counting happens at socket.connect; here we only trip on 429.
    try:
        return _real_urlopen(url, *a, **kw)
    except urllib.error.HTTPError as e:
        if getattr(e, "code", None) == 429:
            open(_TRIP, "a").close()
        raise


urllib.request.urlopen = urlopen_trial

# Socket-level counting covers urllib, websocket-client, and any other
# Python TCP client: every outbound connect counts once. urllib opens
# a fresh connection per request (no keep-alive reuse), so connects ==
# requests on these paths; redirects and retries reconnect and count.
# Fail closed on any shim error.
import socket as _socket

_real_connect = _socket.socket.connect


def _connect_trial(self, address):
    # Count every TCP connect, loopback included: the lanes have no
    # legitimate loopback networking, and the tests rely on refused
    # loopback connects being counted.
    _bump()
    return _real_connect(self, address)


_socket.socket.connect = _connect_trial

# 429 detection for raw-socket HTTP handshakes (the logstream WSClient
# reads its own status line, bypassing urllib). The handshake response
# is the first application bytes on a fresh connection and starts with
# "HTTP/"; upgraded WS frames never do, so this cannot false-trip on
# stream data. Over TLS the WSClient wraps its socket with
# ssl.wrap_socket, so reads arrive via SSLSocket.recv: patch both.
_real_recv = _socket.socket.recv


def _check_429(data):
    if data[:5] == b"HTTP/":
        line = data.split(b"\r\n", 1)[0]
        if b" 429" in line:
            open(_TRIP, "a").close()


def _recv_trial(self, bufsize, flags=0):
    data = _real_recv(self, bufsize, flags)
    _check_429(data)
    return data


_socket.socket.recv = _recv_trial

try:
    import ssl as _ssl

    _real_ssl_recv = _ssl.SSLSocket.recv

    def _ssl_recv_trial(self, bufsize=1024, flags=0):
        data = _real_ssl_recv(self, bufsize, flags)
        _check_429(data)
        return data

    _ssl.SSLSocket.recv = _ssl_recv_trial
except Exception:
    pass  # ssl always present in the supported runtimes
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
    consumed = os.path.join(home, "trial_consumed")
    if os.path.exists(consumed):
        # One-shot: a trial already ran against this state dir. Idle
        # WITHOUT network or exit so an auto-restarting supervisor
        # spins harmlessly instead of re-running the trial. An
        # explicitly new trial needs a fresh state dir.
        msg = ("trial_run: outcome=already-consumed idling (no network); "
               "wipe %s for an explicitly new trial" % home)
        print(msg, file=sys.stderr)
        try:
            with open(os.path.join(home, "trial_outcome"), "a") as f:
                f.write(msg + "\n")
        except OSError:
            pass
        while True:
            time.sleep(3600)
    # Stamp the marker ATOMICALLY before any child (network) activity:
    # a crash mid-write can never leave a half marker, and a restart
    # sees the marker and idles before touching the network.
    tmp_marker = consumed + ".tmp"
    with open(tmp_marker, "w") as f:
        f.write(str(time.time()))
    os.replace(tmp_marker, consumed)
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

    def _stop_child(signum=None, frame=None):
        # On SIGTERM/SIGINT (deploy stop, timeout kill), take the whole
        # child process group down too: an orphaned lane would keep
        # consuming the network outside the trial bounds.
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        raise SystemExit(128 + (signum or 0))

    signal.signal(signal.SIGTERM, _stop_child)
    signal.signal(signal.SIGINT, _stop_child)
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
    line = ("trial_run: outcome=%s wall=%ds requests=%s/%s child_rc=%s"
            % (outcome, seconds, total, max_req, child.returncode))
    print(line)
    try:
        with open(os.path.join(home, "trial_outcome"), "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
