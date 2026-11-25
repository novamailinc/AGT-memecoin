"""Contract tests for the NEW core.live_safety_parsers (not the lost original). Plain asserts, SYNTHETIC inputs.
Source-only: compiled, not executed by the author; expected values hand-traced."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import live_safety_parsers as P
from core import live_safety_evidence as E
FAILS = []
def check(n, c):
    print(("PASS " if c else "FAIL ") + n)
    if not c: FAILS.append(n)

def tf(older_ep=1, older_bps=100, newer_ep=10, newer_bps=300, auth="a", **o):
    st = {"olderTransferFee": {"epoch": older_ep, "maximumFee": 0, "transferFeeBasisPoints": older_bps},
          "newerTransferFee": {"epoch": newer_ep, "maximumFee": 0, "transferFeeBasisPoints": newer_bps}}
    if auth != "ABSENT": st["transferFeeConfigAuthority"] = auth
    st.update(o)
    return {"extensions": [{"extension": "transferFeeConfig", "state": st}]}
T22 = "spl-token-2022"

# transfer fee
r = P.parse_transfer_fee(T22, tf(), 12)
check("epoch >= newer.epoch selects newer", r["status"] == "resolved" and r["current_bps"] == 300 and r["older_bps"] == 100 and r["newer_bps"] == 300)
check("epoch == newer.epoch selects newer", P.parse_transfer_fee(T22, tf(), 10)["current_bps"] == 300)
check("epoch < newer.epoch selects older", P.parse_transfer_fee(T22, tf(), 9)["current_bps"] == 100)
r = P.parse_transfer_fee(T22, tf(), None)
check("epoch None -> epoch_selection_required, no current_bps", r["status"] == "epoch_selection_required" and r["current_bps"] is None)
for bad in (True, -1, "5", 5.0):
    check("bad epoch %r -> epoch_selection_required" % (bad,), P.parse_transfer_fee(T22, tf(), bad)["status"] == "epoch_selection_required")
check("authority set", P.parse_transfer_fee(T22, tf(), 12)["authority_set"] is True)
check("authority null is not set", P.parse_transfer_fee(T22, tf(auth=None), 12)["authority_set"] is False)
check("authority key absent -> unavailable", P.parse_transfer_fee(T22, tf(auth="ABSENT"), 12)["status"] == "unavailable")
check("authority wrong type -> unavailable", P.parse_transfer_fee(T22, tf(auth=5), 12)["status"] == "unavailable")
check("digit-string fields accepted", P.parse_transfer_fee(T22, {"extensions": [{"extension": "transferFeeConfig", "state": {
    "olderTransferFee": {"epoch": "1", "maximumFee": "0", "transferFeeBasisPoints": 0},
    "newerTransferFee": {"epoch": "10", "maximumFee": "0", "transferFeeBasisPoints": 0}, "transferFeeConfigAuthority": None}}]}, 12)["current_bps"] == 0)
check("bps over 10000 -> unavailable", P.parse_transfer_fee(T22, tf(newer_bps=10001), 12)["status"] == "unavailable")
check("bool epoch field -> unavailable", P.parse_transfer_fee(T22, tf(newer_ep=True), 12)["status"] == "unavailable")
check("missing sub-schedule -> unavailable", P.parse_transfer_fee(T22, {"extensions": [{"extension": "transferFeeConfig", "state": {"transferFeeConfigAuthority": None}}]}, 12)["status"] == "unavailable")
check("duplicate transferFeeConfig -> unavailable", P.parse_transfer_fee(T22, {"extensions": tf()["extensions"] * 2}, 12)["status"] == "unavailable")
check("malformed extension entry -> unavailable", P.parse_transfer_fee(T22, {"extensions": ["x"]}, 12)["status"] == "unavailable")
check("classic spl-token -> none", P.parse_transfer_fee("spl-token", {}, None)["status"] == "none")
check("token-2022 with other extensions only -> none", P.parse_transfer_fee(T22, {"extensions": [{"extension": "metadataPointer", "state": {}}]}, None)["status"] == "none")
check("token-2022 with empty list -> none", P.parse_transfer_fee(T22, {"extensions": []}, None)["status"] == "none")
check("token-2022 missing extensions key -> unavailable (not assumed none)", P.parse_transfer_fee(T22, {}, None)["status"] == "unavailable")
check("token-2022 extensions not a list -> unavailable", P.parse_transfer_fee(T22, {"extensions": {}}, None)["status"] == "unavailable")
check("unknown program -> unavailable", P.parse_transfer_fee("other", {}, None)["status"] == "unavailable")
check("non-str program / non-dict info -> unavailable", P.parse_transfer_fee(None, {}, None)["status"] == "unavailable" and P.parse_transfer_fee(T22, None, None)["status"] == "unavailable")

# holders
def acc(a, k="wallet", o="w"): return {"amount": a, "owner_kind": k, "owner": o}
h = P.parse_holders([acc(60)], 100, set())
check("wallet 60 of 100 -> measured covered 60", h["measured"] is True and h["covered_pct"] == 60.0 and h["program_held_pct"] == 0.0 and h["unclassified_pda_pct"] == 0.0)
h = P.parse_holders([acc(10, "program", "p"), acc(20, "pda", "x"), acc(30, "pda", "venue"), acc(5)], 100, {"venue"})
check("program, classified pda and unclassified pda split", h["measured"] and h["program_held_pct"] == 40.0 and h["unclassified_pda_pct"] == 20.0 and h["covered_pct"] == 65.0)
check("wallet whose owner the caller classified as venue counts as program held", P.parse_holders([acc(10, "wallet", "venue")], 100, ["venue"])["program_held_pct"] == 10.0)
for name, args in (("empty accounts", ([], 100, set())), ("non-list accounts", (None, 100, set())), ("zero supply", ([acc(1)], 0, set())),
                   ("bool supply", ([acc(1)], True, set())), ("nan supply", ([acc(1)], float("nan"), set())),
                   ("negative amount", ([acc(-1)], 100, set())), ("nan amount", ([acc(float("nan"))], 100, set())),
                   ("bool amount", ([acc(True)], 100, set())), ("unknown kind", ([acc(1, "dex")], 100, set())),
                   ("empty owner", ([acc(1, "wallet", "")], 100, set())), ("non-dict account", (["x"], 100, set())),
                   ("exceeds supply", ([acc(101)], 100, set())), ("program_owners wrong type", ([acc(1)], 100, "venue")),
                   ("program_owners non-str member", ([acc(1)], 100, {1}))):
    h = P.parse_holders(*args)
    check("%s -> not measured" % name, h["measured"] is False and h["covered_pct"] is None and isinstance(h["reason"], str))
check("total exactly supply measured", P.parse_holders([acc(100)], 100, set())["covered_pct"] == 100.0)

# composes with the typed evidence contract (fields exist and are accepted shapes)
POLICY = {"accepted_lp_statuses": ["burned"], "verified_extensions": [], "max_program_held_pct": None,
          "max_unclassified_pda_pct": None, "max_fee_bps": None, "allow_fee_authority": False}
f = P.parse_transfer_fee("spl-token", {}, None)
ev = {"lp_lock": {"status": "burned"}, "holders": P.parse_holders([acc(60)], 100, set()),
      "extensions": {"complete": True, "names": [], "review": []}, "transfer_fee": f}
check("parser outputs satisfy the evidence contract (synthetic control)", E.evaluate(ev, POLICY) == (True, []))
ev["transfer_fee"] = P.parse_transfer_fee(T22, tf(), None)
check("epoch_selection_required refuses in evidence", E.evaluate(ev, POLICY)[0] is False)
ev["transfer_fee"] = f; ev["holders"] = P.parse_holders([], 100, set())
check("unmeasured holders refuse in evidence", "holders_unmeasured_or_malformed" in E.evaluate(ev, POLICY)[1])
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
