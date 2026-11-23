"""Backtesting engine — replay recorded launch histories through entry/exit rules.

The live scanner records one JSON line per closed candidate via
``record_history()`` (see ``scripts/backtest``). ``BacktestEngine``
replays those histories through configurable entry and exit rules and
produces a compact performance report: win rate, average/median
multiple, profit factor, expectancy, max drawdown, plus per-source and
per-narrative attribution and per-gate block counts.

This module is deliberately standalone: pure logic, stdlib only, no
imports from the live scanner, no network, no secrets. The daemon's
safety gates (insider clusters, sell simulation, unknown-score
fail-closed) are enforced at record time; the backtester replays the
*recorded* safety verdicts and never re-scores anything.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
from typing import Any, Dict, List, Optional, Tuple

# Bump when the recorded-history record shape changes.
HISTORY_SCHEMA_VERSION = 1

# Replay semantics versions (2026-11-23). v1-legacy: the original replay,
# which could gate a simulated entry on a safety report refreshed AFTER
# the earliest retained snapshot (F1 lookahead) and gated on outcome.rug
# without provenance (F2 latent hazard). v2-timestamp-correct: entries
# only at/after safety_eval_t when a real safety score is present;
# records with a score but no trustworthy safety_eval_t are explicitly
# ineligible (never backfilled); rug labels gate only when their
# provenance time shows they existed at the earliest snapshot.
REPLAY_MODEL_V1 = "v1-legacy"
REPLAY_MODEL_V2 = "v2-timestamp-correct"
REPLAY_MODELS = (REPLAY_MODEL_V1, REPLAY_MODEL_V2)

_DEFAULT_ENTRY_RULES = {
    "min_safety_score": 50.0,
    "require_known_safety": True,
    "max_entry_mcap_usd": 500000.0,
}

_DEFAULT_EXIT_RULES = {
    # (multiple, fraction-of-original-position) sold on first touch.
    "tp_ladder": [(2.0, 0.5), (5.0, 0.25)],
    # From the peak, on the remainder, once peak reached >= 1.5x.
    "trailing_stop_pct": 30.0,
    "time_stop_h": 6.0,
}


def _f(value: Any) -> float:
    """Lenient float: None/garbage -> 0.0, never raises."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return v


def record_history(path: str, record: Dict[str, Any]) -> bool:
    """Append one history record as a JSON line. Crash-safe: never raises.

    Returns True on success, False on any I/O trouble.
    """
    try:
        p = os.path.expanduser(str(path))
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return True
    except Exception:
        return False


class BacktestEngine:
    """Replay histories through entry/exit rules. Standalone, no scanner."""

    def __init__(self, entry_rules: Optional[Dict[str, Any]] = None,
                 exit_rules: Optional[Dict[str, Any]] = None,
                 costs: Optional[Dict[str, Any]] = None,
                 seed: int = 0,
                 replay_model: str = REPLAY_MODEL_V2):
        if replay_model not in REPLAY_MODELS:
            raise ValueError("unknown replay_model %r (want one of %s)"
                             % (replay_model, ", ".join(REPLAY_MODELS)))
        self.replay_model = replay_model
        # Stage A cost model (request section 12). OFF by default so
        # historical reports stay comparable; when enabled every
        # simulated fill is dragged and charged:
        #   fee_bps            per swap leg (venue fee; pump.fun curve is
        #                      125 bps flat per the landed fee schedule)
        #   slippage_bps       assumed slippage per fill
        #   price_impact_bps   assumed price impact per fill
        #   fail_prob_entry    probability an entry execution is
        #                      unavailable (trade never opens)
        #   fail_prob_exit     probability an exit leg execution fails at
        #                      that snapshot (leg stays open)
        #   trade_notional_usd / token_account_cost_usd: fixed per-
        #                      position cost (ATA rent) in multiple terms
        # Fills are decided on OBSERVED prices and realized at dragged
        # prices: buys execute at price*(1+drag), sells at price*(1-drag).
        costs = dict(costs or {})
        self.costs_enabled = bool(costs)
        if self.costs_enabled and costs.get("fee_bps") is None:
            # Fail closed: an unknown venue fee must never silently
            # inherit the pump.fun curve rate (or zero). Pass an
            # explicit fee_bps - 0.0 is fine when DELIBERATE.
            raise ValueError(
                "cost model requires an explicit fee_bps (venue fee); "
                "unknown venue must not silently inherit a default")
        self.cost_fee_bps = _f(costs.get("fee_bps"))
        self._cfg_entry = dict(entry_rules or {})
        self._cfg_exit = dict(exit_rules or {})
        self._cfg_costs = dict(costs) if self.costs_enabled else None
        self._seed = seed
        self.cost_slippage_bps = _f(costs.get("slippage_bps"))
        self.cost_impact_bps = _f(costs.get("price_impact_bps"))
        self.cost_fail_entry = _f(costs.get("fail_prob_entry"))
        self.cost_fail_exit = _f(costs.get("fail_prob_exit"))
        self.cost_notional_usd = _f(costs.get("trade_notional_usd"))
        self.cost_ata_usd = _f(costs.get("token_account_cost_usd"))
        self._rng = random.Random(seed)
        entry = dict(_DEFAULT_ENTRY_RULES)
        if entry_rules:
            entry.update(entry_rules)
        exitr = dict(_DEFAULT_EXIT_RULES)
        if exit_rules:
            exitr.update(exit_rules)
        self.min_safety_score = _f(entry.get("min_safety_score"))
        self.require_known_safety = bool(entry.get("require_known_safety"))
        self.max_entry_mcap = _f(entry.get("max_entry_mcap_usd"))
        # tp_ladder: list of (mult, fraction), sorted by mult ascending.
        ladder = []
        for rung in exitr.get("tp_ladder") or []:
            try:
                m, frac = rung
                m, frac = _f(m), _f(frac)
                if m > 1.0 and frac > 0:
                    ladder.append((m, min(frac, 1.0)))
            except (TypeError, ValueError):
                continue
        self.tp_ladder: List[Tuple[float, float]] = sorted(ladder)
        self.trailing_stop_pct = _f(exitr.get("trailing_stop_pct"))
        self.time_stop_h = _f(exitr.get("time_stop_h"))

    # -- entry ---------------------------------------------------------
    def _history_gate(self, hist: Dict[str, Any],
                      snaps: List[Dict[str, Any]]) -> Tuple[bool, str, Dict[str, Any]]:
        """History-level gates. Returns (ok, block_reason, info).

        v2 rug guard (audit F2): a rug label may gate eligibility ONLY
        when its provenance shows it existed at the earliest snapshot.
        Exact provenance is outcome.recorded_t (written by the recorder);
        outcome.end_t is the fallback. A label with no provenance, or
        provenanced after the first snapshot, is a conditional/future
        label: it is kept OUT of trade eligibility (the trade is
        simulated as real-time logic would have seen it) and counted.
        """
        info: Dict[str, Any] = {}
        outcome = hist.get("outcome")
        if not isinstance(outcome, dict):
            outcome = {}
        if outcome.get("rug"):
            if self.replay_model == REPLAY_MODEL_V2:
                first_t = min(
                    (_f(snap.get("t")) for snap in snaps
                     if isinstance(snap, dict)), default=0.0)
                prov = outcome.get("recorded_t")
                if prov is None:
                    prov = outcome.get("end_t")
                if prov is None or _f(prov) > first_t:
                    info["rug_label_ignored"] = True
                else:
                    return False, "rug", info
            else:
                return False, "rug", info
        safety = hist.get("safety")
        if not isinstance(safety, dict):
            safety = {}
        if self.require_known_safety and (
                safety.get("unknown") or safety.get("score") is None):
            return False, "safety-unknown", info
        if _f(safety.get("score")) < self.min_safety_score:
            return False, "safety-score", info
        return True, "", info

    def _find_entry(self, snaps: List[Dict[str, Any]],
                    floor_t: Optional[float] = None
                    ) -> Tuple[Optional[int], bool]:
        """Index of the first snapshot satisfying entry rules, else None.

        v2 (audit F1): when floor_t is set (the safety report's
        evaluation time), snapshots before it are INELIGIBLE - the
        simulated entry may not use information that did not exist yet.
        Returns (index, blocked_by_floor): the second flag is True when
        an otherwise-valid entry existed only BEFORE the floor."""
        blocked_by_floor = False
        for i, snap in enumerate(snaps):
            if not isinstance(snap, dict):
                continue
            price = _f(snap.get("price_usd"))
            if price <= 0:
                continue
            if _f(snap.get("mcap_usd")) > self.max_entry_mcap:
                continue
            if floor_t is not None and _f(snap.get("t")) < floor_t:
                blocked_by_floor = True
                continue
            return i, blocked_by_floor
        return None, blocked_by_floor

    # -- exit simulation ------------------------------------------------
    def _simulate(self, snaps: List[Dict[str, Any]],
                  entry_idx: int) -> Dict[str, Any]:
        """Walk post-entry snapshots; TP ladder, trailing stop, time stop.

        With the cost model on, rungs still TRIGGER on observed prices
        (the strategy sees the market), but every fill REALIZES at a
        dragged price and pays the per-leg fee; a failed exit leg stays
        open for the next snapshot."""
        entry_snap = snaps[entry_idx]
        entry_price = _f(entry_snap.get("price_usd"))
        entry_t = _f(entry_snap.get("t"))
        deadline = entry_t + self.time_stop_h * 3600.0

        if self.costs_enabled:
            drag = (self.cost_slippage_bps + self.cost_impact_bps) / 1e4
            buy_drag = 1.0 + drag
            sell_drag = max(0.0, 1.0 - drag)
            fee = self.cost_fee_bps / 1e4
        else:
            buy_drag = sell_drag = 1.0
            fee = 0.0
        # Entry fill: worse price (buy_drag) and the entry-leg fee comes
        # out of the position itself.
        remainder = 1.0 * (1.0 - fee)
        proceeds = 0.0
        failed_exits = 0
        rung_hit = [False] * len(self.tp_ladder)
        peak = 1.0
        max_dd = 0.0
        exit_reason = "final"
        last_mult = 1.0

        def _try_sell(frac_pos: float, raw_mult: float) -> float:
            """Execute one exit leg; returns proceeds in entry-multiple
            terms, 0.0 when the execution fails (leg stays open)."""
            nonlocal failed_exits
            if self.costs_enabled and self._rng.random() < self.cost_fail_exit:
                failed_exits += 1
                return 0.0
            realized = raw_mult * sell_drag / buy_drag
            return frac_pos * realized * (1.0 - fee)

        for snap in snaps[entry_idx + 1:]:
            if not isinstance(snap, dict):
                continue
            price = _f(snap.get("price_usd"))
            if price <= 0:
                continue
            t = _f(snap.get("t"))
            mult = price / entry_price
            last_mult = mult
            peak = max(peak, mult)
            max_dd = max(max_dd, peak - mult)

            # TP ladder rungs, first touch, executed at the rung price.
            for i, (rung_mult, frac) in enumerate(self.tp_ladder):
                if not rung_hit[i] and mult >= rung_mult:
                    sell = min(frac, remainder)
                    got = _try_sell(sell, rung_mult)
                    if got > 0.0:
                        proceeds += got
                        remainder -= sell
                        rung_hit[i] = True
                    elif not self.costs_enabled:
                        proceeds += 0.0
                        remainder -= sell
                        rung_hit[i] = True
            if remainder <= 1e-12:
                remainder = 0.0
                exit_reason = "tp-all"
                break

            # Trailing stop on the remainder (armed once peak >= 1.5x).
            if (self.trailing_stop_pct > 0 and peak >= 1.5
                    and mult <= peak * (1.0 - self.trailing_stop_pct / 100.0)):
                got = _try_sell(remainder, mult)
                if got > 0.0 or not self.costs_enabled:
                    proceeds += got
                    remainder = 0.0
                    exit_reason = "trailing"
                    break

            # Time stop: sell the remainder at the deadline snapshot.
            if t >= deadline:
                got = _try_sell(remainder, mult)
                if got > 0.0 or not self.costs_enabled:
                    proceeds += got
                    remainder = 0.0
                    exit_reason = "time-stop"
                    break

        if remainder > 0:
            got = _try_sell(remainder, last_mult)
            if got > 0.0 or not self.costs_enabled:
                proceeds += got
                remainder = 0.0
            else:
                exit_reason = "exit-failed"
        # Fixed per-position cost (e.g. token-account rent) in multiple
        # terms, when a notional was supplied to express it against.
        if self.costs_enabled and self.cost_notional_usd > 0 and self.cost_ata_usd > 0:
            proceeds -= self.cost_ata_usd / self.cost_notional_usd
        return {
            # Entry provenance: the exact snapshot the replay entered on
            # (timestamp-correctness is auditable from the report).
            "entry_t": entry_t,
            "entry_price": entry_price,
            "proceeds_mult": proceeds,
            "pnl_mult": proceeds - 1.0,
            "peak_mult": peak,
            "max_drawdown_mult": max_dd,
            "exit_reason": exit_reason,
            "failed_exit_attempts": failed_exits,
            # Unsold remainder: open inventory, NOT realized proceeds.
            "remainder_frac": remainder,
            "last_mult": last_mult,
        }

    # -- realized paper tally ------------------------------------------
    @staticmethod
    def _tally_paper(paper: Dict[str, Any], hist: Dict[str, Any]) -> None:
        """Tally one realized paper outcome as reported (no simulation).

        Guards against malformed values; never raises.
        """
        mult = _f(hist.get("realized_mult"))
        if mult <= 0:
            mult = 1.0
        paper["trades"] += 1
        paper["mults"] = paper.get("mults") or []
        paper["mults"].append(mult)
        paper["total_pnl_sol"] = round(
            _f(paper.get("total_pnl_sol")) + _f(hist.get("pnl_sol")), 6)
        if hist.get("rug"):
            paper["rugs"] += 1
        if mult > 1.0:
            paper["wins"] += 1
        elif mult < 1.0:
            paper["losses"] += 1
        narrs = hist.get("narratives") or []
        if not isinstance(narrs, list):
            narrs = []
        for tag in narrs:
            tag = str(tag)
            if not tag:
                continue
            nar = paper["per_narrative"].setdefault(
                tag, {"trades": 0, "wins": 0})
            nar["trades"] += 1
            if mult > 1.0:
                nar["wins"] += 1
        reason = str(hist.get("exit_reason") or "unknown")
        er = paper["per_exit_reason"].setdefault(
            reason, {"trades": 0, "wins": 0})
        er["trades"] += 1
        if mult > 1.0:
            er["wins"] += 1

    # -- main entry ------------------------------------------------------
    def run(self, histories: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Replay histories. Returns the report dict (never raises)."""
        report: Dict[str, Any] = {
            "schema_version": HISTORY_SCHEMA_VERSION,
            # Cost-free replays are NEVER qualifying Stage A evidence;
            # the cost model's fixed drags and seeded failures are
            # scenario assumptions, not calibrated execution evidence.
            "evidence_label": (
                "scenario_assumptions_not_calibrated"
                if self.costs_enabled else
                "costs_disabled_not_stage_a_evidence"),
            "n_histories": 0,
            "n_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_mult": 0.0,
            "median_mult": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_mult": 0.0,
            "expectancy_mult": 0.0,
            "per_source": {},
            "per_narrative": {},
            "per_gate_blocked": {},
            "skipped_malformed": 0,
            # Replay semantics version (comparisons across runs must key
            # on this: v1-legacy vs v2-timestamp-correct are DIFFERENT
            # models; see module constants).
            "replay_model": self.replay_model,
            # v2: rug labels kept out of eligibility for lack of
            # contemporaneous provenance (trades still simulated).
            "n_rug_future_label_ignored": 0,
            # Trades replayed from window-truncated records (audit F3):
            # their missing earlier observations are IRRECOVERABLE, so
            # their entries are later than real detection would allow.
            "n_window_truncated_trades": 0,
            "trades": [],
            # Stage A cost-model accounting (0 / absent when disabled).
            "costs_enabled": self.costs_enabled,
            "n_failed_entry_executions": 0,
            "n_failed_exit_executions": 0,
            # Positions whose exit never fully executed stay VISIBLE as
            # inventory/exposure: they are never treated as realized
            # proceeds and never dropped from the headline tallies.
            "open_inventory": [],
            # Realized paper outcomes (outcome_kind="paper"): what the bot
            # actually did — never re-simulated, tallied as reported.
            "paper": {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "rugs": 0,
                "total_pnl_sol": 0.0,
                "win_rate": 0.0,
                "avg_mult": 0.0,
                "median_mult": 0.0,
                "expectancy_mult": 0.0,
                "per_narrative": {},
                "per_exit_reason": {},
            },
        }
        blocked = report["per_gate_blocked"]

        def _block(reason: str) -> None:
            blocked[reason] = blocked.get(reason, 0) + 1

        mults: List[float] = []
        gross_win = 0.0
        gross_loss = 0.0

        for hist in histories or []:
            report["n_histories"] += 1
            if not isinstance(hist, dict):
                report["skipped_malformed"] += 1
                continue
            if hist.get("outcome_kind") == "paper":
                # Real paper close — tally as reported, never re-simulate.
                self._tally_paper(report["paper"], hist)
                continue
            snaps = hist.get("snapshots")
            if not isinstance(snaps, list) or not snaps:
                _block("no-snapshots")
                continue
            ok, reason, info = self._history_gate(hist, snaps)
            if not ok:
                _block(reason)
                continue
            if info.get("rug_label_ignored"):
                report["n_rug_future_label_ignored"] += 1
            # v2 (audit F1): a record carrying a REAL safety score may
            # only enter at/after the score's evaluation time; a score
            # without trustworthy timing provenance makes the record
            # explicitly ineligible - never backfilled with a guess.
            floor_t: Optional[float] = None
            if self.replay_model == REPLAY_MODEL_V2:
                saf = hist.get("safety")
                if not isinstance(saf, dict):
                    saf = {}
                has_score = (saf.get("score") is not None
                             and not saf.get("unknown"))
                if has_score:
                    floor_raw = hist.get("safety_eval_t")
                    if floor_raw is None:
                        _block("safety-timing-unknown")
                        continue
                    floor_t = _f(floor_raw)
            entry_idx, floor_blocked = self._find_entry(snaps, floor_t)
            if entry_idx is None:
                _block("safety-timing-no-eligible-snapshot"
                       if floor_blocked else "entry-mcap")
                continue

            if self.costs_enabled and self._rng.random() < self.cost_fail_entry:
                # Entry execution unavailable (route/liquidity failure):
                # the trade never opens. Counted, not silently dropped.
                report["n_failed_entry_executions"] += 1
                continue

            trade = self._simulate(snaps, entry_idx)
            report["n_failed_exit_executions"] += trade.get(
                "failed_exit_attempts", 0)
            if hist.get("window_truncated"):
                report["n_window_truncated_trades"] += 1
            pnl = trade["pnl_mult"]
            mult = trade["proceeds_mult"]
            report["n_trades"] += 1
            if pnl > 0:
                report["wins"] += 1
                gross_win += pnl
            elif pnl < 0:
                report["losses"] += 1
                gross_loss += -pnl
            mults.append(mult)
            report["max_drawdown_mult"] = max(
                report["max_drawdown_mult"], trade["max_drawdown_mult"])

            source = str(hist.get("source") or "unknown")
            src = report["per_source"].setdefault(
                source, {"trades": 0, "wins": 0})
            src["trades"] += 1
            if pnl > 0:
                src["wins"] += 1

            narrs = hist.get("narratives") or []
            if not isinstance(narrs, list):
                narrs = []
            for tag in narrs:
                tag = str(tag)
                if not tag:
                    continue
                nar = report["per_narrative"].setdefault(
                    tag, {"trades": 0, "wins": 0})
                nar["trades"] += 1
                if pnl > 0:
                    nar["wins"] += 1

            trade_rec = {
                "mint": hist.get("mint"),
                "symbol": hist.get("symbol"),
                "source": source,
                "narratives": [str(t) for t in narrs if str(t)],
                "entry_t": trade.get("entry_t"),
                "entry_price": trade.get("entry_price"),
                "window_truncated": bool(hist.get("window_truncated")),
                "proceeds_mult": round(mult, 4),
                "pnl_mult": round(pnl, 4),
                "exit_reason": trade["exit_reason"],
                "peak_mult": round(trade["peak_mult"], 4),
                "max_drawdown_mult": round(trade["max_drawdown_mult"], 4),
                "remainder_frac": round(_f(trade.get("remainder_frac")), 6),
            }
            report["trades"].append(trade_rec)
            if _f(trade.get("remainder_frac")) > 1e-9:
                # Unclosed exposure stays visible as inventory; its cost
                # basis (remainder of the 1.0 entry notional) is NOT
                # realized anywhere.
                report["open_inventory"].append({
                    "mint": hist.get("mint"),
                    "symbol": hist.get("symbol"),
                    "remainder_frac": round(_f(trade["remainder_frac"]), 6),
                    "last_mult": round(_f(trade.get("last_mult")), 4),
                    "cost_basis_mult": round(_f(trade["remainder_frac"]), 6),
                })

        n = report["n_trades"]
        if n:
            report["win_rate"] = round(report["wins"] / n, 4)
            report["avg_mult"] = round(sum(mults) / n, 4)
            report["median_mult"] = round(statistics.median(mults), 4)
            report["expectancy_mult"] = round(report["avg_mult"] - 1.0, 4)
        if gross_loss > 0:
            report["profit_factor"] = round(gross_win / gross_loss, 4)
        elif gross_win > 0:
            report["profit_factor"] = float("inf")
        report["max_drawdown_mult"] = round(report["max_drawdown_mult"], 4)
        for bucket in (report["per_source"], report["per_narrative"]):
            for key, agg in bucket.items():
                t = agg["trades"]
                agg["win_rate"] = round(agg["wins"] / t, 4) if t else 0.0
        if self.costs_enabled:
            report["costs"] = {
                "fee_bps": self.cost_fee_bps,
                "slippage_bps": self.cost_slippage_bps,
                "price_impact_bps": self.cost_impact_bps,
                "fail_prob_entry": self.cost_fail_entry,
                "fail_prob_exit": self.cost_fail_exit,
                "trade_notional_usd": self.cost_notional_usd,
                "token_account_cost_usd": self.cost_ata_usd,
            }
        # Finalize realized paper stats.
        paper = report["paper"]
        mults = paper.pop("mults", None) or []
        pt = paper["trades"]
        if pt:
            paper["win_rate"] = round(paper["wins"] / pt, 4)
            paper["avg_mult"] = round(sum(mults) / len(mults), 4) if mults else 1.0
            paper["median_mult"] = round(statistics.median(mults), 4) if mults else 1.0
            paper["expectancy_mult"] = round(paper["avg_mult"] - 1.0, 4)
        for bucket in (paper["per_narrative"], paper["per_exit_reason"]):
            for key, agg in bucket.items():
                t = agg["trades"]
                agg["win_rate"] = round(agg["wins"] / t, 4) if t else 0.0
        return report

    # -- chronological dev/eval separation (Stage A) --------------------
    def run_with_holdout(self, histories: List[Dict[str, Any]],
                         holdout_fraction: float = 0.3) -> Dict[str, Any]:
        """Split histories CHRONOLOGICALLY and report dev and eval runs
        separately. The LATER segment is the evaluation holdout:
        strategy development belongs on the dev segment, and the
        holdout is touched only for final evaluation, so repeatedly
        tuning against the report never overfits the eval data
        (request Stage A: separate strategy development from final
        evaluation data)."""
        def _first_t(h: Any) -> float:
            if not isinstance(h, dict):
                return float("inf")
            snaps = h.get("snapshots")
            if isinstance(snaps, list):
                for s in snaps:
                    if isinstance(s, dict) and _f(s.get("t")) > 0:
                        return _f(s.get("t"))
            return _f(h.get("t0"))

        ordered = sorted(list(histories or []), key=_first_t)
        n = len(ordered)
        frac = min(max(_f(holdout_fraction), 0.0), 1.0)
        k = min(n, max(0, int(round(n * (1.0 - frac)))))
        # Independent engines per segment: the dev run must not consume
        # randomness (or any state) that belongs to the eval run -
        # otherwise dev histories would leak into eval outcomes through
        # the shared failure-stream (boundary leakage).
        dev_engine = BacktestEngine(entry_rules=self._cfg_entry,
                                    exit_rules=self._cfg_exit,
                                    costs=self._cfg_costs,
                                    seed=self._seed + 1,
                                    replay_model=self.replay_model)
        eval_engine = BacktestEngine(entry_rules=self._cfg_entry,
                                     exit_rules=self._cfg_exit,
                                     costs=self._cfg_costs,
                                     seed=self._seed + 2,
                                     replay_model=self.replay_model)
        return {
            "split_index": k,
            "holdout_fraction": frac,
            # Each segment is its OWN portfolio: capital resets at the
            # boundary, no open position or P&L carries over, and the
            # two reports must never be concatenated as one portfolio.
            "portfolio_note": ("independent segments: capital resets at "
                               "the split; no position or capital "
                               "carryover; not one portfolio"),
            "dev": dev_engine.run(ordered[:k]),
            "eval": eval_engine.run(ordered[k:]),
        }


def synthetic_history(mint: str = "SYNTH1", symbol: str = "SYN",
                      chain: str = "solana", source: str = "synthetic",
                      seed: int = 1, n_snaps: int = 24,
                      start_price: float = 1e-6, drift: float = 0.02,
                      vol: float = 0.05, rug: bool = False,
                      narratives: Optional[List[str]] = None,
                      safety_score: float = 80.0) -> Dict[str, Any]:
    """Deterministic fake history for tests/demos (seeded random)."""
    rng = random.Random(seed)
    t0 = 1700000000.0
    snaps: List[Dict[str, Any]] = []
    price = max(_f(start_price), 1e-12)
    peak = 0.0
    peak_t = t0
    for i in range(max(1, int(n_snaps))):
        t = t0 + i * 60.0
        if i > 0:
            if rug and i > n_snaps // 2:
                price *= 0.05  # the rug: liquidity pulled, price collapses
            else:
                price *= max(0.0001, 1.0 + _f(drift)
                             + _f(vol) * rng.gauss(0.0, 1.0))
        price = max(price, 1e-12)
        snaps.append({
            "t": t,
            "price_usd": price,
            "mcap_usd": price * 1e6,  # small enough to pass the entry ceiling
            "vol5m_usd": price * 1e6 * 0.02 * rng.random(),
            "buys5m": rng.randint(0, 50),
            "sells5m": rng.randint(0, 40),
            "liq_usd": price * 1e6 * 0.1,
        })
        if price > peak:
            peak, peak_t = price, t
    score = _f(safety_score)
    return {
        "mint": mint,
        "symbol": symbol,
        "chain": chain,
        "source": source,
        "t0": t0,
        "snapshots": snaps,
        # Synthetic fixture: all labels are declared contemporaneous with
        # detection (t0) - v2-timestamp-correct treats them as available
        # from the first snapshot.
        "safety_eval_t": t0,
        "safety": {"score": score, "passed": score >= 50.0,
                   "fails": [], "unknown": False},
        "sentiment": None,
        "narratives": list(narratives or []),
        "outcome": {
            "peak_mult": peak / max(_f(start_price), 1e-12),
            "peak_t": peak_t,
            "rug": bool(rug),
            "end_t": snaps[-1]["t"],
            "recorded_t": t0,
        },
    }


def _load_dataset(path: str) -> List[Dict[str, Any]]:
    histories: List[Dict[str, Any]] = []
    with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                histories.append(json.loads(line))
            except Exception:
                histories.append(line)  # counted as malformed, not crashed on
    return histories


def _parse_tp(spec: str) -> List[Tuple[float, float]]:
    ladder: List[Tuple[float, float]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        m, frac = part.split(":", 1)
        m_f, f_f = _f(m), _f(frac)
        if m_f > 1.0 and f_f > 0:
            ladder.append((m_f, min(f_f, 1.0)))
    return ladder


def _print_report(report: Dict[str, Any]) -> None:
    n, t = report["n_histories"], report["n_trades"]
    print("Backtest — %d histories, %d trades" % (n, t))
    print("EVIDENCE: %s" % report.get("evidence_label", "unknown"))
    if report.get("open_inventory"):
        oi = report["open_inventory"]
        print("OPEN INVENTORY (unclosed exposure, NOT realized): %d "
              "positions, cost basis %.4fx of entry" % (
                  len(oi), sum(_f(p.get("cost_basis_mult")) for p in oi)))
    if not t:
        print("no trades taken (all histories blocked or malformed)")
    else:
        print("win rate %.1f%% (%dW/%dL)  avg %.3fx  median %.3fx" % (
            report["win_rate"] * 100.0, report["wins"], report["losses"],
            report["avg_mult"], report["median_mult"]))
        pf = report["profit_factor"]
        pf_s = "inf" if pf == float("inf") else "%.2f" % pf
        print("expectancy %+.3fx  profit factor %s  max drawdown %.3fx" % (
            report["expectancy_mult"], pf_s, report["max_drawdown_mult"]))
    for label, bucket in (("source", report["per_source"]),
                          ("narrative", report["per_narrative"])):
        if bucket:
            bits = ["%s: %d trades %.0f%%" % (
                k, v["trades"], v["win_rate"] * 100.0)
                for k, v in sorted(bucket.items())]
            print("per %s — %s" % (label, " | ".join(bits)))
    if report["per_gate_blocked"]:
        bits = ["%s=%d" % kv for kv in sorted(
            report["per_gate_blocked"].items())]
        print("blocked — " + " ".join(bits))
    if report["skipped_malformed"]:
        print("skipped malformed: %d" % report["skipped_malformed"])
    if report.get("costs_enabled"):
        c = report.get("costs") or {}
        print("costs: fee %.0fbps slippage %.0fbps impact %.0fbps per leg"
              % (c.get("fee_bps", 0), c.get("slippage_bps", 0),
                 c.get("price_impact_bps", 0)))
        print("failed executions: %d entries, %d exit legs" % (
            report.get("n_failed_entry_executions", 0),
            report.get("n_failed_exit_executions", 0)))
    paper = report.get("paper") or {}
    if paper.get("trades"):
        print("--- REAL PAPER OUTCOMES (%d closed positions) ---" % (
            paper["trades"],))
        print("win rate %.1f%% (%dW/%dL)  avg %.3fx  expectancy %+.3fx" % (
            paper["win_rate"] * 100.0, paper["wins"], paper["losses"],
            paper["avg_mult"], paper["expectancy_mult"]))
        print("realized P&L %+.6f SOL  rugs %d" % (
            paper["total_pnl_sol"], paper["rugs"]))
        # Stage-C visibility (owner-approved 2026-11-22): simulated
        # outcomes are never qualified performance; the fill model is
        # unvalidated and dataset paper closes predate price-kind
        # labelling.
        print("qualification: simulation_unvalidated_not_qualified "
              "(fill model unvalidated; expectancy above is NOT "
              "qualified performance)")
        for label, bucket in (("narrative", paper["per_narrative"]),
                              ("exit", paper["per_exit_reason"])):
            if bucket:
                bits = ["%s: %d trades %.0f%%" % (
                    k, v["trades"], v["win_rate"] * 100.0)
                    for k, v in sorted(bucket.items())]
                print("paper per %s — %s" % (label, " | ".join(bits)))
    else:
        print("no real paper closes recorded yet (paper positions close "
              "into this section)")


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry: replay a recorded dataset and print the report."""
    ap = argparse.ArgumentParser(
        prog="backtest",
        description="Replay recorded launch histories through entry/exit "
                    "rules (paper only, no network).")
    ap.add_argument("dataset", nargs="?",
                    default="~/.forex-agent/launch_scanner/backtest_dataset.jsonl",
                    help="JSONL history dataset path")
    ap.add_argument("--min-safety", type=float, default=None,
                    help="override entry min safety score (default 50.0)")
    ap.add_argument("--tp", default=None,
                    help='override TP ladder, e.g. "2:0.5,5:0.25"')
    ap.add_argument("--trailing", type=float, default=None,
                    help="override trailing stop pct (default 30)")
    ap.add_argument("--time-stop-h", type=float, default=None,
                    help="override time stop hours (default 6)")
    ap.add_argument("--with-costs", action="store_true",
                    help="Stage A cost model: 125bps venue fee per leg, "
                         "50bps slippage, 100bps price impact (override "
                         "with --fee-bps/--slippage-bps/--impact-bps)")
    ap.add_argument("--fee-bps", type=float, default=None)
    ap.add_argument("--slippage-bps", type=float, default=None)
    ap.add_argument("--impact-bps", type=float, default=None)
    ap.add_argument("--venue", default=None,
                    help="named venue fee scenario (pumpfun-curve=125bps; "
                         "pumpswap-canonical-max=125bps / "
                         "pumpswap-canonical-min=30bps / "
                         "pumpswap-noncanonical=30bps, as of 2026-11 "
                         "captures); alternative to --fee-bps")
    ap.add_argument("--replay-model", default=REPLAY_MODEL_V2,
                    choices=list(REPLAY_MODELS),
                    help="replay semantics version; v2-timestamp-correct "
                         "(default) never enters before safety_eval_t and "
                         "ignores unprovenanced/future rug labels for "
                         "eligibility; v1-legacy reproduces prior output")
    ap.add_argument("--holdout", type=float, default=None,
                    help="chronological dev/eval split: the LATER "
                         "fraction (e.g. 0.3) is the evaluation holdout")
    args = ap.parse_args(argv)

    entry_rules: Dict[str, Any] = {}
    if args.min_safety is not None:
        entry_rules["min_safety_score"] = args.min_safety
    exit_rules: Dict[str, Any] = {}
    if args.tp is not None:
        exit_rules["tp_ladder"] = _parse_tp(args.tp)
    if args.trailing is not None:
        exit_rules["trailing_stop_pct"] = args.trailing
    if args.time_stop_h is not None:
        exit_rules["time_stop_h"] = args.time_stop_h

    if not os.path.exists(os.path.expanduser(args.dataset)):
        print("no data yet — the recorder builds it from live paper flow")
        print("(expected at %s)" % os.path.expanduser(args.dataset))
        return 0
    histories = _load_dataset(args.dataset)
    costs: Optional[Dict[str, Any]] = None
    if args.with_costs:
        # The venue fee is NEVER silently assumed: --fee-bps explicitly,
        # or a named venue SCENARIO. PumpSwap has no single rate: its
        # canonical tiers run 125 -> 30 bps by market cap, non-canonical
        # pools charge 0.30%, and unknown mcap/quote resolves to the max
        # tier (fee schedule verified from captures+IDLs, as of
        # 2026-11 - a snapshot, never a live-current claim). The named
        # scenarios below pick a tier explicitly; anything else needs
        # --fee-bps.
        venue_fees = {"pumpfun-curve": 125.0,
                      "pumpswap-canonical-max": 125.0,
                      "pumpswap-canonical-min": 30.0,
                      "pumpswap-noncanonical": 30.0}
        if args.fee_bps is not None:
            fee_bps = args.fee_bps
        elif args.venue in venue_fees:
            fee_bps = venue_fees[args.venue]
        else:
            print("--with-costs needs an explicit fee: --fee-bps BPS or "
                  "--venue {%s}" % "|".join(sorted(venue_fees)))
            return 2
        costs = {
            "fee_bps": fee_bps,
            "slippage_bps": (50.0 if args.slippage_bps is None
                             else args.slippage_bps),
            "price_impact_bps": (100.0 if args.impact_bps is None
                                 else args.impact_bps),
        }
    engine = BacktestEngine(entry_rules=entry_rules,
                            exit_rules=exit_rules, costs=costs,
                            replay_model=args.replay_model)
    if args.holdout is not None:
        split = engine.run_with_holdout(histories, args.holdout)
        print("SEGMENTS: %s" % split["portfolio_note"])
        print("=== DEV segment (%d histories) ===" % split["split_index"])
        _print_report(split["dev"])
        print("=== EVAL holdout (later %d histories; final evaluation "
              "only) ===" % (len(histories) - split["split_index"]))
        _print_report(split["eval"])
    else:
        _print_report(engine.run(histories))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
