"""UNEXECUTED DRAFT (a NEW test file: needs reviewer scope clearance before any run). Stdlib only, temp files, no process/thread/network."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from daemon import grad_feed_spool as FS
FAILS = 0
def check(n, ok):
    global FAILS
    print(("PASS " if ok else "FAIL ") + n)
    if not ok: FAILS += 1
T = tempfile.mkdtemp(prefix="feedspool_"); sp = os.path.join(T, "s.jsonl"); cu = os.path.join(T, "c.json")
mig = lambda m: {"source": FS.SOURCE, "mint": m + "pump", "extra": {"migration_pool": "pump-amm"}}
check("F1 an empty/missing spool drains nothing and writes no cursor", FS.SpoolFeed(sp, cu).drain() == [] and not os.path.exists(cu))
n, ref = FS.append_items(sp, [mig("A"), {"source": "other", "mint": "Bpump"}, "junk", mig("C")])
check("F2 the writer appends migration items only (2 in, 2 refused)", (n, ref) == (2, 2))
f = FS.SpoolFeed(sp, cu); got = f.drain()
check("F3 drain returns the items oldest first and advances the cursor; a second drain is empty (at-most-once)", [g["mint"] for g in got] == ["Apump", "Cpump"] and f.drain() == [])
FS.append_items(sp, [mig("D")]); check("F4 only the new item is returned after an append", [g["mint"] for g in f.drain()] == ["Dpump"])
with open(sp, "ab") as fh: fh.write(b'{"spooled_ts": 1, "item": {"source": "pumpportal-migration", "mint": "Epump"')   # torn tail
check("F5 a torn last line (no newline) is not consumed", f.drain() == [])
with open(sp, "ab") as fh: fh.write(b"}}\n")
check("F6 once the writer completes the line it is delivered exactly once", [g["mint"] for g in f.drain()] == ["Epump"] and f.drain() == [])
with open(sp, "ab") as fh: fh.write(b"not json\n")
FS.append_items(sp, [mig("F")]); got = f.drain()
check("F7 a corrupt complete line is skipped and counted, later items still arrive (%s)" % f.stats(), [g["mint"] for g in got] == ["Fpump"] and f.stats()["skipped_corrupt"] == 1)
open(sp, "w").close(); FS.append_items(sp, [mig("G")]); got = f.drain()
check("F8 a truncated/rotated spool resets the cursor, counted (%s)" % f.stats(), [g["mint"] for g in got] == ["Gpump"] and f.stats()["reset_count"] == 1)
open(cu, "w").write("{bad"); FS.append_items(sp, [mig("H")])
check("F9 an unreadable cursor yields nothing and is counted, never a guessed position", f.drain() == [] and f.stats()["cursor_unreadable"] >= 1)
os.remove(cu); g2 = FS.SpoolFeed(sp, cu, max_items=2); FS.append_items(sp, [mig("I"), mig("J")])
a = g2.drain(); b = g2.drain()
check("F10 max_items only bounds one call; the rest follows next call (%d then %d)" % (len(a), len(b)), len(a) == 2 and len(b) >= 1)
check("F11 two readers keep independent cursors", len(FS.SpoolFeed(sp, os.path.join(T, "c2.json")).drain()) >= 4)
print("FAILS: %d" % FAILS); sys.exit(1 if FAILS else 0)
