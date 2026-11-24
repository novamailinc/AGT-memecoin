#!/usr/bin/env python3
"""Walk-forward validation harness for the win classifier.

The discipline that prevents self-deception (2026-10-15 AI-tools research):
- Chronological splits ONLY (no shuffling — time series).
- Train on past, validate on future the model never saw.
- Expanding window with the most recent 25% as the validation fold.
- Verdict: KEEP (OOS AUC >= 0.55) or DISABLE (fall back to fixed bar).

Usage:
    python3 walk_forward.py [--fills PATH] [--min-samples N]

Output: OOS AUC, calibration table (predicted vs actual per decile),
and a one-line verdict.
"""

import argparse
import math
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from win_classifier import (
    load_labeled_trades, WinClassifier, MIN_OOS_AUC, MIN_OOS_SAMPLES,
    _HAS_SKLEARN,
)


def _parse_ts(s):
    try:
        dt = datetime.fromisoformat(str(s))
        return dt.timestamp()
    except Exception:
        return 0.0


def walk_forward(X, y, dates, val_frac=0.25):
    """Single expanding-window split: train on oldest (1-val_frac),
    validate on newest val_frac. Returns dict with oos_auc etc."""
    n = len(y)
    if n < 20:
        return {"error": "n=%d too small" % n}
    order = sorted(range(n), key=lambda i: _parse_ts(dates[i]))
    Xs = [X[i] for i in order]
    ys = [y[i] for i in order]
    cut = int(n * (1 - val_frac))
    Xtr, ytr = Xs[:cut], ys[:cut]
    Xva, yva = Xs[cut:], ys[cut:]
    if len(set(ytr)) < 2 or len(set(yva)) < 2:
        return {"error": "single-class fold"}
    clf = WinClassifier().train(Xtr, ytr)
    probs = [clf.predict_proba(x) for x in Xva]
    try:
        from sklearn.metrics import roc_auc_score
        auc = float(roc_auc_score(yva, probs))
    except Exception:
        auc = 0.5
    # calibration per decile
    cal = []
    idx = sorted(range(len(probs)), key=lambda i: probs[i])
    dec = max(1, len(idx) // 10)
    for d in range(10):
        sl = idx[d * dec:(d + 1) * dec]
        if not sl:
            continue
        pp = sum(probs[i] for i in sl) / len(sl)
        ap = sum(yva[i] for i in sl) / len(sl)
        cal.append((round(pp, 3), round(ap, 3), len(sl)))
    return {
        "n_train": len(ytr), "n_val": len(yva),
        "train_wr": round(sum(ytr) / len(ytr), 3),
        "val_wr": round(sum(yva) / len(yva), 3),
        "oos_auc": round(auc, 3),
        "calibration": cal,
    }


def verdict(result):
    """KEEP / DISABLE / NEED-DATA one-liner."""
    if "error" in result:
        return "NEED-DATA (%s)" % result["error"]
    if result["n_val"] < MIN_OOS_SAMPLES:
        return ("NEED-DATA (n_val=%d < %d; AUC=%.3f, directionally %s)"
                % (result["n_val"], MIN_OOS_SAMPLES, result["oos_auc"],
                   "promising" if result["oos_auc"] >= MIN_OOS_AUC
                   else "weak"))
    if result["oos_auc"] >= MIN_OOS_AUC:
        return "KEEP (OOS AUC %.3f >= %.2f on n=%d)" % (
            result["oos_auc"], MIN_OOS_AUC, result["n_val"])
    return "DISABLE (OOS AUC %.3f < %.2f — fixed bar stays)" % (
        result["oos_auc"], MIN_OOS_AUC)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fills", default="")
    ap.add_argument("--val-frac", type=float, default=0.25)
    args = ap.parse_args()
    if not _HAS_SKLEARN:
        print("scikit-learn not available")
        sys.exit(2)
    X, y, dates = load_labeled_trades(args.fills or None)
    print(f"labeled trades: n={len(X)}, win_rate={sum(y)/len(y):.1%}"
          if y else "no labeled trades")
    if not y:
        sys.exit(1)
    r = walk_forward(X, y, dates, val_frac=args.val_frac)
    if "error" in r:
        print("ERROR:", r["error"])
        sys.exit(1)
    print(f"train n={r['n_train']} (WR {r['train_wr']:.1%}) | "
          f"val n={r['n_val']} (WR {r['val_wr']:.1%})")
    print(f"OOS AUC: {r['oos_auc']}")
    print("calibration (predicted -> actual, n):")
    for pp, ap_, n in r["calibration"]:
        print(f"  {pp:.2f} -> {ap_:.2f} (n={n})")
    print("VERDICT:", verdict(r))


if __name__ == "__main__":
    main()
