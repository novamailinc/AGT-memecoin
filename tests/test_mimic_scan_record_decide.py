#!/usr/bin/env python3
"""Real record->decide scan-path regression for mimic_edge / mimic_edge2.

Drives the PRODUCTION scan_once order (record loop -> watchlist merge ->
engine.consider_candidate -> _filtered_consider -> _filter_decision),
not _filter_decision in isolation. Covers:
  A. corrupt DECISION-RELEVANT key (the candidate's own deployer key)
  B. corrupt OTHER key (only the expiry cleanup would touch it)
  C. legitimate expiry drop + 64-cap preserved (no poison)
  D. clean happy path admits
Corruption must poison the journal and fail closed with
unknown_serial_coverage; corrupt members must survive untouched
(evidence preserved, never healed into a clean window).
"""
import sys, threading, time
sys.path.insert(0, "daemon/launch_scanner")
import mimic_edge as me
import mimic_edge2 as me2

FAILS = []
NOW = time.time()


class _KS:
    def is_halted(self, lane):
        return False


class _Eng:
    DEAD_TTL_S = 86400.0

    def __init__(self, mod, admit):
        self._d = {"positions": {}, "dead_mints": {}, "closes": [],
                   "watchlist": {}, "filter_stats": {}}
        self._lock = threading.Lock()
        self._ks = _KS()
        self._admit = admit
        self.base_calls = []
        self.consider_candidate = (
            lambda cand, now=None: mod._filtered_consider(self, cand, now))

    def ensure_ledger(self):
        pass

    def _ledger_lock(self):
        return self._lock

    def _read(self):
        return self._d

    def _write(self, d):
        self._d = d

    def is_graduation_candidate(self, row, now):
        return row

    def _base_consider_candidate(self, cand, now):
        self.base_calls.append(cand)
        return (self._admit, "stub", False)


class _Feed:
    def __init__(self, items):
        self._items = items

    def drain(self):
        return self._items


def _item(symbol, trader):
    return {"source": "pumpportal-new", "mint": symbol.lower() + "pump",
            "symbol": symbol, "created_ts": NOW,
            "extra": {"trader": trader}}


def _reset(mod, journal):
    mod.JOURNAL.clear()
    mod.JOURNAL.update(journal)


def _run(mod, name, journal, symbol="AAA", trader="T1", admit=True):
    _reset(mod, journal)
    eng = _Eng(mod, admit)
    out = mod.scan_once(eng, feed=_Feed([_item(symbol, trader)]), now=NOW)
    return eng, out


for mod, name in ((me, "mimic_edge"), (me2, "mimic_edge2")):
    # A. corrupt DECISION-RELEVANT key (candidate's own deployer)
    eng, out = _run(mod, name,
                    {"tickers": {}, "deployers": {"T1": ["corrupt"]}})
    fs = eng._d.get("filter_stats", {})
    if out["opened"] != 0:
        FAILS.append("%s A: corrupt same-key ADMITTED" % name)
    if not mod.JOURNAL.get("_corrupt"):
        FAILS.append("%s A: journal not poisoned" % name)
    if mod.JOURNAL["deployers"].get("T1") != ["corrupt"]:
        FAILS.append("%s A: corrupt evidence healed/reset: %r"
                     % (name, mod.JOURNAL["deployers"].get("T1")))
    if int(fs.get("rejected_unknown_coverage") or 0) != 1:
        FAILS.append("%s A: reject not bucketed unknown_coverage: %r"
                     % (name, fs))

    # B. corrupt OTHER key (expiry cleanup path only)
    eng, out = _run(mod, name,
                    {"tickers": {"ZZZ": [NOW, "bad"]}, "deployers": {}})
    fs = eng._d.get("filter_stats", {})
    if out["opened"] != 0:
        FAILS.append("%s B: corrupt other-key ADMITTED" % name)
    if not mod.JOURNAL.get("_corrupt"):
        FAILS.append("%s B: journal not poisoned via cleanup" % name)
    if mod.JOURNAL["tickers"].get("ZZZ") != [NOW, "bad"]:
        FAILS.append("%s B: other-key evidence healed: %r"
                     % (name, mod.JOURNAL["tickers"].get("ZZZ")))

    # C. legitimate expiry drop + no poison + admit
    eng, out = _run(mod, name,
                    {"tickers": {"OLD": [NOW - 90000]}, "deployers": {}})
    if mod.JOURNAL.get("_corrupt"):
        FAILS.append("%s C: legitimate expiry poisoned" % name)
    if "OLD" in mod.JOURNAL.get("tickers", {}):
        FAILS.append("%s C: expired key not dropped" % name)
    if out["opened"] != 1 or len(eng.base_calls) != 1:
        FAILS.append("%s C: clean candidate not admitted: %r"
                     % (name, out))

    # C2. 64-cap preserved on the mutated key (direct record call)
    j = {"tickers": {}, "deployers": {"T1": [NOW - i for i in range(70)]}}
    ok = mod._record_launch(j, "AAA", "T1", NOW)
    if ok is not True or len(j["deployers"]["T1"]) != 64:
        FAILS.append("%s C2: 64-cap broken: ok=%r len=%s"
                     % (name, ok, len(j["deployers"].get("T1", []))))

    # D. happy path records + admits
    eng, out = _run(mod, name, {"tickers": {}, "deployers": {}})
    if out["opened"] != 1:
        FAILS.append("%s D: happy path not admitted: %r" % (name, out))
    if len(mod.JOURNAL["deployers"].get("T1", [])) != 1:
        FAILS.append("%s D: launch not recorded" % name)

print("FAILs:", FAILS if FAILS else 0, FAILS or "ALL PASS")
