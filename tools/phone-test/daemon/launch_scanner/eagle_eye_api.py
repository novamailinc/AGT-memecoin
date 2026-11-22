#!/usr/bin/env python3
"""Eagle Eye lane API: read-only access for trading lanes.

Lanes (degen v2, steady, smart-watcher) use these functions to query
the shared Eagle Eye database. NEVER writes. NEVER signals.

Example:
    from eagle_eye_api import get_hot_tokens, momentum_confirms

    hot = get_hot_tokens(limit=10)  # top momentum, fresh data
    if momentum_confirms(mint, min_score=60):
        # lane's own entry logic decides
        ...
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eagle_eye_db as db


def _conn():
    return db.get_db()


def get_hot_tokens(limit: int = 50, max_age_s: float = 120):
    """Top momentum tokens with fresh data."""
    conn = _conn()
    try:
        return db.get_hot_tokens(conn, limit=limit, max_age_s=max_age_s)
    finally:
        conn.close()


def get_runners(since_s: float = 3600):
    """Runners detected in the last `since_s` seconds."""
    conn = _conn()
    try:
        return db.get_runners(conn, since_s=since_s)
    finally:
        conn.close()


def get_momentum(mint: str):
    """Latest momentum row for one token, or None."""
    conn = _conn()
    try:
        return db.get_momentum(conn, mint)
    finally:
        conn.close()


def get_by_stage(stage: str, limit: int = 500):
    """Tokens in a lifecycle stage."""
    conn = _conn()
    try:
        return db.get_by_stage(conn, stage, limit=limit)
    finally:
        conn.close()


def momentum_confirms(mint: str, min_score: float = 60,
                      max_age_s: float = 300) -> bool:
    """Does Eagle Eye momentum confirm this mint? For lane entry logic.

    Returns True if the token has a fresh momentum score >= min_score.
    Lanes use this as ONE input among their own criteria.
    """
    m = get_momentum(mint)
    if not m:
        return False
    if time.time() - (m.get("ts") or 0) > max_age_s:
        return False  # stale
    return (m.get("momentum_score") or 0) >= min_score


def engine_health():
    """Per-engine heartbeat status. For monitoring."""
    conn = _conn()
    try:
        beats = db.get_engine_heartbeats(conn)
        now = time.time()
        # Expected cadence per engine (seconds)
        expected = {
            db.ENGINE_DISCOVERY: 60,
            db.ENGINE_MOMENTUM: 120,
            db.ENGINE_SMARTFLOW: 400,
        }
        out = {}
        for e in db.ALL_ENGINES:
            row = next((b for b in beats if b["engine_id"] == e), None)
            if not row:
                out[e] = {"alive": False, "reason": "no heartbeat yet"}
                continue
            age = now - (row["last_tick"] or 0)
            alive = age < expected[e] * 3
            out[e] = {
                "alive": alive,
                "age_s": round(age, 1),
                "tokens_touched": row["tokens_touched"],
                "last_error": row["last_error"],
            }
        return out
    finally:
        conn.close()
