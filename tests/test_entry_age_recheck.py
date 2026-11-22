#!/usr/bin/env python3
"""Rule A entry-age re-check (Item 6, 2026-11-16).

Bug: Rule A gated the age at EVALUATION time, but the entry could
land minutes later (entry-stage RPC delays) — 15 post-Rule-A stream
entries entered Nov 16 08:32-15:41 UTC at grad_age 51-261s with ZERO
slow_entry ever journaled. The re-check (grad_select.
recheck_entry_age, called from both lanes' _scan_once between the
filter stage and the entry stage) recomputes the age at entry time
for stream candidates and rejects slow/unknown ones, journaled,
popped, and marked dead. Backfill gated the same way since
2026-11-21 (exit-study Rule 1): the backfill exemption is gone,
so a backfill row older than 30s is rejected as slow_entry and
one with no created_ts as unknown_grad_age, exactly like a
stream candidate.

Plain script: run with the repo venv python from the repo root.
No network, no live state.
"""
import contextlib
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from daemon.launch_scanner import grad_select as gs  # noqa: E402

if not hasattr(gs, "recheck_entry_age"):
    # The helper itself is absent (pre-fix code): report it as a
    # check failure instead of crashing on the missing attribute.
    print("FAIL entry-age recheck helper missing "
          "(recheck_entry_age not defined)")
    print("FAILED: 1 check(s): helper missing")
    sys.exit(1)

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name
          + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


class FakeEngine:
    """Minimal ledger holder: _ledger_lock/_read/_write only."""

    def __init__(self, d):
        self._d = d

    @contextlib.contextmanager
    def _ledger_lock(self):
        yield

    def _read(self):
        return self._d

    def _write(self, d):
        self._d = d


NOW = 1_000_000.0


def _cand(mint, source, age_s=None, future_s=None):
    c = {"mint": mint, "symbol": "S" + mint[-2:], "source": source}
    if age_s is not None:
        c["created_ts"] = NOW - age_s
    if future_s is not None:
        c["created_ts"] = NOW + future_s
    return c


passed = [
    _cand("fresh10", "pumpportal-migration", age_s=10),      # keep
    _cand("edge30", "pumpportal-migration", age_s=30.0),     # keep (==30)
    _cand("slow301", "pumpportal-migration", age_s=30.1),    # slow_entry
    _cand("slow45", "pumpportal-migration", age_s=45),        # slow_entry
    _cand("slow261", "pumpportal-migration", age_s=261),      # slow_entry
    _cand("nocreated", "pumpportal-migration"),               # unknown
    _cand("future60", "pumpportal-migration", future_s=60),   # unknown
    _cand("future4", "pumpportal-migration", future_s=4),     # keep (<5s skew)
    _cand("bf1000", "geckoterminal-backfill", age_s=1000),    # slow_entry
    _cand("bfnone", "geckoterminal-backfill"),                # unknown_grad_age
    _cand("nosource", "", age_s=500),                         # slow_entry
]
ledger = {"watchlist": {c["mint"]: dict(c) for c in passed},
          "dead_mints": {}}
eng = FakeEngine(ledger)
out = {"select_rejected": 0, "select_reasons": {}}

result = gs.recheck_entry_age(passed, eng, out, entry_now=NOW)

kept = {c["mint"] for c in result}
check("keeps fresh stream + boundary + skew only",
      kept == {"fresh10", "edge30", "future4"}, str(sorted(kept)))
check("returns a new list without the stale eight",
      len(result) == 3, str(len(result)))

d = eng._read()
j = d.get("select_journal") or {}
counts = j.get("counts") or {}
check("journal counts slow_entry=5", counts.get("slow_entry") == 5,
      str(counts))
check("journal counts unknown_grad_age=3",
      counts.get("unknown_grad_age") == 3, str(counts))
day = time.strftime("%Y-%m-%d", time.gmtime(NOW + 3600))
by_day = (j.get("counts_by_day") or {}).get(day) or {}
check("journal counts_by_day matches",
      by_day.get("slow_entry") == 5
      and by_day.get("unknown_grad_age") == 3, str(by_day))
recent = {r["mint"]: r for r in (j.get("recent") or [])}
check("journal recent holds all eight rejects",
      set(recent) == {"slow301", "slow45", "slow261", "nocreated",
                      "future60", "bf1000", "bfnone", "nosource"},
      str(sorted(recent)))
check("journal recent marks stage=entry_recheck",
      all((r.get("values") or {}).get("stage") == "entry_recheck"
          for r in recent.values()), str(recent))
check("journal recent records grad_age_s",
      recent["slow261"].get("values", {}).get("grad_age_s") == 261.0,
      str(recent.get("slow261")))
check("watchlist popped the eight stale",
      set(d["watchlist"]) == kept, str(sorted(d["watchlist"])))
dead = d.get("dead_mints") or {}
check("dead_mints holds the eight stale",
      set(dead) == {"slow301", "slow45", "slow261", "nocreated",
                    "future60", "bf1000", "bfnone", "nosource"},
      str(sorted(dead)))
check("dead_mints reason carries select: slow_entry",
      dead["slow45"].get("reason") == "select: slow_entry",
      str(dead.get("slow45")))
check("dead_mints reason carries select: unknown_grad_age",
      dead["nocreated"].get("reason") == "select: unknown_grad_age",
      str(dead.get("nocreated")))
check("out select_rejected == 8", out.get("select_rejected") == 8,
      str(out))
check("out select_reasons match",
      out.get("select_reasons") == {"slow_entry": 5,
                                    "unknown_grad_age": 3}, str(out))

# No-op path: all fresh -> same list, no journal created.
ledger2 = {"watchlist": {"a": _cand("a", "pumpportal-migration",
                                     age_s=5)},
           "dead_mints": {}}
eng2 = FakeEngine(ledger2)
out2 = {"select_rejected": 0, "select_reasons": {}}
fresh = [ledger2["watchlist"]["a"]]
res2 = gs.recheck_entry_age(fresh, eng2, out2, entry_now=NOW)
check("all-fresh: list unchanged, no journal, no out counts",
      res2 == fresh and "select_journal" not in eng2._read()
      and out2 == {"select_rejected": 0, "select_reasons": {}},
      str(out2))

# Wiring guard: both lanes' _scan_once must call the re-check.
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                    "daemon", "launch_scanner")
sel_src = open(os.path.join(root, "grad_select.py")).read()
raw_src = open(os.path.join(root, "grad_raw.py")).read()
check("wiring: grad_select _scan_once calls recheck_entry_age",
      "passed = recheck_entry_age(passed, engine, out)" in sel_src)
check("wiring: grad_raw _scan_once calls _select.recheck_entry_age",
      "passed = _select.recheck_entry_age(passed, engine, out)"
      in raw_src)

# ---- Chain-age gate (2026-11-18): tranche/tranche_protect port --
# The same Nov 16 hole lived in the tranche lanes: they shared the
# filter stage but never ran the entry-stage gates, so Nov 18
# 02:16-05:36 UTC they entered 50-115s-old graduations. The fix
# ports both gates (recheck_entry_age + the SlotClock chain-age
# gate, shared helper recheck_chain_age) into tranche.py and
# tranche_protect.py _scan_once, mirroring grad_raw, and adds the
# select_* out keys their _scan_once was missing (the helpers
# write out["select_reasons"] directly — without the keys the
# first stale candidate would raise KeyError mid-scan).
if not hasattr(gs, "recheck_chain_age"):
    print("FAIL chain-age gate helper missing "
          "(recheck_chain_age not defined)")
    FAILURES.append("chain-age helper missing")
else:
    class FakeClock:
        """age(slot) from a preset map; raises for listed slots."""

        def __init__(self, ages, raise_slots=()):
            self._ages = ages
            self._raise = set(raise_slots)

        def age(self, slot):
            if slot in self._raise:
                raise RuntimeError("clock down")
            return self._ages.get(slot)

    def _ccand(mint, verdict_key="select"):
        return {"mint": mint, "symbol": "C" + mint[-2:],
                "source": "pumpportal-migration",
                verdict_key: {"verdict": "pass", "values": {}}}

    NOW2 = 2_000_000.0
    chain_passed = [
        _ccand("chain10"),              # chain age 10s: keep
        _ccand("chain30"),              # chain age 30s: keep (==30)
        _ccand("chain31"),              # chain age 31s: reject
        _ccand("chain300"),             # chain age 300s: reject
        _ccand("noslot"),               # no slot in ctx: keep
        _ccand("badslot"),              # non-int slot: keep
        _ccand("clockdown"),            # clock raises: keep
        _ccand("tranche10", "tranche"),  # tranche verdict: keep+stamp
    ]
    chain_ctxs = {
        "chain10": {"migration_slot": 101},
        "chain30": {"migration_slot": 102},
        "chain31": {"migration_slot": 103},
        "chain300": {"migration_slot": 104},
        "noslot": {},
        "badslot": {"migration_slot": "x"},
        "clockdown": {"migration_slot": 105},
        "tranche10": {"migration_slot": 106},
    }
    chain_ledger = {"watchlist": {c["mint"]: dict(c)
                                  for c in chain_passed},
                    "dead_mints": {}}
    eng3 = FakeEngine(chain_ledger)
    out3 = {"select_rejected": 0, "select_reasons": {}}
    clock = FakeClock({101: 10.0, 102: 30.0, 103: 31.0,
                       104: 300.0, 105: 999.0, 106: 10.0},
                      raise_slots={105})
    res3 = gs.recheck_chain_age(chain_passed, eng3, out3, chain_ctxs,
                                now=NOW2, clock=clock)
    kept3 = {c["mint"] for c in res3}
    check("chain-age keeps fresh/boundary/no-slot/bad-slot/clock-down",
          kept3 == {"chain10", "chain30", "noslot", "badslot",
                    "clockdown", "tranche10"}, str(sorted(kept3)))
    check("chain-age rejects exactly the two stale",
          len(res3) == 6, str(len(res3)))
    d3 = eng3._read()
    j3 = d3.get("select_journal") or {}
    counts3 = j3.get("counts") or {}
    check("chain-age journal counts slow_entry=2",
          counts3.get("slow_entry") == 2, str(counts3))
    recent3 = {r["mint"]: r for r in (j3.get("recent") or [])}
    check("chain-age journal marks stage=entry_recheck_chain",
          set(recent3) == {"chain31", "chain300"}
          and all((r.get("values") or {}).get("stage")
                  == "entry_recheck_chain"
                  for r in recent3.values()), str(recent3))
    check("chain-age journal records chain grad_age_s",
          recent3["chain300"].get("values", {}).get("grad_age_s")
          == 300.0, str(recent3.get("chain300")))
    check("chain-age watchlist popped the two stale",
          set(d3["watchlist"]) == kept3, str(sorted(d3["watchlist"])))
    check("chain-age dead_mints holds the two stale",
          set(d3.get("dead_mints") or {}) == {"chain31", "chain300"},
          str(sorted(d3.get("dead_mints") or {})))
    check("chain-age out select_rejected == 2",
          out3.get("select_rejected") == 2, str(out3))
    check("chain-age out select_reasons match",
          out3.get("select_reasons") == {"slow_entry": 2}, str(out3))
    by_val = {c["mint"]: (c.get("select") or c.get("tranche")
                          or {}).get("values") or {}
              for c in res3}
    check("chain-age stamps kept candidates with chain basis",
          by_val["chain10"].get("grad_age_basis") == "chain"
          and by_val["chain10"].get("grad_age_chain_s") == 10.0
          and by_val["tranche10"].get("grad_age_basis") == "chain",
          str(by_val))

    # No-ctx path: empty ff_ctxs -> same list, no journal, no counts.
    ledger4 = {"watchlist": {"b": _ccand("b")}, "dead_mints": {}}
    eng4 = FakeEngine(ledger4)
    out4 = {"select_rejected": 0, "select_reasons": {}}
    res4 = gs.recheck_chain_age([ledger4["watchlist"]["b"]], eng4,
                                out4, {}, now=NOW2, clock=clock)
    check("chain-age no-ctx: list unchanged, no journal, no counts",
          len(res4) == 1 and "select_journal" not in eng4._read()
          and out4 == {"select_rejected": 0, "select_reasons": {}},
          str(out4))

    # No-clock path: _slot_clock() None -> same list, no counts.
    _orig_clock_fn = gs._slot_clock
    try:
        gs._slot_clock = lambda: None
        ledger5 = {"watchlist": {"c": _ccand("c")},
                   "dead_mints": {}}
        eng5 = FakeEngine(ledger5)
        out5 = {"select_rejected": 0, "select_reasons": {}}
        res5 = gs.recheck_chain_age(
            [ledger5["watchlist"]["c"]], eng5, out5,
            {"c": {"migration_slot": 101}}, now=NOW2)
        check("chain-age no-clock: list unchanged, no journal",
              len(res5) == 1
              and "select_journal" not in eng5._read()
              and out5 == {"select_rejected": 0, "select_reasons": {}},
              str(out5))
    finally:
        gs._slot_clock = _orig_clock_fn

# Wiring guard: all four lanes' _scan_once must call both gates.
tr_src = open(os.path.join(root, "tranche.py")).read()
tp_src = open(os.path.join(root, "tranche_protect.py")).read()
check("wiring: tranche _scan_once calls _sel.recheck_entry_age",
      "passed = _sel.recheck_entry_age(passed, engine, out)"
      in tr_src)
check("wiring: tranche_protect _scan_once calls _sel.recheck_entry_age",
      "passed = _sel.recheck_entry_age(passed, engine, out)"
      in tp_src)
check("wiring: tranche _scan_once calls _sel.recheck_chain_age",
      "passed = _sel.recheck_chain_age(passed, engine, out,"
      in tr_src)
check("wiring: tranche_protect _scan_once calls _sel.recheck_chain_age",
      "passed = _sel.recheck_chain_age(passed, engine, out,"
      in tp_src)
check("wiring: tranche recheck runs before the entry stage",
      tr_src.index("recheck_entry_age")
      < tr_src.index("# ---- entry stage"))
check("wiring: tranche_protect recheck runs before the entry stage",
      tp_src.index("recheck_entry_age")
      < tp_src.index("# ---- entry stage"))
check("wiring: tranche out carries select_rejected/select_reasons",
      '"select_rejected": 0' in tr_src
      and '"select_reasons": {}' in tr_src)
check("wiring: tranche_protect out carries select_rejected/select_reasons",
      '"select_rejected": 0' in tp_src
      and '"select_reasons": {}' in tp_src)

print()
if FAILURES:
    print("FAILED: %d check(s): %s" % (len(FAILURES), ", ".join(FAILURES)))
    sys.exit(1)
print("ALL ENTRY-AGE RECHECK TESTS PASSED")
