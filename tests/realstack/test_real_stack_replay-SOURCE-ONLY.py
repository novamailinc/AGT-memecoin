"""REAL-STACK TRANCHE HARNESS (SOURCE ONLY, WRITTEN NOT RUN).
Real code: tranche.make_engine() (grad.py engine), real paper_account (scratch HOME), real entry_guard,
real commit_barrier, real sources.py parsing. The ONLY substituted layer is the HTTP byte transport
(sources._curl_bytes), which serves ACTUAL provider response bodies recorded by capture_public.py.
LABEL: replay of real captured responses at a past time. NOT live-market, NOT Stage B, NOT operational
qualification. Limits used are TEST-ENGINEERING limits (tests/_guard_fixture.LIMITS), NOT owner values.
Exit: 0 all checks pass; 1 a check failed; 3 NOT RUN (no/invalid real data or no qualifying candidate);
4 setup refused. Exit 3 is never a pass.
Usage: test_real_stack_replay-SOURCE-ONLY.py CAPTURE.jsonl"""
import base64, hashlib, json, os, sys, tempfile, time, logging
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
ALLOW = {"api.dexscreener.com", "api.geckoterminal.com", "price.jup.ag", "lite-api.jup.ag", "api.jup.ag"}
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
        body = base64.b64decode(r["body_b64"])
        if hashlib.sha256(body).hexdigest() != r["body_sha256"]:
            print("NOT RUN: body hash mismatch for %s" % urlsplit(r["url"]).hostname); sys.exit(3)
        if urlsplit(r["url"]).hostname not in ALLOW or r["status"] != 200:
            print("NOT RUN: non-allowlisted host or non-200 record"); sys.exit(3)
        table.setdefault(r["url"], body)
    return h, table


def main():
    if len(sys.argv) != 2:
        print("usage: CAPTURE.jsonl"); return 4
    hdr, table = load_capture(sys.argv[1])
    say("INFO", "provenance: head=%s providers=%s requests=%d t0_utc=%.0f data=REAL-CAPTURED-REPLAY"
        % (hdr["repo_head"], ",".join(hdr["providers"]), hdr["n_requests"], hdr["t0_utc"]))
    T0 = float(hdr["t0_utc"]); wall0 = time.time()
    _real_time = time.time
    unrecorded = []

    # explicit replay clock: engine sees capture time + real elapsed. Gates and ages are NOT loosened.
    time.time = lambda: T0 + (_real_time() - wall0)
    import _guard_fixture as GF
    GF.block_network()

    def fresh_env(enabled=True, gates=True, drop=None):
        home = tempfile.mkdtemp(prefix="realstack-")
        os.environ["FOREX_AGENT_HOME"] = home
        for k in [k for k in os.environ if k.startswith("PAPER_ACCOUNT_") or k.startswith("AGT_")]:
            del os.environ[k]
        if enabled:
            os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
        if gates:
            GF.set_env()
        if drop:
            os.environ.pop(drop, None)
        for m in [m for m in sys.modules if m.startswith("daemon") or m in ("sources", "tranche", "grad", "paper_account", "entry_guard")]:
            del sys.modules[m]
        from daemon.launch_scanner import sources, tranche, paper_account as pa, entry_guard as eg

        def replay(url, data=None, extra_headers=None, timeout=15):
            if data is None and url in table:
                return table[url]
            unrecorded.append(urlsplit(url).hostname); return None
        sources._curl_bytes = replay
        return home, sources, tranche, pa, eg

    def candidate(eng):
        cs = eng.fetch_candidates()
        for c in cs:
            if eng.is_graduation_candidate(c, time.time()) is not None and c["mint"] in hdr["picked_mints"]:
                return c
        return None

    # ---- positive path
    home, S, T, pa, eg = fresh_env()
    eng = T.make_engine(); eng.ensure_ledger()
    check("guard positive: mandatory_entry_refusal is None with account+gates+limits on",
          eg.mandatory_entry_refusal(eng.LANE) is None)
    cand = candidate(eng)
    if cand is None:
        print("NOT RUN: no qualifying real graduation candidate in capture"); return 3
    before = pa.snapshot()
    res = eng._consider_candidate(cand, time.time())
    say("INFO", "positive entry result=%r" % (res,))
    snap = pa.snapshot()
    opened = bool(res and res[0] is True) if isinstance(res, tuple) else bool(res)
    if not opened:
        say("INFO", "entry refused by REAL path (reason above); reported, not forced")
    check("positive: real entry opened and account cash debited by the stake", opened and snap and before and
          float(snap["cash"]) < float(before["cash"]), "cash %s -> %s" % (before and before["cash"], snap and snap["cash"]))
    d = eng._read(); pos = d["positions"].get(cand["mint"])
    check("positive: lane position carries a shared-account pos id", bool(pos and pos.get("pa_pos_id")))

    # ---- negative controls: same real candidate, each must reach the GUARD (reason text), not an earlier exit
    cases = [("account not enabled", dict(enabled=False), "not enabled"),
             ("gates off", dict(gates=False), "risk gates are off"),
             ("drawdown limit unset", dict(drop="PAPER_ACCOUNT_MAX_DRAWDOWN_PCT"), "max_drawdown_pct")]
    for name, kw, needle in cases:
        h2, S2, T2, pa2, eg2 = fresh_env(**kw)
        e2 = T2.make_engine(); e2.ensure_ledger()
        c2 = candidate(e2)
        r2 = e2._consider_candidate(c2, time.time()) if c2 else None
        ok = isinstance(r2, tuple) and r2[0] is False and needle in str(r2[1]) and "mandatory entry guard" in str(r2[1])
        check("negative control (%s): refused AT the guard" % name, ok, "result=%r" % (r2,))
        s2 = pa2.snapshot()
        check("negative control (%s): no position in lane ledger, no account debit" % name,
              not e2._read()["positions"] and (s2 is None or not any(l["positions"] for l in s2["lanes"].values())))

    # ---- scenario-derived probe (NOT market data): buyback vs shared account. INFO only, no assertion of design.
    home, S, T, pa, eg = fresh_env()
    eng = T.make_engine(); eng.ensure_ledger()
    cand = candidate(eng)
    r = eng._consider_candidate(cand, time.time()) if cand else None
    pos = eng._read()["positions"].get(cand["mint"]) if cand else None
    if not (pos and pos.get("pa_pos_id")):
        say("INFO", "buyback probe not reached: no real entry (%r)" % (r,))
    else:
        pre = pa.snapshot()
        d = eng._read(); p = d["positions"][cand["mint"]]
        gross = round(0.25 * float(p["tokens"]) * float(p["entry_price"]) * 2.0, 6)
        p["legs"]["a"].update(done=True, exit_price=2 * float(p["entry_price"]), realized_usd=gross)
        p["realized_usd"] = gross; p["peak_price"] = 2.0 * float(p["entry_price"]) * 1.0
        eng._write(d)
        say("INFO", "SCENARIO-DERIVED ledger state (leg A marked done, peak=2x entry); real maybe_buyback follows")
        T._LAST_BUYBACK_CHECK = 0.0
        done = T.maybe_buyback(eng, time.time())
        post = pa.snapshot(); lp = eng._read()["positions"].get(cand["mint"]) or {}
        accpos = [x for l in (post or {"lanes": {}})["lanes"].values() for x in l["positions"] if x["mint"] == cand["mint"]]
        say("INFO", "buyback executed=%d lane tokens=%s realized_usd=%s | account cash %s -> %s ; account qty=%s"
            % (len(done), lp.get("tokens"), lp.get("realized_usd"), pre["cash"], post["cash"],
               accpos[0]["qty"] if accpos else None))
        if done:
            say("INFO", "DISCREPANCY=%s (lane tokens vs account qty; account cash change vs buyback cost)"
                % ("YES" if (accpos and abs(float(accpos[0]["qty"]) - float(lp.get("tokens") or 0)) > 1e-9)
                   or post["cash"] == pre["cash"] else "NO"))
    say("INFO", "unrecorded-request count (served None, never faked)=%d hosts=%s" % (len(unrecorded), sorted(set(unrecorded))))
    print("SUMMARY PASS=%d FAIL=%d INFO=%d" % (RES["PASS"], RES["FAIL"], RES["INFO"]))
    return 0 if RES["FAIL"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
