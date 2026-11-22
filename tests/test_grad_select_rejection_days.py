#!/usr/bin/env python3
"""Grad-select rejection day-bucket tests (2026-10-29, gap fix).

Covers the counts_by_day addition to _journal_rejection in
daemon/launch_scanner/grad_select.py:
  * a rejection increments the cumulative counter AND the WAT-day
    bucket for its reason;
  * rejections on two WAT days land in separate buckets while the
    cumulative counter sums both;
  * a pre-fix journal without counts_by_day gains it on the first
    new rejection, with existing cumulative counts preserved;
  * the WAT day boundary: 23:30 WAT buckets to that day, 00:30 WAT
    to the next (gmtime(now+3600) rule);
  * the recent list stays capped at 200 (unchanged behavior);
  * odd inputs (no reason, no mint) never raise and journal as
    "unknown".
The trading engine is a fake ledger dict; no lane code runs.
Plain script — run directly, not via pytest.
"""
import contextlib
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "daemon", "launch_scanner"))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="gs-rej-days-test-")
os.environ["FOREX_AGENT_HOME"] = _TMP

import grad_select as gs  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        FAILURES.append(name)


class FakeEngine:
    def __init__(self, ledger):
        self.ledger = ledger

    @contextlib.contextmanager
    def _ledger_lock(self):
        yield

    def _read(self):
        return self.ledger

    def _write(self, d):
        self.ledger = d


# WAT day boundaries (epoch): Oct 29 00:00 WAT = 1793228400.
OCT29_0030_WAT = 1793228400 + 1800
OCT28_2330_WAT = 1793228400 - 1800


def main():
    eng = FakeEngine({})
    gs._journal_rejection(eng, {"mint": "M1", "symbol": "AAA"},
                          {"reason": "slow_curve", "values": {}},
                          OCT29_0030_WAT)
    j = eng.ledger["select_journal"]
    check("day-bucket-created",
          j["counts"] == {"slow_curve": 1}
          and j["counts_by_day"] == {"2026-10-29": {"slow_curve": 1}},
          str(j))
    check("recent-has-one", len(j["recent"]) == 1)

    gs._journal_rejection(eng, {"mint": "M2", "symbol": "BBB"},
                          {"reason": "slow_curve", "values": {}},
                          OCT28_2330_WAT)
    j = eng.ledger["select_journal"]
    check("two-days-separate-buckets",
          j["counts"] == {"slow_curve": 2}
          and j["counts_by_day"] == {
              "2026-10-29": {"slow_curve": 1},
              "2026-10-28": {"slow_curve": 1}},
          str(j["counts_by_day"]))
    check("wat-boundary-next-day",
          "2026-10-29" in j["counts_by_day"]
          and "2026-10-28" in j["counts_by_day"])

    # Pre-fix journal without counts_by_day: migration-safe.
    eng2 = FakeEngine({"select_journal": {
        "counts": {"serial_deployer": 5}, "recent": [],
        "started_ts": 1792800000}})
    gs._journal_rejection(eng2, {"mint": "M3", "symbol": "CCC"},
                          {"reason": "serial_deployer", "values": {}},
                          OCT29_0030_WAT)
    j2 = eng2.ledger["select_journal"]
    check("old-journal-migrates",
          j2["counts"] == {"serial_deployer": 6}
          and j2["counts_by_day"] == {
              "2026-10-29": {"serial_deployer": 1}},
          str(j2))

    # Recent list stays capped at 200.
    eng3 = FakeEngine({})
    for i in range(205):
        gs._journal_rejection(eng3, {"mint": "M%d" % i, "symbol": "?"},
                              {"reason": "copycat_ticker", "values": {}},
                              OCT29_0030_WAT + i)
    j3 = eng3.ledger["select_journal"]
    check("recent-still-capped-200", len(j3["recent"]) == 200,
          str(len(j3["recent"])))
    check("cumulative-counts-all-205",
          j3["counts"] == {"copycat_ticker": 205}
          and j3["counts_by_day"] == {
              "2026-10-29": {"copycat_ticker": 205}},
          str(j3["counts_by_day"]))

    # Odd inputs never raise.
    eng4 = FakeEngine({})
    try:
        gs._journal_rejection(eng4, {}, {}, OCT29_0030_WAT)
        ok = True
    except Exception as e:  # noqa: BLE001
        ok = False
        print("raised: %r" % e)
    j4 = eng4.ledger.get("select_journal") or {}
    check("odd-inputs-journal-unknown",
          ok and j4.get("counts") == {"unknown": 1}
          and j4.get("counts_by_day") == {"2026-10-29": {"unknown": 1}},
          str(j4))

    if FAILURES:
        print("FAILURES: %s" % FAILURES)
        sys.exit(1)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
