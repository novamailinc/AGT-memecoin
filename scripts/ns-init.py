#!/usr/bin/env python3
"""Minimal init for a PID namespace (used by scripts/contained.sh).

usage: ns-init.py CMD [ARGS...]

Runs CMD as a child and acts as the namespace's PID 1 for everything
that detaches: daemons started with setsid/nohup are re-parented here
when their launcher exits, and a PID 1 that never wait()s leaves them
as zombies after they die. A zombie still answers kill(pid, 0), so
"stop and confirm dead" checks never succeed.

Exit status (shell convention): CMD's own status; 128+N if CMD died of
signal N; 127 if CMD could not be executed. SIGINT/SIGTERM are
forwarded to CMD.

When it is really PID 1 (inside the namespace), after CMD exits it
SIGKILLs every remaining process in the namespace and reaps them all.
When it is NOT PID 1 (unit tests, a normal shell) it never signals
anything outside CMD: it just reaps zombies already re-parented to it
and returns. It marks itself a child subreaper so the orphan path is
the same in both cases.
"""
import ctypes
import os
import signal
import sys

PR_SET_CHILD_SUBREAPER = 36


def _subreaper():
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except Exception:
        pass


def _code(status):
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return os.WEXITSTATUS(status)


def _drain():
    """Reap everything already exited, without blocking."""
    while True:
        try:
            done, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if done == 0:
            return


def main(argv):
    if not argv:
        print("usage: ns-init.py CMD [ARGS...]", file=sys.stderr)
        return 64
    _subreaper()
    pid = os.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError as exc:
            print("ns-init: cannot execute %s: %s" % (argv[0], exc), file=sys.stderr)
            os._exit(127)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: os.kill(pid, s))
    rc = 1
    while True:
        try:
            done, status = os.wait()
        except InterruptedError:
            continue
        except ChildProcessError:
            return rc
        if done == pid:
            rc = _code(status)
            break
    if os.getpid() == 1:
        try:
            os.kill(-1, signal.SIGKILL)  # PID 1 only: this is the namespace
        except OSError:
            pass
        while True:
            try:
                os.wait()
            except InterruptedError:
                continue
            except ChildProcessError:
                break
    else:
        _drain()
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
