"""Tests for core.live_safety_adapter. SYNTHETIC reports shaped like the combined instant_safety output. Compile-checked
only; not executed by the author."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import live_safety_adapter as A
from core import live_safety_evidence as E
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)
def rep(flags=None, **inst):
    i = {"token2022_extensions": [], "mint_authority_status": "revoked", "freeze_authority_status": "revoked", "transfer_fee": None}
    i.update(inst)
    return {"passed": True, "unknown": False, "fails": [], "flags": ["LP lock unverified (instant path)"] if flags is None else flags, "instant": i}
POLICY = {"accepted_lp_statuses": ["burned", "locked", "unverified"], "verified_extensions": [], "max_program_held_pct": None,
          "max_unclassified_pda_pct": None, "max_fee_bps": None, "allow_fee_authority": False}
ev, gaps = A.report_to_evidence(rep())
check("authorities mapped from typed status", ev["authorities"] == {"mint_authority": {"status": "revoked"}, "freeze_authority": {"status": "revoked"}})
check("LP flag -> unverified", ev["lp_lock"] == {"status": "unverified"})
check("no LP flag -> unavailable with a gap", A.report_to_evidence(rep(flags=[]))[0]["lp_lock"]["status"] == "unavailable" and "lp_lock:no_source" in A.report_to_evidence(rep(flags=[]))[1])
check("holders are never measured from this report", ev["holders"]["measured"] is False and "holders:no_coverage_or_unclassified_pda" in gaps)
check("transfer_fee None -> none", ev["transfer_fee"]["status"] == "none" and ev["transfer_fee"]["authority_set"] is False)
check("extensions complete and empty for a clean report", ev["extensions"] == {"complete": True, "names": [], "review": []})
ok, why = E.evaluate(ev, POLICY)
check("full evidence from a clean report STILL refuses (holders gap)", ok is False and "holders_unmeasured_or_malformed" in why)
check("legacy None mint_authority string is not used", A.report_to_evidence(rep(mint_authority=None, mint_authority_status="x"))[0]["authorities"]["mint_authority"]["status"] == "unavailable")
check("missing typed status -> unavailable and gap", "authorities:mint_authority_status_missing" in A.report_to_evidence(rep(mint_authority_status=None))[1])
check("set passes through as set", A.report_to_evidence(rep(freeze_authority_status="set"))[0]["authorities"]["freeze_authority"]["status"] == "set")
check("transfer_fee key absent -> unavailable", A.report_to_evidence({"flags": [], "instant": {"token2022_extensions": []}})[0]["transfer_fee"]["status"] == "unavailable")
r2 = rep(transfer_fee={"status": "epoch_selection_required", "config_authority_status": "revoked"})
e2, g2 = A.report_to_evidence(r2)
check("epoch_selection_required carried and gapped", e2["transfer_fee"]["status"] == "epoch_selection_required" and "transfer_fee:epoch_not_supplied" in g2 and e2["transfer_fee"]["authority_set"] is False)
check("typed authority status set -> authority_set True", A.report_to_evidence(rep(transfer_fee={"status": "epoch_selection_required", "config_authority_status": "set"}))[0]["transfer_fee"]["authority_set"] is True)
for bad in (None, "unavailable", "maybe", True, 5):
    e5, g5 = A.report_to_evidence(rep(transfer_fee={"status": "epoch_selection_required", "config_authority_status": bad}))
    check("authority status %r -> fee unavailable with gap" % (bad,), e5["transfer_fee"]["status"] == "unavailable" and "transfer_fee:authority_status_missing_or_unavailable" in g5)
e5, g5 = A.report_to_evidence(rep(flags=["Token-2022 transfer fee is authority-mutable (can be raised later)"], transfer_fee={"status": "epoch_selection_required"}))
check("legacy flag string alone is NOT used: missing typed status -> unavailable", e5["transfer_fee"]["status"] == "unavailable" and e5["transfer_fee"]["authority_set"] is False)
check("unknown fee status -> unavailable", A.report_to_evidence(rep(transfer_fee={"status": "weird"}))[0]["transfer_fee"]["status"] == "unavailable")
e3, g3 = A.report_to_evidence(rep(flags=["Token-2022 extension 'x' unrecognized (unverified — review)"], token2022_extensions=["x"]))
check("unrecognized extension flag makes extensions incomplete and adds review item", e3["extensions"]["complete"] is False and len(e3["extensions"]["review"]) == 1 and e3["extensions"]["names"] == ["x"])
e4, _ = A.report_to_evidence(rep(flags=["Token-2022 interest-bearing mint: displayed balances drift from real balances (unverified — review)"], token2022_extensions=["interestBearingConfig"]))
check("review-only flag keeps complete True but adds a review item", e4["extensions"]["complete"] is True and len(e4["extensions"]["review"]) == 1)
check("review items refuse in evidence", "extensions_have_review_items" in E.evaluate(e4, POLICY)[1])
check("non-list extension names -> incomplete with gap", (lambda t: t[0]["extensions"]["complete"] is False and "extensions:names_missing" in t[1])(A.report_to_evidence(rep(token2022_extensions="x"))))
check("non-dict report -> every item non-passing", (lambda t: t[0]["authorities"]["mint_authority"]["status"] == "unavailable" and t[0]["holders"]["measured"] is False and "report:no_instant_block_or_flags" in t[1])(A.report_to_evidence(None)))
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
