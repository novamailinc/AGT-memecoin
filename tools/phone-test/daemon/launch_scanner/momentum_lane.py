#!/usr/bin/env python3
"""Momentum paper lane (built 2026-10-23 under the completion-build
order): $1 paper shots on EXISTING momentum-feed runners.

Candidate source: the existing momentum feed — GeckoTerminal trending
pools qualified by daemon/launch_scanner/momentum.py's
MomentumScanner (its "lowcap" + "established" qualifiers). The main
scanner (launch_scanner/__init__.py _momentum_scan) consumes that
feed in-process and emits momentum.alert Telegram events; there is
no persistent candidate bus/file for a separate lane to read, so
this lane's daemon (scripts/momentum-trader) consumes the SAME feed
directly — same fetch (sources.geckoterminal_trending_pools) and
same qualifier class — and this engine re-checks every candidate at
entry. NO Telegram cards from this lane, ever.

Terms (task standard terms WIN where they conflict with the
2026-10-16 pre-registration in docs/momentum-lane-spec.md):
- $1 paper shots, $100 paper bankroll, NO daily cap (the bankroll
  and the killswitch are the limits, mirroring grad_raw's stance).
- Honest fills INCLUDING venue fees via chain_costs.py exactly as
  grad/mirror do, plus the standard 25% round-trip cost gate.
- USER LADDER exits, constants copied from grad.py/ceo.py/mirror.py:
  50% @2x, 25% @3x, final 25% on a 30% trailing stop, -50% hard
  stop, 24h time stop.
- Runner rule re-checked at entry from FRESH tape (mirror.py's
  contract): liq >= $25k, mcap >= $50k, positive 5m momentum,
  fail-closed on unknown tape.
- Safety gate fail-closed via safety.check_token (same pattern as
  grad.py's _safety_ok / mirror.py's entry re-check): unknown =
  no entry (transient, may retry on a later scan), hard fail final.
- Killswitch lane "momentum" {window:100, min_trades:100,
  halt:True} with the pre-registered verdict enforced in-lane like
  grad.py: at >= 100 closes, expectancy <= 0 halts permanently
  (human-only re-enable); > 0 survives.

Kept from docs/momentum-lane-spec.md (2026-10-16) where it does NOT
conflict with the standard terms:
- Veto engine as a HARD gate (veto.veto_check; a veto BLOCKS).
- Counterfactual tracking of vetoed coins for 24h: vetoed mints are
  journaled in the ledger's "vetoed" list with the price at veto;
  the daemon loop stamps a 24h-later price check when due.
- MAX_OPEN = 3 concurrent positions.

DIVERGENCE (flagged, also stamped in the ledger header): the spec's
graduated-only / 30min-6h-since-graduation / price-above-
graduation-price filters describe post-graduation tokens
specifically; the momentum feed's candidates are broader runners
and normally carry NO graduation data. Those three filters are
therefore applied ONLY where the candidate actually carries
graduation info (graduated flag / graduation_ts /
graduation_price); where it doesn't, nothing is fabricated — the
per-entry applicability is journaled in
entry_features["spec_filters"].

Paper only. Nothing here touches real money, keys, or wallets.
Free-only: GeckoTerminal + DexScreener public endpoints, safety via
the existing modules.
"""
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.momentum_lane")

try:
    from daemon.launch_scanner import fills as _fills
    from daemon.launch_scanner import chain_costs as _costs
    from daemon.launch_scanner import killswitch as _ks
    from daemon.launch_scanner import veto as _veto
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import sources as _sources
    from daemon.launch_scanner import momentum as _momentum_mod
    _HAS_DEPS = True
except Exception:
    try:
        import fills as _fills
        import chain_costs as _costs
        import killswitch as _ks
        import veto as _veto
        import safety as _safety
        import sources as _sources
        import momentum as _momentum_mod
        _HAS_DEPS = True
    except Exception:
        _HAS_DEPS = False

# Trial registry (fail-soft: registration must never break trading).
try:
    from daemon.launch_scanner import trials as _trials
except Exception:
    try:
        import trials as _trials  # type: ignore
    except Exception:
        _trials = None  # type: ignore

# F2 sustained-unquotability exit (2026-10-23, user-approved) +
# the direct-reserve cross-probe it needs: fail-soft imports — a
# missing module leaves the pre-F2 behavior byte-identical. (This
# lane copied the shared exit machinery, so it takes the same F2
# branch via the same f2.check choke point.)
try:
    from daemon.launch_scanner import f2 as _f2
    from daemon.launch_scanner import reserves as _reserves
except Exception:
    try:
        import f2 as _f2  # type: ignore
        import reserves as _reserves  # type: ignore
    except Exception:
        _f2 = None  # type: ignore
        _reserves = None  # type: ignore

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
LEDGER_PATH = os.path.join(HOME, "momentum_ledger.json")
MAX_CLOSES = 500
MAX_VETOED = 500

BANKROLL_START = 100.00
NOTIONAL_USD = 1.00       # the user's $1/shot rule
MAX_OPEN = 3              # spec decision (2026-10-16), flagged in task
LANE = "momentum"
DEFAULT_CHAIN = "solana"

# Runner rule (standing, 2026-10-18): re-checked at entry from fresh
# tape, identical thresholds + fail-closed contract to mirror.py.
RUNNER_MIN_LIQ_USD = 25_000.0
RUNNER_MIN_MCAP_USD = 50_000.0

# The user's ladder — identical constants to grad.py/ceo.py/mirror.py.
LEG_A_FRAC, LEG_A_TARGET = 0.50, 2.0   # sell half at 2x
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0   # half the remainder at 3x
LEG_C_FRAC, LEG_C_TRAIL = 0.25, 0.30   # final quarter on 30% trailing stop
HARD_STOP_MULT = 0.50                  # hard exit at -50%
TIME_STOP_H = 24                       # hygiene backstop

# Pre-registered verdict sample (see module docstring + ledger header).
VERDICT_SAMPLE_CLOSES = 100

# Counterfactual horizon for vetoed coins (spec: track for 24h).
COUNTERFACTUAL_H = 24

DS_CHAIN = {"solana": "solana", "base": "base", "ethereum": "ethereum"}

SPEC_DIVERGENCE_NOTE = (
    "Task standard terms win over docs/momentum-lane-spec.md "
    "(2026-10-16) where they conflict: user-ladder exits (not "
    "trail-12%/-30%/4h), {100,100} verdict at 100 closes (not 200), "
    "momentum-feed candidates (broader than post-graduation only), "
    "$25k liquidity runner floor (not $20k). Kept from the spec: "
    "veto engine as hard gate, vetoed-coin 24h counterfactuals, "
    "MAX_OPEN=3. The spec's graduated-only / 30m-6h age / "
    "above-graduation-price filters apply only where a candidate "
    "carries graduation data; applicability is journaled per entry "
    "in entry_features.spec_filters.")


# ---- ledger -------------------------------------------------------------
# Same inter-process locking pattern as grad.py/ceo.py/mirror.py:
# every read-modify-write section holds an exclusive flock on a lock
# file next to the ledger; writes are atomic tmp+rename. The lock
# path is derived from LEDGER_PATH at call time so tests isolate it.

import fcntl


def _lock_path():
    return LEDGER_PATH + ".lock"


class _ledger_lock:
    """Exclusive inter-process lock spanning a read-modify-write section."""

    def __enter__(self):
        self._lf = open(_lock_path(), "w")
        fcntl.flock(self._lf, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self._lf, fcntl.LOCK_UN)
        finally:
            self._lf.close()
        return False


def _preregistration():
    """The verdict terms, stamped into the ledger header at creation
    so they cannot be quietly changed later."""
    return {
        "lane": LANE,
        "sample_closes": VERDICT_SAMPLE_CLOSES,
        "rule": ("At >= %d closes, if mean pnl per close <= 0 the lane "
                 "halts permanently (killswitch lane 'momentum'); "
                 "if > 0 the lane survived its sample. Re-enable is "
                 "human-only." % VERDICT_SAMPLE_CLOSES),
        "killswitch_cfg": {"window": 100, "min_trades": 100,
                           "halt": True},
        "terms": {
            "notional_usd": NOTIONAL_USD,
            "max_open": MAX_OPEN,
            "daily_entry_cap": None,  # none — bankroll-bound by design
            "runner_rule": {"min_liq_usd": RUNNER_MIN_LIQ_USD,
                            "min_mcap_usd": RUNNER_MIN_MCAP_USD,
                            "positive_momentum": True},
            "candidate_source": "momentum feed: "
                                "sources.geckoterminal_trending_pools "
                                "+ momentum.MomentumScanner qualify "
                                "(lowcap + established)",
            "safety_gate": "fail-closed (safety.check_token)",
            "veto": "HARD gate (veto.veto_check)",
            "counterfactuals": "vetoed coins tracked for 24h",
            "cards": "NONE (no Telegram alerts from this lane)",
            "ladder": "50%@2x / 25%@3x / 25% trail 30% / -50% stop / 24h",
        },
        "spec_provenance": "docs/momentum-lane-spec.md (2026-10-16) "
                           "+ task standard terms (win on conflict)",
        "spec_divergence": SPEC_DIVERGENCE_NOTE,
        "registered_ts": time.time(),
    }


def _blank():
    return {
        "bankroll_start": BANKROLL_START,
        "bankroll": BANKROLL_START,
        "positions": {},
        "closes": [],
        "created_ts": time.time(),
        "state": "active",
        "preregistered_verdict": _preregistration(),
        "verdict": None,
        "vetoed": [],         # counterfactual journal (spec)
        "dead_mints": {},     # mint -> {"ts":, "reason":} final skips
    }


def _read():
    """Bare ledger read. Hold _ledger_lock if you will modify+write."""
    try:
        with open(LEDGER_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict):
            b = _blank()
            for k, v in b.items():
                d.setdefault(k, v)
            if not isinstance(d.get("positions"), dict):
                d["positions"] = {}
            if not isinstance(d.get("closes"), list):
                d["closes"] = []
            if not isinstance(d.get("vetoed"), list):
                d["vetoed"] = []
            if not isinstance(d.get("dead_mints"), dict):
                d["dead_mints"] = {}
            return d
    except Exception:
        pass
    return _blank()


def _write(d):
    """Bare ledger write (atomic tmp+rename, fsync'd). Hold _ledger_lock.

    Flush+fsync the tmp file before the rename and fsync the
    directory after it (best-effort): this VM hard-resets, and an
    un-fsync'd rename can come back hollow/stale (2026-10-24
    grad_select ledger loss)."""
    try:
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, LEDGER_PATH)
        try:
            dfd = os.open(os.path.dirname(LEDGER_PATH) or ".",
                          os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        return True
    except Exception:
        return False


def _load():
    """Locked single read (for read-only callers)."""
    with _ledger_lock():
        return _read()


def ensure_ledger():
    """Create the ledger on first run (stamps the pre-registered
    verdict terms + divergence note into its header)."""
    with _ledger_lock():
        if os.path.exists(LEDGER_PATH):
            return _read()
        d = _blank()
        _write(d)
        logger.warning("momentum ledger created: bankroll $%.2f, "
                       "verdict pre-registered at %d closes",
                       d["bankroll"], VERDICT_SAMPLE_CLOSES)
        return d


def account_state():
    """Read-only summary for reports/tests. Never raises."""
    try:
        d = _load()
        return {
            "bankroll": d["bankroll"],
            "bankroll_start": d["bankroll_start"],
            "state": d["state"],
            "n_open": len(d["positions"]),
            "n_closes": len(d["closes"]),
            "n_vetoed": len(d.get("vetoed") or []),
            "verdict": d.get("verdict"),
            "created_ts": d["created_ts"],
        }
    except Exception:
        return {}


def account_cash():
    try:
        return float(_load()["bankroll"])
    except Exception:
        return 0.0


def register_trial():
    """Register this lane in the trial registry (trials.register is
    idempotent for identical terms, so calling on every boot is
    safe). Lanes historically registered via backfill only; this
    lane registers explicitly from its daemon's boot path. Never
    raises."""
    try:
        if _trials is None:
            return None
        pre = _preregistration()
        return _trials.register(
            LANE, "lane", pre["terms"],
            note="momentum lane (2026-10-23 completion build); "
                 "verdict: %s" % pre["rule"])
    except Exception:
        return None


# ---- price feeds ----------------------------------------------------------

def _http_json(url, timeout=12, payload=None):
    try:
        data = None
        headers = {"User-Agent": "forex-agent/1.0"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


_LAST_TAPE_SOURCE = ""


def _fresh_tape(chain, mint):
    """Fresh tape stats dict (DexScreener, GeckoTerminal fallback per
    sources.tape_batch_stats). None = unknown. Never raises."""
    global _LAST_TAPE_SOURCE
    try:
        stats = _sources.tape_batch_stats([mint],
                                          chain=chain or "solana")
        st = (stats or {}).get(mint) or None
        _LAST_TAPE_SOURCE = str((st or {}).get("tape_source") or "")
        return st
    except Exception:
        _LAST_TAPE_SOURCE = ""
        return None


QUOTE_RETRY_GAP_S = 2.0
_LAST_QUOTE_DETAIL = ""


def _ds_pairs_price(pairs):
    """(price, note): price of the highest-liquidity pair that carries
    one — the same selection rule as grad.py/mirror.py."""
    if not isinstance(pairs, list) or not pairs:
        return 0.0, "no pairs returned"
    best_px, best_liq, priced = 0.0, -1.0, 0
    for p in pairs:
        if not isinstance(p, dict):
            continue
        try:
            px = float(p.get("priceUsd") or 0)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0:
            priced += 1
            if liq > best_liq:
                best_px, best_liq = px, liq
    if best_px > 0:
        return best_px, "%d pair(s), %d priced" % (len(pairs), priced)
    return 0.0, "%d pair(s), none priced" % len(pairs)


def _quote_detail(chain, mint):
    """(price, trail): best-pair pairs fetch -> tape fallback -> one
    bounded retry. Trail is plain-language, for skip logs."""
    global _LAST_QUOTE_DETAIL
    ds = DS_CHAIN.get((chain or "").lower())
    if not ds:
        _LAST_QUOTE_DETAIL = "no dexscreener chain for %s" % chain
        return 0.0, _LAST_QUOTE_DETAIL
    url = "https://api.dexscreener.com/tokens/v1/%s/%s" % (ds, mint)
    trail = []
    px, note = _ds_pairs_price(_http_json(url))
    trail.append("pairs fetch: " + note)
    if px <= 0:
        st = _fresh_tape(chain, mint)
        tpx = float((st or {}).get("price_usd") or 0) if st else 0.0
        if tpx > 0:
            trail.append("tape fallback: priced via %s"
                         % ((st or {}).get("tape_source") or "unknown"))
            px = tpx
        else:
            trail.append("tape fallback: "
                         + ("no tape" if not st else "tape unpriced"))
    if px <= 0:
        time.sleep(QUOTE_RETRY_GAP_S)
        px2, note2 = _ds_pairs_price(_http_json(url))
        trail.append("pairs retry: " + note2)
        if px2 > 0:
            px = px2
    _LAST_QUOTE_DETAIL = "; ".join(trail)
    return px, _LAST_QUOTE_DETAIL


def quote_price(chain, mint, spot_index=None):
    """Live quoted price in USD. 0.0 = no source can price the mint."""
    try:
        return _quote_detail(chain or DEFAULT_CHAIN, mint)[0]
    except Exception:
        return 0.0


# ---- entry gates ----------------------------------------------------------

def _runner_gate(chain, mint):
    """(ok, why, tape). Runner rule re-checked from FRESH tape at
    entry — the candidate's own fields are never trusted (mirror.py
    contract). Fail-closed on unknown tape."""
    st = _fresh_tape(chain, mint)
    if not st:
        return False, "runner gate: no live tape", None
    liq = float(st.get("usd_liquidity") or 0)
    mcap = float(st.get("usd_market_cap") or 0)
    chg = st.get("chg_m5")
    chg = float(chg) if chg is not None else 0.0
    if liq < RUNNER_MIN_LIQ_USD:
        return False, "runner gate: liq $%.0f < $25k" % liq, st
    if mcap < RUNNER_MIN_MCAP_USD:
        return False, "runner gate: mcap $%.0f < $50k" % mcap, st
    if chg <= 0:
        return False, "runner gate: not rising (chg_m5 %.1f%%)" % chg, st
    return True, "", st


def _spec_filters(cand, quoted, now):
    """The 2026-10-16 spec's post-graduation filters, applied ONLY
    where the candidate carries graduation data (see the module
    docstring's divergence note). Returns (blocked_reason, journal):
    blocked_reason "" = pass; journal records, per filter, whether
    it was applicable and whether it passed (None = not applicable,
    never fabricated)."""
    journal = {}
    grad_ts = cand.get("graduation_ts")
    if grad_ts is None:
        grad_ts = cand.get("graduated_ts")
    if grad_ts is None:
        extra = cand.get("extra") or {}
        if isinstance(extra, dict):
            grad_ts = extra.get("graduation_ts") or \
                extra.get("graduated_ts")
    graduated = cand.get("graduated")
    if graduated is None and isinstance(cand.get("extra"), dict):
        graduated = cand["extra"].get("graduated")
    grad_price = cand.get("graduation_price")
    if grad_price is None and isinstance(cand.get("extra"), dict):
        grad_price = cand["extra"].get("graduation_price")

    # graduated-only
    if graduated is not None:
        ok = bool(graduated)
        journal["graduated_only"] = {"applicable": True, "passed": ok}
        if not ok:
            return "spec filter: not graduated", journal
    elif grad_ts is not None or grad_price is not None:
        journal["graduated_only"] = {"applicable": True, "passed": True}
    else:
        journal["graduated_only"] = {"applicable": False, "passed": None}

    # age since graduation: 30 min - 6 hours
    if grad_ts is not None:
        try:
            age_s = now - float(grad_ts)
            ok = 1800.0 <= age_s <= 6 * 3600.0
            journal["age_since_graduation_30m_6h"] = {
                "applicable": True, "passed": ok,
                "age_s": round(age_s, 1)}
            if not ok:
                return ("spec filter: age since graduation %.0fm "
                        "outside 30m-6h" % (age_s / 60.0)), journal
        except (TypeError, ValueError):
            journal["age_since_graduation_30m_6h"] = {
                "applicable": False, "passed": None}
    else:
        journal["age_since_graduation_30m_6h"] = {
            "applicable": False, "passed": None}

    # price above graduation price
    if grad_price is not None:
        try:
            gp = float(grad_price)
            ok = gp > 0 and quoted > gp
            journal["price_above_graduation_price"] = {
                "applicable": True, "passed": ok,
                "graduation_price": gp}
            if not ok:
                return "spec filter: price not above graduation price", \
                    journal
        except (TypeError, ValueError):
            journal["price_above_graduation_price"] = {
                "applicable": False, "passed": None}
    else:
        journal["price_above_graduation_price"] = {
            "applicable": False, "passed": None}
    return "", journal


def _safety_ok(chain, mint):
    """(ok, why, report, transient). Fail-closed via
    safety.check_token — same pattern as grad.py's _safety_ok:
    unknown = no entry (transient), hard fail is final."""
    try:
        rep = _safety.check_token(chain, mint, timeout=10)
    except Exception as e:
        return False, "safety error (%s)" % e, None, True
    if not isinstance(rep, dict) or rep.get("unknown"):
        return False, "safety unknown", rep, True
    if not rep.get("passed"):
        return False, "; ".join(rep.get("fails", []))[:160], rep, False
    return True, "", rep, False


def _veto_ok(chain, mint, symbol, safety_report):
    """(ok, why). Veto engine as a HARD gate: a returned veto blocks.
    Same call shape as grad.py (run_bundle=False — the cheap vetoes).
    Engine exceptions fail-open, matching veto.py's own doctrine
    (vetoes act on positive evidence only) and grad/mirror semantics."""
    try:
        rep = _veto.veto_check(mint, chain, symbol=symbol or "",
                               safety_report=safety_report,
                               run_bundle=False, lane=LANE)
        return bool(rep.get("passed")), "; ".join(rep.get("vetoes", []))
    except Exception as e:
        return True, "veto unavailable (%s)" % e


def _mark_dead(mint, reason):
    try:
        with _ledger_lock():
            d = _read()
            d["dead_mints"][mint] = {"ts": time.time(),
                                     "reason": str(reason)[:160]}
            _write(d)
    except Exception:
        pass


def _journal_vetoed(cand, chain, mint, why, price_at_veto):
    """Counterfactual journal (spec): record the vetoed coin + the
    price at veto; the 24h outcome is stamped later by
    update_counterfactuals(). Dedupe: one record per mint ever."""
    try:
        with _ledger_lock():
            d = _read()
            if any(v.get("mint") == mint for v in d["vetoed"]):
                return
            d["vetoed"].append({
                "mint": mint,
                "symbol": str(cand.get("symbol") or "?")[:32],
                "chain": chain,
                "ts": time.time(),
                "veto_reason": str(why)[:200],
                "price_at_veto": float(price_at_veto or 0),
                "price_24h": None,
                "price_24h_ts": None,
                "change_24h_pct": None,
                "checked_24h": False,
            })
            d["vetoed"] = d["vetoed"][-MAX_VETOED:]
            _write(d)
    except Exception:
        pass


def _skip(mint, why, final):
    logger.warning("momentum SKIP %s: %s%s", (mint or "?")[:16], why,
                   "" if final else " (will retry on a later scan)")
    return False, why, final


def consider_candidate(cand, now=None):
    """Decide one momentum-feed candidate (a MomentumScanner.qualify
    runner dict). Returns (opened, reason, final): final=True means
    never reconsider this mint (entered, hard-gated, vetoed, or
    already traded); final=False means a transient block. Never
    raises."""
    try:
        return _consider_candidate(cand, now)
    except Exception as e:
        logger.warning("momentum consider_candidate failed: %s", e)
        return False, "momentum error: %s" % e, False


def _consider_candidate(cand, now):
    if not _HAS_DEPS:
        return _skip(str(cand.get("mint") or ""), "momentum deps missing",
                     False)
    now = time.time() if now is None else now
    mint = str(cand.get("mint") or "")
    symbol = str(cand.get("symbol") or "?")[:32]
    chain = str(cand.get("chain") or DEFAULT_CHAIN).lower()
    if not mint:
        return _skip(mint, "no mint", True)

    try:
        if _ks.is_halted(LANE):
            return _skip(mint, "momentum lane halted by killswitch", True)
    except Exception:
        pass

    with _ledger_lock():
        d = _read()
        if float(d["bankroll"]) < NOTIONAL_USD:
            return _skip(mint, "bankroll exhausted (cash $%.2f)"
                         % float(d["bankroll"]), True)
        if mint in d["positions"]:
            return _skip(mint, "already open", True)
        if any(c.get("mint") == mint for c in d["closes"]):
            return _skip(mint, "already traded (one entry per mint "
                               "ever)", True)
        if any(v.get("mint") == mint for v in d["vetoed"]):
            return _skip(mint, "already vetoed (counterfactual "
                               "journaled)", True)
        if mint in d["dead_mints"]:
            return _skip(mint, "dead: %s"
                         % d["dead_mints"][mint].get("reason", ""), True)
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d)" % MAX_OPEN,
                         False)

    # Pre-registered cost gate (chain_costs, same as every paper lane).
    blocked, why = _costs.cost_gate(chain, NOTIONAL_USD)
    if blocked:
        logger.warning("momentum %s: %s", mint, why)
        return False, why, True

    # Runner rule re-check from fresh tape (fail-closed).
    ok_gate, why_gate, st = _runner_gate(chain, mint)
    if not ok_gate:
        return _skip(mint, why_gate, False)

    quoted = quote_price(chain, mint)
    if quoted <= 0:
        why = "no live quote"
        if _LAST_QUOTE_DETAIL:
            why = "no live quote (%s)" % _LAST_QUOTE_DETAIL
        return _skip(mint, why, False)
    liquidity = float((st or {}).get("usd_liquidity") or 0) or \
        float(cand.get("liq_usd") or 0)
    mcap = float((st or {}).get("usd_market_cap") or 0) or \
        float(cand.get("usd_market_cap") or 0)
    tape_source = (st or {}).get("tape_source") or ""

    # Spec post-graduation filters — only where graduation data exists.
    blocked_reason, spec_journal = _spec_filters(cand, quoted, now)
    if blocked_reason:
        return _skip(mint, blocked_reason, True)

    # Safety gate — fail-closed, hard fail is final.
    ok, why, rep, transient = _safety_ok(chain, mint)
    if not ok:
        if not transient:
            _mark_dead(mint, "safety: " + why)
        return _skip(mint, "safety: " + why, not transient)

    # Veto engine — HARD gate. A veto blocks AND is journaled to the
    # counterfactual tracker with the price at veto.
    ok, why = _veto_ok(chain, mint, symbol, rep)
    if not ok:
        _journal_vetoed(cand, chain, mint, why, quoted)
        return _skip(mint, "veto at entry: %s" % why, True)

    gas_in = _costs.tx_cost_usd(chain, NOTIONAL_USD)
    eff_notional = NOTIONAL_USD - gas_in
    if eff_notional <= 0:
        return _skip(mint, "costs exceed notional", True)
    try:
        filled = _fills.honest_entry(quoted, eff_notional,
                                     liquidity or None)
    except Exception:
        filled = quoted
    if filled <= 0:
        return _skip(mint, "bad fill price", False)
    tokens = eff_notional / filled

    pos = {
        "mint": mint, "symbol": symbol,
        "chain": chain, "source": "momentum", "lane": LANE,
        "score": 0.0, "tier": str(cand.get("lane") or "established"),
        "entry_ts": time.time(), "quoted_price": quoted,
        "entry_price": filled, "notional_usd": NOTIONAL_USD,
        "peak_price": filled,
        "peak_ts": time.time(),
        "tokens": tokens, "gas_in_usd": round(gas_in, 6),
        "legs": {
            "a": {"frac": LEG_A_FRAC, "target": LEG_A_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "b": {"frac": LEG_B_FRAC, "target": LEG_B_TARGET,
                  "done": False, "exit_price": 0, "realized_usd": 0},
            "c": {"frac": LEG_C_FRAC, "trail": LEG_C_TRAIL,
                  "peak": filled, "done": False, "exit_price": 0,
                  "realized_usd": 0},
        },
        "realized_usd": 0.0,
        "status": "open",
        "spot_index": None,
        "pair": str(cand.get("pool_address") or ""),
        "entry_snapshot": {
            "score": 0.0, "tier": str(cand.get("lane") or "established"),
            "mcap": mcap, "liquidity": liquidity,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
            "safety": {
                "score": (rep or {}).get("score", 0),
                "flags": list((rep or {}).get("flags", []))[:6],
            },
        },
        "entry_features": {
            "chain": chain, "source": "momentum-feed",
            "momentum_lane": str(cand.get("lane") or "established"),
            "pool": str(cand.get("pool_address") or ""),
            "chg_24h_pct": cand.get("chg_24h_pct"),
            "chg_1h_pct": cand.get("chg_1h_pct"),
            "vol_24h_usd": cand.get("vol_24h_usd"),
            "mcap": mcap, "liquidity": liquidity,
            "tape_source": tape_source,
            "quoted_price": quoted, "filled_price": filled,
            "cost_summary": _costs.cost_summary(chain, NOTIONAL_USD),
            "spec_filters": spec_journal,
        },
    }

    # Authoritative section: dedupe + caps + bankroll re-checked
    # under the same lock that debits cash and inserts the position.
    with _ledger_lock():
        d = _read()
        if float(d["bankroll"]) < NOTIONAL_USD:
            if d["state"] != "bankroll_exhausted":
                d["state"] = "bankroll_exhausted"
                _write(d)
            return _skip(mint, "bankroll exhausted (cash $%.2f)"
                         % float(d["bankroll"]), True)
        if mint in d["positions"]:
            return _skip(mint, "already open (race)", True)
        if any(c.get("mint") == mint for c in d["closes"]):
            return _skip(mint, "already traded (race)", True)
        if any(v.get("mint") == mint for v in d["vetoed"]):
            return _skip(mint, "already vetoed (race)", True)
        if mint in d["dead_mints"]:
            return _skip(mint, "dead (race)", True)
        if len(d["positions"]) >= MAX_OPEN:
            return _skip(mint, "max open positions (%d) (race)"
                         % MAX_OPEN, False)
        d["bankroll"] = round(float(d["bankroll"]) - NOTIONAL_USD, 6)
        d["positions"][mint] = pos
        _write(d)
    logger.warning("momentum OPEN %s %s @ %s ($1, %s, liq $%.0f, "
                   "mcap $%.0f, cash now $%.2f)",
                   symbol, mint[:12], filled,
                   pos["tier"], liquidity, mcap, account_cash())
    return True, "opened", True


# ---- candidate feed ---------------------------------------------------------

class _FeedCfg:
    """Config stub carrying momentum.py's own qualifier defaults.

    MomentumScanner.qualify reads these attributes off a `degen`
    config object; this lane's daemon has no degen config, so the
    scanner's documented defaults are restated here — identical
    values to the _cfg() fallbacks in momentum.py."""
    momentum_min_gain_24h = 20.0
    momentum_max_drawdown_1h = 15.0
    momentum_min_vol_24h_usd = 100000.0
    momentum_min_liq_usd = 50000.0
    momentum_cooldown_hours = 6.0
    momentum_lowcap_min_mcap_usd = 50000.0
    momentum_lowcap_max_mcap_usd = 10000000.0


def fetch_pools(networks=("solana",)):
    """Trending pools from the existing momentum feed source
    (sources.geckoterminal_trending_pools). Never raises."""
    pools = []
    for net in networks or ("solana",):
        try:
            pools.extend(
                _sources.geckoterminal_trending_pools(str(net)) or [])
        except Exception:
            continue
    return pools


def qualify_pools(pools, now=None, scanner=None):
    """Qualify raw trending pools through the EXISTING
    momentum.MomentumScanner (lowcap + established). The 6h alert
    cooldown is bypassed (skip_cooldown=True): it suppresses
    duplicate Telegram CARDS in the main scanner, and this lane
    sends no cards — entry dedupe is the ledger's job (one entry
    per mint ever). Never raises."""
    out = []
    try:
        scanner = scanner or _momentum_mod.MomentumScanner()
        cfg = _FeedCfg()
        now = time.time() if now is None else now
        for pool in pools or []:
            try:
                r = scanner.qualify(cfg, pool, now, skip_cooldown=True)
            except Exception:
                continue
            if r:
                out.append(r)
    except Exception:
        pass
    return out


def scan_once(now=None, pools=None, networks=("solana",), scanner=None):
    """One detection cycle: fetch (unless pools given) + qualify via
    the momentum feed, then attempt entries. Returns counters.
    Never raises."""
    out = {"pools": 0, "qualified": 0, "opened": 0, "skipped": 0}
    try:
        return _scan_once(out, now, pools, networks, scanner)
    except Exception as e:
        logger.warning("momentum scan_once failed: %s", e)
        return out


def _scan_once(out, now, pools, networks, scanner):
    now = time.time() if now is None else now
    ensure_ledger()
    try:
        if _ks.is_halted(LANE):
            logger.warning("momentum scan: lane halted by killswitch "
                           "— no entries (exits still run)")
            return out
    except Exception:
        pass
    if pools is None:
        pools = fetch_pools(networks)
    out["pools"] = len(pools)
    runners = qualify_pools(pools, now, scanner)
    out["qualified"] = len(runners)
    for cand in runners:
        opened, _why, _final = consider_candidate(cand, now)
        if opened:
            out["opened"] += 1
        else:
            out["skipped"] += 1
    return out


# ---- vetoed-coin counterfactuals -------------------------------------------

def update_counterfactuals(now=None, price_fn=None):
    """Stamp the 24h-later price check on due vetoed records (spec:
    vetoed coins tracked for 24h — did we veto winners?). Fail-soft:
    an unquotable coin stays unchecked and retries next cycle.
    Returns the number of records stamped. Never raises."""
    try:
        return _update_counterfactuals(now, price_fn)
    except Exception as e:
        logger.warning("momentum counterfactuals failed: %s", e)
        return 0


def _update_counterfactuals(now, price_fn):
    now = time.time() if now is None else now
    with _ledger_lock():
        due = [v for v in _read().get("vetoed") or []
               if not v.get("checked_24h")
               and now - float(v.get("ts") or 0) >= COUNTERFACTUAL_H * 3600]
    if not due:
        return 0
    prices = {}
    for v in due:
        try:
            if price_fn:
                px = float(price_fn(v.get("chain") or DEFAULT_CHAIN,
                                    v["mint"], None) or 0)
            else:
                px = quote_price(v.get("chain") or DEFAULT_CHAIN,
                                 v["mint"])
        except Exception:
            px = 0.0
        if px > 0:
            prices[v["mint"]] = px
    if not prices:
        return 0
    stamped = 0
    with _ledger_lock():
        d = _read()
        for v in d.get("vetoed") or []:
            px = prices.get(v.get("mint"))
            if px and not v.get("checked_24h"):
                v["price_24h"] = px
                v["price_24h_ts"] = now
                base = float(v.get("price_at_veto") or 0)
                v["change_24h_pct"] = (round((px / base - 1.0) * 100.0, 2)
                                       if base > 0 else None)
                v["checked_24h"] = True
                stamped += 1
        if stamped:
            _write(d)
    return stamped


# ---- exits (the user's ladder, grad/mirror-identical constants) -----------

def _close_leg(pos, leg_key, exit_price, reason):
    """Close one leg at an honest exit. Returns realized USD."""
    leg = pos["legs"][leg_key]
    if leg["done"]:
        return 0.0
    leg_tokens = pos["tokens"] * leg["frac"]
    try:
        leg_usd = leg_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd,
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = leg_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(pos["chain"], gross)
    net = gross - gas_out
    leg.update(done=True, exit_price=filled_exit,
               realized_usd=round(net, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _close_remaining(pos, exit_price, reason):
    """Close all remaining legs as ONE combined exit (one tx cost) —
    same rule as grad.py/ceo.py/mirror.py."""
    open_legs = [k for k in ("a", "b", "c") if not pos["legs"][k]["done"]]
    if not open_legs:
        return 0.0
    total_tokens = sum(pos["tokens"] * pos["legs"][k]["frac"]
                       for k in open_legs)
    try:
        leg_usd = total_tokens * exit_price
        filled_exit = _fills.honest_exit(
            exit_price, leg_usd,
            pos.get("last_liquidity")
            or pos["entry_snapshot"].get("liquidity") or None)
    except Exception:
        filled_exit = exit_price
    gross = total_tokens * filled_exit
    gas_out = _costs.tx_cost_usd(pos["chain"], gross)
    net = gross - gas_out
    for key in open_legs:
        leg = pos["legs"][key]
        share = (pos["tokens"] * leg["frac"]) / total_tokens \
            if total_tokens else 0
        leg.update(done=True, exit_price=filled_exit,
                   realized_usd=round(net * share, 6), exit_reason=reason)
    pos["realized_usd"] = round(pos["realized_usd"] + net, 6)
    return net


def _peak_fields(pos):
    """Peak price/multiple for the close record, from RECORDED prices
    only — never fabricated (grad/mirror semantics)."""
    entry = float(pos.get("entry_price") or 0)
    candidates = []
    tracked = pos.get("peak_price")
    if tracked:
        candidates.append(float(tracked))
    legs = pos.get("legs") or {}
    lc = legs.get("c") or {}
    if lc.get("peak"):
        candidates.append(float(lc["peak"]))
    for v in legs.values():
        if v.get("done") and v.get("exit_price"):
            candidates.append(float(v["exit_price"]))
    if entry <= 0 or not candidates:
        return {"peak_price": None, "peak_mult": None,
                "peak_unknown": True, "peak_ts": None}
    peak_price = max(candidates)
    return {"peak_price": peak_price,
            "peak_mult": round(peak_price / entry, 4),
            "peak_ts": pos.get("peak_ts"),
            "peak_source": ("legacy_lower_bound"
                            if pos.get("peak_partial") else "tracked")}


def _verdict_check(d):
    """The PRE-REGISTERED verdict, evaluated in-ledger at the moment
    the sample completes (grad.py semantics). Mutates d (lock held
    by caller); returns True when the lane must halt. Runs once."""
    if d.get("verdict"):
        return False
    closes = d.get("closes") or []
    if len(closes) < VERDICT_SAMPLE_CLOSES:
        return False
    exp = sum(float(c.get("pnl_usd") or 0) for c in closes) / len(closes)
    halt = exp <= 0
    d["verdict"] = {
        "ts": time.time(),
        "n_closes": len(closes),
        "expectancy": round(exp, 6),
        "net_usd": round(sum(float(c.get("pnl_usd") or 0)
                             for c in closes), 4),
        "action": ("HALT — sample expectancy <= 0" if halt else
                   "SURVIVED — sample expectancy > 0"),
        "sample_closes": VERDICT_SAMPLE_CLOSES,
    }
    logger.warning("momentum VERDICT at %d closes: expectancy %.4f "
                   "-> %s", len(closes), exp, d["verdict"]["action"])
    return halt


def _finalize(d, mint, pos, closed_reason):
    """Move an open position to closes, settle cash, record the lane
    trade, evaluate the pre-registered verdict. Lock held by caller."""
    realized = round(float(pos["realized_usd"]), 6)
    pnl = round(realized - NOTIONAL_USD, 4)
    d["bankroll"] = round(float(d["bankroll"]) + realized, 6)
    if d["bankroll"] < NOTIONAL_USD:
        d["state"] = "bankroll_exhausted"
    rec = {
        "mint": mint, "symbol": pos["symbol"], "chain": pos["chain"],
        "source": pos["source"], "lane": LANE,
        "entry_ts": pos["entry_ts"], "close_ts": time.time(),
        "entry_price": pos["entry_price"],
        "realized_usd": realized,
        "realized_mult": round(realized / NOTIONAL_USD, 4),
        "pnl_usd": pnl,
        "closed_reason": closed_reason,
        "legs": {k: {"done": v["done"],
                     "exit_price": v.get("exit_price", 0),
                     "realized_usd": v.get("realized_usd", 0),
                     "exit_reason": v.get("exit_reason", "")}
                 for k, v in pos["legs"].items()},
        "entry_snapshot": pos["entry_snapshot"],
        "entry_features": pos.get("entry_features", {}),
        "quote_journal": list(pos.get("quote_journal") or [])[-10:],
        "exit_liquidity": pos.get("last_liquidity"),
    }
    rec.update(_peak_fields(pos))
    # F2 stamp (2026-10-23): pos["_f2"] is set ONLY by the F2 exit
    # branch — its presence is the unmistakable marker. (This lane
    # has no verdict-ledger hook; the close record carries it.)
    if pos.get("_f2"):
        rec["f2"] = dict(pos["_f2"])
    d["closes"].append(rec)
    d["closes"] = d["closes"][-MAX_CLOSES:]
    del d["positions"][mint]
    halt_verdict = _verdict_check(d)
    _write(d)  # lock held by caller
    try:
        _ks.record_trade(LANE, pnl)
        _ks.check_lane(LANE)
        if halt_verdict:
            _ks.halt_lane(
                LANE,
                "pre-registered verdict: expectancy %.4f <= 0 over "
                "%d closes" % (d["verdict"]["expectancy"],
                               d["verdict"]["n_closes"]))
    except Exception:
        pass
    logger.warning("momentum CLOSE %s %s pnl=$%.2f realized=%.3fx "
                   "(%s) cash=$%.2f", pos["chain"], pos["symbol"], pnl,
                   rec["realized_mult"], closed_reason, d["bankroll"])
    return rec


def poll_exits(price_fn=None):
    """Evaluate exits for all open momentum positions. price_fn(chain,
    mint, spot_index) -> float override for tests. Returns close
    records. Never raises."""
    try:
        return _poll_exits(price_fn)
    except Exception as e:
        logger.warning("momentum poll_exits failed: %s", e)
        return []


def _poll_exits(price_fn):
    with _ledger_lock():
        snapshot = {m: p for m, p in _read()["positions"].items()}
    if not snapshot:
        return []
    quotes = {}
    liqs = {}
    for mint, pos in snapshot.items():
        try:
            if price_fn:
                px = float(price_fn(pos["chain"], mint,
                                    pos.get("spot_index")) or 0)
            else:
                px = quote_price(pos["chain"], mint)
                try:
                    st = _fresh_tape(pos["chain"], mint)
                    if st and float(st.get("usd_liquidity") or 0) > 0:
                        liqs[mint] = float(st["usd_liquidity"])
                except Exception:
                    pass
        except Exception:
            px = 0.0
        # F2 cross-probe (2026-10-23): aggregator dark on an ARMED
        # Solana position — the direct pool reserves get their say
        # before anyone may call it unquotable. A live reserve
        # price makes the position quotable and is evaluated
        # normally; both dark is what phase 3's F2 check requires.
        if px <= 0 and not price_fn and _f2 is not None \
                and _reserves is not None \
                and (pos.get("chain") or "solana") == "solana" \
                and _f2.is_candidate(pos):
            try:
                rpx, _rsrc = _reserves.price_for_position(
                    dict(pos, mint=mint))
                if rpx > 0:
                    px = rpx
            except Exception:
                pass
        quotes[mint] = px
    with _ledger_lock():
        d = _read()
        closes = []
        now = time.time()
        for mint, pos in list(d["positions"].items()):
            try:
                px = float(quotes.get(mint, 0) or 0)
                if px <= 0:
                    # F2 (2026-10-23): an ARMED position dark across
                    # every source for a sustained stretch exits at
                    # its last real observed price, stamped — instead
                    # of waiting for the first post-rug dust print.
                    # Never-armed positions keep the exact pre-F2
                    # behavior below (incl. the 24h backstop).
                    _f2_stamp = _f2.check(pos, now) \
                        if _f2 is not None else None
                    if _f2_stamp is not None:
                        pos["_f2"] = _f2_stamp
                        _close_remaining(
                            pos, _f2_stamp["last_real_price"],
                            _f2.F2_REASON)
                        closes.append(_finalize(
                            d, mint, pos, _f2.F2_REASON))
                        continue
                    if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                        _close_remaining(pos, 0.0,
                                         "time_stop_24h_unquotable")
                        closes.append(_finalize(
                            d, mint, pos, "time_stop_24h_unquotable"))
                    continue
                entry = pos["entry_price"]
                mult = px / entry if entry > 0 else 0
                if "peak_price" not in pos:
                    pos["peak_price"] = 0.0
                    pos["peak_partial"] = True
                if px > float(pos.get("peak_price") or 0):
                    pos["peak_price"] = px
                    pos["peak_ts"] = now
                if mint in liqs:
                    pos["last_liquidity"] = liqs[mint]
                jq = pos.setdefault("quote_journal", [])
                jq.append({"ts": now, "px": px, "src": "poll"})
                del jq[:-40]
                # Hard stop first: -50% closes everything immediately.
                if mult <= HARD_STOP_MULT:
                    _close_remaining(pos, px, "hard_stop_-50pct")
                    closes.append(_finalize(d, mint, pos,
                                            "hard_stop_-50pct"))
                    continue
                # Time stop: 24h hygiene backstop.
                if now - pos["entry_ts"] > TIME_STOP_H * 3600:
                    _close_remaining(pos, px, "time_stop_24h")
                    closes.append(_finalize(d, mint, pos,
                                            "time_stop_24h"))
                    continue
                # Leg A: 50% at 2x.
                la = pos["legs"]["a"]
                if not la["done"] and mult >= LEG_A_TARGET:
                    _close_leg(pos, "a", px, "leg_a_2x")
                # Leg B: 25% at 3x.
                lb = pos["legs"]["b"]
                if not lb["done"] and mult >= LEG_B_TARGET:
                    _close_leg(pos, "b", px, "leg_b_3x")
                # Leg C: 25% on 30% trailing stop from peak.
                lc = pos["legs"]["c"]
                if not lc["done"]:
                    if px > lc["peak"]:
                        lc["peak"] = px
                    elif px <= lc["peak"] * (1 - LEG_C_TRAIL):
                        _close_leg(pos, "c", px, "leg_c_trail_30pct")
                if all(pos["legs"][k]["done"] for k in ("a", "b", "c")):
                    closes.append(_finalize(d, mint, pos,
                                            "ladder_complete"))
                else:
                    _write(d)
            except Exception as e:
                logger.warning("momentum poll_exits %s failed: %s",
                               mint, e)
                continue
        return closes


def open_positions():
    """{mint: position} currently open. Never raises."""
    try:
        return _load()["positions"]
    except Exception:
        return {}
