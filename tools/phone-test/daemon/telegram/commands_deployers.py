"""Read-only /deployers command: top deployers by 2x-hit rate.

The command-map in daemon/telegram/commands.py stays untouched — the
integrator registers ``cmd_deployers_handler`` (the adapter matching the
daemon's (ctx, chat_id, user_id, update_id, args) convention); it calls
``cmd_deployers(ctx, args) -> str`` and sends the text.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List


def _ago(ts: float) -> str:
    v = time.time() - ts
    if v < 0:
        v = 0
    if v < 60:
        return "%ds" % int(v)
    if v < 3600:
        return "%.0fm" % (v / 60.0)
    if v < 86400:
        return "%.1fh" % (v / 3600.0)
    return "%.1fd" % (v / 86400.0)


def _table(rows: List[Dict[str, Any]], limit: int) -> str:
    lines = ["🏭 Top deployers by 2x-hit rate (%d shown):" % len(rows[:limit])]
    for r in rows[:limit]:
        dep = r.get("deployer") or "?"
        lines.append("• %s… — %d/%d hit 2x (%.0f%%) · %d rugs · %d dead · %s ago" % (
            dep[:8],
            int(r.get("hits_2x") or 0),
            int(r.get("launches_seen") or 0),
            100.0 * float(r.get("hit_rate") or 0),
            int(r.get("rugs") or 0),
            int(r.get("dead") or 0),
            _ago(float(r.get("last_seen") or 0))))
    return "\n".join(lines)


def cmd_deployers(ctx: Any, args: List[str]) -> str:
    """Read-only /deployers handler.

    Returns the reply text (the adapter sends it). ``args`` may carry an
    optional min-launches override, e.g. ``/deployers 5``. Unknown,
    first-time, and cold deployers are never listed as hot — the
    min-launches filter applies before ranking.
    """
    try:
        from daemon.launch_scanner import deployers
    except Exception as exc:
        return "⚠️ deployer tracking unavailable: %s" % exc

    # Optional min-launches override from the user: /deployers 5.
    min_launches = 3
    if args:
        try:
            min_launches = max(1, int(str(args[0]).strip()))
        except (TypeError, ValueError):
            pass

    class _Degen:  # noqa: D106 — tiny shim for the getattr knobs
        def __init__(self, ml):
            self.deployer_min_launches = ml
            self.deployer_hot_hit_rate = 0.30

    rows = deployers.hot_deployers(_Degen(min_launches), limit=12)
    if not rows:
        text = ("🏭 No hot deployers yet.\n"
                "Deployer history builds as launches are graded: a deployer "
                "becomes hot after ≥%d launches with ≥30%% hitting 2x. "
                "Unknown and first-time deployers stay neutral — never a "
                "boost, never a penalty." % min_launches)
    else:
        total = len(deployers.load_registry())
        text = _table(rows, 12)
        text += ("\n\n%d deployers tracked in history · min launches ≥%d" %
                 (total, min_launches))
    # Phase 6 intel sections (read-only, proposal-only).
    try:
        bl_open = [p for p in deployers.load_bl_proposals()
                   if p.get("status") == "open"]
        if bl_open:
            text += "\n\n🚫 Blacklist proposals (%d open — review only):" % len(
                bl_open)
            for p in bl_open[:5]:
                text += "\n• %s… — %d launches, %d rugs (%.0f%%)" % (
                    str(p.get("deployer") or "?")[:8],
                    int(p.get("launches_seen") or 0),
                    int(p.get("rugs") or 0),
                    100.0 * float(p.get("rug_rate") or 0))
            text += ("\nNothing is auto-blocked — the blacklist is "
                     "human-managed.")
        clusters = deployers.funder_clusters()
        if clusters:
            n_dep = sum(len(v) for v in clusters.values())
            text += ("\n\n🔗 Funder clusters: %d (%d deployers share a "
                     "first-funder — possible bundle farms)" % (
                         len(clusters), n_dep))
    except Exception:
        pass
    return text[:3900]


def cmd_deployers_handler(ctx, chat_id, user_id, update_id, args):
    """Command-map adapter: (ctx, chat_id, user_id, update_id, args)."""
    try:
        ctx.send(chat_id, cmd_deployers(ctx, args or []), None)
    except Exception as exc:
        try:
            ctx.send(chat_id, "⚠️ /deployers failed: %s" % exc, None)
        except Exception:
            pass
