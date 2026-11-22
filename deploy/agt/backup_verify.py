"""Backup verification + off-phone export manifest (Phase 13 finalize).

Two jobs, one module (stdlib only, read-only except for the reports
it writes under $FOREX_AGENT_HOME/backups/):

1. ``--verify-latest`` / ``--verify <archive>`` — mechanically
   verify a forex-backup archive, step by step, and record the
   result in ``backups/verify-latest.json``:
     a. the archive exists and its sha256 sidecar matches;
     b. the tarball opens and lists;
     c. NO credential material is inside (secrets.env, telegram/,
        helius/, live/keypair.json, *.key, the credential-bridge
        files helius/api_key, telegram/bot_token, ...);
     d. every ``*_ledger.json``, ``live10_demo_ledger.json`` and
        ``killswitch/state.json`` parses as JSON;
     e. the killswitch shadow ``killswitch/state.json.bak`` exists;
     f. ``local.db`` is present, non-empty, and passes
        ``PRAGMA quick_check``.
   Any failure fails the whole verify (exit 1) and is named in the
   report. The hourly backup job runs this right after the backup
   step, so a bad archive is loud within the hour instead of being
   discovered at restore time.

2. ``--export-manifest`` — write ``backups/offphone-export-
   manifest.json`` listing every archive with its sha256 + size,
   the file the operator copies off the phone by hand. There is
   still no automated off-phone copy on AGT (no Drive credential
   in the AGT vault, no AGT snapshot API wired); the manifest +
   the documented manual export in backup_recovery.md is the
   bridge until a phone-side solution exists. The manifest is
   honest about that: it is a copy list, not a backup.

Usage:
  backup_verify.py [--home DIR] --verify-latest
  backup_verify.py --verify <archive.tar.gz>
  backup_verify.py [--home DIR] --export-manifest
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time

# Archive members that must NEVER appear (credential material).
FORBIDDEN_BASENAMES = {"secrets.env", "keypair.json", "api_key",
                       "api_key_discovery", "bot_token", "endpoint"}
FORBIDDEN_SUFFIXES = (".key",)
FORBIDDEN_PREFIXES = ("telegram/", "helius/", "secrets")


def home_dir():
    return os.environ.get("FOREX_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".forex-agent")


def _is_forbidden(member):
    name = member.lstrip("./")
    base = os.path.basename(name)
    if base in FORBIDDEN_BASENAMES:
        return True
    if any(name.startswith(p) for p in FORBIDDEN_PREFIXES):
        return True
    if any(base.endswith(s) for s in FORBIDDEN_SUFFIXES):
        return True
    return False


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(archive):
    """Verify one archive. Returns {"archive", "passed", "steps"}.

    steps: list of {"name", "passed", "detail"}. Extraction goes
    to a temp dir that is removed afterwards; nothing is written
    to any live home.
    """
    steps = []

    def step(name, passed, detail=""):
        steps.append({"name": name, "passed": bool(passed),
                      "detail": detail})
        return passed

    if not os.path.isfile(archive):
        step("archive_exists", False, "no such file: %s" % archive)
        return {"archive": archive, "passed": False, "steps": steps}
    step("archive_exists", True, archive)

    sidecar = archive + ".sha256"
    if not os.path.isfile(sidecar):
        step("sha256_sidecar", False, "missing sidecar: %s" % sidecar)
    else:
        try:
            expected = open(sidecar).read().strip().split()[0]
        except OSError as exc:
            expected = ""
            step("sha256_sidecar", False, "unreadable sidecar: %s" % exc)
        if expected:
            actual = _sha256(archive)
            step("sha256_sidecar", actual == expected,
                 "sidecar %s, actual %s" % (expected[:16], actual[:16]))
    if not all(s["passed"] for s in steps):
        return {"archive": archive, "passed": False, "steps": steps}

    tmp = tempfile.mkdtemp(prefix="agt-backup-verify-")
    try:
        try:
            with tarfile.open(archive, "r:gz") as tf:
                members = tf.getnames()
                tf.extractall(tmp)
        except (tarfile.TarError, OSError, ValueError) as exc:
            step("tarball_opens", False, "cannot extract: %s" % exc)
            return {"archive": archive, "passed": False, "steps": steps}
        step("tarball_opens", True, "%d members" % len(members))

        bad = [m for m in members if _is_forbidden(m)]
        step("no_credential_material", not bad,
             "forbidden members: %s" % ", ".join(bad) if bad
             else "no forbidden members")

        ledgers = [m for m in members
                   if m.endswith("_ledger.json")
                   or m.endswith("live10_demo_ledger.json")]
        ks = [m for m in members
              if m.endswith("killswitch/state.json")]
        json_files = ledgers + ks
        bad_json = []
        for rel in json_files:
            p = os.path.join(tmp, rel)
            try:
                with open(p) as fh:
                    json.load(fh)
            except (OSError, ValueError) as exc:
                bad_json.append("%s: %s" % (rel, exc))
        step("ledgers_parse", not bad_json,
             "%d JSON state files checked%s" % (
                 len(json_files),
                 ("; unparsable: " + "; ".join(bad_json))
                 if bad_json else ""))
        if not json_files:
            steps[-1]["passed"] = False
            steps[-1]["detail"] = "no ledger files in archive at all"

        shadow = os.path.join(tmp, "killswitch", "state.json.bak")
        step("killswitch_shadow_present", os.path.isfile(shadow),
             "killswitch/state.json.bak present" if os.path.isfile(shadow)
             else "killswitch shadow missing — a corrupt primary "
                  "could not be restored from the shadow")

        db = os.path.join(tmp, "local.db")
        if not os.path.isfile(db) or os.path.getsize(db) == 0:
            step("local_db_integrity", False,
                 "local.db missing or empty in archive")
        else:
            try:
                conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                                       timeout=10)
                try:
                    row = conn.execute("PRAGMA quick_check").fetchone()
                    ok = bool(row) and str(row[0]).lower() == "ok"
                    step("local_db_integrity", ok,
                         "quick_check=%s (%d bytes)" % (
                             row[0] if row else None,
                             os.path.getsize(db)))
                finally:
                    conn.close()
            except sqlite3.Error as exc:
                step("local_db_integrity", False,
                     "sqlite error: %s" % exc)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    passed = all(s["passed"] for s in steps)
    return {"archive": archive, "passed": passed, "steps": steps}


def latest_archive(home=None):
    home = home or home_dir()
    bdir = os.path.join(home, "backups")
    try:
        names = [n for n in os.listdir(bdir)
                 if n.startswith("forex-backup-")
                 and n.endswith(".tar.gz")]
    except OSError:
        return None
    if not names:
        return None
    names.sort()
    return os.path.join(bdir, names[-1])


def verify_latest(home=None):
    """Verify the newest archive; write backups/verify-latest.json.

    Returns the verify dict. The report is the healthcheck's
    source for the backup-verify check."""
    home = home or home_dir()
    archive = latest_archive(home)
    if archive is None:
        result = {"archive": None, "passed": False,
                  "steps": [{"name": "archive_exists", "passed": False,
                             "detail": "no forex-backup-*.tar.gz in %s"
                             % os.path.join(home, "backups")}]}
    else:
        result = verify_archive(archive)
    result["verified_at_utc"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    bdir = os.path.join(home, "backups")
    os.makedirs(bdir, exist_ok=True)
    report = os.path.join(bdir, "verify-latest.json")
    tmp = "%s.tmp.%d" % (report, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    os.replace(tmp, report)
    result["report"] = report
    return result


def export_manifest(home=None):
    """Write backups/offphone-export-manifest.json: every archive
    with sha256 + size, for the manual off-phone copy.

    Returns (manifest_dict, path). The manifest states plainly
    that the copy itself is manual — no automated off-phone
    destination exists on AGT yet."""
    home = home or home_dir()
    bdir = os.path.join(home, "backups")
    entries = []
    try:
        names = sorted(n for n in os.listdir(bdir)
                       if n.startswith("forex-backup-")
                       and n.endswith(".tar.gz"))
    except OSError:
        names = []
    for name in names:
        path = os.path.join(bdir, name)
        sha = None
        sidecar = path + ".sha256"
        if os.path.isfile(sidecar):
            try:
                sha = open(sidecar).read().strip().split()[0]
            except OSError:
                sha = None
        entries.append({"file": name,
                        "size_bytes": os.path.getsize(path),
                        "sha256": sha,
                        "mtime_utc": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(os.path.getmtime(path)))})
    manifest = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
        "home": home,
        "off_phone_copy": "MANUAL — no automated off-phone "
                          "destination exists on AGT yet (see "
                          "deploy/agt/backup_recovery.md). Copy the "
                          "archives listed here off the phone "
                          "(AGT file download, adb pull, or USB) "
                          "and verify each sha256 after the copy.",
        "archives": entries,
    }
    os.makedirs(bdir, exist_ok=True)
    path = os.path.join(bdir, "offphone-export-manifest.json")
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)
    return manifest, path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    home = home_dir()
    i = 0
    mode = None
    archive = None
    while i < len(argv):
        if argv[i] == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 2
        elif argv[i] == "--verify-latest":
            mode = "verify-latest"
            i += 1
        elif argv[i] == "--verify" and i + 1 < len(argv):
            mode = "verify"
            archive = argv[i + 1]
            i += 2
        elif argv[i] == "--export-manifest":
            mode = "export-manifest"
            i += 1
        else:
            print("usage: backup_verify.py [--home DIR] "
                  "[--verify-latest | --verify <archive> | "
                  "--export-manifest]")
            return 2
    if mode == "verify-latest":
        result = verify_latest(home)
    elif mode == "verify":
        result = verify_archive(archive)
        result["verified_at_utc"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    elif mode == "export-manifest":
        manifest, path = export_manifest(home)
        print("export manifest: %s (%d archives)"
              % (path, len(manifest["archives"])))
        for entry in manifest["archives"]:
            print("  %s %d bytes sha256=%s"
                  % (entry["file"], entry["size_bytes"],
                     (entry["sha256"] or "MISSING SIDECAR")[:16]))
        return 0
    else:
        print("usage: backup_verify.py [--home DIR] "
              "[--verify-latest | --verify <archive> | "
              "--export-manifest]")
        return 2
    for step in result["steps"]:
        print("%s %s: %s" % ("PASS" if step["passed"] else "FAIL",
                             step["name"], step["detail"]))
    print("backup verify: %s (%s)"
          % ("PASS" if result["passed"] else "FAIL",
             result.get("archive") or "no archive"))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
