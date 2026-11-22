#!/usr/bin/env python3
"""Self-test: prove the validator finds a real injected edge and rejects noise.

1. KNOWN EDGE: hour 14 has a true +0.40% mean (noise std 1%). Must be
   discovered on train AND confirmed on test.
2. PURE NOISE: zero-mean noise everywhere. Must discover nothing
   (Bonferroni must hold the line against the multiple-comparisons trap).
3. KELLY: sanity-check the sizing math on known inputs.
"""
import datetime
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_data
import validator

N_CANDLES = 17520  # ~2 years hourly
START_MS = int(datetime.datetime(2024, 1, 1,
                                 tzinfo=datetime.timezone.utc).timestamp() * 1000)


def synth_rows(edge_hour=None, edge_mean=0.004, seed=11):
    rng = random.Random(seed)
    rows = []
    for i in range(N_CANDLES):
        ms = START_MS + i * 3600_000
        dt = datetime.datetime.fromtimestamp(ms / 1000.0,
                                             tz=datetime.timezone.utc)
        mean = edge_mean if (edge_hour is not None and dt.hour == edge_hour) else 0.0
        r = rng.gauss(mean, 0.01)
        o, c = 100.0, 100.0 * (1.0 + r)
        rows.append((ms, o, max(o, c), min(o, c), c, 1.0))
    return rows


def run_backtest_on(rows, symbol):
    orig = fetch_data.load_series
    fetch_data.load_series = lambda s, i="1h": rows if s == symbol else (_ for _ in ()).throw(
        RuntimeError("unexpected symbol in selftest"))
    # BTCUSDT path must not trigger beta-strip for our synthetic symbol
    try:
        return validator.backtest(symbol)
    finally:
        fetch_data.load_series = orig


def test_known_edge():
    rows = synth_rows(edge_hour=14, seed=11)
    res = run_backtest_on(rows, "BTCUSDT")
    names = [d["candidate"] for d in res["discovered_on_train"]]
    assert "hour_14_long" in names, f"FAILED to discover injected edge: {names}"
    confirmed = [v["candidate"] for v in res["validated_on_test"] if v["confirmed"]]
    assert "hour_14_long" in confirmed, \
        f"injected edge not confirmed on test: {confirmed}"
    # no short-side nonsense on the edge hour
    assert "hour_14_short" not in names, "spurious short discovery on edge hour"
    print("PASS: known edge (hour 14 long) discovered on train, confirmed on test")


def test_pure_noise():
    rows = synth_rows(edge_hour=None, seed=12)
    res = run_backtest_on(rows, "BTCUSDT")
    n_disc = len(res["discovered_on_train"])
    assert n_disc == 0, f"FALSE DISCOVERY on pure noise: " \
        f"{[d['candidate'] for d in res['discovered_on_train']]}"
    print("PASS: pure noise -> zero discoveries (Bonferroni held)")


def test_kelly():
    f = validator.kelly_fraction(0.6, 0.02, 0.01)  # p=.6, b=2
    assert abs(f - 0.4) < 1e-9, f"kelly wrong: {f}"  # (0.6*2-0.4)/2 = 0.4
    assert validator.kelly_fraction(0.4, 0.01, 0.02) is None  # negative edge
    assert validator.kelly_fraction(0.5, 0.01, 0.0) is None   # no losses
    print("PASS: Kelly math correct (0.4 on 60%/2:1, None on no-edge)")


def main():
    validator.N_PERMUTATIONS = 100  # speed up self-test; real runs use 500
    test_known_edge()
    test_pure_noise()
    test_kelly()
    print("ALL SELF-TESTS PASSED")


if __name__ == "__main__":
    main()
