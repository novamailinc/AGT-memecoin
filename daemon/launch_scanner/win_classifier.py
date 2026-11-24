#!/usr/bin/env python3
"""Win-probability classifier for paper-trade setups.

Replaces the fixed score bar with a calibrated P(win) per setup — trained
on the system's OWN fills history, not price prediction (which the
literature says is worthless at short horizons).

Design (from 2026-10-15 AI-tools research):
- L2 logistic regression ONLY. With ~414 samples, anything fancier
  (XGBoost, nets) memorizes noise. Logistic is the right bias-variance
  tradeoff at this sample size.
- Entry-time features ONLY. Hold duration, exit type, peak multiple —
  anything computed post-entry — is leakage and must NEVER be a feature.
- Calibrated via cross-validated Platt scaling so P(win)=0.31 means
  ~31% of such setups actually win.
- Advisory-only until walk-forward OOS AUC >= 0.55 on 200+ samples.

Feature set (v1 — from fills journal fields):
  volume_sol, price_sol (log), tokens (log), strategy tag (one-hot),
  hour of day WAT (cyclic sin/cos), weekday (cyclic sin/cos),
  symbol length, symbol digit count.

Feature set (v2 — journaled going forward, see OrderRequest.features):
  score components at entry (drawdown, smart cluster, vol, liq, bounce),
  token age at entry, mcap bucket, liq/mcap ratio.
  v2 features are read from the fill's "features" dict when present;
  v1 features are always computed. The model trains on the union,
  with missing v2 features as 0 (and a has_v2_features indicator).
"""

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.metrics import roc_auc_score
    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False

HOME = Path(os.environ.get("FOREX_AGENT_HOME", str(Path.home() / ".forex-agent")))
FILLS_PATH = HOME / "data" / "memecoin" / "fills.jsonl"
MODEL_PATH = HOME / "launch_scanner" / "win_classifier.json"

# v1 feature names (always available from fills journal)
V1_FEATURES = [
    "volume_sol",
    "log_price_sol",
    "log_tokens",
    "hour_sin", "hour_cos",       # entry hour WAT, cyclic
    "wday_sin", "wday_cos",       # entry weekday, cyclic
    "symbol_len",
    "symbol_digits",
]
# strategy tags observed in fills (one-hot; unknown -> all zeros)
STRATEGY_TAGS = [
    "degen-launch",
    "degen-launch-B",
    "degen-second-wave",
    "degen-second-wave-B",
    "degen-late-runner",
]
# v2 rich features (journaled going forward; 0 + indicator when absent)
V2_FEATURES = [
    "score_drawdown",
    "score_smart",
    "score_vol",
    "score_liq",
    "score_bounce",
    "token_age_min",
    "log_mcap",
    "liq_mcap_ratio",
]
ALL_FEATURES = (V1_FEATURES
                + ["tag_" + t for t in STRATEGY_TAGS]
                + V2_FEATURES
                + ["has_v2_features"])

# Kill criteria (from research phased plan)
MIN_OOS_AUC = 0.55
MIN_OOS_SAMPLES = 200


def _wat_hour_wday(ts_str):
    """Entry hour (WAT = UTC+1) and weekday from ISO timestamp."""
    try:
        dt = datetime.fromisoformat(str(ts_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # WAT = UTC+1
        wat_hour = (dt.hour + 1) % 24
        wday = (dt.weekday() + 1) % 7  # shift so Monday=0 stays sane-ish
        return wat_hour, dt.weekday()
    except Exception:
        return 12, 2  # neutral defaults


def extract_features(buy):
    """Build feature vector from a BUY fill record. Entry-time only."""
    f = {}
    f["volume_sol"] = float(buy.get("volume_sol") or 0)
    price = float(buy.get("price_sol") or 0)
    f["log_price_sol"] = math.log10(price) if price > 0 else -8.0
    toks = float(buy.get("tokens") or 0)
    f["log_tokens"] = math.log10(toks) if toks > 0 else 0.0

    hour, wday = _wat_hour_wday(buy.get("ts"))
    f["hour_sin"] = math.sin(2 * math.pi * hour / 24)
    f["hour_cos"] = math.cos(2 * math.pi * hour / 24)
    f["wday_sin"] = math.sin(2 * math.pi * wday / 7)
    f["wday_cos"] = math.cos(2 * math.pi * wday / 7)

    sym = str(buy.get("symbol") or "")
    f["symbol_len"] = len(sym)
    f["symbol_digits"] = sum(c.isdigit() for c in sym)

    tag = str(buy.get("comment") or "")
    for t in STRATEGY_TAGS:
        f["tag_" + t] = 1.0 if tag == t else 0.0

    # v2 rich features (journaled going forward)
    rich = buy.get("features") or {}
    has_v2 = bool(rich)
    for vf in V2_FEATURES:
        v = rich.get(vf)
        try:
            f[vf] = float(v) if v is not None else 0.0
        except (TypeError, ValueError):
            f[vf] = 0.0
    # log_mcap needs log transform like price
    if has_v2 and f.get("log_mcap"):
        try:
            mc = float(rich.get("log_mcap") or 0)
            f["log_mcap"] = math.log10(mc) if mc > 0 else 0.0
        except (TypeError, ValueError):
            f["log_mcap"] = 0.0
    f["has_v2_features"] = 1.0 if has_v2 else 0.0

    return [f[name] for name in ALL_FEATURES]


def load_labeled_trades(fills_path=None):
    """Load (features, label, date) for all matched BUY->CLOSE pairs.

    Label: 1 if realized_pnl_sol > 0 else 0.
    Skips corrupted records (CLOSE before BUY, duplicates).
    Returns (X, y, dates) as lists.
    """
    path = Path(fills_path) if fills_path else FILLS_PATH
    buys = {}
    closes = []
    seen = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        t, k = d.get("ticket"), d.get("kind")
        if t is None or not k:
            continue
        if (t, k) in seen:
            continue  # duplicate
        seen.add((t, k))
        if k == "BUY":
            buys[t] = d
        elif k == "CLOSE":
            closes.append(d)

    X, y, dates = [], [], []
    for c in closes:
        b = buys.get(c.get("ticket"))
        if not b:
            continue
        # temporal hygiene: skip CLOSE before BUY
        try:
            bts = datetime.fromisoformat(str(b.get("ts")))
            cts = datetime.fromisoformat(str(c.get("ts")))
            if cts < bts:
                continue
        except Exception:
            pass
        pnl = c.get("realized_pnl_sol") or 0
        try:
            label = 1 if float(pnl) > 0 else 0
        except (TypeError, ValueError):
            continue
        X.append(extract_features(b))
        y.append(label)
        dates.append(str(b.get("ts") or ""))
    return X, y, dates


class WinClassifier:
    """Calibrated P(win) model. Advisory-only until walk-forward validates."""

    def __init__(self):
        self.model = None
        self.trained_at = 0
        self.oos_auc = 0.0
        self.oos_n = 0
        self.n_train = 0

    def train(self, X, y):
        """Train L2 logistic + Platt calibration. Returns self."""
        if not _HAS_SKLEARN:
            raise RuntimeError("scikit-learn not available")
        X = np.array(X, dtype=float)
        y = np.array(y, dtype=int)
        base = LogisticRegression(
            C=1.0, max_iter=1000, random_state=42)
        # cv=3 for calibration (small sample); cv="prefit" not needed here
        self.model = CalibratedClassifierCV(base, method="sigmoid", cv=3)
        self.model.fit(X, y)
        self.trained_at = time.time()
        self.n_train = len(y)
        return self

    def predict_proba(self, features):
        """P(win) for one feature vector. 0.5 when untrained."""
        if self.model is None or not _HAS_SKLEARN:
            return 0.5
        p = self.model.predict_proba(
            np.array([features], dtype=float))[0]
        return float(p[1]) if len(p) > 1 else 0.5

    def predict_for_buy(self, buy):
        """P(win) directly from a BUY fill record."""
        return self.predict_proba(extract_features(buy))

    def to_dict(self):
        if self.model is None:
            return {"trained": False}
        # persist the underlying logistic coefs (calibrated wrapper
        # isn't trivially serializable; re-calibrate on load is overkill
        # for advisory mode — store base estimator params)
        cal = self.model
        return {
            "trained": True,
            "trained_at": self.trained_at,
            "n_train": self.n_train,
            "oos_auc": self.oos_auc,
            "oos_n": self.oos_n,
            "feature_names": ALL_FEATURES,
        }

    def save(self, path=None):
        import pickle
        p = Path(path) if path else MODEL_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            pickle.dump(self, f)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass

    @classmethod
    def load(cls, path=None):
        import pickle
        p = Path(path) if path else MODEL_PATH
        try:
            with open(p, "rb") as f:
                obj = pickle.load(f)
            return obj if isinstance(obj, cls) else cls()
        except Exception:
            return cls()
