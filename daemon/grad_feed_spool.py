"""Graduation-feed SPOOL adapter (DRAFT SOURCE, UNEXECUTED, NOT WIRED, OFF). Stdlib only, no network, no thread/process start, no socket.

Why: the only live source of pump.fun graduation items is PumpPortalFeed (pumpportal.py), a websocket thread that lives INSIDE the launch_scanner
daemon (LaunchScanner._ensure_pumpportal / _drain_pumpportal). The grad-cycle driver is a separate process and must not open a stream. This module
is the network-free hand-off: a writer appends graduation items to an append-only jsonl spool, and `SpoolFeed` (a reader with the `.drain()` contract
grad_select.scan_once expects) returns the lines past its OWN byte cursor. The WRITER HOOK inside LaunchScanner is NOT applied here (proposal only:
call `append_items(path, items)` with the list `_drain_pumpportal` already holds). Without that hook the spool stays empty and the feed is inert.

Semantics, stated so nothing is implied:
* At-most-once. The cursor is advanced when drain() returns, so a crash after drain loses those items rather than replaying them: a replayed
  graduation is a STALE entry opportunity, and the lane's own entry-age rules, not this module, decide staleness. No age or count threshold is
  invented here (max_items only bounds one drain call).
* Only complete lines (ending in a newline) are consumed; a torn last line waits for the writer. A corrupt complete line is skipped and counted.
* A spool SHORTER than the cursor (rotated/truncated by someone) is NOT recovered automatically: a reset to 0 would REPLAY every old item
  (at-least-once across a truncation, re-delivering stale graduations), so the feed goes inert with a durable error "spool_shorter_than_cursor"
  until an operator calls reset_cursor(reason) (source only, no caller): the replay is then a deliberate act. Same for an unreadable cursor.
* Items drained but not yet used by the lane when the process dies are LOST (at-most-once, cursor written before drain() returns). This is chosen over
  replay because a replayed graduation is a stale entry; it is a stated data-loss window, not hidden.
* ERRORS SURFACE: any I/O or cursor fault writes a durable `<cursor>.status.json` ({"error", "since", "count", "last_ts"}) and health() returns it; an
  empty feed with health ok is genuinely empty. grad_cycle copies health() into the cycle journal's scan step. A successful drain clears the status.
* Only items with source == "pumpportal-migration" are spooled (what grad_raw.migration_to_candidate accepts); the writer refuses anything else.
* The cursor is per reader (per lane): two lanes see the same spool independently. Spool growth/rotation is not managed here (stated gap).
* Loss / duplication matrix (each is a stated case, none is hidden):
    - writer crashes between PumpPortalFeed.drain() and append_items: those items are LOST to this lane (the in-memory queue died with the process;
      the launch scanner's own pipeline loses them the same way).
    - append_items fails (disk error): returns the refused count, the hook ignores it: lost for this lane only, the launch scanner continues.
    - the reader's cursor write fails: drain() returns [] and does NOT consume, so the next drain re-reads them: NOT lost, NOT duplicated.
    - reader crashes after the cursor was written but before the lane used the items: LOST (at-most-once, by design, see above).
    - reader crashes mid cursor write: the atomic replace leaves the old OR the new cursor, never a torn one.
    - duplicates: this module never re-delivers an item to the same reader. A migration that the SOURCE emits twice is spooled twice (no dedupe here);
      de-duplication by mint belongs to the lane (not verified by this module).
* Visible state: `stats()` returns counts of read, skipped_corrupt, reset_count and the cursor offset. Spool files hold market data only."""
import json
import os
import time

SOURCE = "pumpportal-migration"


def _atomic(path, rec):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rec, fh); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def append_items(path, items, now=None):
    """Writer side. Appends accepted items (dict, source == SOURCE, JSON-serializable) and fsyncs. Returns (appended, refused). Never raises."""
    now = time.time() if now is None else now
    lines, refused = [], 0
    for it in items or []:
        try:
            if not isinstance(it, dict) or it.get("source") != SOURCE:
                refused += 1; continue
            lines.append(json.dumps({"spooled_ts": now, "item": it}, sort_keys=True, allow_nan=False) + "\n")
        except Exception:
            refused += 1
    if not lines:
        return 0, refused
    try:
        with open(path, "a") as fh:
            fh.write("".join(lines)); fh.flush(); os.fsync(fh.fileno())
    except Exception:
        return 0, refused + len(lines)
    return len(lines), refused


ENV = "AGT_GRAD_FEED_SPOOL"


def spool_from_env(items, environ=None):
    """The hook entry point: appends to the spool named by AGT_GRAD_FEED_SPOOL, or does NOTHING when it is unset (default). Never raises. Returns
    the appended count, or None when disabled."""
    try:
        path = (environ if environ is not None else os.environ).get(ENV)
        if not path:
            return None
        return append_items(path, items)[0]
    except Exception:
        return None


class SpoolFeed:
    def __init__(self, path, cursor_path, max_items=200):
        self.path, self.cursor_path, self.max_items = path, cursor_path, int(max_items)
        self._stats = {"read": 0, "skipped_corrupt": 0, "reset_count": 0, "cursor_unreadable": 0}
        self.status_path = cursor_path + ".status.json"

    def _set_error(self, kind, detail=""):
        try:
            prev = self.health()
            rec = {"error": kind, "detail": str(detail)[:200], "since": (prev or {}).get("since") if (prev or {}).get("error") == kind else time.time(),
                   "count": ((prev or {}).get("count", 0) + 1) if (prev or {}).get("error") == kind else 1, "last_ts": time.time()}
            _atomic(self.status_path, rec)
        except Exception:
            pass                         # the in-memory flag below still reports it for this process
        self._mem_error = kind

    def _clear_error(self):
        self._mem_error = None
        try:
            if os.path.exists(self.status_path): os.remove(self.status_path)
        except Exception:
            pass

    def health(self):
        """None when healthy; else the durable error record (or {"error": ..., "unreadable_status": True})."""
        try:
            with open(self.status_path) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {"error": self._mem_error} if getattr(self, "_mem_error", None) else None
        except Exception:
            return {"error": getattr(self, "_mem_error", None) or "status_unreadable", "unreadable_status": True}

    def reset_cursor(self, reason):
        """OPERATOR action (no caller): deliberately set the cursor to 0 (replays the whole spool) or clear an unreadable cursor. Records why."""
        _atomic(self.cursor_path, {"offset": 0, "reset_reason": str(reason)[:200], "reset_ts": time.time()})
        self._stats["reset_count"] += 1; self._clear_error()

    def _cursor(self, count=True):
        try:
            with open(self.cursor_path) as fh:
                off = json.load(fh)["offset"]
            if isinstance(off, bool) or not isinstance(off, int) or off < 0:
                raise ValueError("bad offset")
            return off
        except FileNotFoundError:
            return 0
        except Exception:
            if count:
                self._stats["cursor_unreadable"] += 1
            return None

    def drain(self):
        """Items past the cursor (oldest first, at most max_items). Never raises. An unreadable cursor yields NOTHING (fail closed: no guessing a
        position) and is counted; it does not reset itself."""
        try:
            off = self._cursor()
            if off is None:
                self._set_error("cursor_unreadable"); return []
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            if size < off:
                self._set_error("spool_shorter_than_cursor", "size %d < cursor %d" % (size, off)); return []
            out, pos = [], off
            with open(self.path, "rb") as fh:
                fh.seek(off)
                while len(out) < self.max_items:
                    line = fh.readline()
                    if not line or not line.endswith(b"\n"):
                        break                      # EOF or a torn tail: wait for the writer
                    pos += len(line)
                    try:
                        rec = json.loads(line.decode("utf-8"))
                        item = rec["item"]
                        if not isinstance(item, dict) or item.get("source") != SOURCE:
                            raise ValueError("not a migration item")
                        out.append(item)
                    except Exception:
                        self._stats["skipped_corrupt"] += 1
            if pos != off or size < off:
                _atomic(self.cursor_path, {"offset": pos})
            self._stats["read"] += len(out)
            self._clear_error()
            return out
        except Exception as e:
            self._set_error("io_error", repr(e))
            return []

    def stats(self):
        return dict(self._stats, offset=self._cursor(count=False))
