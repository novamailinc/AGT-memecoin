import sys, time
sys.path.insert(0, "daemon/launch_scanner")
import mimic_edge as me, mimic_edge2 as me2
fails = []
now = time.time()
for mod, name in ((me, "mimic_edge"), (me2, "mimic_edge2")):
    # future timestamp = corruption, not a clean window
    j = {"tickers": {}, "deployers": {"T1": [now + 3600]}}
    ok, r = mod._filter_decision(j, "AAA", "T1", now)
    if ok or r != "unknown_serial_coverage":
        fails.append("%s future ts not corruption: %s %s" % (name, ok, r))
    # bool member = corruption
    j = {"tickers": {}, "deployers": {"T1": [True, now]}}
    ok, r = mod._filter_decision(j, "AAA", "T1", now)
    if ok: fails.append("%s bool member admitted" % name)
    # record failure returns False (was: silent pass)
    bad = {"tickers": 0}  # setdefault on non-dict store raises inside
    if mod._record_launch(bad, "AAA", "T1", now) is not False:
        fails.append("%s record failure not reported" % name)
    # poisoned journal fails closed
    j = {"tickers": {}, "deployers": {}, "_corrupt": True}
    ok, r = mod._filter_decision(j, "AAA", "T1", now)
    if ok or r != "unknown_serial_coverage":
        fails.append("%s poison not fail-closed: %s %s" % (name, ok, r))
    # successful record returns True and counts
    j = {"tickers": {}, "deployers": {}}
    if mod._record_launch(j, "AAA", "T1", now) is not True:
        fails.append("%s good record not True" % name)
    if len(j["deployers"]["T1"]) != 1:
        fails.append("%s good record not stored" % name)
    # SPEC PRESERVED: fresh/restarted (missing) journal admits (documented fail-open limit)
    ok, r = mod._filter_decision({"tickers": {}, "deployers": {}}, "AAA", "T1", now)
    if not ok: fails.append("%s restart-empty journal must admit per spec" % name)
print("FAILs:", fails if fails else 0, fails or "ALL PASS")
