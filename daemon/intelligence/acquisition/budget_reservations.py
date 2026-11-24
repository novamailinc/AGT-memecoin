"""Conservative durable pre-I/O cost reservations, not provider billing.

Reservations are never released, including after successful post-call usage
recording. That double-counts completed work against the legacy counters but
avoids an unsafe cross-store release window. Unknown metered costs refuse.
Local reset/loss and provider-side/external account spend remain outside proof.
"""
import math
import os
import sqlite3
import time
import uuid
from pathlib import Path
from .provider_usage import budget_for,daily_budget_for,feature_budget_for,feature_for


def reserve(tracker,account,provider,workload,cost):
    """Return (admitted, reason, reservation_id), before any provider I/O.

    Shared SQLite write lock serializes checks and reservation inserts across
    processes using this runtime root. No refund/retry of an uncertain request.
    """
    monthly=budget_for(account)['amount']
    daily=daily_budget_for(account)
    feature=feature_for(provider,workload)
    feature_limit=feature_budget_for(account,feature)
    if monthly is None and daily is None and feature_limit is None:
        return True,'unmetered_or_unenforced',None
    if type(cost) not in (int,float) or not math.isfinite(cost) or cost<0:
        return False,'unknown_projected_cost',None
    db=None
    try:
        home=Path(tracker.home or os.environ.get('FOREX_AGENT_HOME') or Path.home()/'.forex-agent')
        home.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(str(home/'acquisition_budget_reservations.sqlite3'),timeout=.1)
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY,account TEXT NOT NULL,feature TEXT NOT NULL,month TEXT NOT NULL,day TEXT NOT NULL,cost REAL NOT NULL,created REAL NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        import re,datetime
        for rid,owner,feat,mon,day_key,amount,created in db.execute('SELECT id,account,feature,month,day,cost,created FROM reservations'):
            if not isinstance(rid,str) or not re.fullmatch(r'budget_[0-9a-f]{32}',rid):raise ValueError('reservation identity invalid')
            if any(not isinstance(value,str) or not re.fullmatch(r'[a-z][a-z0-9_]*',value) for value in (owner,feat)):
                raise ValueError('reservation attribution invalid')
            if any(type(value) not in (int,float) or not math.isfinite(value) or value<0 for value in (amount,created)) or created<=0:
                raise ValueError('reservation numeric evidence invalid')
            stamp=datetime.datetime.fromtimestamp(created,datetime.timezone.utc)
            if mon!=stamp.strftime('%Y-%m') or day_key!=stamp.strftime('%Y-%m-%d'):
                raise ValueError('reservation window identity invalid')
        tracker.store.validate_admission_state(account)
        if tracker.account_disabled(account):return False,'account_disabled',None
        now=time.time();month=time.strftime('%Y-%m',time.gmtime(now));day=time.strftime('%Y-%m-%d',time.gmtime(now))
        rec=tracker.store.get(account)
        # Existing legacy counters may be stale, but treating them as spent is
        # conservative. This version does not reset or migrate that evidence.
        held=db.execute('SELECT COALESCE(sum(cost),0) FROM reservations WHERE account=? AND month=?',(account,month)).fetchone()[0]
        held_day=db.execute('SELECT COALESCE(sum(cost),0) FROM reservations WHERE account=? AND day=?',(account,day)).fetchone()[0]
        held_feature=db.execute('SELECT COALESCE(sum(cost),0) FROM reservations WHERE account=? AND feature=? AND month=?',(account,feature,month)).fetchone()[0]
        spent=rec.get('cost_spent',0);spent_day=rec.get('daily_cost_spent',0);spent_feature=(rec.get('by_feature',{}).get(feature) or {}).get('cost',0)
        for limit,used,reserved,label in ((monthly,spent,held,'monthly'),(daily,spent_day,held_day,'daily'),(feature_limit,spent_feature,held_feature,'feature')):
            if limit is not None and used+reserved+cost>limit:return False,'projected_%s_budget_exhausted'%label,None
        rid='budget_'+uuid.uuid4().hex
        db.execute('INSERT INTO reservations VALUES (?,?,?,?,?,?,?)',(rid,account,feature,month,day,cost,now))
        db.commit()
        os.chmod(home/'acquisition_budget_reservations.sqlite3',0o600)
        fd=os.open(str(home),os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
        return True,'retained_conservative_reservation',rid
    except (OSError,ValueError,TypeError,OverflowError,sqlite3.Error):
        return False,'budget_reservation_unavailable',None
    finally:
        if db is not None:db.close()
