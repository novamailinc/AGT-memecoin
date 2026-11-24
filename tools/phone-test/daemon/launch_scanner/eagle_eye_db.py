#!/usr/bin/env python3
"""Eagle Eye database: SQLite (WAL mode) shared data layer.

SILENT data layer — no signals, no Telegram, no trading.
Scanner writes here; lanes (degen v2, steady, smart-watcher) read here.

Schema:
- tokens: tracked token registry with lifecycle state
- momentum: latest composite score per token (updated by scanner)
- snapshots: append-only price/volume history (pruned to 48h)
- runners: detected runner events
- heartbeat: scanner liveness (missing heartbeat = stall)

Lifecycle: NEW → WATCHLIST → CANDIDATE → COOLDOWN → DEAD → BLACKLISTED

Stages (3-engine ownership — each engine owns one stage, no coordination needed):
- 'new': owned by eagle-eye-discovery (new launches)
- 'trending': owned by eagle-eye-momentum (momentum tracking)
- 'established': owned by eagle-eye-smartflow (smart money flows)
"""
import json
import os
import random
import sqlite3
import time
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional

HOME = Path(os.environ.get("FOREX_AGENT_HOME", str(Path.home() / ".forex-agent")))
DB_PATH = HOME / "eagle_eye.db"

# Lifecycle states
NEW = "NEW"
WATCHLIST = "WATCHLIST"
CANDIDATE = "CANDIDATE"
COOLDOWN = "COOLDOWN"
DEAD = "DEAD"
BLACKLISTED = "BLACKLISTED"

# Stages (3-engine ownership)
STAGE_NEW = "new"           # owned by eagle-eye-discovery
STAGE_TRENDING = "trending"  # owned by eagle-eye-momentum
STAGE_ESTABLISHED = "established"  # owned by eagle-eye-smartflow

# Tiers
TIER_HOT = "hot"
TIER_WARM = "warm"
TIER_COLD = "cold"

# Engine IDs
ENGINE_DISCOVERY = "eagle-eye-discovery"
ENGINE_MOMENTUM = "eagle-eye-momentum"
ENGINE_SMARTFLOW = "eagle-eye-smartflow"
ALL_ENGINES = [ENGINE_DISCOVERY, ENGINE_MOMENTUM, ENGINE_SMARTFLOW]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY,
    symbol TEXT,
    name TEXT,
    pool_address TEXT,
    first_seen REAL,
    tier TEXT DEFAULT 'cold',
    stage TEXT DEFAULT 'new',
    state TEXT DEFAULT 'NEW',
    liq_usd REAL DEFAULT 0,
    fdv_usd REAL DEFAULT 0,
    score REAL DEFAULT 0,
    score_updated_at REAL DEFAULT 0,
    source TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS momentum (
    mint TEXT PRIMARY KEY,
    ts REAL,
    price_usd REAL,
    volume_m5 REAL DEFAULT 0,
    volume_m15 REAL DEFAULT 0,
    volume_h1 REAL DEFAULT 0,
    volume_h24 REAL DEFAULT 0,
    buys_m5 INTEGER DEFAULT 0,
    sells_m5 INTEGER DEFAULT 0,
    buyers_m5 INTEGER DEFAULT 0,
    sellers_m5 INTEGER DEFAULT 0,
    price_chg_m5 REAL DEFAULT 0,
    price_chg_m15 REAL DEFAULT 0,
    price_chg_h1 REAL DEFAULT 0,
    heat_score REAL DEFAULT 0,
    momentum_score REAL DEFAULT 0,
    buy_sell_ratio REAL DEFAULT 0,
    factors_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    ts REAL,
    price_usd REAL,
    vol_m5 REAL DEFAULT 0,
    buys_m5 INTEGER DEFAULT 0,
    sells_m5 INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_mint_ts ON snapshots(mint, ts);

CREATE TABLE IF NOT EXISTS runners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT,
    symbol TEXT,
    detected_ts REAL,
    trigger_score REAL,
    peak_multiple REAL DEFAULT 1.0,
    state TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts REAL,
    cycle_ms INTEGER DEFAULT 0,
    tokens_tracked INTEGER DEFAULT 0
);

-- Per-engine heartbeats (3-engine architecture)
CREATE TABLE IF NOT EXISTS engine_heartbeats (
    engine_id TEXT PRIMARY KEY,
    last_tick REAL,
    tokens_touched INTEGER DEFAULT 0,
    last_error TEXT DEFAULT '',
    cycle_ms INTEGER DEFAULT 0
);
"""

def get_db() -> sqlite3.Connection:
    """Get a WAL-mode connection."""
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # Migration: add stage column to existing DBs
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(tokens)")]
        if "stage" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN stage TEXT DEFAULT 'new'")
            conn.commit()
    except Exception:
        pass
    return conn


def with_retry(fn, max_retries: int = 5):
    """Retry SQLite writes with random jitter (breaks lock convoys)."""
    last = None
    for i in range(max_retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            last = e
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            time.sleep(random.uniform(0.02, 0.15))
    raise last

def upsert_token(conn: sqlite3.Connection, mint: str, symbol: str = "",
                 pool_address: str = "", liq_usd: float = 0,
                 fdv_usd: float = 0, source: str = "") -> None:
    now = time.time()
    conn.execute("""
        INSERT INTO tokens (mint, symbol, pool_address, first_seen, liq_usd, fdv_usd, source)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mint) DO UPDATE SET
            symbol=COALESCE(NULLIF(excluded.symbol,''), tokens.symbol),
            liq_usd=excluded.liq_usd,
            fdv_usd=excluded.fdv_usd
    """, (mint, symbol, pool_address, now, liq_usd, fdv_usd, source))
    conn.commit()

def update_momentum(conn: sqlite3.Connection, mint: str, data: Dict) -> None:
    """Update latest momentum snapshot for a token."""
    now = time.time()
    conn.execute("""
        INSERT INTO momentum (mint, ts, price_usd, volume_m5, volume_m15, volume_h1,
            volume_h24, buys_m5, sells_m5, buyers_m5, sellers_m5,
            price_chg_m5, price_chg_m15, price_chg_h1,
            heat_score, momentum_score, buy_sell_ratio, factors_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mint) DO UPDATE SET
            ts=excluded.ts, price_usd=excluded.price_usd,
            volume_m5=excluded.volume_m5, volume_m15=excluded.volume_m15,
            volume_h1=excluded.volume_h1, volume_h24=excluded.volume_h24,
            buys_m5=excluded.buys_m5, sells_m5=excluded.sells_m5,
            buyers_m5=excluded.buyers_m5, sellers_m5=excluded.sellers_m5,
            price_chg_m5=excluded.price_chg_m5, price_chg_m15=excluded.price_chg_m15,
            price_chg_h1=excluded.price_chg_h1, heat_score=excluded.heat_score,
            momentum_score=excluded.momentum_score, buy_sell_ratio=excluded.buy_sell_ratio,
            factors_json=excluded.factors_json
    """, (mint, now, data.get("price_usd", 0),
          data.get("volume_m5", 0), data.get("volume_m15", 0),
          data.get("volume_h1", 0), data.get("volume_h24", 0),
          data.get("buys_m5", 0), data.get("sells_m5", 0),
          data.get("buyers_m5", 0), data.get("sellers_m5", 0),
          data.get("price_chg_m5", 0), data.get("price_chg_m15", 0),
          data.get("price_chg_h1", 0),
          data.get("heat_score", 0), data.get("momentum_score", 0),
          data.get("buy_sell_ratio", 0),
          json.dumps(data.get("factors", {}))))
    # Also update token score
    conn.execute(
        "UPDATE tokens SET score=?, score_updated_at=? WHERE mint=?",
        (data.get("momentum_score", 0), now, mint))
    conn.commit()

def record_snapshot(conn: sqlite3.Connection, mint: str, price: float,
                    vol_m5: float, buys: int, sells: int) -> None:
    conn.execute(
        "INSERT INTO snapshots (mint, ts, price_usd, vol_m5, buys_m5, sells_m5)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (mint, time.time(), price, vol_m5, buys, sells))
    conn.commit()

def prune_snapshots(conn: sqlite3.Connection, max_age_h: float = 48) -> int:
    cur = conn.execute(
        "DELETE FROM snapshots WHERE ts < ?",
        (time.time() - max_age_h * 3600,))
    conn.commit()
    return cur.rowcount

def set_state(conn: sqlite3.Connection, mint: str, state: str) -> None:
    conn.execute("UPDATE tokens SET state=? WHERE mint=?", (state, mint))
    conn.commit()

def set_tier(conn: sqlite3.Connection, mint: str, tier: str) -> None:
    conn.execute("UPDATE tokens SET tier=? WHERE mint=?", (tier, mint))
    conn.commit()

def record_runner(conn: sqlite3.Connection, mint: str, symbol: str,
                  score: float) -> int:
    cur = conn.execute(
        "INSERT INTO runners (mint, symbol, detected_ts, trigger_score)"
        " VALUES (?, ?, ?, ?)",
        (mint, symbol, time.time(), score))
    conn.commit()
    # Move to COOLDOWN so it doesn't re-trigger
    set_state(conn, mint, COOLDOWN)
    return cur.lastrowid

def update_heartbeat(conn: sqlite3.Connection, cycle_ms: int,
                     tokens_tracked: int) -> None:
    conn.execute(
        "INSERT INTO heartbeat (id, ts, cycle_ms, tokens_tracked)"
        " VALUES (1, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET ts=excluded.ts,"
        " cycle_ms=excluded.cycle_ms, tokens_tracked=excluded.tokens_tracked",
        (time.time(), cycle_ms, tokens_tracked))
    conn.commit()


def update_engine_heartbeat(conn: sqlite3.Connection, engine_id: str,
                            tokens_touched: int, cycle_ms: int = 0,
                            last_error: str = "") -> None:
    """Per-engine heartbeat. Each engine calls this every tick."""
    def _write():
        conn.execute(
            "INSERT INTO engine_heartbeats"
            " (engine_id, last_tick, tokens_touched, last_error, cycle_ms)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(engine_id) DO UPDATE SET"
            " last_tick=excluded.last_tick,"
            " tokens_touched=excluded.tokens_touched,"
            " last_error=excluded.last_error,"
            " cycle_ms=excluded.cycle_ms",
            (engine_id, time.time(), tokens_touched, last_error, cycle_ms))
        conn.commit()
    with_retry(_write)


def get_engine_heartbeats(conn: sqlite3.Connection) -> List[Dict]:
    """All engine heartbeats. For the watcher."""
    cur = conn.execute("SELECT * FROM engine_heartbeats")
    return [dict(r) for r in cur.fetchall()]


def get_by_stage(conn: sqlite3.Connection, stage: str,
                 limit: int = 500) -> List[Dict]:
    """Tokens owned by a stage. Each engine polls its own stage."""
    cur = conn.execute(
        "SELECT mint, symbol, pool_address, state, tier, first_seen,"
        " liq_usd, fdv_usd, score FROM tokens WHERE stage=? LIMIT ?",
        (stage, limit))
    return [dict(r) for r in cur.fetchall()]


def promote_stage(conn: sqlite3.Connection, mint: str,
                  new_stage: str) -> None:
    """Promote a token to the next stage. Called by the owning engine."""
    def _write():
        conn.execute("UPDATE tokens SET stage=? WHERE mint=?",
                     (new_stage, mint))
        conn.commit()
    with_retry(_write)

# ---- Lane API (read-only, for trading lanes) ----

def get_hot_tokens(conn: sqlite3.Connection, limit: int = 50,
                   max_age_s: float = 120) -> List[Dict]:
    """Top momentum tokens with fresh data. For lanes."""
    cutoff = time.time() - max_age_s
    cur = conn.execute("""
        SELECT t.mint, t.symbol, t.state, t.tier,
               m.momentum_score, m.heat_score, m.buy_sell_ratio,
               m.price_usd, m.volume_m5, m.price_chg_m5,
               m.factors_json, m.ts
        FROM tokens t
        JOIN momentum m ON t.mint = m.mint
        WHERE m.ts > ? AND t.state IN ('WATCHLIST','CANDIDATE')
        ORDER BY m.momentum_score DESC
        LIMIT ?
    """, (cutoff, limit))
    return [dict(r) for r in cur.fetchall()]

def get_runners(conn: sqlite3.Connection, since_s: float = 3600) -> List[Dict]:
    """Recently detected runners. For lanes."""
    cur = conn.execute(
        "SELECT * FROM runners WHERE detected_ts > ? ORDER BY detected_ts DESC",
        (time.time() - since_s,))
    return [dict(r) for r in cur.fetchall()]

def get_momentum(conn: sqlite3.Connection, mint: str) -> Optional[Dict]:
    """Latest momentum for one token. For lanes."""
    cur = conn.execute("SELECT * FROM momentum WHERE mint=?", (mint,))
    r = cur.fetchone()
    return dict(r) if r else None

def get_tokens_by_tier(conn: sqlite3.Connection, tier: str) -> List[Dict]:
    """All tokens in a tier. For the scanner."""
    cur = conn.execute(
        "SELECT mint, symbol, pool_address, state FROM tokens WHERE tier=?",
        (tier,))
    return [dict(r) for r in cur.fetchall()]

def count_tokens(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) as n FROM tokens")
    return cur.fetchone()["n"]
