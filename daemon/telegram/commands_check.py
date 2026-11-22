"""``/check <CA>`` — lane-visibility lookup (2026-10-25).

Read-only: answers whether any paper lane entered a given mint, and
what happened to the position. This closes the visibility gap the
graduation lanes have: they trade on paper but send no cards, so a
runner the user hears about elsewhere needs a one-command answer
instead of a ledger hunt.

Covers every paper lane book in FOREX_AGENT_HOME: grad_raw,
grad_select, watcher, watcher_v2, ceo, momentum, steady, and the
degen card mirror. Ledger shapes differ (grad-style closes carry
pnl_usd/realized_mult/closed_reason; steady carries pnl/exit_ts/
exit_reason), so each lane declares its own keys. A lane whose
ledger is missing or unreadable is skipped, never fatal: a missing
book means that lane has no record, not that the lookup failed.

Never trades, never alerts, never touches state. Mint matching is
exact (Solana addresses are case-sensitive base58); no fuzzy
matching, so a hit is always the same coin.
"""

import json
import os
from datetime import datetime, timedelta, timezone

_WAT = timezone(timedelta(hours=1))

# (display name, ledger file, closes key, pnl key, close-ts key,
#  mult key or None, reason key)
_LANES = [
    ("grad_raw", "grad_raw_ledger.json", "closes", "pnl_usd",
     "close_ts", "realized_mult", "closed_reason"),
    ("grad_select", "grad_select_ledger.json", "closes", "pnl_usd",
     "close_ts", "realized_mult", "closed_reason"),
    ("watcher", "watcher_ledger.json", "closes", "pnl_usd",
     "close_ts", "realized_mult", "closed_reason"),
    ("watcher_v2", "watcher_v2_ledger.json", "closes", "pnl_usd",
     "close_ts", "realized_mult", "closed_reason"),
    ("ceo", "ceo_ledger.json", "closes", "pnl_usd",
     "close_ts", "realized_mult", "closed_reason"),
    ("momentum", "momentum_ledger.json", "closes", "pnl_usd",
     "close_ts", "realized_mult", "closed_reason"),
    ("steady", "steady_ledger.json", "closed", "pnl",
     "exit_ts", None, "exit_reason"),
    ("degen_mirror", "degen_mirror_ledger.json", "closes", "pnl_usd",
     "close_ts", None, "closed_reason"),
]


def _home():
    return os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")


def _fmt_ts(ts):
    try:
        dt = datetime.fromtimestamp(float(ts), tz=_WAT)
        return dt.strftime("%b %d %H:%M WAT")
    except (TypeError, ValueError):
        return "?"


def _fmt_price(p):
    try:
        v = float(p)
    except (TypeError, ValueError):
        return "?"
    if v == 0:
        return "0"
    if abs(v) < 0.0001:
        return "%.3g" % v
    return "%.6g" % v


def _load(home, filename):
    try:
        with open(os.path.join(home, filename)) as f:
            return json.load(f)
    except Exception:
        return None


def _positions(d):
    pos = d.get("positions", []) if isinstance(d, dict) else []
    if isinstance(pos, dict):
        return list(pos.values())
    return pos if isinstance(pos, list) else []


def check_mint(mint, home=None):
    """Return the /check reply text for one mint address."""
    mint = (mint or "").strip()
    if not mint:
        return "Usage: /check <contract address>"
    home = home or _home()
    hits = []
    checked = []
    for (name, filename, closes_key, pnl_key, ts_key,
         mult_key, reason_key) in _LANES:
        d = _load(home, filename)
        if not isinstance(d, dict):
            continue
        checked.append(name)
        closes = d.get(closes_key, []) or []
        for c in closes:
            if not isinstance(c, dict):
                continue
            if str(c.get("mint", "")) != mint:
                continue
            try:
                pnl = float(c.get(pnl_key) or 0)
            except (TypeError, ValueError):
                pnl = 0.0
            mult = c.get(mult_key) if mult_key else None
            try:
                mult_s = "%.2fx" % float(mult) if mult is not None else "?"
            except (TypeError, ValueError):
                mult_s = "?"
            pnl_s = ("+$%.2f" % pnl) if pnl >= 0 else ("-$%.2f" % abs(pnl))
            hits.append(
                "✅ %s: CLOSED %s at %s (%s), entered %s"
                % (name, pnl_s, mult_s,
                   c.get(reason_key) or "?",
                   _fmt_ts(c.get("entry_ts"))))
        for p in _positions(d):
            if not isinstance(p, dict):
                continue
            if str(p.get("mint", "")) != mint:
                continue
            peak = p.get("peak_mult")
            try:
                peak_s = "peak %.2fx" % float(peak) if peak is not None else "no peak yet"
            except (TypeError, ValueError):
                peak_s = "no peak yet"
            hits.append(
                "🟢 %s: OPEN, entered %s @ $%s (%s)"
                % (name, _fmt_ts(p.get("entry_ts")),
                   _fmt_price(p.get("entry_price")), peak_s))
    if not hits:
        return (
            "❌ Not in our paper book — no lane entered %s.\n"
            "Checked: %s."
            % (mint, ", ".join(checked) if checked else "no ledgers readable"))
    return "🔍 %s\n%s" % (mint, "\n".join(hits))


def cmd_check_handler(ctx, chat_id, user_id, update_id, args):
    """Adapter matching this daemon's handler convention."""
    if not args:
        ctx.send(chat_id, "Usage: /check <contract address>", None)
        return
    ctx.send(chat_id, check_mint(args[0]), None)
