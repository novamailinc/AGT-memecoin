#!/usr/bin/env python3
"""Grad-select entry card tests (2026-10-29, user-ordered).

Covers the notification-only entry-card section appended to
daemon/launch_scanner/grad_select.py:
  * card rendering: ENTRY label, entry price, filters line,
    stake/ladder line, no-safety line, entry-not-confirmation line,
    unknown ticker -> mint prefix (never guessed);
  * track callback: source 'grad-select-entry' maps to short code
    'ge' and stays within Telegram's 64-byte callback_data cap;
  * first-run backfill: every already-known mint (open or closed)
    seeds as pre-existing and sends nothing (no stale flood);
  * fresh entries: a newly opened position sends exactly once,
    then dedups on the next run;
  * positions-only scan: a position that opens AND closes between
    checks is never carded (it is already dead);
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

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "daemon", "launch_scanner"))
sys.path.insert(0, _ROOT)

_TMP = tempfile.mkdtemp(prefix="gs-entry-test-")
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


def _pos(mint, symbol="?", entry=1e-5, entry_ts=1793200000):
    return {"mint": mint, "symbol": symbol, "chain": "solana",
            "entry_ts": entry_ts, "entry_price": entry,
            "legs": {"a": {"done": False, "exit_price": 0,
                           "exit_reason": ""},
                     "b": {"done": False, "exit_price": 0},
                     "c": {"done": False, "exit_price": 0}},
            "entry_snapshot": {"mcap": 40000, "liquidity": 15000}}


def _close(mint, close_ts, entry_ts=1793200000):
    c = _pos(mint, entry_ts=entry_ts)
    c["close_ts"] = close_ts
    return c


class FakeEngine:
    def __init__(self, ledger):
        self.ledger = ledger

    def _load(self):
        return self.ledger


def main():
    journal_path = os.path.join(_TMP, "cards.json")
    gs.ENTRY_CARDS_JOURNAL = journal_path
    gs._ds_token_info = lambda mint: {}

    sent = []
    fail_next = [False]

    def fake_send(text, mint):
        if fail_next[0]:
            return False
        sent.append((text, mint))
        return True

    gs.send_entry_card = fake_send

    # 1. Rendering: entry signal, honest, complete, never guessed.
    card = gs.render_entry_card(
        _pos("AbCdEfGh1234567890pump", symbol="?", entry=1e-5))
    check("render-has-mint", "AbCdEfGh1234567890pump" in card)
    check("render-has-entry-label", "ENTRY" in card and "grad_select" in card)
    check("render-has-entry-price", "Paper entered at $" in card)
    check("render-has-filters", "Passed evidence filters" in card)
    check("render-has-ladder", "Paper stake $25" in card and "50%" in card)
    check("render-no-safety-line", "No safety audit" in card)
    check("render-not-confirmation", "not a confirmation" in card)
    check("render-no-guessed-ticker",
          "AbCdEfGh…" in card and "?" not in card.split("\n")[1])
    check("render-has-context", "Mcap $40.0K" in card and "Liq $15.0K" in card)

    # 2. Track callback: short code, within cap.
    cb = track_callback("solana", "grad-select-entry",
                        "AbCdEfGh1234567890pump")
    check("track-callback-format", cb.startswith("track:solana:ge:"),
          cb)
    check("track-callback-length", len(cb) <= 64, str(len(cb)))

    # 3. First-run backfill: no sends, known mints pre-existing.
    ledger = {"positions": {
                  "OLDOPEN11111111111111111111111111111111pump":
                      _pos("OLDOPEN11111111111111111111111111111111pump"),
              },
              "closes": [_close("OLDCLOSE33333333333333333333333333333pump",
                                 1793200100)]}
    eng = FakeEngine(ledger)
    out = gs.maybe_entry_cards(eng, now=1793200200)
    check("backfill-sends-nothing", out == [] and sent == [])
    with open(journal_path) as f:
        journal = json.load(f)
    check("backfill-journals-known",
          "OLDOPEN11111111111111111111111111111111pump" in journal["sent"]
          and "OLDCLOSE33333333333333333333333333333pump" in journal["sent"])

    # 4. Fresh entry: sends exactly once, then dedups.
    ledger["positions"]["FRESH444444444444444444444444444444444pump"] = \
        _pos("FRESH444444444444444444444444444444444pump")
    out = gs.maybe_entry_cards(eng, now=1793200300)
    check("fresh-sends-once", out == ["FRESH444444444444444444444444444444444pump"]
          and len(sent) == 1, str(out))
    out = gs.maybe_entry_cards(eng, now=1793200400)
    check("fresh-dedups", out == [] and len(sent) == 1, str(out))

    # 5. Positions-only: opened AND closed between checks -> no card.
    ledger["closes"].append(
        _close("DEAD5555555555555555555555555555555555pump", 1793200500,
               entry_ts=1793200450))
    out = gs.maybe_entry_cards(eng, now=1793200600)
    check("closed-before-check-no-card", out == [] and len(sent) == 1,
          str(out))

    # 6. Send failure: unjournaled, retries next run.
    fail_next[0] = True
    ledger["positions"]["FAIL6666666666666666666666666666666666pump"] = \
        _pos("FAIL6666666666666666666666666666666666pump")
    out = gs.maybe_entry_cards(eng, now=1793200700)
    check("send-failure-no-card", out == [] and len(sent) == 1, str(out))
    fail_next[0] = False
    out = gs.maybe_entry_cards(eng, now=1793200800)
    check("send-failure-retries",
          out == ["FAIL6666666666666666666666666666666666pump"]
          and len(sent) == 2, str(out))

    # 7. Corrupt journal: fail-closed, nothing sends.
    with open(journal_path, "w") as f:
        f.write("{not json")
    ledger["positions"]["NEVER777777777777777777777777777777777pump"] = \
        _pos("NEVER777777777777777777777777777777777pump")
    out = gs.maybe_entry_cards(eng, now=1793200900)
    check("corrupt-journal-fail-closed", out == [] and len(sent) == 2,
          str(out))

    # 8. Throttle: two calls inside the window send at most once.
    os.remove(journal_path)
    gs._entry_last_run = 0
    ledger2 = {"positions": {
                   "THROTTLE8888888888888888888888888888888pump":
                       _pos("THROTTLE8888888888888888888888888888888pump")},
               "closes": []}
    eng2 = FakeEngine(ledger2)
    gs.maybe_entry_cards(eng2, now=1793201000)  # backfill run
    ledger2["positions"]["THROTTLE9999999999999999999999999999999pump"] = \
        _pos("THROTTLE9999999999999999999999999999999pump")
    n_before = len(sent)
    out1 = gs.maybe_entry_cards(eng2, now=1793201100)
    out2 = gs.maybe_entry_cards(eng2, now=1793201110)
    check("throttle-once", len(sent) - n_before == 1 and out2 == [],
          "out1=%s out2=%s" % (out1, out2))

    if FAILURES:
        print("FAILED: %s" % ", ".join(FAILURES))
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
