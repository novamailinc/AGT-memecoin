"""Tests for core.live_safety_extras. Plain asserts. SYNTHETIC fixtures; policy values are TEST-ONLY, not owner values.
NOT executed by the author (source-only); expected values traced by hand. The recorded PYUSD checks run only if the two
recorded fixture files are present at FIX_DIR, otherwise they FAIL (never skip)."""
import json, math, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import live_safety_extras as X
from core import live_safety_parsers as P
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)

POL = {"allow_mint_authority": False, "allow_freeze_authority": False, "max_epoch_age_s": 60.0, "min_holders_covered_pct": 50.0}
NOW = 1000.0
def ev(**o):
    e = {"authorities": {"mint_authority": {"status": "revoked"}, "freeze_authority": {"status": "revoked"}},
         "epoch": X.epoch_item(5, NOW - 10), "holders": {"measured": True, "program_held_pct": 0.0, "unclassified_pda_pct": 0.0, "covered_pct": 80.0}}
    e.update(o); return e

# authorities parsing
check("null -> revoked", X.parse_authorities({"mintAuthority": None, "freezeAuthority": None}) ==
      {"mint_authority": {"status": "revoked"}, "freeze_authority": {"status": "revoked"}})
check("string -> set", X.parse_authorities({"mintAuthority": "abc", "freezeAuthority": "def"})["mint_authority"]["status"] == "set")
check("absent key -> unavailable, never revoked", X.parse_authorities({})["mint_authority"]["status"] == "unavailable")
for bad in ("", "  ", 0, False, [], {}, 5):
    check("odd value %r -> unavailable" % (bad,), X.parse_authorities({"mintAuthority": bad, "freezeAuthority": None})["mint_authority"]["status"] == "unavailable")
check("non-dict info -> unavailable", X.parse_authorities(None)["freeze_authority"]["status"] == "unavailable")

# epoch freshness
check("fresh epoch ok", X.epoch_fresh(X.epoch_item(5, NOW - 10), NOW, 60.0))
check("boundary age == max ok", X.epoch_fresh(X.epoch_item(5, NOW - 60), NOW, 60.0))
check("stale epoch refused", not X.epoch_fresh(X.epoch_item(5, NOW - 61), NOW, 60.0))
check("future-dated epoch refused", not X.epoch_fresh(X.epoch_item(5, NOW + 1), NOW, 60.0))
check("bool/negative/str epoch refused", all(not X.epoch_fresh(X.epoch_item(v, NOW), NOW, 60.0) for v in (True, -1, "5", None, 5.0)))
check("NaN fetched_at refused", not X.epoch_fresh(X.epoch_item(5, float("nan")), NOW, 60.0))
check("bad max age refused", all(not X.epoch_fresh(X.epoch_item(5, NOW), NOW, m) for m in (0, -1, None, float("inf"), True)))
check("non-dict item refused", not X.epoch_fresh(None, NOW, 60.0) and not X.epoch_fresh("x", NOW, 60.0))

# fee selection uses only a fresh epoch
INFO = {"extensions": [{"extension": "transferFeeConfig", "state": {
    "olderTransferFee": {"epoch": 1, "maximumFee": 0, "transferFeeBasisPoints": 100},
    "newerTransferFee": {"epoch": 10, "maximumFee": 0, "transferFeeBasisPoints": 300},
    "transferFeeConfigAuthority": "a", "withdrawWithheldAuthority": "b"}}]}
f = X.fee_with_epoch("spl-token-2022", INFO, X.epoch_item(12, NOW - 5), NOW, 60.0)
check("fresh epoch >= newer.epoch resolves newer 300", f["status"] == "resolved" and f["current_bps"] == 300)
f = X.fee_with_epoch("spl-token-2022", INFO, X.epoch_item(12, NOW - 500), NOW, 60.0)
check("stale epoch -> epoch_selection_required, no current_bps", f["status"] == "epoch_selection_required" and f["current_bps"] is None)
f = X.fee_with_epoch("spl-token-2022", INFO, X.epoch_item(5, NOW - 5), NOW, 60.0)
check("fresh epoch < newer.epoch resolves older 100", f["status"] == "resolved" and f["current_bps"] == 100)
f = X.fee_with_epoch("spl-token-2022", INFO, None, NOW, 60.0)
check("no epoch item -> epoch_selection_required", f["status"] == "epoch_selection_required")

# evaluate_extras
ok, why = X.evaluate_extras(ev(), POL, NOW)
check("synthetic control passes", ok is True and why == [])
check("mint authority set refused", X.evaluate_extras(ev(authorities={"mint_authority": {"status": "set"}, "freeze_authority": {"status": "revoked"}}), POL, NOW)[1] == ["mint_authority_set"])
check("freeze authority set refused", "freeze_authority_set" in X.evaluate_extras(ev(authorities={"mint_authority": {"status": "revoked"}, "freeze_authority": {"status": "set"}}), POL, NOW)[1])
p2 = dict(POL, allow_mint_authority=True)
check("set allowed only when the caller says so", X.evaluate_extras(ev(authorities={"mint_authority": {"status": "set"}, "freeze_authority": {"status": "revoked"}}), p2, NOW)[0])
check("unavailable authority refused even when set is allowed", "mint_authority_unavailable" in X.evaluate_extras(ev(authorities={"mint_authority": {"status": "unavailable"}, "freeze_authority": {"status": "revoked"}}), dict(POL, allow_mint_authority=True), NOW)[1])
check("missing authorities refused", "mint_authority_missing_or_malformed" in X.evaluate_extras(ev(authorities=None), POL, NOW)[1])
check("bogus status refused", "freeze_authority_missing_or_malformed" in X.evaluate_extras(ev(authorities={"mint_authority": {"status": "revoked"}, "freeze_authority": {"status": "maybe"}}), POL, NOW)[1])
check("stale epoch refused", "epoch_missing_stale_or_malformed" in X.evaluate_extras(ev(epoch=X.epoch_item(5, NOW - 999)), POL, NOW)[1])
check("missing epoch refused", "epoch_missing_stale_or_malformed" in X.evaluate_extras(ev(epoch=None), POL, NOW)[1])
check("coverage below policy refused", X.evaluate_extras(ev(holders={"measured": True, "covered_pct": 49.9}), POL, NOW)[1] == ["holders_coverage_below_policy"])
check("coverage at policy ok", X.evaluate_extras(ev(holders={"measured": True, "covered_pct": 50.0}), POL, NOW)[0])
for h in ({"measured": False, "covered_pct": 99.0}, {"measured": True}, {"measured": True, "covered_pct": float("nan")},
          {"measured": True, "covered_pct": 130.0}, {"measured": True, "covered_pct": -1.0}, None, {"measured": 1, "covered_pct": 90.0}):
    check("holders %r refused" % (h,), "holders_coverage_missing_or_malformed" in X.evaluate_extras(ev(holders=h), POL, NOW)[1])
check("covered_pct from the real parser feeds the check", X.evaluate_extras(ev(holders=P.parse_holders([{"amount": 60, "owner_kind": "wallet", "owner": "w"}], 100, set())), POL, NOW)[0])
check("parser coverage 40% of supply refused at 50", "holders_coverage_below_policy" in X.evaluate_extras(ev(holders=P.parse_holders([{"amount": 40, "owner_kind": "wallet", "owner": "w"}], 100, set())), POL, NOW)[1])
for k in X.POLICY_KEYS:
    p = dict(POL); del p[k]
    check("policy missing %s refuses" % k, X.evaluate_extras(ev(), p, NOW) == (False, ["extras_policy_missing_or_malformed"]))
for k, v in (("min_holders_covered_pct", None), ("min_holders_covered_pct", 101), ("min_holders_covered_pct", -1), ("max_epoch_age_s", 0),
             ("max_epoch_age_s", None), ("allow_mint_authority", 1), ("allow_freeze_authority", None)):
    check("policy %s=%r refuses (None is not approval)" % (k, v), X.evaluate_extras(ev(), dict(POL, **{k: v}), NOW)[0] is False)
check("non-dict evidence refused", X.evaluate_extras(None, POL, NOW) == (False, ["extras_evidence_missing"]))

# recorded PYUSD (public mainnet-beta 2026-11-24T18:45Z); path adjustable
FIX = os.environ.get("AGT_FIX_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "intelligence", "acquisition"))
try:
    v = json.load(open(os.path.join(FIX, "live_getAccountInfo_pyusd_token2022.json")))["result"]["value"]
    info = v["data"]["parsed"]["info"]; prog = v["data"]["program"]
    a = X.parse_authorities(info)
    # hand-read from the recorded JSON earlier: mintAuthority and freezeAuthority keys present; their statuses are asserted
    # as 'not unavailable' (the keys exist), without guessing which of set/revoked; the report states the observed pair.
    check("recorded PYUSD: both authority keys present (not unavailable)", a["mint_authority"]["status"] != "unavailable" and a["freeze_authority"]["status"] != "unavailable")
    print("RECORDED authorities:", a)
    f = X.fee_with_epoch(prog, info, X.epoch_item(1095, NOW - 1), NOW, 60.0)
    check("recorded PYUSD: fresh epoch 1095 resolves 0 bps", f["status"] == "resolved" and f["current_bps"] == 0)
    f = X.fee_with_epoch(prog, info, X.epoch_item(1095, NOW - 1000), NOW, 60.0)
    check("recorded PYUSD: stale epoch -> epoch_selection_required", f["status"] == "epoch_selection_required")
except (OSError, KeyError, ValueError) as exc:
    check("recorded PYUSD fixture readable (%s)" % type(exc).__name__, False)
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
