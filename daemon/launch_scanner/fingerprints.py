"""Early-fingerprint toolkit — rising buys, holder growth, volume acceleration.

Catches the pre-viral fingerprints of a runner while it is still small:

* **buy acceleration** — latest 5m buys vs the mean of earlier polls
* **volume acceleration** — latest 5m volume vs the mean of earlier polls
* **holder growth** — RugCheck ``totalHolders``, first vs latest observation

``FingerprintTracker`` keeps a short per-mint time series of DexScreener
stats (pure logic, no I/O). Holder counts come from
``fetch_total_holders`` (one small RugCheck call); the daemon refreshes
them opportunistically and rate-limited, only for low-cap-tracked
Solana mints.

``score()`` returns a dict with the three fingerprints, buy pressure, a
trailing rising-buy streak and a composite 0-100 score:

* HOT     — composite >= fp_hot_score   (default 70)
* WARMING — composite >= fp_warm_score  (default 45)
* COLD    — everything else

Acceleration ratios are floored so tiny bases (1 -> 3 buys) don't fake a
signal, and capped so one crazy poll can't max the score alone.
"""

from __future__ import annotations

import json
import os
import urllib.request
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

_RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

# Hard cap on tracked mints so daemon state stays small.
MAX_TRACKED = 500

# Durable series journal (2026-11-23, Stage A audit F3): every
# observation is ALSO appended to a bounded jsonl journal so backtest
# records can carry the FULL series from detection, not the truncated
# in-memory deque. Bounded: entries older than the retention window
# are compacted away; compaction runs at most every _COMPACT_EVERY
# appends and only when the file exceeds _COMPACT_MIN_BYTES. Historical
# data recorded before the journal existed is window-truncated and
# irrecoverable - records flag it, never reconstruct it.
_JOURNAL_RETENTION_S = 6 * 3600.0
_COMPACT_EVERY = 256
_COMPACT_MIN_BYTES = 1_000_000
_JOURNAL_DEFAULT = "~/.forex-agent/launch_scanner/fp_series_journal.jsonl"


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_total_holders(mint: str, timeout_s: float = 4.0) -> Optional[int]:
    """RugCheck totalHolders for a Solana mint. None on any trouble."""
    if not mint:
        return None
    try:
        req = urllib.request.Request(
            _RUGCHECK_URL.format(mint=mint),
            headers={"User-Agent": "forex-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        total = int(data.get("totalHolders") or 0)
        return total if total > 0 else None
    except Exception:
        return None


class FingerprintTracker:
    """Per-mint acceleration time series + composite fingerprint score."""

    def __init__(self, cfg):
        self.cfg = cfg  # retained so owners can detect config swaps
        self.enabled = bool(getattr(cfg, "fp_enabled", True))
        self.window = max(3, int(getattr(cfg, "fp_window_polls", 6)))
        self.min_polls = max(2, int(getattr(cfg, "fp_min_polls", 3)))
        self.buy_floor = float(getattr(cfg, "fp_buy_floor", 3.0))
        self.vol_floor = float(getattr(cfg, "fp_vol_floor_usd", 1000.0))
        self.hot_score = float(getattr(cfg, "fp_hot_score", 70.0))
        self.warm_score = float(getattr(cfg, "fp_warm_score", 45.0))
        self.max_age_s = 3600.0
        self._series: Dict[str, Deque[Tuple[float, ...]]] = {}
        # obs tuple: (ts, mcap, price, vol_m5, buys, sells, liq, holders|None)
        # Durable full-series journal (audit F3). Never raises.
        self._journal_enabled = bool(
            getattr(cfg, "fp_series_journal_enabled", True))
        self._journal_path = os.path.expanduser(str(
            getattr(cfg, "fp_series_journal_path", _JOURNAL_DEFAULT)))
        self._journal_retention_s = float(
            getattr(cfg, "fp_series_journal_retention_s",
                    _JOURNAL_RETENTION_S))
        self._journal_appends = 0

    # -- state hygiene ------------------------------------------------
    def prune(self, now: float) -> None:
        now_f = float(now)
        for mint in list(self._series):
            q = self._series[mint]
            while q and now_f - q[0][0] > self.max_age_s:
                q.popleft()
            if not q:
                self._series.pop(mint, None)
        if len(self._series) > MAX_TRACKED:
            ordered = sorted(self._series.items(), key=lambda kv: kv[1][0][0])
            for mint, _ in ordered[:len(self._series) - MAX_TRACKED]:
                self._series.pop(mint, None)

    # -- main entry ----------------------------------------------------
    def observe(self, mint: str, stats: Dict[str, Any], now: float,
                holders: Optional[int] = None) -> None:
        """Append one DexScreener-stats observation for a mint."""
        if not self.enabled or not mint or not stats:
            return
        q = self._series.get(mint)
        if q is None:
            q = self._series[mint] = deque(maxlen=self.window)
        obs = (float(now), _f(stats.get("usd_market_cap")),
               _f(stats.get("price_usd")), _f(stats.get("vol_m5")),
               float(stats.get("buys_m5") or 0),
               float(stats.get("sells_m5") or 0),
               _f(stats.get("usd_liquidity")),
               float(holders) if holders else None)
        q.append(obs)
        self._journal_append(mint, obs)

    # -- durable full-series journal (audit F3) ------------------------
    def _journal_append(self, mint: str, obs: Tuple[float, ...]) -> None:
        """Append one observation to the durable journal. Crash-safe:
        never raises; journal trouble never affects live tracking."""
        if not self._journal_enabled:
            return
        try:
            parent = os.path.dirname(self._journal_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self._journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"mint": mint, "obs": list(obs)})
                         + "\n")
            self._journal_appends += 1
            if self._journal_appends % _COMPACT_EVERY == 0:
                self._journal_compact(obs[0])
        except Exception:
            pass

    def _journal_compact(self, now: float) -> None:
        """Drop entries older than the retention window (bounded
        durable storage). Never raises."""
        try:
            if not os.path.exists(self._journal_path):
                return
            if os.path.getsize(self._journal_path) < _COMPACT_MIN_BYTES:
                return
            cutoff = float(now) - self._journal_retention_s
            kept: List[str] = []
            with open(self._journal_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if _f((rec.get("obs") or [0])[0]) >= cutoff:
                            kept.append(line)
                    except Exception:
                        continue  # malformed line: drop on compaction
            tmp = self._journal_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(kept) + ("\n" if kept else ""))
            os.replace(tmp, self._journal_path)
        except Exception:
            pass

    def journal_series(self, mint: str,
                       now: Optional[float] = None
                       ) -> Optional[List[Tuple[float, ...]]]:
        """Full journaled series for one mint (oldest first), or None
        when the journal is disabled/unavailable/has nothing - callers
        then fall back to the truncated in-memory window and MUST flag
        the record window-truncated. Tolerates malformed lines."""
        if not self._journal_enabled or not mint:
            return None
        out: List[Tuple[float, ...]] = []
        try:
            with open(self._journal_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("mint") != mint:
                        continue
                    obs = rec.get("obs")
                    if isinstance(obs, list) and len(obs) == 8:
                        out.append(tuple(_f(v) if i != 7 or v is not None
                                         else None
                                         for i, v in enumerate(obs)))
        except Exception:
            return None
        out.sort(key=lambda o: o[0])
        return out or None

    def observations(self, mint: str) -> List[Tuple[float, ...]]:
        """Read-only copy of the raw per-mint observation series.

        Consumed by the breakout detector (breakouts.py) so it reuses
        this time series instead of inventing a parallel one. The
        returned list is a copy — mutating it never affects the tracker.
        """
        q = self._series.get(mint)
        return list(q) if q else []

    def score(self, mint: str) -> Optional[Dict[str, Any]]:
        """Composite fingerprint for a mint, or None when too early."""
        q = self._series.get(mint)
        if not q or len(q) < self.min_polls:
            return None
        obs = list(q)
        last = obs[-1]
        prev = obs[:-1]

        def _mean(idx: int) -> float:
            return sum(o[idx] for o in prev) / max(1, len(prev))

        # Acceleration vs the earlier-poll mean, floored and capped.
        buy_accel = min(9.99, last[4] / max(self.buy_floor, _mean(4)))
        vol_accel = min(9.99, last[3] / max(self.vol_floor, _mean(3)))

        # Holder growth: first vs latest non-None holder observation.
        h_obs = [o[7] for o in obs if o[7] is not None]
        holder_growth_pct: Optional[float] = None
        if len(h_obs) >= 2 and h_obs[0] > 0:
            holder_growth_pct = max(-999.0, min(
                999.0, (h_obs[-1] - h_obs[0]) / h_obs[0] * 100.0))

        buys_all = sum(o[4] for o in obs)
        sells_all = sum(o[5] for o in obs)
        buy_pressure = buys_all / max(1.0, buys_all + sells_all)

        rising_buys = 0
        for i in range(len(obs) - 1, 0, -1):
            if obs[i][4] > obs[i - 1][4]:
                rising_buys += 1
            else:
                break

        parts: List[Tuple[float, float]] = [
            (30.0, min(buy_accel, 4.0) / 4.0),
            (25.0, min(vol_accel, 4.0) / 4.0),
            (15.0, buy_pressure),
            (10.0, min(rising_buys, 3) / 3.0),
        ]
        if holder_growth_pct is not None:
            hg_norm = max(-1.0, min(1.0, holder_growth_pct / 50.0))
            parts.append((20.0, (hg_norm + 1.0) / 2.0))
        total_w = sum(w for w, _ in parts)
        composite = round(sum(w * v for w, v in parts) / total_w * 100.0, 1)

        if composite >= self.hot_score:
            verdict = "HOT"
        elif composite >= self.warm_score:
            verdict = "WARMING"
        else:
            verdict = "COLD"
        return {
            "composite": composite,
            "verdict": verdict,
            "buy_accel": round(buy_accel, 2),
            "vol_accel": round(vol_accel, 2),
            "holder_growth_pct": (round(holder_growth_pct, 1)
                                  if holder_growth_pct is not None else None),
            "buy_pressure": round(buy_pressure, 3),
            "rising_buys": rising_buys,
            "polls": len(obs),
        }
