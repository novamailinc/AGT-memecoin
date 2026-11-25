"""REAL-ENGINE BUYBACK GATE / ADOPTION / LEGACY / FAULT CHECKS (derived from the replay harness rev2; SOURCE ONLY, WRITTEN NOT RUN). Uses the recorded provider responses, the real make_engine, real account, real maybe_buyback; no fake engine; named faults only. Positive model-fill buyback is blocked fail-closed (impact gate) so this file checks refusal, crash ADOPTION, fault refusal and the legacy hold. REAL-STACK REPLAY HARNESS rev2 (SOURCE ONLY, WRITTEN NOT RUN). NO entry-path claim: safety/veto/filler inputs are not captured,
so _consider_candidate is NOT driven here. Covers (1) real freshness guard functions on a REAL captured tape row with a TIGHT
env cap, (2) buyback accounting ASSERTIONS on a fixture-seeded position through the real quote path and real account.
LABEL: the real add_to_position call in the adoption case uses GF.at_cost_marks (FIXTURE marks, engineering only; NOT real-provider mark acceptance). Fixture items are labeled: seeded position/ledger state, TEST-ENGINEERING limits, tight test caps (not policy), replay clock.
Real code: tranche.make_engine() (grad.py engine), real paper_account (scratch HOME), real entry_guard,
real commit_barrier, real sources.py parsing. The ONLY substituted layer is the HTTP byte transport
(sources._curl_bytes), which serves ACTUAL provider response bodies recorded by capture_public.py.
LABEL: replay of real captured responses at a past time. NOT live-market, NOT Stage B, NOT operational
qualification. Limits used are TEST-ENGINEERING limits (tests/_guard_fixture.LIMITS), NOT owner values.
Exit: 0 all checks pass; 1 a check failed; 3 NOT RUN (no/invalid real data or no qualifying candidate);
4 setup refused. Exit 3 is never a pass.
Usage: test_real_stack_replay-SOURCE-ONLY.py CAPTURE.jsonl"""
import base64, hashlib, io, json, os, sys, tempfile, time, logging
import urllib.request
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
ALLOW = {"api.dexscreener.com", "api.geckoterminal.com"}
RES = {"PASS": 0, "FAIL": 0, "INFO": 0}


def say(k, m):
    RES[k] += 1; print("%s %s" % (k, m), flush=True)


def check(name, cond, extra=""):
    say("PASS" if cond else "FAIL", name + ((" | " + extra) if extra else ""))


def load_capture(path):
    """Refuse anything without capture-generated provenance. Body hashes are recomputed here."""
    try:
        lines = [json.loads(l) for l in open(path) if l.strip()]
    except Exception as e:
        print("NOT RUN: capture unreadable (%s)" % type(e).__name__); sys.exit(3)
    if not lines or lines[0].get("kind") != "agt-real-capture":
        print("NOT RUN: no capture header"); sys.exit(3)
    h, recs = lines[0], lines[1:]
    tool = os.path.join(ROOT, "tests", "realstack", "capture_public.py")
    if h.get("tool_sha256") != hashlib.sha256(open(tool, "rb").read()).hexdigest():
        print("NOT RUN: capture tool hash differs from this tree's capture_public.py"); sys.exit(3)
    if h.get("n_requests") != len(recs) or not recs:
        print("NOT RUN: header request count mismatch"); sys.exit(3)
    table = {}
    for r in recs:
        if r.get("over_cap") or r.get("status") != 200:
            continue    # recorded failure: kept as provenance, never served (the engine then sees None, as it did live)
        body = base64.b64decode(r["body_b64"])
        if hashlib.sha256(body).hexdigest() != r["body_sha256"]:
            print("NOT RUN: body hash mismatch for %s" % urlsplit(r["url"]).hostname); sys.exit(3)
        if urlsplit(r["url"]).hostname not in ALLOW:
            print("NOT RUN: non-allowlisted host record"); sys.exit(3)
        table.setdefault(r["url"], body)
    return h, table


def main():
    if len(sys.argv) > 2:
        print("usage: [CAPTURE.jsonl]"); return 4
    cap = sys.argv[1] if len(sys.argv) == 2 else os.path.join(ROOT, "tests", "realstack", "data", "capture-20261125T064432Z.jsonl")
    say("INFO", "capture file %s sha256=%s" % (cap, hashlib.sha256(open(cap, "rb").read()).hexdigest()))
    hdr, table = load_capture(cap)
    say("INFO", "provenance: head=%s providers=%s requests=%d t0_utc=%.0f data=REAL-CAPTURED-REPLAY coverage=%s first-body-per-URL"
        % (hdr["repo_head"], ",".join(hdr["providers"]), hdr["n_requests"], hdr["t0_utc"], hdr.get("coverage")))
    T0 = float(hdr["t0_utc"]); wall0 = time.time(); _real_time = time.time
    unrecorded = []
    time.time = lambda: T0 + (_real_time() - wall0)   # explicit replay clock (capture time + real elapsed); gates NOT touched
    import _guard_fixture as GF
    GF.block_network()

    class _R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def serve(url):
        if url in table:
            return table[url]
        unrecorded.append(urlsplit(url).hostname); return None

    def urlopen_replay(req, data=None, timeout=None, **kw):
        url = req if isinstance(req, str) else req.full_url
        b = serve(url) if (data is None and (isinstance(req, str) or req.data is None)) else None
        if b is None:
            raise OSError("replay: URL not in capture (never fabricated)")
        return _R(b)
    urllib.request.urlopen = urlopen_replay

    def fresh_env(extra=None):
        home = tempfile.mkdtemp(prefix="realstack-")
        os.environ["FOREX_AGENT_HOME"] = home
        for k in [k for k in os.environ if k.startswith(("PAPER_ACCOUNT_", "AGT_"))]:
            del os.environ[k]
        os.environ["PAPER_ACCOUNT_ENABLED"] = "1"; GF.set_env(); os.environ.update(extra or {})
        for m in [m for m in sys.modules if m.startswith("daemon")]:
            del sys.modules[m]
        from daemon.launch_scanner import sources, tranche, paper_account as pa, entry_guard as eg
        sources._curl_bytes = lambda url, data=None, extra_headers=None, timeout=15: (serve(url) if data is None else None)
        return home, sources, tranche, pa, eg

    say("INFO", "PROVENANCE NOTE: replay serves the FIRST recorded body per URL; during capture the engine's repeat of the tape URL (quote call) was REFUSED "
        "and fell back. This is a recorded-provider-response engineering test, NOT an exact replay of the capture-time execution, NOT live-quote acceptance. "
        "Provider price differences in the raw data are unqualified (pool, liquidity, as-of, quote asset may differ).")
    mint = hdr["picked_mints"][0] if hdr.get("picked_mints") else None
    if not mint:
        print("NOT RUN: capture has no picked mint"); return 3

    # ---- (1) freshness: real guard functions, REAL captured tape row, TIGHT test cap (30 s; test value, not policy)
    home, S, T, pa, eg = fresh_env({"PAPER_ACCOUNT_MAX_DATA_AGE_S": "30"})
    check("fixture note: tight test cap resolves to 30 (TEST value; min(env, owner) rule)", pa.max_data_age_s() == 30.0, repr(pa.max_data_age_s()))
    row = (S.tape_batch_stats([mint], chain="solana") or {}).get(mint)
    if not row:
        print("NOT RUN: capture has no tape row for the picked mint"); return 3
    base = float(row.get("source_asof") or row.get("fetched_at"))
    say("INFO", "real tape row stamps: source_asof=%r fetched_at=%r received_at=%r tape_source=%r"
        % (row.get("source_asof"), row.get("fetched_at"), row.get("received_at"), row.get("tape_source")))
    check("fresh control: real row, now=base+5s, cap 30 -> None", eg.quote_age_refusal(row, pa.max_data_age_s(), base + 5.0) is None)
    r = eg.quote_age_refusal(row, pa.max_data_age_s(), base + 100.0)
    check("stale control: same real row, now=base+100s -> refused (age)", bool(r) and "exceeds max_data_age_s" in r, repr(r))
    r = eg.quote_age_refusal(row, pa.max_data_age_s(), base - 50.0)
    check("future-dated control -> refused", bool(r) and "future" in r, repr(r))
    fill = {"provenance": {"quote_requested_at": base}}
    check("post_quote fresh (now=base+5) -> None", eg.post_quote_refusal(fill, None, False, 30.0, base + 5.0) is None)
    r = eg.post_quote_refusal(fill, None, False, 30.0, base + 100.0)
    check("post_quote stale (now=base+100) -> refused", bool(r) and "exceeds" in r, repr(r))

    # ---- (2) buyback accounting on a FIXTURE-SEEDED position, real quote path (captured bodies), real account.
    # PREDECLARED CORRECTNESS RULES (shared capital; a header comment about temporary overstatement is NOT a correctness rule):
    #   I1 inventory: account position qty after buyback == lane unsold tokens (account qty before + lane tokens bought).
    #   I2 no double use: the buyback cost must leave the account's SPENDABLE cash (cash_spendable after <= before - cost), because the
    #      cost was already credited to shared cash by the leg-A credit and is otherwise spendable by any lane a second time.
    #   I3 conservation: account book_value (cash + cost_exposure) after == before (money moved from cash into the position, nothing created).
    # A violation exits 1 as a product finding; the legacy tranche.py "overstates cash until close" note is descriptive, not asserted as pass.
    home, S, T, pa, eg = fresh_env()
    eng = T.make_engine(); eng.ensure_ledger()
    px = eng.quote_price("solana", mint)
    check("precondition: real quote path returns a price from captured bodies", px and px > 0, "px=%r unrecorded=%d" % (px, len(unrecorded)))
    if not (px and px > 0):
        print("NOT RUN: no quote from capture"); return 3
    # INTEGRATION FACT (diagnostic, not an assertion, not a stake choice): the tranche lane's own stake constant vs the shared account cash.
    okx, whyx, _ = pa.try_debit(eng.LANE, "RSFACT25pump", float(T.NOTIONAL_USD), 1.0, "rs:fact:lane-stake")
    say("INFO", "INTEGRATION FACT: lane stake constant $%.2f vs shared account start cash $%.2f -> real try_debit at the lane stake: ok=%s why=%r"
        % (float(T.NOTIONAL_USD), float(pa.start_balance()), okx, whyx))
    # ENGINEERING SEED (labelled): stake $2.00 fits the shared $10 cash; it is NOT an operating stake choice.
    # entry is set so the REAL captured quote sits above leg A/B targets (mult = 1.05 x LEG_B_TARGET) and the peak is
    # px/0.6 so the real quote is a >=30% retrace from peak (buyback condition and leg-C trail both see real data).
    if okx is True:
        print("ABORT: the lane-stake diagnostic debit was ACCEPTED (an open lot now perturbs I1-I3); exit 4"); return 4
    stake = 2.0
    entry = px / (1.05 * float(T.LEG_B_TARGET))
    tokens = stake / entry
    ok, why, pid = pa.try_debit(eng.LANE, mint, stake, tokens, "rs:seed:open")
    check("precondition: real try_debit seeds the shared-account position", ok is True, repr((ok, why)))
    legqty = T.LEG_A_FRAC * tokens
    peak = px / 0.6
    gross = round(legqty * peak, 6)   # FIXTURE: leg A banked at the seeded peak; proceeds cover the buyback cost as the rule requires
    okc, whyc = pa.credit(eng.LANE, mint, gross, "rs:seed:legA", pos_id=pid, qty=legqty, fee=0.0,
                          price_kind="current_price_simulated_fill")   # the spendable kind a real sim leg credits with
    check("precondition: real credit of leg A", okc is True, repr((okc, whyc)))
    d = eng._read()
    d["positions"][mint] = {"mint": mint, "symbol": "RS", "chain": "solana", "lane": eng.LANE, "status": "open",
        "entry_ts": time.time(), "entry_price": entry, "quoted_price": entry, "peak_price": px / 0.6, "notional_usd": stake,
        "tokens": tokens, "realized_usd": gross, "pa_pos_id": pid, "tranche_credit": True,
        "tranche_credited": {"net": gross, "qty": legqty}, "gas_in_usd": 0.0,
        "legs": {"a": {"frac": T.LEG_A_FRAC, "target": T.LEG_A_TARGET, "done": True, "exit_price": peak, "realized_usd": gross},
                 "b": {"frac": T.LEG_B_FRAC, "target": T.LEG_B_TARGET, "done": False, "exit_price": 0, "realized_usd": 0},
                 "c": {"frac": T.LEG_C_FRAC, "trail": T.LEG_C_TRAIL, "peak": peak, "done": False, "exit_price": 0, "realized_usd": 0}},
        "entry_snapshot": {"liquidity": 0}}
    eng._write(d)
    say("INFO", "SCENARIO-DERIVED from the captured quote, NOT an observed entry or peak. FIXTURE-SEEDED state: leg A done, peak=quote/0.6, entry=quote/(1.05*LEG_B_TARGET), stake $2.00 (not market-observed); everything else real")
    def acct_qty():
        sn = pa.snapshot()
        return (sum(float(x["qty"]) for l in sn["lanes"].values() for x in l["positions"] if x["mint"] == mint),
                float(sn.get("cash_spendable", sn["cash"])), float(sn["book_value"]))
    q0, c0, bv0 = acct_qty(); cost0 = None; t0 = float(eng._pos_total_qty(eng._read()["positions"][mint]))
    import copy
    LOGS = []
    class _H(logging.Handler):
        def emit(self, rec): LOGS.append(rec.getMessage())
    logging.getLogger().addHandler(_H()); logging.getLogger().setLevel(logging.INFO)
    def has_log(sub): return any(sub in m for m in LOGS)
    def lanepos(): return eng._read()["positions"][mint]
    lp0 = copy.deepcopy(lanepos()); q0, c0, bv0 = acct_qty()
    sn_a = json.dumps(pa.snapshot(), sort_keys=True, default=str)
    # (1) NEGATIVE CONTROL that reaches the impact gate: freshness (pre-quote row), post-quote and account/lane checks pass, then the impact gate refuses
    T._LAST_BUYBACK_CHECK = 0.0
    done = T.maybe_buyback(eng, time.time())
    check("1 refused at the fail-closed impact gate (no positive buyback on the model-fill path)", done == [] and has_log("no Jupiter quote-impact measurement on the model-fill buyback path") and has_log("buyback blocked fail-closed"), repr((done, [m for m in LOGS if "buyback" in m][-2:])))
    check("1 refusal reached the gate AFTER freshness/post-quote (those refusal strings were not logged)", not any(has_log(x) for x in ("data age", "quote request time unknown", "post-quote", "refused at commit", "shared paper account unavailable", "simulated-fill position", "no account lot id", "adoption lookup failed", "identity mismatch", "refused by account", "no open leg")), repr(LOGS[-6:]))
    check("1 both books untouched by the refusal (lane record equal, account snapshot equal)", lanepos() == lp0 and json.dumps(pa.snapshot(), sort_keys=True, default=str) == sn_a)
    # (2) NAMED FAULT: the account interface raises -> refusal, never a lane-only buyback
    real_pa = eng._paper_account
    def _boom(): raise RuntimeError("INJECTED FAULT: account unavailable")
    eng._paper_account = _boom; T._LAST_BUYBACK_CHECK = 0.0
    done = T.maybe_buyback(eng, time.time())
    eng._paper_account = real_pa
    check("2 injected account fault: no buyback, lane record unchanged, no lane-only path", done == [] and lanepos() == lp0 and has_log("INJECTED FAULT: account unavailable") and has_log("failed:") and not has_log("ADOPTING") and not has_log("buyback blocked fail-closed"), repr((done, LOGS[-3:])))
    # (3) CRASH ADOPTION: the real account applied the add (engineering numbers, labelled) but the lane book was never written
    cost, bought = 1.0, 40000.0
    bop = "%s:buyback:%s:%s" % (eng.LANE, mint, pid)
    oka, whya = pa.add_to_position(eng.LANE, mint, pid, cost, bought, bop, marks=GF.at_cost_marks(pa))
    check("3 precondition: real account add applied (ENGINEERING numbers: cost $1.00, 40000 units; not a market fill)", oka is True, repr((oka, whya)))
    qa, ca, bva = acct_qty()
    check("3 account qty rose by exactly the bought units; spendable fell by exactly the cost", abs((qa - q0) - bought) < 1e-6 and abs((c0 - ca) - cost) < 1e-6, "dq=%.6f dcash=%.6f" % (qa - q0, c0 - ca))
    T._LAST_BUYBACK_CHECK = 0.0
    done = T.maybe_buyback(eng, time.time())
    lp = lanepos()
    check("3 adoption: maybe_buyback adopts the account-applied add (one record, applied cost/qty)", len(done) == 1 and abs(float(done[0]["tokens"]) - bought) < 1e-6 and abs(float(done[0]["cost"]) - cost) < 1e-6, repr(done))
    fb, fc = float(T.LEG_B_FRAC), float(T.LEG_C_FRAC)
    check("3 adoption: bought units allocated to the OPEN legs pro rata, none to the done leg, original tokens unchanged",
          abs(float(lp["legs"]["b"].get("buyback_qty", 0)) - bought * fb / (fb + fc)) < 1e-6 and abs(float(lp["legs"]["c"].get("buyback_qty", 0)) - bought * fc / (fb + fc)) < 1e-6
          and not lp["legs"]["a"].get("buyback_qty") and float(lp["tokens"]) == float(lp0["tokens"]),
          repr({k: lp["legs"][k].get("buyback_qty") for k in "abc"}))
    check("3 adoption: lane marked used, realized_usd reduced by exactly the cost", lp.get("buyback_used") is True and abs(float(lp["realized_usd"]) - (float(lp0["realized_usd"]) - cost)) < 1e-6, repr((lp.get("buyback_used"), lp.get("realized_usd"))))
    qb, cb, bvb = acct_qty()
    indep = float(lp0["tokens"]) * (fb + fc) + bought   # independent arithmetic, not the helper
    openq = sum(eng._leg_qty(lp, k) for k in ("a", "b", "c") if not lp["legs"][k]["done"])
    check("3 DIRECT inventory: account remaining qty == independent (tokens*(fb+fc)+bought) AND == sum of open-leg quantities (helper)", abs(qb - indep) < 1e-6 * max(1.0, qb) and abs(openq - qb) < 1e-6 * max(1.0, qb), "account=%.6f independent=%.6f open_legs=%.6f" % (qb, indep, openq))
    check("3 adoption adds no further account movement (cash/qty unchanged by the lane edit)", abs(cb - ca) < 1e-9 and abs(qb - qa) < 1e-9)
    check("3 detector sensitivity: reconciled position is NOT flagged legacy", eng._legacy_unreconciled_buyback(lp) is False)
    lp_snap = copy.deepcopy(lanepos()); sn_b = json.dumps(pa.snapshot(), sort_keys=True, default=str)
    T._LAST_BUYBACK_CHECK = 0.0
    done2 = T.maybe_buyback(eng, time.time())
    check("3 repeat is a no-op (buyback_used): nothing changes in either book", done2 == [] and lanepos() == lp_snap and json.dumps(pa.snapshot(), sort_keys=True, default=str) == sn_b)
    # (4) LEGACY record (pre-fix buyback: tokens inflated, no buyback_qty anywhere) on the real engine: automatic exits are HELD, nothing mutated
    leg = copy.deepcopy(lp0); leg["buyback_used"] = True; leg["tokens"] = float(lp0["tokens"]) * 1.25
    leg.setdefault("entry_features", {})["buyback"] = {"ts": 1.0, "price": px, "tokens": 0.25 * float(lp0["tokens"]), "cost": 1.0}
    for k in "abc": leg["legs"][k].pop("buyback_qty", None)
    leg.pop("buyback_qty_total", None)
    check("4 detector flags the legacy-shape record", eng._legacy_unreconciled_buyback(leg) is True)
    check("4 detector control: unflagged for a position with no buyback and for the reconciled one", eng._legacy_unreconciled_buyback(lp0) is False and eng._legacy_unreconciled_buyback(lp) is False)
    before_leg = copy.deepcopy(leg); sn_c = json.dumps(pa.snapshot(), sort_keys=True, default=str)
    r1 = eng._close_leg(leg, "b", px, "engineering-test")
    pe = leg.get("pending_exit") or {}
    check("4 _close_leg HELD with the visible flag, returned 0, leg not done", r1 == 0.0 and pe.get("why") == "legacy_unreconciled_buyback" and not leg["legs"]["b"]["done"], repr(pe))
    leg2 = copy.deepcopy(before_leg)
    r2 = eng._close_remaining(leg2, px, "engineering-test")
    pe2 = leg2.get("pending_exit") or {}
    check("4 _close_remaining HELD with the visible flag, returned 0", r2 == 0.0 and pe2.get("why") == "legacy_unreconciled_buyback", repr(pe2))
    after_leg = copy.deepcopy(leg); after_leg.pop("pending_exit", None)
    check("4 nothing else mutated (lane fields equal apart from pending_exit; account snapshot equal)", after_leg == before_leg and json.dumps(pa.snapshot(), sort_keys=True, default=str) == sn_c)
    # (4b) a PRE-EXISTING pending_exit reason is preserved (not overwritten) by the legacy hold; no repair
    leg3 = copy.deepcopy(before_leg); leg3["pending_exit"] = {"leg": "b", "why": "account_remaining_qty_mismatch", "ts": 1.0}
    r3 = eng._close_leg(leg3, "b", px, "engineering-test"); r4 = eng._close_remaining(copy.deepcopy(leg3), px, "engineering-test")
    check("4b legacy hold keeps an existing pending_exit reason (not overwritten), returns 0, nothing else changed", r3 == 0.0 and r4 == 0.0 and leg3["pending_exit"] == {"leg": "b", "why": "account_remaining_qty_mismatch", "ts": 1.0} and {k: v for k, v in leg3.items() if k != "pending_exit"} == {k: v for k, v in before_leg.items() if k != "pending_exit"}, repr(leg3.get("pending_exit")))
    say("INFO", "NOT COVERED (labelled): the pre-fire adoption lookup fault (applied_add raising), quote_age(None), the account refusal paths of add_to_position on the real engine, a positive model-fill buyback (blocked by design until a real Jupiter quote-impact measure exists), exits/close lifecycle after a buyback (Jupiter marks unrecorded), sim-fill positions, entry path.")
    say("INFO", "unrecorded-request count (raised/None, never faked)=%d hosts=%s" % (len(unrecorded), sorted(set(unrecorded))))
    say("INFO", "limits = TEST-ENGINEERING fixture (not policy); replay clock = capture t0 + elapsed; seeded state SCENARIO-DERIVED")
    print("SUMMARY PASS=%d FAIL=%d INFO=%d" % (RES["PASS"], RES["FAIL"], RES["INFO"]))
    return 1 if RES["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
