"""REAL ENGINE + REAL ACCOUNT + REAL GUARD REFUSAL CHECKS (SOURCE ONLY, WRITTEN NOT RUN). NO DATA, NO NETWORK, NO STUBS.
Real: tranche.make_engine() (grad.py engine module), real killswitch, real paper_account in a fresh scratch FOREX_AGENT_HOME,
real entry_guard.mandatory_entry_refusal / quote_age_refusal / post_quote_refusal / commit_barrier, real grad_trader_cycle.commit_refusal.
Fixture inputs, labeled: (a) env limits from tests/_guard_fixture.LIMITS are TEST-ENGINEERING values, not owner policy;
(b) the rows given to quote_age_refusal / post_quote_refusal are hand-built timing rows that exercise the REAL guard functions
(no market data is claimed); (c) the seeded account position uses the real try_debit.
Exit 0 all pass, 1 any fail. Prints PASS/FAIL/INFO lines and a SUMMARY line."""
import hashlib, os, sys, tempfile, time
sys.dont_write_bytecode = True
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tests"))
import _guard_fixture as GF
GF.block_network()
RES = {"PASS": 0, "FAIL": 0, "INFO": 0}
HOMES = []


def say(k, m):
    RES[k] += 1; print("%s %s" % (k, m), flush=True)


def check(name, cond, extra=""):
    say("PASS" if cond else "FAIL", name + ((" | " + extra) if extra else ""))


def env(enabled=True, gates=True, drop=(), extra=None):
    home = tempfile.mkdtemp(prefix="rsguard-"); HOMES.append(home)
    os.environ["FOREX_AGENT_HOME"] = home
    for k in [k for k in os.environ if k.startswith(("PAPER_ACCOUNT_", "AGT_"))]:
        del os.environ[k]
    if enabled:
        os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
    if gates:
        GF.set_env()
    for k in drop:
        os.environ.pop(k, None)
    os.environ.update(extra or {})
    for m in [m for m in sys.modules if m.startswith("daemon")]:
        del sys.modules[m]
    from daemon.launch_scanner import tranche, paper_account as pa, entry_guard as eg
    eng = tranche.make_engine()
    return home, tranche, eng, pa, eg


def dirhash(home):
    h = hashlib.sha256()
    for r, _, fs in sorted(os.walk(home)):
        for f in sorted(fs):
            p = os.path.join(r, f)
            h.update(os.path.relpath(p, home).encode()); h.update(open(p, "rb").read())
    return h.hexdigest()


def main():
    say("INFO", "fixture limits = tests/_guard_fixture.LIMITS (TEST-ENGINEERING, not owner values); no data, no network, no stubs")
    # C1 account not enabled
    home, T, eng, pa, eg = env(enabled=False)
    why = eg.mandatory_entry_refusal(eng.LANE)
    check("C1 account not enabled -> guard refuses", bool(why) and "not enabled" in why, repr(why))
    # C2 enabled, gates off
    home, T, eng, pa, eg = env(gates=False)
    why = eg.mandatory_entry_refusal(eng.LANE)
    check("C2 gates off -> guard refuses", bool(why) and "risk gates are off" in why, repr(why))
    # C3 gates on but NO limits (true state today)
    home, T, eng, pa, eg = env(gates=False, extra={"PAPER_ACCOUNT_GATES_ENABLED": "1"})
    why = eg.mandatory_entry_refusal(eng.LANE)
    names = ("max_daily_loss_usd", "max_drawdown_pct", "max_exposure_pct")
    check("C3 gates on, no limits -> guard names all three limits", bool(why) and all(n in why for n in names), repr(why))
    ra = eg.quote_age_refusal({"fetched_at": time.time(), "received_at": time.time()}, pa.max_data_age_s(), time.time())
    check("C3b no max_data_age_s -> quote_age_refusal refuses naming it", bool(ra) and "max_data_age_s" in ra, repr(ra))
    rp = eg.post_quote_refusal({}, None, False, pa.max_data_age_s(), time.time())
    check("C3c no max_data_age_s -> post_quote_refusal refuses naming it", bool(rp) and "max_data_age_s" in rp, repr(rp))
    # C4 each limit unset individually
    for var, nm in (("PAPER_ACCOUNT_MAX_DAILY_LOSS_USD", "max_daily_loss_usd"), ("PAPER_ACCOUNT_MAX_DRAWDOWN_PCT", "max_drawdown_pct"),
                    ("PAPER_ACCOUNT_MAX_EXPOSURE_PCT", "max_exposure_pct")):
        home, T, eng, pa, eg = env(drop=(var,))
        why = eg.mandatory_entry_refusal(eng.LANE)
        others = [n for n in names if n != nm]
        check("C4 only %s unset -> guard names it and not the others" % nm,
              bool(why) and nm in why and not any(o in why for o in others), repr(why))
    # C5 positive control for the guard itself (so refusals above are not vacuous)
    home, T, eng, pa, eg = env()
    check("C5 all on with fixture limits -> guard returns None (control, TEST-ENGINEERING limits)",
          eg.mandatory_entry_refusal(eng.LANE) is None)
    # C6 cycle flag on, lane named but not registered -> refusal through real commit_refusal
    home, T, eng, pa, eg = env(extra={"AGT_GRAD_CYCLE_ENABLED": "1", "AGT_GRAD_CYCLE_LANES": "tranche"})
    why = eg.mandatory_entry_refusal(eng.LANE)
    check("C6 cycle flag on + lane named, not registered -> guard refuses", bool(why) and "mandatory entry guard" in why, repr(why))
    try:
        bar = eg.commit_barrier(eng, 0.5); ctx = type(bar).__name__
        say("INFO", "commit_barrier with flag on returned %s (real _fault_lock path)" % ctx)
    except Exception as e:
        say("INFO", "commit_barrier flag on raised %s (caller treats as refusal)" % type(e).__name__)
    # C7 refusal leaves account and lane ledger byte-identical
    for label, kw in (("not enabled", dict(enabled=False)), ("gates off", dict(gates=False))):
        home, T, eng, pa, eg = env(**kw)
        eng.ensure_ledger()
        before = dirhash(home)
        why = eg.mandatory_entry_refusal(eng.LANE)
        check("C7 (%s) guard call leaves state dir byte-identical" % label, bool(why) and dirhash(home) == before)
    # C8 exits/credits still work while entries are refused (real try_debit then real credit with gates off)
    home, T, eng, pa, eg = env()
    ok, why, pid = pa.try_debit(eng.LANE, "RSGUARDMINTpump", 2.5, 1000.0, "rs:op:open")
    check("C8a real try_debit under fixture limits reserves cash", ok is True, repr((ok, why, pid)))
    os.environ.pop("PAPER_ACCOUNT_GATES_ENABLED", None)
    refused = eg.mandatory_entry_refusal(eng.LANE)
    check("C8b entries now refused (gates off)", bool(refused))
    ok2, why2 = pa.credit(eng.LANE, "RSGUARDMINTpump", 2.5, "rs:op:close", pos_id=pid, qty=1000.0, fee=0.0,
                          price_kind=getattr(getattr(pa, "PRICE_KIND", None), "UNKNOWN", None))
    say("INFO", "C8c credit while entries refused -> %r (price_kind arg depends on module API)" % ((ok2, why2),))
    check("C8c credit path not blocked by the entry refusal", ok2 is True, repr((ok2, why2)))
    # C9 killswitch: lane halted -> gate false and buyback returns [] (NOT a guard control; exits before the guard)
    home, T, eng, pa, eg = env()
    eng._ks.halt_lane(eng.LANE, "rs-test")
    check("C9 killswitch halted -> gate False", eng._ks.gate(eng.LANE) is False)
    T._LAST_BUYBACK_CHECK = 0.0
    check("C9b halted lane -> maybe_buyback returns [] (exits before guard; not a guard control)", T.maybe_buyback(eng, time.time()) == [])
    say("INFO", "scratch homes (listed, not deleted): %s" % ",".join(HOMES))
    say("INFO", "NOT COVERED: owner_config looser-than-env tighten-only (owner_config format not exercised), real cycle FAULT (only the not-registered refusal)")
    print("SUMMARY PASS=%d FAIL=%d INFO=%d" % (RES["PASS"], RES["FAIL"], RES["INFO"]))
    return 0 if RES["FAIL"] == 0 else 1



if __name__ == "__main__":
    sys.exit(main())
