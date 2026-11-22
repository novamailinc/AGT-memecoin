"""Restore drill for the AGT deployment (Phase 13 finalize).

Runs the documented restore procedure (deploy/agt/
backup_recovery.md steps 1-9) against a SCRATCH home — never the
live home — and records pass/fail per step. Usage:

  restore_drill.py --archive <forex-backup-*.tar.gz>
                   --home <scratch restore dir>
                   [--expected <balances JSON>]
                   [--report <path>]
                   [--port N]

Steps:
  1. archive_exists — the archive + sha256 sidecar exist.
  2. checksum — the sidecar matches the archive.
  3. extract — the tarball extracts to the scratch home.
  4. no_credential_material — no secrets ever land in a restore
     (same forbidden list as backup_verify).
  5. layout_normalize — the archive stores local.db at its ROOT
     and heartbeat.json at its root, while the live home expects
     storage/local.db and run/heartbeat.json. A naive "move the
     copy into place" would leave the deployment starting on a
     FRESH EMPTY database (db_init would create one, silently
     losing the event journal + safety state). The drill moves
     them to their live paths and records each move. Both layouts
     are accepted (an archive that already has storage/local.db
     is left alone).
  6. ledgers_parse — every *_ledger.json, live10_demo_ledger.json
     and killswitch/state.json parses.
  7. killswitch_shadow — killswitch/state.json.bak exists.
  8. balances_match — bankrolls per lane + Live10 lane balances
     match the --expected snapshot (recorded before the stop);
     skipped with a named reason when no --expected is given.
  9. db_check — db_init.py --check passes on the restored home.
 10. deployment_starts — credentials --preflight passes (env
     passed through) and the status dashboard answers /health
     against the restored home. This proves the restored copy
     boots; it does NOT prove new closes (that needs the traders
     running on the phone — an on-phone drill step).

Safety: --home must NOT be the live home (~/.forex-agent) unless
--force-live-home is passed. The drill never passes it. The
archive is only ever read, never modified.

Exit 0 iff every executed step passes. The drill report (JSON +
Markdown) is written to --report, or to
<home>/restore-drill-<UTC date>.md by default.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
AGT = os.path.join(REPO, "deploy", "agt")
sys.path.insert(0, AGT)
import backup_verify as BV  # noqa: E402

LIVE_HOME = os.path.join(os.path.expanduser("~"), ".forex-agent")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Drill(object):
    def __init__(self, archive, home, expected=None, port=18766):
        self.archive = os.path.abspath(archive)
        self.home = os.path.abspath(home)
        self.expected = expected
        self.port = port
        self.steps = []

    def step(self, name, passed, detail=""):
        self.steps.append({"name": name, "passed": bool(passed),
                           "detail": detail})
        print("%s %s: %s" % ("PASS" if passed else "FAIL", name,
                             detail))
        return passed

    @property
    def passed(self):
        return all(s["passed"] for s in self.steps)

    def run(self):
        # 1. archive exists
        if not self.step("archive_exists",
                         os.path.isfile(self.archive),
                         self.archive if os.path.isfile(self.archive)
                         else "no such file: %s" % self.archive):
            return self
        sidecar = self.archive + ".sha256"
        # 2. checksum
        if not os.path.isfile(sidecar):
            self.step("checksum", False,
                      "missing sidecar: %s" % sidecar)
            return self
        expected_sha = open(sidecar).read().strip().split()[0]
        actual_sha = _sha256(self.archive)
        if not self.step("checksum", actual_sha == expected_sha,
                         "sidecar %s, actual %s"
                         % (expected_sha[:16], actual_sha[:16])):
            return self
        # 3. extract to the scratch home (fresh dir)
        if os.path.exists(self.home):
            shutil.rmtree(self.home)
        os.makedirs(self.home)
        try:
            with tarfile.open(self.archive, "r:gz") as tf:
                members = tf.getnames()
                tf.extractall(self.home)
        except (tarfile.TarError, OSError, ValueError) as exc:
            self.step("extract", False, "cannot extract: %s" % exc)
            return self
        self.step("extract", True,
                  "%d members -> %s" % (len(members), self.home))
        # 4. no credential material
        bad = [m for m in members if BV._is_forbidden(m)]
        self.step("no_credential_material", not bad,
                  "forbidden members: %s" % ", ".join(bad) if bad
                  else "no forbidden members")
        # 5. layout normalize (see module docstring — the drill's
        # real find: local.db + heartbeat.json sit at the archive
        # root, not at their live paths).
        moves = []
        for src_rel, dst_rel in (
                ("local.db", os.path.join("storage", "local.db")),
                ("heartbeat.json",
                 os.path.join("run", "heartbeat.json"))):
            src = os.path.join(self.home, src_rel)
            dst = os.path.join(self.home, dst_rel)
            if os.path.isfile(src) and not os.path.isfile(dst):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
                moves.append("%s -> %s" % (src_rel, dst_rel))
            elif os.path.isfile(dst):
                moves.append("%s already at live path" % dst_rel)
            else:
                moves.append("%s absent (not in archive)" % src_rel)
        self.step("layout_normalize", True, "; ".join(moves))
        # 6. ledgers parse
        json_files = []
        for root, _dirs, files in os.walk(self.home):
            for name in files:
                if name.endswith("_ledger.json") or name in (
                        "live10_demo_ledger.json",):
                    json_files.append(os.path.join(root, name))
        ks = os.path.join(self.home, "killswitch", "state.json")
        if os.path.isfile(ks):
            json_files.append(ks)
        bad_json = []
        for p in sorted(json_files):
            try:
                with open(p) as fh:
                    json.load(fh)
            except (OSError, ValueError) as exc:
                bad_json.append("%s: %s" % (p, exc))
        ok = not bad_json and bool(json_files)
        self.step("ledgers_parse", ok,
                  "%d JSON state files checked%s" % (
                      len(json_files),
                      ("; unparsable: " + "; ".join(bad_json))
                      if bad_json else
                      ("; no state files at all" if not json_files
                       else "")))
        # 7. killswitch shadow
        shadow = os.path.join(self.home, "killswitch",
                              "state.json.bak")
        self.step("killswitch_shadow", os.path.isfile(shadow),
                  "killswitch/state.json.bak present"
                  if os.path.isfile(shadow)
                  else "killswitch shadow missing")
        # 8. balances match the pre-stop snapshot
        if self.expected is None:
            self.step("balances_match", True,
                      "SKIPPED — no --expected snapshot given "
                      "(balances not compared)")
        else:
            mismatches = []
            for lane, want in (self.expected.get("lanes") or {}).items():
                p = os.path.join(self.home, "%s_ledger.json" % lane)
                try:
                    got = json.load(open(p))
                except (OSError, ValueError) as exc:
                    mismatches.append("%s: unreadable (%s)" % (lane, exc))
                    continue
                if got.get("bankroll") != want.get("bankroll"):
                    mismatches.append(
                        "%s bankroll: restored %r, expected %r"
                        % (lane, got.get("bankroll"),
                           want.get("bankroll")))
            live10 = self.expected.get("live10")
            if live10:
                try:
                    got = json.load(open(os.path.join(
                        self.home, "live10_demo_ledger.json")))
                except (OSError, ValueError) as exc:
                    mismatches.append("live10: unreadable (%s)" % exc)
                    got = None
                if got is not None:
                    for lane, want_bal in live10.items():
                        got_bal = ((got.get("lanes") or {}).get(lane)
                                   or {}).get("balance")
                        if got_bal != want_bal:
                            mismatches.append(
                                "live10 %s balance: restored %r, "
                                "expected %r" % (lane, got_bal, want_bal))
            self.step("balances_match", not mismatches,
                      "all balances match the pre-stop snapshot"
                      if not mismatches else "; ".join(mismatches))
        # 9. db check on the restored home
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "db_init.py"),
             "--home", self.home, "--check"],
            capture_output=True, text=True)
        self.step("db_check", proc.returncode == 0,
                  (proc.stdout + proc.stderr).strip().splitlines()[-1]
                  if (proc.stdout + proc.stderr).strip() else
                  "db_init --check rc=%d" % proc.returncode)
        # 10. deployment starts from the restored copy: preflight +
        # dashboard /health against the restored home.
        env = dict(os.environ)
        env["FOREX_AGENT_HOME"] = self.home
        env["HOME"] = self.home
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "credentials.py"),
             "--preflight"], capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            self.step("deployment_starts", False,
                      "credentials --preflight failed: %s"
                      % (proc.stdout + proc.stderr).strip())
            return self
        server = subprocess.Popen(
            [sys.executable, os.path.join(AGT, "status_server.py"),
             "--port", str(self.port), "--home", self.home],
            env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        try:
            healthy = False
            detail = ""
            for _ in range(10):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/health" % self.port,
                            timeout=2) as resp:
                        body = json.load(resp)
                        healthy = body.get("status") in (
                            "ok", "degraded")
                        detail = "dashboard /health=%s" % (
                            body.get("status"))
                        break
                except Exception as exc:
                    detail = "dashboard not answering: %s" % exc
            self.step("deployment_starts", healthy,
                      "preflight ok; " + detail + " (new closes NOT "
                      "proven here — traders run only on the phone)")
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        return self

    def report(self, path=None):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if path is None:
            path = os.path.join(
                self.home, "restore-drill-%s.md"
                % time.strftime("%Y-%m-%d", time.gmtime()))
        result = {"archive": self.archive, "home": self.home,
                  "drilled_at_utc": stamp, "passed": self.passed,
                  "steps": self.steps}
        lines = ["# Restore drill — %s" % stamp, "",
                 "Archive: `%s`" % self.archive,
                 "Restore home (scratch): `%s`" % self.home,
                 "Result: **%s**" % ("PASS" if self.passed else "FAIL"),
                 "",
                 "| step | result | detail |",
                 "|---|---|---|"]
        for s in self.steps:
            lines.append("| %s | %s | %s |" % (
                s["name"], "PASS" if s["passed"] else "FAIL",
                s["detail"]))
        lines += ["",
                  "Note: the drill proves the restored copy boots "
                  "(preflight + dashboard /health). New closes from "
                  "the restored copy are proven only by the on-phone "
                  "drill step (start the deployment, watch for "
                  "closes).",
                  ""]
        with open(path, "w") as fh:
            fh.write("\n".join(lines))
        with open(path + ".json", "w") as fh:
            json.dump(result, fh, indent=2, sort_keys=True)
        return path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    archive = None
    home = None
    expected = None
    report = None
    port = 18766
    force_live = False
    i = 0
    while i < len(argv):
        if argv[i] == "--archive" and i + 1 < len(argv):
            archive = argv[i + 1]
            i += 2
        elif argv[i] == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 2
        elif argv[i] == "--expected" and i + 1 < len(argv):
            expected = json.load(open(argv[i + 1]))
            i += 2
        elif argv[i] == "--report" and i + 1 < len(argv):
            report = argv[i + 1]
            i += 2
        elif argv[i] == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
        elif argv[i] == "--force-live-home":
            force_live = True
            i += 1
        else:
            print("usage: restore_drill.py --archive <tar.gz> --home "
                  "<scratch dir> [--expected <json>] [--report <path>] "
                  "[--port N]")
            return 2
    if not archive or not home:
        print("usage: restore_drill.py --archive <tar.gz> --home "
              "<scratch dir> [--expected <json>] [--report <path>] "
              "[--port N]")
        return 2
    if os.path.abspath(home) == os.path.abspath(LIVE_HOME) \
            and not force_live:
        print("REFUSING: --home is the live home %s; the drill runs "
              "only against a scratch home" % LIVE_HOME)
        return 2
    drill = Drill(archive, home, expected=expected, port=port)
    drill.run()
    path = drill.report(report)
    print("drill report: %s" % path)
    print("restore drill: %s" % ("PASS" if drill.passed else "FAIL"))
    return 0 if drill.passed else 1


if __name__ == "__main__":
    sys.exit(main())
