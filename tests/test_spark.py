#!/usr/bin/env python3
"""Tests for the Spark ignition detector."""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "daemon", "launch_scanner"))

import spark


def _tok(**kw):
    base = {
        "age_sec": 90,
        "holder_velocity": 12.0,
        "holder_accel": 2.0,
        "buy_sell_ratio": 3.0,
        "wash_flag": False,
        "top10_pct": 25.0,
    }
    base.update(kw)
    return base


def test_ignition_fires_on_strong_signals():
    ok, reasons, metrics = spark.detect_ignition(_tok())
    assert ok, f"should ignite: {reasons}"
    assert len(reasons) >= 3
    assert metrics["holder_velocity"] == 12.0


def test_ignition_rejects_low_velocity():
    ok, _, _ = spark.detect_ignition(_tok(holder_velocity=3.0))
    assert not ok


def test_ignition_rejects_low_buy_pressure():
    ok, _, _ = spark.detect_ignition(_tok(buy_sell_ratio=1.2))
    assert not ok


def test_ignition_rejects_flat_accel_unless_very_high():
    # Flat accel + moderate velocity = no ignition
    ok, _, _ = spark.detect_ignition(_tok(holder_accel=0, holder_velocity=10.0))
    assert not ok
    # Flat accel + very high velocity = ignition (strong override)
    ok, _, _ = spark.detect_ignition(_tok(holder_accel=0, holder_velocity=20.0))
    assert ok


def test_ignition_wash_veto():
    ok, reasons, _ = spark.detect_ignition(_tok(wash_flag=True))
    assert not ok
    assert any("wash" in r for r in reasons)


def test_bundle_veto():
    # Coordinated early buying = dump setup, vetoed
    ok, reasons, _ = spark.detect_ignition(_tok(top10_pct=55.0))
    assert not ok
    assert any("bundle veto" in r for r in reasons)


def test_bundle_veto_unknown_ok():
    # Unknown concentration does not veto (fail-open on missing data)
    vetoed, _ = spark.bundle_veto(None)
    assert not vetoed


def test_age_window():
    ok, _, _ = spark.detect_ignition(_tok(age_sec=10))   # too young
    assert not ok
    ok, _, _ = spark.detect_ignition(_tok(age_sec=300))  # too old
    assert not ok
    ok, _, _ = spark.detect_ignition(_tok(age_sec=90))   # in window
    assert ok


def test_signal_ttl():
    # Record and retrieve
    spark.record_spark("TESTMINT", "TEST", ["r1"], {"v": 1})
    sig = spark.get_spark("TESTMINT")
    assert sig is not None
    assert sig["symbol"] == "TEST"
    # Expired signals return None (monkey-patch ts)
    state = spark._load_state()
    state["TESTMINT"]["ts"] = time.time() - spark.SPARK_TTL_SEC - 1
    spark._save_state(state)
    assert spark.get_spark("TESTMINT") is None
    spark.clear_spark("TESTMINT")


def test_spark_stats():
    spark.record_spark("STATMINT", "STAT", ["r"], {})
    st = spark.spark_stats()
    assert st["active_signals"] >= 1
    spark.clear_spark("STATMINT")
    spark.clear_spark("TESTMINT")
