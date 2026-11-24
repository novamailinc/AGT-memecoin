"""ns-init.py: exit-status and orphan-reaping contract. Harmless processes only
(python/sleep of <= 1s). No network, no daemons, no repo state. ns-init only
signals outside its command when it is PID 1 of a namespace, which this test
never creates.

Preconditions the orphan tests need (and check, failing loudly with a clear
message instead of passing or skipping): /proc must be the /proc of THIS pid
namespace. Under `unshare -p` without --mount-proc, /proc/<pid> resolves to an
unrelated host process and every state read is meaningless. scripts/contained.sh
mounts a private /proc; a wrapper that does not will fail the precondition."""
import os
import subprocess
import sys
import textwrap
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
NS_INIT = os.path.join(os.path.dirname(HERE), "scripts", "ns-init.py")


def _run(*cmd, timeout=20):
    return subprocess.run([sys.executable, NS_INIT, *cmd],
                          capture_output=True, text=True, timeout=timeout)


def _proc_is_local():
    try:
        return os.readlink("/proc/self") == str(os.getpid())
    except OSError:
        return False


class NsInitContract(unittest.TestCase):
    def test_exit_status_passthrough(self):
        self.assertEqual(_run("sh", "-c", "exit 0").returncode, 0)
        self.assertEqual(_run("sh", "-c", "exit 7").returncode, 7)

    def test_signal_death_is_128_plus_n(self):
        # The command resets SIGTERM to the default first and then kills itself.
        # An inherited SIG_IGN (from a wrapper, nohup-style launchers, a CI
        # runner) survives exec and would make a plain `sh -c "kill -TERM $$"`
        # exit 0, so the disposition is set explicitly instead of assumed.
        term = "import os, signal; signal.signal(signal.SIGTERM, signal.SIG_DFL); os.kill(os.getpid(), signal.SIGTERM)"
        self.assertEqual(_run(sys.executable, "-c", term).returncode, 128 + 15)
        # SIGKILL cannot be ignored: independent of any inherited disposition.
        kill = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
        self.assertEqual(_run(sys.executable, "-c", kill).returncode, 128 + 9)

    def test_unexecutable_command_is_127(self):
        r = _run("/nonexistent/definitely-not-a-command")
        self.assertEqual(r.returncode, 127, r.stderr)
        self.assertIn("cannot execute", r.stderr)

    def test_no_command_is_usage_error(self):
        self.assertEqual(_run().returncode, 64)


# Grandchild orphan: its parent exits at once, so it is re-parented to the
# nearest subreaper. Prints "<pid> <state-letter-or-None>" a second after the
# orphan (0.2s) has exited.
ORPHAN = textwrap.dedent("""
    import os, time
    r, w = os.pipe()
    if os.fork() == 0:
        os.close(r)
        gc = os.fork()
        if gc == 0:
            time.sleep(0.2); os._exit(0)
        os.write(w, str(gc).encode()); os._exit(0)
    os.close(w)
    pid = int(os.read(r, 32).decode())
    time.sleep(1.0)
    st = None
    try:
        with open("/proc/%d/status" % pid) as fh:
            st = [l.split()[1] for l in fh if l.startswith("State:")][0]
    except OSError:
        pass
    print(pid, st)
""")

# Control driver: makes ITS OWN process a subreaper and never wait()s, so the
# orphan must stay a zombie. Runs in a separate interpreter, so the subreaper
# flag never touches the test runner. Exits (and is reaped) when done.
CONTROL = textwrap.dedent("""
    import ctypes, subprocess, sys
    ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0)
    p = subprocess.run([sys.executable, "-c", sys.argv[1]], capture_output=True, text=True, timeout=20)
    sys.stdout.write(p.stdout)
""")


class OrphanReaping(unittest.TestCase):
    def setUp(self):
        if not _proc_is_local():
            self.fail("/proc is not this pid namespace's /proc (host /proc under "
                      "unshare -p without --mount-proc): process states are "
                      "meaningless. Run through scripts/contained.sh.")

    def test_orphan_is_reaped_under_ns_init(self):
        r = _run(sys.executable, "-c", ORPHAN)
        self.assertEqual(r.returncode, 0, r.stderr)
        pid, st = r.stdout.split()
        self.assertEqual(st, "None", "orphan %s left as state %s" % (pid, st))

    def test_control_orphan_stays_zombie_without_reaper(self):
        p = subprocess.run([sys.executable, "-c", CONTROL, ORPHAN],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        pid, st = p.stdout.split()
        self.assertEqual(st, "Z", "control did not produce a zombie (state %s)" % st)


if __name__ == "__main__":
    unittest.main()
