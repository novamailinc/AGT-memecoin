"""telegram — Telegram signal + control bot daemon.

Every cycle:
  1. Long-poll the Telegram Bot API for updates (messages, button taps).
  2. Pair the first ``/start`` chat as the sole control channel; every
     other chat/user is ignored and audited.
  3. Route commands: read-only queries answer immediately; paper-only
     mutations park a two-step confirmation; /halt engages the kill
     switch at once; live/mainnet requests are refused on sight.
  4. Poll the durable event journal for new signal / trade / position /
     kill-switch / health / daemon events and fan them out as alerts.

Security posture:
  * Token lives at ``$FOREX_AGENT_HOME/telegram/bot_token`` (0600), or in
    the ``TELEGRAM_BOT_TOKEN`` env var; it never appears in logs, state,
    backups, or exceptions.
  * Telegram update offsets, processed-update replay log, alert dedup
    log, pending confirms, and the pairing are all persisted in durable
    daemon state: restarts neither lose pairing nor re-send alerts.
  * Telegram failures (network, API) are caught and logged; this daemon
    can die or stall without affecting the trading daemons.

This daemon NEVER enables live/mainnet trading; no such command exists.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from typing import Any, Dict, List, Optional

from daemon.common import Daemon, get_daemon_state, get_store, main, save_daemon_state, state_load_ok
from agent.events import bus as event_bus

from .alerts import format_alert, format_alert_markup
from . import blink as _blink
from . import filters as _filters
from . import thesis_watch as _thesis_watch
from .client import TelegramClient, TelegramError, TelegramFloodError, redact
from .commands import HandlerContext, handle_callback, handle_message, control_only
from .security import SecurityContext

logger = logging.getLogger("forex_agent.daemon.telegram")

POLL_TIMEOUT_S = 15      # long-poll window inside one cycle
JOURNAL_LIMIT = 100
TOKEN_FILE = "telegram/bot_token"

# Telegram Bot API hard limit for sendMessage text. 2026-10-17: an alert
# longer than this retried every poll for 6 days (1,301 failed sends) and
# head-of-line blocked every alert behind it. Never send longer; truncate
# honestly instead of dropping or stalling the queue.
TELEGRAM_MAX_TEXT_LEN = 4096
_TRUNC_MARKER = "\n…(message truncated to Telegram's 4096-char limit)"


def _fit_text(text: str) -> str:
    """Truncate text to Telegram's sendMessage limit, honestly marked."""
    if len(text) <= TELEGRAM_MAX_TEXT_LEN:
        return text
    cut = TELEGRAM_MAX_TEXT_LEN - len(_TRUNC_MARKER)
    return text[:cut] + _TRUNC_MARKER


def _is_permanent_send_error(exc: BaseException) -> bool:
    """True for Telegram 400-class errors: the message will never send no
    matter how often we retry, so retrying only blocks the queue."""
    msg = str(exc)
    return "HTTP 400" in msg or "Bad Request" in msg


def _home_dir() -> str:
    return os.environ.get("FOREX_AGENT_HOME") or os.path.join(os.path.expanduser("~"), ".forex-agent")


def load_token() -> Optional[str]:
    """Load the bot token from env or the 0600 token file. Never logged."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token and token.strip():
        return token.strip()
    path = os.path.join(_home_dir(), TOKEN_FILE)
    try:
        with open(path, "r") as fh:
            token = fh.read().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.error("telegram token file unreadable: %s", exc)
        return None
    if not token:
        return None
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            logger.warning("telegram token file %s has permissive mode %o; "
                           "run: chmod 600 %s", path, mode, path)
    except OSError:
        pass
    return token


class RealTools:
    """Thin facade over the agent's real tool functions."""

    def __init__(self):
        from agent.tools import signals as t_signals
        from agent.tools import trading as t_trading
        from agent.tools import account as t_account
        from agent.tools import misc as t_misc
        self._signals, self._trading = t_signals, t_trading
        self._account, self._misc = t_account, t_misc

    def forex_get_health(self):
        if control_only():
            return {'mode':'development_control_only', 'storage':'local',
                    'daemons':{'telegram':'running','trading':'not_started'},
                    'note':'No broker/worker health requests in control-only mode'}
        return self._misc.forex_get_health()
    def forex_get_signals(self, limit=20): return self._signals.forex_get_signals(limit=limit)
    def forex_get_signal(self, signal_id): return self._signals.forex_get_signal(signal_id)
    def forex_get_positions(self): return self._account.forex_get_positions()
    def forex_get_account(self): return self._account.forex_get_account()
    def forex_get_performance(self, lookback_days=30):
        return self._account.forex_get_performance(lookback_days=lookback_days)
    def forex_get_trade_history(self, symbol=None, limit=10):
        return self._account.forex_get_trade_history(symbol=symbol, limit=limit)
    def forex_get_risk(self): return self._account.forex_get_risk()
    def forex_request_trade(self, **kw): return self._trading.forex_request_trade(**kw)
    def forex_close_position(self, ticket, request_id=None):
        return self._trading.forex_close_position(ticket, request_id=request_id)
    def forex_kill_switch(self, engage=True, reason=None):
        if control_only():
            if not engage:
                return {'ok':False,'message':'Resume disabled in control-only mode'}
            store=get_store()
            store.set_kill_switch(True, 'telegram_control_only')
            state=store.get_kill_switch()
            return {'ok':state.get('engaged') is True, **state}
        return self._trading.forex_kill_switch(engage=engage, reason=reason)


class TelegramDaemon(Daemon):
    name = "telegram"
    interval = 20.0

    def __init__(self, interval: Optional[float] = None,
                 client_factory=None, tools=None):
        super().__init__(interval)
        self._client_factory = client_factory or (lambda token: TelegramClient(token))
        self._tools_override = tools
        self._client: Optional[TelegramClient] = None
        self._token: Optional[str] = None

    # -- internals ------------------------------------------------------
    def _tools(self):
        if self._tools_override is not None:
            return self._tools_override
        return RealTools()

    def _ensure_client(self) -> Optional[TelegramClient]:
        if self._client is not None:
            return self._client
        token = load_token()
        if not token:
            return None
        if token != self._token:
            self._token = token
            try:
                self._client = self._client_factory(token)
            except Exception as exc:
                logger.error("telegram client init failed: %s", redact(str(exc), token))
                self._client = None
        return self._client

    # -- cycle -----------------------------------------------------------
    def run_once(self) -> None:
        store = get_store()
        event_bus.configure(store=store)
        # state_load_ok False (e.g. DB locked on read) -> run read-only
        # this cycle: never persist, or the empty state wipes pairing
        # and cursor (2026-10-10 outage).
        state = get_daemon_state(store, self.name)
        _state_ok = state_load_ok(state)
        client = self._ensure_client()
        if client is None:
            logger.debug("telegram: no bot token configured; idle")
            if _state_ok:
                save_daemon_state(store, self.name, state)
            return
        # Restore flood cooldown across restarts: a restart must not
        # reset the backoff and re-hammer Telegram (2026-10-10).
        try:
            flood = getattr(client, "flood", None)
            if flood is not None:
                flood.cool_until = float(
                    state.get("flood_cool_until") or 0.0)
        except (TypeError, ValueError):
            pass
        sec = SecurityContext(state)

        ctx = HandlerContext(
            send=self._make_sender(client, state),
            delete_message=self._make_deleter(client),
            security=sec,
            tools=self._tools(),
            publish=lambda e, severity=None: event_bus.publish(dict(e), severity=severity),
        )

        # 1. pull updates -------------------------------------------------
        # A failed long-poll must NOT skip the alert fan-out below: inbound
        # message fetching and outbound alert delivery are independent.
        # getUpdates failures are logged and update handling is skipped,
        # but queued alerts still go out on this cycle.
        updates: list = []
        updates_ok = True
        try:
            updates = client.get_updates(offset=state.get("offset"), timeout=POLL_TIMEOUT_S)
        except TelegramError as exc:
            logger.warning("telegram getUpdates failed: %s", exc)
            updates_ok = False
        except Exception:
            logger.exception("telegram getUpdates crashed")
            updates_ok = False

        if updates_ok:
            max_id = None
            for update in updates:
                try:
                    uid = int(update.get("update_id", -1))
                except (TypeError, ValueError):
                    continue
                if max_id is None or uid > max_id:
                    max_id = uid
                if sec.seen_update(uid):
                    continue  # redelivery: already handled
                sec.mark_update(uid)
                try:
                    self._route_update(ctx, client, update, uid)
                except Exception:
                    logger.exception("telegram update handling crashed (update dropped, no retry storm)")
            if max_id is not None:
                state["offset"] = max_id + 1

        # 2. fan out alerts ----------------------------------------------
        # Runs even when the update pull above failed.
        self._fan_out(ctx, client, sec, state, store)

        if not control_only():
            # 3. card pulse ---------------------------------------------------
            # Blink the live-status line on tracked cards (own try/except so a
            # pulse failure can never break the alert loop above).
            try:
                _blink.maybe_poll(state, client)
            except Exception:
                logger.exception("blink maybe_poll crashed")

            # 4. lifecycle cards: thesis-break watcher -------------------------
            # Re-prices watched 🎯 PICKs and fires thesis-break / plan-level
            # alerts on confirmation. Display-only; own try/except.
            try:
                _thesis_watch.maybe_check(state, ctx.publish)
            except Exception:
                logger.exception("thesis_watch maybe_check crashed")

        # Never persist when the initial read failed: an empty state
        # would wipe pairing/cursor (2026-10-10 outage).
        if _state_ok:
            save_daemon_state(store, self.name, state)
        else:
            logger.warning("telegram: skipping state persist after failed read")

    # -- update routing --------------------------------------------------
    def _route_update(self, ctx: HandlerContext, client: TelegramClient,
                      update: Dict[str, Any], update_id: int) -> None:
        if "message" in update:
            msg = update["message"] or {}
            chat = msg.get("chat") or {}
            frm = msg.get("from") or {}
            handle_message(ctx, chat.get("id"), frm.get("id"), update_id,
                           msg.get("text") or "",
                           message_id=msg.get("message_id"))
        elif "callback_query" in update:
            cb = update["callback_query"] or {}
            if control_only():
                self._answer(client, cb.get('id'), 'Disabled in control-only mode')
                return
            msg = cb.get("message") or {}
            chat = msg.get("chat") or {}
            frm = cb.get("from") or {}
            handle_callback(ctx, chat.get("id"), frm.get("id"), update_id,
                            cb.get("id"), cb.get("data") or "",
                            answer=lambda qid, text: self._answer(client, qid, text))
        # other update types are intentionally ignored

    def _answer(self, client: TelegramClient, callback_id: str, text: Optional[str]) -> None:
        try:
            client.answer_callback_query(callback_id, text=text)
        except TelegramError as exc:
            logger.warning("answerCallbackQuery failed: %s", exc)

    def _make_sender(self, client: TelegramClient, state=None):
        def _send(chat_id: Any, text: str, reply_markup: Optional[Dict[str, Any]]) -> None:
            try:
                result=client.send_message(chat_id, text, reply_markup=reply_markup)
                if state is not None and control_only():
                    import time
                    state['last_command_delivery']={
                        'chat_id':chat_id,'message_id':(result or {}).get('message_id'),
                        'accepted_at':time.time(),'status':'telegram_api_accepted_not_read_receipt'}
            except TelegramError as exc:
                if state is not None and control_only():
                    state['command_send_failures']=int(state.get('command_send_failures',0))+1
                logger.warning("telegram send failed: %s", exc)
        return _send

    def _make_deleter(self, client: TelegramClient):
        def _delete(chat_id: Any, message_id: Any) -> None:
            try:
                client.delete_message(chat_id, message_id)
            except TelegramError as exc:
                logger.warning("telegram delete failed: %s", exc)
                raise
        return _delete

    # -- alert fan-out ----------------------------------------------------
    def _fan_out(self, ctx: HandlerContext, client: TelegramClient,
                 sec: SecurityContext, state: Dict[str, Any], store) -> None:
        chat_id = state.get("paired_chat_id")
        since = state.get("last_event_ts")
        try:
            events: List[Dict[str, Any]] = event_bus.poll(since=since, limit=JOURNAL_LIMIT)
        except Exception:
            logger.exception("telegram journal poll failed")
            return
        # Oldest-first, in-order at-least-once: last_event_ts only advances
        # past alerts that were actually handled. A failed send stops the
        # pass (break, not continue) so the failed alert is retried on the
        # next poll instead of being skipped forever.
        events = sorted(events, key=lambda e: e.get("ts", "") or "")
        advanced = since or ""
        for event in events:
            event_id = event.get("event_id") or ""
            ts = event.get("ts", "") or ""
            if event_id and sec.alert_seen(event_id):
                advanced = max(advanced, ts)  # already delivered: safe to skip
                continue
            if not chat_id:
                # Not paired yet: deliberate policy drop (nothing to send
                # to); advance so unpaired history doesn't flood on pairing.
                advanced = max(advanced, ts)
                continue
            # Personal filters — signal cards ONLY. Safety-critical events
            # (kill switch, health, trades, positions) always send
            # immediately, never filtered, never queued. The filter runs
            # before formatting because it may trim digest items in place.
            if event.get("event") in _filters.SIGNAL_EVENTS:
                flt = _filters.get_filters(state, chat_id)
                if not _filters.event_passes(event, flt):
                    # Filtered out: deliberate skip, never retried.
                    if event_id:
                        sec.mark_alert(event_id)
                    advanced = max(advanced, ts)
                    continue
                if _filters.in_quiet(flt):
                    # Quiet hours: queue a compact summary for the digest.
                    item = _filters.summarize_for_quiet(event)
                    if item is not None:
                        _filters.queue_quiet(state, item)
                    if event_id:
                        sec.mark_alert(event_id)
                    advanced = max(advanced, ts)
                    continue
            text = format_alert(event)
            if text is None:
                advanced = max(advanced, ts)  # never alertable: safe to skip
                continue
            markup = format_alert_markup(event)
            # Card pulse: blinkable single-mint cards get a live-status
            # first line (green while running, red while dumping).
            blink_line = _blink.initial_line(event)
            send_text = (blink_line + "\n" + text) if blink_line else text
            # 2026-10-17: truncate to the Bot API limit BEFORE sending —
            # an over-long message fails with a permanent 400 and must
            # never reach the API, let alone block the queue retrying.
            send_text = _fit_text(send_text)
            try:
                result = client.send_message(chat_id, send_text,
                                             reply_markup=markup)
            except TelegramFloodError as exc:
                # Backoff, not an error: stay quiet, keep the alert
                # queued, persist the cooldown so a restart can't
                # reset it and re-hammer (2026-10-10).
                logger.info("telegram flood cooldown: %s", exc)
                try:
                    flood = getattr(client, "flood", None)
                    if flood is not None:
                        state["flood_cool_until"] = float(
                            flood.cool_until)
                except (TypeError, ValueError):
                    pass
                break  # do NOT advance past the unsent alert
            except TelegramError as exc:
                if _is_permanent_send_error(exc):
                    # 2026-10-17: a permanent 400 (e.g. message too long,
                    # bad markup) will fail identically on every retry —
                    # retrying it head-of-line blocked ALL alerts for 6
                    # days. Log loudly, mark it seen, and ADVANCE past it
                    # so the queue keeps flowing. Truncation above should
                    # make the length case unreachable; this is the net.
                    logger.error(
                        "telegram alert permanently undeliverable, "
                        "skipping (event=%s ts=%s): %s",
                        event.get("event"), ts, exc)
                    if event_id:
                        sec.mark_alert(event_id)
                    advanced = max(advanced, ts)
                    continue
                logger.warning("telegram alert send failed: %s", exc)
                break  # do NOT advance past a failed send; retry next poll
            except Exception:
                logger.exception("telegram alert send crashed")
                break
            # Mark delivered only AFTER the send succeeded — marking before
            # would silently drop alerts whose send failed.
            if event_id:
                sec.mark_alert(event_id)
            if blink_line:
                try:
                    _blink.register(state, event, chat_id,
                                    (result or {}).get("message_id"), send_text)
                except Exception:
                    logger.exception("blink register crashed")
            # Lifecycle cards (2026-10-09): track freshly sent 🎯 PICKs
            # for thesis-break watching. Registration happens only
            # AFTER a successful send — no card, no watcher.
            if event.get("event") == "launch.intraday_top3":
                try:
                    _thesis_watch.register_pick(state, event)
                except Exception:
                    logger.exception("thesis_watch register crashed")
            advanced = max(advanced, ts)
        # Quiet-hours flush: one digest card when quiet ends. Runs even
        # when the loop above broke on a failed send — the queue is
        # independent of the journal cursor.
        if chat_id:
            self._flush_quiet(client, state, chat_id)
        if advanced and (not since or advanced > since):
            state["last_event_ts"] = advanced

    @staticmethod
    def _flush_quiet(client: TelegramClient, state: Dict[str, Any],
                     chat_id: Any) -> None:
        """Send the quiet-hours digest if quiet has ended and anything is
        queued. The queue is cleared ONLY after a successful send — a
        failed send keeps everything for the next cycle."""
        try:
            flt = _filters.get_filters(state, chat_id)
            if _filters.in_quiet(flt):
                return
            pending = state.get("quiet_queue") or []
            if not pending:
                return
            digest = _filters.format_quiet_digest(pending)
        except Exception:
            logger.exception("quiet digest build crashed")
            return
        try:
            # Same 4096 guard as the main fan-out (2026-10-17).
            client.send_message(chat_id, _fit_text(digest), reply_markup=None)
        except TelegramError as exc:
            logger.warning("quiet digest send failed: %s", exc)
            return  # keep the queue; retry next cycle
        except Exception:
            logger.exception("quiet digest send crashed")
            return
        state["quiet_queue"] = []
        logger.warning("quiet digest sent: %d held signals", len(pending))


def main_entry(argv=None):
    if argv and '--control-only' in argv:
        os.environ['TELEGRAM_CONTROL_ONLY']='1'
    return main(lambda: TelegramDaemon(), argv)


if __name__ == "__main__":
    sys.exit(main_entry(sys.argv[1:]))
