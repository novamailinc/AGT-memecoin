#!/usr/bin/env python3
"""Grad-select 2x confirmation card tests (2026-10-28, user-ordered).

Covers the notification-only card section appended to
daemon/launch_scanner/grad_select.py:
  * card rendering: CA present, Copy-CA/tap buttons implied by the
    send path, honest no-safety line, confirmation-not-entry line,
    unknown ticker -> mint prefix (never guessed);
  * track callback: source 'grad-select-confirm' maps to short code
    'gs' and stays within Telegram's 64-byte callback_data cap;
  * first-run backfill: already-done leg-A mints seed the journal as
    pre-existing and send nothing (no stale flood);
  * fresh events: a newly banked leg A sends exactly once, then
    dedups on the next run;
  * closes catch: a position that banks leg A and finalizes between
    checks is still carded once via its close record;
  * send failure: the mint stays unjournaled and retries next run;
  * corrupt journal: fail-closed, nothing sends;
  * throttle: two calls inside the check window send at most once.
No network: DexScreener lookup and the Telegram send are stubbed.
The trading engine is a fake ledger dict; no lane code runs.
Plain script — run directly, not via pytest.
"""
import json
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "daemon", "launch_scanner"))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="gs-confirm-test-")
os.environ["FOREX_AGENT_HOME"] = _TMP

import grad_select as gs  # noqa: E402
from signal_bus import track_callback  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s %s" % (name, detail))
        FAILURES.append(name)


def _pos(mint, done_a=True, symbol="?", entry=1e-5, exit_a=2e-5,
         entry_ts=1793200000, reason_a=None):
    # Close records mark every leg done at finalization, so a true
    # 2x bank is proven by exit_reason 'leg_a_2x', not done alone.
    if reason_a is None:
        reason_a = "leg_a_2x" if done_a else ""
    return {"mint": mint, "symbol": symbol, "chain": "solana",
            "entry_ts": entry_ts, "entry_price": entry,
            "legs": {"a": {"done": done_a,
                           "exit_price": exit_a if done_a else 0,
                           "exit_reason": reason_a},
                     "b": {"done": False, "exit_price": 0},
                     "c": {"done": False, "exit_price": 0}},
            "entry_snapshot": {"mcap": 40000, "liquidity": 15000}}


def _close(mint, close_ts, done_a=True, symbol="?", entry=1e-5,
           exit_a=2e-5, reason_a=None):
    c = _pos(mint, done_a=done_a, symbol=symbol, entry=entry,
             exit_a=exit_a, reason_a=reason_a)
    c["close_ts"] = close_ts
    return c


class FakeEngine:
    def __init__(self, ledger):
        self.ledger = ledger

    def _load(self):
        return self.ledger


def main():
    journal_path = os.path.join(_TMP, "cards.json")
    gs.CONFIRM_CARDS_JOURNAL = journal_path
    gs._ds_token_info = lambda mint: {}

    sent = []
    fail_next = [False]

    def fake_send(text, mint):
        if fail_next[0]:
            return False
        sent.append((text, mint))
        return True

    gs.send_confirmation_card = fake_send

    # 1. Rendering: honest, complete, never guessed.
    card = gs.render_confirmation_card(
        _pos("AbCdEfGh1234567890pump", done_a=True, symbol="?",
             entry=1e-5, exit_a=2.5e-5))
    check("render-has-mint", "AbCdEfGh1234567890pump" in card)
    check("render-has-2x", "2x CONFIRMED" in card and "2.50x" in card)
    check("render-no-safety-line", "No safety audit" in card)
    check("render-not-entry", "confirmation, not an entry" in card)
    check("render-no-guessed-ticker",
          "AbCdEfGh…" in card and "?" not in card.split("\n")[1])
    check("render-has-context", "Mcap $40.0K" in card and "Liq $15.0K" in card)

    # 2. Track callback: short code, within cap.
    cb = track_callback("solana", "grad-select-confirm",
                        "AbCdEfGh1234567890pump")
    check("track-callback-format", cb.startswith("track:solana:gs:"),
          cb)
    check("track-callback-length", len(cb) <= 64, str(len(cb)))

    # 3. First-run backfill: no sends, both mints pre-existing.
    ledger = {"positions": {
                  "OLDOPEN11111111111111111111111111111111pump":
                      _pos("OLDOPEN11111111111111111111111111111111pump"),
                  "NOTDONE2222222222222222222222222222222pump":
                      _pos("NOTDONE2222222222222222222222222222222pump",
                           done_a=False),
              },
              "closes": [_close("OLDCLOSE33333333333333333333333333333pump",
                                 1793200100)]}
    eng = FakeEngine(ledger)
    out = gs.maybe_confirmation_cards(eng, now=1793200200)
    check("backfill-sends-nothing", out == [] and sent == [])
    with open(journal_path) as f:
        journal = json.load(f)
    check("backfill-journals-done",
          "OLDOPEN11111111111111111111111111111111pump" in journal["sent"]
          and "OLDCLOSE33333333333333333333333333333pump" in journal["sent"]
          and "NOTDONE2222222222222222222222222222222pump"
          not in journal["sent"])

    # 4. Fresh event: sends exactly once, then dedups.
    ledger["positions"]["FRESH444444444444444444444444444444444pump"] = \
        _pos("FRESH444444444444444444444444444444444pump")
    out = gs.maybe_confirmation_cards(eng, now=1793200300)
    check("fresh-sends-once", out == ["FRESH444444444444444444444444444444444pump"]
          and len(sent) == 1, str(out))
    out = gs.maybe_confirmation_cards(eng, now=1793200400)
    check("fresh-dedups", out == [] and len(sent) == 1, str(out))

    # 5. Closes catch: banks leg A and finalizes between checks.
    del ledger["positions"]["NOTDONE2222222222222222222222222222222pump"]
    ledger["closes"].append(
        _close("FAST5555555555555555555555555555555555pump", 1793200500))
    out = gs.maybe_confirmation_cards(eng, now=1793200600)
    check("closes-catch", out == ["FAST5555555555555555555555555555555555pump"]
          and len(sent) == 2, str(out))

    # 6. Send failure: unjournaled, retries next run.
    fail_next[0] = True
    ledger["positions"]["FAIL6666666666666666666666666666666666pump"] = \
        _pos("FAIL6666666666666666666666666666666666pump")
    out = gs.maybe_confirmation_cards(eng, now=1793200700)
    check("send-failure-no-card", out == [] and len(sent) == 2, str(out))
    fail_next[0] = False
    out = gs.maybe_confirmation_cards(eng, now=1793200800)
    check("send-failure-retries",
          out == ["FAIL6666666666666666666666666666666666pump"]
          and len(sent) == 3, str(out))

    # 7. Corrupt journal: fail-closed, nothing sends.
    with open(journal_path, "w") as f:
        f.write("{not json")
    ledger["positions"]["NEVER777777777777777777777777777777777pump"] = \
        _pos("NEVER777777777777777777777777777777777pump")
    out = gs.maybe_confirmation_cards(eng, now=1793200900)
    check("corrupt-journal-fail-closed", out == [] and len(sent) == 3,
          str(out))

    # 8. Throttle: two calls inside the window send at most once.
    os.remove(journal_path)
    gs._confirm_last_run = 0
    ledger2 = {"positions": {
                   "THROTTLE8888888888888888888888888888888pump":
                       _pos("THROTTLE8888888888888888888888888888888pump")},
               "closes": []}
    eng2 = FakeEngine(ledger2)
    gs.maybe_confirmation_cards(eng2, now=1793201000)  # backfill run
    ledger2["positions"]["THROTTLE9999999999999999999999999999999pump"] = \
        _pos("THROTTLE9999999999999999999999999999999pump")
    n_before = len(sent)
    out1 = gs.maybe_confirmation_cards(eng2, now=1793201100)
    out2 = gs.maybe_confirmation_cards(eng2, now=1793201110)
    check("throttle-once", len(sent) - n_before == 1 and out2 == [],
          "out1=%s out2=%s" % (out1, out2))

    # 9. Loser close: done at finalization but never banked -> no card.
    # (2026-10-29 regression: a time-stop close fired a false 0.50x
    # "2x CONFIRMED" card because closes mark every leg done at
    # finalization; exit_reason 'leg_a_2x' is the only proof of a bank.)
    os.remove(journal_path)
    gs._confirm_last_run = 0
    ledger3 = {"positions": {},
               "closes": [_close("LOSERAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump",
                                 1793202000, reason_a="hard_stop_-50pct",
                                 exit_a=0.5e-5)]}
    eng3 = FakeEngine(ledger3)
    gs.maybe_confirmation_cards(eng3, now=1793202100)  # backfill run
    n_before = len(sent)
    ledger3["closes"].append(
        _close("LOSERBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBpump",
               1793202200, reason_a="time_stop_24h", exit_a=0.8e-5))
    out = gs.maybe_confirmation_cards(eng3, now=1793202300)
    check("loser-close-no-card", out == [] and len(sent) == n_before,
          str(out))
    with open(journal_path) as f:
        journal3 = json.load(f)
    check("loser-close-not-journaled",
          "LOSERBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBpump"
          not in journal3["sent"])

    # 10. True-bank close still cards (closes-catch with proof).
    ledger3["closes"].append(
        _close("TRUEBANKCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCpump",
               1793202400, reason_a="leg_a_2x", exit_a=2e-5))
    out = gs.maybe_confirmation_cards(eng3, now=1793202500)
    check("true-bank-close-cards",
          out == ["TRUEBANKCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCpump"],
          str(out))

    if FAILURES:
        print("FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
