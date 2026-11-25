"""ONE shared paper-trading account for every memecoin paper lane.

Why this exists (2026-11-22, user-approved shared demo account):
before this module every lane kept a PRIVATE bankroll constant
(grad $100, ceo $100, steady $100, ...) and its own ledger, so the
demo could be "spending" $1,000+ across lanes that were presented
as one account — the same paper capital was counted several times
over, and nothing could answer "what is the account worth right
now". This module is the single source of truth for paper capital:

  * one cash balance, configurable start ($10.00 engineering
    default — the owner approved a shared $10 example on
    2026-11-22; it is a knob, not a promise),
  * lanes DEBIT on entry and CREDIT on close; a debit that the
    cash balance cannot cover is refused (fail-closed), so two
    lanes can never spend the same dollars,
  * account-level exposure = cash + open allocated cost basis;
    per-lane, per-position and daily P/L are tracked here so
    account-level risk enforcement (Stage 2: daily-loss / drawdown
    gates) has verified numbers to gate on,
  * every mutation is journaled append-only BEFORE the ledger
    write, so a crashed writer never silently drops money.

Persistence follows the repo's hardened conventions (live10
mirror / killswitch, 2026-11-11): exclusive flock around every
read-modify-write (two processes race after a watchdog restart),
per-pid tmp file + fsync + os.replace + directory fsync, a .bak
shadow after every save, and tmp-file sweeps. A corrupt live file
falls back to the shadow; if both are unreadable, debits are
REFUSED (fail-closed) while credits are journaled so no realized
money is lost.

Accounting contract (Stage 1.3, 2026-11-22 review):

  * POSITIONS track quantity and ALLOCATED cost basis (average
    cost), never stake-minus-proceeds: entry records qty and cost;
    each sale (partial or final) allocates basis in proportion to
    the units sold, realizes P/L = (gross proceeds - fees) minus
    the allocated basis, and credits NET proceeds to cash. A
    profitable tranche that recovers more than the original stake
    realizes the gain immediately; remaining basis is per-unit
    allocated and can never go negative. Tracked separately per
    position: remaining qty, remaining basis, cumulative realized
    P/L, cumulative net proceeds, cumulative fees.
  * CONSERVATION INVARIANT (no external flows): after any sale,
    cash + remaining basis == prior cash + prior basis + net
    realized P/L of the sale. Tests pin this against hand-computed
    examples, including profitable and losing partial sales.
  * OPERATION IDs. Every mutation carries a stable caller-supplied
    op_id (distinct from the WAL seq): an uncertain response must
    be retryable, so a repeated op_id returns the ORIGINAL outcome
    without re-applying — even after the position was closed and
    reopened (the retry must not attach to the new instance). The
    same op_id with different parameters is a caller bug and is
    refused. Each journaled mutation also gets an immutable event
    id (ev#<seq>) so repeated tranches of one position are
    distinct in the audit trail.
  * POSITION IDs. Every open instance carries an immutable pos_id
    "lane/mint#<debit-seq>". A mint can be closed and REOPENED
    (ceo has a 24h cooldown, not a ban); lane+mint alone cannot
    tell instance 1 from instance 2, so journal replay matches
    credits to the exact instance they settled. A replayed credit
    whose pos_id disagrees with the open instance is a DIVERGENCE:
    replay stops there and the account goes degraded rather than
    settling the wrong instance.
  * TORN JOURNAL. A crash mid-append leaves a partial LAST line;
    exactly one unparseable trailing record is tolerated and
    truncated under the lock (its money effect, if any, is lost
    with the crashed writer, which never saved state for it). Any
    other unparseable record, or a hole/inversion in the seq
    chain, means the WAL cannot be trusted: the account is
    DEGRADED — debits are refused, credits are journaled for
    manual reconciliation but not applied, and replay is skipped
    (an incomplete WAL must not drive state).
  * EQUITY BASELINES. Day-start and peak equity are captured only
    from trustworthy marks (marks_status "ok") and persisted. At a
    rollover with no trustworthy mark the previous baseline
    CARRIES and gate inputs report unavailable (fail-closed); a
    baseline is NEVER reset to a later favorable value on the same
    day. A mark's value is the liquidation value of the REMAINING
    units of the position (the lane owns units; net proceeds of
    past tranches are already in cash, so marked equity can
    neither lose nor double-count them).

PAPER ONLY. This account is simulated dollars; nothing here
touches, custodies, or prices real funds.

Env knobs (all optional; defaults are conservative engineering
defaults chosen for a $10 demo, flagged here as agent-chosen):
  PAPER_ACCOUNT_START          starting cash (default 10.00)
  PAPER_ACCOUNT_LANE_CAP       max simultaneous open cost basis
                               per lane (default: no cap)
  PAPER_ACCOUNT_MARK_TTL_S     max age of an accepted mark
                               (default 120s)
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import glob
import json
import logging
import math
import os
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

HOME = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser(
    "~/.forex-agent")
ACCOUNT_DIR = os.path.join(HOME, "paper_account")
LEDGER_PATH = os.path.join(ACCOUNT_DIR, "ledger.json")
JOURNAL_PATH = os.path.join(ACCOUNT_DIR, "events.jsonl")
LOCK_FILE = os.path.join(ACCOUNT_DIR, ".lock")

DEFAULT_START = 10.00  # owner-approved $10 example (2026-11-22)
DEFAULT_MARK_TTL_S = 120.0

_MUTATION_EVENTS = ("debit", "credit", "write_down", "pending_sol", "settle_sol", "debit_refused",
                    "hold", "hold_released", "fee")

# Cash split (2026-11-22, quarantine design v4 Stage A): proceeds are
# spendable ONLY when the tranche's exit price was a current observed
# quote (exit_provenance.CURRENT). Stale-price estimates, write-downs,
# mixed closes and unknown/legacy kinds are QUARANTINED in
# cash_estimated: visible, reported, never spendable for new entries.
# Unknown kinds fail closed into the estimated bucket.
_SPENDABLE_KINDS = frozenset({"current_price_simulated_fill"})
_EPS = 1e-9


def _env_f(name: str, default: Optional[float]) -> Optional[float]:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("paper_account: bad %s=%r, using default %s",
                       name, raw, default)
        return default


def start_balance() -> float:
    return _env_f("PAPER_ACCOUNT_START", DEFAULT_START)


def lane_cap() -> Optional[float]:
    return _env_f("PAPER_ACCOUNT_LANE_CAP", None)


def mark_ttl_s() -> float:
    return _env_f("PAPER_ACCOUNT_MARK_TTL_S", DEFAULT_MARK_TTL_S)


# ---- Stage 2 account-level risk gates -------------------------------
# INACTIVE BY DEFAULT (PAPER_ACCOUNT_GATES_ENABLED=1 opts in, on top of
# PAPER_ACCOUNT_ENABLED). Thresholds resolve in order: explicit env
# override (tests/tuning), then the OWNER-APPROVED value in
# daemon/owner_config (set via /config_set), then NOTHING - there are
# no placeholder defaults. A gate whose threshold is unset blocks the
# entry fail-closed; it never falls back to an invented number.
# Threshold selection and gate activation stay owner-owned.

# Authoritative set of owner_config keys that the entry gates read. Keep in
# lockstep with the _resolve_threshold(...) calls below; owner_config derives
# its enforcement notes from this (drift guard: tests/test_owner_config_enforced_source.py).
ENFORCED_OWNER_KEYS = ("max_daily_loss_usd", "max_drawdown_pct",
                       "max_simultaneous_trades", "max_simultaneous_positions",
                       "max_data_age_s", "max_price_impact_pct",
                       "max_total_exposure_usd",
                       "max_exposure_pct", "max_token_exposure_usd")


def gates_enabled() -> bool:
    return os.environ.get("PAPER_ACCOUNT_GATES_ENABLED") == "1"


_LEGACY_HOOKS: Dict[str, Any] = {}


def register_legacy_hook(lane: str, fn) -> None:
    """A lane that can carry LEGACY positions (account units out of step with the lane's own books) registers a callable
    returning the set of its account pos_ids whose marks are unprovable, or None when it cannot tell. Engine-loaded lane
    variants (separate module instances with their own ledger) register under their own lane name. account_marks omits
    those marks (no peak/baseline update, entry gate fails closed). In-process only; never persisted."""
    if isinstance(lane, str) and lane and callable(fn):
        _LEGACY_HOOKS[lane] = fn


def legacy_hooks() -> Dict[str, Any]:
    return dict(_LEGACY_HOOKS)


def _boundary_marks(marks):
    """Caller-supplied marks win over the central collector, but they never bypass the lanes' legacy truth: a mark for a
    position its lane cannot prove consistent is dropped HERE, at the account boundary, whatever supplied it."""
    if not marks:
        return marks
    try:
        try:
            from daemon.launch_scanner import account_marks
        except ImportError:
            import account_marks  # sibling import context (scripts)
        return account_marks.omit_unproven(marks)
    except Exception:
        return {}


def _central_marks() -> Dict[str, Dict[str, Any]]:
    """Marks from the central marker (account_marks.collect_marks),
    collected by try_debit BEFORE the account lock when the caller
    supplies none. Caller-supplied marks always win; this is only
    the default. Any failure returns an empty map, which the view
    treats as untrusted whenever positions are open (fail-closed)
    and as fully marked on an empty account. Never raises: a broken
    marker must degrade gating, not crash a lane's entry path.
    """
    try:
        try:
            from daemon.launch_scanner import account_marks
        except ImportError:
            import account_marks  # sibling import context (scripts)
        marks = account_marks.collect_marks()
        return marks if isinstance(marks, dict) else {}
    except Exception:
        return {}


def _owner_threshold(owner_key: str) -> Optional[float]:
    """Owner-approved threshold from daemon/owner_config, or None when
    unset (fail closed - never a placeholder)."""
    try:
        from daemon import owner_config  # noqa: PLC0415
        return float(owner_config.require(owner_key))
    except Exception:
        return None


def _resolve_threshold(env_name: str, owner_key: str) -> Optional[float]:
    """Effective gate limit. TIGHTEN-ONLY: an env override (runtime/
    fixture config, NOT authenticated owner approval) may tighten the
    owner-approved limit but never loosen it; with both present the
    LOWER value wins. With only one, that one applies. With neither,
    None - the gate fails closed."""
    env_v = _env_f(env_name, None)
    owner_v = _owner_threshold(owner_key)
    if env_v is not None and owner_v is not None:
        return min(env_v, owner_v)
    return env_v if env_v is not None else owner_v


def max_daily_loss_usd() -> Optional[float]:
    return _resolve_threshold("PAPER_ACCOUNT_MAX_DAILY_LOSS_USD",
                              "max_daily_loss_usd")


def max_drawdown_pct() -> Optional[float]:
    return _resolve_threshold("PAPER_ACCOUNT_MAX_DRAWDOWN_PCT",
                              "max_drawdown_pct")


def max_exposure_pct() -> Optional[float]:
    return _resolve_threshold("PAPER_ACCOUNT_MAX_EXPOSURE_PCT",
                              "max_exposure_pct")


def max_simultaneous_trades() -> Optional[float]:
    return _resolve_threshold("PAPER_ACCOUNT_MAX_SIMULTANEOUS",
                              "max_simultaneous_trades")


def max_simultaneous_positions() -> Optional[float]:
    """Directive: Maximum simultaneous positions (distinct tokens). A
    separate mandatory cap from max_simultaneous_trades (open lots)."""
    return _resolve_threshold("PAPER_ACCOUNT_MAX_POSITIONS",
                              "max_simultaneous_positions")


def max_price_impact_pct() -> Optional[float]:
    """Maximum Jupiter quote price impact (percent). MANDATORY for demo
    execution entries: unset refuses."""
    return _resolve_threshold("PAPER_ACCOUNT_MAX_PRICE_IMPACT_PCT",
                              "max_price_impact_pct")


def max_data_age_s() -> Optional[float]:
    """Directive: Maximum data age. MANDATORY for entries (unset refuses);
    when set it also TIGHTENS the accepted age of marks below the legacy
    default TTL. Never loosens it."""
    return _resolve_threshold("PAPER_ACCOUNT_MAX_DATA_AGE_S",
                              "max_data_age_s")


def max_total_exposure_usd() -> Optional[float]:
    return _resolve_threshold("PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD",
                              "max_total_exposure_usd")


def max_token_exposure_usd() -> Optional[float]:
    """Maximum exposure to ONE token (mint), across all lanes. Mandatory:
    unset -> new entries fail closed."""
    return _resolve_threshold("PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD",
                              "max_token_exposure_usd")


def configured_trade_size() -> Optional[float]:
    """The configured EXACT trade size lanes should use, or None when no
    valid size is configured (unset, invalid, or env/owner conflict; the
    debit/hold gate refuses invalid and conflicting values itself)."""
    try:
        from daemon import owner_config  # noqa: PLC0415
        state, owner_v = owner_config.stored_state("trade_amount_usd")
    except Exception:
        return None
    env_raw = os.environ.get("PAPER_ACCOUNT_TRADE_SIZE_USD")
    env_v = None
    if env_raw not in (None, ""):
        try:
            env_v = float(env_raw)
        except ValueError:
            return None
        if not (env_v > 0 and env_v == env_v and env_v != float("inf")):
            return None
    if state == "invalid":
        return None
    if state == "ok":
        if env_v is not None and abs(env_v - owner_v) > 1e-9:
            return None
        return float(owner_v)
    return env_v


def _trade_size_refusal(new_stake: float) -> Optional[str]:
    """Exact configured trade size (directive: a fixed size is used as
    configured, not as a cap). Applies only when a size is configured;
    unset leaves lane sizing unchanged (no invented default). A present
    but invalid value, or an env value conflicting with the owner value,
    refuses fail-closed."""
    try:
        from daemon import owner_config  # noqa: PLC0415
        state, owner_v = owner_config.stored_state("trade_amount_usd")
    except Exception:
        return "gate: configured trade size unreadable; entry blocked fail-closed"
    if state == "invalid":
        return ("gate: configured trade_amount_usd is present but invalid; "
                "entry blocked fail-closed")
    env_raw = os.environ.get("PAPER_ACCOUNT_TRADE_SIZE_USD")
    env_v = None
    if env_raw not in (None, ""):
        try:
            env_v = float(env_raw)
        except ValueError:
            return "gate: PAPER_ACCOUNT_TRADE_SIZE_USD invalid; entry blocked fail-closed"
        if not (env_v > 0 and env_v == env_v and env_v != float("inf")):
            return "gate: PAPER_ACCOUNT_TRADE_SIZE_USD invalid; entry blocked fail-closed"
    want = owner_v if state == "ok" else env_v
    if state == "ok" and env_v is not None and abs(env_v - owner_v) > 1e-9:
        return ("gate: trade size conflict (owner %.4f vs env %.4f); entry "
                "blocked fail-closed" % (owner_v, env_v))
    if want is None:
        return None
    if abs(float(new_stake) - want) > 1e-9 * max(1.0, want):
        return ("gate: stake $%.4f is not the configured trade size $%.4f "
                "(exact size, not a cap)" % (float(new_stake), want))
    return None


def _held_total(led: Dict[str, Any]) -> float:
    """Sum of reserved (unconsumed, unreleased) hold amounts."""
    return sum(float(h["amount"]) for h in (led.get("holds") or {}).values())


def _position_identities(led: Dict[str, Any]) -> set:
    """Distinct POSITION identities: one per token (mint) held across all
    lanes and lots (an add-on lot or another lane entering the same token
    is the same position), one per pending-SOL entry, and one per hold
    (a hold with a recorded mint joins that token's identity)."""
    ids = set()
    for st in led.get("lanes", {}).values():
        for key, p in (st.get("open") or {}).items():
            ids.add(("mint", p.get("mint", key)))
    for pid in (led.get("pending_sol") or {}):
        ids.add(("pending", pid))
    for op, h in (led.get("holds") or {}).items():
        ids.add(("mint", h["mint"]) if h.get("mint") else ("hold", op))
    return ids


def _open_trade_count(led: Dict[str, Any]) -> int:
    """Open TRADES: every open lot in every lane, every pending-SOL entry
    and every reserved hold. An add-on lot or a second lane on a held token
    is a separate trade (conservative; no identity merging)."""
    n = sum(len(st.get("open", {})) for st in led.get("lanes", {}).values())
    return n + len(led.get("pending_sol", {})) + len(led.get("holds") or {})


def _open_position_count(led: Dict[str, Any], mint: Optional[str] = None) -> int:
    """Open positions by IDENTITY (see _position_identities). With `mint`,
    the count AFTER an entry on that mint: +1 only when that token is not
    already a position."""
    ids = _position_identities(led)
    if mint:
        ids.add(("mint", mint))
    return len(ids)


def _gate_check_locked(led: Dict[str, Any], view: Dict[str, Any],
                       new_stake: float,
                       pure_stake: Optional[float] = None,
                       mint: Optional[str] = None) -> Optional[str]:
    """Account-level entry gate, evaluated UNDER the account lock as
    part of the debit's critical section: the evaluation and the
    reservation are atomic, so two lanes can never each pass on the
    same available capacity. Returns None when entry is allowed,
    else the refusal reason. Fail-closed: anything untrusted blocks
    new risk. Exits never pass through here (risk reduction is
    always allowed; credit() enforces ownership, quantity, op
    idempotency and ledger integrity instead).

    Gates (thresholds: env override, else owner_config, else the gate
    FAILS CLOSED - no placeholder defaults):
      * degraded WAL (handled by the caller before this),
      * marks: with any position open, every mark must be present,
        fresh, finite and provenanced (marks_status "ok"), else the
        equity figure is untrusted and entries block; an empty
        account is fully marked by definition (equity == cash),
      * daily loss vs the persisted day-start equity baseline,
      * drawdown vs the persisted peak equity,
      * total exposure after this entry vs marked equity.
    book_value is NEVER a gate input (cost basis hides unrealized
    losses).
    """
    if view["marks_status"] != "ok":
        return ("gate: marks not trustworthy (%s); entry blocked "
                "fail-closed" % view["marks_status"])
    me = view["marked_equity_conservative"]
    dl = view.get("daily_loss")
    if dl is None or view.get("drawdown_pct") is None:
        return ("gate: no trustworthy day-start/peak baseline yet; "
                "entry blocked fail-closed")
    dl_lim, dd_lim, exp_lim = (max_daily_loss_usd(), max_drawdown_pct(),
                               max_exposure_pct())
    if dl_lim is None or dd_lim is None or exp_lim is None:
        unset = [n for n, v in (("max_daily_loss_usd", dl_lim),
                                ("max_drawdown_pct", dd_lim),
                                ("max_exposure_pct", exp_lim))
                 if v is None]
        return ("gate: no owner-approved limit for %s; entry blocked "
                "fail-closed (set via /config_set or env override)"
                % ", ".join(unset))
    # new_stake is the full reserve (exposure/cash); the configured size
    # applies to the PURE stake when the caller reserves stake + fee.
    _ts = _trade_size_refusal(new_stake if pure_stake is None else pure_stake)
    if _ts:
        return _ts
    if max_data_age_s() is None:
        return ("gate: no owner-approved limit for max_data_age_s; entry "
                "blocked fail-closed (set via /config_set or env override)")
    tok_lim = max_token_exposure_usd()
    if tok_lim is None:
        return ("gate: no owner-approved limit for max_token_exposure_usd; "
                "entry blocked fail-closed (set via /config_set or env override)")
    if not mint:
        return "gate: entry has no mint; per-token exposure unknown; entry blocked fail-closed"
    # Per-MINT exposure from TRUSTED MARKS (view marks_status is ok here),
    # across all lanes, plus reserved holds for this mint (a hold with no
    # recorded mint cannot be attributed, so it counts conservatively).
    tok_now = float((view.get("marked_by_mint") or {}).get(mint, 0.0))
    tok_hold = sum(float(h["amount"]) for h in (led.get("holds") or {}).values()
                   if h.get("mint") in (None, mint))
    if tok_now + tok_hold + new_stake > tok_lim:
        return ("gate: token %s exposure $%.2f (marked $%.2f + held $%.2f) + "
                "new $%.2f > limit $%.2f"
                % (mint, tok_now + tok_hold + new_stake, tok_now, tok_hold,
                   new_stake, tok_lim))
    sim_lim, tot_lim = max_simultaneous_trades(), max_total_exposure_usd()
    pos_lim = max_simultaneous_positions()
    if sim_lim is None or tot_lim is None or pos_lim is None:
        unset = [n for n, v in (("max_simultaneous_trades", sim_lim),
                                ("max_simultaneous_positions", pos_lim),
                                ("max_total_exposure_usd", tot_lim))
                 if v is None]
        return ("gate: no owner-approved limit for %s; entry blocked "
                "fail-closed (set via /config_set or env override)"
                % ", ".join(unset))
    # Reserved holds are ADMITTED reservations (consume turns one into a
    # position and removes it in the same locked mutation), so they count
    # as exposure and as a position slot here. Held cash stays inside
    # `cash`, so the view's cost_exposure/book_value must not include it.
    held = _held_total(led)
    if _open_trade_count(led) + 1 > sim_lim:
        return ("gate: simultaneous trades %d (after this entry, every open "
                "lot/pending/hold counts) > limit %d"
                % (_open_trade_count(led) + 1, int(sim_lim)))
    if _open_position_count(led, mint) > pos_lim:
        return ("gate: simultaneous positions %d (after this entry, by "
                "token identity) > limit %d"
                % (_open_position_count(led, mint), int(pos_lim)))
    if view["cost_exposure"] + held + new_stake > tot_lim:
        return ("gate: total exposure $%.2f after entry > limit $%.2f"
                % (view["cost_exposure"] + held + new_stake, tot_lim))
    if dl > dl_lim:
        return ("gate: daily loss $%.2f > limit $%.2f"
                % (dl, dl_lim))
    if view["drawdown_pct"] > dd_lim:
        return ("gate: drawdown %.1f%% > limit %.1f%%"
                % (view["drawdown_pct"], dd_lim))
    if me <= 0:
        return "gate: marked equity $%.2f; no capacity" % me
    after = view["cost_exposure"] + held + new_stake
    if after > exp_lim / 100.0 * me:
        return ("gate: exposure $%.2f after entry > %.0f%% of marked "
                "equity $%.2f" % (after, exp_lim, me))
    return None


# ---- Section 4.5 provider-health decision gate ----------------------
# Operational memory informs the decision engine (request 4.5: "the
# bot must know when its market information is incomplete and stop
# treating uncertain information as reliable"). When
# PROVIDER_HEALTH_GATE_ENABLED=1 (default OFF - gate activation stays
# owner-owned), every entry debit consults the DURABLE acquisition
# provider-health record before reserving stake. The providers the
# decision depends on are named explicitly in
# PROVIDER_HEALTH_GATE_PROVIDERS (comma-separated); an enabled gate
# with no provider list is an incomplete configuration and fails
# closed. Unknown, stale, degraded, down or unconfigured health for a
# listed provider BLOCKS the entry. No thresholds are invented: the
# durable record's own status classification is the only input. Never
# raises: a gate error fails closed.

def provider_health_gate_enabled() -> bool:
    return os.environ.get("PROVIDER_HEALTH_GATE_ENABLED") == "1"


def provider_health_gate_mode() -> str:
    """Gate semantics (default "any_down", the original behavior):
    any named provider not-ok blocks the entry. "all_down"
    (2026-11-23, section 4.5 faithful to the fallback chain): the
    entry is refused only when NO named provider is known-ok -
    Configured names must be interchangeable for the SAME
    entry-critical capability. This status-only gate cannot establish
    capability coverage; mixing unrelated provider roles is invalid.
    A healthy provider does not prove any particular read succeeded.
    Unknown mode strings fail closed
    (treated as incomplete configuration)."""
    return os.environ.get("PROVIDER_HEALTH_GATE_MODE") or "any_down"


def _provider_health_refusal() -> Optional[str]:
    """None when the entry may proceed; the refusal reason otherwise.
    Only consulted when provider_health_gate_enabled()."""
    raw = os.environ.get("PROVIDER_HEALTH_GATE_PROVIDERS") or ""
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return ("gate: provider-health gate enabled but "
                "PROVIDER_HEALTH_GATE_PROVIDERS names no providers; "
                "entry blocked fail-closed (incomplete configuration)")
    try:
        try:
            from daemon.intelligence.acquisition import provider_health
        except ImportError:
            from intelligence.acquisition import provider_health
        view = provider_health.read_durable_statuses()
    except Exception as exc:
        return ("gate: provider health unreadable (%s); entry blocked "
                "fail-closed" % exc)
    if view is None:
        return ("gate: no durable provider-health record; market-data "
                "reliability unknown, entry blocked fail-closed")
    if view.get("stale"):
        return ("gate: durable provider-health record is stale (age "
                "%ss); entry blocked fail-closed" % view.get("age_s"))
    statuses = view.get("providers") or {}
    mode = provider_health_gate_mode()
    if mode == "all_down":
        # Refuse only when NO named provider is known-ok. A
        # missing record is not ok: unknown health cannot be the
        # reliable source the entry relies on.
        states = {n: statuses.get(n) for n in names}
        if any(st == "ok" for st in states.values()):
            return None
        return ("gate: no named provider known-ok (%s); market "
                "information not reliable, entry blocked "
                "fail-closed" % ", ".join(
                    "%s=%s" % (n, st if st is not None
                               else "no-record")
                    for n, st in states.items()))
    if mode != "any_down":
        return ("gate: unknown PROVIDER_HEALTH_GATE_MODE %r; entry "
                "blocked fail-closed (incomplete configuration)"
                % mode)
    for name in names:
        st = statuses.get(name)
        if st is None:
            return ("gate: no health record for provider %s; entry "
                    "blocked fail-closed" % name)
        if st != "ok":
            return ("gate: provider %s %s; market information not "
                    "reliable, entry blocked fail-closed" % (name, st))
    return None


def _new_ledger() -> Dict[str, Any]:
    now = time.time()
    return {
        "account_id": uuid.uuid4().hex,
        "created_ts": now,
        "seq": 0,
        "start_balance": start_balance(),
        "cash": start_balance(),
        "cash_spendable": start_balance(),
        "cash_estimated": 0.0,
        # pos_id -> {lane, mint, qty, basis, entry_qty, entry_cost,
        #            reason, ts, status}: write-down register.
        # Units moved here are HELD UNRESOLVED: not sold (no fee,
        # no proceeds, no realized loss recognized), carrying value
        # zero, cost preserved for later evidence-led resolution
        # (design v4 Stage B). Never re-sellable.
        "unresolved": {},
        # op_id -> {pos_id, lane, mint, qty, basis, proceeds, fee,
        #           price_kind, ts, status}: units the lane recorded
        # as sold under an UNVERIFIED price (any non-spendable
        # price_kind). The proceeds sit quarantined in
        # cash_estimated; the units are neither open nor
        # held_unresolved - they are assumed_sold_unverified
        # (design v4 Stage C). Permanently quarantined absent a
        # timestamp-correct fill model; no promotion path exists.
        "assumed_sold": {},
        "realized_pnl": 0.0,
        "day": time.strftime("%Y-%m-%d", time.gmtime(now)),
        "day_realized_pnl": 0.0,
        # op_id -> {lane, amount, ts}: spendable cash RESERVED at seal
        # time for an execution whose outcome is not yet known. Held
        # cash is still ours (cash unchanged) but unspendable by every
        # lane and book until consumed (entry/fee) or released
        # (unlanded). Holds never expire silently: an unresolved
        # execution's exposure is never freed without a recorded
        # resolution.
        "holds": {},
        "lanes": {},
        # op_id -> {"event": kind, "lane":, "mint":, "pos_id":,
        #           "event_id":, params...}: caller-retry dedupe.
        "ops": {},
        # Equity baselines: captured only from trustworthy marks,
        # never reset to a later favorable value (see docstring).
        "day_baseline": None,   # {"day":, "value":, "ts":}
        "peak_equity": None,    # {"value":, "ts":}
    }


def _lane(led: Dict[str, Any], lane: str) -> Dict[str, Any]:
    lanes = led.setdefault("lanes", {})
    if lane not in lanes:
        lanes[lane] = {"open": {}, "realized_pnl": 0.0,
                       "n_entries": 0, "n_closes": 0}
    return lanes[lane]


def _pos_id(lane: str, mint: str, seq: int) -> str:
    """Immutable ID of one open instance: unique per debit, stable
    across reopens of the same mint."""
    return "%s/%s#%d" % (lane, mint, seq)


def _pos_basis(pos: Dict[str, Any]) -> float:
    """Remaining allocated cost basis (v2) or full stake (legacy
    Stage-1 record, which has no quantity tracking)."""
    if "basis" in pos:
        return float(pos["basis"])
    return float(pos["stake"])


def _pos_qty(pos: Dict[str, Any]) -> Optional[float]:
    q = pos.get("qty")
    return float(q) if isinstance(q, (int, float)) else None


def _valid_amount(v: Any) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


@contextlib.contextmanager
def _lock():
    """Exclusive inter-process lock for the account ledger. Same
    pattern as the live10 mirror / killswitch locks: two processes
    can exist after a watchdog restart, and the fd is released
    automatically if the holder dies."""
    os.makedirs(ACCOUNT_DIR, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    """Per-pid tmp + fsync + os.replace + directory fsync (the
    shared-tmp race is the prime suspect for the five 0-byte ledger
    incidents, 2026-11-04..11)."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except Exception:
        raise


def _journal(event: Dict[str, Any]) -> None:
    """Append-only event journal, written BEFORE the ledger save:
    if the process dies mid-save the money movement is still
    recoverable from the journal."""
    event = dict(event)
    event["ts"] = event.get("ts") or time.time()
    try:
        with open(JOURNAL_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        logger.exception("paper_account: journal write failed")
        if event.get("instance") is True:
            raise


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _journal_read() -> Tuple[List[Dict[str, Any]], Optional[str],
                             Optional[int]]:
    """Parse the WAL. Returns (entries, status, tail_offset).

    status is None when the journal is clean, "torn_tail" when
    exactly one unparseable record sits at the END of the file
    (the signature of a crash mid-append; tail_offset is the byte
    the caller truncates to, under the lock), and "torn" for any
    other unparseable content — mid-file corruption or multiple
    bad records, which no automatic repair can justify.
    """
    entries: List[Dict[str, Any]] = []
    bad: List[int] = []
    last_off: Optional[int] = None
    try:
        with open(JOURNAL_PATH, "rb") as f:
            off = 0
            for raw in f:
                cur = off
                off += len(raw)
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                last_off = cur
                try:
                    ev = json.loads(line)
                    if isinstance(ev, dict):
                        entries.append(ev)
                except ValueError:
                    bad.append(cur)
    except FileNotFoundError:
        return [], None, None
    except Exception:
        return [], "torn", None
    if not bad:
        return entries, None, None
    if len(bad) == 1 and bad[0] == last_off:
        return entries, "torn_tail", bad[0]
    return entries, "torn", None


def _journal_truncate(offset: int) -> None:
    """Drop a torn tail record. Caller must hold _lock(). The torn
    bytes are a partial write whose state was never saved, so no
    money movement is lost with them."""
    with open(JOURNAL_PATH, "r+") as f:
        f.truncate(offset)
        f.flush()
        os.fsync(f.fileno())


def _seq_violation(entries: List[Dict[str, Any]]
                   ) -> Optional[Tuple[Any, Any]]:
    """Mutation events carry a strictly +1 seq chain. A gap means
    a journaled mutation was lost (replay would skip money); a
    duplicate or inversion means the file was tampered with or
    two uncoordinated writers raced. Either way: untrusted."""
    seqs = [ev.get("seq") for ev in entries
            if ev.get("event") in _MUTATION_EVENTS
            and isinstance(ev.get("seq"), int)]
    for a, b in zip(seqs, seqs[1:]):
        if b != a + 1:
            return (a, b)
    return None


def _apply_debit(led: Dict[str, Any], lane: str, mint: str,
                 stake: float, qty: Optional[float], pid: str,
                 ts: Optional[float], instance: bool = False) -> None:
    """Shared debit mutation. Caller holds the lock and has
    validated input and cash."""
    led["cash"] = round(float(led["cash"]) - stake, 6)
    led["cash_spendable"] = round(
        float(led.get("cash_spendable", led["cash"] + stake))
        - stake, 6)
    st = _lane(led, lane)
    key = pid if instance else mint
    st["open"][key] = {
        **({"mint": mint, "instance": True} if instance else {}),
        "pos_id": pid,
        "qty": qty,                 # remaining units
        "basis": stake,             # remaining allocated cost
        "entry_qty": qty,
        "entry_cost": stake,
        "realized_pnl": 0.0,
        "proceeds": 0.0,            # cumulative NET proceeds
        "fees": 0.0,
        "ts": ts or time.time(),
    }
    st["n_entries"] += 1


def _position_key(st: dict, mint: str, pos_id: Optional[str]) -> str:
    if pos_id is not None:
        pos = st["open"].get(pos_id)
        if (isinstance(pos, dict) and pos.get("instance") is True
                and pos.get("mint") == mint and pos.get("pos_id") == pos_id):
            return pos_id
    return mint


def _apply_credit(led: Dict[str, Any], lane: str, mint: str,
                  qty_sold: Optional[float], net: float, fee: float,
                  final: bool,
                  price_kind: Optional[str] = None,
                  op_id: Optional[str] = None,
                  ts: Optional[float] = None,
                  pos_id: Optional[str] = None
                  ) -> Tuple[Optional[float], str]:
    """Shared credit mutation. Returns (realized_delta, "") or
    (None, reason) leaving state untouched. Average-cost
    allocation; a partial sale realizes P/L against the allocated
    basis of the units sold. Caller holds the lock."""
    st = _lane(led, lane)
    key = _position_key(st, mint, pos_id)
    pos = st["open"].get(key)
    if pos is None:
        return None, "no open position"
    basis = _pos_basis(pos)
    have_qty = _pos_qty(pos)
    disposed = qty_sold
    if final or qty_sold is None:
        if qty_sold is not None and have_qty is not None \
                and qty_sold < have_qty - _EPS:
            return None, "qty under remaining: not final"
        disposed = have_qty
        alloc = basis
        realized = round(net - alloc, 6)
        st["open"].pop(key)
        pos["basis"] = 0.0
        pos["qty"] = 0.0 if have_qty is not None else None
        closed = True
    elif have_qty is None or "basis" not in pos:
        # Legacy Stage-1 record: no quantity tracking, so no
        # allocation is possible. Full close only.
        return None, "legacy position: full close only"
    else:
        if not _valid_amount(qty_sold) or qty_sold <= 0:
            return None, "bad qty"
        if qty_sold > have_qty + _EPS:
            return None, "qty exceeds remaining units"
        if qty_sold >= have_qty - _EPS:
            # De-facto full close: this tranche consumes every
            # remaining unit (within dust), so it settles ALL the
            # remaining basis — the same accounting as an explicit
            # final close. A tranche that left dust units behind
            # would strand their basis in a zombie position.
            disposed = have_qty
            alloc = basis
            realized = round(net - alloc, 6)
            st["open"].pop(key)
            pos["basis"] = 0.0
            pos["qty"] = 0.0
            closed = True
        else:
            alloc = round(basis * (qty_sold / have_qty), 6)
            alloc = min(alloc, basis)
            realized = round(net - alloc, 6)
            pos["qty"] = round(have_qty - qty_sold, 12)
            pos["basis"] = round(basis - alloc, 6)
            closed = False
    pos["realized_pnl"] = round(
        float(pos.get("realized_pnl") or 0.0) + realized, 6)
    pos["proceeds"] = round(
        float(pos.get("proceeds") or 0.0) + net, 6)
    pos["fees"] = round(float(pos.get("fees") or 0.0) + fee, 6)
    led["cash"] = round(float(led["cash"]) + net, 6)
    if price_kind in _SPENDABLE_KINDS:
        led["cash_spendable"] = round(
            float(led.get("cash_spendable") or 0.0) + net, 6)
    else:
        led["cash_estimated"] = round(
            float(led.get("cash_estimated") or 0.0) + net, 6)
    led["realized_pnl"] = round(
        float(led["realized_pnl"]) + realized, 6)
    led["day_realized_pnl"] = round(
        float(led.get("day_realized_pnl") or 0.0) + realized, 6)
    st["realized_pnl"] = round(
        float(st["realized_pnl"]) + realized, 6)
    if closed:
        st["n_closes"] += 1
    if price_kind not in _SPENDABLE_KINDS:
        # Unit-state bookkeeping (design v4 Stage C): the disposed
        # units are ASSUMED SOLD, proceeds UNVERIFIED - recorded so
        # every unit is in exactly one economic state and the
        # original-cost conservation track stays auditable. The
        # proceeds are already quarantined in cash_estimated above.
        reg = led.setdefault("assumed_sold", {})
        reg[op_id or "%s:%s:%s" % (lane, mint, pos_id)] = {
            "pos_id": pos_id,
            "lane": lane, "mint": mint,
            "qty": disposed,
            "basis": alloc,
            "proceeds": net,
            "fee": fee,
            "price_kind": price_kind or "unknown",
            "ts": ts or time.time(),
            "status": "assumed_sold_unverified",
        }
    return realized, ""


def _reconcile(led: Dict[str, Any], entries: List[Dict[str, Any]]
               ) -> Tuple[Dict[str, Any], Optional[str]]:
    """Replay journaled mutations the ledger has not reached.

    The journal is the write-ahead log (every mutation is journaled
    BEFORE the state save), so after a crash the ledger can be
    BEHIND the journal — and a stale .bak shadow can be behind by
    more. Without reconciliation, restoring that shadow would
    RESURRECT a settled position, and its later close would credit
    the money a second time. Every mutation carries a monotonic
    seq (the state version AFTER the mutation), an immutable
    event id, the caller's op_id, and the pos_id of the open
    INSTANCE it moved; replay applies journaled entries past the
    ledger's seq, idempotently:

      any event whose op_id is already recorded in the state is
      skipped (caller retry that reached the journal twice, or a
      stale shadow re-seeing an applied op);
      debit  — skipped when the same pos_id is already open,
               applied when the mint is closed; a mint open under
               a DIFFERENT pos_id is a divergence (stop, degraded);
      credit — applied when the matching pos_id is open; skipped
               when the mint is fully closed (already settled); a
               mint open under a different pos_id is a divergence
               (a credit for instance #1 must never settle a
               reopened instance #2). Partial credits replay with
               the journaled qty/fee so tranche math is identical.

    Entries without a seq predate this scheme; reconciliation runs
    only over seq-bearing entries, and anything ambiguous is a
    divergence, never a guess. Returns (ledger, divergence) where
    divergence is None or a human-readable reason; on divergence
    replay STOPS at the offending entry (earlier valid mutations
    are kept) and the caller degrades the account.
    """
    led_seq = led.get("seq")
    if not isinstance(led_seq, int):
        return led, "ledger has no seq"
    ops = led.setdefault("ops", {})
    pending = []
    for ev in entries:
        ev_seq = ev.get("seq")
        if not isinstance(ev_seq, int):
            continue
        if ev_seq > led_seq:
            pending.append((ev_seq, ev))
    if not pending:
        return led, None
    pending.sort(key=lambda t: t[0])
    replayed = 0
    for ev_seq, ev in pending:
        kind = ev.get("event")
        lane, mint = ev.get("lane"), ev.get("mint")
        op_id = ev.get("op_id")
        if kind in _MUTATION_EVENTS and op_id and op_id in ops:
            # Caller op already applied: idempotent skip. The seq
            # still advances — the state includes this mutation.
            led["seq"] = ev_seq
            continue
        if kind == "debit" and lane and mint:
            pid = ev.get("pos_id") or _pos_id(lane, mint, ev_seq)
            st = _lane(led, lane)
            key = pid if ev.get("instance") is True else mint
            cur = st["open"].get(key)
            if cur is not None:
                if cur.get("pos_id", _pos_id(lane, mint, ev_seq)) == pid:
                    if op_id:
                        ops[op_id] = {"event": "debit", "pos_id": pid,
                                      "lane": lane, "mint": mint,
                                      "stake": ev.get("stake"), "qty": ev.get("qty"),
                                      **({"instance": True} if ev.get("instance") is True else {})}
                    led["seq"] = ev_seq
                    continue  # state already has it: idempotent
                return led, ("replay divergence: debit %s but %s "
                             "open as %s"
                             % (pid, mint, cur.get("pos_id")))
            stake = float(ev.get("stake") or 0)
            if stake <= 0 or float(led["cash"]) < stake:
                return led, ("replay cannot apply debit %s "
                             "(bad stake or cash)" % pid)
            _apply_debit(led, lane, mint, stake, ev.get("qty"),
                         pid, ev.get("ts"), instance=ev.get("instance") is True)
            if op_id:
                ops[op_id] = {"event": "debit", "pos_id": pid,
                              "lane": lane, "mint": mint,
                              "stake": stake, "qty": ev.get("qty"),
                              **({"instance": True} if ev.get("instance") is True else {})}
            replayed += 1
        elif kind == "credit" and lane and mint:
            st = _lane(led, lane)
            pos = st["open"].get(_position_key(st, mint, ev.get("pos_id")))
            if pos is None:
                if ev.get("instance") is True:
                    return led, "replay divergence: instance credit has no matching lot"
                led["seq"] = ev_seq
                continue  # already settled: idempotent skip
            pid = ev.get("pos_id")
            if pid and pos.get("pos_id") and pos["pos_id"] != pid:
                return led, ("replay divergence: credit %s but %s "
                             "open as %s"
                             % (pid, mint, pos["pos_id"]))
            realized, why = _apply_credit(
                led, lane, mint, ev.get("qty"),
                float(ev.get("net") if ev.get("net") is not None
                      else (float(ev.get("proceeds") or 0)
                            - float(ev.get("fee") or 0))),
                float(ev.get("fee") or 0),
                bool(ev.get("final", True)),
                price_kind=ev.get("price_kind"),
                op_id=op_id, ts=ev.get("ts"), pos_id=pid)
            if realized is None:
                return led, ("replay cannot apply credit %s/%s: %s"
                             % (lane, mint, why))
            if op_id:
                ops[op_id] = {"event": "credit", "lane": lane,
                              "mint": mint, "pos_id": pid,
                              "realized": realized,
                              **({"instance": True, "proceeds": ev.get("proceeds"),
                                  "fee": ev.get("fee"), "qty": ev.get("qty"),
                                  "price_kind": ev.get("price_kind")}
                                 if ev.get("instance") is True else {})}
            replayed += 1
        elif kind == "write_down" and lane and mint:
            ok, why = _apply_write_down(
                led, lane, mint, ev.get("pos_id"),
                ev.get("reason"), ev.get("ts"), qty=ev.get("qty"))
            if not ok:
                return led, ("replay cannot apply write_down "
                             "%s/%s: %s" % (lane, mint, why))
            if op_id:
                ops[op_id] = {"event": "write_down", "lane": lane,
                              "mint": mint,
                              "pos_id": ev.get("pos_id"),
                              **({"instance": True, "qty": ev.get("qty"),
                                  "reason": ev.get("reason")}
                                 if ev.get("instance") is True else {})}
            replayed += 1
        elif kind == "debit_refused":
            if not op_id or not (ev.get("instance") is True or ev.get("durable_refusal") is True):
                return led, "invalid refused entry event"
            ops[op_id] = {k:v for k,v in ev.items() if k not in ("seq", "event_id", "ts")}
            replayed += 1
        elif kind == "hold":
            amount = float(ev.get("amount") or 0)
            if amount <= 0 or not lane:
                return led, "replay cannot apply hold (bad amount/lane)"
            _st = ev.get("stake")
            _mt = ev.get("mint")
            led.setdefault("holds", {})[op_id] = {
                "lane": lane, "amount": amount, "ts": ev.get("ts"),
                **({"stake": float(_st)} if _st is not None else {}),
                **({"mint": _mt} if _mt else {})}
            led["cash_spendable"] = round(
                float(led.get("cash_spendable", 0.0)) - amount, 8)
            if op_id:
                ops[op_id] = {"event": "hold", "lane": lane,
                              "amount": amount,
                              **({"stake": float(_st)} if _st is not None else {}),
                              **({"mint": _mt} if _mt else {})}
            replayed += 1
        elif kind == "hold_released":
            hold_op = ev.get("hold_op_id")
            amount = float(ev.get("amount") or 0)
            held = led.setdefault("holds", {}).pop(hold_op, None)
            if held is None:
                # A release for a hold that never existed would MINT
                # spendable cash: corrupt journal, fail closed.
                return led, ("replay divergence: hold release for "
                             "unknown hold %s" % hold_op)
            if abs(float(held["amount"]) - amount) > 1e-9:
                return led, ("replay divergence: hold release $%.8f "
                             "but held $%.8f"
                             % (amount, float(held["amount"])))
            led["cash_spendable"] = round(
                float(led.get("cash_spendable", 0.0)) + amount, 8)
            if op_id:
                ops[op_id] = {"event": "hold_released",
                              "hold_op_id": hold_op, "amount": amount}
            replayed += 1
        elif kind == "fee":
            amount = float(ev.get("amount") or 0)
            if amount <= 0:
                return led, "replay cannot apply fee (bad amount)"
            led["cash"] = round(float(led.get("cash", 0.0)) - amount, 8)
            led["cash_spendable"] = round(
                float(led.get("cash_spendable", 0.0)) - amount, 8)
            if op_id:
                ops[op_id] = {"event": "fee",
                              "hold_op_id": ev.get("hold_op_id"),
                              "amount": amount}
            replayed += 1
        elif kind in ("pending_sol", "settle_sol"):
            from daemon.launch_scanner import pending_sol
            reason = pending_sol.apply(led, ev, __import__(__name__, fromlist=["*"]))
            if reason:
                return led, "replay divergence: " + reason
            replayed += 1
        # The seq advances only for entries actually processed: on
        # a divergence return above it stays BEHIND the offending
        # entry, so the next load sees the divergence again and
        # keeps the account degraded instead of healing silently.
        led["seq"] = ev_seq
    if replayed:
        logger.warning("paper_account: reconciled %d journaled "
                       "mutation(s) the ledger had not reached",
                       replayed)
    return led, None


def _derive_buckets_locked(led: Dict[str, Any],
                           entries: List[Dict[str, Any]]
                           ) -> Tuple[Dict[str, Any], Optional[str]]:
    """Derive the spendable/estimated cash split for a ledger that
    predates it, by replaying the journal from genesis (design v4
    Stage A). EXACT OR FROZEN: a seq-less mutation, a genesis debit
    the verified bucket could not have funded, an unreconstructable
    position, or any mismatch against the stored state leaves the
    account DEGRADED (entries refused, credits journaled-only)
    rather than clamping the inconsistency into an apparently
    usable balance. Credits journaled without a price_kind route to
    the ESTIMATED bucket (conservative)."""
    unseq = [ev for ev in entries
             if ev.get("event") in _MUTATION_EVENTS
             and not isinstance(ev.get("seq"), int)]
    if unseq:
        return led, ("cash-split migration: %d journaled mutations "
                     "predate the seq scheme; manual reconciliation "
                     "required" % len(unseq))
    gen = _new_ledger()
    gen["start_balance"] = float(led.get("start_balance")
                                 or gen["start_balance"])
    gen["cash"] = gen["cash_spendable"] = gen["start_balance"]
    gen["cash_estimated"] = 0.0
    seqd = sorted((ev for ev in entries
                   if ev.get("event") in _MUTATION_EVENTS),
                  key=lambda e: e["seq"])
    for ev in seqd:
        lane, mint = ev.get("lane"), ev.get("mint")
        if ev["event"] == "debit":
            stake = float(ev.get("stake") or 0)
            if (stake <= 0
                    or float(gen["cash_spendable"]) < stake - _EPS):
                return led, ("cash-split migration: genesis debit "
                             "%s exceeds verified cash (history "
                             "financed by estimated proceeds); "
                             "manual reconciliation required"
                             % ev.get("op_id"))
            _apply_debit(gen, lane, mint, stake, ev.get("qty"),
                         ev.get("pos_id")
                         or _pos_id(lane, mint, int(ev["seq"])),
                         ev.get("ts"), instance=ev.get("instance") is True)
        elif ev["event"] == "write_down":
            ok, why = _apply_write_down(
                gen, lane, mint, ev.get("pos_id"),
                ev.get("reason"), ev.get("ts"), qty=ev.get("qty"))
            if not ok:
                return led, ("cash-split migration: write_down "
                             "%s: %s; manual reconciliation "
                             "required" % (ev.get("op_id"), why))
        elif ev["event"] == "debit_refused":
            continue  # exact attempt refusal, no economics
        elif ev["event"] == "hold":
            amount = float(ev.get("amount") or 0)
            if amount <= 0:
                return led, ("cash-split migration: bad hold %s; "
                             "manual reconciliation required"
                             % ev.get("op_id"))
            gen.setdefault("holds", {})[ev.get("op_id")] = {
                "lane": lane, "amount": amount, "ts": ev.get("ts")}
            gen["cash_spendable"] = round(
                float(gen["cash_spendable"]) - amount, 8)
        elif ev["event"] == "hold_released":
            amount = float(ev.get("amount") or 0)
            held = gen.setdefault("holds", {}).pop(ev.get("hold_op_id"), None)
            if held is None:
                return led, ("cash-split migration: hold release for "
                             "unknown hold %s; manual reconciliation "
                             "required" % ev.get("hold_op_id"))
            if abs(float(held["amount"]) - amount) > 1e-9:
                return led, ("cash-split migration: hold release "
                             "mismatch %s; manual reconciliation "
                             "required" % ev.get("op_id"))
            gen["cash_spendable"] = round(
                float(gen["cash_spendable"]) + amount, 8)
        elif ev["event"] == "fee":
            amount = float(ev.get("amount") or 0)
            if amount <= 0:
                return led, ("cash-split migration: bad fee %s; "
                             "manual reconciliation required"
                             % ev.get("op_id"))
            gen["cash"] = round(float(gen["cash"]) - amount, 8)
            gen["cash_spendable"] = round(
                float(gen["cash_spendable"]) - amount, 8)
        elif ev["event"] in ("pending_sol", "settle_sol"):
            from daemon.launch_scanner import pending_sol
            reason = pending_sol.apply(gen, ev, __import__(__name__, fromlist=["*"]))
            if reason:
                return led, "cash-split migration: " + reason
        else:  # credit
            gst = _lane(gen, lane)
            pos = gst["open"].get(_position_key(gst, mint, ev.get("pos_id")))
            if pos is None:
                return led, ("cash-split migration: orphan credit "
                             "%s (position not open in genesis "
                             "replay); manual reconciliation "
                             "required" % ev.get("op_id"))
            realized, why = _apply_credit(
                gen, lane, mint, ev.get("qty"),
                float(ev.get("net") if ev.get("net") is not None
                      else (float(ev.get("proceeds") or 0)
                            - float(ev.get("fee") or 0))),
                float(ev.get("fee") or 0),
                bool(ev.get("final", True)),
                price_kind=ev.get("price_kind"),
                op_id=ev.get("op_id"), ts=ev.get("ts"),
                pos_id=ev.get("pos_id"))
            if realized is None:
                return led, ("cash-split migration: credit %s: %s"
                             % (ev.get("op_id"), why))
    if abs(float(gen["cash"]) - float(led["cash"])) > 1e-6:
        return led, ("cash-split migration: replayed cash $%.6f "
                     "!= stored $%.6f; manual reconciliation "
                     "required" % (float(gen["cash"]),
                                   float(led["cash"])))
    for lane in set(list((gen.get("lanes") or {}).keys())
                    + list((led.get("lanes") or {}).keys())):
        g = set(((gen["lanes"].get(lane) or {}).get("open")
                 or {}).keys())
        s = set(((led["lanes"].get(lane) or {}).get("open")
                 or {}).keys())
        if g != s:
            return led, ("cash-split migration: open positions "
                         "diverge in lane %s; manual "
                         "reconciliation required" % lane)
    led["cash_spendable"] = gen["cash_spendable"]
    led["cash_estimated"] = gen["cash_estimated"]
    # The unit-state register is derived by the same replay.
    led["assumed_sold"] = gen.get("assumed_sold") or {}
    logger.warning("paper_account: cash split derived from journal "
                   "replay: spendable $%.6f, estimated $%.6f",
                   gen["cash_spendable"], gen["cash_estimated"])
    return led, None


def verify_replay() -> Tuple[Optional[bool], str]:
    """READ-ONLY check on an EXISTING account (no financial writes: no torn-tail truncation, no ledger creation, no .bak restore, no reconcile, and no directory or lock-file creation: a virgin path returns (None, account_dir_or_lock_file_missing_not_created); it holds the exclusive account flock during the read phase; _derive_buckets_locked logs a WARNING). Replays the WAL from genesis
    on a deepcopy of the stored ledger file and compares: cash total, cash_spendable, cash_estimated, assumed_sold keys and the per-lane open
    position key sets (the first two are checked inside _derive_buckets_locked, the rest here). NOT checked, because the replay keeps them local:
    holds, pending_sol, per-position qty/basis. Returns (True, why) only on a full match of those; (False, reason) on a divergence; (None, reason)
    when it cannot be evaluated (unreadable/torn/holed journal, missing ledger file). It is the account against its OWN journal, not independent
    conservation."""
    import copy
    try:
        # never CREATE anything: _lock() would makedirs the account dir and O_CREAT the lock file on a virgin path
        if not (os.path.isdir(ACCOUNT_DIR) and os.path.isfile(LOCK_FILE)):
            return None, "account_dir_or_lock_file_missing_not_created"
        with _lock():
            entries, jstatus, _off = _journal_read()
            if jstatus is not None:
                return None, "journal_status:%s" % jstatus
            if _seq_violation(entries) is not None:
                return None, "journal_seq_violation"
            led = _read_json(LEDGER_PATH)
            if not isinstance(led, dict):
                return None, "ledger_file_unreadable_or_missing"
            led2 = copy.deepcopy(led)
        _res, reason = _derive_buckets_locked(led2, entries)
        if reason:
            return False, str(reason)[:300]
        for k in ("cash_spendable", "cash_estimated"):
            if abs(float(led.get(k) or 0.0) - float(led2.get(k) or 0.0)) > 1e-6:
                return False, "%s differs from the replay" % k
        if set((led.get("assumed_sold") or {}).keys()) != set((led2.get("assumed_sold") or {}).keys()):
            return False, "assumed_sold keys differ from the replay"
        return True, "cash_buckets_assumed_sold_keys_and_open_position_sets_match_the_journal"
    except Exception as e:
        return None, "verify_replay_error:%r" % (e,)


def _load_locked() -> Tuple[Optional[Dict[str, Any]], bool,
                            Optional[str]]:
    """Read the ledger under the lock. Returns (ledger, healthy,
    degraded). First run creates it; a corrupt live file falls
    back to the .bak shadow; both unreadable -> (None, False, ...)
    so callers fail closed. A loaded ledger is reconciled against
    the journal (write-ahead log) so a crash or stale shadow can
    never resurrect spendable cash or a settled position. Journal
    damage beyond one torn trailing record, a seq hole, or a
    replay divergence leaves the account DEGRADED: debits are
    refused and credits are journaled-only until a human
    reconciles the WAL. Caller must hold _lock()."""
    degraded: Optional[str] = None
    entries, jstatus, tail_off = _journal_read()
    if jstatus == "torn_tail":
        logger.warning("paper_account: truncating torn journal "
                       "tail at byte %d", tail_off)
        try:
            _journal_truncate(tail_off)  # type: ignore[arg-type]
        except Exception:
            logger.exception("paper_account: torn-tail truncate "
                             "failed")
            degraded = "journal tail torn and truncation failed"
    elif jstatus == "torn":
        degraded = ("journal torn (unparseable record mid-file); "
                    "manual WAL reconciliation required")
    if degraded is None:
        hole = _seq_violation(entries)
        if hole is not None:
            degraded = ("journal seq violation %s -> %s; manual "
                        "WAL reconciliation required" % hole)
    if not os.path.exists(LEDGER_PATH):
        led = _new_ledger()
        _journal({"event": "create", "start_balance":
                  led["start_balance"], "account_id": led["account_id"]})
        _save_locked(led)
        return led, True, degraded
    led = _read_json(LEDGER_PATH)
    if led is None:
        logger.error("paper_account: %s unreadable, trying shadow",
                     LEDGER_PATH)
        led = _read_json(LEDGER_PATH + ".bak")
        if led is None:
            logger.error("paper_account: shadow also unreadable; "
                         "failing closed (debits refused)")
            return None, False, degraded or "ledger unreadable"
        logger.warning("paper_account: restored from shadow copy")
    led.setdefault("ops", {})
    if degraded is None:
        # Only a clean WAL may drive replay: with a torn journal or
        # a seq hole the entries cannot be trusted to be complete,
        # so the state is left exactly as read (debits are refused
        # anyway; see the degraded paths in try_debit/credit).
        seq_before = led.get("seq")
        led, divergence = _reconcile(led, entries)
        if divergence is not None:
            degraded = divergence
            logger.error("paper_account: %s", divergence)
        if led.get("seq") != seq_before:
            # Persist the reconciled state: otherwise the on-disk
            # file stays behind the journal and the NEXT load
            # replays the same mutations over the same stale state
            # (a credit would count twice). On divergence the
            # partial replay up to the offending entry is valid
            # and is kept.
            _save_locked(led)
    if degraded is None and "cash_spendable" not in led:
        # Bucket migration (design v4 Stage A): derive the split
        # from a genesis journal replay; exact or frozen.
        led, degraded = _derive_buckets_locked(led, entries)
        if degraded is None:
            _save_locked(led)
        else:
            logger.error("paper_account: %s", degraded)
    # Day rollover: reset the daily counter, keep the cumulative.
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if led.get("day") != today:
        led["day"] = today
        led["day_realized_pnl"] = 0.0
    return led, True, degraded


def _save_locked(led: Dict[str, Any]) -> None:
    """Caller must hold _lock(). Live file, then shadow (best
    effort), then sweep stale tmp files."""
    _atomic_write(LEDGER_PATH, led)
    try:
        _atomic_write(LEDGER_PATH + ".bak", led)
    except Exception as e:
        logger.warning("paper_account: shadow write failed: %s", e)
    try:
        cutoff = time.time() - 3600
        for pattern in (LEDGER_PATH + ".tmp.*",
                        LEDGER_PATH + ".bak.tmp.*"):
            for stale in glob.glob(pattern):
                try:
                    if os.path.getmtime(stale) < cutoff:
                        os.unlink(stale)
                except OSError:
                    pass
    except Exception:
        pass


def _op_lookup(led: Dict[str, Any], op_id: str, kind: str,
               lane: str, mint: str, params: Dict[str, Any]
               ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Caller-retry dedupe. Returns (record, None) when op_id was
    already applied with the SAME shape (caller should get the
    original outcome, never a re-apply), or (None, reason) when
    the op_id was used for something else (caller bug), or
    (None, None) when the op_id is new."""
    rec = led.get("ops", {}).get(op_id)
    if rec is None:
        return None, None
    if (rec.get("event") != kind or rec.get("lane") != lane
            or rec.get("mint") != mint):
        return None, ("op_id %r already used for %s %s/%s"
                      % (op_id, rec.get("event"), rec.get("lane"),
                         rec.get("mint")))
    for k, v in params.items():
        if rec.get(k) is not None and rec.get(k) != v:
            return None, ("op_id %r parameter conflict on %s"
                          % (op_id, k))
    return rec, None


def try_debit(lane: str, mint: str, stake: float, qty: float,
              op_id: str, ts: Optional[float] = None,
              marks: Optional[Dict[str, Dict[str, Any]]] = None,
              durable_refusal: bool = False):
    """Legacy one-open-mint reservation; behavior and WAL shape unchanged by default.

    durable_refusal=True (DEMO entry intents only: their op ids are unique per attempt) journals a
    refusal as a replayable debit_refused op, so a lost intent release is classified STALE from the
    account and the retry re-quotes. Left False, a refusal is transient and writes nothing (stable
    op ids of other lanes must stay retryable)."""
    return _try_debit(lane, mint, stake, qty, op_id, ts, marks, instance=False,
                      durable_refusal=bool(durable_refusal))


def try_debit_instance(lane: str, mint: str, stake: float, qty: float,
                       op_id: str, ts: Optional[float] = None,
                       marks: Optional[Dict[str, Dict[str, Any]]] = None):
    """Reserve an independent exact lot UNDER the existing lane and caps.

    Only this API admits multiple same-mint lots. Credit and marks must name
    the returned pos_id. Legacy calls cannot overwrite or absorb these lots.
    """
    return _try_debit(lane, mint, stake, qty, op_id, ts, marks, instance=True)


def _debit_refused(led, instance, lane, mint, stake, qty, op_id, why, durable=False):
    if instance or durable:
        seq = int(led.get("seq") or 0) + 1
        event = {"event": "debit_refused",
                 ("instance" if instance else "durable_refusal"): True,
                 "lane": lane, "mint": mint, "stake": stake, "qty": qty,
                 "op_id": op_id, "reason": why, "seq": seq,
                 "event_id": "ev#%d" % seq}
        _journal(event)
        led["ops"][op_id] = {k:v for k,v in event.items()
                              if k not in ("seq", "event_id")}
        led["seq"] = seq
        _save_locked(led)
    return False, why, None


def _try_debit(lane: str, mint: str, stake: float, qty: float,
              op_id: str, ts: Optional[float] = None,
              marks: Optional[Dict[str, Dict[str, Any]]] = None,
              instance: bool = False, durable_refusal: bool = False
              ) -> Tuple[bool, str, Optional[str]]:
    """Reserve `stake` paper dollars for `lane` buying `qty` units
    of `mint`. op_id is the caller's stable operation ID: an
    uncertain response may be retried with the SAME op_id and gets
    the original outcome back without re-applying — even if the
    position was closed and reopened meanwhile.

    Returns (True, "ok", pos_id) or (False, reason, None). The
    pos_id immutably names this open instance; pass it back to
    credit() so a close can never settle a later reopen of the
    same mint. Fail-closed: any corruption, degradation or
    inconsistency refuses the debit — a refused paper entry costs
    nothing, a doubled one quietly invents money.

    When PAPER_ACCOUNT_GATES_ENABLED=1 (default OFF),
    `marks` must carry the caller's current liquidation marks and
    the account-level risk gates run INSIDE the same lock as the
    reservation (see _gate_check_locked): evaluation and debit are
    one atomic section, so two lanes cannot each pass on the same
    available capacity. When `marks` is None and gates are enabled,
    the account collects them centrally (account_marks) BEFORE
    taking the lock; a position it cannot price blocks further
    entries fail-closed.
    """
    if not _valid_amount(stake) or stake <= 0:
        return False, "stake must be a positive finite number", None
    if not _valid_amount(qty) or qty <= 0:
        return False, "qty must be a positive finite number", None
    stake = float(stake)
    qty = float(qty)
    if not lane or not mint:
        return False, "lane and mint are required", None
    if not op_id or not isinstance(op_id, str):
        return False, "caller op_id is required", None
    if gates_enabled() and marks is None:
        marks = _central_marks()
    elif marks is not None:
        marks = _boundary_marks(marks)
    def _refuse(*a):
        return _debit_refused(*a, durable=durable_refusal)
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return (False, "account ledger unreadable "
                    "(fail-closed)", None)
        if degraded:
            return False, "account degraded: %s" % degraded, None
        prior = led.get("ops", {}).get(op_id)
        if prior is not None and prior.get("event") == "debit_refused":
            _exp = {"lane": lane, "mint": mint, "stake": stake, "qty": qty}
            if (any(prior.get(k) != v for k, v in _exp.items())
                    or bool(prior.get("instance")) != bool(instance)):
                return False, "refused entry attempt parameter conflict", None
            return False, "original refusal: " + prior["reason"], None
        rec, conflict = _op_lookup(led, op_id, "debit", lane, mint,
                                   {"stake": stake, "qty": qty,
                                    "instance": instance})
        if conflict:
            return False, conflict, None
        if rec is not None:
            if bool(rec.get("instance")) != instance:
                return False, "op_id instance ownership conflict", None
            return True, "ok (retry of applied op)", rec["pos_id"]
        cash = float(led["cash"])
        spendable = float(led.get("cash_spendable", 0.0))
        if spendable < stake:
            return _refuse(led, instance, lane, mint, stake, qty, op_id,
                                ("account spendable cash $%.2f < stake "
                            "$%.2f (total $%.2f incl. quarantined "
                            "estimated proceeds)"
                            % (spendable, stake, cash)))
        st = _lane(led, lane)
        if (mint in st["open"] or (not instance and any(
                p.get("mint") == mint for p in st["open"].values()))):
            return _refuse(led, instance, lane, mint, stake, qty, op_id,
                                  "%s already has an open %s position" % (lane, mint))
        cap = lane_cap()
        if cap is not None:
            open_basis = sum(_pos_basis(p) for p in st["open"].values())
            open_basis += sum(float(p["basis"]) for p in
                              led.get("pending_sol", {}).values() if p["lane"] == lane)
            if open_basis + stake > cap:
                return _refuse(led, instance, lane, mint, stake, qty, op_id,
                                    ("lane cap $%.2f would be exceeded "
                                "($%.2f open + $%.2f new)"
                                % (cap, open_basis, stake)))
        if not gates_enabled():
            # MANDATORY (owner spec: no bypass of risk checks): an entry is
            # never admitted with the account gates off. Evaluated inside
            # the lock so direct callers cannot skip a lane-side precheck.
            return _refuse(led, instance, lane, mint, stake, qty, op_id,
                                  "mandatory gates off; entry blocked fail-closed")
        if True:
            view = _view_locked(led, marks, degraded)
            _update_baselines_locked(led, view)
            view.update(_gate_fields(led, view))
            refusal = _gate_check_locked(led, view, stake, mint=mint)
            if refusal is not None:
                return _refuse(led, instance, lane, mint, stake, qty, op_id, refusal)
        if provider_health_gate_enabled():
            refusal = _provider_health_refusal()
            if refusal is not None:
                return _refuse(led, instance, lane, mint, stake, qty, op_id, refusal)
        led["seq"] = int(led.get("seq") or 0) + 1
        pid = _pos_id(lane, mint, led["seq"])
        _apply_debit(led, lane, mint, stake, qty, pid, ts, instance=instance)
        led["ops"][op_id] = {"event": "debit", "lane": lane,
                             "mint": mint, "pos_id": pid,
                             "stake": stake, "qty": qty,
                             **({"instance": True} if instance else {})}
        _journal({"event": "debit", "event_id": "ev#%d" % led["seq"],
                  "op_id": op_id, "lane": lane, "mint": mint,
                  "stake": stake, "qty": qty,
                  **({"instance": True} if instance else {}),
                  "cash_after": led["cash"],
                  "seq": led["seq"], "pos_id": pid, "ts": ts})
        _save_locked(led)
        return True, "ok", pid


def hold_stake(lane: str, amount: float, op_id: str,
               ts: Optional[float] = None,
               marks: Optional[Dict[str, Dict[str, Any]]] = None,
               stake: Optional[float] = None,
               mint: Optional[str] = None
               ) -> Tuple[bool, str]:
    """Reserve `amount` of spendable cash at SEAL time for an
    execution whose outcome is not yet known (design: account-wide
    hold so a fill that settles before this process learns of it is
    always adoptable). ADMISSION HAPPENS HERE: the lane cap, gates
    and provider-health checks run against the hold, so a later
    consume is pure recovery of recorded truth, never a fresh
    admission. Refusals are NOT journaled (a hold is not an entry
    attempt; cash freeing up later may let a retry succeed).
    Idempotent by op_id: a retry of an applied hold returns its
    outcome; the same op_id with different parameters is a
    conflict. Held cash stays part of `cash` (it is still ours)
    but leaves `cash_spendable` until consumed or released. Holds
    NEVER expire silently."""
    if not _valid_amount(amount) or amount <= 0:
        return False, "hold amount must be a positive finite number"
    amount = float(amount)
    if stake is not None:
        # `amount` is the FULL reserve (stake + max fee); `stake` is the
        # pure stake the configured trade size applies to.
        if not _valid_amount(stake) or stake <= 0 or float(stake) > amount + 1e-9:
            return False, "hold stake must be a positive finite number <= the reserve"
        stake = float(stake)
    if not lane:
        return False, "lane is required"
    if not op_id or not isinstance(op_id, str):
        return False, "caller op_id is required"
    if gates_enabled() and marks is None:
        marks = _central_marks()
    elif marks is not None:
        marks = _boundary_marks(marks)
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return False, "account ledger unreadable (fail-closed)"
        if degraded:
            return False, "account degraded: %s" % degraded
        prior = led.get("ops", {}).get(op_id)
        if prior is not None:
            _ps = prior.get("stake")
            if (prior.get("event") == "hold"
                    and prior.get("lane") == lane
                    and ((_ps is None and stake is None)
                         or (_ps is not None and stake is not None
                             and abs(float(_ps) - stake) < 1e-9))
                    and abs(float(prior.get("amount", -1)) - amount) < 1e-9):
                return True, "ok (retry of applied op)"
            return False, "hold op_id parameter conflict"
        holds = led.setdefault("holds", {})
        spendable = float(led.get("cash_spendable", 0.0))
        if spendable < amount:
            return (False, "account spendable cash $%.2f < hold "
                    "$%.2f (total $%.2f incl. quarantined estimated "
                    "proceeds)" % (spendable, amount,
                                   float(led.get("cash", 0.0))))
        st = _lane(led, lane)
        cap = lane_cap()
        if cap is not None:
            open_basis = sum(_pos_basis(p) for p in st["open"].values())
            open_basis += sum(float(p["basis"]) for p in
                              led.get("pending_sol", {}).values()
                              if p["lane"] == lane)
            open_basis += sum(float(h["amount"]) for h in holds.values()
                              if h["lane"] == lane)
            if open_basis + amount > cap:
                return (False, "lane cap $%.2f would be exceeded "
                        "($%.2f open + $%.2f new incl. hold)"
                        % (cap, open_basis, amount))
        if gates_enabled():
            view = _view_locked(led, marks, degraded)
            _update_baselines_locked(led, view)
            view.update(_gate_fields(led, view))
            refusal = _gate_check_locked(led, view, amount, stake, mint=mint)
            if refusal is not None:
                return False, refusal
        if provider_health_gate_enabled():
            refusal = _provider_health_refusal()
            if refusal is not None:
                return False, refusal
        led["seq"] = int(led.get("seq") or 0) + 1
        holds[op_id] = {"lane": lane, "amount": amount, "ts": ts,
                        **({"stake": stake} if stake is not None else {}),
                        **({"mint": mint} if mint else {})}
        led["cash_spendable"] = round(spendable - amount, 8)
        led["ops"][op_id] = {"event": "hold", "lane": lane,
                             "amount": amount,
                             **({"stake": stake} if stake is not None else {}),
                             **({"mint": mint} if mint else {})}
        _journal({"event": "hold", "event_id": "ev#%d" % led["seq"],
                  "op_id": op_id, "lane": lane, "amount": amount,
                  **({"stake": stake} if stake is not None else {}),
                  **({"mint": mint} if mint else {}),
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def release_hold(hold_op_id: str, reason: str,
                 ts: Optional[float] = None) -> Tuple[bool, str]:
    """Release a hold whose execution UNAMBIGUOUSLY did not happen
    (provider reports the tx never landed / never existed). Never
    call while the outcome is unknown: held exposure is freed only
    by a recorded resolution. Idempotent."""
    if not hold_op_id or not isinstance(hold_op_id, str):
        return False, "hold_op_id is required"
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return False, "account ledger unreadable (fail-closed)"
        if degraded:
            return False, "account degraded: %s" % degraded
        rel_op = hold_op_id + ":release"
        if led.get("ops", {}).get(rel_op) is not None:
            return True, "ok (retry of applied op)"
        held = led.setdefault("holds", {}).get(hold_op_id)
        if held is None:
            if led.get("ops", {}).get(hold_op_id) is None:
                return False, "no such hold"
            return False, ("hold %s already consumed; refusing to "
                           "release" % hold_op_id)
        led["seq"] = int(led.get("seq") or 0) + 1
        del led["holds"][hold_op_id]
        led["cash_spendable"] = round(
            float(led.get("cash_spendable", 0.0))
            + float(held["amount"]), 8)
        led["ops"][rel_op] = {"event": "hold_released",
                              "hold_op_id": hold_op_id,
                              "amount": float(held["amount"])}
        _journal({"event": "hold_released",
                  "event_id": "ev#%d" % led["seq"],
                  "op_id": rel_op, "hold_op_id": hold_op_id,
                  "amount": float(held["amount"]), "reason": reason,
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def consume_hold_for_entry(hold_op_id: str, lane: str, mint: str,
                           stake: float, qty: float, entry_op_id: str,
                           ts: Optional[float] = None,
                           instance: bool = True
                           ) -> Tuple[bool, str, Optional[str]]:
    """Adopt a SETTLED fill funded by a prior hold: apply the entry
    debit and release the hold's remainder in one locked mutation.
    NO admission gates run here: admission happened at hold time
    (seal), and the fill is recorded economic truth — refusing it
    would strand real exposure invisible to the account. The hold
    must exist, cover the stake, and belong to the same lane. A
    crash between the two journaled events is repaired by retry:
    an applied entry with the hold still present finishes the
    release."""
    if not _valid_amount(stake) or stake <= 0:
        return False, "stake must be a positive finite number", None
    if not _valid_amount(qty) or qty <= 0:
        return False, "qty must be a positive finite number", None
    stake = float(stake)
    qty = float(qty)
    if not lane or not mint:
        return False, "lane and mint are required", None
    if not hold_op_id or not isinstance(hold_op_id, str):
        return False, "hold_op_id is required", None
    if not entry_op_id or not isinstance(entry_op_id, str):
        return False, "caller entry_op_id is required", None
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return (False, "account ledger unreadable "
                    "(fail-closed)", None)
        if degraded:
            return False, "account degraded: %s" % degraded, None
        rec, conflict = _op_lookup(led, entry_op_id, "debit", lane, mint,
                                   {"stake": stake, "qty": qty,
                                    "instance": instance})
        if conflict:
            return False, conflict, None
        held = led.setdefault("holds", {}).get(hold_op_id)
        if rec is not None:
            # Entry already applied; finish the release if a crash
            # split the pair.
            if held is not None:
                led["seq"] = int(led.get("seq") or 0) + 1
                del led["holds"][hold_op_id]
                led["cash_spendable"] = round(
                    float(led.get("cash_spendable", 0.0))
                    + float(held["amount"]), 8)
                rel_op = hold_op_id + ":release"
                led["ops"][rel_op] = {"event": "hold_released",
                                      "hold_op_id": hold_op_id,
                                      "amount": float(held["amount"])}
                _journal({"event": "hold_released",
                          "event_id": "ev#%d" % led["seq"],
                          "op_id": rel_op, "hold_op_id": hold_op_id,
                          "amount": float(held["amount"]),
                          "reason": "consumed (crash repair)",
                          "seq": led["seq"], "ts": ts})
                _save_locked(led)
            return True, "ok (retry of applied op)", rec["pos_id"]
        if held is None:
            return False, "no such hold to consume", None
        if held["lane"] != lane:
            return False, ("hold lane %s != entry lane %s"
                           % (held["lane"], lane)), None
        if float(held["amount"]) + 1e-9 < stake:
            return (False, "hold $%.8f does not cover stake $%.8f"
                    % (float(held["amount"]), stake), None)
        if float(led["cash"]) < stake:
            return (False, "account cash $%.2f < stake $%.2f"
                    % (float(led["cash"]), stake), None)
        st = _lane(led, lane)
        if mint in st["open"] or (not instance and any(
                p.get("mint") == mint for p in st["open"].values())):
            return False, "%s already has an open %s position" % (lane, mint), None
        led["seq"] = int(led.get("seq") or 0) + 1
        pid = _pos_id(lane, mint, led["seq"])
        _apply_debit(led, lane, mint, stake, qty, pid, ts, instance=instance)
        led["ops"][entry_op_id] = {"event": "debit", "lane": lane,
                                   "mint": mint, "pos_id": pid,
                                   "stake": stake, "qty": qty,
                                   **({"instance": True} if instance else {})}
        _journal({"event": "debit", "event_id": "ev#%d" % led["seq"],
                  "op_id": entry_op_id, "lane": lane, "mint": mint,
                  "stake": stake, "qty": qty,
                  **({"instance": True} if instance else {}),
                  "cash_after": led["cash"],
                  "seq": led["seq"], "pos_id": pid, "ts": ts,
                  "hold_op_id": hold_op_id})
        led["seq"] = int(led.get("seq") or 0) + 1
        del led["holds"][hold_op_id]
        led["cash_spendable"] = round(
            float(led.get("cash_spendable", 0.0))
            + float(held["amount"]), 8)
        rel_op = hold_op_id + ":release"
        led["ops"][rel_op] = {"event": "hold_released",
                              "hold_op_id": hold_op_id,
                              "amount": float(held["amount"])}
        _journal({"event": "hold_released",
                  "event_id": "ev#%d" % led["seq"],
                  "op_id": rel_op, "hold_op_id": hold_op_id,
                  "amount": float(held["amount"]), "reason": "consumed",
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok", pid


def settle_hold_fee(hold_op_id: str, fee_amount: float, fee_op_id: str,
                    ts: Optional[float] = None) -> Tuple[bool, str]:
    """Book a REAL fee loss (settled failed tx) against a hold and
    release the remainder: cash AND spendable fall by the fee, the
    rest of the reservation returns. The shared ledger's fee
    accounting is authoritative and crash-safe: the fee event and
    the release are journaled before the state save, replayed
    idempotently after a crash. Idempotent by fee_op_id."""
    if not _valid_amount(fee_amount) or fee_amount <= 0:
        return False, "fee amount must be a positive finite number"
    fee_amount = float(fee_amount)
    if not hold_op_id or not isinstance(hold_op_id, str):
        return False, "hold_op_id is required"
    if not fee_op_id or not isinstance(fee_op_id, str):
        return False, "caller fee_op_id is required"
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return False, "account ledger unreadable (fail-closed)"
        if degraded:
            return False, "account degraded: %s" % degraded
        if led.get("ops", {}).get(fee_op_id) is not None:
            return True, "ok (retry of applied op)"
        held = led.setdefault("holds", {}).get(hold_op_id)
        if held is None:
            return False, ("no such hold %s for fee settlement"
                           % hold_op_id)
        if fee_amount > float(held["amount"]) + 1e-9:
            return False, ("fee $%.8f exceeds hold $%.8f"
                           % (fee_amount, float(held["amount"])))
        led["seq"] = int(led.get("seq") or 0) + 1
        led["cash"] = round(float(led["cash"]) - fee_amount, 8)
        led["cash_spendable"] = round(
            float(led.get("cash_spendable", 0.0)) - fee_amount, 8)
        led["ops"][fee_op_id] = {"event": "fee",
                                 "hold_op_id": hold_op_id,
                                 "amount": fee_amount}
        _journal({"event": "fee", "event_id": "ev#%d" % led["seq"],
                  "op_id": fee_op_id, "hold_op_id": hold_op_id,
                  "amount": fee_amount, "seq": led["seq"], "ts": ts})
        led["seq"] = int(led.get("seq") or 0) + 1
        del led["holds"][hold_op_id]
        led["cash_spendable"] = round(
            float(led.get("cash_spendable", 0.0))
            + float(held["amount"]), 8)
        rel_op = hold_op_id + ":release"
        led["ops"][rel_op] = {"event": "hold_released",
                              "hold_op_id": hold_op_id,
                              "amount": float(held["amount"])}
        _journal({"event": "hold_released",
                  "event_id": "ev#%d" % led["seq"],
                  "op_id": rel_op, "hold_op_id": hold_op_id,
                  "amount": float(held["amount"]), "reason": "fee settled",
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def book_fee(fee_amount: float, fee_op_id: str,
             ts: Optional[float] = None) -> Tuple[bool, str]:
    """Book a REAL, already-incurred network fee (a landed-failed swap) that is not tied to a
    hold: cash AND spendable fall by the fee. Idempotent by fee_op_id; journaled before the
    state save and replayed after a crash (same "fee" event as settle_hold_fee, no hold)."""
    if not _valid_amount(fee_amount) or fee_amount <= 0:
        return False, "fee amount must be a positive finite number"
    if not fee_op_id or not isinstance(fee_op_id, str):
        return False, "caller fee_op_id is required"
    fee_amount = float(fee_amount)
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return False, "account ledger unreadable (fail-closed)"
        if degraded:
            return False, "account degraded: %s" % degraded
        if led.get("ops", {}).get(fee_op_id) is not None:
            return True, "ok (retry of applied op)"
        led["seq"] = int(led.get("seq") or 0) + 1
        led["cash"] = round(float(led["cash"]) - fee_amount, 8)
        led["cash_spendable"] = round(
            float(led.get("cash_spendable", 0.0)) - fee_amount, 8)
        led["ops"][fee_op_id] = {"event": "fee", "hold_op_id": None,
                                 "amount": fee_amount}
        _journal({"event": "fee", "event_id": "ev#%d" % led["seq"],
                  "op_id": fee_op_id, "hold_op_id": None,
                  "amount": fee_amount, "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def list_holds(prefix: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Live holds, optionally filtered by op_id prefix. Lets a
    book sweep reservations it sealed but never recorded (crash
    between hold and seal save) so orphan holds are released by
    their owner, never silently expired."""
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            return {}
        holds = dict(led.get("holds") or {})
    if prefix is None:
        return holds
    return {k: v for k, v in holds.items() if k.startswith(prefix)}


def credit(lane: str, mint: str, proceeds: float, op_id: str,
           ts: Optional[float] = None, pos_id: Optional[str] = None,
           qty: Optional[float] = None, fee: float = 0.0,
           price_kind: Optional[str] = None
           ) -> Tuple[bool, str]:
    """Settle a sale of `qty` units of `mint` (None = ALL remaining
    units, i.e. a full close) for `proceeds` gross paper dollars
    with `fee` in fees. Net proceeds (gross - fee) credit the
    shared cash immediately — tranche receipts are never held
    outside the account. Average-cost allocation: the sold units
    carry basis proportionally, and realized P/L = net - allocated
    basis is booked per sale (a profitable tranche that recovers
    more than the original stake realizes the gain while units
    remain; remaining basis is per-unit allocated and never goes
    negative).

    op_id is the caller's stable operation ID for THIS settlement
    (each tranche gets its own): retries return the original
    outcome without re-applying. When pos_id is given it must
    match the open instance; a mismatch means the caller holds a
    stale close (the mint was reopened since) and the credit is
    refused + journaled rather than settling the wrong instance.
    Returns (True, "ok") or (False, reason); a False return means
    nothing was credited — the event is still journaled so the
    money movement is recoverable by hand. On a DEGRADED account
    credits are journaled-only (never applied): a suspect ledger
    must not silently gain money.
    """
    if not _valid_amount(proceeds) or proceeds < 0:
        return False, "proceeds must be a nonnegative finite number"
    if not _valid_amount(fee) or fee < 0:
        return False, "fee must be a nonnegative finite number"
    proceeds = float(proceeds)
    fee = float(fee)
    if fee > proceeds:
        return False, "fee exceeds gross proceeds"
    if qty is not None and (not _valid_amount(qty) or qty <= 0):
        return False, "qty must be a positive finite number"
    if not lane or not mint:
        return False, "lane and mint are required"
    if not op_id or not isinstance(op_id, str):
        return False, "caller op_id is required"
    net = round(proceeds - fee, 6)
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None:
            # Never drop realized money silently: journal it even
            # when the ledger cannot be updated.
            _journal({"event": "credit_dropped", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id,
                      "reason": "ledger unreadable"})
            return False, "account ledger unreadable (journaled)"
        if degraded:
            _journal({"event": "credit_degraded", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id,
                      "reason": degraded})
            return False, ("account degraded: %s (journaled for "
                           "manual reconciliation)" % degraded)
        rec, conflict = _op_lookup(led, op_id, "credit", lane, mint,
                                   {})
        if conflict:
            return False, conflict
        if rec is not None:
            if rec.get("instance") is True:
                params = {"proceeds": proceeds, "fee": fee, "qty": qty,
                          "price_kind": price_kind, "pos_id": pos_id}
                if any(rec.get(k) != v for k, v in params.items()):
                    return False, "op_id instance settlement parameter conflict"
            return True, "ok (retry of applied op)"
        st = _lane(led, lane)
        key = _position_key(st, mint, pos_id)
        pos = st["open"].get(key)
        if pos is None:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id})
            return False, ("no open %s position for %s (journaled)"
                           % (mint, lane))
        if pos_id and pos.get("pos_id") and pos["pos_id"] != pos_id:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pos_id,
                      "reason": "pos_id mismatch (open is %s)"
                                % pos["pos_id"]})
            return False, ("stale pos_id %s: %s is open as %s "
                           "(journaled, not applied)"
                           % (pos_id, mint, pos["pos_id"]))
        pid = pos.get("pos_id")
        realized, why = _apply_credit(led, lane, mint, qty, net,
                                      fee, qty is None,
                                      price_kind=price_kind,
                                      op_id=op_id, ts=ts, pos_id=pid)
        if realized is None:
            _journal({"event": "credit_orphan", "lane": lane,
                      "mint": mint, "proceeds": proceeds,
                      "fee": fee, "qty": qty, "ts": ts,
                      "op_id": op_id, "pos_id": pid,
                      "reason": why})
            return False, "%s (journaled, not applied)" % why
        led["seq"] = int(led.get("seq") or 0) + 1
        led["ops"][op_id] = {"event": "credit", "lane": lane,
                             "mint": mint, "pos_id": pid,
                             "realized": realized,
                             **({"instance": True, "proceeds": proceeds,
                                 "fee": fee, "qty": qty, "price_kind": price_kind}
                                if pos.get("instance") is True else {})}
        _journal({"event": "credit", "event_id": "ev#%d" % led["seq"],
                  "op_id": op_id, "lane": lane, "mint": mint,
                  "qty": qty, "proceeds": proceeds, "fee": fee,
                  **({"instance": True} if pos.get("instance") is True else {}),
                  "net": net, "realized": realized,
                  "final": key not in st["open"],
                  "cash_after": led["cash"],
                  "price_kind": price_kind,
                  "seq": led["seq"], "pos_id": pid, "ts": ts})
        _save_locked(led)
        return True, "ok"


def _apply_write_down(led: Dict[str, Any], lane: str, mint: str,
                      pid: str, reason: Optional[str],
                      ts: Optional[float],
                      qty: Optional[float] = None
                      ) -> Tuple[bool, str]:
    """Move open-position units to the unresolved register
    (write-down of unquotable units). No cash moves, no fee, no
    realized P/L is recognized: a write-down is not a sale. Caller
    holds the lock. A full write-down (qty=None) is idempotent per
    pos_id: a replay or retry of an applied write-down is a no-op.
    Partial write-downs (qty given) move that many units with
    average-cost basis; CONSERVATION: moved qty + basis accumulate
    in the register entry so partials plus the remaining position
    always recompose the original cost and units exactly."""
    reg = led.setdefault("unresolved", {})
    st = _lane(led, lane)
    key = _position_key(st, mint, pid)
    if (qty is None and pid in reg
            and st["open"].get(key, {}).get("pos_id") != pid):
        return True, "already unresolved"
    pos = st["open"].get(key)
    if pos is None:
        if pid in reg:
            return True, "already unresolved"
        return False, "no open position"
    if pos.get("pos_id") != pid:
        return False, ("pos_id mismatch (open is %s)"
                       % pos.get("pos_id"))
    have_qty = _pos_qty(pos)
    basis = _pos_basis(pos)
    if qty is None or have_qty is None \
            or qty >= have_qty - _EPS:
        move_qty, alloc, final = have_qty, basis, True
    else:
        if not _valid_amount(qty) or qty <= 0:
            return False, "bad qty"
        move_qty = qty
        alloc = round(basis * qty / have_qty, 6) \
            if have_qty > 0 else 0.0
        final = False
    entry = reg.setdefault(pid, {"lane": lane, "mint": mint,
                                 **({"instance": True} if pos.get("instance") is True else {}),
                                 "qty": 0.0, "basis": 0.0,
                                 "entry_qty": pos.get("entry_qty"),
                                 "entry_cost": pos.get("entry_cost"),
                                 "reason": reason,
                                 "ts": ts or time.time(),
                                 "status": "held_unresolved"})
    entry["qty"] = round(float(entry.get("qty") or 0.0)
                         + (move_qty or 0.0), 8)
    entry["basis"] = round(float(entry.get("basis") or 0.0)
                           + alloc, 6)
    entry["reason"] = reason
    entry["ts"] = ts or entry["ts"]
    if final:
        st["open"].pop(key)
    else:
        pos["qty"] = round(have_qty - move_qty, 8)
        pos["basis"] = round(basis - alloc, 6)
    return True, "ok"


def write_down(lane: str, mint: str, op_id: str,
               ts: Optional[float] = None,
               pos_id: Optional[str] = None,
               reason: Optional[str] = None,
               qty: Optional[float] = None
               ) -> Tuple[bool, str]:
    """Write a position down to zero carrying value: units and their
    original cost move to the unresolved register as
    held_unresolved (design v4 Stage B). NOT a sale: no fee, no
    proceeds, no realized loss recognized; the register keeps the
    original cost so a later evidence-led resolution can reconcile
    exactly. Same durability contract as credit(): journaled with
    seq before the state write, idempotent on op_id, pinned to the
    pos_id instance."""
    if not isinstance(lane, str) or not lane:
        return False, "bad lane"
    if not isinstance(mint, str) or not mint:
        return False, "bad mint"
    if not isinstance(op_id, str) or not op_id:
        return False, "bad op_id"
    ts = time.time() if ts is None else ts
    with _lock():
        led, healthy, degraded = _load_locked()
        if led is None:
            return False, "ledger unreadable"
        prior = led["ops"].get(op_id)
        if prior is not None:
            # Instance operations are exact. A different event or target can
            # never ACK an unapplied disposal. Legacy behavior stays isolated.
            st = _lane(led, lane)
            target = st["open"].get(_position_key(st, mint, pos_id))
            instance = (prior.get("instance") is True or
                        (isinstance(target, dict) and target.get("instance") is True))
            if instance:
                expected = {"event": "write_down", "lane": lane, "mint": mint,
                            "pos_id": pos_id, "qty": qty, "reason": reason}
                if any(prior.get(k) != v for k, v in expected.items()):
                    return False, "op_id instance write_down parameter conflict"
            return True, "already applied"
        if degraded is not None:
            _journal({"event": "write_down_intent", "lane": lane,
                      "mint": mint, "op_id": op_id, "pos_id": pos_id,
                      "reason": reason, "ts": ts,
                      "note": "account degraded: intent journaled, "
                              "not applied"})
            return False, ("account degraded (%s): write-down "
                           "journaled, not applied" % degraded)
        pid = pos_id
        st = _lane(led, lane)
        pos = st["open"].get(_position_key(st, mint, pid))
        if pid is None and pos is not None:
            pid = pos.get("pos_id")
        if pid is None:
            _journal({"event": "write_down_orphan", "lane": lane,
                      "mint": mint, "op_id": op_id,
                      "pos_id": pos_id, "reason": reason, "ts": ts})
            return False, "no open position (journaled)"
        unresolved = (led.get("unresolved") or {}).get(pid) or {}
        instance = (isinstance(pos, dict) and pos.get("instance") is True
                    or unresolved.get("instance") is True)
        if unresolved and (unresolved.get("lane") != lane or unresolved.get("mint") != mint):
            return False, "unresolved instance ownership conflict"
        ok, why = _apply_write_down(led, lane, mint, pid, reason,
                                    ts, qty=qty)
        if not ok:
            _journal({"event": "write_down_refused", "lane": lane,
                      "mint": mint, "op_id": op_id, "pos_id": pid,
                      "reason": why, "ts": ts})
            return False, "%s (journaled, not applied)" % why
        led["seq"] = int(led.get("seq") or 0) + 1
        led["ops"][op_id] = {"event": "write_down", "lane": lane,
                             "mint": mint, "pos_id": pid,
                             **({"instance": True, "qty": qty, "reason": reason}
                                if instance else {})}
        _journal({"event": "write_down",
                  "event_id": "ev#%d" % led["seq"],
                  "op_id": op_id, "lane": lane, "mint": mint,
                  "pos_id": pid, "reason": reason,
                  **({"instance": True} if instance else {}),
                  "qty": qty,
                  "register_qty": led["unresolved"][pid]["qty"],
                  "register_basis": led["unresolved"][pid]["basis"],
                  "seq": led["seq"], "ts": ts})
        _save_locked(led)
        return True, "ok"


def account_identity(expected=None):
    """Generation identity, no regeneration when a native book is already bound."""
    with _lock():
        if expected is not None and not os.path.exists(LEDGER_PATH):
            raise ValueError("bound shared account is missing")
        led, healthy, degraded = _load_locked()
        if not healthy or led is None or degraded:
            raise ValueError("shared account identity unavailable")
        identity = led.get("account_id")
        if not isinstance(identity,str) or not identity:
            raise ValueError("legacy account generation unbound")
        if expected is not None and identity != expected:
            raise ValueError("shared account generation mismatch")
        entries,status,_ = _journal_read()
        creates = [e for e in entries if e.get("event")=="create"]
        if status is not None or len(creates)!=1 or creates[0].get("account_id")!=identity:
            raise ValueError("shared account generation WAL mismatch")
        return identity


def operation_receipt(op_id: str) -> Tuple[Optional[dict], Optional[str]]:
    """Read exact durable applied operation after WAL recovery under the lock.

    None with no error means absent, not refused or authorized. A degraded
    account returns no usable receipt. Bridge recovery must check exact shape,
    not infer an amount or position ID from the coarser op_outcome helper.
    """
    with _lock():
        led, healthy, degraded = _load_locked()
        if not healthy or led is None or degraded:
            return None, degraded or "account ledger unreadable"
        rec = led.get("ops", {}).get(op_id)
        entries, status, _ = _journal_read()
        if status is not None:
            return None, "operation WAL unreadable"
        matches = [e for e in entries if e.get("op_id") == op_id
                   and e.get("event") in _MUTATION_EVENTS]
        if rec is None:
            if matches:
                return None, "operation WAL/state disagreement"
            return None, None
        if len(matches) != 1:
            return None, "operation WAL identity missing or duplicated"
        event = matches[0]
        if rec.get("instance") is not True:
            return None, "operation has no exact instance contract"
        fields = ("event", "lane", "mint", "pos_id", "instance")
        fields += {"debit": ("stake", "qty"),
                   "debit_refused": ("stake", "qty", "reason"),
                   "credit": ("proceeds", "fee", "qty", "price_kind", "realized"),
                   "write_down": ("qty", "reason"),
                   "pending_sol": ("qty", "proceeds_sol", "ticket", "native_basis_sol",
                                   "sold_at", "fee_sol"),
                   "settle_sol": ("pending_id", "fx", "proceeds")}.get(rec.get("event"), ())
        if any(event.get(k) != rec.get(k) for k in fields):
            return None, "operation WAL/state parameter disagreement"
        # Visible bytes are not durability. Repair uncertain prior publication
        # under the same account lock before acknowledging an exact receipt.
        for path in (JOURNAL_PATH, LEDGER_PATH):
            with open(path, "rb") as fh:
                os.fsync(fh.fileno())
        dfd = os.open(ACCOUNT_DIR, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return copy.deepcopy(rec), None


def op_receipts(op_ids):
    """Batch form of op_receipt: ONE journal read for many op_ids. {op_id: credit event or None}. A truncated or
    corrupt journal yields None for every id (unknown is never proof)."""
    want = set(op_ids)
    out = {o: None for o in want}
    entries, status, _ = _journal_read()
    if status is not None:
        out["_unreadable"] = True
        return out
    for ev in entries:
        if ev.get("op_id") in want and ev.get("event") == "credit":
            out[ev["op_id"]] = dict(ev)
    return out


def op_receipt(op_id: str):
    """Read-only receipt for an APPLIED credit op: the journaled credit event (qty, proceeds, fee, net,
    price_kind), or None when there is no applied credit for the op or the journal is truncated/corrupt
    (unknown is never proof). Lets a caller finish its own side of a crash window with the ORIGINAL
    settlement facts instead of re-deriving or upgrading them."""
    entries, status, _ = _journal_read()
    if status is not None:
        return None
    rec = None
    for ev in entries:
        if ev.get("op_id") == op_id and ev.get("event") == "credit":
            rec = dict(ev)
    return rec


def op_outcome(op_id: str) -> str:
    """Journal-backed outcome for one op_id (Stage C, 2026-11-22):

      'applied'  the op settled (credit / write_down journal event)
      'orphan'   the op was deliberately NOT applied because the
                 account never held the units (credit_orphan /
                 write_down_orphan): a CONVERGED outcome for
                 pre-flag positions, not ledger divergence
      'unknown'  anything else - including a hard refusal
                 (write_down_refused), a degraded-account intent
                 note, or no record at all

    The cross-ledger close-intent recovery uses this to distinguish
    a benign orphan from a settle failure it must freeze."""
    entries, status, _ = _journal_read()
    # Truncated OR corrupt history is UNKNOWN, never proof: the
    # lost record could be this op's debit or settle.
    if status is not None:
        return "unknown"
    outcome = "unknown"
    for ev in entries:
        if ev.get("op_id") != op_id:
            continue
        kind = ev.get("event")
        if kind in ("credit", "write_down", "debit"):
            outcome = "applied"
        elif kind == "write_down_orphan" and outcome != "applied":
            # Only one variant exists: the position is absent.
            outcome = "orphan"
        elif kind == "credit_orphan" and outcome != "applied":
            # credit_orphan without a reason is the absent-position
            # variant; a reasoned one (pos_id mismatch, apply
            # refusal) names a REAL inconsistency and must stay
            # unknown so recovery freezes it.
            if not ev.get("reason"):
                outcome = "orphan"
            else:
                outcome = "unknown"
    return outcome


def position_debit(pos_id: str):
    """Read-only: the journaled DEBIT event {qty, stake, lane, mint} that opened `pos_id`, or None when absent or the
    journal is truncated/corrupt (unknown is never proof). Also None when more than one debit carries the pos_id."""
    entries, status, _ = _journal_read()
    if status is not None or not pos_id:
        return None
    hits = [ev for ev in entries if ev.get("event") == "debit" and ev.get("pos_id") == pos_id]
    if len(hits) != 1:
        return None
    ev = hits[0]
    return {"qty": ev.get("qty"), "stake": ev.get("stake"), "lane": ev.get("lane"), "mint": ev.get("mint")}


def pos_ever_existed(pos_id: Optional[str] = None,
                     lane: Optional[str] = None,
                     mint: Optional[str] = None) -> bool:
    """True when the journal holds ANY debit/credit/write_down for
    this pos_id (or, when pos_id is None, for this lane+mint).

    Orphan tolerance needs POSITIVE evidence the position belongs
    to a pre-account epoch: a missing open position alone proves
    nothing - a stale-backup restore also removes positions that
    WERE debited, and converging those would silently lose the
    debit. The journal is the account's own history; a debit in
    it means the position lived here and its absence is
    divergence, not an orphan."""
    entries, status, _ = _journal_read()
    # Absence proves never-debited ONLY on a complete, clean
    # journal; truncated/corrupt history is unknown - assume the
    # position existed (fail closed).
    if status is not None:
        return True
    for ev in entries:
        if ev.get("event") not in ("debit", "credit", "write_down"):
            continue
        if pos_id is not None and ev.get("pos_id") == pos_id:
            return True
        if pos_id is None and lane is not None \
                and ev.get("lane") == lane and ev.get("mint") == mint:
            return True
    return False


def _mark_value(mark: Any, now: float, ttl: float,
                want_qty: Optional[float],
                want_pos_id: Optional[str]
                ) -> Tuple[Optional[float], Optional[str]]:
    """Validate one mark: {value, ts, source, qty, pos_id} with a
    finite, nonnegative value, an observation timestamp within the
    TTL in BOTH directions (future-dated sources are rejected, not
    trusted), a qty matching the position's CURRENT remaining
    units, and a pos_id matching the CURRENT open instance (the
    mark is collected before the account lock; a tranche close or
    a close/reopen in between must not let a stale-sized or
    stale-instance value through - same-qty reopens get a NEW
    pos_id precisely so their old marks die). ts is the source's
    observation time, so a cached price older than the TTL is
    stale even when the marker just wrapped it. Returns
    (value, None) or (None, problem). A mark that cannot prove it
    is fresh, well-formed and bound to the current position
    instance is not a mark."""
    if not isinstance(mark, dict):
        return None, ("malformed (need {value, ts, source, qty, "
                      "pos_id})")
    v = mark.get("value")
    if (not isinstance(v, (int, float)) or isinstance(v, bool)
            or not math.isfinite(v) or v < 0):
        return None, "invalid value %r" % (v,)
    mts = mark.get("ts")
    if (not isinstance(mts, (int, float))
            or isinstance(mts, bool) or not math.isfinite(mts)):
        return None, "no usable timestamp"
    if not mark.get("source") or not isinstance(mark["source"], str):
        return None, "no provenance"
    if abs(now - mts) > ttl:
        return None, "stale (%.0fs old, ttl %.0fs)" % (now - mts, ttl)
    if want_qty is None:
        return None, "position qty unknown (cannot bind mark)"
    mq = mark.get("qty")
    if (not isinstance(mq, (int, float)) or isinstance(mq, bool)
            or not math.isfinite(mq) or mq < 0):
        return None, "no qty binding"
    if abs(float(mq) - want_qty) > 1e-6 * max(1.0, abs(want_qty)):
        return None, ("qty drift (mark %.8g vs current %.8g - "
                      "position changed since collection)"
                      % (float(mq), want_qty))
    if not want_pos_id:
        return None, "position id unknown (cannot bind mark)"
    if mark.get("pos_id") != want_pos_id:
        return None, ("pos_id drift (mark %r vs current %r - "
                      "instance changed since collection)"
                      % (mark.get("pos_id"), want_pos_id))
    return float(v), None


def _view_locked(led: Dict[str, Any],
                 marks: Optional[Dict[str, Dict[str, Any]]],
                 degraded: Optional[str]) -> Dict[str, Any]:
    """Account view from an already-loaded ledger. Caller holds the
    lock. Shared by snapshot() and the gated debit path so both see
    IDENTICAL numbers (the gate never evaluates a different view
    than the one an observer would read)."""
    out_lanes = {}
    exposure = 0.0
    open_positions = []
    mint_of: Dict[Any, str] = {}
    by_mint: Dict[str, float] = {}
    for name, st in led.get("lanes", {}).items():
        basis = sum(float(p["basis"]) for p in led.get("pending_sol", {}).values()
                    if p["lane"] == name)
        positions = []
        for mint, p in st.get("open", {}).items():
            pb = _pos_basis(p)
            basis += pb
            mark_key = p["pos_id"] if p.get("instance") is True else mint
            real_mint = p.get("mint", mint)
            open_positions.append((name, mark_key, _pos_qty(p),
                                   p.get("pos_id")))
            mint_of[(name, mark_key)] = real_mint
            positions.append({"mint": real_mint, "qty": _pos_qty(p),
                              **({"mark_key": mark_key, "instance": True}
                                 if p.get("instance") is True else {}),
                              "basis": round(pb, 6),
                              "pos_id": p.get("pos_id")})
        exposure += basis
        out_lanes[name] = {
            "open_count": len(st.get("open", {})),
            "pending_sol_count": sum(p["lane"] == name for p in
                                     led.get("pending_sol", {}).values()),
            "open_basis": round(basis, 6),
            "realized_pnl": st.get("realized_pnl", 0.0),
            "n_entries": st.get("n_entries", 0),
            "n_closes": st.get("n_closes", 0),
            "positions": positions,
        }
    # Pending proceeds are SOL holdings, never still-live token inventory.
    for pending_id, p in led.get("pending_sol", {}).items():
        open_positions.append((p["lane"], pending_id, p["proceeds_sol"], pending_id))
    cash = float(led["cash"])
    holds = led.get("holds") or {}
    out = {
        "holds": {k: dict(v) for k, v in holds.items()},
        "held_cash": round(sum(float(h["amount"])
                               for h in holds.values()), 6),
        "pending_sol": list(copy.deepcopy(led.get("pending_sol", {})).values()),
        "pending_sol_basis": round(sum(float(p["basis"]) for p in
                                       led.get("pending_sol", {}).values()), 6),
        "start_balance": led["start_balance"],
        "cash": round(cash, 6),
        "unresolved": sorted(
            (dict(v) for v in (led.get("unresolved")
                               or {}).values()),
            key=lambda v: v.get("ts") or 0),
        "unresolved_cost": round(sum(
            float(v.get("basis") or 0.0)
            for v in (led.get("unresolved") or {}).values()), 6),
        "assumed_sold": sorted(
            (dict(v) for v in (led.get("assumed_sold")
                               or {}).values()),
            key=lambda v: v.get("ts") or 0),
        "assumed_sold_cost": round(sum(
            float(v.get("basis") or 0.0)
            for v in (led.get("assumed_sold") or {}).values()), 6),
        "assumed_sold_proceeds": round(sum(
            float(v.get("proceeds") or 0.0)
            for v in (led.get("assumed_sold") or {}).values()), 6),
        "cash_spendable": round(float(led.get("cash_spendable")
                                      or 0.0), 6),
        "cash_estimated": round(float(led.get("cash_estimated")
                                      or 0.0), 6),
        "cost_exposure": round(exposure, 6),
        "book_value": round(cash + exposure, 6),
        "realized_pnl": led.get("realized_pnl", 0.0),
        "day": led.get("day"),
        "day_realized_pnl": led.get("day_realized_pnl", 0.0),
        "degraded": degraded,
        "lanes": out_lanes,
    }
    marked = None
    problems = []
    by_source: Dict[str, float] = {}
    if not open_positions:
        # Nothing to mark: equity is exactly cash, fully trusted.
        marked = 0.0
    elif marks:
        total = 0.0
        by_source: Dict[str, float] = {}
        now = time.time()
        ttl = mark_ttl_s()
        try:
            _mda = max_data_age_s()
        except Exception:
            _mda = None
        if _mda is not None and _mda > 0:
            ttl = min(ttl, float(_mda))  # owner age only tightens
        for lane, mint, want_qty, want_pos_id in open_positions:
            m = (marks.get(lane) or {}).get(mint)
            if m is None:
                problems.append("%s/%s: no mark" % (lane, mint))
                continue
            v, problem = _mark_value(m, now, ttl, want_qty,
                                     want_pos_id)
            if problem is not None:
                problems.append("%s/%s: %s" % (lane, mint, problem))
            else:
                total += v  # type: ignore[operator]
                _mk = mint_of.get((lane, mint))
                if _mk is not None:
                    by_mint[_mk] = round(by_mint.get(_mk, 0.0) + v, 6)
                src = str(m.get("source"))
                by_source[src] = round(by_source.get(src, 0.0) + v,
                                       6)
        if not problems:
            marked = round(total, 6)
    if marked is not None:
        out["marks_status"] = "ok"
        out["marked_exposure"] = marked
        # Per-source split of the trusted marked value, so risk
        # evidence shows how much of "equity" is a curve SPOT
        # estimate (not a conservative liquidation value: it
        # ignores the price impact of selling the position back
        # into the curve) versus a Jupiter-listed price.
        out["marked_by_source"] = by_source
        out["marked_by_mint"] = by_mint
        out["marked_equity"] = round(cash + marked, 6)
        # CONSERVATIVE SERIES (quarantine acceptance, 2026-11-22):
        # the equity series every risk input consumes is
        # cash_SPENDABLE + HELD (reserved) cash + marked exposure. Quarantined estimated
        # proceeds are excluded by construction, so a stale-price
        # tranche can neither boost risk headroom (baseline/peak
        # captured without it) nor mask a drawdown (the series is
        # internally consistent: estimates never enter it at any
        # point in time). marked_equity above stays the gross
        # diagnostic (total cash incl. quarantined).
        # Reserved holds are REAL cash (admitted reservations, still ours,
        # not estimated proceeds): spendable + held + marks.
        out["marked_equity_conservative"] = round(
            float(led.get("cash_spendable") or 0.0)
            + sum(float(h["amount"]) for h in (led.get("holds") or {}).values())
            + marked, 6)
        out["unrealized_pnl"] = round(marked - exposure, 6)
    elif marks or open_positions:
        out["marks_status"] = ("; ".join(problems) if problems
                               else "unavailable")
        out["marked_exposure"] = None
        out["marked_equity"] = None
        out["marked_equity_conservative"] = None
        out["unrealized_pnl"] = None
    else:
        out["marks_status"] = "unavailable"
        out["marked_exposure"] = None
        out["marked_equity"] = None
        out["marked_equity_conservative"] = None
        out["unrealized_pnl"] = None
    return out


def _update_baselines_locked(led: Dict[str, Any],
                             view: Dict[str, Any]) -> bool:
    """Capture day-start / peak equity from a trustworthy view.
    NEVER resets a baseline to a later favorable value on the same
    day; at a rollover with no trustworthy mark the previous
    baseline carries (the gate fields report stale and fail
    closed). Caller holds the lock. Returns True when changed."""
    if view["marks_status"] != "ok":
        return False
    if view.get("degraded") is not None \
            or "cash_spendable" not in led:
        # A degraded or pre-split account must not capture equity
        # baselines: the conservative series would read collapsed.
        return False
    me = view["marked_equity_conservative"]
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    changed = False
    baseline = led.get("day_baseline")
    if not isinstance(baseline, dict) or baseline.get("day") != today:
        led["day_baseline"] = {"day": today, "value": me, "ts": now}
        changed = True
    peak = led.get("peak_equity")
    if not isinstance(peak, dict)             or not _valid_amount(peak.get("value"))             or me > float(peak["value"]):
        led["peak_equity"] = {"value": me, "ts": now}
        changed = True
    return changed


def _gate_fields(led: Dict[str, Any], view: Dict[str, Any]
                 ) -> Dict[str, Any]:
    """daily_loss / drawdown_pct gate inputs: None unless BOTH the
    baseline/peak and the current equity are trustworthy (a stale
    baseline or missing marks = fail-closed inputs, never a silent
    fallback to book value)."""
    out = {"day_start_equity": None, "peak_equity": None,
           "daily_loss": None, "drawdown_pct": None}
    baseline = led.get("day_baseline")
    if isinstance(baseline, dict):
        out["day_start_equity"] = dict(baseline)
    peak = led.get("peak_equity")
    if isinstance(peak, dict):
        out["peak_equity"] = dict(peak)
    if view["marks_status"] != "ok":
        return out
    me = view["marked_equity_conservative"]
    today = view["day"]
    if (isinstance(baseline, dict)
            and _valid_amount(baseline.get("value"))
            and baseline.get("day") == today):
        out["daily_loss"] = round(
            max(0.0, float(baseline["value"]) - me), 6)
    if (isinstance(peak, dict) and _valid_amount(peak.get("value"))
            and float(peak["value"]) > 0):
        out["drawdown_pct"] = round(
            max(0.0, (float(peak["value"]) - me)
                / float(peak["value"]) * 100.0), 6)
    return out


def snapshot(marks: Optional[Dict[str, Dict[str, Any]]] = None
             ) -> Optional[Dict[str, Any]]:
    """Read-only account view. None when the ledger is unreadable.
    Includes "degraded": reason when the WAL is untrusted.

    ACCOUNTING CONTRACT (2026-11-22 review): cash + open allocated
    basis is BOOK value, NOT liquidation equity — a position down
    80% still carries its full remaining cost in book_value, so a
    drawdown gate reading book_value would miss the unrealized
    loss. The four quantities are kept explicitly separate:

      cash            uncommitted dollars (including net proceeds
                      of partial tranches already settled)
      cost_exposure   open positions at remaining allocated basis
      book_value      cash + cost_exposure (accounting identity,
                      never a drawdown input)
      realized_pnl    settled P/L only (cumulative + daily)

    `marks` optionally supplies current liquidation values of the
    REMAINING units as
    {lane: {mint: {"value": usd, "ts": epoch, "source": str}}}
    (the lanes own honest-fill marks; this module never fabricates
    them). Every mark is validated: finite, nonnegative, provenance
    present, and no older than PAPER_ACCOUNT_MARK_TTL_S. Only when
    EVERY open position has a valid fresh mark (or no position is
    open at all — equity is then exactly cash) does the view add
    marked_exposure / marked_equity / unrealized_pnl with
    marks_status "ok". Otherwise the marked fields are None and
    marks_status names the failure ("stale", "invalid", "no
    mark", "unavailable") — a stale or missing mark must degrade a
    gate to fail-closed, never silently fall back to cost.

    EQUITY BASELINES (gate inputs for Stage 2): on every snapshot
    with marks_status "ok" the marked equity is recorded as the
    day-start baseline when none exists for the current UTC day,
    and folded into the all-time peak. A baseline is NEVER reset
    to a later favorable value on the same day; when no
    trustworthy mark exists after a rollover the previous baseline
    carries with its original day visible, and daily_loss /
    drawdown_pct report None so a gate fails closed. Both are None
    until the first trustworthy mark.
    """
    if marks:
        marks = _boundary_marks(marks)
    try:
        with _lock():
            led, healthy, degraded = _load_locked()
            if not healthy or led is None:
                return None
            out = _view_locked(led, marks, degraded)
            if _update_baselines_locked(led, out):
                _save_locked(led)
            out.update(_gate_fields(led, out))
            return out
    except Exception:
        logger.exception("paper_account: snapshot failed")
        return None
