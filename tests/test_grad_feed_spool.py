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
check("F8 a truncated/rotated spool is NOT silently replayed: nothing is delivered and the durable error says why (%s)" % f.health(), got == [] and (f.health() or {}).get("error") == "spool_shorter_than_cursor")
f.reset_cursor("test: operator accepts replay of the whole spool"); got = f.drain()
check("F8b only the explicit operator reset re-reads from 0, health clears, reset counted", [g["mint"] for g in got] == ["Gpump"] and f.health() is None and f.stats()["reset_count"] == 1)
open(cu, "w").write("{bad"); FS.append_items(sp, [mig("H")])
_c0 = f.stats()["cursor_unreadable"]; f.drain(); _c1 = f.stats()["cursor_unreadable"]; f.stats(); f.stats()
check("F9 an unreadable cursor yields nothing, is surfaced durably (%s), never a guessed position, and stats() does not double count (%d -> %d)" % (f.health(), _c0, f.stats()["cursor_unreadable"]), (f.health() or {}).get("error") == "cursor_unreadable" and f.stats()["cursor_unreadable"] == _c1)
os.remove(cu); g2 = FS.SpoolFeed(sp, cu, max_items=2); FS.append_items(sp, [mig("I"), mig("J")])
a = g2.drain(); b = g2.drain()
check("F10 max_items only bounds one call; the rest follows next call (%d then %d)" % (len(a), len(b)), len(a) == 2 and len(b) >= 1)
check("F11 two readers keep independent cursors", len(FS.SpoolFeed(sp, os.path.join(T, "c2.json")).drain()) >= 4)
ok0 = FS.spool_from_env([mig("Z")], environ={})
check("F12 with AGT_GRAD_FEED_SPOOL unset the hook entry point does nothing (None)", ok0 is None)
sp2 = os.path.join(T, "env.jsonl"); n2 = FS.spool_from_env([mig("Y")], environ={FS.ENV: sp2})
check("F13 with the env set it appends and counts", n2 == 1 and os.path.exists(sp2))
f3 = FS.SpoolFeed(sp2, os.path.join(T, "c3.json")); _orig = FS._atomic
def _boom(path, rec): raise OSError("disk full")
FS._atomic = _boom; r1 = f3.drain(); FS._atomic = _orig; r2 = f3.drain(); r3 = f3.drain()
check("F14 a failed cursor write consumes NOTHING and the I/O error is surfaced; the items arrive on the next drain exactly once (%d, %d, %d)" % (len(r1), len(r2), len(r3)), r1 == [] and [i["mint"] for i in r2] == ["Ypump"] and r3 == [] and f3.health() is None)
sp4 = os.path.join(T, "ident.jsonl"); cu4 = os.path.join(T, "ident-cursor.json")
FS.append_items(sp4, [mig("K1"), mig("K2")]); f4 = FS.SpoolFeed(sp4, cu4); _g4 = f4.drain()
check("F15a the cursor stores the spool identity (inode + first-line hash) (%s)" % (json.load(open(cu4)).get("ident"),), len(_g4) == 2 and set((json.load(open(cu4)).get("ident") or {}).keys()) == {"ino", "head"})
_old = os.path.getsize(sp4); os.remove(sp4)                                  # truncate-and-REGROW past the old offset with different content
FS.append_items(sp4, [mig("N%d" % i) for i in range(12)])
check("F15b precondition: the regrown spool is LONGER than the old cursor offset (%d > %d)" % (os.path.getsize(sp4), _old), os.path.getsize(sp4) > _old)
_g5 = f4.drain()
check("F15 a spool regrown past the old offset is NOT read from the middle of a line: nothing delivered, durable error spool_identity_changed (%s)" % (f4.health(),), _g5 == [] and (f4.health() or {}).get("error") == "spool_identity_changed")
f4.reset_cursor("test: operator accepts the new spool"); _g6 = f4.drain()
check("F15c only the explicit operator reset re-reads the new spool from 0 and re-stamps the identity (%d items)" % len(_g6), len(_g6) == 12 and f4.health() is None)
sp7 = os.path.join(T, "legacy.jsonl"); cu7 = os.path.join(T, "legacy-cursor.json")
FS.append_items(sp7, [mig("L1"), mig("L2")]); open(cu7, "w").write(json.dumps({"offset": 40}))      # a cursor with a nonzero offset and NO identity
f7 = FS.SpoolFeed(sp7, cu7); _g7 = f7.drain()
check("F16 a nonzero offset with no stored identity is REFUSED (cannot prove it is the same spool), not silently adopted (%s)" % (f7.health(),), _g7 == [] and (f7.health() or {}).get("error") == "cursor_missing_identity")
sp8 = os.path.join(T, "race.jsonl"); cu8 = os.path.join(T, "race-cursor.json")
FS.append_items(sp8, [mig("R1"), mig("R2")]); f8 = FS.SpoolFeed(sp8, cu8); f8.drain()
FS.append_items(sp8, [mig("R3")]); _real_stat = os.stat; _calls = {"n": 0}
def _rot_stat(path, *a, **k):
    if str(path) == sp8 and _calls["n"] == 0:      # the path is replaced right after the read and before the post-read identity check
        _calls["n"] += 1; os.rename(sp8, sp8 + ".old"); FS.append_items(sp8, [mig("Q%d" % i) for i in range(5)])
    return _real_stat(path, *a, **k)
FS.os.stat = _rot_stat
try: _g8 = f8.drain()
finally: FS.os.stat = _real_stat
_off8 = json.load(open(cu8))["offset"]
check("F17 a rotation between the read and the post-read identity check consumes NOTHING: no items, cursor unchanged (%d items, offset %d), error surfaced (%s)" % (len(_g8), _off8, f8.health()),
      _g8 == [] and (f8.health() or {}).get("error") == "spool_identity_changed" and _off8 < os.path.getsize(sp8 + ".old"))
print("FAILS: %d" % FAILS); sys.exit(1 if FAILS else 0)
