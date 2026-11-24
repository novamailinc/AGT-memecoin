#!/usr/bin/env python3
"""Hermetic REAL-path integration test (2026-11-22).

Drives the ACTUAL production modules end to end, fixtures only, no
network, temp FOREX_AGENT_HOME, no live Telegram sends:

  scripts/smart-watcher handle_sighting() [real upstream detection:
  real wallet_group / eval_convergence / add_sighting / _attempt_fire /
  fire_card; only the two network calls - check_safety and enrich -
  are stubbed]
    -> real signal bus file (fire_card's own make_signal/publish)
    -> watcher_lane.process_new_fires() (the exact call the
       scripts/watcher-trader runner loop makes)
    -> shared paper account entry ($10 simulated account)
    -> watcher_lane.poll_exits() (the runner's exit pass)
    -> simulated daemon restart (module reload; ledgers survive)
    -> real telegram command handlers render exact state.

Also covered: duplicate intake (second process_new_fires opens
nothing), negative safety (check_safety False -> blocked, no bus
record, block reason journaled), proceeds-vs-PnL separation.

Acquisition extension: actual smart-watcher get_buy_details -> shared
reserves RPC -> primary/fallback -> transaction parser -> sighting;
only the HTTP attempt and pacer are fixtures. Total transport outage
must create no buy. Complete exit polling then joins the real verdict
funnel to durable outcome memory and reload retrieval.

Boundary: network transports, enrichment, safety and tape/price sources
are fixtures. This proves programmed integration, NOT real provider
availability, strategy qualification, memory-based decisions, or an
AGT process/reboot test. No live source polling or real credentials.

Run: python3 tests/test_realpath_e2e.py   (never pytest)
"""
import importlib
import importlib.machinery
import json
import os
import sys
import tempfile
import time
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "daemon", "launch_scanner"))
sys.path.insert(0, _REPO_ROOT)
import tests._guard_fixture as _gfb; _gfb.block_network()  # no network in tests (mandatory-guard migration, disclosed)

HOME = os.environ.get("AGT_REALPATH_TEST_HOME") or tempfile.mkdtemp(prefix="realpath-home-")
TMPD = os.path.join(HOME, "realpath-test")
os.makedirs(TMPD, exist_ok=True)
os.environ["FOREX_AGENT_HOME"] = HOME
os.environ["PAPER_ACCOUNT_ENABLED"] = "1"
os.environ["PAPER_ACCOUNT_START"] = "10"

from daemon.launch_scanner import signal_bus as BUS          # noqa: E402
from daemon.launch_scanner import watcher_lane as W          # noqa: E402
from daemon.launch_scanner import paper_account as PA        # noqa: E402
_gfb.set_limits_env(); _gfb.install_price_feed(at_cost=True)  # mandatory-guard migration (disclosed): gates on + TEST-FIXTURE limits, mechanics-only marks via real pipeline
from daemon.launch_scanner import smartwatch_filters as SWF  # noqa: E402
# The smart-watcher SCRIPT imports smartwatch_filters top-level (its
# sys.path points at daemon/launch_scanner), a distinct module object
# from daemon.launch_scanner.smartwatch_filters: patch BOTH instances'
# ledger path or the script's sightings land in the real home ledger.
import smartwatch_filters as SWF_TOP  # noqa: E402

W.LEDGER_PATH = os.path.join(TMPD, "watcher_ledger.json")
SWF._LEDGER_FILE = SWF.Path(TMPD) / "smart_convergence.json"
SWF_TOP._LEDGER_FILE = SWF.Path(TMPD) / "smart_convergence.json"

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


MINT = ("REALPATHmint%034d" % 1) + "pump"
MINT2 = "REALPATHmint%038d" % 2
PRICES = {}
# Verifiably CLEAN DexScreener-shaped tape for the entry-time toxicity
# re-check (2026-11-23): volume.m5 present (avg trade well above the $2
# dust threshold), h1 txns present, pairCreatedAt present and 1h old
# (past the 5-minute bot-swarm window).
GOOD_TAPE = {"fetched_at": __import__("time").time(), "received_at": __import__("time").time(),  # freshness stamps (TEST-FIXTURE)
   "usd_liquidity": 100_000.0, "usd_market_cap": 500_000.0,
             "chg_m5": 10.0, "buys_m5": 100, "sells_m5": 40,
             "vol_m5": 9000.0, "buys_h1": 400, "sells_h1": 150,
             "pair_created_ms": (time.time() - 3600) * 1000,
             "dex": "pump", "tape_source": "dexscreener"}


class _SafetyStub:
    def check_token(self, chain, mint, timeout=10):
        return {"passed": True, "score": 80, "flags": [], "fails": [],
                "unknown": False}


def _install_watcher_stubs(mod):
    mod._fresh_tape = lambda chain, mint: dict(GOOD_TAPE)
    mod.quote_price = lambda chain, mint, spot_index=None: PRICES.get(
        mint, 0.001)
    import tests._guard_fixture as _GFQ  # after sys.path setup (this line runs below it)
    mod.quote_price = _GFQ.stamped_quote(mod, mod.quote_price)  # fixture: quote stamp at call time
    mod._safety = _SafetyStub()


_install_watcher_stubs(W)
W.ensure_ledger()

# --- load the real smart-watcher script ------------------------------
sw = importlib.machinery.SourceFileLoader(
    "smart_watcher_script",
    os.path.join(_REPO_ROOT, "scripts", "smart-watcher")).load_module()
sw._HAS_CARDS = False  # suppress card build (lp_lock/saturation calls)

now_ms = time.time() * 1000
INFO = {"symbol": "RPATH", "mcap": 500_000.0, "liq": 100_000.0,
        "vol": 50_000.0, "price": 0.001,
        "txns": {"m5": {"buys": 40, "sells": 15}},
        "volume": {"m5": 25_000.0},
        "pairCreatedAt": now_ms - 20 * 60 * 1000}

REGISTRY = {"WalA": {"label": "pack-one-1"},
            "WalB": {"label": "pack-two-1"},
            "WalC": {"label": "pack-three-1"}}


def _enrich(mint):
    d = dict(INFO)
    return d


sw.check_safety = lambda mint: (True, "ok", {"passed": True,
                                             "score": 80.0})
sw.enrich = _enrich
seen = set()

# --- 1. real transaction acquisition -> wallet-buy parser -> sightings ---
from daemon.launch_scanner import reserves as R

class Pacer:
    def acquire(self, *a, **kw): return True
    def report(self, *a, **kw): pass

calls = []
fail_primary = [False]
unknown_primary = [False]
fail_detail = [False]
sell_mode = [False]
response_mode = ['normal']
def transport(url, body, method, host, timeout):
    req = json.loads(body)
    calls.append((host, req["method"]))
    if fail_primary[0] and "alchemy" in host:
        return None, "fail", 0.001
    if req['method']=='getSignaturesForAddress':
        wallet=req['params'][0]
        newest=('sell:' if sell_mode[0] else 'sig:')+wallet
        previous=('sig:' if sell_mode[0] else 'old:')+wallet
        return [{'signature':newest},{'signature':previous}], 'ok', 0.001
    if fail_detail[0]:
        return None,'fail',0.001
    wallet = req["params"][0].split(":", 1)[1]
    tx = {"blockTime": int(time.time()),
          "transaction": {"signatures": [req["params"][0]],
                          "message": {"accountKeys": [wallet, "fixture-token-account"]}},
          "meta": {"err": None, "preBalances": [2000000000], "postBalances": [600000000],
                   "preTokenBalances": [], "postTokenBalances": [
                       {"accountIndex": 1, "owner": wallet, "mint": MINT,
                        "uiTokenAmount": {"uiAmount": 1000}}]}}
    if req['params'][0].startswith('sell:'):
        tx['meta']['preTokenBalances']=tx['meta']['postTokenBalances']
        tx['meta']['postTokenBalances']=[]
    return tx, "ok", 0.001

# Central routine router with real adapters/decoder/intake; only transport and
# quota tokens are fixtures. Unknown-age history never claims Chainstack recency.
from daemon.intelligence.acquisition import routine
from daemon.intelligence.acquisition.provider_router import ProviderRouter
from daemon.intelligence.acquisition.request_scheduler import SharedQuota
os.environ['ALCHEMY_API_KEY']='fixture-not-real'
class RpcResponse:
    def __init__(self,value,status=200):
        self.value=value;self.status=status;self.headers={}
    def read(self):
        if self.status!=200:return b''
        return json.dumps({'jsonrpc':'2.0','id':1,'result':self.value}).encode()
    def __enter__(self):return self
    def __exit__(self,*a):pass

def central_transport(req,timeout=12):
    import urllib.error
    from urllib.parse import urlsplit
    method=json.loads(req.data)['method']
    if response_mode[0]=='null':return RpcResponse(None)
    if response_mode[0]=='outage':raise urllib.error.URLError('fixture total outage')
    value,kind,elapsed=transport(req.full_url,req.data,method,urlsplit(req.full_url).hostname,timeout)
    if kind!='ok':
        if unknown_primary[0]:
            # UNKNOWN post-I/O outcome (transport failure): under the
            # 2026-11-24 v3 contract the router must NOT retry this
            # uncertain request on any other provider.
            raise urllib.error.URLError('fixture provider unavailable')
        # ANSWERED refusal (HTTP 429): the provider definitively
        # processed and rejected the request; failover is allowed.
        return RpcResponse(None,status=429)
    if response_mode[0]=='failed':value['meta']['err']={'InstructionError':[0,'fixture_failure']}
    return RpcResponse(value)
central_router=ProviderRouter(home=os.environ['FOREX_AGENT_HOME'],helius_enabled=False,transport=central_transport)
with patch.object(routine,'router',return_value=central_router), \
        patch.object(SharedQuota,'acquire',return_value=(True,0.,'fixture token')):
    from daemon.launch_scanner.wallet_intake import WalletIntake
    intake=WalletIntake(os.environ['FOREX_AGENT_HOME'])
    for wallet in ("WalA", "WalB", "WalC"):
        intake.stage(wallet,None,'old:'+wallet,[])
        fail_primary[0] = wallet == "WalB"
        if wallet=='WalA':
            fail_detail[0]=True
            sw.poll_wallet(wallet,'unused-fixture',REGISTRY,seen,intake)
            check('real poll persists unavailable detail before cursor advance',
                  intake.wallet(wallet)['pending']==['sig:'+wallet]
                  and intake.wallet(wallet)['cursor']=='sig:'+wallet)
            fail_detail[0]=False
            intake=WalletIntake(os.environ['FOREX_AGENT_HOME'])
        sw.poll_wallet(wallet,'unused-fixture',REGISTRY,seen,intake)
        check('actual poll acknowledges parsed and persisted sighting for '+wallet,
              intake.wallet(wallet)['pending']==[])
    check("primary answered failure traversed actual public fallback without Helius",
          any("mainnet-beta.solana" in host for host,_ in calls) and not any("helius" in host for host,_ in calls))
    # Unknown post-I/O primary failure: no fallback traversal (the
    # uncertain request is never retried anywhere), no fabrication -
    # the poll degrades and keeps the cursor instead of inventing data.
    unknown_primary[0]=True
    fail_primary[0]=True
    intake.stage("WalU",None,'old:WalU',[])
    calls_before=len(calls)
    sw.poll_wallet("WalU",'unused-fixture',REGISTRY,seen,intake)
    traversed=[host for host,_ in calls[calls_before:] if "mainnet-beta.solana" in host]
    check("unknown primary failure: no fallback traversal, no fabrication",
          traversed==[] and intake.wallet("WalU")['cursor']=='old:WalU'
          and intake.wallet("WalU")['pending']==[])
    unknown_primary[0]=False
    check("actual poll used signature discovery and transaction detail methods",
          {method for _,method in calls}=={'getSignaturesForAddress','getTransaction'})
    fail_primary[0] = False
    sell_mode[0]=True
    import smartgrade as GRADE
    with patch.object(GRADE,'save_grades',return_value=False):
        try:
            sw.poll_wallet('WalA','unused-fixture',REGISTRY,seen,intake)
            check('actual poll refuses acknowledgement after sell history write failure',False)
        except OSError:
            check('actual poll refuses acknowledgement after sell history write failure',
                  intake.wallet('WalA')['pending']==['sell:WalA'])
    sw.poll_wallet('WalA','unused-fixture',REGISTRY,seen,intake)
    sw.poll_wallet('WalA','unused-fixture',REGISTRY,seen,intake)
    sells=GRADE.load_grades()['WalA']['sell_events']
    check('actual closed-account sell retry records one causal event',
          len(sells)==1 and sells[0]['signature']=='sell:WalA'
          and sells[0]['sold_fraction']==1
          and sells[0]['holding_period_status']=='unavailable_no_causal_buy_match'
          and intake.wallet('WalA')['pending']==[])
    sell_mode[0]=False
    response_mode[0]='null'
    with patch.object(R, '_rpc_attempt'):
        check("definitive primary null yields no fabricated wallet buy",
              sw.get_buy_details("WalN", "sig:WalN", "unused") == [])
    # An RPC-success response can still describe a failed chain
    # transaction. The parser, not the fake transport, must reject it.
    def failed_chain(*args):
        tx, kind, elapsed = transport(*args)
        tx["meta"]["err"] = {"InstructionError": [0, "fixture_failure"]}
        return tx, kind, elapsed
    response_mode[0]='failed'
    with patch.object(R, '_rpc_attempt'):
        check("failed chain transaction produces no sighting input",
              sw.get_buy_details("WalF", "sig:WalF", "unused") == [])
    before_outage = len(seen)
    response_mode[0]='outage'
    with patch.object(R, '_rpc_attempt'):
        check("total provider failure yields no fabricated wallet buy",
              sw.get_buy_details("WalD", "sig:WalD", "unused") == [])
    check("provider outage created no resolved token", len(seen) == before_outage)

bus_path = BUS.BUS_PATH
check("bus file exists after real detection", os.path.exists(bus_path))
lines = [l for l in open(bus_path).read().splitlines() if l.strip()]
check("exactly one bus record from converged detection",
      len(lines) == 1)
sig = json.loads(lines[-1])
check("bus record is a smart-watcher converged fire",
      sig.get("source") == "smart-watcher"
      and sig.get("tier") == "converged" and sig.get("mint") == MINT)
ev = sig.get("evidence") or {}
# The real gate fires at 2 independent groups (CONVERGENCE_WALLETS):
# the third sighting lands after resolution and is not recorded.
check("measured features only (buys/age/safety/groups)",
      sig.get("buys") == 40
      and abs(float(sig.get("age_min") or 0) - 20.0) < 0.5
      and ev.get("safety_score") == 80.0
      and (ev.get("components") or {}).get("convergence_groups") == 2)
check("detection resolved the mint (seen)", MINT in seen)

# --- 1b. second actual wallet source observes the SAME lifecycle mint ---
# This joins its durable grade/state handoff to the lifecycle evidence below.
# It does NOT pretend scanner fasttrack routes to watcher-lane trade entry.
from daemon.launch_scanner import smartmoney as SM, smartgrade as SG, LaunchScanner
from storage import Store
from daemon.common import get_daemon_state
from types import SimpleNamespace
scanner_state={'seen':{},'degen_positions':{}}
scanner=LaunchScanner.__new__(LaunchScanner)
scanner_store=Store(os.path.join(TMPD,'scanner-source.db'))
scanner_cfg=SimpleNamespace(degen=SimpleNamespace(smart_fasttrack_enabled=False))
scanner_detail_failed=[True]
def scanner_rpc(method,params):
    if method=='getSignaturesForAddress':return [{'signature':'scanner:WalA'}]
    if scanner_detail_failed[0]:return None
    return {'blockTime':int(time.time()),'transaction':{'signatures':['scanner:WalA'],
        'message':{'accountKeys':[{'pubkey':'WalA'},{'pubkey':'scanner-token-account'}]}},
        'meta':{'err':None,'preBalances':[2000000000],'postBalances':[600000000],
        'preTokenBalances':[],'postTokenBalances':[{'accountIndex':1,'owner':'WalA',
        'mint':MINT,'uiTokenAmount':{'uiAmount':1000}}]}}
try:
    with patch.object(SM,'api_key',return_value='fixture'),patch.object(SM,'load_registry',return_value={'WalA':{}}),patch.object(SM,'_rpc',side_effect=scanner_rpc),patch.object(SM.time,'sleep'):
        check('second source outage returns no fabricated same-mint activity',SM.poll_wallets(scanner_state,poll_seconds=0)=={})
        check('second source outage keeps actual signature pending',SM._intake().wallet('WalA')['pending']==['scanner:WalA'])
        scanner_detail_failed[0]=False
        fresh=SM.poll_wallets(scanner_state,poll_seconds=0)
        check('second source validated evidence resolves same lifecycle mint',fresh=={MINT:['WalA']})
        with patch.object(SG,'save_grades',return_value=False):
            try:scanner._handoff_smart_wallet_activity(scanner_cfg,scanner_store,scanner_state,fresh)
            except OSError:pass
            else:check('second source grade failure propagates',False)
        check('second source grade failure retains actual signature',SM._intake().wallet('WalA')['pending']==['scanner:WalA'])
        fresh=SM.poll_wallets(scanner_state,poll_seconds=0)
        scanner._handoff_smart_wallet_activity(scanner_cfg,scanner_store,scanner_state,fresh)
        scanner_state=get_daemon_state(scanner_store,'launch_scanner')
        check('second source successful grade handoff durably accepts signature',SM._intake().wallet('WalA')['pending']==[])
        check('second source next cycle does not replay graded lifecycle mint',SM.poll_wallets(scanner_state,poll_seconds=0)=={})
        check('same-mint grade observation survives durable scanner reload',scanner_state['smart_activity']=={MINT:['WalA']} and any(b['mint']==MINT for b in SG.load_grades()['WalA']['buys']))
finally:scanner_store.close()

# --- 2. duplicate detection: same mint re-sighted, no 2nd record -----
for w in ("WalA", "WalB", "WalC"):
    sw.handle_sighting(w, {"mint": MINT, "block_ts": time.time(),
                           "tokens_bought": 1000.0, "sol_spent": 1.4},
                       REGISTRY, seen)
lines = [l for l in open(bus_path).read().splitlines() if l.strip()]
check("re-sighting a resolved mint writes no duplicate bus record",
      len(lines) == 1)

# --- 3. negative safety: blocked, no bus record, reason journaled ----
sw.check_safety = lambda mint: (False, "honeypot risk", {"passed": False})
for w in ("WalA", "WalB", "WalC"):
    sw.handle_sighting(w, {"mint": MINT2, "block_ts": time.time(),
                           "tokens_bought": 1000.0, "sol_spent": 1.4},
                       REGISTRY, seen)
lines = [l for l in open(bus_path).read().splitlines() if l.strip()]
check("negative safety writes no bus record", len(lines) == 1)
ledger = json.loads(open(SWF._LEDGER_FILE).read())
rec2 = ledger.get(MINT2) or {}
check("negative safety block reason journaled",
      "safety" in str(rec2.get("last_block_reason") or ""))
check("unsafe mint NOT resolved for retry eligibility",
      MINT2 not in seen)
sw.check_safety = lambda mint: (True, "ok", {"passed": True,
                                             "score": 80.0})

# --- 4. watcher-trader intake (the runner loop's own call) -----------
res = W.process_new_fires()
check("intake opened exactly one position", res.get("opened") == 1
      and res.get("fires") == 1)
pos = W.open_positions().get(MINT)
snap = PA.snapshot()
check("entry through shared $10 account ($1 watcher size)",
      pos is not None and abs(snap["cash"] - 9.0) < 1e-9)
check("pinned shared position id",
      (pos or {}).get("pa_pos_id", "").startswith("watcher/%s#" % MINT))

res2 = W.process_new_fires()
check("duplicate intake opens nothing",
      res2.get("opened") == 0 and res2.get("fires") == 0)

# --- 5. exit pass at fixture 2.1x: leg A (50% at 2x) settles ---------
PRICES[MINT] = pos["entry_price"] * 2.1
W.poll_exits(price_fn=lambda c, m, s: PRICES.get(m, 0.0))
pos = W.open_positions()[MINT]
leg_a = pos["legs"]["a"]
snap = PA.snapshot()
check("leg A settled at a current-price simulated fill",
      leg_a.get("done") and leg_a.get("exit_price_kind")
      == "current_price_simulated_fill")
proceeds = leg_a.get("realized_usd") or 0.0
check("proceeds credited to shared cash",
      abs(snap["cash"] - (9.0 + proceeds)) < 1e-6)
check("proceeds are not P/L (realized_pnl = proceeds - settled cost)",
      abs(snap["realized_pnl"] - (proceeds - 0.5)) < 1e-6)
qty_open = snap["lanes"]["watcher"]["positions"][0]["qty"]

# Optional checkpoint for the separate external-process acceptance test.
# The child stops after a real entry and partial settlement; a fresh
# interpreter must recover this exact account/position, not a reset.
if os.environ.get("AGT_REALPATH_CHECKPOINT") == "1":
    with open(os.path.join(HOME, "fixture-checkpoint.json"), "w") as f:
        json.dump({"mint": MINT, "ledger": W.LEDGER_PATH,
                   "cash": snap["cash"], "pos_id": pos["pa_pos_id"],
                   "entry_price": pos["entry_price"],
                   "bus_path": bus_path}, f)
    if FAILS:
        raise SystemExit(1)
    print("REALPATH CHECKPOINT WRITTEN")
    raise SystemExit(0)

# --- 6. simulated daemon restart: ledgers survive --------------------
_lp = W.LEDGER_PATH
W2 = importlib.reload(W)
PA2 = importlib.reload(PA)
W2.LEDGER_PATH = _lp
_install_watcher_stubs(W2)
check("after restart position survives with pinned id",
      MINT in W2.open_positions()
      and W2.open_positions()[MINT]["pa_pos_id"]
      == pos["pa_pos_id"])
check("after restart shared cash unchanged",
      abs(PA2.snapshot()["cash"] - snap["cash"]) < 1e-9)

# --- 7. real telegram handlers render exact state --------------------
os.environ.pop("TELEGRAM_ALLOWED_CHAT_ID", None)
os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
from daemon.telegram import commands as TG                   # noqa: E402

SENT = []


class _Tools:
    def forex_get_health(self):
        return {"mode": "paper", "kill_switch": "clear",
                "checks": {"db": True}}

    def forex_get_positions(self):
        return {"positions": []}

    def forex_get_account(self):
        return {"account": {"balance": 0.0, "equity": 0.0,
                            "margin": 0.0}}


class CaptureCtx:
    security = None
    tools = _Tools()
    audit_log = []

    def send(self, chat_id, text, markup):
        SENT.append(text)

    def publish(self, event, sev=None):
        pass


ctx = CaptureCtx()
TG._cmd_positions(ctx, 1, 1, 1, ["demo"])
TG._cmd_account(ctx, 1, 1, 1, ["demo"])
demo_pos, demo_acct = SENT[0], SENT[1]
exp_cash = "$%.2f" % snap["cash"]
check("/account demo renders exact cash", exp_cash in demo_acct)
check("/account demo labels SIMULATED", "SIMULATED" in demo_acct)
check("/positions demo renders pinned id", pos["pa_pos_id"] in demo_pos)
check("/positions demo renders exact remaining qty",
      str(qty_open) in demo_pos)
# --- 8. completed close -> real verdict funnel -> durable memory -> restart ---
from daemon.launch_scanner import dossiers as D
PRICES[MINT] = pos["entry_price"] * 0.1
W2.poll_exits(price_fn=lambda c, m, spot: PRICES.get(m, 0))
check("remaining position closes through actual exit polling",
      MINT not in W2.open_positions())
outcomes = D.outcome_records(mint=MINT)
check("actual watcher close reaches durable outcome archive",
      len(outcomes) == 1 and outcomes[0]["lane"] == "watcher"
      and outcomes[0]["qualified"] is False
      and outcomes[0]["provenance"] == "simulated")
D2 = importlib.reload(D)
mem = D2.launch_memory(MINT)
check("memory retrieval joins resolved outcome after module restart",
      mem is not None and len(mem.get("outcomes") or []) == 1
      and "watcher" in (mem.get("resolved_lanes") or []))
cash_before_reload = PA2.snapshot()["cash"]
check("post-close restart retains shared account cash",
      abs(importlib.reload(PA).snapshot()["cash"] - cash_before_reload) < 1e-9)


print()
if FAILS:
    print("FAILED: %d checks: %s" % (len(FAILS), FAILS))
    sys.exit(1)
print("ALL REALPATH-E2E TESTS PASSED")
