#!/usr/bin/env python3
"""Degen learner plumbing tests (2026-10-23 signal-audit fixes):

1. Component-less records (smart-watcher) are EXCLUDED from signal
   stats — missing is not 0.
2. >=10 winners-with-features gate blocks all tuning below threshold.
3. Peak tracker: peaks persist past the 48h polling window (90d
   retention); candidate mints are loadable for peak tracking.
4. degen-feed counterfactual log: bounded JSONL records for all
   decisions, logging only.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import json
import os
import sys
import tempfile
import time

REPO = _REPO_ROOT


def load_script(name):
    path = "%s/scripts/%s" % (REPO, name)
    src = open(path).read().split('if __name__ == "__main__":')[0]
    g = {"__name__": "test", "__file__": path}
    exec(compile(src, path, "exec"), g)
    return g


fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


L = load_script("degen-learner")
COMPS = ["freshness", "buy_pressure", "buyer_count", "momentum",
         "fdv_zone", "smart_money", "deployer", "holders", "lp_locked"]

# --- 1. exclusion: missing != 0 -------------------------------------
check("learner: record_has_components true",
      L["record_has_components"]({"components": {"freshness": 1.0}}))
check("learner: record_has_components false on {}",
      not L["record_has_components"]({"components": {}}))
check("learner: record_has_components false when absent",
      not L["record_has_components"]({"symbol": "X"}))
check("learner: record_has_components false on None",
      not L["record_has_components"]({"components": None}))
# 2026-10-25 fix: tracker-style components (non-empty, but only tape
# fields, no score keys) must NOT count as features — the old
# non-empty check counted these as featured winners, so with zero
# genuine winners the stats read winners 0.0 on every signal and
# ratcheted weights to the floor on missing-data noise.
check("learner: record_has_components false on tracker-only dict",
      not L["record_has_components"](
          {"components": {"buys_m5": 458, "sells_m5": 149,
                          "chg_m5": 93.17, "vol_h24": 359303.29,
                          "safety_score": 1.0}}))
check("learner: record_has_components true on partial score dict",
      L["record_has_components"]({"components": {"buy_pressure": 7.5}}))
check("learner: tracker-only winners give w_n=0 per signal",
      L["compute_signal_stats"](
          [{"components": {"buys_m5": 10}}],
          [{"components": {"freshness": 4.0}}],
          COMPS)["freshness"]["w_n"] == 0)

winners = [
    {"components": {"freshness": 10.0, "buy_pressure": 20.0}},
    {"components": {}},            # smart-watcher style: no components
    {"symbol": "noc"},             # no components key at all
]
losers = [
    {"components": {"freshness": 4.0, "buy_pressure": 10.0}},
    {"components": {}},
]
# The learner's main() pre-filters to component-bearing records; emulate
feat_w = [r for r in winners if L["record_has_components"](r)]
feat_l = [r for r in losers if L["record_has_components"](r)]
stats = L["compute_signal_stats"](feat_w, feat_l, COMPS)
check("learner: freshness winner avg excludes component-less (10 not 3.3)",
      stats["freshness"]["w_avg"] == 10.0)
check("learner: freshness loser avg excludes component-less (4 not 2)",
      stats["freshness"]["l_avg"] == 4.0)
check("learner: freshness counts reflect featured records only",
      stats["freshness"]["w_n"] == 1 and stats["freshness"]["l_n"] == 1)
# Per-component presence: a record with only freshness contributes
# nothing to buy_pressure averages inside compute_signal_stats either.
stats2 = L["compute_signal_stats"](
    [{"components": {"freshness": 9.0}}],
    [{"components": {"buy_pressure": 3.0}}], COMPS)
check("learner: per-component missing excluded (buy_pressure w_n=0)",
      stats2["buy_pressure"]["w_n"] == 0 and
      stats2["buy_pressure"]["l_n"] == 1)
check("learner: per-component missing excluded (freshness l_n=0)",
      stats2["freshness"]["l_n"] == 0 and
      stats2["freshness"]["w_n"] == 1)

# --- 2. >=10 winners-with-features gate -------------------------------
check("learner: gate constant is 10", L["MIN_WINNERS_WITH_FEATURES"] == 10)
check("learner: gated at 0 winners-with-features", L["is_gated"](0))
check("learner: gated at 9 winners-with-features", L["is_gated"](9))
check("learner: not gated at 10 winners-with-features",
      not L["is_gated"](10))
check("learner: not gated at 25 winners-with-features",
      not L["is_gated"](25))
check("learner: advisory dry-run is mandatory",
      L["DRY_RUN"] is True)
try:
    L["save_weights"]({"weights": {}})
    _promotion_refused = False
except RuntimeError:
    _promotion_refused = True
check("learner: direct weight promotion refused", _promotion_refused)

# --- 3. peak tracker: durable peaks + candidates ----------------------
P = load_script("degen-peak-tracker")
now = time.time()
check("peak: retention far beyond 48h polling window",
      P["PEAK_RETENTION_SECS"] >= 30 * 24 * 3600 >
      P["TRACK_WINDOW_SECS"])
peaks = {
    "fresh48h": {"alert_ts": now - 3600, "peak_mult": 2.0},
    "old10d": {"alert_ts": now - 10 * 86400, "peak_mult": 5.0},
    "ancient120d": {"alert_ts": now - 120 * 86400, "peak_mult": 9.0},
}
dropped = P["prune_peaks"](peaks, now)
check("peak: 10-day-old peak SURVIVES pruning (learner can grade it)",
      "old10d" in peaks and peaks["old10d"]["peak_mult"] == 5.0)
check("peak: 120-day-old peak pruned at retention horizon",
      "ancient120d" not in peaks and dropped == ["ancient120d"])
check("peak: fresh peak kept", "fresh48h" in peaks)

tmp = tempfile.mkdtemp(prefix="degen_cf_")
cpath = os.path.join(tmp, "candidates.jsonl")
with open(cpath, "w") as f:
    f.write(json.dumps({"mint": "MINT1", "symbol": "A",
                        "alert_ts": now - 100, "alert_mcap": 50000}) + "\n")
    f.write("not json\n")
    f.write(json.dumps({"mint": "MINT2", "symbol": "B",
                        "alert_ts": now - 200, "alert_mcap": 0}) + "\n")
cands = P["load_candidate_mints"](cpath)
check("peak: candidate mints loaded (valid line only)",
      list(cands) == ["MINT1"] and cands["MINT1"]["alert_mcap"] == 50000)
check("peak: candidate loader fail-soft on missing file",
      P["load_candidate_mints"](os.path.join(tmp, "nope.jsonl")) == {})
outs = {"O1": {"alert_ts": now - 100, "alert_mcap": 60000},
        "O2": {"alert_ts": now - 200, "alert_mcap": 70000},
        "Oold": {"alert_ts": now - 100 * 3600, "alert_mcap": 70000}}
cands2 = {"Cnew": {"alert_ts": now - 50, "alert_mcap": 55000},
          "Cold": {"alert_ts": now - 5000, "alert_mcap": 55000},
          "O1": {"alert_ts": now - 90, "alert_mcap": 60000}}
poll = P["build_poll_set"](outs, cands2, now, cap=3)
check("peak: poll set = fresh outcomes first, newest candidates fill cap",
      set(poll) == {"O1", "O2", "Cnew"} and len(poll) == 3)
poll2 = P["build_poll_set"](outs, cands2, now, cap=300)
check("peak: poll set under cap includes all fresh, excludes stale",
      set(poll2) == {"O1", "O2", "Cnew", "Cold"})

# --- 4. degen-feed counterfactual logging ------------------------------
F = load_script("degen-feed")
fpath = os.path.join(tmp, "degen_candidates.jsonl")
F["CANDIDATES_FILE"] = fpath
pool = {"mint": "MintX", "name": "CF", "fdv": 80000.0,
        "reserve": 30000.0, "age_min": 4.0,
        "txns_m5": {"buys": 12, "sells": 4, "buyers": 9},
        "txns_m15": {"buys": 30, "sells": 10},
        "chg_m5": 12.0, "chg_m15": 20.0, "chg_h1": 30.0}
F["log_candidate"](pool, "hard_filter", "runner: liq < $25k",
                    score=41.5, components={"freshness": 12.5})
F["log_candidate"](pool, "fired", "", score=72.0,
                    components={"freshness": 20.0})
with open(fpath) as f:
    lines = [json.loads(x) for x in f if x.strip()]
check("feed: candidate log wrote both records", len(lines) == 2)
r0 = lines[0]
check("feed: candidate record carries features + decision",
      r0["decision"] == "hard_filter" and r0["score"] == 41.5 and
      r0["components"]["freshness"] == 12.5 and r0["buys_m5"] == 12 and
      r0["chg_m15"] == 20.0)
check("feed: candidate record mirrors record_alert field names",
      r0["alert_mcap"] == 80000.0 and r0["alert_ts"] > 0 and
      r0["mint"] == "MintX")
# bounded rotation: tiny byte cap + keep=3 -> file trims to newest 3
F["CANDIDATES_MAX_BYTES"] = 10
F["CANDIDATES_KEEP"] = 3
for i in range(6):
    F["log_candidate"](pool, "sub_threshold", "x", score=float(i),
                        components={})
with open(fpath) as f:
    kept = [json.loads(x) for x in f if x.strip()]
check("feed: candidate log bounded by rotation", len(kept) == 3)
check("feed: rotation keeps the NEWEST records",
      kept[-1]["score"] == 5.0)
# fail-soft: unwritable path must not raise
F["CANDIDATES_FILE"] = os.path.join(tmp, "no_such_dir", "x.jsonl")
try:
    F["log_candidate"](pool, "fired", "", score=1.0, components={})
    check("feed: candidate log fail-soft on bad path", True)
except Exception:
    check("feed: candidate log fail-soft on bad path", False)
# _candidate_score is gate-free: a runner-gate reject still scores > 0
dusty = dict(pool, reserve=100.0, fdv=1000.0)
cs, cc = F["_candidate_score"](dusty)
check("feed: _candidate_score scores hard rejects (skip_gates)",
      cs is not None and cs > 0 and "freshness" in cc)
ok, _why = F["passes_hard_filters"](dusty, F["get_weights"]())
check("feed: hard filters still reject the same pool (unchanged)",
      not ok)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
