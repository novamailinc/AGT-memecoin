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

# Regime tagging (Stage A request section 12, 2026-11-23). DECLARED
# CONTEMPORANEOUS RULES: every label is computed ONLY from snapshots at
# or before the trade's own entry - never post-entry prices, outcomes,
# or dataset-wide hindsight. These are PER-MINT pre-entry regimes.
# Market-WIDE conditions (launch-heavy, market selloff) need cross-mint
# context the history records do not carry: regime_context_note says so
# instead of faking labels from one mint.
REGIME_RULES = {
    # entry price vs the first pre-entry snapshot price
    "trend_up_mult": 1.2,
    "trend_down_mult": 0.8,
    # mean |snapshot-to-snapshot return| over the pre-entry window
    "high_vol_mean_abs_ret": 0.10,
    # entry-snapshot usd_liquidity
    "low_liq_usd": 50000.0,
    # pre-entry peak-to-entry drawdown
    "selloff_drawdown": 0.40,
}


def _regime_labels(snaps: List[Any], entry_idx: int,
                   rules: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, str]:
    """Contemporaneous per-trade regime labels at entry.

    Reads ONLY snaps[:entry_idx + 1]. Any dimension without enough
    contemporaneous data is "unknown", never guessed.
    """
    r = dict(REGIME_RULES)
    if rules:
        r.update(rules)
    pre = [snap for snap in snaps[:entry_idx + 1]
           if isinstance(snap, dict) and _f(snap.get("price_usd")) > 0]
    labels = {"trend": "unknown", "volatility": "unknown",
              "liquidity": "unknown", "selloff": "unknown"}
    if not pre:
        return labels
    entry_px = _f(pre[-1].get("price_usd"))
    # trend: entry vs first pre-entry price
    if len(pre) >= 2:
        first_px = _f(pre[0].get("price_usd"))
        mult = entry_px / first_px if first_px > 0 else 1.0
        labels["trend"] = ("up" if mult >= _f(r["trend_up_mult"])
                           else "down" if mult <= _f(r["trend_down_mult"])
                           else "flat")
    # volatility: mean absolute snap-to-snap return
    if len(pre) >= 3:
        rets = []
        for a, b in zip(pre, pre[1:]):
            pa, pb = _f(a.get("price_usd")), _f(b.get("price_usd"))
            if pa > 0:
                rets.append(abs(pb / pa - 1.0))
        if rets:
            labels["volatility"] = (
                "high-vol"
                if sum(rets) / len(rets) >= _f(r["high_vol_mean_abs_ret"])
                else "low-vol")
    # liquidity: the entry snapshot's own field
    liq = _f(pre[-1].get("liq_usd"))
    if liq > 0:
        labels["liquidity"] = ("low-liq"
                               if liq < _f(r["low_liq_usd"])
                               else "normal-liq")
    # selloff: deepest pre-entry peak-to-entry drawdown
    if len(pre) >= 2:
        peak = max(_f(snap.get("price_usd")) for snap in pre)
        if peak > 0:
            dd = 1.0 - entry_px / peak
            labels["selloff"] = ("selloff"
                                 if dd >= _f(r["selloff_drawdown"])
                                 else "no-selloff")
    return labels

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


def _evidence_time(value):
    """Numeric finite nonnegative event time, no coercion of missing evidence.

    Zero remains valid for explicitly constructed relative-time fixtures.
    """
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        return False


def record_history(path: str, record: Dict[str, Any]) -> bool:
    """Append history. Stable outcome_id records use exact durable replacement.

    Legacy records retain their existing append contract. An outcome_id opts in
    to full-file integrity checks and exact replay under an interprocess lock;
    it never infers an identity for old records or silently changes an outcome.
    """
    try:
        p = os.path.expanduser(str(path))
        parent = os.path.dirname(p) or "."
        os.makedirs(parent, exist_ok=True)
        outcome_id = record.get("outcome_id")
        if outcome_id is not None and (not isinstance(outcome_id, str) or not outcome_id):
            raise ValueError("invalid history outcome identity")
        import fcntl
        import tempfile
        with open(p + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if outcome_id is None:
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, default=str) + "\n")
                return True
            records = []
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    for line in fh:
                        if line.strip():
                            item = json.loads(line)
                            if not isinstance(item, dict):
                                raise ValueError("invalid history record")
                            records.append(item)
            seen_ids = set()
            for item in records:
                if "outcome_id" not in item:
                    continue  # Old records retain their unkeyed contract.
                prior_id = item["outcome_id"]
                if not isinstance(prior_id, str) or not prior_id or prior_id in seen_ids:
                    raise ValueError("invalid or duplicate history outcome identity")
                seen_ids.add(prior_id)
            matches = [r for r in records if r.get("outcome_id") == outcome_id]
            if matches and (len(matches) != 1 or matches[0] != record):
                raise ValueError("history outcome identity conflict")
            if not matches:
                records.append(record)
                fd, tmp = tempfile.mkstemp(prefix=".history-", dir=parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        for item in records:
                            fh.write(json.dumps(item, allow_nan=False) + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, p)
                finally:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
            # An exact visible replay still owes the file/directory barriers.
            with open(p, "rb") as fh:
                os.fsync(fh.fileno())
            fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return True
    except Exception:
        return False


class BacktestEngine:
    """Replay histories through entry/exit rules. Standalone, no scanner."""

    def __init__(self, entry_rules: Optional[Dict[str, Any]] = None,
                 exit_rules: Optional[Dict[str, Any]] = None,
                 costs: Optional[Dict[str, Any]] = None,
                 seed: int = 0,
                 replay_model: str = REPLAY_MODEL_V2,
                 require_provider_inputs: bool = False):
        if replay_model not in REPLAY_MODELS:
            raise ValueError("unknown replay_model %r (want one of %s)"
                             % (replay_model, ", ".join(REPLAY_MODELS)))
        if type(require_provider_inputs) is not bool:raise ValueError('provider qualification flag must be bool')
        self.require_provider_inputs=require_provider_inputs
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
        if costs is not None and not isinstance(costs,dict):raise ValueError('costs must be a scenario dictionary')
        costs = dict(costs or {})
        known_costs={'fee_bps','slippage_bps','price_impact_bps','fail_prob_entry','fail_prob_exit','trade_notional_usd','token_account_cost_usd'}
        if set(costs)-known_costs:raise ValueError('unknown cost scenario field')
        for name,value in costs.items():
            if not _evidence_time(value):raise ValueError('cost scenario needs finite nonnegative numeric '+name)
        if costs.get('fee_bps',0)>10000:raise ValueError('fee_bps cannot exceed 10000')
        for name in ('fail_prob_entry','fail_prob_exit'):
            if costs.get(name,0)>1:raise ValueError(name+' must be between 0 and 1')
        try:
            if not math.isfinite(float(costs.get('slippage_bps',0))+float(costs.get('price_impact_bps',0))):raise ValueError('combined cost drag unavailable')
            if costs.get('token_account_cost_usd',0)>0:
                notional=costs.get('trade_notional_usd',0)
                if notional<=0 or not math.isfinite(costs['token_account_cost_usd']/notional):raise ValueError('fixed cost needs positive finite usable notional')
        except OverflowError:raise ValueError('cost scenario cannot be represented')
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
                if not _evidence_time(prov) or prov > first_t:
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
        raw_mult = hist.get("realized_mult")
        mult = float(raw_mult) if _evidence_time(raw_mult) else None
        paper["trades"] += 1
        paper["mults"] = paper.get("mults") or []
        if mult is None:
            paper["unavailable_mult_count"] += 1
        else:
            paper["mults"].append(mult)
        raw_pnl = hist.get("pnl_sol")
        try:
            pnl = float(raw_pnl) if type(raw_pnl) in (int,float) else None
        except (OverflowError, ValueError):
            pnl = None
        if pnl is None or not math.isfinite(pnl):
            paper["unavailable_pnl_count"] += 1
        else:
            paper["pnls"].append(pnl)
        if hist.get("rug"):
            paper["rugs"] += 1
        if mult is not None and mult > 1.0:
            paper["wins"] += 1
        elif mult is not None and mult < 1.0:
            paper["losses"] += 1
        narrs = hist.get("narratives") or []
        if not isinstance(narrs, list):
            narrs = []
        for tag in narrs:
            tag = str(tag)
            if not tag:
                continue
            nar = paper["per_narrative"].setdefault(
                tag, {"trades": 0, "wins": 0, "unavailable_mult_count": 0})
            nar["trades"] += 1
            if mult is None:
                nar["unavailable_mult_count"] += 1
            elif mult > 1.0:
                nar["wins"] += 1
        reason = str(hist.get("exit_reason") or "unknown")
        er = paper["per_exit_reason"].setdefault(
            reason, {"trades": 0, "wins": 0, "unavailable_mult_count": 0})
        er["trades"] += 1
        if mult is None:
            er["unavailable_mult_count"] += 1
        elif mult > 1.0:
            er["wins"] += 1

    # -- main entry ------------------------------------------------------
    @staticmethod
    def _provider_qualification(histories,required):
        from daemon.launch_scanner import provider_inputs
        statuses=[provider_inputs.record_status(h) for h in histories or []]
        return {'required':required,'histories_by_state':{s:statuses.count(s) for s in set(statuses)},
            'cohort_qualified':bool(statuses) and all(s=='provider-recorded' for s in statuses),
            'scope':'adapter-consistency-not-authentication-freshness-or-completeness'}

    def run(self, histories: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._run(histories,self._provider_qualification(histories,self.require_provider_inputs))

    def _run(self, histories,provider_qualification):
        """Replay a segment with its explicitly retained cohort boundary."""
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
            # Trades whose series has missing middle intervals or
            # journal failures: also NOT complete series.
            # Observed diagnostic coverage ONLY: counts trades whose
            # series_integrity is anything other than journal-whole.
            # NOT a completeness qualification - "journal-whole" itself
            # carries no sequence proof (see series_integrity_note).
            "n_trades_not_journal_whole": 0,
            # Buckets of the qualified series_integrity diagnostic per
            # trade (legacy records: "legacy-unlabeled").
            "per_series_integrity": {},
            # Per-regime tallies (declared contemporaneous rules, see
            # REGIME_RULES): per dimension, per label.
            "per_regime": {},
            "regime_context_note": (
                "per-mint pre-entry regimes only; market-WIDE "
                "conditions (launch-heavy, market selloff) need "
                "cross-mint context the records do not carry"),
            "series_integrity_note": (
                "diagnostic coverage only, never a completeness proof: "
                "the gap check catches intervals > max(5x median, "
                "300s), so journal-whole-no-detected-gaps means no "
                "DETECTED gaps/truncation/failures - it does not prove "
                "every poll exists and must not count as qualified "
                "complete history"),
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
                "pnls": [],
                "unavailable_mult_count": 0,
                "unavailable_pnl_count": 0,
                "economics_complete": True,
                "win_rate": 0.0,
                "avg_mult": 0.0,
                "median_mult": 0.0,
                "expectancy_mult": 0.0,
                "per_narrative": {},
                "per_exit_reason": {},
            },
        }
        report['provider_input_qualification']=dict(provider_qualification)
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
            if self.require_provider_inputs and not report['provider_input_qualification']['cohort_qualified']:
                _block('provider-input-cohort-unavailable')
                continue
            input_evidence=hist.get('snapshot_input_evidence')
            if 'snapshot_input_evidence' not in hist:
                report['n_snapshot_inputs_legacy_unrecorded']=report.get('n_snapshot_inputs_legacy_unrecorded',0)+1
            if self.replay_model==REPLAY_MODEL_V2 and 'snapshot_input_evidence' in hist:
                from daemon.launch_scanner import input_availability as inputs
                recorded_t=(hist.get('outcome') or {}).get('recorded_t') if isinstance(hist.get('outcome'),dict) else None
                valid=inputs.availability(recorded_t)=='recorded' and inputs.matches(hist.get('mint'),input_evidence,hist.get('snapshots'),until=recorded_t)
                if not valid:
                    _block('snapshot-input-unavailable')
                    continue
            snaps = hist.get("snapshots")
            if not isinstance(snaps, list) or not snaps:
                _block("no-snapshots")
                continue
            if self.replay_model == REPLAY_MODEL_V2:
                times=[snap.get('t') for snap in snaps if isinstance(snap,dict)]
                if (not times or any(not _evidence_time(t) for t in times)
                        or any(later<earlier for earlier,later in zip(times,times[1:]))):
                    _block('snapshot-timing-invalid')
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
                    if not _evidence_time(floor_raw):
                        _block("safety-timing-unknown")
                        continue
                    floor_t = float(floor_raw)
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
            trade["regimes"] = _regime_labels(snaps, entry_idx)
            report["n_failed_exit_executions"] += trade.get(
                "failed_exit_attempts", 0)
            if hist.get("window_truncated"):
                report["n_window_truncated_trades"] += 1
            integrity = hist.get("series_integrity") or "legacy-unlabeled"
            bucket = report["per_series_integrity"].setdefault(
                integrity, {"trades": 0})
            bucket["trades"] += 1
            if integrity != "journal-whole-no-detected-gaps":
                report["n_trades_not_journal_whole"] += 1
            trade_pnl = _f(trade.get("pnl_mult"))
            for dim, label in (trade.get("regimes") or {}).items():
                bucket = report["per_regime"].setdefault(
                    dim, {}).setdefault(
                    label, {"trades": 0, "wins": 0, "losses": 0,
                            "pnl_mult_sum": 0.0})
                bucket["trades"] += 1
                if trade_pnl > 0:
                    bucket["wins"] += 1
                else:
                    bucket["losses"] += 1
                bucket["pnl_mult_sum"] = round(
                    bucket["pnl_mult_sum"] + trade_pnl, 6)
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
                "series_gaps": int(hist.get("series_gaps") or 0),
                # Qualified diagnostic, never a completeness proof;
                # records predating the journal carry no label.
                "series_integrity": (
                    hist.get("series_integrity") or "legacy-unlabeled"),
                "regimes": trade.get("regimes") or {},
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
        pnls = paper.pop("pnls")
        try:
            pnl_total = math.fsum(pnls)
            if not math.isfinite(pnl_total):pnl_total = None
        except (OverflowError,ValueError):
            pnl_total = None
        paper["total_pnl_sol"] = (round(pnl_total,6)
            if not paper["unavailable_pnl_count"] and pnl_total is not None else None)
        paper["economics_complete"] = (not paper["unavailable_mult_count"]
            and paper["total_pnl_sol"] is not None)
        if not paper['economics_complete']:
            for key in ('win_rate','avg_mult','median_mult','expectancy_mult'):
                paper[key] = None
        elif pt:
            paper["win_rate"] = round(paper["wins"] / pt, 4)
            # Divide before summing to avoid overflowing finite means.
            paper["avg_mult"] = round(math.fsum(x/len(mults) for x in mults),4)
            ordered_mults=sorted(mults);mid=len(mults)//2
            paper["median_mult"] = round(ordered_mults[mid] if len(mults)%2 else
                ordered_mults[mid-1]/2+ordered_mults[mid]/2,4)
            paper["expectancy_mult"] = round(paper["avg_mult"] - 1.0, 4)
        for bucket in (paper["per_narrative"], paper["per_exit_reason"]):
            for key, agg in bucket.items():
                t = agg["trades"]
                agg["win_rate"] = (None if agg['unavailable_mult_count'] else
                    round(agg["wins"] / t, 4) if t else 0.0)
        return report

    # -- chronological dev/eval separation (Stage A) --------------------
    def run_with_holdout(self, histories: List[Dict[str, Any]],
                         holdout_fraction: float = 0.3) -> Dict[str, Any]:
        """Split histories CHRONOLOGICALLY and report dev and eval runs
        separately. The LATER segment is the evaluation holdout:
        strategy development belongs on the dev segment. Calling this
        function does not enforce an untouched evaluation set: repeatedly
        tuning against its report can overfit. Snapshot boundary diagnostics
        do not establish source completeness or full evidence separation."""
        def _first_t(h: Any) -> float:
            if not isinstance(h, dict):
                return float("inf")
            snaps = h.get("snapshots")
            if self.replay_model == REPLAY_MODEL_V2:
                if not isinstance(snaps,list):return float('inf')
                times=[snap.get('t') for snap in snaps if isinstance(snap,dict)]
                if (not times or any(not _evidence_time(t) for t in times)
                        or any(b<a for a,b in zip(times,times[1:]))):return float('inf')
                return times[0] # zero is explicit relative time, not missing
            if isinstance(snaps, list):
                for s in snaps:
                    if isinstance(s, dict) and _f(s.get("t")) > 0:
                        return _f(s.get("t"))
            return _f(h.get("t0"))

        supplied = list(histories or [])
        rejected = []
        if self.replay_model == REPLAY_MODEL_V2:
            # Unknown chronology cannot move valid records across the split.
            # Retain a separate accounting report, including paper economics.
            rejected = [h for h in supplied if not math.isfinite(_first_t(h))]
            supplied = [h for h in supplied if math.isfinite(_first_t(h))]
        provider_cohort=self._provider_qualification(histories,self.require_provider_inputs)
        def segment_qualification(rows):
            return provider_cohort if self.require_provider_inputs else self._provider_qualification(rows,False)
        ordered = sorted(supplied, key=_first_t)
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
                                    replay_model=self.replay_model,
                                    require_provider_inputs=self.require_provider_inputs)
        eval_engine = BacktestEngine(entry_rules=self._cfg_entry,
                                     exit_rules=self._cfg_exit,
                                     costs=self._cfg_costs,
                                     seed=self._seed + 2,
                                     replay_model=self.replay_model,
                                    require_provider_inputs=self.require_provider_inputs)
        # Sorting start times alone does not prevent dev evidence extending
        # beyond eval start. Report that limitation without dropping losses,
        # truncating histories, or claiming an untouched/calibrated holdout.
        def _window(h: Any) -> Optional[Tuple[float, float]]:
            if not isinstance(h,dict) or not isinstance(h.get('snapshots'),list):
                return None
            times=[snap.get('t') for snap in h['snapshots'] if isinstance(snap,dict)]
            if (not times or any(not _evidence_time(t) for t in times)
                    or any(b<a for a,b in zip(times,times[1:]))):
                return None
            return times[0],times[-1]
        windows=[_window(h) for h in ordered]
        unknown=len(rejected)+sum(w is None for w in windows)
        dev_end=max((w[1] for w in windows[:k] if w is not None),default=None)
        eval_start=min((w[0] for w in windows[k:] if w is not None),default=None)
        status=('unknown-timing' if unknown else
                'empty-segment' if dev_end is None or eval_start is None else
                'overlap' if dev_end>=eval_start else 'separated')
        boundary={
            'status':status, 'unknown_history_count':unknown,
            'dev_latest_snapshot_t':dev_end, 'eval_earliest_snapshot_t':eval_start,
            'snapshot_windows_separated':status=='separated',
            'scope':'retained snapshots only; safety, labels, source availability and tuning isolation not verified',
            'stage_a_qualified':False,
        }
        return {
            "snapshot_boundary": boundary,
            "provider_input_qualification":provider_cohort,
            "cohort": {"input_count":len(ordered)+len(rejected),
                       "admitted_count":len(ordered),
                       "rejected_count":len(rejected),
                       "rejection_reason":"unknown snapshot chronology; excluded from split, retained in rejected report"},
            "rejected": BacktestEngine(entry_rules=self._cfg_entry,
                                       exit_rules=self._cfg_exit,
                                       costs=self._cfg_costs,
                                       seed=self._seed+3,
                                       replay_model=self.replay_model,
                                    require_provider_inputs=self.require_provider_inputs)._run(rejected,segment_qualification(rejected)),
            "split_index": k,
            "holdout_fraction": frac,
            # Each segment is its OWN portfolio: capital resets at the
            # boundary, no open position or P&L carries over, and the
            # two reports must never be concatenated as one portfolio.
            "portfolio_note": ("independent segments: capital resets at "
                               "the split; no position or capital "
                               "carryover; not one portfolio"),
            "dev": dev_engine._run(ordered[:k],segment_qualification(ordered[:k])),
            "eval": eval_engine._run(ordered[k:],segment_qualification(ordered[k:])),
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
        if paper['win_rate'] is None:
            print("return statistics unavailable: incomplete economics (%d invalid multipliers, %d invalid P&L records)" %
                  (paper['unavailable_mult_count'],paper['unavailable_pnl_count']))
        else:
            print("win rate %.1f%% (%dW/%dL)  avg %.3fx  expectancy %+.3fx" % (
                paper["win_rate"] * 100.0, paper["wins"], paper["losses"],
                paper["avg_mult"], paper["expectancy_mult"]))
        if paper['total_pnl_sol'] is None:
            print("realized P&L unavailable: missing/invalid evidence or nonfinite aggregate")
        else:
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
                bits = ["%s: %d trades %s" % (
                    k, v["trades"], 'unavailable' if v['win_rate'] is None else
                    '%.0f%%' % (v['win_rate'] * 100.0))
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
                    help="uncalibrated scenario costs, explicit fee required; "
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
    ap.add_argument("--require-provider-inputs", action="store_true",
                    help="require adapter-consistency evidence across the entire supplied cohort; not Stage A qualification")
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
    try:
        engine = BacktestEngine(entry_rules=entry_rules,
                                exit_rules=exit_rules, costs=costs,
                                replay_model=args.replay_model,
                                require_provider_inputs=args.require_provider_inputs)
    except ValueError as exc:
        print('invalid replay scenario: %s' % exc)
        return 2
    if args.require_provider_inputs:
        q=engine._provider_qualification(histories,True)
        print("PROVIDER INPUT COHORT: %s (adapter consistency only)" % ("qualified" if q["cohort_qualified"] else "unavailable"))
    if args.holdout is not None:
        split = engine.run_with_holdout(histories, args.holdout)
        print("SEGMENTS: %s" % split["portfolio_note"])
        print("=== DEV segment (%d histories) ===" % split["split_index"])
        _print_report(split["dev"])
        print("SNAPSHOT BOUNDARY: %s; Stage A unqualified" %
              split["snapshot_boundary"]["status"])
        print("=== EVAL holdout (later %d admitted histories; not an "
              "enforced untouched evaluation set) ===" %
              (split["cohort"]["admitted_count"] - split["split_index"]))
        _print_report(split["eval"])
        if split["cohort"]["rejected_count"]:
            print("=== REJECTED COHORT (%d histories; not in split) ===" %
                  split["cohort"]["rejected_count"])
            _print_report(split["rejected"])
    else:
        _print_report(engine.run(histories))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
