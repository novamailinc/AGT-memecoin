"""Exit-liquidity clock (measurement only, safe batch 2026-10-09).

For every closed paper position: how long did the run last, where was
the peak, and how fast did it give it back?

Per close it records: hold time, close multiple, peak multiple,
time-to-peak, and dump speed (multiples lost per hour from peak to
close). The dataset lives in runtime state (``<home>/launch_scanner/
exit_clock.json``, 0600, atomic writes), bounded (1000 records) with a
7-day TTL — mirroring the proof/footprint stores.

MEASUREMENT ONLY. Two observation hooks, zero behavior change:
  - note_price(mint, price_sol, ts): called from the exit loop for open
    positions so time-to-peak is measured. In-memory only, bounded to
    ~200 mints; a restart just means "unknown" peak timing for the
    closes that follow (recorded honestly, never guessed).
  - record_close(...): called once per close, next to the existing
    narrative/backtest close recorders. Never raises, never gates,
    never touches exits, entries, caps, or sizing.

The weekly review answers: median time-to-peak, % of winners that
round-tripped (peak >= 2x but closed <= 1x), median dump speed.
Honest zeros when the dataset is empty.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("exit_clock")

_SUBDIR = "launch_scanner"
_FILENAME = "exit_clock.json"
_TTL_S = 7 * 24 * 3600
_MAX_RECORDS = 1000
_MAX_PEAKS = 200  # in-memory peak watch, open positions only

# mint -> [peak_px, peak_ts]; process-local only.
_PEAKS: Dict[str, List[float]] = {}


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if v == v else default  # NaN -> default


def _testing() -> bool:
    return os.environ.get("FOREX_AGENT_TESTING") == "1"


def _resolve_path(path: Optional[str]) -> Optional[str]:
    if path is not None:
        return str(path)
    if _testing():
        return None
    try:
        from daemon.common import get_home  # noqa: PLC0415
        return os.path.join(get_home(), _SUBDIR, _FILENAME)
    except Exception:
        logger.exception("exit_clock: could not resolve home")
        return None


def _validate_records(records):
    from . import learning_effects
    learning_effects._validate(records)
    if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
        raise ValueError('exit clock records unavailable')
    return records


def _all_records(path):
    from . import learning_effects as effects
    try:doc=json.loads(open(path).read(),object_pairs_hook=effects._pairs,parse_constant=effects._constant)
    except FileNotFoundError:return []
    if isinstance(doc,dict):
        # Primitive validates envelope including receipts on an exact read retry;
        # reader must not treat an envelope as an empty legacy list.
        if set(doc)!={'version','value','effects'} or type(doc['version']) is not int or doc['version']!=1:
            raise ValueError('exit clock envelope unavailable')
        receipts=doc['effects']
        if not isinstance(receipts,dict) or any(not isinstance(k,str) or not k or not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for k,v in receipts.items()):
            raise ValueError('exit clock receipts unavailable')
        doc=doc['value']
    return _validate_records(doc)


def _validate_record(record):
    import math
    if not isinstance(record,dict):raise ValueError('exit clock record must be a map')
    required=('close_ts','hold_h','close_mult','peak_mult','pnl_sol')
    for field in required:
        if type(record.get(field)) not in (int,float) or not math.isfinite(record[field]):raise ValueError('exit clock finite field required')
    if not isinstance(record.get('mint'),str) or not record['mint']:raise ValueError('exit clock mint required')
    for field in ('time_to_peak_h','dump_mult_per_h'):
        v=record.get(field)
        if v is not None and (type(v) not in (int,float) or not math.isfinite(v)):raise ValueError('exit clock optional finite field required')
    return record


def append_record(path,record,effect_id):
    from . import learning_effects
    from pathlib import Path
    _validate_record(record)
    p=Path(path)
    if not p.is_absolute():raise ValueError('exit clock path must be absolute')
    p.parent.mkdir(parents=True,exist_ok=True)
    def reducer(records,payload):
        _validate_records(records)
        _validate_record(payload)
        candidates=records+[payload]
        watermark=max(_f(r.get('close_ts')) for r in candidates)
        cutoff=watermark-_TTL_S
        result=[r for r in candidates if _f(r.get('close_ts'))>=cutoff]
        result.sort(key=lambda r:(_f(r.get('close_ts')),json.dumps(r,sort_keys=True,separators=(',',':'))))
        return _validate_records(result[-_MAX_RECORDS:])
    return learning_effects.apply(p,effect_id,record,reducer,[],legacy_import=_validate_records)


def _load(path: Optional[str]) -> List[Dict[str, Any]]:
    p = _resolve_path(path)
    if not p:
        return []
    try:data=_all_records(p)
    except Exception:
        logger.exception('exit clock read unavailable')
        return []
    cutoff = time.time() - _TTL_S
    return [r for r in data
            if isinstance(r, dict) and _f(r.get("close_ts")) >= cutoff]


def note_price(mint: str, price_sol: float, ts: float) -> None:
    """Observe an open position's price (exit-loop hook).

    Tracks the peak price + its timestamp per mint so record_close can
    compute time-to-peak. Pure observation — never raises, never
    affects the exit loop.
    """
    try:
        if not mint:
            return
        px = _f(price_sol)
        if px <= 0:
            return
        cur = _PEAKS.get(str(mint))
        if cur is None or px > cur[0]:
            _PEAKS[str(mint)] = [px, float(ts)]
        if len(_PEAKS) > _MAX_PEAKS:
            # Drop the stalest peak; open-position peaks get refreshed
            # every cycle, so anything stale is a dead entry.
            oldest = min(_PEAKS, key=lambda k: _PEAKS[k][1])
            _PEAKS.pop(oldest, None)
    except Exception:
        pass  # observation must never break the exit loop


def record_close(degen: Any, mint: str, entry_ts: float, entry_px: float,
                 close_ts: float, close_px: float, peak_px: float,
                 reason: str, pnl_sol: float, label: str = "",
                 path: Optional[str] = None, effect_id=None,
                 record_only=False, pinned_peak=None, use_pinned_peak=False) -> Optional[Dict[str, Any]]:
    """Record one closed paper position's full run shape.

    Measurement only. Returns the record, or None when disabled /
    invalid. Never raises.
    """
    try:
        if not getattr(degen, "exit_clock_enabled", True):
            return None
        import math
        if not all(type(v) in (int,float) and math.isfinite(v) for v in (entry_ts,entry_px,close_ts,close_px,peak_px,pnl_sol)):
            return None
        if not mint or entry_px <= 0 or close_px < 0:
            return None
        mint = str(mint)
        close_mult = close_px / entry_px
        peak_mult = _f(peak_px) / entry_px if _f(peak_px) > 0 else close_mult
        hold_h = max(0.0, (float(close_ts) - float(entry_ts)) / 3600.0)

        # Time-to-peak from the in-memory peak watch; unknown (None)
        # when the watch never saw this mint (e.g. daemon restarted
        # mid-position) — recorded distinctly, never guessed.
        peak_info = pinned_peak if use_pinned_peak else (_PEAKS.get(mint) if record_only else _PEAKS.pop(mint, None))
        time_to_peak_h: Optional[float] = None
        dump_per_h: Optional[float] = None
        if peak_info and peak_info[1] >= float(entry_ts):
            time_to_peak_h = max(
                0.0, (peak_info[1] - float(entry_ts)) / 3600.0)
            decay_h = max(1.0 / 3600.0,
                          (float(close_ts) - peak_info[1]) / 3600.0)
            dump_per_h = max(0.0, peak_mult - close_mult) / decay_h

        rec = {
            "close_ts": float(close_ts),
            "mint": mint,
            "label": str(label or ""),
            "reason": str(reason or ""),
            "hold_h": round(hold_h, 4),
            "close_mult": round(close_mult, 4),
            "peak_mult": round(peak_mult, 4),
            "time_to_peak_h": (round(time_to_peak_h, 4)
                               if time_to_peak_h is not None else None),
            "dump_mult_per_h": (round(dump_per_h, 4)
                                 if dump_per_h is not None else None),
            "pnl_sol": round(float(pnl_sol or 0.0), 6),
        }
        _validate_record(rec)
        if record_only:return rec
        p=_resolve_path(path)
        if p:
            import uuid
            append_record(p,rec,effect_id or 'legacy-'+uuid.uuid4().hex)
        return rec
    except Exception:
        logger.exception("exit_clock record_close failed for %s", mint)
        return None


def _median(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def weekly_section(path: Optional[str] = None) -> List[str]:
    """Weekly review section: median time-to-peak, round-trip rate,
    median dump speed. Honest zeros on an empty dataset."""
    out = ["== EXIT-LIQUIDITY CLOCK (last 7 days) =="]
    try:
        records = _load(path)
    except Exception:
        out.append("exit-clock data unavailable (read failed).")
        return out
    if not records:
        out.append("no closed positions recorded this week.")
        return out
    n = len(records)
    ttp = [r["time_to_peak_h"] for r in records
           if isinstance(r.get("time_to_peak_h"), (int, float))]
    med_ttp = _median(ttp)
    winners = [r for r in records if _f(r.get("peak_mult")) >= 2.0]
    roundtrips = [r for r in winners if _f(r.get("close_mult")) <= 1.0]
    dumps = [r["dump_mult_per_h"] for r in records
             if isinstance(r.get("dump_mult_per_h"), (int, float))]
    med_dump = _median(dumps)
    out.append("closes: %d | winners (peak >= 2x): %d" % (n, len(winners)))
    out.append("median time-to-peak: %s"
               % ("%.2fh (n=%d)" % (med_ttp, len(ttp))
                  if med_ttp is not None else "n/a (peak watch restarted)"))
    out.append("round-tripped (peak >= 2x, closed <= 1x): %d/%d (%.0f%%)"
               % (len(roundtrips), len(winners),
                  100.0 * len(roundtrips) / max(1, len(winners))))
    out.append("median dump speed: %s"
               % ("%.2f mult/h (n=%d)" % (med_dump, len(dumps))
                  if med_dump is not None else "n/a"))
    return out
