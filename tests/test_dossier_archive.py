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


def test_eviction_suppressed_when_archive_write_fails():
    """A failed archive write must NOT still unlink the dossier:
    losing the memory is worse than a stale dossier."""
    with _Ctx() as c:
        now = time.time()
        c.write_dossier("MINT-NODEL", now - 25 * 3600,
                        deployer="DEP-1")
        # Make the archive unwritable: its parent path is a FILE,
        # so makedirs/open fail and _archive_append returns False.
        blocker = os.path.join(c.home, "memory")
        with open(blocker, "w") as f:
            f.write("not a dir")
        dossiers._sweep(now)
        check("durability: failed archive keeps the dossier",
              os.path.exists(os.path.join(dossiers.DOSSIER_DIR,
                                          "MINT-NODEL.json")))


def test_archive_status_distinguishes_empty_from_unavailable():
    with _Ctx() as c:
        st = dossiers.archive_status()
        check("status: missing archive is available zero-history",
              st.get("available") is True
              and st.get("records") == 0)
        # Present but unreadable file -> unavailable, never "zero".
        os.makedirs(os.path.dirname(dossiers.ARCHIVE_FILE),
                    exist_ok=True)
        with open(dossiers.ARCHIVE_FILE, "w") as f:
            f.write(json.dumps({"v": 1, "mint": "M1"}) + "\n")
        os.chmod(dossiers.ARCHIVE_FILE, 0o000)
        try:
            st2 = dossiers.archive_status()
            check("status: unreadable archive is UNAVAILABLE",
                  st2.get("available") is False)
            mem = dossiers.launch_memory("M1")
            check("status: launch_memory says unavailable, not None",
                  isinstance(mem, dict)
                  and mem.get("unavailable") is True)
        finally:
            os.chmod(dossiers.ARCHIVE_FILE, 0o600)


def test_outcome_linkage_via_verdict_ledger_close():
    with _Ctx() as c:
        os.environ["FOREX_AGENT_HOME"] = c.home
        try:
            from daemon.launch_scanner import verdict_ledger
            ok = verdict_ledger.append(
                "grad", "close",
                {"mint": "MINT-OC", "pnl_usd": 1.5,
                 "realized_mult": 2.0, "closed_reason": "trail"})
            check("outcome: verdict-ledger close lands in archive",
                  ok is True)
            outs = dossiers.outcome_records(mint="MINT-OC")
            check("outcome: one outcome record with fields",
                  len(outs) == 1
                  and outs[0].get("lane") == "grad"
                  and outs[0].get("realized_mult") == 2.0
                  and outs[0].get("closed_reason") == "trail")
            check("outcome: simulated provenance travels with the "
                  "record, never masquerades as verified",
                  outs[0].get("provenance") == "simulated"
                  and outs[0].get("qualified") is False)
            mem = dossiers.launch_memory("MINT-OC")
            check("outcome: launch_memory joins outcomes + lanes",
                  mem is not None
                  and len(mem.get("outcomes") or []) == 1
                  and mem.get("resolved_lanes") == ["grad"])
            # A non-close kind does NOT create an outcome.
            verdict_ledger.append("grad", "verdict", {"closes": 1})
            check("outcome: non-close kinds are not outcomes",
                  len(dossiers.outcome_records(mint="MINT-OC"))
                  == 1)
        finally:
            del os.environ["FOREX_AGENT_HOME"]


def test_compaction_bounds_size_and_keeps_resolved_state():
    with _Ctx() as c:
        now = time.time()
        real_cap = dossiers.ARCHIVE_MAX_BYTES
        dossiers.ARCHIVE_MAX_BYTES = 2000  # tiny cap for the test
        try:
            for i in range(60):
                dossiers.record_outcome(
                    "MINT-C%d" % (i % 3), "grad", ts=now + i,
                    pnl_usd=float(i), realized_mult=1.0 + i * 0.01,
                    closed_reason="trail")
            for i in range(29):
                c.write_dossier("MINT-C%d" % (i % 3),
                                now - (25 + i) * 3600,
                                deployer="DEP-%d" % i)
                dossiers._last_sweep = 0.0
                dossiers._sweep(now + i + 100)
            # The kept set (3 launches + 60 outcomes + anchor)
            # is larger than the tiny test cap, so every sweep
            # past the cap rewrites to the SAME bounded kept set
            # instead of growing: two consecutive sweeps produce
            # the same size. (With the production 8 MB cap and
            # real volumes the kept set fits under the cap; the
            # cap then bounds total size outright.)
            c.write_dossier("MINT-C0", now - 60 * 3600,
                            deployer="DEP-last")
            dossiers._last_sweep = 0.0
            dossiers._sweep(now + 5000)
            size_a = os.path.getsize(dossiers.ARCHIVE_FILE)
            dossiers._last_sweep = 0.0
            dossiers._sweep(now + 6000)
            size_b = os.path.getsize(dossiers.ARCHIVE_FILE)
            check("compaction: repeated sweeps hold a bounded "
                  "steady state, never regrow",
                  size_a == size_b)
            with open(dossiers.ARCHIVE_FILE) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            anchors = [l for l in lines
                       if l.get("record_type")
                       == "compaction_anchor"]
            check("compaction: anchor names the compaction",
                  len(anchors) >= 1)
            kept = [l for l in lines
                    if l.get("record_type", "launch") == "launch"]
            mints = {l.get("mint") for l in kept}
            check("compaction: latest launch per mint survives",
                  mints == {"MINT-C0", "MINT-C1", "MINT-C2"})
            kept_outcomes = [l for l in lines
                             if l.get("record_type") == "outcome"]
            check("compaction: all outcomes survive",
                  len(kept_outcomes) == 60)
            # And it stays bounded: a second sweep past the cap
            # re-compacts rather than growing back.
            size1 = os.path.getsize(dossiers.ARCHIVE_FILE)
            dossiers._last_sweep = 0.0
            dossiers._sweep(now + 1000)
            check("compaction: steady state does not regrow",
                  os.path.getsize(dossiers.ARCHIVE_FILE)
                  <= size1)
        finally:
            dossiers.ARCHIVE_MAX_BYTES = real_cap


def test_concurrent_appends_never_interleave():
    import threading
    with _Ctx() as c:
        now = time.time()

        def worker(n):
            for i in range(25):
                dossiers.record_outcome(
                    "MINT-T", "lane%d" % n, ts=now + i,
                    realized_mult=1.0, closed_reason="t")
        threads = [threading.Thread(target=worker, args=(n,))
                   for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(dossiers.ARCHIVE_FILE) as f:
            lines = [l for l in f if l.strip()]
        ok = 0
        for l in lines:
            try:
                json.loads(l)
                ok += 1
            except Exception:
                pass
        check("concurrency: every appended line is intact",
              ok == 100 and len(lines) == 100)


def test_missing_archive_with_existing_history_is_unavailable():
    with _Ctx() as c:
        # The bot HAS closed trades before (verdict ledger exists)
        # but the archive is gone: lost history, not first use.
        vdir = os.path.join(c.home, "verdict_ledger")
        os.makedirs(vdir, exist_ok=True)
        with open(os.path.join(vdir, "events.jsonl"), "w") as f:
            f.write(json.dumps({"seq": 1}) + "\n")
        st = dossiers.archive_status()
        check("status: missing archive + existing ledger = lost "
              "history, unavailable",
              st.get("available") is False
              and st.get("error")
              == "archive_missing_with_existing_history")


def test_torn_tail_marks_history_partial():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier("MINT-P", now - 25 * 3600, deployer="DEP-1",
                        safety={"ts": now,
                                "report": {"unknown": False,
                                           "source": "rugcheck"}})
        dossiers._sweep(now)
        # Simulate a crash mid-append: a torn final line.
        with open(dossiers.ARCHIVE_FILE, "a") as f:
            f.write('{"v": 1, "mint": "TORN')
        st = dossiers.archive_status()
        check("partial: torn line counted, archive still readable",
              st.get("available") is True
              and st.get("unreadable_lines") == 1)
        mem = dossiers.launch_memory("MINT-P", now=now + 5)
        check("partial: launch_memory labels partial history",
              mem is not None and mem.get("partial") is True)


def test_compaction_never_discards_concurrent_append():
    import threading
    with _Ctx() as c:
        now = time.time()
        real_cap = dossiers.ARCHIVE_MAX_BYTES
        dossiers.ARCHIVE_MAX_BYTES = 2000
        try:
            for i in range(30):
                dossiers.record_outcome("MINT-R", "grad", ts=now + i,
                                        realized_mult=1.0,
                                        closed_reason="t")
            # Hold the write lock, start a racing append (it
            # blocks), then run compaction in a second thread
            # (also blocks). Release: both must serialize and the
            # appended record must SURVIVE the rename.
            lock = dossiers._archive_write_lock()
            lock.__enter__()
            results = {}

            def racer():
                results["appended"] = dossiers.record_outcome(
                    "MINT-RACER", "grad", ts=now + 999,
                    realized_mult=2.0, closed_reason="race")

            def compactor():
                dossiers._maybe_compact_archive(now + 1000)
                results["compacted"] = True

            t1 = threading.Thread(target=racer)
            t2 = threading.Thread(target=compactor)
            t1.start()
            t2.start()
            time.sleep(0.3)
            lock.__exit__(None, None, None)
            t1.join(timeout=30)
            t2.join(timeout=30)
            check("race: racing append and compaction both finished",
                  results.get("appended") is True
                  and results.get("compacted") is True)
            survivors = [r.get("mint") for r in
                         dossiers.outcome_records()]
            check("race: racing append survived compaction",
                  "MINT-RACER" in survivors)
        finally:
            dossiers.ARCHIVE_MAX_BYTES = real_cap


def test_cross_process_appends_never_interleave():
    """Real separate PROCESSES (threads are not cross-process
    proof): four children append concurrently; every line must
    stay intact."""
    import subprocess
    with _Ctx() as c:
        child = (
            "import sys, time;"
            "sys.path.insert(0, %r);"
            "from daemon.launch_scanner import dossiers;"
            "dossiers.ARCHIVE_FILE = %r;"
            "[dossiers.record_outcome('MINT-X', 'p%%d' %% int(sys.argv[1]),"
            " ts=%f + i, realized_mult=1.0, closed_reason='x')"
            " for i in range(25)]"
            % (os.path.join(os.path.dirname(__file__), ".."),
               dossiers.ARCHIVE_FILE, time.time()))
        procs = [subprocess.Popen(
            [sys.executable, "-c", child, str(n)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for n in range(4)]
        for p in procs:
            p.wait(timeout=60)
        with open(dossiers.ARCHIVE_FILE) as f:
            lines = [l for l in f if l.strip()]
        ok = 0
        for l in lines:
            try:
                json.loads(l)
                ok += 1
            except Exception:
                pass
        check("cross-process: 4 processes x 25 appends all intact",
              ok == 100 and len(lines) == 100)


def test_rollup_records_survive_compaction_and_merge():
    with _Ctx() as c:
        now = time.time()
        real_cap = dossiers.ARCHIVE_MAX_BYTES
        real_raw = dossiers.OUTCOME_RAW_S
        dossiers.ARCHIVE_MAX_BYTES = 2000
        dossiers.OUTCOME_RAW_S = 100  # roll up anything older
        try:
            for i in range(30):
                dossiers.record_outcome(
                    "MINT-OLD", "grad", ts=now - 1000 - i,
                    realized_mult=2.0 if i % 2 else 0.5,
                    closed_reason="trail")
            dossiers._last_sweep = 0.0
            dossiers._sweep(now)
            with open(dossiers.ARCHIVE_FILE) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            rollups = [l for l in lines
                       if l.get("record_type") == "outcome_rollup"]
            check("rollup: old outcomes aggregated per lane-year",
                  len(rollups) == 1
                  and rollups[0].get("count") == 30
                  and rollups[0].get("wins") == 15
                  and rollups[0].get("losses") == 15
                  and rollups[0].get("provenance") == "simulated"
                  and rollups[0].get("qualified") is False)
            # A second compaction merges the existing rollup
            # instead of duplicating it. (Compaction only runs
            # past the cap, so drop the cap to force it.)
            dossiers.record_outcome("MINT-OLD2", "grad",
                                    ts=now - 2000,
                                    realized_mult=3.0,
                                    closed_reason="trail")
            dossiers.ARCHIVE_MAX_BYTES = 100
            dossiers._maybe_compact_archive(now + 1)
            with open(dossiers.ARCHIVE_FILE) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            rollups = [l for l in lines
                       if l.get("record_type") == "outcome_rollup"]
            check("rollup: second compaction merges, no duplicate",
                  len(rollups) == 1
                  and rollups[0].get("count") == 31
                  and rollups[0].get("wins") == 16)
        finally:
            dossiers.ARCHIVE_MAX_BYTES = real_cap
            dossiers.OUTCOME_RAW_S = real_raw


def test_exit_price_kind_labels():
    with _Ctx() as c:
        now = time.time()
        dossiers.record_outcome("MINT-K1", "grad", ts=now,
                                realized_mult=2.0,
                                closed_reason="trail")
        dossiers.record_outcome("MINT-K2", "grad", ts=now,
                                realized_mult=1.5,
                                closed_reason="f2_last_known_price")
        dossiers.record_outcome("MINT-K3", "grad", ts=now,
                                realized_mult=0.0,
                                closed_reason="stop_unquotable")
        dossiers.record_outcome("MINT-K4", "grad", ts=now,
                                realized_mult=1.1,
                                closed_reason=None)
        k = {o["mint"]: o.get("exit_price_kind")
             for o in dossiers.outcome_records()}
        check("kind: current-price fill labeled",
              k.get("MINT-K1") == "current_price_simulated_fill")
        check("kind: stale estimate labeled",
              k.get("MINT-K2") == "stale_price_estimate")
        check("kind: write-down labeled",
              k.get("MINT-K3") == "write_down_zero")
        check("kind: missing reason is unknown, never assumed",
              k.get("MINT-K4") == "unknown")


def test_deployer_history_current_price_only():
    with _Ctx() as c:
        now = time.time()
        c.write_dossier("MINT-DH", born_ts=now - 90000,
                        deployer="DEP-DH",
                        safety={"report": {"unknown": False,
                                           "source": "x"}})
        dossiers._last_sweep = 0.0
        dossiers._sweep(now)
        dossiers.record_outcome("MINT-DH", "grad", ts=now,
                                realized_mult=2.0,
                                closed_reason="trail")
        dossiers.record_outcome("MINT-DH", "grad", ts=now,
                                realized_mult=3.0,
                                closed_reason="f2_last_known_price")
        dossiers.record_outcome("MINT-DH", "grad", ts=now,
                                realized_mult=0.0,
                                closed_reason="stop_unquotable")
        h = dossiers._intel_fields("DEP-DH").get(
            "deployer_history") or {}
        check("history: wins/losses count current-price fills only",
              h.get("simulated_wins") == 1
              and h.get("simulated_losses") == 0)
        check("history: non-current-price samples counted apart",
              h.get("outcomes_not_current_price") == 2
              and h.get("outcomes") == 3)


def test_outcome_rollup_preserves_deployer_linkage():
    with _Ctx() as c:
        now = time.time()
        real_cap = dossiers.ARCHIVE_MAX_BYTES
        real_raw = dossiers.OUTCOME_RAW_S
        dossiers.ARCHIVE_MAX_BYTES = 100  # force compaction
        dossiers.OUTCOME_RAW_S = 100
        try:
            c.write_dossier("MINT-LINK", born_ts=now - 90000,
                            deployer="DEP-LINK",
                            safety={"report": {"unknown": False,
                                               "source": "x"}})
            dossiers._last_sweep = 0.0
            dossiers._sweep(now - 500)
            dossiers.record_outcome("MINT-LINK", "grad",
                                    ts=now - 1000,
                                    realized_mult=2.0,
                                    closed_reason="trail")
            dossiers._maybe_compact_archive(now)
            rollups = [r for r in dossiers.launch_archive(limit=1000)
                       if r.get("record_type") == "outcome_rollup"]
            check("rollup: keyed lane+year+deployer, linkage kept",
                  len(rollups) == 1
                  and rollups[0].get("deployer") == "DEP-LINK"
                  and rollups[0].get("lane") == "grad"
                  and rollups[0].get("count") == 1
                  and rollups[0].get("wins") == 1
                  and rollups[0].get(
                      "kind_current_price_simulated_fill") == 1)
        finally:
            dossiers.ARCHIVE_MAX_BYTES = real_cap
            dossiers.OUTCOME_RAW_S = real_raw


def test_launch_rollup_after_raw_window():
    with _Ctx() as c:
        now = time.time()
        real_cap = dossiers.ARCHIVE_MAX_BYTES
        real_lraw = dossiers.LAUNCH_RAW_S
        dossiers.ARCHIVE_MAX_BYTES = 100
        dossiers.LAUNCH_RAW_S = 100
        try:
            c.write_dossier("MINT-OLD-L", born_ts=now - 90000,
                            deployer="DEP-OLD",
                            safety={"report": {"unknown": False,
                                               "source": "x"}})
            dossiers._last_sweep = 0.0
            dossiers._sweep(now - 1000)  # archived 1000s ago
            dossiers._maybe_compact_archive(now)
            raw = [r for r in dossiers.launch_archive(limit=1000)
                   if r.get("record_type", "launch") == "launch"]
            lru = [r for r in dossiers.launch_archive(limit=1000)
                   if r.get("record_type") == "launch_rollup"]
            check("launch rollup: aged launch folded, raw gone",
                  len(raw) == 0 and len(lru) == 1
                  and lru[0].get("deployer") == "DEP-OLD"
                  and lru[0].get("launches") == 1
                  and lru[0].get("conclusive") == 1)
            h = dossiers._intel_fields("DEP-OLD").get(
                "deployer_history") or {}
            check("launch rollup: deployer history still visible "
                  "and labeled as rolled up",
                  h.get("archived_launches") == 1
                  and h.get("includes_rollup") is True)
        finally:
            dossiers.ARCHIVE_MAX_BYTES = real_cap
            dossiers.LAUNCH_RAW_S = real_lraw


def test_max_kept_launches_hard_cap():
    with _Ctx() as c:
        now = time.time()
        real_cap = dossiers.ARCHIVE_MAX_BYTES
        real_max = dossiers.MAX_KEPT_LAUNCHES
        # Byte cap huge: compaction must trigger on the launch
        # COUNT alone.
        dossiers.ARCHIVE_MAX_BYTES = 10 ** 9
        dossiers.MAX_KEPT_LAUNCHES = 5
        try:
            for i in range(12):
                c.write_dossier("MINT-CAP-%02d" % i,
                                born_ts=now - 90000 + i,
                                deployer="DEP-CAP",
                                safety={"report": {"unknown": False,
                                                   "source": "x"}})
                dossiers._last_sweep = 0.0
                dossiers._sweep(now - 100 + i)
            dossiers._maybe_compact_archive(now)
            with open(dossiers.ARCHIVE_FILE) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            raw = [l for l in lines
                   if l.get("record_type", "launch") == "launch"]
            lru = [l for l in lines
                   if l.get("record_type") == "launch_rollup"]
            check("cap: per-mint launches never exceed the hard cap",
                  len(raw) <= 5)
            check("cap: overflow folded into deployer-year rollup",
                  sum(int(r.get("launches") or 0) for r in lru)
                  == 7)
            check("cap: nothing lost, raw + rollup == archived",
                  len(raw) + sum(int(r.get("launches") or 0)
                                 for r in lru) == 12)
        finally:
            dossiers.ARCHIVE_MAX_BYTES = real_cap
            dossiers.MAX_KEPT_LAUNCHES = real_max


def test_end_to_end_memory_consumed_after_restart():
    """Full consumer chain in one flow: birth -> dossier -> close
    outcome via the verdict-ledger funnel -> TTL eviction archives
    -> live dossier GONE -> durable archive is the only deployer
    linkage -> rugger registry refresh joins dead_mints through
    the archive -> intel surface consumes both registries -> a
    fresh PROCESS (restart) sees the same consumed record."""
    import subprocess
    from daemon.launch_scanner import rugger_registry as rr
    with _Ctx() as c:
        real_reg = rr.REGISTRY_PATH
        rr.REGISTRY_PATH = os.path.join(c.home, "rugger_registry.json")
        try:
            now = time.time()
            mints = ["MINT-E2E-%d" % i for i in range(3)]
            # 1. births -> dossiers with one deployer.
            for m in mints:
                c.write_dossier(m, born_ts=now - 90000,
                                deployer="DEP-E2E",
                                safety={"report": {"unknown": False,
                                                   "source": "x"}})
            # 2. one close outcome via the verdict-ledger funnel.
            os.environ["FOREX_AGENT_HOME"] = c.home
            try:
                from daemon.launch_scanner import verdict_ledger
                verdict_ledger.append(
                    "grad", "close",
                    {"mint": mints[0], "pnl_usd": -1.0,
                     "realized_mult": 0.4, "closed_reason": "stop"})
            finally:
                del os.environ["FOREX_AGENT_HOME"]
            # 3. TTL eviction archives and unlinks the dossiers.
            dossiers._last_sweep = 0.0
            dossiers._sweep(now)
            check("e2e: live dossiers evicted after archiving",
                  not os.listdir(dossiers.DOSSIER_DIR))
            # 4. grad ledger marks all three mints dead.
            with open(os.path.join(c.home, "grad_ledger.json"),
                      "w") as f:
                json.dump({"dead_mints": {
                    m: {"ts": now, "reason":
                        "safety: rugcheck danger: rug"}
                    for m in mints}}, f)
            # 5. registry refresh: dead_mints join through the
            # ARCHIVE (live dossiers are gone).
            rr.refresh()
            r = rr.lookup("DEP-E2E")
            check("e2e: registry flags deployer via archive join",
                  r["flagged"] is True and r["dead"] == 3
                  and r["launches"] == 3)
            srcs = rr.load_registry()["deployers"]["DEP-E2E"][
                "sources"]
            check("e2e: archive join auditable in sources",
                  "dead_mints+launch_archive" in srcs)
            # 6. intel surface consumes BOTH durable inputs.
            fields = dossiers._intel_fields("DEP-E2E")
            check("e2e: rugger advisory surfaced from durable join",
                  (fields.get("deployer_rugger") or {})
                  .get("flagged") is True)
            h = fields.get("deployer_history") or {}
            check("e2e: deployer history consumed with kinds",
                  h.get("archived_launches") == 3
                  and h.get("outcomes") == 1
                  and h.get("simulated_losses") == 1
                  and h.get("outcomes_not_current_price") == 0)
            # 7. restart proof: a FRESH PROCESS with only the home
            # env reads the same consumed record.
            child = (
                "import json, os;"
                "import sys;"
                "sys.path.insert(0, %r);"
                "from daemon.launch_scanner import rugger_registry"
                " as rr;"
                "print(json.dumps(rr.lookup('DEP-E2E')))"
                % os.path.join(os.path.dirname(__file__), ".."))
            env = dict(os.environ)
            env["FOREX_AGENT_HOME"] = c.home
            out = subprocess.check_output(
                [sys.executable, "-c", child], env=env,
                timeout=60)
            got = json.loads(out.decode())
            check("e2e: fresh process (restart) sees flagged record",
                  got.get("flagged") is True
                  and got.get("dead") == 3)
        finally:
            rr.REGISTRY_PATH = real_reg


def test_sweep_refreshes_registry_on_cadence():
    """The runtime (not a manual script) keeps the advisory
    registry fresh: the dossier sweep re-derives it at most once
    per REGISTRY_REFRESH_S."""
    from daemon.launch_scanner import rugger_registry as rr
    with _Ctx() as c:
        real_reg = rr.REGISTRY_PATH
        real_last = dossiers._last_registry_refresh
        real_s = dossiers.REGISTRY_REFRESH_S
        rr.REGISTRY_PATH = os.path.join(c.home, "rugger_registry.json")
        dossiers._last_registry_refresh = 0.0
        dossiers.REGISTRY_REFRESH_S = 900
        try:
            now = time.time()
            dossiers._last_sweep = 0.0
            dossiers._sweep(now)
            check("cadence: sweep refreshes the registry (no script)",
                  os.path.exists(rr.REGISTRY_PATH))
            os.unlink(rr.REGISTRY_PATH)
            dossiers._sweep(now + 400)  # past sweep gate, within cadence
            check("cadence: throttled within REGISTRY_REFRESH_S",
                  not os.path.exists(rr.REGISTRY_PATH))
            dossiers._sweep(now + 1000)  # past both gates
            check("cadence: refreshes again after the interval",
                  os.path.exists(rr.REGISTRY_PATH))
        finally:
            rr.REGISTRY_PATH = real_reg
            dossiers._last_registry_refresh = real_last
            dossiers.REGISTRY_REFRESH_S = real_s


def test_build_path_replay_drives_memory_consumption():
    """Runtime-wiring proof with recorded responses injected at
    the provider seams (the instant_safety fake-RPC pattern):
    observe_births -> background _build -> dossier carries the
    recorded safety precompute -> TTL eviction archives ->
    registry joins dead_mints through the archive -> the NEXT
    birth by the same deployer consumes the durable record AT
    BUILD TIME through the same real code path."""
    from daemon.launch_scanner import rugger_registry as rr

    class _FakeSafety:
        @staticmethod
        def check_token(chain, mint, timeout=10):
            return {"unknown": False, "passed": True,
                    "source": "recorded-replay", "score": 95}

    with _Ctx() as c:
        real_reg = rr.REGISTRY_PATH
        real_safety = dossiers._safety
        real_res = dossiers._res
        rr.REGISTRY_PATH = os.path.join(c.home, "rugger_registry.json")
        dossiers._safety = _FakeSafety
        dossiers._res = None
        try:
            now = time.time()
            born = now - 90000  # already past TTL for the sweep
            mints = ["MINT-B1", "MINT-B2", "MINT-B3"]
            for m in mints:
                item = {"source": "pumpportal-new", "mint": m,
                        "symbol": m[-2:], "created_ts": born,
                        "extra": {"trader": "DEP-B",
                                  "initial_buy_sol": 1.0,
                                  "market_cap_sol": 30.0,
                                  "bonding_curve": "BC"}}
                q = dossiers.observe_births([item], now=born)
                check("replay: %s accepted by the real intake" % m,
                      q == 1)
            # Wait for the background worker to build all three.
            # (read_dossier hides already-expired dossiers, so read
            # the raw files here.)
            def _raw(m):
                try:
                    with open(os.path.join(dossiers.DOSSIER_DIR,
                                           m + ".json")) as f:
                        return json.load(f)
                except Exception:
                    return None
            built = {}
            for _ in range(200):
                built = {m: _raw(m) for m in mints}
                if all(built.values()):
                    break
                time.sleep(0.05)
            check("replay: _build wrote all three dossiers",
                  all(built.values()))
            check("replay: recorded safety precompute attached",
                  all((d.get("safety") or {}).get("report", {})
                      .get("source") == "recorded-replay"
                      for d in built.values()))
            # TTL eviction archives all three (real sweep).
            dossiers._last_sweep = 0.0
            dossiers._sweep(now)
            check("replay: eviction archived + unlinked all three",
                  not os.listdir(dossiers.DOSSIER_DIR)
                  and len([r for r in dossiers.launch_archive(
                      deployer="DEP-B", limit=10)
                      if r.get("record_type", "launch") == "launch"])
                  == 3)
            # Dead-mint evidence joins through the ARCHIVE only.
            with open(os.path.join(c.home, "grad_ledger.json"),
                      "w") as f:
                json.dump({"dead_mints": {
                    m: {"ts": now, "reason":
                        "safety: rugcheck danger: rug"}
                    for m in mints}}, f)
            rr.refresh()
            r = rr.lookup("DEP-B")
            check("replay: registry flags via the archive join",
                  r["flagged"] is True and r["dead"] == 3)
            # The NEXT birth consumes the durable record at build
            # time through the same real path.
            item4 = {"source": "pumpportal-new", "mint": "MINT-B4",
                     "symbol": "B4", "created_ts": now,
                     "extra": {"trader": "DEP-B",
                               "initial_buy_sol": 1.0,
                               "market_cap_sol": 30.0,
                               "bonding_curve": "BC"}}
            dossiers.observe_births([item4], now=now)
            d4 = None
            for _ in range(200):
                d4 = dossiers.read_dossier("MINT-B4")
                if d4 and d4.get("deployer_rugger"):
                    break
                time.sleep(0.05)
            check("replay: next build consumes rugger advisory",
                  ((d4 or {}).get("deployer_rugger") or {})
                  .get("flagged") is True)
            check("replay: next build consumes durable history",
                  ((d4 or {}).get("deployer_history") or {})
                  .get("archived_launches") == 3)
        finally:
            rr.REGISTRY_PATH = real_reg
            dossiers._safety = real_safety
            dossiers._res = real_res


def test_registry_refresh_stamps_only_after_success():
    """A failed refresh attempt must not suppress the retry for a
    whole cadence (validator lifecycle finding)."""
    from daemon.launch_scanner import rugger_registry as rr
    with _Ctx() as c:
        real_reg = rr.REGISTRY_PATH
        real_last = dossiers._last_registry_refresh
        rr.REGISTRY_PATH = os.path.join(c.home, "rugger_registry.json")
        dossiers._last_registry_refresh = 0.0
        real_refresh = rr.refresh
        try:
            def _boom(*a, **k):
                raise RuntimeError("forced")
            rr.refresh = _boom
            dossiers._maybe_refresh_registry(1000.0)
            check("lifecycle: failed attempt leaves cadence unstamped",
                  dossiers._last_registry_refresh == 0.0)
            rr.refresh = real_refresh
            dossiers._maybe_refresh_registry(1001.0)
            check("lifecycle: retry after failure runs immediately",
                  dossiers._last_registry_refresh == 1001.0
                  and os.path.exists(rr.REGISTRY_PATH))
        finally:
            rr.refresh = real_refresh
            rr.REGISTRY_PATH = real_reg
            dossiers._last_registry_refresh = real_last


def main():
    test_registry_refresh_stamps_only_after_success()
    test_build_path_replay_drives_memory_consumption()
    test_sweep_refreshes_registry_on_cadence()
    test_end_to_end_memory_consumed_after_restart()
    test_exit_price_kind_labels()
    test_deployer_history_current_price_only()
    test_outcome_rollup_preserves_deployer_linkage()
    test_launch_rollup_after_raw_window()
    test_max_kept_launches_hard_cap()
    test_missing_archive_with_existing_history_is_unavailable()
    test_torn_tail_marks_history_partial()
    test_compaction_never_discards_concurrent_append()
    test_cross_process_appends_never_interleave()
    test_rollup_records_survive_compaction_and_merge()
    test_eviction_suppressed_when_archive_write_fails()
    test_archive_status_distinguishes_empty_from_unavailable()
    test_outcome_linkage_via_verdict_ledger_close()
    test_compaction_bounds_size_and_keeps_resolved_state()
    test_concurrent_appends_never_interleave()
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
