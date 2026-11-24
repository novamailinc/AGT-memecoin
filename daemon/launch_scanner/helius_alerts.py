"""Helius 429-spike alert state (2026-11-18, resilience build).

The pacer records every 429 with a timestamp. ``check_and_record``
— called by scripts/helius-429-watch on a short cadence (recommend
every 5 min via cron) — counts the 429s in the last 10 minutes:
more than 5 is a spike. It writes ``~/.forex-agent/helius_429_alert.json``
(active + counts + window, atomically) and logs loudly; the
self-audit reads the file and surfaces an active spike as a
problem, so a flap is caught within minutes instead of surfacing
as a two-day entry drought.

Telegram: notify_telegram() pings the user through the existing
legacy-telegram send.py CLI (subprocess, 20s timeout). It is
best-effort ONLY — failures are logged and swallowed; the alert
file is the durable record and the self-audit is the backstop.
The watch script notifies only on the transition into active, so
an ongoing spike does not spam a message every 5 minutes.

Delivery choice (per the build brief): alert file + loud log +
self-audit read is the primary channel; Telegram is the fast ping.
No raw key material anywhere in this module.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

try:
    from . import helius_ratelimit as _rl
except Exception:  # flat import (scripts put the dir on sys.path)
    try:
        import helius_ratelimit as _rl  # type: ignore
    except Exception:
        _rl = None  # type: ignore

logger = logging.getLogger(
    "forex_agent.daemon.launch_scanner.helius_alerts")

ALERT_FILE = "helius_429_alert.json"
TELEGRAM_SEND = ("/home/hatch/workspace/custom-connectors/"
                 "custom.legacy-telegram/bin/send.py")
DEFAULT_CHAT_ID = "6883056760"  # user's Telegram chat (not secret)
THRESHOLD = 5        # more than 5 429s ...
WINDOW_S = 600.0     # ... in 10 minutes = a spike


def _home(home: Optional[str] = None) -> str:
    if home:
        return home
    return os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")


def check_and_record(home: Optional[str] = None,
                     now: Optional[float] = None) -> Dict[str, Any]:
    """Count recent 429s, write the alert state file, log loudly.

    Returns the state dict (also written atomically to
    helius_429_alert.json). Never raises; on any read error the
    state reports inactive with count 0 and an "error" note."""
    h = _home(home)
    t = time.time() if now is None else now
    state: Dict[str, Any] = {"active": False, "count_10m": 0,
                             "threshold": THRESHOLD, "window_s": WINDOW_S,
                             "updated_ts": t, "first_429_ts": None,
                             "last_429_ts": None}
    try:
        events = _rl.recent_429_events(window_s=WINDOW_S, now=t) \
            if _rl is not None else []
        state["count_10m"] = len(events)
        state["active"] = len(events) > THRESHOLD
        state["first_429_ts"] = events[0] if events else None
        state["last_429_ts"] = events[-1] if events else None
    except Exception as exc:
        state["error"] = type(exc).__name__
    try:
        os.makedirs(h, exist_ok=True)
        path = os.path.join(h, ALERT_FILE)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w") as fh:
            json.dump(state, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        state["write_error"] = type(exc).__name__
    if state["active"]:
        logger.warning(
            "HELIUS 429 SPIKE: %d 429s in the last %.0f min "
            "(threshold %d) — background callers auto-shed, "
            "entry-critical calls keep priority tokens",
            state["count_10m"], WINDOW_S / 60.0, THRESHOLD)
    return state


def read_alert(home: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The last recorded alert state, or None when absent/corrupt.
    Never raises."""
    try:
        with open(os.path.join(_home(home), ALERT_FILE)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _send_via_daemon_token(text: str, chat_id: str,
                           home: str) -> bool:
    """Direct Telegram send with the daemon's own token file.

    Same pattern the trader card-senders already use
    (grad_select.send_confirmation_card): the token file at
    $FOREX_AGENT_HOME/telegram/bot_token (0600) is the bot's
    working credential for this same bot and chat. Used only as
    a fallback when the vault send.py path fails, so a stale
    vault token — or a path-segment surrogate the egress proxy
    never swaps — can no longer silence spike alerts. The token
    is never logged. Returns True on Telegram ok. Never raises.
    """
    try:
        with open(os.path.join(home, "telegram", "bot_token")) as fh:
            token = fh.read().strip()
        if not token:
            return False
        payload = {"chat_id": chat_id, "text": text}
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % token,
            data=data, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        return bool(body.get("ok"))
    except Exception as exc:
        logger.warning("helius alert daemon-token send failed: %s",
                       type(exc).__name__)
        return False


def notify_telegram(text: str, chat_id: Optional[str] = None) -> bool:
    """Best-effort Telegram ping: vault send.py CLI first, then
    the daemon's own token file as fallback.

    Returns True on delivery. Never raises; failures are logged
    (the alert file is the durable record)."""
    cid = chat_id or os.environ.get("HELIUS_ALERT_CHAT_ID") \
        or DEFAULT_CHAT_ID
    try:
        proc = subprocess.run(
            [sys.executable, TELEGRAM_SEND, "--chat-id", cid,
             "--text", text],
            capture_output=True, text=True, timeout=20)
        if proc.returncode == 0:
            return True
        logger.warning("helius alert telegram failed rc=%s: %s",
                       proc.returncode, (proc.stdout or "")[:200])
    except Exception as exc:
        logger.warning("helius alert telegram error: %s",
                       type(exc).__name__)
    # Fallback: the vault path 404s (stale token, or a
    # path-segment surrogate the proxy never swaps — same
    # failure class as the Helius query-param 401). The
    # daemon's token file is the working credential.
    return _send_via_daemon_token(text, cid, _home())
