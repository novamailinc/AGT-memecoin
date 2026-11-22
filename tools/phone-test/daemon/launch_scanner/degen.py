"""Degen lane: high-recall runner alerts for real-money manual trading.

The safe lane is selective: it only alerts coins that pass strict quality
gates. The degen lane runs in parallel with a different philosophy — catch
more potential runners, accept more rugs, but NEVER compromise on the two
things that mean certain death with real money:

  * honeypot / cannot-sell: hard block, always. A coin you cannot exit
    is not a gamble, it's theft. Paper or real, the ledger would lie.

Everything else (insider clusters, high tax, low/unknown safety score,
unknown deployer) becomes a VISIBLE RISK LABEL on the card, not a wall.

Scoring is CALIBRATED against 108k backtested coins (2026-10-10 analysis):
332 confirmed 3x+ winners vs 640 confirmed non-winners. The evidence:

  * mcap $30k-$200k at detection: 71-72% precision (above $200k: 12%,
    above $500k: 2% — dead zone). Winners median $62k, losers $639k.
  * vol/mcap turnover >= 0.10: 85% recall. Winners median 0.28, losers 0.02.
  * Combined mcap sweet spot + turnover: 72% precision, 82% recall.
  * 97% of winners peak within 1 hour of detection (median 12 min).
  * Winners average 5.0x peak (median), p25 3.9x — the 3x math holds.

Components (evidence-weighted):

  * mcap sweet spot (0-40): $30k-$200k is the 3x zone;
  * turnover (0-30): vol/mcap — small coin with huge volume = coiled;
  * smart money (0-20): grade-weighted tracked-wallet buys as confirmation;
  * freshness (0-10): newer pools have more runway (speed game).

Qualifying bar: 60 (backtest: 71% precision, 89% recall on confirmed).

The bot itself stays paper-only: degen lane emits ALERTS for the user's
manual trading. The bot's own paper entries on degen signals are small
(0.01 SOL) and few — they exist so the ledger can prove expectancy.

Feature flag: ``degen_lane_enabled`` (off -> on). Knobs are read off the
degen config object with safe defaults so the module works standalone.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

# Hard blocks: these mean certain loss, never loosened, even in degen mode.
# Matched (case-insensitive substring) against safety report fail strings.
_HARD_BLOCK_MARKERS = (
    "honeypot",
    "cannot sell",
    "cannot-sell",
    "cannotsell",
)

# Dust floor: below this mcap there is no room for a real run.
_DUST_MCAP_USD = 10_000.0


def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default  # NaN -> default


def _cfg(degen: Any, name: str, default: float) -> float:
    try:
        return float(getattr(degen, name, default))
    except (TypeError, ValueError):
        return default


def hard_block_reason(safety_report: Optional[Dict[str, Any]]) -> Optional[str]:
    """Return the hard-block reason, or None if the coin is not certainly dead.

    Only honeypot / cannot-sell block. Everything else is a label.
    Fail-OPEN on missing/unknown reports: unknown safety does not block
    in degen mode (it becomes a UNVETTED label instead).
    """
    if not isinstance(safety_report, dict):
        return None
    fails = safety_report.get("fails") or []
    for fail in fails:
        text = str(fail).lower()
        for marker in _HARD_BLOCK_MARKERS:
            if marker in text:
                return "hard-block: %s" % fail
    # Some reports carry explicit honeypot flags outside `fails`.
    for key in ("honeypot", "is_honeypot", "cannot_sell", "cannotSellAll"):
        if safety_report.get(key):
            return "hard-block: %s flagged" % key
    return None


def risk_labels(safety_report: Optional[Dict[str, Any]],
                deployer_status: str = "unknown") -> List[str]:
    """Human-readable risk factors for the card. Never blocks, only labels."""
    labels: List[str] = []
    if not isinstance(safety_report, dict) or safety_report.get("unknown"):
        labels.append("⚠️ UNVETTED — no safety report")
        return labels
    score = _f(safety_report.get("score"), -1)
    if score >= 0:
        labels.append("score %.0f/100" % score)
        if score < 30:
            labels.append("⚠️ low safety score")
    for fail in (safety_report.get("fails") or [])[:3]:
        text = str(fail)
        low = text.lower()
        if any(m in low for m in _HARD_BLOCK_MARKERS):
            continue  # hard blocks are reported separately, not as labels
        labels.append("⚠️ %s" % text)
    for warn in (safety_report.get("warnings") or [])[:3]:
        labels.append("⚠️ %s" % warn)
    taxes = safety_report.get("taxes") or {}
    buy_tax = _f(taxes.get("buy"), 0)
    sell_tax = _f(taxes.get("sell"), 0)
    if buy_tax > 5 or sell_tax > 5:
        labels.append("⚠️ tax buy %.0f%% / sell %.0f%%" % (buy_tax, sell_tax))
    if deployer_status and deployer_status != "ok":
        labels.append("👤 deployer: %s" % deployer_status)
    return labels or ["risk: none flagged"]


def _smart_money_score(buyers: List[Dict[str, Any]],
                       weights: Dict[str, float]) -> Tuple[float, List[str]]:
    """Tracked-wallet buys, WATCHLIST-ONLY since 2026-10-20.

    User-approved research change: copy/cluster-following measured -41%
    avg in the literature and our own follower grades show 0 active
    wallets, so smart money contributes 0 points here — it is recorded
    (names returned for card/journal display) but is never score
    evidence. Re-enabling requires follower-grade evidence, not a
    config edit. Returns (0.0, buyer names).
    """
    if not buyers:
        return 0.0, []
    names: List[str] = []
    for b in buyers[:8]:
        addr = str(b.get("wallet") or b.get("address") or "")
        w = float(weights.get(addr, 1.0))
        names.append("%s×%.1f" % (addr[:6], w))
    return 0.0, names


def _turnover_score(vol_usd: float, mcap_usd: float) -> float:
    """0-30. Vol/mcap ratio: small coin with huge volume = coiled spring.

    Backtest: winners median 0.28, losers median 0.02. Ratio >= 0.10
    gives 85% recall on winners.
    """
    if mcap_usd <= 0 or vol_usd <= 0:
        return 0.0
    ratio = vol_usd / mcap_usd
    if ratio >= 0.30:
        return 30.0
    if ratio >= 0.15:
        return 22.0
    if ratio >= 0.10:
        return 15.0
    if ratio >= 0.05:
        return 8.0
    return 0.0


def _mcap_sweet_spot_score(mcap_usd: float) -> float:
    """0-40. $30k-$200k is the 3x zone (71-72% precision in backtest).

    Above $200k precision collapses (12%), above $500k it's 2% — dead.
    Below $30k is too early to judge (no footprint yet).
    """
    if mcap_usd <= 0:
        return 10.0  # unknown mcap: small neutral, not a bonus
    if mcap_usd < _DUST_MCAP_USD:
        return 0.0  # dust — no real run possible
    if mcap_usd < 30_000:
        return 15.0  # very early — some room but no footprint
    if mcap_usd < 80_000:
        return 40.0  # prime zone: 71% precision
    if mcap_usd < 200_000:
        return 35.0  # sweet zone: 72% precision
    if mcap_usd < 500_000:
        return 10.0  # likely already run: 12% precision
    return 0.0  # dead zone: 2% precision


def _freshness_score(age_minutes: float) -> float:
    """0-10. Newer pools have more runway."""
    if age_minutes <= 0:
        return 5.0  # unknown age: neutral
    if age_minutes <= 30:
        return 10.0
    if age_minutes <= 120:
        return 7.0
    if age_minutes <= 360:
        return 4.0
    return 1.0


class DegenVerdict(Tuple):
    """(qualifies, score, reasons, risk_labels, smart_buyers).

    Kept as a plain tuple subclass for backward-compatible unpacking.
    """


def evaluate(mint: str,
             chain: str,
             market: Dict[str, Any],
             safety_report: Optional[Dict[str, Any]],
             smart_buyers: List[Dict[str, Any]],
             wallet_weights: Dict[str, float],
             deployer_status: str = "unknown",
             degen: Any = None,
             now: Optional[float] = None) -> Tuple[bool, float, List[str],
                                                  List[str], List[str]]:
    """Score a candidate for the degen lane.

    Returns (qualifies, score, reasons, risk_labels, smart_buyer_names).
    ``qualifies`` is True when the 3x-potential score clears the bar AND
    no hard block (honeypot/cannot-sell) fired. Bar is 60 per backtest
    calibration (71% precision, 89% recall on confirmed coins).
    """
    now = now if now is not None else time.time()
    min_score = _cfg(degen, "degen_min_score", 30.0)

    block = hard_block_reason(safety_report)
    if block:
        return False, 0.0, [block], risk_labels(safety_report,
                                               deployer_status), []

    mcap = _f(market.get("mcap_usd"), 0.0)
    vol = _f(market.get("vol_usd"), _f(market.get("vol_24h_usd"), 0.0))
    launched_ts = _f(market.get("launched_ts"), 0.0)
    age_min = (now - launched_ts) / 60.0 if launched_ts > 0 else -1.0

    mc_score = _mcap_sweet_spot_score(mcap)
    to_score = _turnover_score(vol, mcap)
    sm_score, sm_names = _smart_money_score(smart_buyers, wallet_weights)
    fr_score = _freshness_score(age_min)
    score = mc_score + to_score + sm_score + fr_score

    reasons: List[str] = []
    if mc_score >= 35:
        reasons.append("mcap sweet spot %.0f/40 ($%.0fk)" % (mc_score,
                                                            mcap / 1000))
    elif mc_score >= 10:
        reasons.append("mcap %.0f/40" % mc_score)
    if to_score >= 15:
        ratio = vol / mcap if mcap > 0 else 0
        reasons.append("turnover %.0f/30 (vol/mcap %.2f)" % (to_score, ratio))
    if sm_names:
        reasons.append("smart money (watchlist)")
    if fr_score >= 7:
        reasons.append("fresh %.0f/10" % fr_score)

    labels = risk_labels(safety_report, deployer_status)
    # Two paths to qualify:
    # 1. Footprint path: mcap sweet spot + turnover (proven 71% precision).
    # 2. Early path: strong smart money + fresh (for brand-new coins with
    #    no volume data yet — the data arrives on later polls).
    footprint_ok = mc_score >= 15 and to_score > 0
    early_ok = sm_score >= 12 and fr_score >= 7
    qualifies = score >= min_score and (footprint_ok or early_ok)
    if not qualifies and score >= min_score:
        reasons.append("held: missing mcap/turnover footprint")
    return qualifies, round(score, 1), reasons, labels, sm_names


def evaluate_launch(mint: str,
                    chain: str,
                    market: Dict[str, Any],
                    safety_report: Optional[Dict[str, Any]],
                    degen: Any = None,
                    now: Optional[float] = None) -> Tuple[bool, float,
                                                         List[str],
                                                         List[str]]:
    """Loose first-sight screen for the launch path.

    At launch detection there's almost no data (no volume, no mcap).
    This doesn't pretend to predict — it filters for *gambleable*:
    has a CA, not a honeypot, not dust, fresh. The risk labels carry
    the truth. The user's expectancy math (3 rugs / 2 winners at 3x+)
    is what makes this work, not precision.
    """
    now = now if now is not None else time.time()

    block = hard_block_reason(safety_report)
    if block:
        return False, 0.0, [block], risk_labels(safety_report, "unknown")

    # INSTANT GATE (standing rule; 2026-10-29 SR fix): an unknown safety
    # report never sends an instant card — unvetted coins belong to the
    # hourly digest only. Known reports (even score 0) still pass here;
    # hard blocks above still apply. SR (79TgAXjmp) fired instant with
    # no report at all; this gate alone stops that shape.
    if not isinstance(safety_report, dict) or safety_report.get("unknown"):
        return False, 0.0, ["unvetted: no safety report"], \
            risk_labels(safety_report, "unknown")

    mcap = _f(market.get("mcap_usd"), 0.0)
    launched_ts = _f(market.get("launched_ts"), 0.0)
    age_min = (now - launched_ts) / 60.0 if launched_ts > 0 else 0.0

    # Dust filter: below $10k there's no room for a run.
    if 0 < mcap < _DUST_MCAP_USD:
        return False, 0.0, ["dust mcap"], risk_labels(safety_report,
                                                     "unknown")
    # Too old: if it's been hours with no traction, it's not a runner.
    if age_min > 120:
        return False, 0.0, ["stale (>2h, no run)"],
        risk_labels(safety_report, "unknown")

    reasons = ["fresh launch", "gambleable (not honeypot)"]
    if mcap >= 30_000:
        reasons.append("mcap $%.0fk" % (mcap / 1000))
    labels = risk_labels(safety_report, "unknown")
    # Score is informational; this path qualifies on filters, not points.
    return True, 35.0, reasons, labels


def enriched_runner_ok(live_mcap_usd, live_liq_usd):
    """Post-enrichment runner gate for the launch path (2026-10-29 SR
    fix): a card sends ONLY on proven mcap >= $50k and liq >= $25k
    (the 2026-10-18 runner rule). Unknown/missing values count as 0
    and fail closed. This replaces the old NOMCAP 'fire anyway'
    branch, which sent cards on unproven data."""
    try:
        return float(live_mcap_usd or 0) >= 50_000.0 and \
            float(live_liq_usd or 0) >= 25_000.0
    except Exception:
        return False


def position_size_note(entry_sol: float, mcap_usd: float) -> str:
    """Plain-language downside math for the card."""
    if entry_sol <= 0:
        return ""
    worst = entry_sol  # rug = total loss
    return ("💸 If this rugs, you lose %.3f SOL. "
            "Size like it will." % worst)
