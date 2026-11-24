#!/usr/bin/env python3
"""Grad-raw 2x confirmation card tests (2026-10-29, user-ordered).

Covers the notification-only card section appended to
daemon/launch_scanner/grad_raw.py:
  * card rendering: grad_raw label, no-filters honesty lines,
    confirmation-not-entry line, unknown ticker -> mint prefix;
  * track callback: source 'grad-raw-confirm' maps to short code
    'gr' and stays within Telegram's 64-byte callback_data cap;
  * first-run backfill: truly banked leg-A mints seed as
    pre-existing and send nothing; loser closes (done at
    finalization, no leg_a_2x exit_reason) are NOT pre-existing;
  * fresh events: a newly banked leg A sends exactly once, then
    dedups on the next run;
  * closes catch: a position that banks leg A and finalizes between
    checks is still carded once via its close record;
  * loser closes never card (2026-10-29 regression: the grad_select
    checker carded a time-stop close as a false 2x because closes
    mark every leg done; exit_reason is the proof);
  * send failure: the mint stays unjournaled and retries next run;
  * corrupt journal: fail-closed, nothing sends;
  * throttle: two calls inside the check window send at most once;
  * daily cap: max 10 cards per WAT day; over-cap mints are
    journaled as capped-dropped, never retried stale; the cap
    resets on the next WAT day.
No network: DexScreener lookup and the Telegram send are stubbed.
The trading engine is a fake ledger dict; no lane code runs.
Plain script — run directly, not via pytest.
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "daemon", "launch_scanner"))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="gr-confirm-test-")
os.environ["FOREX_AGENT_HOME"] = _TMP

import grad_raw as gr  # noqa: E402
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
    gr.CONFIRM_CARDS_JOURNAL = journal_path
    gr._ds_token_info = lambda mint: {}

    sent = []
    fail_next = [False]

    def fake_send(text, mint):
        if fail_next[0]:
            return False
        sent.append((text, mint))
        return True

    gr.send_confirmation_card = fake_send
    gr._CARDS_ENABLED = True  # live default is False (user kill order)

    # 1. Rendering: grad_raw label, honest no-filter lines.
    card = gr.render_confirmation_card(
        _pos("AbCdEfGh1234567890pump", done_a=True, symbol="?",
             entry=1e-5, exit_a=2.5e-5))
    check("render-has-mint", "AbCdEfGh1234567890pump" in card)
    check("render-has-grad-raw", "grad_raw" in card)
    check("render-has-2x", "2x CONFIRMED" in card and "2.50x" in card)
    check("render-no-filters-line", "No filters on this lane" in card)
    check("render-no-safety-line", "No safety audit" in card)
    check("render-not-entry", "confirmation, not an entry" in card)
    check("render-no-guessed-ticker",
          "AbCdEfGh…" in card and "?" not in card.split("\n")[1])
    check("render-has-context", "Mcap $40.0K" in card and "Liq $15.0K" in card)

    # 2. Track callback: short code, within cap.
    cb = track_callback("solana", "grad-raw-confirm",
                        "AbCdEfGh1234567890pump")
    check("track-callback-format", cb.startswith("track:solana:gr:"),
          cb)
    check("track-callback-length", len(cb) <= 64, str(len(cb)))

    # 3. First-run backfill: true banks pre-existing, loser close not.
    ledger = {"positions": {
                  "OLDOPEN11111111111111111111111111111111pump":
                      _pos("OLDOPEN11111111111111111111111111111111pump"),
                  "NOTDONE2222222222222222222222222222222pump":
                      _pos("NOTDONE2222222222222222222222222222222pump",
                           done_a=False),
              },
              "closes": [_close("OLDCLOSE33333333333333333333333333333pump",
                                 1793200100),
                         _close("OLDLOSER44444444444444444444444444444pump",
                                 1793200100, reason_a="hard_stop_-50pct",
                                 exit_a=0.5e-5)]}
    eng = FakeEngine(ledger)
    out = gr.maybe_confirmation_cards(eng, now=1793200200)
    check("backfill-sends-nothing", out == [] and sent == [])
    with open(journal_path) as f:
        journal = json.load(f)
    check("backfill-journals-true-banks",
          "OLDOPEN11111111111111111111111111111111pump" in journal["sent"]
          and "OLDCLOSE33333333333333333333333333333pump" in journal["sent"])
    check("backfill-excludes-loser-close",
          "OLDLOSER44444444444444444444444444444pump"
          not in journal["sent"])
    check("backfill-excludes-notdone",
          "NOTDONE2222222222222222222222222222222pump"
          not in journal["sent"])

    # 4. Fresh event: sends exactly once, then dedups.
    ledger["positions"]["FRESH5555555555555555555555555555555555pump"] = \
        _pos("FRESH5555555555555555555555555555555555pump")
    out = gr.maybe_confirmation_cards(eng, now=1793200300)
    check("fresh-sends-once", out == ["FRESH5555555555555555555555555555555555pump"]
          and len(sent) == 1, str(out))
    out = gr.maybe_confirmation_cards(eng, now=1793200400)
    check("fresh-dedups", out == [] and len(sent) == 1, str(out))

    # 5. Closes catch: banks leg A and finalizes between checks.
    del ledger["positions"]["NOTDONE2222222222222222222222222222222pump"]
    ledger["closes"].append(
        _close("FAST6666666666666666666666666666666666pump", 1793200500))
    out = gr.maybe_confirmation_cards(eng, now=1793200600)
    check("closes-catch", out == ["FAST6666666666666666666666666666666666pump"]
          and len(sent) == 2, str(out))

    # 6. Loser close: done at finalization, never banked -> no card.
    ledger["closes"].append(
        _close("LOSER777777777777777777777777777777777pump", 1793200700,
               reason_a="time_stop_24h", exit_a=0.8e-5))
    out = gr.maybe_confirmation_cards(eng, now=1793200800)
    check("loser-close-no-card", out == [] and len(sent) == 2, str(out))

    # 7. Send failure: unjournaled, retries next run.
    fail_next[0] = True
    ledger["positions"]["FAIL8888888888888888888888888888888888pump"] = \
        _pos("FAIL8888888888888888888888888888888888pump")
    out = gr.maybe_confirmation_cards(eng, now=1793200900)
    check("send-failure-no-card", out == [] and len(sent) == 2, str(out))
    fail_next[0] = False
    out = gr.maybe_confirmation_cards(eng, now=1793201000)
    check("send-failure-retries",
          out == ["FAIL8888888888888888888888888888888888pump"]
          and len(sent) == 3, str(out))

    # 8. Corrupt journal: fail-closed, nothing sends.
    with open(journal_path, "w") as f:
        f.write("{not json")
    ledger["positions"]["NEVER999999999999999999999999999999999pump"] = \
        _pos("NEVER999999999999999999999999999999999pump")
    out = gr.maybe_confirmation_cards(eng, now=1793201100)
    check("corrupt-journal-fail-closed", out == [] and len(sent) == 3,
          str(out))

    # 9. Throttle: two calls inside the window send at most once.
    os.remove(journal_path)
    gr._confirm_last_run = 0
    ledger2 = {"positions": {
                   "THROTTLEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump":
                       _pos("THROTTLEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAApump")},
               "closes": []}
    eng2 = FakeEngine(ledger2)
    gr.maybe_confirmation_cards(eng2, now=1793201200)  # backfill run
    ledger2["positions"]["THROTTLEBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBpump"] = \
        _pos("THROTTLEBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBpump")
    n_before = len(sent)
    out1 = gr.maybe_confirmation_cards(eng2, now=1793201300)
    out2 = gr.maybe_confirmation_cards(eng2, now=1793201310)
    check("throttle-once", len(sent) - n_before == 1 and out2 == [],
          "out1=%s out2=%s" % (out1, out2))

    # 10. Daily cap: 12 fresh -> 10 sent, 2 capped-dropped, no retry.
    os.remove(journal_path)
    gr._confirm_last_run = 0
    ledger3 = {"positions": {}, "closes": []}
    eng3 = FakeEngine(ledger3)
    gr.maybe_confirmation_cards(eng3, now=1793202000)  # backfill run
    n_before = len(sent)
    for i in range(12):
        m = "CAP%02d%038d" % (i, i) + "pump"
        ledger3["positions"][m] = _pos(m)
    out = gr.maybe_confirmation_cards(eng3, now=1793202100)
    check("cap-sends-10", len(out) == 10 and len(sent) - n_before == 10,
          "out=%d" % len(out))
    with open(journal_path) as f:
        journal3 = json.load(f)
    capped = [m for m, r in journal3["sent"].items() if r.get("capped")]
    check("cap-drops-2", len(capped) == 2, str(capped))
    n_before = len(sent)
    out = gr.maybe_confirmation_cards(eng3, now=1793202200)
    check("cap-no-retry-same-day", out == [] and len(sent) == n_before,
          str(out))

    # 11. Cap resets on the next WAT day.
    ledger3["positions"]["NEXTDAY0000000000000000000000000000000pump"] = \
        _pos("NEXTDAY0000000000000000000000000000000pump")
    out = gr.maybe_confirmation_cards(eng3, now=1793202100 + 86400)
    check("cap-resets-next-day",
          out == ["NEXTDAY0000000000000000000000000000000pump"], str(out))

    # 12. Kill flag: disabled checker sends nothing, journals nothing.
    gr._CARDS_ENABLED = False
    ledger3["positions"]["KILLED00000000000000000000000000000000pump"] = \
        _pos("KILLED00000000000000000000000000000000pump")
    n_before = len(sent)
    out = gr.maybe_confirmation_cards(eng3, now=1793202100 + 2 * 86400)
    check("cards-disabled-sends-nothing",
          out == [] and len(sent) == n_before, str(out))
    gr._CARDS_ENABLED = True

    if FAILURES:
        print("FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
