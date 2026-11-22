"""Telegram Bot API client for the paper-trading alert/control bot.

Design notes
------------
* stdlib-only (urllib), matching repo conventions elsewhere.
* The bot token is NEVER logged, raised in exceptions, or echoed into
  error bodies handed to callers. Every string that could contain the
  token passes through ``_redact`` before it reaches logs or messages.
* Transport failures raise :class:`TelegramError` and are caught by the
  daemon loop: Telegram going down must never crash the daemon, and this
  daemon going down must never affect the trading daemons.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger("forex_agent.daemon.telegram")

API_BASE = "https://api.telegram.org"

_REDACTED = "[REDACTED]"

# Methods that mutate chat state (the ones Telegram flood-limits hard).
_MUTATING = frozenset({
    "sendMessage", "editMessageText", "deleteMessage",
    "answerCallbackQuery",
})

_RETRY_AFTER_RE = re.compile(r"retry after (\d+)", re.IGNORECASE)


class TelegramError(Exception):
    """A Telegram API or transport failure (token never included)."""


class TelegramFloodError(TelegramError):
    """Raised when the client is cooling down after a 429.

    No HTTP call is made — this is the backoff itself. Callers should
    treat it like a failed send (keep the alert queued, try later),
    not like a bug.
    """


class FloodGuard:
    """Per-client 429 backoff. 2026-10-10: the daemon retried failed
    sends with no backoff — 3,306 hammering attempts that kept
    extending Telegram's flood ban. Now a 429 parks ALL mutating calls
    until Telegram's own retry_after elapses (+5s buffer)."""

    def __init__(self) -> None:
        self.cool_until = 0.0

    def cooling(self, now: Optional[float] = None) -> float:
        """Seconds of cooldown remaining (0 when clear)."""
        return max(0.0, self.cool_until - (now if now is not None
                                           else time.time()))

    def check(self) -> None:
        """Raise TelegramFloodError instead of making a doomed call."""
        left = self.cooling()
        if left > 0:
            raise TelegramFloodError(
                "Telegram flood cooldown: %.0fs remaining" % left)

    def note_429(self, retry_after_s: Any) -> float:
        """Park mutating calls for Telegram's retry_after + buffer."""
        try:
            ra = float(retry_after_s)
        except (TypeError, ValueError):
            ra = 60.0
        wait = max(1.0, ra) + 5.0
        self.cool_until = time.time() + wait
        logger.warning("Telegram 429: flood cooldown %.0fs", wait)
        return wait


def _parse_retry_after(raw: str) -> Optional[float]:
    """Extract Telegram's retry_after from a 429 body."""
    try:
        payload = json.loads(raw)
        ra = ((payload or {}).get("parameters") or {}).get("retry_after")
        if ra is not None:
            return float(ra)
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        m = _RETRY_AFTER_RE.search(raw or "")
        if m:
            return float(m.group(1))
    except (ValueError, TypeError):
        pass
    return None


def redact(text: Any, token: Optional[str]) -> Any:
    """Strip every occurrence of the token from a string (best effort)."""
    if not token or not isinstance(text, str):
        return text
    return text.replace(token, _REDACTED)


class TelegramClient:
    """Minimal Bot API client: getMe / getUpdates / sendMessage / answerCallbackQuery."""

    def __init__(self, token: str, api_base: str = API_BASE, timeout: float = 30.0):
        if not token or not isinstance(token, str):
            raise ValueError("Telegram bot token is required")
        self._token = token
        self._base = api_base.rstrip("/")
        self._timeout = timeout
        self._flood = FloodGuard()

    @property
    def flood(self) -> FloodGuard:
        """The client's 429 backoff guard (persist cool_until in state)."""
        return self._flood

    # -- low level -----------------------------------------------------
    def _url(self, method: str) -> str:
        return "%s/bot%s/%s" % (self._base, self._token, method)

    def _request(self, method: str, params: Optional[Dict[str, Any]] = None,
                 use_get: bool = False) -> Dict[str, Any]:
        params = params or {}
        # Flood backoff: never make a doomed mutating call while cooling.
        if method in _MUTATING:
            self._flood.check()
        # Keep the token out of logs AND out of the URL query string.
        safe_params = {k: v for k, v in params.items()}
        try:
            if use_get:
                url = self._url(method) + "?" + urllib.parse.urlencode(safe_params)
                req = urllib.request.Request(
                    url, method="GET",
                    headers={"Connection": "close"})
            else:
                body = urllib.parse.urlencode(safe_params).encode("utf-8")
                req = urllib.request.Request(
                    self._url(method), data=body, method="POST",
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "Connection": "close"},
                )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", "replace")
            except Exception:
                raw = ""
            if exc.code == 429:
                # Park mutating calls until Telegram's retry_after
                # elapses — hammering only extends the ban.
                ra = _parse_retry_after(raw)
                wait = self._flood.note_429(ra if ra is not None else 60.0)
                raise TelegramFloodError(
                    "Telegram API HTTP 429 for %s: cooling down %.0fs"
                    % (method, wait)
                ) from None
            raise TelegramError(
                "Telegram API HTTP %s for %s: %s"
                % (exc.code, method, redact(raw[:500], self._token))
            ) from None
        except Exception as exc:
            raise TelegramError(
                "Telegram transport failed for %s: %s"
                % (method, redact(str(exc), self._token))
            ) from None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise TelegramError(
                "Telegram API returned non-JSON for %s" % method) from None
        if not payload.get("ok"):
            if payload.get("error_code") == 429:
                ra = _parse_retry_after(raw)
                wait = self._flood.note_429(ra if ra is not None else 60.0)
                raise TelegramFloodError(
                    "Telegram API 429 for %s: cooling down %.0fs"
                    % (method, wait)
                ) from None
            desc = payload.get("description", "unknown error")
            raise TelegramError(
                "Telegram API error for %s: %s" % (method, redact(str(desc), self._token)))
        return payload.get("result")

    # -- bot API --------------------------------------------------------
    def get_me(self) -> Dict[str, Any]:
        """Authenticate the token; returns the bot user record."""
        return self._request("getMe", use_get=True)

    def get_updates(self, offset: Optional[int] = None,
                    timeout: int = 15, limit: int = 100) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"timeout": timeout, "limit": limit,
                                  "allowed_updates": json.dumps(["message", "callback_query"])}
        if offset is not None:
            params["offset"] = offset
        result = self._request("getUpdates", params)
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: Any, text: str,
                     reply_markup: Optional[Dict[str, Any]] = None,
                     disable_notification: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": disable_notification,
        }
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        return self._request("sendMessage", params)

    def answer_callback_query(self, callback_query_id: str,
                              text: Optional[str] = None,
                              show_alert: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {"callback_query_id": callback_query_id,
                                  "show_alert": show_alert}
        if text:
            params["text"] = text
        return self._request("answerCallbackQuery", params)

    def delete_message(self, chat_id: Any, message_id: Any) -> Dict[str, Any]:
        """Best-effort delete (used to scrub secrets the user sent)."""
        return self._request("deleteMessage",
                             {"chat_id": chat_id, "message_id": message_id})

    def edit_message_text(self, chat_id: Any, message_id: Any,
                          text: str) -> Dict[str, Any]:
        """Edit a sent message's text (used by the card pulse). Edits are
        silent: Telegram never re-notifies on edit."""
        return self._request("editMessageText",
                             {"chat_id": chat_id, "message_id": message_id,
                              "text": text, "disable_web_page_preview": True})
