#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). NEVER RUN without main's explicit clearance for this exact blob. Spawns CHILD processes (some SIGKILL themselves) through the same contained helper chain as
tests/real_lane_entry_recovery_harness-SOURCE-ONLY.py (temp home, real grad.py + paper_account + demo fill, network blocked, shared $10 DEMO account only, no executor, no systemd, no supervisor, no activation).

PURPOSE (T1/T2 of the launcher qualification plan): drive the REAL main loop of scripts/grad-trader (loaded by path, its main() executed with the real loop body), at the combined lineage d17f832a, with the two
flags named separately: AGT_GRAD_ENTRY_INTENT_ENABLED (call it EI) and AGT_GRAD_CYCLE_ENABLED (call it CYC). Four-case matrix (T1), each a SIGKILL at the instrumented entry seam inside the real loop, then a fresh
child runs the real loop for 5 passes:
  S1 EI=1 CYC=0  combined source, cycle flag off: the legacy order (scan, time exits, exits), NO cycle latch.
  S2 EI=1 CYC=1  combined source, cycle flag on: _gtc.main_cycle -> real run_cycle(defer_scan=True) + scan_phase, the interrupted_prior_cycle latch exists.
  S3 EI=0 CYC=0  baseline for S1 (what the entry-intent flag changes).
  S4 EI=0 CYC=1  baseline for S2.
Plus T2, two real processes against one temp ledger after the same kind of kill (the asymmetry the reviewer named): process A = the real grad-trader main loop (journals when CYC=1); process B = a realtime-sync-STYLE
process that calls GR.poll_exits(price_fn) directly regardless of CYC (it does not call register or main_cycle, so it has no cycle journal). C1 EI=1 CYC=0, C2 EI=1 CYC=1.
Recovery, management and entry permission are asserted SEPARATELY. Reviewer semantics, documented not endorsed: unresolved or frozen intents must block new entries; successful adoption may restore entry
permission in the legacy loop because that path has no latch; neither the absence of a latch alone nor its presence is claimed to be approved or a defect. The harness never calls clear_fault, never writes fault files.
EVIDENCE CLASSES (kept distinct): (1) this harness = combined-source behavior of the real script main loop, flag-off and flag-on, under fixtures; (2) the cycle harness = direct run_cycle calls; (3) current deployed default behavior
is the legacy loop with both flags unset and is NOT a separate run: S3 is its closest stand-in, and prod-comb scripts/grad-trader (44b334ae) differs from the d17f832a script (6846b62f), which this harness loads.
CONTAINMENT / SOURCE-REVIEW POINTS: tests._guard_fixture.block_network() runs BEFORE any repo import (helper chain); temp FOREX_AGENT_HOME per scenario (the singleton pidfile, ledger, journals, fault files all live there); no token,
chat id or config read; the script's feed is replaced by a fake that never opens a socket (sys.modules["pumpportal"] is a fake namespace), the script's killswitch import is a fake namespace; GR.fetch_candidates returns [];
candidates enter through the REAL _scan_once watchlist and consider_candidate path via GR._migration_rows (the stream-to-candidate conversion is stubbed, disclosed). time inside the script is a shim whose sleep ends the pass (no real sleeping,
no real clock change for other modules). V3 (test-only; replaces the sl3 FAIL expectation in the S3/S4 flag-off baseline: S3 is REPLAY-MANAGED (lane position re-created bound to the existing account op/pos, credited by the loop's own exits), S4 is GATED UNMANAGED; the old name-list-vs-pos_id test was vacuous and is replaced by pa_pos_id/entry_op comparisons; v2 file, blob 3a43f7b5, and the sl3 log are preserved unchanged). V2 SUCCESSOR (test-only; the sl1 ABORT 93 and sl2 36/9/13 outcomes of blob ce857b63 are preserved unchanged): (a) token checks read the token FILES directly (the B.tok(...,0.0) idiom returns None without looking, which made the sl2 LOOP_START read fail and the SURVIVED-absent test vacuous), with a positive sensitivity control; (b) the C1/C2 post-run assertions use the final shared journal (sl2 C1 used B's earlier snapshot); (c) PRICE PATH, proven from d17f832a source: the real script calls _grad.poll_exits() with no price_fn, _poll_exits prices a held mint from jup_price.get_prices_observed FIRST, and tests._guard_fixture.install() (called in the boot chain) replaces that function with an at-cost feed (basis/qty), so exits saw cost, never the GR.quote_price stub. This file wraps that same seam (jup_price.get_prices_observed) ONLY while GR.poll_exits runs, returning entry_price*MULT for lane positions; entry quotes and account marks outside poll_exits still see the at-cost feed. Source label is asserted per position (last_price_source == 'jup-v3', last quote px / entry_price == MULT). BARE-IMPORT VARIANT (this file; the sibling real_supported_loop_entry_recovery_harness-SOURCE-ONLY.v3.py with the alias is preserved unchanged for comparison): NO sys.modules alias is installed. The helper chain's package import of grad is replaced (asserted textual edit) by the SAME bare `import grad as GR` the production scripts use, so fixtures patch the bare-named module that scripts/grad-trader then imports by itself; whether 'daemon.launch_scanner.grad' ever appears in sys.modules is MEASURED at boot, at LOOP_START, at the kill seam, per pass and at the end, and any appearance FAILS a check (it is detected, never suppressed). Live identity is asserted through the real register()/hook path: T._ENGINES['grad'] is the bare module, abort/begin/end hooks are installed on it only when AGT_GRAD_CYCLE_ENABLED=1, and the legacy marks hook registered in paper_account is the bare module's function. Process B (realtime-sync-style) never registers, so only package-absence is asserted there. OLD TEXT, SUPERSEDED HERE (alias variant): the script does `import grad as _grad` (bare name). In the children sys.modules["grad"] is ALIASED to the same module object the fixtures patch
(daemon.launch_scanner.grad); in production the bare name and the package name can be two module objects. Each child reports git blob hashes of grad.py, grad_cycle.py, grad_trader_cycle.py, scripts/grad-trader and grad_entry_intent.py,
and the parent asserts them against the d17f832a values, plus alias identity (M._grad is GR, sys.modules["grad"] is GR, M._gtc is the imported grad_trader_cycle).
CLAIM LIMITS: one machine, SIGKILL at ONE instrumented seam (not power loss, not SIGTERM, not supervisor restart: the stale singleton pidfile left by the SIGKILL is reclaimed by the real guard_singleton, which is measured, not the supervisor),
fixture price and safety stubs, demo mode, grad lane only. B is NOT the realtime-sync script. A passing run would NOT show the live deployment is safe to enable, and it measures cadence only as pass counts, not wall time.
Every assertion names one expected value; hypotheses are labeled; baselines that measure a hazard (S3 duplicate-entry attempt on the killed mint) are printed as INFO and not asserted either way."""
import importlib.util, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
def _load(n, f):
    sp = importlib.util.spec_from_file_location(n, os.path.join(HERE, f)); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m
E = _load("eibase3", "real_lane_entry_recovery_harness-SOURCE-ONLY.py")
B = E.B; check = B.check; FAILS = B.FAILS

# Prelude: E.PRE with exactly two textual changes (asserted): the cycle flag comes from argv[5] instead of the helper's fixed "1", and the helper's own T.register(GR) is dropped (the real script registers itself).
PRE_S = E.PRE
for _a, _b in (('os.environ["AGT_GRAD_CYCLE_ENABLED"] = "1"', 'os.environ["AGT_GRAD_CYCLE_ENABLED"] = sys.argv[5]'), ("T.register(GR)", "pass"),
               ("from daemon.launch_scanner import demo_fill as DF, grad as GR", "from daemon.launch_scanner import demo_fill as DF\nimport grad as GR   # BARE, exactly like scripts/grad-trader; no package grad import here\nPKG_AT_BOOT = 'daemon.launch_scanner.grad' in sys.modules")):
    assert PRE_S.count(_a) == 1, "helper chain changed: %r count %d" % (_a, PRE_S.count(_a))
    PRE_S = PRE_S.replace(_a, _b)

COMMON = PRE_S + r'''
import hashlib, types
SCHED = json.loads(sys.argv[4]); KILLPASS = int(sys.argv[6]); NPASS = int(sys.argv[7]); MULT = float(sys.argv[8]); ROLE = sys.argv[9]
PASSNO = [0]; PASSES = []; CALLS = {"scan": [], "cand": []}
GR._migration_rows = lambda feed, now: [mkcand(k) for k in SCHED.get(str(PASSNO[0]), [])]
GR.fetch_candidates = lambda pages=None: []
def _q(chain, mint, spot_index=None):
    p = GR.open_positions().get(mint)
    return float(p.get("entry_price") or 0.00007) * MULT if p else 0.00007
GR.quote_price = G.stamped_quote(GR, _q)
FakeJup.mult = MULT
_real_scan = GR.scan_once
def _scan(*a, **k):
    r = _real_scan(*a, **k); CALLS["scan"].append([PASSNO[0], {x: r.get(x) for x in ("opened", "skipped", "watching", "stream", "pools")} if isinstance(r, dict) else None]); return r
GR.scan_once = _scan
_real_cc = GR.consider_candidate
def _cc(cand, now, *a, **k):
    r = _real_cc(cand, now, *a, **k); CALLS["cand"].append([PASSNO[0], str(cand.get("mint")), bool(r[0]), str(r[1])[:90], bool(r[2])]); return r
GR.consider_candidate = _cc
import daemon.launch_scanner.jup_price as _JP
_UP = [False]; _orig_gpo = _JP.get_prices_observed
def _gpo(mints, now=None):
    r = dict(_orig_gpo(mints, now))
    if _UP[0]:
        _lp = GR.open_positions()
        for _m in (mints or []):
            _p = _lp.get(_m)
            if isinstance(_p, dict) and float(_p.get("entry_price") or 0) > 0: r[_m] = (float(_p["entry_price"]) * MULT, time.time())
    return r
_JP.get_prices_observed = _gpo                                   # FIXTURE (disclosed): same seam the production poll_exits reads first; active only inside GR.poll_exits
_real_pe = GR.poll_exits
def _pe(*a, **k):
    _UP[0] = True
    try: return _real_pe(*a, **k)
    finally: _UP[0] = False
GR.poll_exits = _pe
class _Seam(set):
    def add(self, x):
        if PASSNO[0] >= KILLPASS:
            token("SEAM_REACHED", pa_pos_id=str(x), at_pass=PASSNO[0], intents=intents_map(), accts=acct(), rec_files=rec_files(), pidfile=os.path.exists(os.path.join(home, "run", "grad-trader.pid")), live_ident=_live_ident())
            os.kill(os.getpid(), signal.SIGKILL)
        set.add(self, x)
if KILLPASS >= 0: GR._INFLIGHT_OPEN = _Seam()
def _blob(rel):
    b = open(os.path.join(os.environ["AGT_REPO_ROOT"], rel), "rb").read(); return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()
IDENT = {rel: _blob(rel) for rel in ("daemon/launch_scanner/grad.py", "daemon/grad_cycle.py", "daemon/grad_trader_cycle.py", "scripts/grad-trader", "daemon/launch_scanner/grad_entry_intent.py")}
def _snap(extra=None):
    F = full(); cred = {}; deb = []
    for e in F["j"]["events"]:
        if e.get("event") == "credit": cred[str(e.get("pos_id"))] = cred.get(str(e.get("pos_id")), 0) + 1
        if e.get("event") == "debit" and e.get("lane") == "grad": deb.append(str(e.get("pos_id")))
    try: fr = GC._read_fault(GR) or {}
    except Exception as ex: fr = {"reasons": [{"reason": "unreadable:%r" % (ex,)}]}
    PASSES.append({"pass": PASSNO[0], "lane": sorted(GR.open_positions()), "psrc": {m: [p.get("last_price_source"), ((p.get("quote_journal") or [{}])[-1]).get("px"), p.get("entry_price")] for m, p in GR.open_positions().items() if isinstance(p, dict)}, "acct": [x["pos_id"] for x in F["acct"]], "credits_by_pos": cred, "debit_pos": deb, "rec_files": F["rec_files"],
                   "pkg_grad": PKGN in sys.modules, "n_acts": len(ACTS), "fault_reasons": [e.get("reason") for e in (fr.get("reasons") or [])], "scan_calls": len(CALLS["scan"]), "extra": extra})
PKGN = "daemon.launch_scanner.grad"
def _live_ident():
    hooks = PA.legacy_hooks()
    return {"pkg_grad_loaded": PKGN in sys.modules, "sysmod_grad_is_GR": sys.modules.get("grad") is GR, "GR_name": GR.__name__, "registered_is_GR": T._ENGINES.get("grad") is GR, "registered_lanes": sorted(T._ENGINES),
            "abort_hook": callable(getattr(GR, "_ABORT_HOOK", None)), "begin_hook": callable(getattr(GR, "_FAILURE_BEGIN_HOOK", None)), "end_hook": callable(getattr(GR, "_FAILURE_END_HOOK", None)),
            "legacy_hook_registered": "grad" in hooks, "legacy_hook_is_GR_fn": hooks.get("grad") is GR.legacy_unproven_pos_ids}
PRE_STATE = full()
def _final(role, rc, extra=None):
    print(json.dumps({"name": name, "role": role, "rc": rc, "pre": PRE_STATE, "passes": PASSES, "calls": CALLS, "after": full(), "acts": ACTS, "clear_log_exists": os.path.exists(GC._clear_log_path(GR)),
                      "clear_enabled": GC.CLEAR_COMMIT_ENABLED, "fault_active": list(GC.fault_active(GR)), "mode": list(T.mode_info(GR)), "ident": IDENT, "live_ident": _live_ident(), "pkg_at_boot": PKG_AT_BOOT, "extra": extra}, default=str))
'''

LOOP_A = r'''
class _Done(BaseException): pass
class _FakeFeed:
    def start(self): pass
    def drain(self): return []
PKG_BEFORE_SCRIPT = PKGN in sys.modules; SYSGRAD_BEFORE = sys.modules.get("grad") is GR   # NO alias: the script's bare `import grad` finds the already-imported bare module by itself
sys.modules["pumpportal"] = types.SimpleNamespace(PumpPortalFeed=_FakeFeed)
sys.modules["killswitch"] = types.SimpleNamespace(check_lane=lambda lane: {"lane": lane})
import importlib.machinery, importlib.util as _iu
_p = os.path.join(os.environ["AGT_REPO_ROOT"], "scripts", "grad-trader")
_ld = importlib.machinery.SourceFileLoader("grad_trader_script", _p); _sp = _iu.spec_from_loader("grad_trader_script", _ld); M = _iu.module_from_spec(_sp); _ld.exec_module(M)
class _ShimTime:
    time = staticmethod(time.time)
    def sleep(self, s):
        _snap(); PASSNO[0] += 1
        if PASSNO[0] >= NPASS: raise _Done()
M.time = _ShimTime()
IDENT2 = {"bare_grad_in_sysmodules_before_script": SYSGRAD_BEFORE, "no_pkg_grad_before_script": not PKG_BEFORE_SCRIPT, "no_pkg_grad_at_boot": not PKG_AT_BOOT, "no_pkg_grad_after_script_import": PKGN not in sys.modules, "GR_name_is_grad": GR.__name__ == "grad", "script_grad_is_GR": M._grad is GR, "script_gtc_is_T": M._gtc is T, "script_flag_on": M._FLAG_ON, "has_grad": M._HAS_GRAD}
token("LOOP_START", mode=list(T.mode_info(GR)), pidfile_at_start=os.path.exists(os.path.join(home, "run", "grad-trader.pid")), ident2=IDENT2, live_ident_pre_main=_live_ident())
if ROLE.endswith("go"): wait_for(os.path.join(CTL, "GO"), 30.0)
try: rc = M.main()
except _Done: rc = "done"
except SystemExit as _e: rc = "exit:%r" % (_e.code,)
if ROLE == "kill": token("SURVIVED", rc=str(rc)); os._exit(99)
_final(ROLE, rc, {"ident2": IDENT2})
'''

LOOP_B = r'''
wait_for(os.path.join(CTL, "GO"), 30.0)
for _i in range(NPASS):
    PASSNO[0] = _i
    _n = len(GR.poll_exits(price_fn=gpf(MULT)))
    _snap({"n_closed": _n}); time.sleep(0.05)
_final(ROLE, "done")
'''
KILL = COMMON + LOOP_A
RESTART = COMMON + LOOP_A
PROC_B = COMMON + LOOP_B

EXPECT = {"daemon/launch_scanner/grad.py": "f74349dff06a8e39e5d46800f21bc0c30c4f1faa", "daemon/grad_cycle.py": "c2b627f2f4c234528f63697221663f12530d7e0f", "daemon/grad_trader_cycle.py": "44fac6aa9ac6d5a71aa1998b079329eacda77fc1",
          "scripts/grad-trader": "6846b62f0024de3ef96485e71ffd8beb2dc9ccdc", "daemon/launch_scanner/grad_entry_intent.py": "38d201575f832f39800a77456dada655fc0ff804"}
KILLED_MINT = "GradMint1pump"; FRESH_MINT = "GradMint9pump"

def cash_ok(a):
    ev = a["j"]["events"]; exp = 10.0 - sum(float(e["stake"]) for e in ev if e["event"] == "debit") + sum(float(e.get("net") or 0) for e in ev if e["event"] == "credit")
    return abs(float(a["j"]["cash"]) - exp) < 1e-5, exp
def deb_pos(a, sub): return [e.get("pos_id") for e in a["j"]["events"] if e["event"] == "debit" and e.get("lane") == "grad" and sub in str(e.get("pos_id"))]
def cum(passes, pid): return [p["credits_by_pos"].get(pid, 0) for p in passes]
def first_pass(passes, pred): return next((p["pass"] for p in passes if pred(p)), None)
def tokfile(h, name, tag):
    p = os.path.join(h, "ctl", name + "." + tag)
    if not os.path.exists(p): return None
    try: return json.load(open(p))
    except Exception as e: return {"unreadable": repr(e)}
def ctl_names(h): return sorted(os.listdir(os.path.join(h, "ctl")))
def live_ok(li, cyc, registered=True):
    # expected: bare module registered, hooks iff cycle flag on, legacy hook is the bare module's function, package grad NOT loaded
    if not li or li.get("pkg_grad_loaded") or not li.get("sysmod_grad_is_GR") or li.get("GR_name") != "grad": return False
    if not registered: return not li.get("registered_is_GR")
    h = cyc == "1"
    return bool(li.get("registered_is_GR")) and li.get("registered_lanes") == ["grad"] and li.get("abort_hook") == h and li.get("begin_hook") == h and li.get("end_hook") == h
def ident_ok(r): return all((r.get("ident") or {}).get(k) == v for k, v in EXPECT.items()) and all(bool(v) for k, v in ((r.get("extra") or {}).get("ident2") or {}).items() if k != "script_flag_on")

def kill_phase(h, lab, ei, cyc):
    pk = B.spawn(KILL, h, "k" + lab, ei, json.dumps({"0": [1]}), cyc, "0", "6", "1.0", "kill")
    try: pk.wait(timeout=min(30, max(1.0, B.left())))
    except Exception: raise B.Stop("kill child did not exit")
    t = tokfile(h, "k" + lab, "SEAM_REACHED"); ls = tokfile(h, "k" + lab, "LOOP_START"); names = ctl_names(h)
    check("%s kill child (real scripts/grad-trader main loop) hit the seam (token file read directly), died by SIGKILL (rc %s), loop mode at start %s; control-dir listing %s contains SEAM_REACHED and LOOP_START and NO SURVIVED file (non-vacuous: the same reader finds the present tokens)" % (lab, pk.returncode, (ls or {}).get("mode"), [n for n in names if n.startswith("k" + lab)]),
          t is not None and not t.get("unreadable") and pk.returncode == -9 and ls is not None and ("k%s.SURVIVED" % lab) not in names and ("k%s.SEAM_REACHED" % lab) in names and ("k%s.LOOP_START" % lab) in names
          and ls.get("mode") == (["on", ""] if cyc == "1" else ["off", "flag_off"]))
    li = (t or {}).get("live_ident"); pm = (ls or {}).get("live_ident_pre_main")
    check("%s BARE IDENTITY at the kill seam (inside the real main, after the real register()): package grad loaded %s, registered_is_GR %s, hooks abort/begin/end %s/%s/%s (expected all %s), legacy hook registered %s; before main nothing registered (pre-main registered_is_GR %s, package loaded %s)" %
          (lab, (li or {}).get("pkg_grad_loaded"), (li or {}).get("registered_is_GR"), (li or {}).get("abort_hook"), (li or {}).get("begin_hook"), (li or {}).get("end_hook"), cyc == "1", (li or {}).get("legacy_hook_registered"), (pm or {}).get("registered_is_GR"), (pm or {}).get("pkg_grad_loaded")),
          live_ok(li, cyc) and live_ok(pm, cyc, registered=False) and (ls or {}).get("ident2", {}).get("no_pkg_grad_after_script_import") is True)
    return t, ls

def common_pre(lab, ei, t, r, cyc="?"):
    pid = t["pa_pos_id"]; pre = r["pre"]
    ex = r.get("extra") or {}; li = r.get("live_ident") or {}
    check("%s BARE IDENTITY (restart child, end of run, cyc=%s): package grad never loaded (pkg_grad_loaded %s, per-pass flags %s, at boot %s), sys.modules['grad'] is the patched module named %r, T._ENGINES['grad'] is it (%s, lanes %s), hooks abort/begin/end %s/%s/%s installed iff cycle flag on, legacy hook registered %s and is the bare module's function %s" %
          (lab, cyc, li.get("pkg_grad_loaded"), sorted(set(p.get("pkg_grad") for p in r["passes"])), r.get("pkg_at_boot"), li.get("GR_name"), li.get("sysmod_grad_is_GR"), li.get("registered_is_GR"), li.get("registered_lanes"), li.get("abort_hook"), li.get("begin_hook"), li.get("end_hook"), li.get("legacy_hook_registered"), li.get("legacy_hook_is_GR_fn")),
          live_ok(li, cyc) and not any(p.get("pkg_grad") for p in r["passes"]) and r.get("pkg_at_boot") is False and li.get("legacy_hook_registered") is True and li.get("legacy_hook_is_GR_fn") is True)
    lane_ids = [x.get("pa_pos_id") for x in pre["lane"]["positions"].values()]
    check("%s pre-restart state: killed pos_id %s held by the account (%s), NOT in the lane (%s), record present %s (expected %s)" % (lab, pid, [x["pos_id"] for x in pre["acct"]], lane_ids, pre["rec_files"], "1 file" if ei == "1" else "none"),
          pid in [x["pos_id"] for x in pre["acct"]] and pid not in lane_ids and ((len(pre["rec_files"] or []) == 1) if ei == "1" else not pre["rec_files"]))
    check("%s script/module identities: blobs equal the d17f832a values and alias/identity flags all true (%s)" % (lab, (r.get("extra") or {}).get("ident2")), ident_ok(r))
    return pid

def recovery_and_management(lab, r, pid):
    passes = r["passes"]; a = r["after"]; classes = [x[2] for x in r["acts"]]; c = cum(passes, pid)
    check("%s RECOVERY: exactly one adoption (classes %s == ['B']), record dir empty (%s)" % (lab, classes, a["rec_files"]), classes == ["B"] and not a["rec_files"])
    adopt_pass = first_pass(passes, lambda p: p["n_acts"] > 0)
    check("%s MANAGEMENT: the recovered position is credited (cumulative credits per pass %s, first credited pass %s, adoption pass %s; credits are ladder-leg sales, NOT a close; not a cadence claim)" % (lab, c, first_pass(passes, lambda p: p["credits_by_pos"].get(pid, 0) > 0), adopt_pass),
          c[-1] >= 1)
    ps = passes[-1].get("psrc", {}).get(KILLED_MINT)
    print("INFO %s PRICE PATH per pass for the killed mint [last_price_source, last quote px, entry_price]: %s" % (lab, [p.get("psrc", {}).get(KILLED_MINT) for p in passes]))
    check("%s PRICE PATH (fixture proof): the adopted position was priced through the production poll_exits source seam (last_price_source %r == 'jup-v3', last px / entry_price = %s == %s)" % (lab, (ps or [None])[0], (round(float(ps[1]) / float(ps[2]), 6) if ps and ps[1] and ps[2] else None), 3.0),
          bool(ps) and ps[0] == "jup-v3" and bool(ps[1]) and bool(ps[2]) and abs(float(ps[1]) / float(ps[2]) - 3.0) < 1e-6)
    ok, exp = cash_ok(a)
    check("%s ECONOMICS: cash %s == 10 - sum(debit stakes) + sum(credit nets) = %s, and the killed mint was debited exactly once (%s)" % (lab, a["j"]["cash"], round(exp, 6), deb_pos(a, KILLED_MINT)), ok and len(deb_pos(a, KILLED_MINT)) == 1)
    return adopt_pass

def entry_permission_latch(lab, r, pid):
    passes = r["passes"]; calls = r["calls"]
    check("%s ENTRY PERMISSION (latch on): fault interrupted_prior_cycle recorded (%s), the injected scan path is never reached (scan calls %d == 0, candidate considerations %s), no debit beyond the killed one (%s), fault still active (%s), no clear log (%s), CLEAR_COMMIT_ENABLED %s, mode %s" %
          (lab, any("interrupted_prior_cycle" in p["fault_reasons"] for p in passes), len(calls["scan"]), calls["cand"], deb_pos(r["after"], "Mint"), r["fault_active"], r["clear_log_exists"], r["clear_enabled"], r["mode"]),
          any("interrupted_prior_cycle" in p["fault_reasons"] for p in passes) and len(calls["scan"]) == 0 and not any(x[1] == FRESH_MINT for x in calls["cand"]) and len(deb_pos(r["after"], "Mint")) == 1
          and bool(r["fault_active"][0]) and r["clear_log_exists"] is False and r["clear_enabled"] is False and r["mode"] == ["on", ""])

def t1(lab, ei, cyc):
    h = B.newhome(); t, _ = kill_phase(h, lab, ei, cyc)
    p, r = E.run(h, RESTART, "r" + lab, ei, json.dumps({"1": [9]}), cyc, "-1", "5", "3.0", "restart")
    if not r or "passes" not in r:
        check("%s restart child produced a result (rc %s)" % (lab, p.returncode), False); return
    check("%s restart child ran the real loop for 5 passes and ended by the pass counter (rc %s, loop result %s)" % (lab, p.returncode, r.get("rc")), p.returncode == 0 and r.get("rc") == "done" and len(r["passes"]) == 5)
    pid = common_pre(lab, ei, t, r, cyc); passes = r["passes"]; a = r["after"]
    print("INFO %s singleton pidfile left by the SIGKILL (present at the seam: %s); the restart child's guard_singleton must reclaim the stale file for the loop to run at all (loop ran: %s)" % (lab, t.get("pidfile"), r.get("rc") == "done"))
    print("INFO %s MEASURED per pass: lane %s, credits for the killed pos_id %s, n_acts %s, fault reasons %s, scan calls %s; candidate considerations %s" % (lab, [p["lane"] for p in passes], cum(passes, pid), [p["n_acts"] for p in passes], [p["fault_reasons"] for p in passes], [p["scan_calls"] for p in passes], r["calls"]["cand"]))
    if ei == "1":
        adopt_pass = recovery_and_management(lab, r, pid)
        if cyc == "0":
            fresh = [x for x in r["calls"]["cand"] if x[1] == FRESH_MINT]
            check("%s ENTRY PERMISSION (no latch, legacy loop): no cycle fault ever recorded (%s), no clear log (%s); the absence of a latch is MEASURED, not endorsed" % (lab, [p["fault_reasons"] for p in passes], r["clear_log_exists"]),
                  all(p["fault_reasons"] == [] for p in passes) and r["clear_log_exists"] is False and r["clear_enabled"] is False and r["mode"] == ["off", "flag_off"])
            fresh_opened = [x for x in fresh if x[2]]
            check("%s ENTRY PERMISSION after adoption, HYPOTHESIS (reviewer: adoption may restore permission in this loop): the fresh candidate %s was considered (%s) and admitted exactly once, only after the adoption pass %s; debits are the killed + the fresh pos_id (%s)" %
                  (lab, FRESH_MINT, fresh, adopt_pass, deb_pos(a, "Mint")),
                  len(fresh_opened) == 1 and adopt_pass is not None and fresh_opened[0][0] >= adopt_pass and len(deb_pos(a, "Mint")) == 2 and len(deb_pos(a, FRESH_MINT)) == 1)
        else:
            entry_permission_latch(lab, r, pid)
    else:
        baseline_flag_off(lab, cyc, r, pid)

def baseline_flag_off(lab, cyc, r, pid):
    """V3 (replaces the sl3 FAIL expectation; the v2 file and the sl3 log are preserved). EI flag off. Common: no recovery action, no record, exactly ONE account debit for the killed mint.
    S3 (cycle flag off): REPLAY-MANAGED. The pass-0 scan reconsiders the persisted watchlist mint and the lane position is re-created BOUND to the existing account op/pos (sl2/sl3 observation, mechanism untraced), then the
    loop's own exits credit its ladder legs. S4 (cycle flag on): GATED UNMANAGED. The cycle fault blocks the scan and the lane never holds the position."""
    passes = r["passes"]; a = r["after"]; c = cum(passes, pid)
    kd = [e for e in a["j"]["events"] if e["event"] == "debit" and e.get("lane") == "grad" and KILLED_MINT in str(e.get("pos_id"))]
    kop = kd[0].get("op_id") if kd else None
    lp = (a["lane"]["positions"] or {}).get(KILLED_MINT)
    credits = [e for e in a["j"]["events"] if e["event"] == "credit" and e.get("pos_id") == pid]
    check("%s FLAG-OFF NO RECOVERY: no intent action (classes %s == []), no record dir content (%s), and exactly ONE account debit for the killed mint (%s, op_id %s)" % (lab, [x[2] for x in r["acts"]], a["rec_files"], [e.get("pos_id") for e in kd], kop),
          r["acts"] == [] and not a["rec_files"] and len(kd) == 1 and kop is not None)
    if cyc == "1":
        lane_pa = [x.get("pa_pos_id") for x in a["lane"]["positions"].values()]
        check("%s GATED UNMANAGED (S4): the lane holds no position at any pass (per-pass lane %s) and none bound to %s (lane pa_pos_ids %s), the account still holds it (%s), credits for it %s == 0, credit events %d == 0" %
              (lab, [p["lane"] for p in passes], pid, lane_pa, [x["pos_id"] for x in a["acct"]], c, len(credits)),
              all(p["lane"] == [] for p in passes) and pid not in lane_pa and pid in [x["pos_id"] for x in a["acct"]] and c[-1] == 0 and len(credits) == 0)
        entry_permission_latch(lab, r, pid)
    else:
        first = [x for x in r["calls"]["cand"] if x[1] == KILLED_MINT]
        ops = [e.get("op_id") for e in credits]
        check("%s REPLAY-MANAGED (S3): the killed mint was reconsidered by the scan and opened (candidate log %s), its lane position is BOUND to the existing account position (lane pa_pos_id %s == %s, lane entry_op %s == the killed debit op_id %s), no second debit for it, and its ladder legs were credited in the shared journal (%d credit events, op_ids %s, all of the form grad:leg:*:%s)" %
              (lab, first, (lp or {}).get("pa_pos_id"), pid, (lp or {}).get("entry_op"), kop, len(credits), ops, pid),
              bool(first) and first[0][0] == 0 and first[0][2] is True and lp is not None and lp.get("pa_pos_id") == pid and lp.get("entry_op") == kop and len(kd) == 1
              and len(credits) >= 1 and all(str(o).startswith("grad:leg:") and str(o).endswith(":" + pid) for o in ops) and c[-1] == len(credits))
        print("INFO %s BASELINE HAZARD MEASUREMENT (NOT asserted either way): debits for the killed mint %s, for the fresh mint %s, all grad debit pos_ids %s, account holds %s; candidate considerations %s; no cycle fault: %s" %
              (lab, deb_pos(a, KILLED_MINT), deb_pos(a, FRESH_MINT), deb_pos(a, "Mint"), [x["pos_id"] for x in a["acct"]], r["calls"]["cand"], [p["fault_reasons"] for p in passes]))

def shared_events(h):
    ev = []
    for ln in open(os.path.join(h, "paper_account", "events.jsonl")):
        try: e = json.loads(ln)
        except Exception: continue
        if e.get("event") in ("debit", "credit"): ev.append(e)   # same filter as the children's jstate()
    return ev
def final_after(h, ra, rb):
    ev = shared_events(h); cands = [x["after"] for x in (ra, rb)]
    best = max(cands, key=lambda a: len(a["j"]["events"]))
    check("T2 FINAL SNAPSHOT: the child snapshot used for the post-run assertions has as many journal events as the final shared journal file read by the parent (%d vs file %d; children's own counts A %d, B %d)" % (len(best["j"]["events"]), len(ev), len(ra["after"]["j"]["events"]), len(rb["after"]["j"]["events"])), len(best["j"]["events"]) == len(ev))
    return best
def t2(lab, cyc):
    h = B.newhome(); t, _ = kill_phase(h, lab, "1", cyc)
    pa = B.spawn(RESTART, h, "a" + lab, "1", json.dumps({"1": [9]}), cyc, "-1", "5", "3.0", "restartgo")
    pb = B.spawn(PROC_B, h, "b" + lab, "1", json.dumps({}), cyc, "-1", "5", "3.0", "procb")
    B.need(h, "a" + lab, "LOOP_START", 20.0); E.go(h)
    ra = B.finish(pa); rb = B.finish(pb)
    if not ra or not rb or "passes" not in ra or "passes" not in rb:
        check("%s both concurrent children produced results (rc A %s, rc B %s)" % (lab, pa.returncode, pb.returncode), False); return
    check("%s both children ended cleanly (A real grad-trader loop rc %s / %s, B realtime-sync-style poll_exits loop rc %s / %s)" % (lab, pa.returncode, ra.get("rc"), pb.returncode, rb.get("rc")), pa.returncode == 0 and pb.returncode == 0 and ra.get("rc") == "done" and rb.get("rc") == "done")
    pid = common_pre(lab, "1", t, ra, cyc); a = final_after(h, ra, rb)
    lb = rb.get("live_ident") or {}
    check("%s BARE IDENTITY (process B, realtime-sync style, never registers): package grad not loaded (%s, per-pass %s, at boot %s), sys.modules['grad'] is the patched module (%s), nothing registered (lanes %s)" % (lab, lb.get("pkg_grad_loaded"), sorted(set(p.get("pkg_grad") for p in rb["passes"])), rb.get("pkg_at_boot"), lb.get("sysmod_grad_is_GR"), lb.get("registered_lanes")),
          live_ok(lb, cyc, registered=False) and not any(p.get("pkg_grad") for p in rb["passes"]) and rb.get("pkg_at_boot") is False and lb.get("registered_lanes") == [])
    classes = [x[2] for x in ra["acts"]] + [x[2] for x in rb["acts"]]
    print("INFO %s MEASURED: A acts %s, B acts %s, A candidate considerations %s, A faults per pass %s, A credits per pass %s, B credits per pass %s, B n_closed per pass %s" %
          (lab, [x[2] for x in ra["acts"]], [x[2] for x in rb["acts"]], ra["calls"]["cand"], [p["fault_reasons"] for p in ra["passes"]], cum(ra["passes"], pid), cum(rb["passes"], pid), [p["extra"] for p in rb["passes"]]))
    check("%s CONCURRENT RECOVERY: exactly ONE adoption across both processes (classes %s == ['B']), record dir empty (%s)" % (lab, classes, a["rec_files"]), classes == ["B"] and not a["rec_files"])
    ev = a["j"]["events"]; opids = [e.get("op_id") for e in ev if e.get("op_id")]
    ok, exp = cash_ok(a)
    check("%s NO DUPLICATE ECONOMIC EFFECTS: killed mint debited once (%s), every WAL op_id unique (%d ids, %d distinct), journal status %s, cash identity holds (%s vs %s)" %
          (lab, deb_pos(a, KILLED_MINT), len(opids), len(set(opids)), a["j"]["status"], a["j"]["cash"], round(exp, 6)), len(deb_pos(a, KILLED_MINT)) == 1 and len(opids) == len(set(opids)) and ok)
    allc = max(cum(ra["passes"], pid)[-1], cum(rb["passes"], pid)[-1])
    check("%s MANAGEMENT (separate): the recovered position is credited exactly in the shared ledger (final credits for the pos_id: %d >= 1; leg identity not asserted)" % (lab, sum(1 for e in ev if e["event"] == "credit" and e.get("pos_id") == pid)), allc >= 1)
    if cyc == "1":
        check("%s FAULT DURABILITY / ENTRY PERMISSION (A journals, B does not): the cycle fault interrupted_prior_cycle was recorded by A's cycles (%s), A never reached the scan (calls %d), no fresh debit (%s), fault active %s, no clear log %s; B's direct poll_exits had no journal so its own passes cannot be what recorded it" %
              (any("interrupted_prior_cycle" in p["fault_reasons"] for p in ra["passes"]), len(ra["calls"]["scan"]), len(ra["calls"]["scan"]), deb_pos(a, "Mint"), ra["fault_active"], ra["clear_log_exists"]),
              any("interrupted_prior_cycle" in p["fault_reasons"] for p in ra["passes"]) and len(ra["calls"]["scan"]) == 0 and len(deb_pos(a, "Mint")) == 1 and bool(ra["fault_active"][0]) and ra["clear_log_exists"] is False)
    else:
        fresh_opened = [x for x in ra["calls"]["cand"] if x[1] == FRESH_MINT and x[2]]
        check("%s ENTRY PERMISSION (no latch), HYPOTHESIS: the fresh candidate was admitted exactly once (%s) and no cycle fault exists (%s) although B ran concurrently; debits %s" % (lab, fresh_opened, [p["fault_reasons"] for p in ra["passes"]], deb_pos(a, "Mint")),
              len(fresh_opened) == 1 and all(p["fault_reasons"] == [] for p in ra["passes"]) and len(deb_pos(a, "Mint")) == 2 and len(deb_pos(a, FRESH_MINT)) == 1)

def main():
    try:
        h0 = B.newhome(); open(os.path.join(h0, "ctl", "ctl0.SURVIVED"), "w").write("{}")
        check("TOKEN READER SENSITIVITY CONTROL: tokfile finds a present token (%s) and reports a missing one as None (%s), and B.tok with bound 0.0 on the PRESENT token returns %s (documenting the vacuous idiom)" % (tokfile(h0, "ctl0", "SURVIVED"), tokfile(h0, "ctl0", "NOPE"), B.tok(h0, "ctl0", "SURVIVED", 0.0)),
              tokfile(h0, "ctl0", "SURVIVED") == {} and tokfile(h0, "ctl0", "NOPE") is None and B.tok(h0, "ctl0", "SURVIVED", 0.0) is None)
        for lab, ei, cyc in (("S1", "1", "0"), ("S2", "1", "1"), ("S3", "0", "0"), ("S4", "0", "1")):
            try: B.scen(lab, lambda lab=lab, ei=ei, cyc=cyc: t1(lab, ei, cyc))
            except Exception as e:
                import traceback
                check("%s aborted with an uncaught exception: %s: %s" % (lab, type(e).__name__, str(e)[:160]), False); print("INFO traceback %s: %s" % (lab, traceback.format_exc().strip().splitlines()[-3:]))
        for lab, cyc in (("C1", "0"), ("C2", "1")):
            try: B.scen(lab, lambda lab=lab, cyc=cyc: t2(lab, cyc))
            except Exception as e:
                import traceback
                check("%s aborted with an uncaught exception: %s: %s" % (lab, type(e).__name__, str(e)[:160]), False); print("INFO traceback %s: %s" % (lab, traceback.format_exc().strip().splitlines()[-3:]))
    finally: B.reap_all()
    alive = [p.pid for p in B.KIDS if p.poll() is None]
    check("all children reaped (still alive: %s), total %d children spawned" % (alive, len(B.KIDS)), not alive)
    print("elapsed %.1fs of 90s budget; FAILS: %d" % (90.0 - B.left(), len(FAILS))); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
