#!/usr/bin/env python3
"""Event recorder + replay helpers (2026-10-23, P4).

Append-only JSONL record of the events the paper bot's verdicts
depend on — births, migrations, entries, exits, and tape quotes —
so any ledger close can later be replayed offline against what the
system actually saw, instead of reconstructed from memory.

Design rules (mirroring the rest of the codebase):
  * Fail-soft, always: recording must NEVER break trading. Every
    public function swallows its own errors; record() drops silently
    and returns False on any failure.
  * Free / local only: plain files under
    ``$FOREX_AGENT_HOME/recorder/``. No network, no new deps.
  * Base dir is read from FOREX_AGENT_HOME *at call time*, not
    import time, so tests can isolate with a temp home (the pattern
    used by the other suites).
  * Rotation: one file per UTC day, ``events-YYYYMMDD.jsonl``;
    when a file would exceed MAX_FILE_BYTES it rolls to
    ``events-YYYYMMDD.<n>.jsonl``. Hard cap: oldest files are
    pruned so the directory stays <= MAX_DIR_BYTES. Both caps are
    module constants so tests can monkeypatch them tiny.
  * Thread-safe appends (a single module lock; daemons record from
    their main loop and, for the raw lane, observer callbacks).

Replay helpers (simulate_ladder / classify_diff) live here so both
``scripts/replay-window`` and the tests share one implementation.
The ladder constants below are the SAME constants as grad.py —
replay-window imports grad's own constants and passes them in
explicitly, so a future ladder change in grad.py is what replay
uses; these defaults only keep the helpers usable standalone.
Paper only. Never writes ledgers.
"""
import json
import logging
import os
import re
import threading
import time

logger = logging.getLogger("forex_agent.daemon.launch_scanner.recorder")

# Module-level enabled flag (env RECORDER_ENABLED, default on).
# record() re-reads the env at call time as well, so flipping the
# env (or monkeypatching ENABLED) both work.
ENABLED = os.environ.get("RECORDER_ENABLED", "1").strip().lower() \
    not in ("0", "false", "no", "off", "")

# Rotation / retention caps — module constants, monkeypatchable.
MAX_FILE_BYTES = 25 * 1024 * 1024    # 25 MB per file
MAX_DIR_BYTES = 200 * 1024 * 1024    # 200 MB for the whole dir

KINDS = ("birth", "migration", "entry", "exit", "quote")

# Ladder constants — identical to grad.py (see module docstring).
LEG_A_FRAC, LEG_A_TARGET = 0.50, 2.0
LEG_B_FRAC, LEG_B_TARGET = 0.25, 3.0
LEG_C_FRAC, LEG_C_TRAIL = 0.25, 0.30
HARD_STOP_MULT = 0.50

_lock = threading.Lock()

_FILE_RE = re.compile(r"^events-(\d{8})(?:\.(\d+))?\.jsonl$")


# ---- paths (FOREX_AGENT_HOME read at call time) ---------------------------

def _home():
    return os.environ.get("FOREX_AGENT_HOME",
                          "/home/hatch/.forex-agent")


def recorder_dir():
    """Directory holding the event files. Call-time env lookup."""
    return os.path.join(_home(), "recorder")


def _is_enabled():
    env = os.environ.get("RECORDER_ENABLED")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "no",
                                           "off", "")
    return bool(ENABLED)


def _date_str(ts):
    return time.strftime("%Y%m%d", time.gmtime(ts))


def _parse_file_name(name):
    """(date_str, roll_index) or None for non-recorder files."""
    m = _FILE_RE.match(name or "")
    if not m:
        return None
    return m.group(1), int(m.group(2) or 0)


def _list_all_files(directory):
    """All recorder files in `directory`, oldest first (by day,
    then roll index). Never raises."""
    out = []
    try:
        for name in os.listdir(directory):
            parsed = _parse_file_name(name)
            if parsed is not None:
                out.append((parsed[0], parsed[1],
                            os.path.join(directory, name)))
    except Exception:
        return []
    out.sort(key=lambda t: (t[0], t[1]))
    return [p for _d, _n, p in out]


def _target_file(directory, date_str, line_len):
    """The file a new line of `line_len` bytes belongs in, applying
    the size rotation. Never raises (falls back to the base file)."""
    try:
        max_bytes = int(globals().get("MAX_FILE_BYTES")
                        or MAX_FILE_BYTES)
    except Exception:
        max_bytes = MAX_FILE_BYTES
    base = os.path.join(directory, "events-%s.jsonl" % date_str)
    try:
        if not os.path.exists(base):
            return base
        size = os.path.getsize(base)
        if size == 0 or size + line_len <= max_bytes:
            return base
        n = 1
        while True:
            cand = os.path.join(directory,
                                "events-%s.%d.jsonl" % (date_str, n))
            if not os.path.exists(cand):
                return cand
            csize = os.path.getsize(cand)
            if csize == 0 or csize + line_len <= max_bytes:
                return cand
            n += 1
    except Exception:
        return base


_last_write_ns = 0


def _stamp_monotonic(path):
    """Bump the just-written file's mtime forward when the clock did
    not move. _prune orders by mtime; on filesystems with coarse
    timestamps (or fast bursts) several files share one mtime, and
    recycled rotation names then make 'newest' ambiguous, so pruning
    can delete the just-written records. Never raises."""
    global _last_write_ns
    try:
        now = time.time_ns()
        if now <= _last_write_ns:
            now = _last_write_ns + 1
        _last_write_ns = now
        os.utime(path, ns=(now, now))
    except Exception:
        pass


def _prune(directory):
    """Enforce MAX_DIR_BYTES: delete oldest files until the dir is
    under the cap. 'Oldest' is by mtime, NOT filename: a pruned
    filename can be recycled by the size rotation for a brand-new
    record, and name order would then delete the just-written file.
    The newest file is never deleted (a single file may exceed a
    tiny patched cap — deleting the just-written record would be
    worse). Never raises."""
    try:
        max_dir = int(globals().get("MAX_DIR_BYTES")
                      or MAX_DIR_BYTES)
    except Exception:
        max_dir = MAX_DIR_BYTES
    try:
        entries = []  # (mtime, path, size)
        total = 0
        for p in _list_all_files(directory):
            try:
                st = os.stat(p)
                # st_mtime_ns, not st_mtime: at epoch magnitude the
                # float rounds several fast writes to one value, and
                # ties make 'newest' ambiguous (newest files pruned).
                entries.append((st.st_mtime_ns, p, st.st_size))
                total += st.st_size
            except Exception:
                continue
        # Tie-break equal mtimes by the recorder's own name order
        # (day, roll index): lexicographic path order misranks
        # '.10' before '.2' and can delete genuinely newer files.
        def _tie_key(path):
            parsed = _parse_file_name(os.path.basename(path))
            return parsed if parsed is not None else ("", -1)
        entries.sort(key=lambda t: (t[0], _tie_key(t[1])))
        # Oldest first; keep at least the newest file.
        for _mtime, p, size in entries[:-1]:
            if total <= max_dir:
                break
            try:
                os.unlink(p)
                total -= size
            except Exception:
                continue
    except Exception:
        pass


# ---- recording --------------------------------------------------------------

def record(kind, payload=None, ts=None):
    """Append one event: {"ts", "kind", ...payload} as a JSON line.

    Returns True when written, False when dropped (disabled or any
    error). NEVER raises — recording must never break trading."""
    try:
        if not _is_enabled():
            return False
        ts = time.time() if ts is None else float(ts)
        rec = {"ts": ts, "kind": str(kind)}
        if isinstance(payload, dict):
            for k, v in payload.items():
                if k not in ("ts", "kind"):
                    rec[k] = v
        line = json.dumps(rec, separators=(",", ":"),
                          default=str) + "\n"
        line_len = len(line.encode("utf-8"))
        with _lock:
            directory = recorder_dir()
            os.makedirs(directory, exist_ok=True)
            path = _target_file(directory, _date_str(ts), line_len)
            with open(path, "a") as f:
                f.write(line)
            _stamp_monotonic(path)
            _prune(directory)
        return True
    except Exception:
        return False


# ---- reading (offline replay) ----------------------------------------------

def list_files(from_ts=None, to_ts=None, directory=None):
    """Recorder files overlapping [from_ts, to_ts] (epoch seconds;
    either bound may be None), oldest first. Overlap is judged by
    the file's UTC day. Never raises."""
    try:
        directory = directory or recorder_dir()
        files = _list_all_files(directory)
        if from_ts is None and to_ts is None:
            return files
        out = []
        for p in files:
            parsed = _parse_file_name(os.path.basename(p))
            if parsed is None:
                continue
            try:
                day_start = _day_start_epoch(parsed[0])
            except Exception:
                out.append(p)
                continue
            day_end = day_start + 86400.0
            if from_ts is not None and day_end < float(from_ts):
                continue
            if to_ts is not None and day_start > float(to_ts):
                continue
            out.append(p)
        return out
    except Exception:
        return []


def _day_start_epoch(date_str):
    """Epoch seconds of 00:00 UTC on YYYYMMDD."""
    import calendar
    st = time.strptime(date_str, "%Y%m%d")
    return float(calendar.timegm(st))


def iter_events(from_ts=None, to_ts=None, kinds=None, directory=None):
    """Yield event dicts in file order, filtered by ts window and
    (optionally) kind set. Malformed lines are skipped. Never
    raises."""
    try:
        kind_set = set(kinds) if kinds else None
        for path in list_files(from_ts, to_ts, directory=directory):
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(rec, dict):
                            continue
                        if kind_set is not None and \
                                rec.get("kind") not in kind_set:
                            continue
                        ets = rec.get("ts")
                        if ets is not None:
                            try:
                                ets = float(ets)
                            except (TypeError, ValueError):
                                continue
                            if from_ts is not None and \
                                    ets < float(from_ts):
                                continue
                            if to_ts is not None and \
                                    ets > float(to_ts):
                                continue
                        yield rec
            except Exception:
                continue
    except Exception:
        return


def read_events(from_ts=None, to_ts=None, kinds=None, directory=None):
    """list(iter_events(...)). Never raises."""
    try:
        return list(iter_events(from_ts, to_ts, kinds=kinds,
                                directory=directory))
    except Exception:
        return []


# ---- replay: ladder simulation + diff classification ------------------------

def _quote_price(q):
    """Price out of a quote record / (ts, price) pair / bare float."""
    try:
        if isinstance(q, dict):
            return float(q.get("price") or 0), float(q.get("ts") or 0)
        if isinstance(q, (list, tuple)) and len(q) == 2:
            return float(q[1] or 0), float(q[0] or 0)
        return float(q or 0), 0.0
    except (TypeError, ValueError):
        return 0.0, 0.0


def simulate_ladder(entry_price, quotes,
                    leg_a_frac=LEG_A_FRAC, leg_a_target=LEG_A_TARGET,
                    leg_b_frac=LEG_B_FRAC, leg_b_target=LEG_B_TARGET,
                    leg_c_frac=LEG_C_FRAC, leg_c_trail=LEG_C_TRAIL,
                    hard_stop_mult=HARD_STOP_MULT):
    """Simple ladder simulation over recorded quote snapshots.

    Mirrors grad.py's exit evaluation order on idealised fills
    (no honest-fill slippage, no costs — replay checks whether the
    recorded tape *explains* the ledger close, it does not
    re-price it): hard stop first, then leg A @target, leg B
    @target, leg C on a trailing stop from its running peak. Any
    legs still open at the end of the quotes are closed at the
    last quote (mark-to-last), so a realized multiple is always
    produced when data exists.

    Returns {"realized_mult": float|None, "closed_reason": str,
    "legs_done": {"a": bool, "b": bool, "c": bool}, "n_quotes": int}.
    realized_mult is None iff entry_price <= 0 or no usable quote.
    Never raises."""
    try:
        entry = float(entry_price or 0)
        pts = []
        for q in quotes or []:
            px, ts = _quote_price(q)
            if px > 0:
                pts.append((ts, px))
        pts.sort(key=lambda t: t[0])
        if entry <= 0 or not pts:
            return {"realized_mult": None,
                    "closed_reason": "missing_data",
                    "legs_done": {"a": False, "b": False, "c": False},
                    "n_quotes": len(pts)}
        fracs = {"a": float(leg_a_frac), "b": float(leg_b_frac),
                 "c": float(leg_c_frac)}
        done = {"a": False, "b": False, "c": False}
        realized = 0.0
        c_peak = entry
        reason = "end_of_quotes"
        for _ts, px in pts:
            mult = px / entry
            if mult <= float(hard_stop_mult):
                remaining = sum(f for k, f in fracs.items()
                                if not done[k])
                realized += remaining * mult
                for k in done:
                    done[k] = True
                reason = "hard_stop_-50pct"
                break
            if not done["a"] and mult >= float(leg_a_target):
                realized += fracs["a"] * mult
                done["a"] = True
            if not done["b"] and mult >= float(leg_b_target):
                realized += fracs["b"] * mult
                done["b"] = True
            if not done["c"]:
                if px > c_peak:
                    c_peak = px
                elif px <= c_peak * (1.0 - float(leg_c_trail)):
                    realized += fracs["c"] * mult
                    done["c"] = True
            if all(done.values()):
                reason = "ladder_complete"
                break
        else:
            # No break: close whatever remains at the last quote.
            last_mult = pts[-1][1] / entry
            remaining = sum(f for k, f in fracs.items()
                            if not done[k])
            if remaining > 0:
                realized += remaining * last_mult
                for k in done:
                    done[k] = True
        if all(done.values()) and reason == "end_of_quotes":
            # Remaining legs were marked to last quote above; the
            # ladder itself did not complete on a trigger.
            pass
        return {"realized_mult": round(realized, 6),
                "closed_reason": reason,
                "legs_done": dict(done),
                "n_quotes": len(pts)}
    except Exception:
        return {"realized_mult": None,
                "closed_reason": "missing_data",
                "legs_done": {"a": False, "b": False, "c": False},
                "n_quotes": 0}


def classify_diff(diff, has_data=True):
    """MATCH <= 0.15, DRIFT <= 0.5, MISS > 0.5 or missing data."""
    try:
        if not has_data or diff is None:
            return "MISS"
        d = abs(float(diff))
        if d <= 0.15:
            return "MATCH"
        if d <= 0.5:
            return "DRIFT"
        return "MISS"
    except Exception:
        return "MISS"
