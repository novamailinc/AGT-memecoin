import math, sys, time
sys.path.insert(0, "daemon/launch_scanner")
import grad_select as gs
import mimic_edge as me
import mimic_edge2 as me2
fails = []
now = time.time()
base = {"complete": True, "creator": "C1", "count": 1,
        "window_start": now - 90000, "window_end": now,
        "finality": "finalized"}
# 1. NaN window bounds must NOT validate (before fix: returned count)
for k, v in (("window_start", float("nan")), ("window_end", float("nan")),
             ("window_start", float("inf")), ("window_end", float("-inf"))):
    p = dict(base); p[k] = v
    if gs._valid_authoritative_count(p, "C1", now) is not None:
        fails.append("NaN/inf %s accepted" % k)
# 2. sane proof still validates
if gs._valid_authoritative_count(dict(base), "C1", now) != 1:
    fails.append("sane proof rejected")
# 3. mimic_edge corrupt journal must fail CLOSED
for mod, name in ((me, "mimic_edge"), (me2, "mimic_edge2")):
    for bad in ({"tickers": [1, 2]}, {"tickers": {"AAA": "notalist"}},
                {"deployers": 42}, ["notadict"]):
        try:
            ok, reason = mod._filter_decision(bad, "AAA", "T1", now)
        except Exception as e:
            fails.append("%s raised on %r: %s" % (name, bad, e)); continue
        if ok:
            fails.append("%s fail-OPEN on corrupt journal %r" % (name, bad))
        elif reason != "unknown_serial_coverage":
            fails.append("%s wrong reason %r" % (name, reason))
    # 4. healthy journal still works: clean admits, serial rejects
    j = {"tickers": {}, "deployers": {}}
    ok, r = mod._filter_decision(j, "AAA", "T1", now)
    if not ok: fails.append("%s clean journal rejected" % name)
    j = {"tickers": {}, "deployers": {"T1": [now - i for i in range(mod.FILTER_MAX_OCCUR)]}}
    ok, r = mod._filter_decision(j, "AAA", "T1", now)
    if ok or r != "serial deployer":
        fails.append("%s serial reject broken: %s %s" % (name, ok, r))
print("FAILs:", fails if fails else 0, fails or "ALL PASS")
