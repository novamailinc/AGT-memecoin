"""One durable Helius account hold. Success never shortens an observed hold."""
import os
import sqlite3
import time
from pathlib import Path


def _db(home):
    root=Path(home or os.environ.get('FOREX_AGENT_HOME') or Path.home()/'.forex-agent')
    root.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(str(root/'helius_account_hold.sqlite3'),timeout=5)
    db.execute('PRAGMA synchronous=FULL')
    db.execute('CREATE TABLE IF NOT EXISTS hold (id INTEGER PRIMARY KEY,until REAL NOT NULL)')
    return db


def allowed(home=None,now=None):
    from .usage_tracker import UsageTracker
    if not UsageTracker(home=home).nonessential_allowed('helius_main','card')[0]:return False
    with _db(home) as db:
        row=db.execute('SELECT until FROM hold WHERE id=1').fetchone()
    return not row or (time.time() if now is None else now)>=row[0]


def throttled(home=None,wait=None,monthly=False,now=None):
    import math
    from .usage_tracker import UsageTracker
    if monthly:UsageTracker(home=home).record_monthly_quota('helius_main')
    try:delay=float(wait)
    except (TypeError,ValueError):delay=300.
    if not math.isfinite(delay) or delay<0:delay=300.
    until=(time.time() if now is None else now)+max(30.,delay)
    with _db(home) as db:
        db.execute('INSERT INTO hold VALUES (1,?) ON CONFLICT(id) DO UPDATE SET until=max(until,excluded.until)',(until,))
