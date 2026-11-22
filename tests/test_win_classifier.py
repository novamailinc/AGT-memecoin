#!/usr/bin/env python3
"""Tests for win_classifier and walk_forward.

The classifier must NEVER use post-entry information (leakage).
The walk-forward harness must reject bad models honestly.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "daemon", "launch_scanner"))

from win_classifier import (
    extract_features, load_labeled_trades, WinClassifier,
    ALL_FEATURES, _HAS_SKLEARN,
)
from walk_forward import walk_forward, verdict

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (" " + detail if detail and not cond else ""))


# --- feature extraction: entry-time only, no leakage ---
buy = {
    "ticket": 1, "kind": "BUY", "symbol": "TEST123",
    "volume_sol": 0.025, "price_sol": 1e-6, "tokens": 25000,
    "comment": "degen-launch", "ts": "2026-10-15T14:30:00+00:00",
}
f = extract_features(buy)
check("feature vector length matches ALL_FEATURES", len(f) == len(ALL_FEATURES),
      f"{len(f)} vs {len(ALL_FEATURES)}")
check("no NaN/inf in features", all(
    isinstance(x, (int, float)) and x == x and abs(x) != float("inf")
    for x in f))
# hour 14 UTC = 15 WAT
check("strategy tag one-hot", f[ALL_FEATURES.index("tag_degen-launch")] == 1.0)
check("other tags zero", f[ALL_FEATURES.index("tag_degen-launch-B")] == 0.0)
check("has_v2_features=0 when absent",
      f[ALL_FEATURES.index("has_v2_features")] == 0.0)

# v2 rich features
buy2 = dict(buy, features={
    "score_drawdown": 35, "score_smart": 25, "token_age_min": 45,
    "log_mcap": 500000, "liq_mcap_ratio": 0.2,
})
f2 = extract_features(buy2)
check("v2 features picked up",
      f2[ALL_FEATURES.index("score_drawdown")] == 35.0)
check("has_v2_features=1 when present",
      f2[ALL_FEATURES.index("has_v2_features")] == 1.0)
check("log_mcap log-transformed",
      abs(f2[ALL_FEATURES.index("log_mcap")] - 5.699) < 0.01)

# --- leakage guard: these must NEVER be features ---
leak_words = ["hold", "duration", "exit", "peak", "pnl", "realized",
              "close", "multiple"]
check("no leakage feature names",
      not any(any(w in name for w in leak_words) for name in ALL_FEATURES),
      str([n for n in ALL_FEATURES
           if any(w in n for w in leak_words)]))

# --- walk-forward honesty ---
# synthetic: perfect signal (feature 0 = label), enough for KEEP verdict
N_SYN = 900
X = [[float(i % 2)] + [0.0] * (len(ALL_FEATURES) - 1) for i in range(N_SYN)]
y = [i % 2 for i in range(N_SYN)]
dates = ["2026-09-%02dT10:00:00+00:00" % ((i % 28) + 1) for i in range(N_SYN)]
if _HAS_SKLEARN:
    r = walk_forward(X, y, dates, val_frac=0.25)
    check("walk-forward runs", "oos_auc" in r, str(r.get("error")))
    if "oos_auc" in r:
        check("perfect signal -> high AUC", r["oos_auc"] > 0.9,
              f"AUC={r['oos_auc']}")
        check("verdict KEEP on good model", verdict(r).startswith("KEEP"),
              verdict(r))
    # random labels -> DISABLE or NEED-DATA, never KEEP on 200+ samples
    import random
    random.seed(7)
    yr = [random.randint(0, 1) for _ in range(250)]
    Xr = [[random.gauss(0, 1) for _ in range(len(ALL_FEATURES))]
          for _ in range(250)]
    dr = ["2026-09-%02dT10:00:00+00:00" % ((i % 28) + 1) for i in range(250)]
    rr = walk_forward(Xr, yr, dr, val_frac=0.25)
    v = verdict(rr)
    check("random labels never KEEP", not v.startswith("KEEP"), v)
else:
    print("SKIP sklearn-dependent walk-forward tests (no sklearn)")

# --- classifier trains and predicts ---
if _HAS_SKLEARN:
    clf = WinClassifier().train(X, y)
    p = clf.predict_proba(X[0])
    check("predict_proba in [0,1]", 0.0 <= p <= 1.0, str(p))
    check("untrained predicts 0.5",
          WinClassifier().predict_proba(X[0]) == 0.5)
else:
    print("SKIP classifier tests (no sklearn)")

print(f"\n{len(FAIL)} failures")
sys.exit(1 if FAIL else 0)
