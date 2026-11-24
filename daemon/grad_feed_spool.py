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
* A spool shorter than the cursor (rotated/truncated) resets the cursor to 0 and sets `reset_count`.
* Only items with source == "pumpportal-migration" are spooled (what grad_raw.migration_to_candidate accepts); the writer refuses anything else.
* The cursor is per reader (per lane): two lanes see the same spool independently. Spool growth/rotation is not managed here (stated gap).
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


class SpoolFeed:
    def __init__(self, path, cursor_path, max_items=200):
        self.path, self.cursor_path, self.max_items = path, cursor_path, int(max_items)
        self._stats = {"read": 0, "skipped_corrupt": 0, "reset_count": 0, "cursor_unreadable": 0}

    def _cursor(self):
        try:
            with open(self.cursor_path) as fh:
                off = json.load(fh)["offset"]
            if isinstance(off, bool) or not isinstance(off, int) or off < 0:
                raise ValueError("bad offset")
            return off
        except FileNotFoundError:
            return 0
        except Exception:
            self._stats["cursor_unreadable"] += 1
            return None

    def drain(self):
        """Items past the cursor (oldest first, at most max_items). Never raises. An unreadable cursor yields NOTHING (fail closed: no guessing a
        position) and is counted; it does not reset itself."""
        try:
            off = self._cursor()
            if off is None:
                return []
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            if size < off:
                off = 0; self._stats["reset_count"] += 1
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
            return out
        except Exception:
            return []

    def stats(self):
        return dict(self._stats, offset=self._cursor())
