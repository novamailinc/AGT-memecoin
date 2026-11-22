"""Point-in-time integrity helpers (TradingAgents #7 stretch, 2026-11-17).

Tuning research must never learn from forward-test outcomes: any
training set used to calibrate a threshold or score must end strictly
BEFORE the forward window starts. These stdlib-only helpers are the
single choke point for that rule, shared by the research scripts
(``scripts/bear-calibrate``, ``scripts/ab-forward-report``).

Rules enforced here:

* ``assert_training_before_forward`` — training_end must be strictly
  less than forward_start. Equality is a violation: a close resolved
  exactly at the window start is a forward outcome, not training data.
* ``split_training_before_forward`` — a stored close is training data
  only when its ``close_ts`` is present AND < forward_start. Closes
  at/after the start are excluded, and undated closes (no usable
  ``close_ts``) are excluded too — fail-closed, because an unplaceable
  close cannot be proven to precede the window. (All 3,333 stored
  closes on 2026-11-17 carry ``close_ts``, so this costs nothing live.)
* ``resolved_at`` stamping — every generated lesson already carries
  ``resolved_at`` (the ``close_ts`` of the newest settled decision it
  is based on; see ``lessons.generate_lessons``). ``ensure_resolved_at``
  stamps it on a verdict/lesson record only where it is missing and
  NEVER overwrites an existing value — history is never rewritten.
  The verdict chain's resolved_at source is documented, not rebuilt:
  every event in ``~/.forex-agent/verdict_ledger/events.jsonl``
  carries ``ts`` (verified 2026-11-17: 9,100/9,100 events); that ``ts``
  IS the event's resolution timestamp — see ``verdict_resolved_at``.

Paper only. No network, no writes to ledgers or the verdict chain.
"""
import json
import os
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
    _WAT = ZoneInfo("Africa/Lagos")
except Exception:  # pragma: no cover — zoneinfo is stdlib 3.9+
    _WAT = timezone.utc

FORWARD_STATE_FILE = "ab_forward.json"


class PointInTimeError(ValueError):
    """Training data reaches into (or up to) the forward window."""


def fmt_ts(ts):
    """Human-readable form: '2026-11-17 15:32:56 WAT (epoch 1794925976.66)'."""
    try:
        dt = datetime.fromtimestamp(float(ts), _WAT)
        return "%s (epoch %.2f)" % (
            dt.strftime("%Y-%m-%d %H:%M:%S WAT"), float(ts))
    except Exception:
        return "epoch %s" % (ts,)


def _num(value):
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def assert_training_before_forward(training_end_ts, forward_start_ts,
                                   label="training"):
    """Fail loudly unless training ends strictly before the forward window.

    ``training_end_ts`` None (empty training set) always passes.
    Raises PointInTimeError when training_end >= forward_start, with
    both timestamps in human-readable form. Returns None on pass.
    """
    end = _num(training_end_ts)
    start = _num(forward_start_ts)
    if end is None or start is None:
        return None
    if end >= start:
        raise PointInTimeError(
            "POINT-IN-TIME VIOLATION: %s ends at %s, which is not "
            "strictly before the forward window start %s — training "
            "data must end before the forward window starts; a close "
            "resolved at/after the window start is a forward outcome, "
            "not training data." % (label, fmt_ts(end), fmt_ts(start)))
    return None


def split_training_before_forward(closes, forward_start_ts,
                                  ts_field="close_ts"):
    """Split stored closes into (training, excluded) at the forward start.

    ``forward_start_ts`` None means no forward window is recorded:
    everything is training, nothing is excluded. Otherwise a close is
    training only when its ``ts_field`` is present AND strictly less
    than ``forward_start_ts``; at/after-start and undated closes are
    excluded (fail-closed). Never raises; non-dict rows are excluded.
    """
    start = _num(forward_start_ts)
    training, excluded = [], []
    for c in closes or []:
        if not isinstance(c, dict):
            excluded.append(c)
            continue
        ts = _num(c.get(ts_field))
        if start is None:
            training.append(c)
        elif ts is not None and ts < start:
            training.append(c)
        else:
            excluded.append(c)
    return training, excluded


def training_window(closes, ts_field="close_ts"):
    """(earliest, latest, n) of ``ts_field`` over closes, or (None, None, 0).

    Undated closes are ignored here (the split is what excludes them);
    the window describes dated training data only. Never raises.
    """
    stamps = [_num(c.get(ts_field)) for c in (closes or [])
              if isinstance(c, dict)]
    stamps = [t for t in stamps if t is not None]
    if not stamps:
        return None, None, 0
    return min(stamps), max(stamps), len(stamps)


def load_forward_start(home):
    """started_ts from <home>/ab_forward.json, or None.

    None when the file is absent, unreadable, or carries no usable
    started_ts — i.e. no forward window is recorded, so no exclusion
    applies. Never raises.
    """
    try:
        path = os.path.join(home, FORWARD_STATE_FILE)
        with open(path) as f:
            st = json.load(f)
        return _num((st or {}).get("started_ts"))
    except Exception:
        return None


def assert_forward_window(closes, forward_start_ts, ts_field="entry_ts"):
    """Fail loudly unless every close in the list is inside the window.

    The list is the set of closes a report already counted as forward
    (e.g. entry_ts >= window start by construction); this asserts that
    construction rather than assuming it. Raises PointInTimeError
    naming the offending close's mint and timestamps when any close
    has ``ts_field`` < forward_start_ts or no usable ``ts_field``.
    Returns the count checked. Never raises on an empty list.
    """
    start = _num(forward_start_ts)
    n = 0
    for c in closes or []:
        if not isinstance(c, dict):
            continue
        n += 1
        ts = _num(c.get(ts_field))
        if start is None or ts is None or ts < start:
            raise PointInTimeError(
                "POINT-IN-TIME VIOLATION: counted forward close %s has "
                "%s=%s, before the forward window start %s — a forward "
                "report must count only closes with %s >= window start."
                % (c.get("mint") or "?",
                   ts_field,
                   fmt_ts(ts) if ts is not None else "missing",
                   fmt_ts(start) if start is not None else "missing",
                   ts_field))
    return n


def verdict_resolved_at(event):
    """The verdict chain's resolved_at source: the event's own ``ts``.

    Every event in ``verdict_ledger/events.jsonl`` carries ``ts``
    (checkpoint / close / verdict alike); that timestamp is when the
    event resolved and is the event's resolved_at. Returns None when
    the event carries no usable ``ts``. Read-only: this never writes
    or rewrites the chain.
    """
    if not isinstance(event, dict):
        return None
    return _num(event.get("ts"))


def ensure_resolved_at(record, resolved_ts=None):
    """Return a copy of ``record`` with ``resolved_at`` stamped if missing.

    Never overwrites an existing non-None ``resolved_at`` and never
    mutates the input — history is stamped forward, never rewritten.
    ``resolved_ts`` defaults to the record's own ``close_ts`` (then its
    ``ts``); when no timestamp is available the copy keeps
    ``resolved_at`` None rather than inventing one. Returns the copy
    (a non-dict input is returned unchanged).
    """
    if not isinstance(record, dict):
        return record
    if record.get("resolved_at") is not None:
        return dict(record)
    resolved = _num(resolved_ts)
    if resolved is None:
        resolved = _num(record.get("close_ts"))
    if resolved is None:
        resolved = _num(record.get("ts"))
    out = dict(record)
    out["resolved_at"] = resolved
    return out
