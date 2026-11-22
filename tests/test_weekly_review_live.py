"""Plain-script tests for the weekly review's live-ledger paper section.

Run from the repo root:
    ~/workspace/Forex-agent/.venv/bin/python tests/test_weekly_review_live.py

Covers the 2026-11-02 repoint: paper closes come from the live lane
ledgers (USD), capped lanes state their stored window, and an empty
week never claims the bot opened no qualifying positions.
"""

import json
import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from daemon.launch_scanner import weekly_review as wr  # noqa: E402

NOW = 1793634000.0  # 2026-11-02 ~16:40 WAT (fixed clock for the tests)


def _write_ledger(tmp, lane, closes):
    with open(os.path.join(tmp, "%s_ledger.json" % lane), "w") as fh:
        json.dump({"closes": closes}, fh)


def _close(ts, mult, pnl, reason="hard_stop_-50pct", symbol="TST",
           lane="grad_select"):
    return {"close_ts": ts, "realized_mult": mult, "pnl_usd": pnl,
            "closed_reason": reason, "symbol": symbol, "lane": lane,
            "mint": "Mint%s" % symbol, "entry_notional_usd": 25.0,
            "realized_usd": 25.0 * mult}


def test_loader_adapts_and_filters():
    tmp = tempfile.mkdtemp()
    _write_ledger(tmp, "grad_select", [
        _close(NOW - 86400, 2.0, 25.0),          # in week, win
        _close(NOW - 86400, 0.5, -12.5),         # in week, loss
        _close(NOW - 10 * 86400, 3.0, 50.0),     # outside week
    ])
    _write_ledger(tmp, "grad_raw", [])
    # momentum/tranche/tranche_protect/raw_tight ledgers missing on purpose
    recs, coverage, missing = wr._live_ledger_closes(home=tmp)
    assert len(recs) == 3, recs
    r = recs[0]
    assert r["t_close"] == NOW - 86400 and r["pnl_usd"] == 25.0
    assert r["exit_reason"] == "hard_stop_-50pct" and r["lane"] == "grad_select"
    assert coverage["grad_select"][0] == 3
    assert coverage["grad_raw"][0] == 0
    assert set(missing) == {"tranche", "tranche_protect", "raw_tight",
                            "momentum"}, missing
    print("PASS loader adapts + missing ledgers reported, not fatal")


def test_loader_fallbacks():
    tmp = tempfile.mkdtemp()
    # no realized_mult / pnl_usd: derive from realized_usd + notional
    _write_ledger(tmp, "grad_select", [
        {"close_ts": NOW - 100, "realized_usd": 50.0,
         "entry_notional_usd": 25.0, "closed_reason": "ladder_complete",
         "symbol": "FB", "mint": "MintFB"},
        {"symbol": "NO_TS"},  # skipped: no close time
    ])
    recs, coverage, _ = wr._live_ledger_closes(home=tmp)
    assert len(recs) == 1, recs
    assert recs[0]["realized_mult"] == 2.0 and recs[0]["pnl_usd"] == 25.0
    assert coverage["grad_select"][3] == 1  # skipped
    print("PASS loader fallbacks + skip counting")


def test_empty_week_never_claims_no_positions():
    out = wr._paper_section_live([], {"grad_select": (500, NOW - 86400,
                                     NOW, 0)}, [])
    text = "\n".join(out)
    assert "No closes from the live lane ledgers fell in this window." in text
    assert "opened no qualifying positions" not in text
    assert "No paper positions closed this week." not in text
    assert "coverage: grad_select stored closes start" in text
    print("PASS empty week uses coverage-gap wording, not the old claim")


def test_nonempty_week_usd_and_by_lane():
    papers = [_close(NOW - 100, 2.0, 25.0),
              _close(NOW - 100, 0.5, -12.5),
              _close(NOW - 100, 1.0, 0.0, lane="tranche")]
    out = wr._paper_section_live(
        papers, {"grad_select": (2, NOW - 200, NOW, 0),
                 "tranche": (1, NOW - 200, NOW, 0)}, [])
    text = "\n".join(out)
    assert "closes: 3 | wins(>=1.5x): 1 | losses(<1x): 1 | flat: 1" in text
    assert "USD (stored closes only)" in text
    assert "SOL" not in text
    assert "by lane (trades, win rate, avg x):" in text
    assert "grad_select: 2 trades" in text and "tranche: 1 trades" in text
    assert "by narrative" not in text and "safety" not in text
    print("PASS nonempty week: USD, by-lane, no narrative/safety fields")


def test_parse_as_of():
    assert wr._parse_as_of([]) <= time.time() + 5
    assert wr._parse_as_of(["--as-of", "1793634000"]) == 1793634000.0
    assert wr._parse_as_of(["--as-of", "bogus"]) <= time.time() + 5
    print("PASS --as-of parsing (epoch, garbage falls back to now)")


if __name__ == "__main__":
    test_loader_adapts_and_filters()
    test_loader_fallbacks()
    test_empty_week_never_claims_no_positions()
    test_nonempty_week_usd_and_by_lane()
    test_parse_as_of()
    print("ALL LIVE-LEDGER WEEKLY-REVIEW TESTS PASSED")
