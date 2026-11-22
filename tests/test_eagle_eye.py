#!/usr/bin/env python3
"""Tests for Eagle Eye momentum detection engine."""
import os
import sys
import tempfile
import time

sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")

# Use temp DB for tests
_tmp = tempfile.mkdtemp()
os.environ["FOREX_AGENT_HOME"] = _tmp

import eagle_eye_db as db
from eagle_eye_score import (
    score_token, compute_heat, heat_band,
    score_volume_accel, score_buy_pressure, score_price_momentum,
    score_freshness, score_liquidity,
)

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)

# ---- Scoring ----
check("heat cold", heat_band(20) == "cold")
check("heat building", heat_band(40) == "building")
check("heat hot", heat_band(70) == "hot")
check("heat peak", heat_band(100) == "peak")
check("heat formula", abs(compute_heat(30, 100) - 30.0) < 0.1)
check("heat zero div", compute_heat(10, 0) == 0.0)

# Volume accel
s, _ = score_volume_accel(200, 100, 70)  # 2x RVOL, hot
check("rvol 2x scores", s > 10)
s2, _ = score_volume_accel(100, 100, 20)  # 1x, cold
check("rvol 1x low", s2 < 5)

# Buy pressure
s, _ = score_buy_pressure(20, 5, 15, 5)  # 4x ratio
check("strong b/s scores", s > 15)
s, _ = score_buy_pressure(10, 10, 8, 8)  # 1x ratio
check("weak b/s zero", s == 0.0)
s, _ = score_buy_pressure(2, 1, 2, 1)  # too few txns
check("few txns zero", s == 0.0)

# Price momentum
s, _ = score_price_momentum(10, 8, 5)
check("uptrend scores", s > 5)
s, _ = score_price_momentum(-5, -3, -1)
check("downtrend zero", s == 0.0)
s, r = score_price_momentum(80, 50, 30)  # extreme
check("extreme capped", s <= 5 and "caution" in r)

# Freshness
s, _ = score_freshness(10 * 60)  # 10 min
check("immature zero", s == 0.0)
s, _ = score_freshness(45 * 60)  # 45 min
check("peak zone max", s == 15.0)
s, _ = score_freshness(200 * 60)  # 200 min
check("decaying", 5 < s < 15)

# Liquidity
s, _ = score_liquidity(3000, 50000)
check("low liq zero", s == 0.0)
s, _ = score_liquidity(50000, 100000)
check("good liq scores", s > 5)

# Full token scoring
base = {
    "price_usd": 0.001, "volume_m5": 5000, "volume_m15": 12000,
    "volume_h1": 30000, "volume_h24": 200000,
    "baseline_m5": 2000, "vol_1m": 1500,
    "buys_m5": 30, "sells_m5": 10, "buyers_m5": 25, "sellers_m5": 8,
    "price_chg_m5": 8, "price_chg_m15": 12, "price_chg_h1": 20,
    "age_s": 3600, "liq_usd": 50000,
}
r = score_token(base)
check("strong token high score", r["score"] >= 60)
check("factors traceable", len(r["factors"]) == 5)
check("heat computed", r["heat"] > 0)

# Immature pool
r2 = score_token({**base, "age_s": 600})  # 10 min
check("immature pool zero", r2["score"] == 0.0)

# Already pumped (flat volume, high price)
r3 = score_token({**base, "volume_m5": 2100, "baseline_m5": 2000,
                  "price_chg_m5": 2, "price_chg_m15": 3, "price_chg_h1": 400})
check("pumped flat scores lower", r3["score"] < r["score"])

# ---- Database ----
conn = db.get_db()

# Token lifecycle
db.upsert_token(conn, "mint1", symbol="TKN1", liq_usd=20000, fdv_usd=500000)
t = conn.execute("SELECT * FROM tokens WHERE mint='mint1'").fetchone()
check("token inserted", t is not None and t["state"] == "NEW")
check("default tier cold", t["tier"] == "cold")

# Momentum update
db.update_momentum(conn, "mint1", {
    "price_usd": 0.001, "volume_m5": 5000, "momentum_score": 75.5,
    "heat_score": 60, "buy_sell_ratio": 2.0, "factors": {"a": 1},
})
m = db.get_momentum(conn, "mint1")
check("momentum stored", m is not None and m["momentum_score"] == 75.5)

# State transitions
db.set_state(conn, "mint1", db.WATCHLIST)
db.set_tier(conn, "mint1", "warm")
t = conn.execute("SELECT state, tier FROM tokens WHERE mint='mint1'").fetchone()
check("state set", t["state"] == "WATCHLIST")
check("tier set", t["tier"] == "warm")

# Lane API
hot = db.get_hot_tokens(conn, limit=10)
check("hot tokens found", len(hot) == 1 and hot[0]["mint"] == "mint1")

# Runner recording
rid = db.record_runner(conn, "mint1", "TKN1", 75.5)
check("runner recorded", rid > 0)
runners = db.get_runners(conn, since_s=3600)
check("runners listed", len(runners) == 1)
t = conn.execute("SELECT state FROM tokens WHERE mint='mint1'").fetchone()
check("runner → cooldown", t["state"] == "COOLDOWN")

# Snapshots + prune
db.record_snapshot(conn, "mint1", 0.001, 5000, 30, 10)
n = db.prune_snapshots(conn, max_age_h=0)  # prune all
check("snapshots pruned", n >= 1)

# Heartbeat
db.update_heartbeat(conn, 150, 42)
hb = conn.execute("SELECT * FROM heartbeat WHERE id=1").fetchone()
check("heartbeat written", hb["tokens_tracked"] == 42)

# Stale data filtered
db.upsert_token(conn, "mint2", symbol="OLD")
db.update_momentum(conn, "mint2", {"momentum_score": 99, "price_usd": 1})
# Manually age it
conn.execute("UPDATE momentum SET ts=? WHERE mint='mint2'",
             (time.time() - 9999,))
conn.commit()
db.set_state(conn, "mint2", db.WATCHLIST)
hot = db.get_hot_tokens(conn, limit=10, max_age_s=120)
check("stale filtered", all(h["mint"] != "mint2" for h in hot))

conn.close()

# ---- 3-engine architecture tests ----
_tmp2 = tempfile.mkdtemp()
os.environ["FOREX_AGENT_HOME"] = _tmp2
# Re-import to pick up new HOME (use fresh module namespace via importlib)
import importlib
db2 = importlib.reload(db)
conn2 = db2.get_db()

# Stage column defaults to 'new'
db2.upsert_token(conn2, "mA", symbol="TOKA")
t = conn2.execute("SELECT stage FROM tokens WHERE mint='mA'").fetchone()
check("default stage new", t["stage"] == "new")

# Stage ownership: each engine polls only its stage
db2.upsert_token(conn2, "mB", symbol="TOKB")
db2.upsert_token(conn2, "mC", symbol="TOKC")
conn2.execute("UPDATE tokens SET stage='trending' WHERE mint='mB'")
conn2.execute("UPDATE tokens SET stage='established' WHERE mint='mC'")
conn2.commit()
new_toks = db2.get_by_stage(conn2, "new")
trend_toks = db2.get_by_stage(conn2, "trending")
est_toks = db2.get_by_stage(conn2, "established")
check("stage new isolated", [x["mint"] for x in new_toks] == ["mA"])
check("stage trending isolated", [x["mint"] for x in trend_toks] == ["mB"])
check("stage established isolated",
      [x["mint"] for x in est_toks] == ["mC"])

# promote_stage
db2.promote_stage(conn2, "mA", db2.STAGE_TRENDING)
t = conn2.execute("SELECT stage FROM tokens WHERE mint='mA'").fetchone()
check("promote_stage works", t["stage"] == "trending")

# Per-engine heartbeats
db2.update_engine_heartbeat(conn2, db2.ENGINE_DISCOVERY, 10, 500, "")
db2.update_engine_heartbeat(conn2, db2.ENGINE_MOMENTUM, 25, 1200, "")
db2.update_engine_heartbeat(conn2, db2.ENGINE_SMARTFLOW, 5, 300, "test err")
beats = db2.get_engine_heartbeats(conn2)
check("3 heartbeats", len(beats) == 3)
ids = {b["engine_id"] for b in beats}
check("all engines", ids == set(db2.ALL_ENGINES))
sf = next(b for b in beats if b["engine_id"] == db2.ENGINE_SMARTFLOW)
check("heartbeat error stored", sf["last_error"] == "test err")
check("heartbeat tokens", sf["tokens_touched"] == 5)

# Heartbeat update is idempotent (upsert)
db2.update_engine_heartbeat(conn2, db2.ENGINE_DISCOVERY, 99, 100, "")
beats = db2.get_engine_heartbeats(conn2)
check("heartbeat upsert", len(beats) == 3)
d = next(b for b in beats if b["engine_id"] == db2.ENGINE_DISCOVERY)
check("heartbeat updated", d["tokens_touched"] == 99)

# with_retry: succeeds after transient lock
_calls = {"n": 0}
def _flaky():
    _calls["n"] += 1
    if _calls["n"] < 3:
        raise db2.sqlite3.OperationalError("database is locked")
    return "ok"
check("with_retry recovers", db2.with_retry(_flaky) == "ok")
check("with_retry tried 3x", _calls["n"] == 3)

# with_retry: raises non-lock errors immediately
def _bad():
    raise ValueError("boom")
try:
    db2.with_retry(_bad)
    check("with_retry non-lock raises", False)
except ValueError:
    check("with_retry non-lock raises", True)

# eagle_eye_api
sys.path.insert(0, "/home/hatch/workspace/Forex-agent/Forex-agent-main/daemon/launch_scanner")
os.environ["FOREX_AGENT_HOME"] = _tmp2  # api uses same HOME
import eagle_eye_api as api
check("api get_by_stage", len(api.get_by_stage("trending")) >= 1)
check("api momentum_confirms false (no data)",
      api.momentum_confirms("mA") is False)
# Fresh high score → confirms
db2.update_momentum(conn2, "mA", {
    "price_usd": 0.01, "momentum_score": 80, "heat_score": 70,
    "buy_sell_ratio": 2.0, "factors": {}})
db2.set_state(conn2, "mA", db2.WATCHLIST)
check("api momentum_confirms true",
      api.momentum_confirms("mA", min_score=60) is True)
check("api momentum_confirms bar",
      api.momentum_confirms("mA", min_score=90) is False)
# Stale → False
conn2.execute("UPDATE momentum SET ts=? WHERE mint='mA'",
              (time.time() - 9999,))
conn2.commit()
check("api stale no confirm",
      api.momentum_confirms("mA", min_score=60, max_age_s=300) is False)

health = api.engine_health()
check("api engine_health 3 engines", len(health) == 3)
check("api discovery alive", health[db2.ENGINE_DISCOVERY]["alive"] is True)

conn2.close()

print(f"\n{len(fails)} failures")
sys.exit(1 if fails else 0)
