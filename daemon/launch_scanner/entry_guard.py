"""Mandatory capital-gate entry guard (offline fix; no activation).

Spec: every proposed trade must pass enforceable checks; Telegram can
never override mandatory safety (owner master directive section 11).

Contract:
  * A paper ENTRY is refused unless ALL hold: the shared paper account is
    enabled (PAPER_ACCOUNT_ENABLED == "1"), the paper_account module
    imports, its account gates are enabled (PAPER_ACCOUNT_GATES_ENABLED
    == "1"), and every mandatory gate limit (daily loss, drawdown,
    exposure) resolves to a value. Anything else returns a refusal
    reason. There is no acknowledgement, config key or env override that
    skips this check.
  * Lanes call this through a FUNCTION-LOCAL import at the entry site, so
    a variant that execs a lane file and rebinds its module-level hooks
    (e.g. _safety_ok, _paper_account) cannot neuter it.
  * Exits, credits and recovery never call this.
  * This closes the capital-gate bypass only. It is not a complete risk
    engine: trade-size, simultaneous positions, token exposure, slippage,
    price impact, failed attempts and data age are separate open items.
"""
import os


def mandatory_entry_refusal(lane):
    """Return None when an entry may proceed to try_debit, else a reason."""
    if os.environ.get("PAPER_ACCOUNT_ENABLED") != "1":
        return ("mandatory entry guard: shared paper account not enabled; "
                "entries blocked (no private-counter capital)")
    try:
        try:
            from daemon.launch_scanner import paper_account as _pa
        except ImportError:
            import paper_account as _pa
    except Exception:
        return ("mandatory entry guard: paper account unavailable; "
                "entries blocked fail-closed")
    try:
        if not _pa.gates_enabled():
            return ("mandatory entry guard: account risk gates are off; "
                    "entries blocked fail-closed")
        unset = [n for n, v in (("max_daily_loss_usd", _pa.max_daily_loss_usd()),
                                ("max_drawdown_pct", _pa.max_drawdown_pct()),
                                ("max_exposure_pct", _pa.max_exposure_pct()))
                 if v is None]
    except Exception:
        return ("mandatory entry guard: limit resolution failed; "
                "entries blocked fail-closed")
    if unset:
        return ("mandatory entry guard: no owner-approved limit for %s; "
                "entries blocked fail-closed" % ", ".join(unset))
    # Commit-time cycle fault check (R1). Flag off: nothing below runs. Flag on: a registered lane with an unresolved fault refuses, and a broken
    # import refuses (fail closed). Fault files only; no ledger lock is taken here (the caller already holds the lane ledger lock).
    if os.environ.get("AGT_GRAD_CYCLE_ENABLED") == "1":
        try:
            try:
                from daemon import grad_trader_cycle as _gtc
            except ImportError:
                import grad_trader_cycle as _gtc
            _why = _gtc.commit_refusal(lane)
        except Exception:
            return ("mandatory entry guard: cycle fault check unavailable with "
                    "AGT_GRAD_CYCLE_ENABLED=1; entries blocked fail-closed")
        if _why:
            return "mandatory entry guard: " + _why
    return None


def unrouted_opener_refusal(lane):
    """Openers that keep their OWN ledger (mirror, challenge-trader) and do
    not yet reserve cash from the single shared $10 account. Per the owner
    spec every entry must pass the shared mandatory path, and none exists
    for them yet, so their ENTRIES are refused unconditionally (exits,
    credits and recovery are not routed here). Remove only when the opener
    reserves through paper_account and passes mandatory_entry_refusal."""
    return ("mandatory entry guard: %s opens on its own ledger and is not "
            "routed through the shared account; entries blocked until it is"
            % lane)


def quote_age_refusal(row, cap_s, now):
    """Entry freshness gate for a quote/tape row. ONE age, no double count:
    age = now - (source_asof if the source gave one, else fetched_at, the
    request start, which already contains fetch latency). Unknown (missing,
    non-finite, future-dated stamp) or unset cap refuses. Returns a refusal
    string or None."""
    import math
    if cap_s is None or not (cap_s > 0):
        return "gate: no owner-approved limit for max_data_age_s (data age); entry blocked fail-closed"
    if not isinstance(row, dict):
        return "data age unknown (no quote/tape row); entry blocked fail-closed"
    base = row.get("source_asof")
    if base is None:
        base = row.get("fetched_at")
    try:
        base = float(base)
        recv = float(row.get("received_at"))
    except (TypeError, ValueError):
        return "data age unknown (missing timestamp stamps); entry blocked fail-closed"
    if not (math.isfinite(base) and math.isfinite(recv) and math.isfinite(now)):
        return "data age unknown (non-finite stamp); entry blocked fail-closed"
    age = now - base
    if age < -1.0:
        return "data age invalid (future-dated stamp, %.1fs); entry blocked fail-closed" % -age
    if age > cap_s:
        return "data age %.1fs exceeds max_data_age_s %.1fs; entry blocked" % (age, cap_s)
    return None


def post_quote_refusal(fill, prior, committed, cap_s, now):
    """POST-QUOTE timing gate (commit-time). `prior` is the durable journal fill for this op read BEFORE the buy
    call (None if none). If prior is not None the returned fill must EQUAL it (a replay of the original economics
    from the durable journal; never re-aged, never re-quoted); any difference refuses. Otherwise the fill is NEW and
    its provenance.quote_requested_at (an OBSERVATION time: how long ago we asked, not the market's age) must be
    within the owner's mandatory max_data_age_s at commit. A caller-claimed old timestamp never proves a replay.
    Fail closed: unset/non-positive cap, missing/invalid/non-finite/bool/future request time refuse."""
    import math
    if cap_s is None or not (isinstance(cap_s, (int, float)) and not isinstance(cap_s, bool) and cap_s > 0):
        return "gate: no owner-approved limit for max_data_age_s (post-quote); entry blocked fail-closed"
    if prior is not None and committed is True:
        # replay accepted ONLY with COMMITTED account authority (the shared account shows the debit for this op and
        # nothing settled it). A journaled fill alone proves nothing: buy() persists a NEW fill before this gate runs.
        if isinstance(fill, dict) and fill == prior:
            return None
        return "post-quote: fill differs from the committed journaled original; entry blocked fail-closed"
    # no prior, or a persisted-but-UNCOMMITTED prior (refused/crashed before debit): the fill is aged like a new one
    prov = fill.get("provenance") if isinstance(fill, dict) else None
    req = prov.get("quote_requested_at") if isinstance(prov, dict) else None
    if not isinstance(req, (int, float)) or isinstance(req, bool) or not math.isfinite(req) or not math.isfinite(now):
        return "post-quote: quote request time unknown; entry blocked fail-closed"
    age = now - req
    if age < -1.0:
        return "post-quote: quote request time in the future (%.1fs); entry blocked fail-closed" % -age
    if age > cap_s:
        return "post-quote: quote age %.1fs at commit exceeds max_data_age_s %.1fs; entry blocked" % (age, cap_s)
    return None
