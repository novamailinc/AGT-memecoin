#!/usr/bin/env python3
"""Tests for the persistent launch archive in dossiers (2026-11-23).

Section 4.1 of the engineering request requires long-term memory of
launches. Dossiers expire after 24h; on TTL eviction the sweep now
archives a compact record to an append-only JSONL that never
expires. The archive is advisory memory: no gate reads it in this
slice, so lane behavior is unchanged.

No network, no live state: temp home dirs throughout. Run:
python3 tests/test_dossier_archive.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.launch_scanner import dossiers

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


class _Ctx:
    """Temp home with dossiers module pointed at it; restores."""

    def __init__(self):
        self.home = tempfile.mkdtemp(prefix="dossier-arch-")
        self._real = {}

    def __enter__(self):
        self._real["HOME"] = dossiers.HOME
        self._real["DIR"] = dossiers.DOSSIER_DIR
        self._real["ARCH"] = dossiers.ARCHIVE_FILE
        self._real["sweep"] = dossiers._last_sweep
        dossiers.HOME = self.home
        dossiers.DOSSIER_DIR = os.path.join(self.home, "dossiers")
        dossiers.ARCHIVE_FILE = os.path.join(
            self.home, "memory", "launch_archive.jsonl")
        dossiers._last_sweep = 0.0
        os.makedirs(dossiers.DOSSIER_DIR, exist_ok=True)
        return self

    def __exit__(self, *exc):
        dossiers.HOME = self._real["HOME"]
        dossiers.DOSSIER_DIR = self._real["DIR"]
        dossiers.ARCHIVE_FILE = self._real["ARCH"]
        dossiers._last_sweep = self._real["sweep"]
        return False

    def write_dossier(self, mint, born_ts, **fields):
        d = {"mint": mint, "born_ts": born_ts}
        d.update(fields)
        with open(os.path.join(dossiers.DOSSIER_DIR, mint + ".json"),
                  "w") as f:
            json.dump(d, f)
        return d


def test_expired_dossier_is_archived_before_eviction():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier(
            "MINT-A", now - 25 * 3600, deployer="DEP-1",
            safety={"ts": now - 25 * 3600,
                    "report": {"unknown": False, "source": "rugcheck"}},
            initial_buy_sol=2.5, market_cap_sol=30.0, migrated=True,
            reserve_samples=[1, 2, 3])
        dossiers._sweep(now)
        check("archive: expired dossier file evicted",
              not os.path.exists(
                  os.path.join(dossiers.DOSSIER_DIR, "MINT-A.json")))
        recs = dossiers.launch_archive(mint="MINT-A")
        check("archive: one record persisted", len(recs) == 1)
        if recs:
            r = recs[0]
            check("archive: record carries deployer + verdict",
                  r.get("deployer") == "DEP-1"
                  and r.get("safety_verdict") == "conclusive"
                  and r.get("safety_source") == "rugcheck")
            check("archive: record carries birth facts",
                  r.get("initial_buy_sol") == 2.5
                  and r.get("market_cap_sol") == 30.0
                  and r.get("migrated") is True
                  and r.get("reserve_samples") == 3)
            check("archive: record carries archive timestamp",
                  r.get("archived_ts") == now)


def test_fresh_dossier_not_archived_or_evicted():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier("MINT-B", now - 3600, deployer="DEP-2")
        dossiers._sweep(now)
        check("archive: fresh dossier kept",
              os.path.exists(
                  os.path.join(dossiers.DOSSIER_DIR, "MINT-B.json")))
        check("archive: nothing archived for fresh dossier",
              dossiers.launch_archive() == [])


def test_unknown_safety_archived_as_unknown():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier(
            "MINT-C", now - 26 * 3600, deployer="DEP-3",
            safety={"ts": now, "report": {"unknown": True}})
        dossiers._sweep(now)
        recs = dossiers.launch_archive(mint="MINT-C")
        check("archive: unknown verdict remembered as unknown",
              len(recs) == 1
              and recs[0].get("safety_verdict") == "unknown")


def test_deployer_filter_and_order():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier("MINT-D", now - 25 * 3600, deployer="DEP-9")
        dossiers._sweep(now)
        dossiers._last_sweep = 0.0
        c.write_dossier("MINT-E", now - 25 * 3600, deployer="DEP-9")
        c.write_dossier("MINT-F", now - 25 * 3600, deployer="DEP-X")
        dossiers._sweep(now + 1)
        mine = dossiers.launch_archive(deployer="DEP-9")
        check("archive: deployer filter returns that deployer's "
              "launches only",
              len(mine) == 2
              and all(r.get("deployer") == "DEP-9" for r in mine))
        check("archive: other deployer filtered out",
              all(r.get("mint") != "MINT-F" for r in mine))
        allr = dossiers.launch_archive()
        check("archive: newest first",
              len(allr) == 3 and allr[0].get("mint") in
              ("MINT-E", "MINT-F"))


def test_archive_never_raises_on_corrupt_state():
    with _Ctx() as c:
        now = time.time()
        # Corrupt dossier file: sweep must survive it.
        with open(os.path.join(dossiers.DOSSIER_DIR, "BAD.json"),
                  "w") as f:
            f.write("{not json")
        dossiers._sweep(now)
        # Archive file corrupt: reader must survive it.
        os.makedirs(os.path.dirname(dossiers.ARCHIVE_FILE),
                    exist_ok=True)
        with open(dossiers.ARCHIVE_FILE, "w") as f:
            f.write("{garbage\n")
        check("archive: corrupt archive reads as empty, no raise",
              dossiers.launch_archive() == [])
        # Missing archive dir entirely.
        os.unlink(dossiers.ARCHIVE_FILE)
        os.rmdir(os.path.dirname(dossiers.ARCHIVE_FILE))
        check("archive: missing archive file reads as empty",
              dossiers.launch_archive(mint="MINT-Z") == [])


def test_schema_version_written_and_enforced():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier("MINT-V", now - 25 * 3600, deployer="DEP-1")
        dossiers._sweep(now)
        with open(dossiers.ARCHIVE_FILE) as f:
            rec = json.loads(f.readline())
        check("memory: record carries schema v1", rec.get("v") == 1)
        # A future-version record is skipped, never misread.
        with open(dossiers.ARCHIVE_FILE, "a") as f:
            f.write(json.dumps({"v": 99, "mint": "MINT-FUTURE",
                                "archived_ts": now}) + "\n")
        check("memory: unknown schema version skipped",
              all(r.get("mint") != "MINT-FUTURE"
                  for r in dossiers.launch_archive()))


def test_launch_memory_conflict_resolution():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier(
            "MINT-X", now - 25 * 3600, deployer="DEP-1",
            safety={"ts": now, "report": {"unknown": False,
                                          "source": "rugcheck"}})
        dossiers._sweep(now)
        dossiers._last_sweep = 0.0
        # Second, disagreeing record for the SAME mint (a rebuild
        # raced the eviction): conflict must surface, never a
        # silent pick.
        c.write_dossier(
            "MINT-X", now - 25 * 3600, deployer="DEP-1",
            safety={"ts": now, "report": {"unknown": True}})
        dossiers._sweep(now + 1)
        mem = dossiers.launch_memory("MINT-X", now=now + 2)
        check("memory: both records kept", mem is not None
              and mem.get("entries") == 2)
        check("memory: disagreement resolves to conflicting",
              mem is not None and mem.get("conflict") is True
              and mem.get("safety_verdict") == "conflicting")


def test_launch_memory_staleness_labels():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier("MINT-S", now - 25 * 3600, deployer="DEP-1")
        dossiers._sweep(now)
        fresh = dossiers.launch_memory("MINT-S", now=now + 60)
        check("memory: recent record is fresh",
              fresh is not None and fresh.get("stale") is False
              and fresh.get("age_s") == 60)
        old = dossiers.launch_memory(
            "MINT-S", now=now + 31 * 86400)
        check("memory: old record labeled stale, never silent",
              old is not None and old.get("stale") is True)
        check("memory: unknown mint resolves to None",
              dossiers.launch_memory("MINT-NONE") is None)


def test_intel_fields_consume_archive():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier(
            "MINT-H1", now - 25 * 3600, deployer="DEP-H",
            safety={"ts": now, "report": {"unknown": False,
                                          "source": "rugcheck"}})
        c.write_dossier("MINT-H2", now - 25 * 3600, deployer="DEP-H",
                        safety={"ts": now,
                                "report": {"unknown": True}})
        c.write_dossier("MINT-H3", now - 25 * 3600, deployer="DEP-Z")
        dossiers._sweep(now)
        intel = dossiers._intel_fields("DEP-H")
        hist = (intel or {}).get("deployer_history") or {}
        check("memory: intel consumes durable deployer history",
              hist.get("archived_launches") == 2
              and hist.get("conclusive") == 1
              and hist.get("unknown") == 1)
        other = dossiers._intel_fields("DEP-Z") or {}
        check("memory: other deployer's history is separate",
              (other.get("deployer_history") or {}).get(
                  "archived_launches") == 1)


def main():
    test_expired_dossier_is_archived_before_eviction()
    test_fresh_dossier_not_archived_or_evicted()
    test_unknown_safety_archived_as_unknown()
    test_deployer_filter_and_order()
    test_archive_never_raises_on_corrupt_state()
    test_schema_version_written_and_enforced()
    test_launch_memory_conflict_resolution()
    test_launch_memory_staleness_labels()
    test_intel_fields_consume_archive()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL DOSSIER-ARCHIVE TESTS PASSED")


if __name__ == "__main__":
    main()
