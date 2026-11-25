"""core.live_safety_adapter: maps the CURRENT instant_safety report (combined bytes md5 2c7b66b1 plus the typed-authority
diff f890ffbc) into the typed evidence contract of core.live_safety_evidence. Pure, no I/O, no clock, no policy values.
NEW file, authored 2026-11-24; nothing calls it. Its purpose is to make the producer's emitter gap explicit: for every
evidence item the report cannot support, it answers a NON-passing status instead of inventing one.
Returns (evidence, gaps): gaps is a list of "item:reason" strings naming what the report does not carry.
Per item (what the report can and cannot say):
  authorities  from instant.mint_authority_status / freeze_authority_status (typed diff); absent or odd -> unavailable.
  lp_lock      no positive lock source exists in the report. The flag "LP lock unverified" -> unverified; otherwise unavailable.
  holders      from instant.holder_rows + supply_raw via parse_holders when the emitter supplies them; otherwise NOT measured.
               program_owners is the caller's classification and defaults to empty (nothing is classified here).
  extensions   complete only when instant.token2022_extensions is a list of non-empty strings and no flag says unreadable,
               unrecognized, unparseable or incomplete; every flag marked "(unverified" or "review" becomes a review item.
  transfer_fee key absent -> unavailable; None -> none; dict -> its own status if it is one of the contract statuses AND
               config_authority_status is "set" or "revoked" (authority_set = it is "set"); otherwise unavailable.
Limit: it reads what the producer stated; it cannot show the parser saw every extension."""
from core.live_safety_evidence import FEE_STATUSES
from core import live_safety_parsers as _P
from core import live_safety_extras as _X

_BAD_EXT_WORDS = ("unreadable", "unrecognized", "unparseable", "incomplete", "undecod")


def _flags(rep):
    f = rep.get("flags") if isinstance(rep, dict) else None
    return [x for x in f if isinstance(x, str)] if isinstance(f, list) else None


def report_to_evidence(rep, program_owners=()):
    gaps = []
    inst = rep.get("instant") if isinstance(rep, dict) else None
    flags = _flags(rep)
    if not isinstance(inst, dict) or flags is None:
        gaps.append("report:no_instant_block_or_flags")
        inst = {}
        flags = flags or []
    ev = {}
    # authorities (typed statuses only; the legacy mint_authority strings are NOT used: None there is ambiguous)
    au = {}
    for out_key, src in (("mint_authority", "mint_authority_status"), ("freeze_authority", "freeze_authority_status")):
        s = inst.get(src)
        if s in ("revoked", "set", "unavailable"):
            au[out_key] = {"status": s}
        else:
            au[out_key] = {"status": "unavailable"}
            gaps.append("authorities:%s_missing" % src)
    ev["authorities"] = au
    # lp lock
    if any("LP lock unverified" in f for f in flags):
        ev["lp_lock"] = {"status": "unverified"}
    else:
        ev["lp_lock"] = {"status": "unavailable"}
        gaps.append("lp_lock:no_source")
    # holders
    # Holders: from the emitter's raw rows (instant.holder_rows, instant.supply_raw) when present. program_owners is
    # the CALLER's classification of program/venue owners; the default is empty, so every off-curve owner stays an
    # unclassified PDA. Nothing here classifies owners.
    rows, sup = inst.get("holder_rows"), inst.get("supply_raw")
    if rows is not None and sup is not None:
        ev["holders"] = _P.parse_holders(rows, sup, program_owners)
        if ev["holders"]["measured"] is not True:
            gaps.append("holders:unmeasured:%s" % ev["holders"].get("reason"))
    else:
        ev["holders"] = {"measured": False, "program_held_pct": None, "unclassified_pda_pct": None, "covered_pct": None}
        gaps.append("holders:no_coverage_or_unclassified_pda")
    # Epoch evidence (local read time) for extras; None when the emitter did not supply it.
    ei = inst.get("epoch_info")
    if isinstance(ei, dict):
        ev["epoch"] = _X.epoch_item(ei.get("epoch"), ei.get("fetched_at"))
    else:
        ev["epoch"] = None
        gaps.append("epoch:not_supplied")
    # extensions
    names = inst.get("token2022_extensions")
    names_ok = isinstance(names, list) and all(isinstance(n, str) and n for n in names)
    bad = [f for f in flags if any(w in f.lower() for w in _BAD_EXT_WORDS)]
    review = [{"name": "flag", "reason": f[:80]} for f in flags if "(unverified" in f or "review" in f.lower()]
    complete = names_ok and not bad
    ev["extensions"] = {"complete": complete, "names": list(names) if names_ok else [], "review": review}
    # Typed parse status from the emitter (when present): anything but complete is True makes the extension item
    # incomplete. Absent (older emitter) leaves the legacy flag-word logic unchanged; no_fee_proven requires it anyway.
    xp = inst.get("extensions_parse")
    if xp is not None and not (isinstance(xp, dict) and xp.get("complete") is True):
        complete = False
        ev["extensions"]["complete"] = False
        gaps.append("extensions:parse_incomplete")
    if not names_ok:
        gaps.append("extensions:names_missing")
    if bad:
        gaps.append("extensions:parser_reported_unreadable")
    # transfer fee
    if "transfer_fee" not in inst:
        ev["transfer_fee"] = {"status": "unavailable", "current_bps": None, "newer_bps": None, "authority_set": False}
        gaps.append("transfer_fee:key_absent")
    else:
        tf = inst["transfer_fee"]
        if tf is None:
            ev["transfer_fee"] = {"status": "none", "current_bps": None, "newer_bps": None, "authority_set": False}
        elif isinstance(tf, dict) and tf.get("status") == "ambiguous_duplicate":
            # duplicate transferFeeConfig entries: the emitter refuses to pick one (never last-wins)
            ev["transfer_fee"] = {"status": "unavailable", "current_bps": None, "newer_bps": None, "authority_set": False}
            gaps.append("transfer_fee:duplicate_extension_ambiguous")
        elif isinstance(tf, dict) and tf.get("status") in FEE_STATUSES:
            # authority_set comes ONLY from the emitter's typed config_authority_status ("set" or "revoked"). A missing,
            # malformed or "unavailable" status makes the whole fee item unavailable. The legacy flag string is not used.
            st = tf.get("config_authority_status")
            if st in ("set", "revoked"):
                ev["transfer_fee"] = {"status": tf["status"], "current_bps": None, "newer_bps": None,
                                      "authority_set": st == "set"}
                if tf["status"] == "epoch_selection_required":
                    gaps.append("transfer_fee:epoch_not_supplied")
            else:
                ev["transfer_fee"] = {"status": "unavailable", "current_bps": None, "newer_bps": None, "authority_set": False}
                gaps.append("transfer_fee:authority_status_missing_or_unavailable")
        else:
            ev["transfer_fee"] = {"status": "unavailable", "current_bps": None, "newer_bps": None, "authority_set": False}
            gaps.append("transfer_fee:malformed")
    return ev, gaps
