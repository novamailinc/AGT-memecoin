"""Read-only status dashboard for the AGT deployment.

Phase 13 (AGT packaging skeleton). A small stdlib-only HTTP server
that serves verified live state on the deployment port (AGT publishes
the port over the tunnel; the AGT supervisor health-checks it with a
TCP connect).

Rules:
  * READ-ONLY. GET only; any other method gets 405. No endpoint can
    trade, halt, resume, or change state. No auth of its own — the
    bind is 127.0.0.1 and AGT's tunnel is the access control; do not
    bind 0.0.0.0.
  * Every field is read from a verified live state file on each
    request (no caching, no invented values). Each section reports
    its source file and one of three statuses:
      ok            — read and parsed from live state.
      unavailable   — the source exists by design but is missing or
                      unreadable right now (file absent, corrupt, DB
                      locked); the reason is stated, the field is
                      empty, never estimated.
      unimplemented — no source exists for this field yet (skeleton
                      gap, listed for the finalize pass); the reason
                      is stated, the field is empty, never faked.
  * Paper only. Mainnet is disabled; the dashboard says so.
  * No credential material is ever read or served. State files that
    could contain secrets are never opened (helius/, telegram/,
    secrets.env are not dashboard sources).
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

try:
    import voided_closes as _VOID
except ImportError:  # pragma: no cover - AGT dir is always on the path
    _VOID = None

LAGOS = timezone(timedelta(hours=1))  # Africa/Lagos: UTC+1, no DST
DEFAULT_PORT = int(os.environ.get("AGT_STATUS_PORT", "8765"))
PAPER_STAKE_USD = 25.0  # standing paper stake per position (grad lanes)

LANES = ("grad_select", "grad_raw", "tranche", "tranche_protect",
         "momentum")
LIVE10_LANES = ("grad_select", "tranche", "tranche_protect", "grad_raw")
SIGNAL_EVENTS = ("launch.detected", "degen.warning",
                 "launch.early_digest", "launch.lowcap_runner",
                 "momentum.alert", "launch.second_wave",
                 "launch.milestone")


def home_dir():
    return os.environ.get("FOREX_AGENT_HOME") or os.path.join(
        os.path.expanduser("~"), ".forex-agent")


def _read_json(path):
    """(data, error). error None on success."""
    try:
        with open(path) as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "file absent: %s" % path
    except (OSError, ValueError) as exc:
        return None, "unreadable %s: %s" % (path, exc)


def _section(status, source, data=None, reason=None):
    out = {"status": status, "source": source}
    if reason:
        out["reason"] = reason
    if data is not None:
        out["data"] = data
    return out


def _lagos_midnight_epoch(now=None):
    now = now or datetime.now(LAGOS)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ---------------------------------------------------------------------------
# State readers — one per dashboard section. Each returns a section dict.
# ---------------------------------------------------------------------------

def read_supervisor(home):
    path = os.path.join(home, "supervisor_state.json")
    data, err = _read_json(path)
    if err:
        return _section("unavailable", path, reason=err)
    daemons = {}
    for name, d in (data.get("daemons") or {}).items():
        daemons[name] = {"state": d.get("state"), "pid": d.get("pid"),
                         "owned": d.get("owned"),
                         "restarts": d.get("restarts"),
                         "uptime_s": d.get("uptime_s")}
    return _section("ok", path, {
        "supervisor_pid": data.get("pid"),
        "state_ts": data.get("ts"),
        "state_age_s": round(time.time() - (data.get("ts") or 0), 1)
        if data.get("ts") else None,
        "daemons": daemons,
        "running": sum(1 for d in daemons.values()
                       if d["state"] in ("running", "adopted")),
        "total": len(daemons)})


def read_heartbeat(home):
    path = os.path.join(home, "run", "heartbeat.json")
    data, err = _read_json(path)
    if err:
        return _section("unavailable", path, reason=err)
    beats = data.get("beats") or {}
    newest = max((b.get("ts") or 0 for b in beats.values()), default=0)
    return _section("ok", path, {
        "checked_at": data.get("checked_at"),
        "actions": data.get("actions"),
        "daemons": data.get("daemons"),
        "beats_tracked": len(beats),
        "newest_beat_age_s": round(time.time() - newest, 1)
        if newest else None})


def _lane_ledger(home, lane):
    path = os.path.join(home, "%s_ledger.json" % lane)
    return path, _read_json(path)


def _void_index():
    """(index, entries, error) for the void list. A load error is
    returned, never swallowed: a silently dropped void list would
    re-inflate the totals."""
    if _VOID is None:
        return {}, [], "voided_closes module unavailable"
    entries, err = _VOID.load_voided()
    return _VOID.void_index(entries), entries, err


def read_balances(home):
    path = os.path.join(home, "live10_demo_ledger.json")
    data, err = _read_json(path)
    if err:
        return _section("unavailable", path, reason=err)
    index, entries, void_err = _void_index()
    # A funded void is subtracted ONLY when the voided close is
    # actually present in that lane's stored closes (see
    # voided_closes.funded_void_for_lane): the void list is global,
    # but a home whose ledgers never held the phantom must not be
    # adjusted for it. Lanes whose ledger cannot be read get no
    # adjustment, and the unmatched voids are listed.
    lane_closes = {}
    for _lane in LIVE10_LANES:
        _lpath = os.path.join(home, "%s_ledger.json" % _lane)
        _ldata, _lerr = _read_json(_lpath)
        lane_closes[_lane] = (
            _ldata.get("closes") or []
            if not _lerr and isinstance(_ldata, dict) else None)
    unmatched = []
    if _VOID is not None:
        for entry in entries:
            closes = lane_closes.get(entry.get("lane"))
            if closes is None:
                unmatched.append({
                    "mint": entry.get("mint"),
                    "lane": entry.get("lane"),
                    "reason": "lane ledger unreadable — no "
                              "adjustment applied"})
            elif not _VOID.entry_has_close(entry, closes):
                unmatched.append({
                    "mint": entry.get("mint"),
                    "lane": entry.get("lane"),
                    "reason": "no matching close in the lane "
                              "ledger — no adjustment applied"})
    lanes = {}
    for lane in LIVE10_LANES:
        entry = (data.get("lanes") or {}).get(lane)
        if entry is None:
            continue
        # The Live10 ledger carries balances, not per-close
        # records, so a funded phantom is removed via the void
        # entry's recorded funded amount (paper pnl x the Live10
        # scale in force at the time; 0.0 when unfunded). The raw
        # ledger value is kept alongside, labelled.
        funded_void, void_count = _VOID.funded_void_for_lane(
            entries, lane, lane_closes.get(lane)) \
            if _VOID is not None else (0.0, 0)
        balance_raw = entry.get("balance")
        net_raw = entry.get("net")
        lanes[lane] = {"start": entry.get("start"),
                       "balance": (round(balance_raw - funded_void, 4)
                                   if balance_raw is not None else None),
                       "balance_raw": balance_raw,
                       "voided_funded_usd": funded_void,
                       "voided_funded_closes": void_count,
                       "closes": (entry.get("closes") - void_count
                                  if entry.get("closes") is not None
                                  else None),
                       "closes_raw": entry.get("closes"),
                       # A funded void was counted as a funded win;
                       # an unfunded void never entered these counts
                       # (void_count is 0 for unfunded lanes).
                       "wins": (entry.get("wins") - void_count
                                if entry.get("wins") is not None
                                else None),
                       "wins_raw": entry.get("wins"),
                       "net": (round(net_raw - funded_void, 4)
                               if net_raw is not None else None),
                       "net_raw": net_raw,
                       "funded_closes": (
                           entry.get("funded_closes") - void_count
                           if entry.get("funded_closes") is not None
                           else None),
                       "funded_closes_raw": entry.get("funded_closes")}
    ceo_path = os.path.join(home, "ceo_ledger.json")
    ceo, ceo_err = _read_json(ceo_path)
    ceo_data = None
    if not ceo_err and isinstance(ceo, dict):
        ceo_data = {"bankroll": ceo.get("bankroll"),
                    "bankroll_start": ceo.get("bankroll_start"),
                    "source": ceo_path}
    raw_total = round(sum(
        (v.get("balance_raw") or 0) for v in lanes.values()), 4)
    total = round(sum(
        (v.get("balance") or 0) for v in lanes.values()), 4)
    return _section("ok", path, {
        # Headline total is void-excluded (the honest number). The
        # raw ledger total is shown alongside, labelled, so the
        # exclusion is auditable — never silently rewritten.
        "live10_total": total,
        "live10_total_raw": raw_total,
        "live10_total_basis": "void-excluded: raw Live10 ledger "
                              "total minus funded voided closes "
                              "(see voided_closes section)",
        "voided_funded_usd_total": round(raw_total - total, 4),
        "void_list_error": void_err,
        "voided_unmatched": unmatched or None,
        "live10_lanes": lanes,
        "ceo_account": ceo_data,
        "ceo_unavailable_reason": ceo_err})


def read_positions(home):
    out = {}
    missing = []
    for lane in LANES:
        path, (data, err) = _lane_ledger(home, lane)
        if err:
            missing.append("%s: %s" % (lane, err))
            continue
        positions = data.get("positions") or {}
        items = []
        for mint, pos in positions.items():
            if isinstance(pos, dict):
                items.append({"mint": mint,
                              "entry_price": pos.get("entry_price"),
                              "stake_usd": pos.get("stake_usd"),
                              "opened_ts": pos.get("opened_ts")
                              or pos.get("entry_ts"),
                              "symbol": pos.get("symbol")})
            else:
                items.append({"mint": mint, "raw": str(pos)[:200]})
        out[lane] = {"open": len(items), "positions": items}
    total_open = sum(v["open"] for v in out.values())
    # Exposure is arithmetic on real state, with its basis stated:
    # open positions x the standing $25 paper stake. Positions that
    # carry their own stake are summed exactly instead when present.
    at_risk = 0.0
    for lane, v in out.items():
        for pos in v["positions"]:
            stake = pos.get("stake_usd")
            at_risk += float(stake) if stake else PAPER_STAKE_USD
    status = "ok" if not missing else "unavailable"
    return _section(status, "per-lane <lane>_ledger.json", {
        "lanes": out,
        "total_open": total_open,
        "capital_at_risk_usd": round(at_risk, 2),
        "capital_at_risk_basis": "open positions x standing $25 paper "
                                 "stake, except positions carrying "
                                 "their own stake_usd",
        "missing_ledgers": missing or None},
        reason="; ".join(missing) if missing else None)


def read_recent_trades(home, limit=10):
    closes = []
    voided = []
    missing = []
    index, _entries, void_err = _void_index()
    for lane in LANES:
        path, (data, err) = _lane_ledger(home, lane)
        if err:
            missing.append("%s: %s" % (lane, err))
            continue
        for c in (data.get("closes") or []):
            item = {
                "lane": lane,
                "mint": c.get("mint"), "symbol": c.get("symbol"),
                "close_ts": c.get("close_ts"),
                "pnl_usd": c.get("pnl_usd"),
                "realized_mult": c.get("realized_mult"),
                "closed_reason": c.get("closed_reason")}
            entry = _VOID.find_void(index, lane, c.get("mint"),
                                    c.get("close_ts")) \
                if _VOID is not None else None
            if entry is not None:
                item["voided"] = True
                item["void_reason"] = entry.get("reason")
                voided.append(item)
            else:
                closes.append(item)
    closes.sort(key=lambda c: c.get("close_ts") or 0, reverse=True)
    voided.sort(key=lambda c: c.get("close_ts") or 0, reverse=True)
    return _section("ok" if not missing else "unavailable",
                    "per-lane <lane>_ledger.json", {
                        "trades": closes[:limit],
                        "voided_closes": voided,
                        "void_list_error": void_err,
                        "missing_ledgers": missing or None},
                    reason="; ".join(missing) if missing else None)


def read_pnl(home):
    today_start = _lagos_midnight_epoch()
    per_lane = {}
    missing = []
    index, _entries, void_err = _void_index()
    for lane in LANES:
        path, (data, err) = _lane_ledger(home, lane)
        if err:
            missing.append("%s: %s" % (lane, err))
            continue
        closes = data.get("closes") or []
        # Voided closes are excluded from every sum/count below;
        # their PnL is reported separately, labelled, never silently
        # dropped.
        kept, voided = _VOID.filter_closes(index, lane, closes) \
            if _VOID is not None else (closes, [])
        realized = sum((c.get("pnl_usd") or 0) for c in kept)
        realized_raw = sum((c.get("pnl_usd") or 0) for c in closes)
        wins = sum(1 for c in kept if (c.get("pnl_usd") or 0) > 0)
        today = [c for c in kept
                 if (c.get("close_ts") or 0) >= today_start]
        per_lane[lane] = {
            "realized_usd_all_time": round(realized, 2),
            "realized_usd_all_time_raw": round(realized_raw, 2),
            "voided_usd": round(realized_raw - realized, 2),
            "voided_closes": len(voided),
            "closes_all_time": len(kept),
            "closes_all_time_raw": len(closes),
            "wins_all_time": wins,
            "realized_usd_today_lagos": round(
                sum((c.get("pnl_usd") or 0) for c in today), 2),
            "closes_today_lagos": len(today),
            "bankroll": data.get("bankroll"),
            "bankroll_start": data.get("bankroll_start")}
    total = round(sum(v["realized_usd_all_time"]
                      for v in per_lane.values()), 2)
    total_raw = round(sum(v["realized_usd_all_time_raw"]
                          for v in per_lane.values()), 2)
    return _section("ok" if not missing else "unavailable",
                    "per-lane <lane>_ledger.json", {
                        "per_lane": per_lane,
                        "total_realized_usd_all_time": total,
                        "total_realized_usd_all_time_raw": total_raw,
                        "voided_usd_total": round(total_raw - total, 2),
                        "void_list_error": void_err,
                        "unrealized_usd": None,
                        "missing_ledgers": missing or None},
                    reason=("unrealized PnL is unimplemented: no live "
                            "mark source exists for open paper "
                            "positions; it is reported as null, never "
                            "estimated." +
                            (" Missing ledgers: " + "; ".join(missing)
                             if missing else "")))


def read_killswitch(home):
    path = os.path.join(home, "killswitch", "state.json")
    data, err = _read_json(path)
    if err:
        return _section("unavailable", path, reason=err)
    _index, entries, void_err = _void_index()
    lanes = {}
    for lane, v in (data.get("lanes") or {}).items():
        trades = v.get("trades") or []
        # Voided closes are excluded from the rolling window, read
        # at read time: window trades carry no mint, only (ts,
        # pnl), so the match is (lane, pnl to the cent, ts within
        # 5s of the voided close_ts) — see
        # voided_closes.find_void_by_pnl_ts. The raw window is
        # kept alongside, labelled. A phantom in the window
        # inflates the rolling net by ~$13k and would mask a
        # bleeding lane, so excluding it is a safety matter, not
        # cosmetics.
        kept = []
        voided_count = 0
        for t in trades:
            match = _VOID.find_void_by_pnl_ts(
                entries, lane, t.get("pnl"), t.get("ts")) \
                if _VOID is not None else None
            if match is None:
                kept.append(t)
            else:
                voided_count += 1
        net = sum((t.get("pnl") or 0) for t in kept)
        net_raw = sum((t.get("pnl") or 0) for t in trades)
        lanes[lane] = {
            "halted": bool(v.get("halted")),
            "halt_reason": v.get("halt_reason") or "",
            "halt_ts": v.get("halt_ts"),
            "window_trades": len(kept),
            "window_trades_raw": len(trades),
            "window_net_usd": round(net, 2),
            "window_net_usd_raw": round(net_raw, 2),
            "voided_trades": voided_count,
            "window_wins": sum(1 for t in kept
                               if (t.get("pnl") or 0) > 0)}
    events = (data.get("events") or [])[-10:]
    return _section("ok", path, {
        "lanes": lanes,
        "halted_lanes": [k for k, v in lanes.items()
                         if v["halted"]],
        "void_list_error": void_err,
        "recent_events": events})


def read_risk(home):
    path = os.path.join(home, "helius_429_alert.json")
    alert, alert_err = _read_json(path)
    review = {}
    review_dir = os.path.join(home, "review_queue")
    try:
        for name in sorted(os.listdir(review_dir)):
            if not name.endswith(".jsonl"):
                continue
            lane = name.replace("_review.jsonl", "")
            with open(os.path.join(review_dir, name),
                      errors="replace") as fh:
                review[lane] = sum(1 for _ in fh)
    except OSError as exc:
        review = {"error": "review_queue unreadable: %s" % exc}
    return _section("ok", path, {
        "helius_429_alert": alert if not alert_err else None,
        "helius_429_alert_error": alert_err,
        "review_queue_counts": review})


def read_discovery(home):
    db = os.path.join(home, "storage", "local.db")
    if not os.path.exists(db):
        return _section("unavailable", db,
                        reason="database file absent: %s" % db)
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True,
                               timeout=5)
        try:
            since = (datetime.now(timezone.utc)
                     - timedelta(hours=24)).isoformat()
            counts = {}
            for event in SIGNAL_EVENTS:
                row = conn.execute(
                    "SELECT COUNT(*) FROM event_journal "
                    "WHERE event = ? AND ts >= ?",
                    (event, since)).fetchone()
                counts[event] = row[0] if row else 0
            latest = conn.execute(
                "SELECT event, ts FROM event_journal "
                "ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _section("unavailable", db,
                        reason="event_journal unreadable: %s" % exc)
    return _section("ok", db, {
        "signal_counts_last_24h": counts,
        "latest_event": {"event": latest[0], "ts": latest[1]}
        if latest else None,
        "pumpportal_stream": None},
        reason="PumpPortal stream connectivity is unimplemented: no "
               "state file records stream state (finalize pass).")


def read_provider_health(home):
    data = {}
    missing = []
    for name in ("helius_pacer.json", "helius_guard.json",
                 "helius_429_alert.json"):
        path = os.path.join(home, name)
        content, err = _read_json(path)
        if err:
            missing.append("%s: %s" % (name, err))
        else:
            data[name] = content
    return _section("ok" if data else "unavailable",
                    "helius_pacer.json, helius_guard.json, "
                    "helius_429_alert.json",
                    {"helius": data,
                     "router": None,
                     "missing_files": missing or None},
                    reason=("Router health is unimplemented: the "
                            "acquisition router writes no health state "
                            "file yet (Phases 5-12). " +
                            ("; ".join(missing) if missing else "")))


def read_execution_failures(home):
    sup = read_supervisor(home)
    restarts = {}
    if sup["status"] == "ok":
        restarts = {name: d.get("restarts")
                    for name, d in
                    sup["data"]["daemons"].items()
                    if (d.get("restarts") or 0) > 0}
    alert = read_risk(home)
    spike = None
    if alert["status"] == "ok":
        spike = (alert["data"] or {}).get("helius_429_alert")
    return _section("ok", "supervisor_state.json + "
                    "helius_429_alert.json", {
                        "daemon_restarts": restarts,
                        "helius_429_spike": spike,
                        "live_execution_failures": None},
                    reason="Live/mainnet execution failures are "
                           "unimplemented: paper-only, no mainnet "
                           "execution runs on this deployment.")


def read_scheduler(home):
    path = os.path.join(home, "scheduler_state.json")
    data, err = _read_json(path)
    if err:
        return _section("unavailable", path, reason=err)
    return _section("ok", path, {"jobs": data})


def read_versions(home):
    """Deployment version: written by start.sh preflight into
    $FOREX_AGENT_HOME/agt_version.json (git SHA when the checkout
    has one, else null with the reason). Strategy/model versions
    still have no registry, so those fields stay None — the
    deployment commit is the only version this section reports."""
    path = os.path.join(home, "agt_version.json")
    data, err = _read_json(path)
    if err:
        return _section(
            "unimplemented", path,
            {"commit": None, "strategies": None, "models": None},
            reason="No version file yet (start.sh writes "
                   "agt_version.json at preflight; %s). No "
                   "strategy/model version registry exists in the "
                   "repo yet; nothing is reported rather than a "
                   "guessed version." % err)
    return _section("ok", path, {
        "commit": data.get("commit"),
        "commit_source": data.get("commit_source"),
        "branch": data.get("branch"),
        "generated_at_utc": data.get("generated_at_utc"),
        "strategies": None,
        "models": None},
        reason="Strategy/model versions are unimplemented: no "
               "version registry exists in the repo yet; the "
               "deployment commit is the only version reported.")


def read_voided_closes(home=None):
    """The void list itself, as a dashboard section: which closes
    are excluded from the totals, and why. `home` is unused (the
    list is checked in with the deploy, not state) but kept for
    the uniform reader signature."""
    entries, err = _VOID.load_voided() if _VOID is not None \
        else ([], "voided_closes module unavailable")
    if err:
        return _section("unavailable",
                        _VOID.DEFAULT_VOID_FILE if _VOID is not None
                        else "voided_closes.json",
                        reason=err)
    return _section("ok",
                    _VOID.DEFAULT_VOID_FILE, {
                        "count": len(entries),
                        "voided": [{
                            "mint": e.get("mint"),
                            "lane": e.get("lane"),
                            "close_ts": e.get("close_ts"),
                            "paper_pnl_usd": e.get("paper_pnl_usd"),
                            "funded": e.get("funded"),
                            "funded_pnl_usd": e.get("funded_pnl_usd"),
                            "reason": e.get("reason"),
                            "voided_at": e.get("voided_at"),
                        } for e in entries]})


def collect_status(home=None):
    home = home or home_dir()
    sup = read_supervisor(home)
    hb = read_heartbeat(home)
    ok = sup["status"] == "ok" and hb["status"] == "ok"
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "deployment": {
            "repo": "https://github.com/olamoreA/meme-project",
            "branch": "main",
            "mode": "paper",
            "mainnet_enabled": False,
        },
        "bot_status": sup,
        "heartbeat": hb,
        "balances": read_balances(home),
        "open_positions": read_positions(home),
        "recent_trades": read_recent_trades(home),
        "pnl": read_pnl(home),
        "killswitch": read_killswitch(home),
        "risk": read_risk(home),
        "discovery": read_discovery(home),
        "provider_health": read_provider_health(home),
        "execution_failures": read_execution_failures(home),
        "scheduler": read_scheduler(home),
        "versions": read_versions(home),
        "voided_closes": read_voided_closes(home),
        "_health": "ok" if ok else "degraded",
    }


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def _html(status):
    def row(name, sec):
        st = sec.get("status", "?")
        extra = ""
        if name == "balances" and sec.get("data"):
            extra = " — Live10 total $%s (raw ledger $%s, void-excluded)" % (
                sec["data"].get("live10_total"),
                sec["data"].get("live10_total_raw"))
        if name == "voided_closes" and sec.get("data"):
            extra = " — %s voided close(s) excluded from totals" % (
                sec["data"].get("count"))
        if name == "killswitch" and sec.get("data"):
            halted = sec["data"].get("halted_lanes") or []
            extra = " — halted: %s" % (", ".join(halted) or "none")
        if name == "open_positions" and sec.get("data"):
            extra = " — %s open" % sec["data"].get("total_open")
        return ("<tr><td>%s</td><td>%s</td><td>%s%s</td></tr>"
                % (name, st, sec.get("source", ""), extra))
    rows = "".join(row(k, v) for k, v in status.items()
                   if isinstance(v, dict) and "status" in v)
    return ("<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Paper bot — AGT status</title></head><body>"
            "<h1>Paper bot — AGT status (paper only, mainnet "
            "disabled)</h1>"
            "<p>Generated %s. Full JSON: <a href='/status'>/status</a> "
            "— <a href='/health'>/health</a></p>"
            "<table border='1'><tr><th>section</th><th>status</th>"
            "<th>source</th></tr>%s</table>"
            "<p>Sections marked unimplemented have no live source yet; "
            "they are empty on purpose, never estimated.</p>"
            "</body></html>"
            % (status.get("generated_at_utc", ""), rows))


class Handler(BaseHTTPRequestHandler):
    home = None

    def log_message(self, *args):  # keep stdout clean
        pass

    def _send(self, code, body, ctype):
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        home = self.home or home_dir()
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, _html(collect_status(home)), "text/html")
        elif path == "/health":
            status = collect_status(home)
            checks = {k: v.get("status")
                      for k, v in status.items()
                      if isinstance(v, dict) and "status" in v}
            self._send(200, json.dumps(
                {"status": status["_health"], "checks": checks}),
                "application/json")
        elif path == "/status":
            self._send(200, json.dumps(collect_status(home), indent=2,
                                       default=str),
                       "application/json")
        else:
            self._send(404, json.dumps({"error": "not found"}),
                       "application/json")

    def _method_not_allowed(self):
        self._send(405, json.dumps({"error": "read-only: GET only"}),
                   "application/json")

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed


def make_server(home=None, port=DEFAULT_PORT, host="127.0.0.1"):
    handler = type("BoundHandler", (Handler,),
                   {"home": home or home_dir()})
    return ThreadingHTTPServer((host, port), handler)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = DEFAULT_PORT
    home = home_dir()
    i = 0
    while i < len(argv):
        if argv[i] == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
        elif argv[i] == "--home" and i + 1 < len(argv):
            home = argv[i + 1]
            i += 2
        else:
            print("usage: status_server.py [--port N] [--home DIR]")
            return 2
    server = make_server(home=home, port=port)
    print("agt status dashboard on 127.0.0.1:%d (home %s)"
          % (server.server_address[1], home), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
