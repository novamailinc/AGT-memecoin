"""core.live_safety_parsers: NEW IMPLEMENTATION (authored 2026-11-24 16:15 PST). NOT the recovered old module.
The original bytes (tip 4d4ebf27, md5 d433cdbf) were lost in a workspace reset and could not be re-verified; this file
is written from the interfaces its consumers use (core.live_safety_extras calls parse_transfer_fee(program, info, epoch)
and parse_holders(accounts, supply, program_owners); core.live_safety_evidence reads the result shapes) and from the
jsonParsed shapes visible in daemon/launch_scanner/instant_safety.py (md5 2c7b66b1). Pure: no I/O, no clock, no defaults
for any policy value. Every malformed, absent or ambiguous input answers a non-passing status; nothing is guessed.
Other functions the old module may have exported are unknown and NOT provided here.

parse_transfer_fee(program, info, epoch) -> {"status": none|resolved|epoch_selection_required|unavailable,
    "current_bps": int|None, "older_bps": int|None, "newer_bps": int|None, "authority_set": bool}
  program: the jsonParsed data.program string. "spl-token" -> none (classic mint has no extensions).
  "spl-token-2022" -> inspect info["extensions"]; anything else, or a missing/non-list extensions key, is unavailable.
  Selection rule (same as instant_safety's recorded rule): newer schedule if epoch >= newer.epoch, else older.
  epoch None or not a non-negative int -> epoch_selection_required with current_bps None.

parse_holders(accounts, supply, program_owners) -> {"measured": bool, ...}
  accounts: list of {"amount": raw number, "owner_kind": "wallet"|"program"|"pda", "owner": non-empty str}
  supply: raw supply, same unit as amount, finite and > 0.
  program_owners: set/frozenset/list/tuple of owner strings the CALLER has classified as program or venue held.
  "program" kind, or any owner in program_owners, counts as program held; "pda" kind not in program_owners counts as
  unclassified PDA; "wallet" counts toward coverage only. Empty accounts, unknown kinds, negative or non-finite
  amounts, or a total above supply are NOT measured."""
import math

_U64 = 2 ** 64 - 1


def _int_field(v, upper):
    """Non-negative integer from an int or a digit string; bool, float, signed or oversized values are rejected."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and v.isascii() and v.isdigit():
        n = int(v)
    else:
        return None
    return n if 0 <= n <= upper else None


def _fee_result(status, current=None, older=None, newer=None, auth=False):
    return {"status": status, "current_bps": current, "older_bps": older, "newer_bps": newer, "authority_set": auth}


def _schedule(sub):
    if not isinstance(sub, dict):
        return None
    ep = _int_field(sub.get("epoch"), _U64)
    mx = _int_field(sub.get("maximumFee"), _U64)
    bps = _int_field(sub.get("transferFeeBasisPoints"), 10000)
    if ep is None or mx is None or bps is None:
        return None
    return {"epoch": ep, "bps": bps}


def parse_transfer_fee(program, info, epoch):
    if not isinstance(program, str) or not isinstance(info, dict):
        return _fee_result("unavailable")
    if program == "spl-token":
        return _fee_result("none")
    if program != "spl-token-2022":
        return _fee_result("unavailable")
    exts = info.get("extensions")
    if not isinstance(exts, list):
        return _fee_result("unavailable")
    found = []
    for e in exts:
        if not isinstance(e, dict) or not isinstance(e.get("extension"), str) or not e["extension"]:
            return _fee_result("unavailable")   # a malformed entry could be hiding a fee
        if e["extension"] == "transferFeeConfig":
            found.append(e)
    if not found:
        return _fee_result("none")
    if len(found) != 1:
        return _fee_result("unavailable")
    state = found[0].get("state")
    if not isinstance(state, dict):
        return _fee_result("unavailable")
    older, newer = _schedule(state.get("olderTransferFee")), _schedule(state.get("newerTransferFee"))
    if older is None or newer is None:
        return _fee_result("unavailable")
    if "transferFeeConfigAuthority" not in state:
        return _fee_result("unavailable")       # absent is not "no authority"
    a = state["transferFeeConfigAuthority"]
    if a is None:
        auth = False
    elif isinstance(a, str) and a.strip():
        auth = True
    else:
        return _fee_result("unavailable")
    ep_ok = isinstance(epoch, int) and not isinstance(epoch, bool) and 0 <= epoch <= _U64
    if not ep_ok:
        return _fee_result("epoch_selection_required", None, older["bps"], newer["bps"], auth)
    current = newer["bps"] if epoch >= newer["epoch"] else older["bps"]
    return _fee_result("resolved", current, older["bps"], newer["bps"], auth)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _unmeasured(reason):
    return {"measured": False, "reason": reason, "covered_pct": None, "program_held_pct": None, "unclassified_pda_pct": None}


def parse_holders(accounts, supply, program_owners):
    if not _num(supply) or supply <= 0:
        return _unmeasured("supply_invalid")
    if not isinstance(accounts, list) or not accounts:
        return _unmeasured("accounts_missing_or_empty")
    if not isinstance(program_owners, (set, frozenset, list, tuple)) or any(
            not isinstance(o, str) for o in program_owners):
        return _unmeasured("program_owners_malformed")
    known = set(program_owners)
    total = prog = pda = 0.0
    for a in accounts:
        if not isinstance(a, dict):
            return _unmeasured("account_malformed")
        amt, kind, owner = a.get("amount"), a.get("owner_kind"), a.get("owner")
        if not _num(amt) or amt < 0 or kind not in ("wallet", "program", "pda") or not isinstance(owner, str) or not owner:
            return _unmeasured("account_malformed")
        total += amt
        if kind == "program" or owner in known:
            prog += amt
        elif kind == "pda":
            pda += amt
    covered = total * 100.0 / supply
    if not math.isfinite(covered) or covered > 100.0 + 1e-6:
        return _unmeasured("accounts_exceed_supply")
    return {"measured": True, "reason": None, "covered_pct": covered,
            "program_held_pct": prog * 100.0 / supply, "unclassified_pda_pct": pda * 100.0 / supply}
