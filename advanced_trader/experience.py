"""Experience store — the bot learns from its own closed trades.

Every closed, journaled trade is recorded here, keyed by the dimensions
that actually predict the next trade: signal source, deployer, lane,
entry pattern, and holding-time bucket. Open positions are never
recorded; guesses are never recorded. Only closed trades teach.

How the learning is used (in ``trader.evaluate``): a candidate's stake
is multiplied by the experience weight of its matching keys, taking
the *minimum* across dimensions (most pessimistic wins):

* No experience for a key -> weight 1.0 (neutral, trade normally).
* Positive expectancy with enough data -> up to ``experience_weight_max``
  (default 1.25x size, still inside the Kelly caps).
* Negative expectancy with enough data -> 0.5 (down-weighted).
* ``experience_block_streak`` consecutive losses on a key -> 0.0: the
  pattern is blocked entirely until the store is reset by a human.

Persistence is a single atomic JSON file (same temp-file + ``os.replace``
pattern as the ticket counter). No database, no shared-DB assumptions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from .config import TraderConfig

DIMENSIONS = ("source", "deployer", "lane", "pattern", "hold_bucket")


def hold_bucket(hold_seconds: float) -> str:
    """Bucket a holding time for experience keys."""
    if hold_seconds < 3600:
        return "<1h"
    if hold_seconds < 6 * 3600:
        return "1-6h"
    if hold_seconds < 24 * 3600:
        return "6-24h"
    return ">24h"


class ExperienceStore:
    """Persistent per-key outcome stats. Atomic JSON, no database."""

    def __init__(
        self, state_file: str | Path | None = None, config: TraderConfig | None = None
    ) -> None:
        self.config = config or TraderConfig()
        self._file = Path(state_file or self.config.experience_state_file)
        # {dimension: {key: {"n": int, "wins": int, "pnl": float,
        #                    "streak_losses": int}}}
        self._stats: Dict[str, Dict[str, Dict[str, Any]]] = {
            dim: {} for dim in DIMENSIONS
        }
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text())
        except (ValueError, OSError) as exc:
            raise ValueError(
                f"experience state file {self._file} is corrupt: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise ValueError(f"experience state file {self._file} is corrupt")
        for dim in DIMENSIONS:
            bucket = data.get(dim, {})
            if isinstance(bucket, dict):
                self._stats[dim] = {
                    str(k): {
                        "n": int(v.get("n", 0)),
                        "wins": int(v.get("wins", 0)),
                        "pnl": float(v.get("pnl", 0.0)),
                        "streak_losses": int(v.get("streak_losses", 0)),
                    }
                    for k, v in bucket.items()
                    if isinstance(v, dict)
                }

    def _save(self) -> None:
        tmp = self._file.with_suffix(self._file.suffix + ".tmp")
        tmp.write_text(json.dumps(self._stats))
        os.replace(tmp, self._file)

    # -- recording (closed trades only) -----------------------------------
    def record(self, outcome: Dict[str, Any]) -> None:
        """Record one CLOSED trade. ``outcome`` needs ``pnl`` (net, after
        costs); optional keys: ``source``, ``deployer``, ``lane``,
        ``pattern``, ``hold_seconds``. Only closed, journaled trades
        reach this method — never open positions, never guesses."""
        pnl = float(outcome["pnl"])
        won = pnl > 0
        keys: Dict[str, str] = {}
        for dim in ("source", "deployer", "lane", "pattern"):
            value = outcome.get(dim)
            if value:
                keys[dim] = str(value)
        if "hold_seconds" in outcome and outcome["hold_seconds"] is not None:
            keys["hold_bucket"] = hold_bucket(float(outcome["hold_seconds"]))
        for dim, key in keys.items():
            entry = self._stats[dim].setdefault(
                key, {"n": 0, "wins": 0, "pnl": 0.0, "streak_losses": 0}
            )
            entry["n"] += 1
            entry["pnl"] += pnl
            if won:
                entry["wins"] += 1
                entry["streak_losses"] = 0
            else:
                entry["streak_losses"] += 1
        self._save()

    # -- reading ------------------------------------------------------------
    def stats(self, dimension: str, key: str) -> Dict[str, Any] | None:
        """Raw stats for one key, or None if never seen."""
        entry = self._stats.get(dimension, {}).get(str(key))
        if entry is None:
            return None
        n = entry["n"]
        return {
            "n": n,
            "wins": entry["wins"],
            "hit_rate": (entry["wins"] / n) if n else 0.0,
            "expectancy": (entry["pnl"] / n) if n else 0.0,
            "pnl": entry["pnl"],
            "streak_losses": entry["streak_losses"],
        }

    def _weight_for_key(self, dimension: str, key: str) -> float:
        entry = self._stats.get(dimension, {}).get(str(key))
        if entry is None:
            return 1.0  # no experience: neutral
        if entry["streak_losses"] >= self.config.experience_block_streak:
            return 0.0  # blocked: this pattern keeps losing
        n = entry["n"]
        if n < self.config.experience_min_trades:
            return 1.0  # not enough data to judge
        expectancy = entry["pnl"] / n
        if expectancy < 0:
            return 0.5  # down-weight losing patterns
        if expectancy > 0:
            return float(self.config.experience_weight_max)
        return 1.0

    def weight_for(self, keys: Dict[str, str]) -> float:
        """Combined weight for a candidate: the minimum across every
        dimension with experience. One bad dimension vetoes the trade
        (weight 0.0 blocks it); winning dimensions can only raise the
        weight to ``experience_weight_max``."""
        weight = float(self.config.experience_weight_max)
        seen_any = False
        for dim, key in keys.items():
            if dim not in DIMENSIONS or not key:
                continue
            if self._stats.get(dim, {}).get(str(key)) is None:
                continue
            seen_any = True
            weight = min(weight, self._weight_for_key(dim, str(key)))
        if not seen_any:
            return 1.0
        return weight

    def all_stats(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Full store, for reporting and audit."""
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for dim, bucket in self._stats.items():
            out[dim] = {
                key: self.stats(dim, key)  # type: ignore[assignment]
                for key in bucket
            }
        return out
