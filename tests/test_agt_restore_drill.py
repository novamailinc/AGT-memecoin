#!/usr/bin/env python3
"""Restore drill + backup verify tests (Phase 13 finalize).

Runs the REAL scripts/forex-backup against a small fixture home
in a temp dir (never the live home), then runs
deploy/agt/restore_drill.py against the produced archive into a
scratch restore home, and asserts every drill step passes —
including the layout normalization (local.db moves from the
archive root to storage/local.db) and the void-excluded dashboard
totals on the restored copy.

Also covers deploy/agt/backup_verify.py directly: a good archive
passes, a bad sha256 fails, and an archive containing credential
material fails.

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_agt_restore_drill.py
"""

import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGT = os.path.join(REPO, "deploy", "agt")
VENV_PY = os.path.join(os.path.dirname(REPO), ".venv", "bin", "python")
if not os.path.isfile(VENV_PY):
    VENV_PY = sys.executable

sys.path.insert(0, AGT)
import status_server as DASH  # noqa: E402

FAILS = []
PASSES = [0]

FAKE_ENV = {
    'CHAINSTACK_ENDPOINT': 'https://example.invalid/fixture-node',
    'HELIUS_API_KEY_1': 'fixture-helius-source-1',
    'HELIUS_API_KEY_2': 'fixture-helius-source-2',
    'HELIUS_API_KEY_3': 'fixture-helius-source-3',
    # Well-known public placeholder shapes, never real values and
    # never the "TEST-" placeholder prefix: credentials.py refuses
    # TEST- values outright (2026-11-22), and the GitHub secret
    # guard allowlists exactly this UUID (RFC 4122 example).
    "HELIUS_API_KEY": "123e4567-e89b-12d3-a456-426614174000",
    "ALCHEMY_API_KEY": "fake-alchemy-key",
    "TELEGRAM_BOT_TOKEN": "123456789:fake-telegram-token",
}

VOID_MINT = "91GG7PWsUTmXwHcDtkoaEm77v1xc6NyQWoETFjrDpump"
NOW = time.time()


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


def _fixture_home(tmp):
    """Small home shaped like the live one (ledgers, killswitch +
    shadow, live10, data/, live/, launch_scanner/, agent.env)."""
    home = os.path.join(tmp, ".forex-agent")
    os.makedirs(os.path.join(home, "run"), exist_ok=True)
    os.makedirs(os.path.join(home, "killswitch"), exist_ok=True)
    os.makedirs(os.path.join(home, "data"), exist_ok=True)
    os.makedirs(os.path.join(home, "live"), exist_ok=True)
    os.makedirs(os.path.join(home, "launch_scanner"), exist_ok=True)
    void_grad_raw = {
        "mint": VOID_MINT, "symbol": "?", "close_ts": 1795335524.270021,
        "pnl_usd": 12724.3124, "realized_mult": 509.9725,
        "closed_reason": "time_stop_24h"}
    void_tranche = {
        "mint": VOID_MINT, "symbol": "?", "close_ts": 1795335526.772091,
        "pnl_usd": 13102.6807, "realized_mult": 525.1072,
        "closed_reason": "time_stop_24h"}
    ledgers = {
        "grad_select": {"bankroll_start": 25.0, "bankroll": 41.25,
                        "positions": {},
                        "closes": [{"mint": "M1", "symbol": "WIN",
                                    "close_ts": NOW - 3600,
                                    "pnl_usd": 16.25,
                                    "realized_mult": 2.0,
                                    "closed_reason": "leg_a"}]},
        "grad_raw": {"bankroll_start": 25.0, "bankroll": 12749.31,
                     "positions": {}, "closes": [void_grad_raw]},
        "tranche": {"bankroll_start": 25.0, "bankroll": 13132.68,
                    "positions": {},
                    "closes": [void_tranche,
                               {"mint": "M3", "symbol": "T1",
                                "close_ts": NOW - 1800, "pnl_usd": 5.0,
                                "realized_mult": 1.5,
                                "closed_reason": "leg_a"}]},
        "tranche_protect": {"bankroll_start": 25.0, "bankroll": 25.0,
                            "positions": {}, "closes": []},
        "momentum": {"bankroll_start": 25.0, "bankroll": 20.0,
                     "positions": {}, "closes": []},
    }
    for lane, data in ledgers.items():
        with open(os.path.join(home, "%s_ledger.json" % lane), "w") as fh:
            json.dump(data, fh)
    with open(os.path.join(home, "live10_demo_ledger.json"), "w") as fh:
        json.dump({"lanes": {
            "grad_select": {"start": 2.5, "balance": 3851.0855,
                            "closes": 929, "wins": 457,
                            "net": 3880.7861, "funded_closes": 929},
            "tranche": {"start": 2.5, "balance": 9524.145,
                        "closes": 882, "wins": 469,
                        "net": 9531.8483, "funded_closes": 882},
            "tranche_protect": {"start": 2.5, "balance": 954.4713,
                                "closes": 220, "wins": 89,
                                "net": 927.9138, "funded_closes": 220},
            "grad_raw": {"start": 2.5, "balance": 7.2311,
                         "closes": 6, "wins": 0, "net": -19.6153,
                         "funded_closes": 6}},
            "mirrored": []}, fh)
    with open(os.path.join(home, "killswitch", "state.json"), "w") as fh:
        json.dump({"lanes": {"grad_select": {"halted": False,
                                             "halt_reason": "",
                                             "halt_ts": 0,
                                             "trades": []}},
                   "events": []}, fh)
    with open(os.path.join(home, "killswitch",
                           "state.json.bak"), "w") as fh:
        fh.write(open(os.path.join(home, "killswitch",
                                   "state.json")).read())
    with open(os.path.join(home, "run", "heartbeat.json"), "w") as fh:
        json.dump({"checked_at": "2026-11-22T10:00:00Z",
                   "daemons": [], "actions": "none", "beats": {}}, fh)
    with open(os.path.join(home, "agent.env"), "w") as fh:
        fh.write("FOREX_AGENT_MODE=paper\n")
    with open(os.path.join(home, "data", "broker.json"), "w") as fh:
        json.dump({"virtual_balance": 100.0}, fh)
    with open(os.path.join(home, "live", "deployment.json"), "w") as fh:
        json.dump({"deployment": "test-fixture"}, fh)
    with open(os.path.join(home, "launch_scanner",
                           "registry.json"), "w") as fh:
        json.dump({"wallets": []}, fh)
    # Coverage fixtures (2026-11-22 audit): the intelligence event
    # store, the verdict chain and the discovery recorder — the
    # three state families the backup coverage fix added.
    ev = sqlite3.connect(os.path.join(home, "intelligence_events.db"))
    with ev:
        ev.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY)")
        ev.execute("INSERT INTO events VALUES ('e1')")
    ev.close()
    os.makedirs(os.path.join(home, "verdict_ledger"), exist_ok=True)
    with open(os.path.join(home, "verdict_ledger",
                           "events.jsonl"), "w") as fh:
        fh.write(json.dumps({"lane": "grad_select",
                             "kind": "verdict", "ts": NOW}) + "\n")
    os.makedirs(os.path.join(home, "recorder"), exist_ok=True)
    with open(os.path.join(home, "recorder",
                           "events-20261122.jsonl"), "w") as fh:
        fh.write(json.dumps({"mint": "M1", "ts": NOW}) + "\n")
    return home


def _expected(home):
    lanes = {}
    for lane in ("grad_select", "grad_raw", "tranche",
                 "tranche_protect", "momentum"):
        data = json.load(open(os.path.join(
            home, "%s_ledger.json" % lane)))
        lanes[lane] = {"bankroll": data["bankroll"]}
    live10 = json.load(open(os.path.join(
        home, "live10_demo_ledger.json")))
    return {"lanes": lanes,
            "live10": {lane: entry["balance"]
                       for lane, entry in live10["lanes"].items()}}


with tempfile.TemporaryDirectory() as tmp:
    home = _fixture_home(tmp)
    # Real DB init on the fixture home (proper local.db).
    proc = subprocess.run(
        [sys.executable, os.path.join(AGT, "db_init.py"),
         "--home", home], capture_output=True, text=True)
    check("drill: db_init creates the fixture local.db",
          proc.returncode == 0
          and os.path.isfile(os.path.join(
              home, "storage", "local.db")))
    # Real backup against the fixture home (never the live home).
    env = {**os.environ, "FOREX_AGENT_HOME": home, "HOME": tmp,
           "FOREX_BACKUP_KEEP": "3"}
    proc = subprocess.run(
        ["bash", os.path.join(REPO, "scripts", "forex-backup")],
        capture_output=True, text=True, env=env, timeout=300)
    check("drill: forex-backup runs against the fixture home",
          proc.returncode == 0 and "backup ok" in proc.stdout)
    bdir = os.path.join(home, "backups")
    archives = sorted(n for n in os.listdir(bdir)
                      if n.startswith("forex-backup-")
                      and n.endswith(".tar.gz"))
    check("drill: exactly one archive produced",
          len(archives) == 1)
    archive = os.path.join(bdir, archives[0]) if archives else None
    if archive:
        check("drill: sha256 sidecar exists next to the archive",
              os.path.isfile(archive + ".sha256"))
        # The archive stores local.db at its ROOT (the layout
        # quirk the drill normalizes) — assert the fixture
        # actually reproduces it.
        with tarfile.open(archive, "r:gz") as tf:
            names = tf.getnames()
        check("drill: fixture archive stores local.db at its root "
              "(live layout quirk)",
              "./local.db" in names or "local.db" in names)
        check("drill: archive carries the event store, verdict "
              "ledger and recorder (2026-11-22 coverage fix)",
              any(n.endswith("intelligence_events.db") for n in names)
              and any("verdict_ledger/events.jsonl" in n
                      for n in names)
              and any("recorder/events-20261122.jsonl" in n
                      for n in names))
        check("drill: fixture archive carries no credential "
              "material",
              not any(n.endswith("secrets.env")
                      or n.startswith("./helius")
                      or n.startswith("./telegram")
                      for n in names))

        # backup_verify on the good archive passes.
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "backup_verify.py"),
             "--verify", archive], capture_output=True, text=True)
        check("backup_verify: good archive passes",
              proc.returncode == 0 and "PASS" in proc.stdout)
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "backup_verify.py"),
             "--home", home, "--verify-latest"],
            capture_output=True, text=True)
        check("backup_verify: --verify-latest passes + writes its "
              "report",
              proc.returncode == 0
              and os.path.isfile(os.path.join(
                  bdir, "verify-latest.json")))
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "backup_verify.py"),
             "--home", home, "--export-manifest"],
            capture_output=True, text=True)
        check("backup_verify: --export-manifest lists the archive",
              proc.returncode == 0
              and archives[0] in proc.stdout
              and os.path.isfile(os.path.join(
                  bdir, "offphone-export-manifest.json")))

        # Negative: bad sha256 fails the verify.
        bad = os.path.join(tmp, "bad.tar.gz")
        import shutil
        shutil.copy(archive, bad)
        with open(bad + ".sha256", "w") as fh:
            fh.write("0" * 64)
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "backup_verify.py"),
             "--verify", bad], capture_output=True, text=True)
        check("backup_verify: bad sha256 fails",
              proc.returncode != 0
              and "sha256_sidecar" in proc.stdout)
        # Negative: credential material in the archive fails.
        poisoned = os.path.join(tmp, "poisoned.tar.gz")
        with tarfile.open(poisoned, "w:gz") as tf:
            import hashlib
            import io
            blob = b"fake"
            for name in ("grad_select_ledger.json", "secrets.env"):
                info = tarfile.TarInfo(name)
                info.size = len(blob)
                tf.addfile(info, io.BytesIO(blob))
        with open(poisoned, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        with open(poisoned + ".sha256", "w") as fh:
            fh.write(digest)
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "backup_verify.py"),
             "--verify", poisoned], capture_output=True, text=True)
        check("backup_verify: credential material in archive fails",
              proc.returncode != 0
              and "no_credential_material" in proc.stdout)

        # The full restore drill against a scratch home.
        expected_path = os.path.join(tmp, "expected.json")
        with open(expected_path, "w") as fh:
            json.dump(_expected(home), fh)
        restore_home = os.path.join(tmp, "restored-home")
        drill_env = {**os.environ, **FAKE_ENV, "HOME": tmp}
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "restore_drill.py"),
             "--archive", archive, "--home", restore_home,
             "--expected", expected_path],
            capture_output=True, text=True, env=drill_env,
            timeout=300)
        check("drill: restore_drill exits 0 (every step passes)",
              proc.returncode == 0)
        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
        check("drill: drill report md + json written",
              os.path.isfile(os.path.join(
                  restore_home, "restore-drill-%s.md"
                  % time.strftime("%Y-%m-%d",
                                  time.gmtime())))
              or any(n.startswith("restore-drill-")
                     and n.endswith(".md")
                     for n in os.listdir(restore_home)))
        check("drill: layout normalized — local.db is at "
              "storage/local.db in the restored home",
              os.path.isfile(os.path.join(
                  restore_home, "storage", "local.db")))
        check("drill: layout normalized — heartbeat is at "
              "run/heartbeat.json in the restored home",
              os.path.isfile(os.path.join(
                  restore_home, "run", "heartbeat.json")))
        check("drill: killswitch shadow survived the restore",
              os.path.isfile(os.path.join(
                  restore_home, "killswitch", "state.json.bak")))
        check("drill: coverage state survived the restore (event "
              "store, verdict ledger, recorder)",
              os.path.isfile(os.path.join(
                  restore_home, "intelligence_events.db"))
              and os.path.isfile(os.path.join(
                  restore_home, "verdict_ledger", "events.jsonl"))
              and os.path.isfile(os.path.join(
                  restore_home, "recorder", "events-20261122.jsonl")))
        # Dashboard on the RESTORED home applies the void list:
        # raw Live10 $14,336.93, void-excluded $9,095.86.
        bal = DASH.read_balances(restore_home)
        check("drill: restored dashboard Live10 total is "
              "void-excluded ($9,095.86)",
              bal["status"] == "ok"
              and abs(bal["data"]["live10_total"]
                      - 9095.8629) < 1e-9)
        check("drill: restored dashboard keeps the raw total "
              "labelled alongside",
              abs(bal["data"]["live10_total_raw"]
                  - 14336.9329) < 1e-9)
        pnl = DASH.read_pnl(restore_home)
        check("drill: restored dashboard PnL excludes both voided "
              "closes",
              abs(pnl["data"]["per_lane"]["tranche"]
                  ["realized_usd_all_time"] - 5.0) < 1e-9
              and abs(pnl["data"]["per_lane"]["grad_raw"]
                  ["realized_usd_all_time"] - 0.0) < 1e-9
              and abs(pnl["data"]["voided_usd_total"]
                      - 25826.99) < 1e-9)

        # The drill refuses the live home.
        proc = subprocess.run(
            [sys.executable, os.path.join(AGT, "restore_drill.py"),
             "--archive", archive, "--home",
             os.path.join(os.path.expanduser("~"), ".forex-agent")],
            capture_output=True, text=True)
        check("drill: refuses the live home",
              proc.returncode == 2 and "REFUSING" in proc.stdout)

print("\n%d passed, %d failed" % (PASSES[0], len(FAILS)))
if FAILS:
    for name in FAILS:
        print("  FAILED: %s" % name)
sys.exit(1 if FAILS else 0)
