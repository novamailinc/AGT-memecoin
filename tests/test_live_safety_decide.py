"""Tests for core.live_safety_decide and the adapter's holders/epoch intake. SYNTHETIC reports shaped like the combined
instant_safety output plus the proposed emitter fields. Policy numbers are TEST-ONLY, not owner values. Compile-checked
only; not executed by the author."""
import copy, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import live_safety_decide as D
from core import live_safety_adapter as A
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)
NOW = 1000.0
PE = {"accepted_lp_statuses": ["unverified"], "verified_extensions": ["transferFeeConfig"], "max_program_held_pct": None,
      "max_unclassified_pda_pct": 10.0, "max_fee_bps": 500, "allow_fee_authority": False}
PX = {"allow_mint_authority": False, "allow_freeze_authority": False, "max_epoch_age_s": 60.0, "min_holders_covered_pct": 50.0}
def sched(ep, bps): return {"status": "available", "epoch": ep, "maximumFee": 0, "transferFeeBasisPoints": bps}
def rep(**inst):
    i = {"token2022_extensions": [], "mint_authority_status": "revoked", "freeze_authority_status": "revoked", "transfer_fee": None,
         "holder_rows": [{"amount": 60.0, "owner_kind": "wallet", "owner": "w"}], "supply_raw": 100.0,
         "epoch_info": {"epoch": 12, "fetched_at": NOW - 5}}
    i.update(inst)
    return {"passed": True, "unknown": False, "fails": [], "flags": ["LP lock unverified (instant path)"], "instant": i}
def fee_rep(**kw):
    return rep(token2022_extensions=["transferFeeConfig"], transfer_fee={
        "status": "epoch_selection_required", "config_authority_status": "revoked", "schedules": {"olderTransferFee": sched(1, 100), "newerTransferFee": sched(10, 300)}}, **kw)
def go(r, pe=PE, px=PX, now=NOW, owners=()): return D.decide(r, pe, px, now, owners)

ok, why, gaps = go(rep())
check("synthetic control passes", ok is True and why == [])
check("control has no gaps", gaps == [])
ok, why, gaps = go({"passed": True, "flags": [], "instant": {"transfer_fee": None, "token2022_extensions": [],
                    "mint_authority_status": "revoked", "freeze_authority_status": "revoked"}})
check("report without emitter fields refuses and names gaps", ok is False and "holders:no_coverage_or_unclassified_pda" in gaps and "epoch:not_supplied" in gaps)
r = rep(); del r["instant"]["epoch_info"]
ok, why, gaps = go(r)
check("missing epoch refuses", ok is False and "epoch_missing_stale_or_malformed" in why and "epoch:not_supplied" in gaps)
check("stale epoch refuses", "epoch_missing_stale_or_malformed" in go(rep(epoch_info={"epoch": 12, "fetched_at": NOW - 500}))[1])
check("future epoch refuses", "epoch_missing_stale_or_malformed" in go(rep(epoch_info={"epoch": 12, "fetched_at": NOW + 5}))[1])
check("bool epoch refuses", "epoch_missing_stale_or_malformed" in go(rep(epoch_info={"epoch": True, "fetched_at": NOW}))[1])
rows = [{"amount": 60.0, "owner_kind": "wallet", "owner": "w"}, {"amount": 40.0, "owner_kind": "pda", "owner": "p"}]
ok, why, gaps = go(rep(holder_rows=rows))
check("unclassified PDA over the limit refuses with no owner classification", ok is False and "holders_unclassified_pda_over_limit" in why)
check("caller classification of that owner as program-held clears it", go(rep(holder_rows=rows), owners=("p",))[0] is True)
ok, why, gaps = go(rep(holder_rows=[{"amount": 60.0, "owner_kind": "wallet", "owner": None}]))
check("unknown owner makes holders unmeasured and refuses", ok is False and any(g.startswith("holders:unmeasured") for g in gaps))
check("coverage below policy refuses", "holders_coverage_below_policy" in go(rep(holder_rows=[{"amount": 40.0, "owner_kind": "wallet", "owner": "w"}]))[1])
# fee resolution
ok, why, gaps = go(fee_rep())
check("fresh epoch resolves fee from the report's own schedules (newer, 300 <= 500)", ok is True and why == [])
ok, why, gaps = go(fee_rep(), pe=dict(PE, max_fee_bps=200))
check("resolved 300 bps over a 200 limit refuses", ok is False and "transfer_fee_over_limit" in why)
ok, why, gaps = go(fee_rep(epoch_info={"epoch": 5, "fetched_at": NOW - 5}), pe=dict(PE, max_fee_bps=150))
check("epoch below newer.epoch selects older, but max(current, newer) still applies", ok is False and "transfer_fee_over_limit" in why)
ok, why, gaps = go(fee_rep(epoch_info={"epoch": 12, "fetched_at": NOW - 500}))
check("stale epoch leaves the fee unresolved", "transfer_fee_unresolved:epoch_selection_required" in why and "epoch_missing_stale_or_malformed" in why)
r = fee_rep(); r["instant"]["transfer_fee"]["schedules"]["newerTransferFee"] = {"status": "unknown"}
check("unavailable schedule leaves the fee unresolved", "transfer_fee_unresolved:epoch_selection_required" in go(r)[1])
r = fee_rep(); r["instant"]["transfer_fee"]["schedules"]["olderTransferFee"]["transferFeeBasisPoints"] = True
check("bool bps leaves the fee unresolved", "transfer_fee_unresolved:epoch_selection_required" in go(r)[1])
r = fee_rep(); r["instant"]["transfer_fee"]["config_authority_status"] = "set"
check("typed authority set refuses under allow_fee_authority False", "transfer_fee_authority_set" in go(r)[1])
r = fee_rep(); del r["instant"]["transfer_fee"]["config_authority_status"]
ok, why, gaps = go(r)
check("missing typed authority status refuses (not inferred)", ok is False and "transfer_fee_unresolved:unavailable" in why and "transfer_fee:authority_status_missing_or_unavailable" in gaps)
r = fee_rep(); r["flags"].append("Token-2022 transfer fee is authority-mutable (can be raised later)")
check("a flag string alone does not change a typed revoked status", go(r)[0] is True)
# authorities / policies / clock
check("mint authority set refuses", "mint_authority_set" in go(rep(mint_authority_status="set"))[1])
check("unavailable freeze authority refuses", "freeze_authority_unavailable" in go(rep(freeze_authority_status="unavailable"))[1])
check("missing evidence policy refuses", go(rep(), pe={})[0] is False and "policy_missing_or_malformed" in go(rep(), pe={})[1])
check("missing extras policy refuses", "extras_policy_missing_or_malformed" in go(rep(), px={})[1])
check("None min_holders_covered_pct refuses (None is not approval)", go(rep(), px=dict(PX, min_holders_covered_pct=None))[0] is False)
check("NaN now refuses", go(rep(), now=float("nan")) == (False, ["now_missing_or_malformed"], []))
check("non-dict report refuses", go(None)[0] is False)
r0 = rep(); r1 = copy.deepcopy(r0); go(r0)
check("input report is not mutated", r0 == r1)
# scoped epoch requirement (typed extension parse) and duplicate fee extensions
def xp(kind="spl", key="absent", entries=(), complete=True, count=None):
    en = [{"name": n, "status": s} for n, s in entries]
    return {"token_program_id": "x", "token_program_kind": kind, "extensions_key": key,
            "entry_count": len(en) if count is None else count, "entries": en, "complete": complete}
def nofee(parse=None, **kw):
    r = rep(extensions_parse=xp() if parse is None else parse, **kw)
    del r["instant"]["epoch_info"]
    return r
def needs_epoch(r): return "epoch_missing_stale_or_malformed" in go(r)[1]
ok, why, gaps = go(nofee())
check("classic SPL, complete typed parse, no fee: passes without an epoch", ok is True and why == [])
check("no_fee_proven true for it", D.no_fee_proven(nofee()) is True)
r = nofee(xp("token2022", "list", [("metadataPointer", "parsed")]), token2022_extensions=["metadataPointer"])
check("Token-2022 with a complete parsed list and no fee is proven (epoch not required; other policy checks separate)", D.no_fee_proven(r) is True and not needs_epoch(r))
r = nofee(xp("token2022", "absent"))
check("Token-2022 with the extensions key MISSING is not proven (epoch required)", needs_epoch(r))
r = nofee(xp("spl", "list", [("metadataPointer", "parsed")]), token2022_extensions=["metadataPointer"])
check("classic SPL with a list is not proven", needs_epoch(r))
check("other program kind is not proven", needs_epoch(nofee(xp("other", "absent"))))
check("missing program kind is not proven", needs_epoch(nofee(dict(xp(), token_program_kind=None))))
r = rep(); del r["instant"]["epoch_info"]
check("report without extensions_parse (older emitter) requires epoch", needs_epoch(r))
check("complete not exactly True requires epoch", needs_epoch(nofee(dict(xp(), complete="yes"))))
for st in ("malformed", "unparsed", "unrecognized"):
    r = nofee(xp("token2022", "list", [("foo", st)], complete=True), token2022_extensions=["foo"])
    check("entry status %s cannot prove no-fee even if the producer says complete" % st, needs_epoch(r))
r = nofee(xp("token2022", "list", [("a", "parsed"), ("a", "parsed")], complete=True), token2022_extensions=["a", "a"])
check("duplicate extension name cannot prove no-fee", needs_epoch(r))
r = nofee(xp("token2022", "list", [], complete=True, count=1))
check("entry_count mismatch cannot prove no-fee", needs_epoch(r))
r = nofee(xp("token2022", "list", [("transferFeeConfig", "parsed")]), token2022_extensions=["transferFeeConfig"])
check("transferFeeConfig entry with transfer_fee None cannot prove no-fee", needs_epoch(r))
r = nofee(xp("token2022", "list", [("a", "parsed")]), token2022_extensions=["b"])
check("names differing from entries cannot prove no-fee", needs_epoch(r))
r = nofee(xp("token2022", "list", [("", "parsed")]), token2022_extensions=[""])
check("empty entry name cannot prove no-fee", needs_epoch(r))
r = nofee(); r["flags"].append("Token-2022 some future extension note")
check("a future extension-related flag defeats the proof (defense in depth)", needs_epoch(r))
r = nofee(); r["flags"].append("Token-2022 transfer fee config oddity")
check("a fee-related flag defeats the proof", needs_epoch(r))
r = nofee(); r["flags"].append("mystery item (unverified - review)")
check("the adapter's own review item defeats the proof independent of typed fields", needs_epoch(r))
r = nofee(); r["flags"] = "oops"
check("malformed flags defeat the proof", D.no_fee_proven(r) is False)
check("non-dict report is not proven", D.no_fee_proven(None) is False)
r = nofee(); r["instant"]["mint_authority_status"] = "set"
check("no-fee exemption does not waive other checks (mint authority set)", "mint_authority_set" in go(r)[1])
r = nofee(); r["instant"]["holder_rows"] = [{"amount": 40.0, "owner_kind": "wallet", "owner": "w"}]
check("no-fee exemption does not waive holders coverage", "holders_coverage_below_policy" in go(r)[1])
r2 = fee_rep(extensions_parse=xp("token2022", "list", [("transferFeeConfig", "parsed")])); del r2["instant"]["epoch_info"]
check("fee mint without epoch still refuses even with a complete typed parse", go(r2)[0] is False)
r2 = nofee(xp("token2022", "list", [("transferFeeConfig", "parsed"), ("transferFeeConfig", "parsed")], complete=False),
           transfer_fee={"status": "ambiguous_duplicate", "config_authority_status": "ambiguous", "entry_count": 2},
           token2022_extensions=["transferFeeConfig", "transferFeeConfig"])
ok, why, gaps = go(r2)
check("ambiguous duplicate fee refuses with named gap", ok is False and "transfer_fee_unresolved:unavailable" in why and "transfer_fee:duplicate_extension_ambiguous" in gaps)
ok, why, gaps = go(rep(extensions_parse=xp(complete=False)))
check("adapter marks typed-incomplete parse as incomplete extensions with a gap", "extensions:parse_incomplete" in gaps)


print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
