#!/usr/bin/env python3
"""Bounded trial supervisor tests (2026-11-22).

scripts/trial_run.py enforces the approved phone trial's bounds:
a hard wall clock, a hard outbound request ceiling counted across
BOTH transports the lanes use (urllib + the curl subprocess shim),
an immediate stop on HTTP 429, and no restart after exit. These
tests run fully offline: children hit 127.0.0.1:9 (discard, refused)
or just sleep.

Plain script - run directly (scripts/run_script_tests.py).
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRIAL = os.path.join(REPO, "scripts", "trial_run.py")

fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " " + name)
    if not cond:
        fails.append(name)


def run_trial(args, env_extra=None, timeout=60):
    env = dict(os.environ)
    env.pop("TRIAL_TRIP_FILE", None)
    env.pop("TRIAL_COUNT_FILE", None)
    if env_extra:
        env.update(env_extra)
    with tempfile.TemporaryDirectory() as td:
        env["FOREX_AGENT_HOME"] = td
        p = subprocess.run([sys.executable, TRIAL] + args, env=env,
                           capture_output=True, text=True, timeout=timeout)
        last = [l for l in p.stdout.strip().splitlines()
                if l.startswith("trial_run: outcome=")]
        return (p.returncode, last[-1] if last else "", p.stdout,
                p.stderr, td)


# 1. Hard wall: a child that sleeps well past the wall is killed and
#    the supervisor exits on its own (no restart, outcome reported).
t0 = time.monotonic()
rc, out, so, se, _ = run_trial(
    ["--seconds", "3", "--", sys.executable, "-c",
     "import time; time.sleep(60)"])
check("wall: supervisor returns promptly",
      3 <= time.monotonic() - t0 < 20)
check("wall: outcome reports the wall", "wall reached" in out)
check("wall: exit code 0 (supervised stop, no restart)", rc == 0)

# 2. Request ceiling via the urllib shim: connection-refused calls
#    still count; the trip fires and the supervisor kills the child.
rc, out, so, se, _ = run_trial(
    ["--seconds", "30", "--max-requests", "5", "--",
     sys.executable, "-c",
     "import time, urllib.request\n"
     "for i in range(50):\n"
     "    try: urllib.request.urlopen('http://127.0.0.1:9/', timeout=1)\n"
     "    except Exception: pass\n"
     "time.sleep(60)"])
check("ceiling: outcome reports the trip", "trip" in out)
check("ceiling: request count stops at ceiling+1",
      "requests=6/5" in out)

# 3. 429 via the urllib shim trips immediately (local stub server).
server = os.path.join(tempfile.gettempdir(), "trial_429_server.py")
with open(server, "w") as f:
    f.write(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(429); self.end_headers()\n"
        "    def log_message(self, *a): pass\n"
        "HTTPServer(('127.0.0.1', 0), H).serve_forever()\n")
stub = subprocess.Popen([sys.executable, server],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    # discover the port is overkill; bind our own throwaway server instead
    stub.kill()
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(429)
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    rc, out, so, se, _ = run_trial(
        ["--seconds", "30", "--max-requests", "100", "--",
         sys.executable, "-c",
         "import time, urllib.request\n"
         "try: urllib.request.urlopen('http://127.0.0.1:%d/', timeout=5)\n"
         "except Exception: pass\n"
         "time.sleep(60)" % port])
    check("429: outcome reports the trip", "trip" in out)
finally:
    try:
        httpd.shutdown()
    except Exception:
        pass
    stub.poll()

# 4. curl shim: the PATH shim counts curl subprocess calls too and
#    refuses past the ceiling before touching the network.
curl = "/usr/bin/curl" if os.path.exists("/usr/bin/curl") else None
if curl is None:
    # find the real one the way trial_run does
    for cand in ("/bin/curl",):
        if os.path.exists(cand):
            curl = cand
check("curl available for shim test", curl is not None)
if curl:
    rc, out, so, se, _ = run_trial(
        ["--seconds", "30", "--max-requests", "3", "--",
         "/bin/sh", "-c",
         "i=0; while [ $i -lt 10 ]; do curl -sS "
         "http://127.0.0.1:9/ 2>/dev/null; i=$((i+1)); done; sleep 60"])
    check("curl shim: outcome reports the trip", "trip" in out)
    check("curl shim: request count stops at ceiling+1",
          "requests=4/3" in out)

# 5. A child that exits cleanly on its own reports completed.
rc, out, so, se, _ = run_trial(
    ["--seconds", "30", "--", sys.executable, "-c", "print('ok')"])
check("clean child: outcome completed", "completed" in out and rc == 0)

# 6. Wall stop kills the whole process group: child AND its own
#    subprocess grandchild are dead after the supervisor returns.
with tempfile.TemporaryDirectory() as td:
    pidfile = os.path.join(td, "pids")
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = td
    p = subprocess.run(
        [sys.executable, TRIAL, "--seconds", "2", "--",
         sys.executable, "-c",
         "import subprocess, sys, time\n"
         "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
         "open(%r, 'w').write('%%d %%d' %% (__import__('os').getpid(), g.pid))\n"
         "time.sleep(60)" % pidfile],
        env=env, capture_output=True, text=True, timeout=60)
    with open(pidfile) as f:
        child_pid, grand_pid = map(int, f.read().split())
    dead = True
    for pid in (child_pid, grand_pid):
        try:
            os.kill(pid, 0)
            dead = False
        except (ProcessLookupError, PermissionError):
            pass
    check("wall: child and grandchild processes are dead", dead)

# 7. One-shot: a rerun against the same state dir idles WITHOUT
#    network or exit (an auto-restarting supervisor spins harmlessly)
#    and records the refusal in trial_outcome.
with tempfile.TemporaryDirectory() as td:
    env = dict(os.environ)
    env["FOREX_AGENT_HOME"] = td
    a = [sys.executable, TRIAL, "--seconds", "5", "--",
         sys.executable, "-c", "print('ok')"]
    subprocess.run(a, env=env, capture_output=True, text=True, timeout=60)
    p2 = subprocess.Popen(a, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    time.sleep(3)
    alive = p2.poll() is None  # idling, not exited
    p2.kill()
    p2.wait()
    with open(os.path.join(td, "trial_outcome")) as f:
        outcome_log = f.read()
    check("one-shot: rerun idles without exit and logs refusal",
          alive and "already-consumed" in outcome_log)

# 9. WS-style handshake 429 (raw socket status line) trips the trial.
try:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    ws_port = srv.getsockname()[1]

    def _serve_429():
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 429 Too Many Requests\r\n\r\n")
        conn.close()

    threading.Thread(target=_serve_429, daemon=True).start()
    rc, out, so, se, _ = run_trial(
        ["--seconds", "30", "--max-requests", "100", "--",
         sys.executable, "-c",
         "import socket, time\n"
         "s = socket.create_connection(('127.0.0.1', %d), timeout=5)\n"
         "s.sendall(b'GET / HTTP/1.1\\r\\n\\r\\n')\n"
         "s.recv(4096)\n"
         "time.sleep(60)" % ws_port])
    check("ws handshake 429: outcome reports the trip", "trip" in out)
finally:
    try:
        srv.close()
    except Exception:
        pass

# 8. Redirects count per connect: a 302 chain produces two counted
#    connections, so redirect loops cannot bypass the ceiling.
try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class H302(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/final":
                self.send_response(200)
            else:
                self.send_response(302)
                self.send_header("Location", "/final")
            self.end_headers()

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), H302)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    rc, out, so, se, _ = run_trial(
        ["--seconds", "30", "--max-requests", "100", "--",
         sys.executable, "-c",
         "import urllib.request\n"
         "urllib.request.urlopen('http://127.0.0.1:%d/', timeout=5).read()"
         % port])
    check("redirect: counted once per connect (2 for one 302)",
          "requests=2/100" in out)
finally:
    try:
        httpd.shutdown()
    except Exception:
        pass

print("FAILURES: %d" % len(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
