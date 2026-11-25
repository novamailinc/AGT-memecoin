"""REAL-STACK REPLAY HARNESS rev2 (SOURCE ONLY, WRITTEN NOT RUN). NO entry-path claim: safety/veto/filler inputs are not captured,
so _consider_candidate is NOT driven here. Covers (1) real freshness guard functions on a REAL captured tape row with a TIGHT
env cap, (2) buyback accounting ASSERTIONS on a fixture-seeded position through the real quote path and real account.
Fixture items are labeled: seeded position/ledger state, TEST-ENGINEERING limits, tight test caps (not policy), replay clock.
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
    if len(sys.argv) != 2:
        print("usage: CAPTURE.jsonl"); return 4
    hdr, table = load_capture(sys.argv[1])
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
    q0, c0, bv0 = acct_qty(); cost0 = None; t0 = float(eng._read()["positions"][mint]["tokens"])
    T._LAST_BUYBACK_CHECK = 0.0
    done = T.maybe_buyback(eng, time.time())
    check("precondition: real maybe_buyback executed (so the invariants below are measured, not skipped)", len(done) == 1, repr(done))
    if len(done) == 1:
        q1, c1, bv1 = acct_qty(); t1 = float(eng._read()["positions"][mint]["tokens"]); cost = float(done[0]["cost"])
        say("INFO", "lane tokens %.6f -> %.6f ; account qty %.6f -> %.6f ; spendable cash %.6f -> %.6f ; book_value %.6f -> %.6f ; buyback cost %.6f"
            % (t0, t1, q0, q1, c0, c1, bv0, bv1, cost))
        check("I1 inventory: account qty follows lane tokens by the bought amount", abs((q1 - q0) - (t1 - t0)) <= 1e-6 * max(1.0, t1),
              "d_account=%.6f d_lane=%.6f" % (q1 - q0, t1 - t0))
        check("I2 no double use: spendable cash fell by the buyback cost", c1 <= c0 - cost + 1e-6, "dspendable=%.6f cost=%.6f" % (c1 - c0, cost))
        check("I3 conservation: account book_value unchanged (CAN PASS VACUOUSLY if nothing moved; not an accounting pass on its own)", abs(bv1 - bv0) <= 1e-6, "dbook=%.6f" % (bv1 - bv0))
        sn1 = pa.snapshot(); expo = float(sn1["cost_exposure"]); want = stake * (1.0 - float(T.LEG_A_FRAC)) + cost
        check("I3b lane-implied exposure == account cost_exposure (unsold original basis + buyback cost)", abs(expo - want) <= 1e-4, "account=%.6f lane_implied=%.6f" % (expo, want))
    # ---- I4 lifecycle (diagnostics collected, never an early exit before this point). Real poll_exits with the engine's own quote path
    # (captured bodies, no price_fn override). If exits cannot progress with recorded inputs, I4 is reported NOT RUN, never stubbed.
    if len(done) == 1:
        closes_before = len(eng._read().get("closes") or [])
        for _ in range(3):
            try:
                eng.poll_exits()
            except Exception as e:
                say("INFO", "poll_exits raised %s" % type(e).__name__)
        d4 = eng._read(); pos4 = d4["positions"].get(mint); closes4 = d4.get("closes") or []
        sn = pa.snapshot()
        accopen = [x for l in sn["lanes"].values() for x in l["positions"] if x["mint"] == mint]
        say("INFO", "I4 state: lane position present=%s pending_exit=%r legs_done=%r closes %d->%d ; account open lots for mint=%d qty=%s ; unrecorded=%d"
            % (pos4 is not None, (pos4 or {}).get("pending_exit"), {k: v.get("done") for k, v in ((pos4 or {}).get("legs") or {}).items()},
               closes_before, len(closes4), len(accopen), [x["qty"] for x in accopen], len(unrecorded)))
        try:
            say("INFO", "I4 context: _demo_mode()=%r (exits needing the demo filler/Jupiter raise on unrecorded hosts; never stubbed)" % (eng._demo_mode(),))
        except Exception as e:
            say("INFO", "I4 context: _demo_mode unavailable (%s)" % type(e).__name__)
        check("I4e open side: no lane position left with pending_exit or settlement_pending",
              pos4 is None or not (pos4.get("pending_exit") or pos4.get("settlement_pending")),
              repr({k: (pos4 or {}).get(k) for k in ("pending_exit", "settlement_pending")}))
        if pos4 is None and len(closes4) > closes_before:
            rec = closes4[-1]
            start = float(pa.start_balance())
            check("I4a account position fully closed (no open lot, qty 0)", not accopen, repr(accopen))
            check("I4b close record carries no pending_exit/settlement_pending (VACUOUS if the fields do not exist on close records; see I4e)", not rec.get("pending_exit") and not rec.get("settlement_pending"))
            lane_pnl = float(rec.get("pnl_usd"))
            acct_pnl = float(sn["realized_pnl"])
            check("I4c account realized PnL == lane close PnL (1e-4; _finalize rounds pnl to 4 decimals)", abs(acct_pnl - lane_pnl) <= 1e-4, "account=%.6f lane=%.6f" % (acct_pnl, lane_pnl))
            check("I4d final account cash == start + realized PnL (no cash created or lost)", abs(float(sn["cash"]) - (start + acct_pnl)) <= 1e-6,
                  "cash=%.6f start+pnl=%.6f" % (float(sn["cash"]), start + acct_pnl))
        else:
            say("INFO", "I4 NOT RUN TO CLOSE: lane position still open or no close with recorded inputs (see I4 state line); not stubbed")
            RES["NOTRUN"] = RES.get("NOTRUN", 0) + 1
    say("INFO", "unrecorded-request count (raised/None, never faked)=%d hosts=%s" % (len(unrecorded), sorted(set(unrecorded))))
    say("INFO", "limits = TEST-ENGINEERING fixture (not policy); replay clock = capture t0 + elapsed; ledger timestamps carry replay time")
    print("SUMMARY PASS=%d FAIL=%d INFO=%d I4_NOT_RUN=%d" % (RES["PASS"], RES["FAIL"], RES["INFO"], RES.get("NOTRUN", 0)))
    return 1 if RES["FAIL"] else (3 if RES.get("NOTRUN") else 0)


if __name__ == "__main__":
    sys.exit(main())
