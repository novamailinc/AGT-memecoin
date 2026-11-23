#!/usr/bin/env python3
"""Decision memory for the paper-only grad lanes (2026-11-17).

The systematic version of the Rule A discovery: every paper entry
logs the features that mattered, every close settles that record
with its outcome, and a nightly reflector turns the settled
decisions into TEMPLATED lessons from the lane's own stats — no
LLM, no model prose, no RPC.

Lanes: grad_select, grad_raw, tranche, tranche_protect. Each lane
gets ``<HOME>/lessons/<lane>_lessons.jsonl`` (one JSON record per
line, created by this code, never shipped with data) and a
generated ``<lane>_lessons.json`` written by scripts/lessons-reflect.

Contract:
  * ADVISORY ONLY in v1. Lessons are context, never a filter:
    nothing in this module gates, rejects, or sizes an entry.
    The evaluation path may journal which lesson ids were in
    context (``lessons_in_context``); CEO auto-tune may read them
    via :func:`get_lessons`. Entry behaviour is unchanged.
  * Pure Python, stdlib only. Every public function never raises.
  * Idempotent settlement: a close settles its pending record
    once; re-running settles nothing twice, and a close with no
    pending record is recorded exactly once from the close record
    itself (``settle_from_close``), deduped by (mint, entry_ts).
  * Point-in-time seed: every generated lesson carries
    ``resolved_at`` — the close_ts of the newest settled decision
    it is based on. The stretch item (point-in-time lesson
    application) is NOT built here.
  * Home resolution: ``home`` passed explicitly wins; otherwise
    ``FOREX_AGENT_HOME`` at call time; otherwise
    ``~/.forex-agent``. Callers that run an engine instance pass
    ``os.path.dirname(LEDGER_PATH)`` so temp ledgers in tests stay
    isolated from the live runtime.
  * Writes are atomic (per-process tmp + os.replace) under a
    per-file flock, so a crash or reboot never leaves a torn
    jsonl behind.

Paper only. Nothing here touches real money, keys, or wallets.
"""

import fcntl
import json
import os
import time

LANES = ("grad_select", "grad_raw", "tranche", "tranche_protect")

LEDGER_FILES = {
    "grad_select": "grad_select_ledger.json",
    "grad_raw": "grad_raw_ledger.json",
    "tranche": "tranche_ledger.json",
    "tranche_protect": "tranche_protect_ledger.json",
}

FAST_AGE_S = 30.0  # Rule A bar; mirrors grad_select.ENTRY_MAX_AGE_S

# Band edges, shared by the reflector and the tests. TTG = time
# from coin creation to graduation (curve speed); curve_trades =
# bonding-curve signatures at graduation; copycat = OTHER
# same-ticker launches in the trailing 24h.
DEPLOYER_BANDS = ("0", "1-2", ">=3")
CURVE_TTG_BANDS = ("<=60s", "60-600s", ">600s")
CURVE_TRADES_BANDS = ("<=100", "101-250", ">250")
COPYCAT_BANDS = ("0", "1-2", ">=3")


def _home(home=None):
    if home:
        return str(home)
    return os.environ.get(
        "FOREX_AGENT_HOME",
        os.path.join(os.path.expanduser("~"), ".forex-agent"))


def lessons_dir(home=None):
    """<HOME>/lessons, created on demand. Never raises."""
    try:
        d = os.path.join(_home(home), "lessons")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return os.path.join(_home(home), "lessons")


def decisions_path(lane, home=None):
    return os.path.join(lessons_dir(home), "%s_lessons.jsonl" % lane)


def generated_path(lane, home=None):
    return os.path.join(lessons_dir(home), "%s_lessons.json" % lane)


def ledger_path(lane, home=None):
    return os.path.join(_home(home), LEDGER_FILES.get(lane, ""))


class _file_lock:
    """Exclusive flock on <path>.lock. Never raises."""

    def __init__(self, path):
        self._path = path + ".lock"
        self._fh = None

    def __enter__(self):
        try:
            self._fh = open(self._path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._fh = None
        return self

    def __exit__(self, *a):
        try:
            if self._fh is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        except Exception:
            pass
        return False


def _read_all(lane, home=None):
    """All decision records for a lane, in file order. [] if none."""
    try:
        p = decisions_path(lane, home)
        if not os.path.exists(p):
            return []
        out = []
        with open(p) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out
    except Exception:
        return []


def _write_all(lane, records, home=None):
    """Rewrite the lane's jsonl atomically (per-process tmp +
    os.replace). Never raises."""
    try:
        p = decisions_path(lane, home)
        tmp = "%s.tmp-%d" % (p, os.getpid())
        with open(tmp, "w") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def _num(v):
    """float(v) or None — never raises, never coerces junk."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract_features(entry_features, pos_or_close=None):
    """Flatten the features that mattered into one dict.

    ``entry_features`` is a lane close/position record's
    entry_features: the select measurements live under
    ``select`` (grad_select / grad_raw), ``tranche`` or
    ``tranche_protect``; the pool-age / mcap / liquidity live at
    the top level. Missing measurements stay None (unknown) —
    never a placeholder, never a 0 that would farm a band.
    """
    ef = entry_features if isinstance(entry_features, dict) else {}
    sel = None
    for key in ("select", "tranche", "tranche_protect"):
        if isinstance(ef.get(key), dict):
            sel = ef[key]
            break
    sel = sel or {}
    rec = pos_or_close if isinstance(pos_or_close, dict) else {}
    grad_age = _num(sel.get("grad_age_s"))
    if grad_age is None:
        grad_age = _num(ef.get("grad_age_s"))
    entry_price = _num(ef.get("filled_price"))
    if entry_price is None:
        entry_price = _num(ef.get("quoted_price"))
    if entry_price is None:
        entry_price = _num(rec.get("entry_price"))
    return {
        "grad_age_s": grad_age,
        "deployer_launches_24h": _num(
            sel.get("deployer_launches_24h")),
        "curve_trades": _num(sel.get("curve_trades")),
        "ttg_s": _num(sel.get("ttg_s")),
        "copycat_others_24h": _num(sel.get("ticker_others_24h")),
        "entry_price": entry_price,
        "mcap": _num(ef.get("mcap")),
        "liquidity": _num(ef.get("liquidity")),
    }


def _key(rec):
    return (str(rec.get("mint") or ""),
            _num(rec.get("entry_ts")))


def log_decision(lane, mint, entry_ts, features, symbol=None,
                 lessons_in_context=None, home=None):
    """Append a PENDING decision record at entry.

    Idempotent per (mint, entry_ts): a second call for the same
    entry MERGES features (fills measurements that were None —
    the entry path logs pool age/mcap/liq first, the detection
    stamp follows with the select measurements) instead of
    appending a duplicate. A settled record for the same key is
    never touched. Returns True when a record was written or
    merged, False otherwise. Never raises."""
    try:
        mint = str(mint or "")
        if not mint or lane not in LANES:
            return False
        feats = {k: _num(v) for k, v in
                 (features or {}).items()} if isinstance(features, dict) \
            else {}
        with _file_lock(decisions_path(lane, home)):
            recs = _read_all(lane, home)
            for rec in recs:
                if _key(rec) == (mint, _num(entry_ts)):
                    if rec.get("status") == "settled":
                        return False
                    merged = dict(rec.get("features") or {})
                    for k, v in feats.items():
                        if merged.get(k) is None and v is not None:
                            merged[k] = v
                    rec["features"] = merged
                    if symbol and not rec.get("symbol"):
                        rec["symbol"] = symbol
                    if lessons_in_context:
                        rec["lessons_in_context"] = list(
                            lessons_in_context)[:8]
                    _write_all(lane, recs, home)
                    return True
            recs.append({
                "kind": "decision", "lane": lane, "mint": mint,
                "symbol": symbol or "",
                "status": "pending",
                "entry_ts": _num(entry_ts),
                "features": feats,
                "lessons_in_context": list(
                    lessons_in_context or [])[:8],
                "logged_ts": time.time(),
            })
            _write_all(lane, recs, home)
            return True
    except Exception:
        return False


def settle_decision(lane, mint, entry_ts, pnl_usd, realized_mult,
                    close_reason, close_ts, home=None,
                    exit_price_kind=None):
    """Settle a PENDING record at close. Idempotent: a close
    settles its pending record once; a second call for the same
    (mint, entry_ts) is a no-op returning False. Returns True
    only when a record was settled by this call. Never raises.

    exit_price_kind is recorded with the settled decision (Stage-C
    visibility, 2026-11-22) and gates LEARNING only: generate_lessons
    excludes non-current-price outcomes from lesson statistics (4.6:
    never learn from incomplete trade records). It never gates an
    exit or changes a settlement amount."""
    try:
        mint = str(mint or "")
        if not mint or lane not in LANES:
            return False
        with _file_lock(decisions_path(lane, home)):
            recs = _read_all(lane, home)
            changed = False
            for rec in recs:
                if _key(rec) == (mint, _num(entry_ts)):
                    if rec.get("status") == "settled":
                        return False
                    rec["status"] = "settled"
                    rec["pnl_usd"] = _num(pnl_usd)
                    rec["realized_mult"] = _num(realized_mult)
                    rec["close_reason"] = str(close_reason or "")
                    rec["close_ts"] = _num(close_ts)
                    rec["settled_ts"] = time.time()
                    rec["exit_price_kind"] = str(
                        exit_price_kind or "unknown")
                    changed = True
                    break
            if changed:
                _write_all(lane, recs, home)
            return changed
    except Exception:
        return False


def settle_from_close(lane, close_rec, home=None):
    """Settle the decision for one ledger close record.

    Settles the pending record when one exists; when the entry
    predates decision logging (or the stamp path missed it),
    appends a settled record built from the close record's own
    entry_features instead — so every close lands in the log
    exactly once, deduped by (mint, entry_ts). Returns True when
    a record was settled or created, False when it was already
    settled. Never raises."""
    try:
        rec = close_rec if isinstance(close_rec, dict) else {}
        mint = str(rec.get("mint") or "")
        if not mint or lane not in LANES:
            return False
        if settle_decision(
                lane, mint, rec.get("entry_ts"), rec.get("pnl_usd"),
                rec.get("realized_mult"), rec.get("closed_reason"),
                rec.get("close_ts"), home=home,
                exit_price_kind=rec.get("exit_price_kind")):
            return True
        with _file_lock(decisions_path(lane, home)):
            recs = _read_all(lane, home)
            for r in recs:
                if _key(r) == _key(rec) and \
                        r.get("status") == "settled":
                    return False
            recs.append({
                "kind": "decision", "lane": lane, "mint": mint,
                "symbol": str(rec.get("symbol") or ""),
                "status": "settled",
                "entry_ts": _num(rec.get("entry_ts")),
                "features": extract_features(
                    rec.get("entry_features") or {}, rec),
                "lessons_in_context": [],
                "logged_ts": time.time(),
                "pnl_usd": _num(rec.get("pnl_usd")),
                "realized_mult": _num(rec.get("realized_mult")),
                "close_reason": str(rec.get("closed_reason") or ""),
                "close_ts": _num(rec.get("close_ts")),
                "settled_ts": time.time(),
                "exit_price_kind": str(
                    rec.get("exit_price_kind") or "unknown"),
                "source": "close",
            })
            _write_all(lane, recs, home)
            return True
    except Exception:
        return False


def load_decisions(lane, home=None):
    """All decision records for a lane (pending + settled)."""
    try:
        if lane not in LANES:
            return []
        return _read_all(lane, home)
    except Exception:
        return []


def decisions_from_ledger(lane, ledger_file=None, home=None):
    """Settled decisions rebuilt from a lane ledger's closes.

    The ledgers keep only the last 500 closes, so this is a
    bounded backfill, not a full history — and it is how the
    reflector produces lessons on real data from day one, before
    the pending log has accumulated settled records of its own.
    Never raises."""
    try:
        if lane not in LANES:
            return []
        path = ledger_file or ledger_path(lane, home)
        if not path or not os.path.exists(path):
            return []
        with open(path) as f:
            d = json.load(f)
        out = []
        for c in (d.get("closes") or []):
            if not isinstance(c, dict) or not c.get("mint"):
                continue
            out.append({
                "kind": "decision", "lane": lane,
                "mint": str(c.get("mint") or ""),
                "symbol": str(c.get("symbol") or ""),
                "status": "settled",
                "entry_ts": _num(c.get("entry_ts")),
                "features": extract_features(
                    c.get("entry_features") or {}, c),
                "lessons_in_context": [],
                "pnl_usd": _num(c.get("pnl_usd")),
                "realized_mult": _num(c.get("realized_mult")),
                "close_reason": str(c.get("closed_reason") or ""),
                "close_ts": _num(c.get("close_ts")),
                "source": "ledger",
            })
        return out
    except Exception:
        return []


def merged_settled_decisions(lane, ledger_file=None, home=None):
    """Settled decisions from the jsonl log + the ledger, deduped
    by (mint, entry_ts). The jsonl record wins a tie (it may carry
    lessons_in_context the ledger lacks). Sorted by close_ts.
    Never raises."""
    try:
        merged = {}
        for rec in decisions_from_ledger(lane, ledger_file, home):
            merged[_key(rec)] = rec
        for rec in _read_all(lane, home):
            if rec.get("status") == "settled":
                merged[_key(rec)] = rec
        out = [r for r in merged.values()
               if _num(r.get("close_ts")) is not None]
        out.sort(key=lambda r: _num(r.get("close_ts")) or 0)
        return out
    except Exception:
        return []


# ---- templated lesson generation --------------------------------------

def _band(value, edges, labels):
    """Band label for a numeric value, or 'unknown' when missing."""
    if value is None:
        return "unknown"
    for edge, label in zip(edges, labels):
        if value <= edge:
            return label
    return labels[-1]


def _band_of(dimension, features):
    if dimension == "entry_speed":
        age = features.get("grad_age_s")
        if age is None:
            return "unknown"
        return "fast (age<=30s)" if age <= FAST_AGE_S \
            else "slow (age>30s)"
    if dimension == "deployer":
        n = features.get("deployer_launches_24h")
        if n is None:
            return "unknown"
        return "0" if n == 0 else ("1-2" if n <= 2 else ">=3")
    if dimension == "curve_speed":
        return _band(features.get("ttg_s"),
                     (60.0, 600.0), ("<=60s", "60-600s", ">600s"))
    if dimension == "curve_trades":
        return _band(features.get("curve_trades"),
                     (100.0, 250.0), ("<=100", "101-250", ">250"))
    if dimension == "copycat":
        n = features.get("copycat_others_24h")
        if n is None:
            return "unknown"
        return "0" if n == 0 else ("1-2" if n <= 2 else ">=3")
    return "unknown"


def _fmt_money(v):
    return "%s$%.2f" % ("+" if v >= 0 else "-", abs(v))


def _lesson_text(dimension, band, closes, wins, net_pnl):
    wr = (100.0 * wins / closes) if closes else 0.0
    if dimension == "entry_speed":
        return ("%s entries: %s on %d closes, %d wins "
                "(%.1f%% win rate)" % (band, _fmt_money(net_pnl),
                                      closes, wins, wr))
    if dimension == "deployer":
        return ("deployer with %s launches in 24h: %s on %d closes, "
                "%d wins (%.1f%% win rate)"
                % (band, _fmt_money(net_pnl), closes, wins, wr))
    if dimension == "curve_speed":
        return ("curve time-to-graduation %s: %s on %d closes, "
                "%d wins (%.1f%% win rate)"
                % (band, _fmt_money(net_pnl), closes, wins, wr))
    if dimension == "curve_trades":
        return ("curve with %s signatures: %s on %d closes, %d wins "
                "(%.1f%% win rate)"
                % (band, _fmt_money(net_pnl), closes, wins, wr))
    if dimension == "copycat":
        return ("copycat count %s other same-ticker launches in 24h: "
                "%s on %d closes, %d wins (%.1f%% win rate)"
                % (band, _fmt_money(net_pnl), closes, wins, wr))
    if dimension == "overall":
        return ("all settled decisions: %s on %d closes, %d wins "
                "(%.1f%% win rate)" % (_fmt_money(net_pnl), closes,
                                      wins, wr))
    return "%s %s: %s on %d closes, %d wins" % (
        dimension, band, _fmt_money(net_pnl), closes, wins)


def _qualified_for_learning(rec):
    """4.6 hygiene: a settled decision feeds lesson statistics only
    when its outcome was priced from a current observed quote
    (exit_provenance.CURRENT). Stale estimates, zero write-downs and
    unclassified/legacy records are incomplete trade records and are
    EXCLUDED from learning; their count is reported, never learned
    from. Anything unreadable or unlabeled fails closed (excluded)."""
    try:
        return str(rec.get("exit_price_kind") or "") == \
            "current_price_simulated_fill"
    except Exception:
        return False


def _summarize(rows):
    closes = len(rows)
    wins = sum(1 for r in rows
               if (_num(r.get("pnl_usd")) or 0) > 0)
    net = round(sum(_num(r.get("pnl_usd")) or 0 for r in rows), 2)
    resolved = max((_num(r.get("close_ts")) or 0 for r in rows),
                   default=0) or None
    return closes, wins, net, resolved


def generate_lessons(lane, decisions, generated_at=None):
    """Templated lessons from settled decisions.

    One lesson per non-empty band of each dimension (entry_speed,
    deployer, curve_speed, curve_trades, copycat) plus one overall
    lesson. Text is templated from the numbers only — no LLM, no
    model prose. Every lesson carries ``resolved_at``: the
    close_ts of the newest settled decision it is based on.
    4.6 hygiene: lesson statistics learn ONLY from outcomes priced
    from a current observed quote (exit_price_kind
    "current_price_simulated_fill"); stale estimates, write-downs and
    unclassified records are incomplete trade records and are
    excluded from the numbers, with their count reported per lesson
    and per payload. Returns a payload dict ready for
    :func:`write_lessons`. Never raises."""
    try:
        settled = [r for r in (decisions or [])
                   if isinstance(r, dict)
                   and r.get("status") == "settled"
                   and _num(r.get("close_ts")) is not None]
        gen = generated_at or time.time()
        qualified = [r for r in settled if _qualified_for_learning(r)]
        excluded = [r for r in settled if not _qualified_for_learning(r)]
        lessons = []
        dims = ("entry_speed", "deployer", "curve_speed",
                "curve_trades", "copycat")
        for dim in dims:
            groups = {}
            for r in qualified:
                band = _band_of(dim, r.get("features") or {})
                groups.setdefault(band, []).append(r)
            xgroups = {}
            for r in excluded:
                band = _band_of(dim, r.get("features") or {})
                xgroups.setdefault(band, []).append(r)
            for band in sorted(groups):
                rows = groups[band]
                closes, wins, net, resolved = _summarize(rows)
                lessons.append({
                    "id": "%s:%s:%s" % (lane, dim, band),
                    "lane": lane, "dimension": dim, "band": band,
                    "text": _lesson_text(dim, band, closes, wins,
                                         net),
                    "closes": closes, "wins": wins,
                    "net_pnl_usd": net,
                    "win_rate": round(
                        wins / closes, 4) if closes else 0.0,
                    "excluded_unqualified": len(xgroups.get(band, [])),
                    "resolved_at": resolved,
                    "generated_at": gen,
                })
        closes, wins, net, resolved = _summarize(qualified)
        if closes:
            lessons.append({
                "id": "%s:overall:all" % lane,
                "lane": lane, "dimension": "overall", "band": "all",
                "text": _lesson_text("overall", "all", closes, wins,
                                     net),
                "closes": closes, "wins": wins, "net_pnl_usd": net,
                "win_rate": round(wins / closes, 4) if closes else 0.0,
                "excluded_unqualified": len(excluded),
                "resolved_at": resolved, "generated_at": gen,
            })
        # Stage-C visibility (owner-approved 2026-11-22): the
        # payload states the provenance of the outcomes the lessons
        # were templated from, and that the numbers are simulated
        # and unvalidated - never qualified performance.
        try:
            try:
                from daemon.launch_scanner import \
                    exit_provenance as _pk
            except ImportError:
                import exit_provenance as _pk  # type: ignore
            _bd = _pk.sample_breakdown(settled)
            _vis = {
                "price_kind_breakdown": _bd,
                "qualification_status": _pk.QUALIFICATION_STATUS,
                "fill_model_validated": _pk.FILL_MODEL_VALIDATED,
            }
        except Exception:
            _vis = {}
        return {
            "lane": lane, "generated_at": gen,
            "resolved_at": resolved if closes else None,
            "n_settled": len(settled),
            "n_qualified": len(qualified),
            "n_excluded_unqualified": len(excluded),
            "learning_price_kind": "current_price_simulated_fill",
            "lessons": lessons,
            "visibility": _vis,
        }
    except Exception:
        return {"lane": lane, "generated_at": generated_at or 0,
                "resolved_at": None, "n_settled": 0, "lessons": []}


def write_lessons(lane, payload, home=None):
    """Write the generated lessons file atomically. Never raises."""
    try:
        if lane not in LANES:
            return False
        p = generated_path(lane, home)
        tmp = "%s.tmp-%d" % (p, os.getpid())
        with open(tmp, "w") as f:
            f.write(json.dumps(payload, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def read_lessons(lane, home=None):
    """The generated lessons payload, or None when absent."""
    try:
        if lane not in LANES:
            return None
        p = generated_path(lane, home)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _rank(lessons):
    """Most material first: |net pnl|, then closes, then id."""
    return sorted(lessons or [],
                  key=lambda l: (-abs(_num(l.get("net_pnl_usd")) or 0),
                                 -int(l.get("closes") or 0),
                                 str(l.get("id") or "")))


def get_lessons(lane, n=5, home=None):
    """Top ``n`` lessons for a lane, most material first.

    Advisory context only — callers must never gate on this.
    [] when no generated file exists yet. Never raises."""
    try:
        payload = read_lessons(lane, home)
        if not payload:
            return []
        return _rank(payload.get("lessons") or [])[:max(0, int(n))]
    except Exception:
        return []


def lessons_in_context(lane, n=5, home=None):
    """Lesson ids currently in context for a lane (for journaling
    at decision time). Never raises."""
    try:
        return [str(l.get("id") or "") for l in
                get_lessons(lane, n=n, home=home) if l.get("id")]
    except Exception:
        return []


def context_block(lane, n=5, home=None):
    """Human-readable context block for a lane (journal / CEO
    read). Advisory only. Never raises."""
    try:
        top = get_lessons(lane, n=n, home=home)
        if not top:
            return ("Decision-memory lessons for %s: none yet "
                    "(no settled decisions reflected)." % lane)
        lines = ["Decision-memory lessons for %s (advisory only — "
                 "never gate entries):" % lane]
        for l in top:
            lines.append("- %s" % l.get("text", ""))
        return "\n".join(lines)
    except Exception:
        return "Decision-memory lessons for %s: unavailable." % lane
