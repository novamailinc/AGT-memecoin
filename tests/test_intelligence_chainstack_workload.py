#!/usr/bin/env python3
"""Phase 6 tests — Chainstack historical + recovery workload
(chainstack_workload.py) + event_backfill.py Phase 6 extensions.

Plain script, no network: HTTP is driven through an injected fake
opener keyed by (host, method); recorded fixtures under
tests/data/intelligence/acquisition/ are replayed as fake responses
(real on-chain data captured 2026-11-22 via reserves.rpc()).

Covers (spec §5): historical transaction retrieval (fixture-based);
recovery of a deliberately missed event range (known missing range
the backfill closes); duplicate elimination (same event from two
providers -> one record); independent transaction verification on a
sampled set (match / conflict / unverified); measured backfill
performance (mocked transport timing, labelled as such); historical
dataset completeness stated only where the provider makes that data
available; Yellowstone gRPC investigation (paid on all three — no
client built). Live Alchemy/Chainstack verification is DEFERRED to
AGT; nothing here claims a live result.
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import io
import json
import os
import sys
import tempfile
import time
import urllib.error

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import chainstack_workload as CW
from daemon.intelligence.acquisition import event_backfill as EB
from daemon.intelligence.acquisition import provider_router as PR

fails = []
total = [0]

HOME = tempfile.mkdtemp(prefix="acq_p6_")
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"

FIXTURES = os.path.join(REPO, "tests/data/intelligence/acquisition")


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


TX_FIXTURE = load_fixture("live_getTransaction_pump.json")
SIGS_FIXTURE = load_fixture("live_getSignaturesForAddress_pump.json")
SLOT_FIXTURE = load_fixture("live_getSlot.json")
ACCT_FIXTURE = load_fixture("live_getAccountInfo_wsol.json")
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TX_SIG = "eY5SFmn2qGBCbwqWUQEKJJr6N7VevCWvKTKqkCaC6RBbuSqYTQtfK9djE8EWSTHYFbnyMLUoYiC3ZAWVDiCt7q8"
WSOL = "So11111111111111111111111111111111111111112"

# ---------------- fake transport ----------------------------------------
class FakeResp:
    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok(value):
    return (200, {"jsonrpc": "2.0", "id": 1, "result": value})


def rpc_err(code, message):
    return (200, {"jsonrpc": "2.0", "id": 1,
                  "error": {"code": code, "message": message}})


class MethodTransport:
    """Fake opener dispatching on (host, method). ``handlers`` maps
    (host_key, method) or (None, method) to a list of items consumed
    in order (sticky at the last). Records every (host, method,
    params) call. Optional per-call latency simulates transport
    time for the mocked-timing measurement."""

    def __init__(self, handlers, latency_s=0.0):
        self.handlers = handlers
        self.calls = []
        self.latency_s = latency_s
        self._idx = {}

    def __call__(self, req, timeout=None):
        if self.latency_s:
            time.sleep(self.latency_s)
        url = req.full_url
        body = json.loads(req.data.decode())
        method = body.get("method")
        host = "other"
        for key in ("g.alchemy.com", "chainstack.example",
                    "api.mainnet-beta"):
            if key in url:
                host = key
                break
        self.calls.append((host, method, body.get("params")))
        items = self.handlers.get((host, method)) \
            or self.handlers.get((None, method))
        if items is None:
            # Fail closed in tests: an unmocked call is an error,
            # never a silent empty success — the router's failover
            # must not "succeed" on a None result.
            return FakeResp({"jsonrpc": "2.0", "id": 1,
                             "error": {"code": -32601,
                                       "message": "method not mocked "
                                       "for this host"}})
        k = (host, method)
        i = self._idx.get(k, 0)
        self._idx[k] = min(i + 1, len(items) - 1)
        item = items[min(i, len(items) - 1)]
        if item[0] == "raise":
            raise item[1]
        status, payload = item
        if status >= 400:
            raise urllib.error.HTTPError(
                url, status, "err", {},
                io.BytesIO(json.dumps(payload).encode()))
        return FakeResp(payload, status)

    def hosts_for(self, method):
        return [h for h, m, _ in self.calls if m == method]


def make_router(transport):
    home = tempfile.mkdtemp(prefix="acq_p6_router_")
    return PR.ProviderRouter(home=home, helius_enabled=False,
                             transport=transport)


def sig_row(sig, slot, idx=0):
    return {"signature": sig, "slot": slot, "blockTime": 1795341600 + slot,
            "confirmationStatus": "finalized", "err": None,
            "transactionIndex": idx}


def fake_tx(sig, slot, fee=5000):
    return {"slot": slot, "blockTime": 1795341600 + slot,
            "meta": {"fee": fee, "err": None, "preBalances": [100, 0],
                     "postBalances": [90, 10], "innerInstructions": []},
            "transaction": {"signatures": [sig], "message": {
                "accountKeys": [{"pubkey": "AAA" + sig[:4]},
                                {"pubkey": "BBB" + sig[:4]}],
                "instructions": [{"programId":
                                  "11111111111111111111111111111111"}]}}}


# ================= Yellowstone gRPC: investigate only ====================
status = CW.yellowstone_grpc_status()
check("yellowstone: not supported by the plan",
      status["supported_by_plan"] is False)
check("yellowstone: no client built, nothing enabled",
      status["client_built"] is False and status["enabled"] is False)
check("yellowstone: paid on all three providers",
      status["per_provider"] == {"helius_main": "paid",
                                 "alchemy": "paid",
                                 "chainstack": "paid"})
check("yellowstone: finding records the paid verdict + no build",
      "PAID" in status["finding"] and "No gRPC client is built"
      in status["finding"])

# ================= completeness (only what providers offer) =============
comp = CW.historical_completeness()
check("completeness: Chainstack has NO deep history on Free",
      comp["chainstack"]["deep_history"] is False)
check("completeness: Alchemy carries deep history (archive)",
      comp["alchemy"]["deep_history"] is True)
check("completeness: signature history is Alchemy's leg",
      comp["signature_history_provider"] == "alchemy")
check("completeness: Chainstack cannot serve sigs/program accounts",
      "getSignaturesForAddress" in comp["chainstack"]["methods_unavailable"]
      and "getProgramAccounts" in
      comp["chainstack"]["methods_unavailable"])
check("completeness: retention window unmeasured, never assumed",
      comp["chainstack"]["retention_window"]["measured"] is False
      and comp["chainstack"]["retention_window"]["window_slots"] is None)
check("completeness: no historical account-state read on any free tier",
      "paid-only" in comp["account_state_history"])

# ================= historical retrieval (fixture-based) =================
# getTransaction for the recorded pump tx, served by Alchemy (no
# recent assertion -> archive leg).
t1 = MethodTransport({
    (None, "getTransaction"): [ok(TX_FIXTURE["result"])],
})
r1 = make_router(t1)
res = CW.fetch_transaction(r1, TX_SIG)
check("fetch_transaction (fixture): ok",
      res.get("kind") == "ok")
check("fetch_transaction (fixture): blockTime matches the recording",
      (res.get("result") or {}).get("blockTime")
      == TX_FIXTURE["result"]["blockTime"] == 1795341615)
check("fetch_transaction (fixture): fee matches the recording",
      ((res.get("result") or {}).get("meta") or {}).get("fee") == 5010)
check("fetch_transaction without recent: Alchemy serves (archive leg)",
      res.get("provider") == "alchemy"
      and t1.hosts_for("getTransaction") == ["g.alchemy.com"])

# Same read with recent=True: Chainstack is attempted FIRST.
t2 = MethodTransport({
    ("chainstack.example", "getTransaction"): [ok(TX_FIXTURE["result"])],
    ("g.alchemy.com", "getTransaction"): [ok(TX_FIXTURE["result"])],
})
r2 = make_router(t2)
res2 = CW.fetch_transaction(r2, TX_SIG, recent=True)
check("fetch_transaction recent: Chainstack attempted first",
      t2.calls[0][0] == "chainstack.example"
      and res2.get("provider") == "chainstack")

# getBlock (synthetic block with a signature list).
block = {"blockHeight": 100, "blockTime": 1795341700,
         "blockhash": "blk1", "parentSlot": 472499390,
         "previousBlockhash": "blk0",
         "signatures": ["sigA", "sigB"]}
t3 = MethodTransport({(None, "getBlock"): [ok(block)]})
r3 = make_router(t3)
res3 = CW.fetch_block(r3, 472499391, recent=True)
check("fetch_block: ok, slot's block returned",
      res3.get("kind") == "ok"
      and (res3.get("result") or {}).get("blockhash") == "blk1")
check("fetch_block recent: Chainstack first",
      t3.calls[0][0] == "chainstack.example")

# getSignaturesForAddress: Chainstack is NEVER sent it, even with
# recent=True — the capability gate drops it and Alchemy serves.
t4 = MethodTransport({
    ("g.alchemy.com", "getSignaturesForAddress"):
        [ok(SIGS_FIXTURE["result"])],
    ("chainstack.example", "getSignaturesForAddress"):
        [ok(SIGS_FIXTURE["result"])],
})
r4 = make_router(t4)
res4 = CW.fetch_signatures(r4, PUMP, limit=2)
check("fetch_signatures (fixture): 2 rows returned",
      res4.get("kind") == "ok" and len(res4.get("result") or []) == 2)
check("fetch_signatures: first row is the recorded tx signature",
      (res4.get("result") or [{}])[0].get("signature") == TX_SIG)
check("fetch_signatures: Chainstack never called for it",
      "chainstack.example" not in t4.hosts_for("getSignaturesForAddress"))
check("fetch_signatures: Alchemy served (archive leg)",
      res4.get("provider") == "alchemy")

# ================= market reconstruction + replay dataset ===============
rows = [sig_row("sigR2", 200, 1), sig_row("sigR1", 199, 0)]
t5 = MethodTransport({
    (None, "getSignaturesForAddress"): [ok(rows)],
    (None, "getTransaction"): [ok(fake_tx("sigR1", 199)),
                               ok(fake_tx("sigR2", 200))],
})
r5 = make_router(t5)
recon = CW.reconstruct_market_activity(r5, PUMP, limit=2)
check("reconstruct: 2 events from 2 signatures",
      len(recon["events"]) == 2 and recon["stats"]["events"] == 2)
check("reconstruct: events reconciled oldest-first by slot",
      [e["slot"] for e in recon["events"]] == [199, 200])
check("reconstruct: no failures recorded",
      recon["stats"]["failed"] == [])
check("reconstruct: completeness statement attached",
      recon["completeness"]["chainstack"]["deep_history"] is False)

# One tx fetch fails: the signature is listed, no event is invented.
# (Rows arrive newest-first and are fetched in that order, so the
# FIRST fetch — sigR2 — is the failing one.)
t6 = MethodTransport({
    (None, "getSignaturesForAddress"): [ok(rows)],
    (None, "getTransaction"): [rpc_err(-32002, "archive not available"),
                               ok(fake_tx("sigR1", 199))],
    # The public fallback must fail too — otherwise it would
    # "succeed" the failed fetch with a replayed body.
    ("api.mainnet-beta", "getTransaction"):
        [rpc_err(-32601, "method not mocked for this host")],
})
r6 = make_router(t6)
recon6 = CW.reconstruct_market_activity(r6, PUMP, limit=2)
check("reconstruct with a failed tx: 1 event, 1 failure listed",
      len(recon6["events"]) == 1
      and len(recon6["stats"]["failed"]) == 1
      and recon6["stats"]["failed"][0]["signature"] == "sigR2")

ds_path = os.path.join(HOME, "replay-dataset.json")
out = CW.build_replay_dataset(recon["events"], ds_path,
                              metadata={"source": "test"})
saved = json.load(open(ds_path))
check("replay dataset: written with events + completeness metadata",
      out["events"] == 2 and len(saved["events"]) == 2
      and saved["metadata"]["completeness"]["chainstack"]
      ["deep_history"] is False)

# ================= verification: classification + sampling ==============
cases = [
    ({"kind": "token_creation", "signature": "a"}, "token_creation", 70),
    ({"kind": "pool_creation", "signature": "b"}, "pool_creation", 80),
    ({"kind": "liquidity_change", "signature": "c"}, "liquidity_change", 75),
    ({"kind": "token_migration", "signature": "d"}, "token_migration", 90),
    ({"kind": "bonding_curve_graduation", "signature": "e"},
     "bonding_curve_graduation", 85),
    ({"kind": "buy", "amount_sol": 5.0, "signature": "f"},
     "important_trade", 60),
    ({"kind": "sell", "amount_sol": 0.1, "signature": "g"},
     "routine", 10),
    ({"kind": "buy", "signature": "h", "decision_priority": True},
     "high_priority_decision", 55),
    ({"kind": "sell", "signature": "i", "conflicting": True},
     "conflicting_decoder", 100),
    ({"kind": "routine", "signature": "j"}, "routine", 10),
]
for event, want_kind, want_prio in cases:
    got = CW.classify_verification_event(event)
    check("classify %s -> %s/%d" % (event.get("kind"), want_kind, want_prio),
          got["kind"] == want_kind and got["priority"] == want_prio)

batch = [{"kind": "routine", "signature": "r%d" % i} for i in range(5)]
batch += [{"kind": "token_creation", "signature": "tc%d" % i}
          for i in range(3)]
batch += [{"kind": "token_migration", "signature": "mig"},
          {"kind": "buy", "amount_sol": 2.0, "signature": "bigbuy"},
          {"kind": "sell", "signature": "x", "conflicting": True}]
sel = CW.select_for_verification(batch, budget=3)
check("select: budget respected (3 of 6 eligible)",
      len(sel["selected"]) == 3)
check("select: routine never selected",
      all(cls["kind"] != "routine" for _, cls in sel["selected"]))
check("select: highest priority first (conflicting decoder leads)",
      sel["selected"][0][1]["kind"] == "conflicting_decoder")
check("select: skipped count is the routine count",
      sel["skipped_below_threshold"] == 5
      and sel["considered"] == len(batch))
sel_all = CW.select_for_verification(batch, budget=50)
check("select: large budget still excludes routine",
      len(sel_all["selected"]) == 6
      and all(cls["priority"] >= CW.MIN_VERIFY_PRIORITY
              for _, cls in sel_all["selected"]))

# ================= verification: digest + verify ========================
digest = CW.canonical_tx_digest(TX_FIXTURE["result"])
check("digest (fixture): fee + blockTime captured",
      digest["fee"] == 5010 and digest["blockTime"] == 1795341615)
check("digest (fixture): accounts + programs non-empty",
      len(digest["accounts"]) > 0 and len(digest["programs"]) > 0)
check("digest: identical digests have no differences",
      CW._digest_differences(digest, dict(digest)) == [])
tampered = dict(digest)
tampered["fee"] = 9999
check("digest: tampered fee is one field difference",
      CW._digest_differences(digest, tampered) == ["fee"])

# Match: Chainstack returns the same tx -> match, provider chainstack.
t7 = MethodTransport({
    ("chainstack.example", "getTransaction"): [ok(TX_FIXTURE["result"])],
})
r7 = make_router(t7)
v = CW.verify_transaction(r7, TX_SIG, TX_FIXTURE["result"])
check("verify_transaction match: status match, verified",
      v["status"] == "match" and v["verified"] is True
      and v["match"] is True and v["differences"] == [])
check("verify_transaction match: Chainstack was the verifier",
      v["provider"] == "chainstack"
      and t7.hosts_for("getTransaction") == ["chainstack.example"])

# Conflict: Chainstack returns the same tx with a different fee.
conflict_tx = json.loads(json.dumps(TX_FIXTURE["result"]))
conflict_tx["meta"]["fee"] = 6000
t8 = MethodTransport({
    ("chainstack.example", "getTransaction"): [ok(conflict_tx)],
})
r8 = make_router(t8)
v8 = CW.verify_transaction(r8, TX_SIG, TX_FIXTURE["result"])
check("verify_transaction conflict: fee difference flagged",
      v8["status"] == "conflict" and v8["verified"] is True
      and v8["differences"] == ["fee", "meta"])

# Unverified: the re-read errors -> unverified, NOT a conflict.
t9 = MethodTransport({
    ("chainstack.example", "getTransaction"):
        [rpc_err(-32005, "rate limited")],
    ("g.alchemy.com", "getTransaction"):
        [rpc_err(-32005, "rate limited")],
    ("api.mainnet-beta", "getTransaction"):
        [rpc_err(-32005, "rate limited")],
})
r9 = make_router(t9)
v9 = CW.verify_transaction(r9, TX_SIG, TX_FIXTURE["result"])
check("verify_transaction error: unverified, not a conflict",
      v9["status"] == "unverified" and v9["verified"] is False
      and v9["match"] is None and v9["error"] is not None)

# Account-state verification: match + conflict.
acct = ACCT_FIXTURE["result"]
t10 = MethodTransport({
    ("chainstack.example", "getAccountInfo"): [ok(acct)],
})
r10 = make_router(t10)
va = CW.verify_account_state(r10, WSOL, acct)
check("verify_account_state match (fixture wSOL)",
      va["status"] == "match" and va["verified"] is True)
acct_changed = json.loads(json.dumps(acct))
acct_changed["value"]["lamports"] += 1
t11 = MethodTransport({
    ("chainstack.example", "getAccountInfo"): [ok(acct_changed)],
})
r11 = make_router(t11)
va11 = CW.verify_account_state(r11, WSOL, acct)
check("verify_account_state conflict: lamports flagged",
      va11["status"] == "conflict" and va11["differences"] == ["lamports"])

# verify_sample end to end: only the risky events are verified.
# (Two DISTINCT signatures — identical params would hit the
# router's response cache and make only one transport call.)
t12 = MethodTransport({
    ("chainstack.example", "getTransaction"): [ok(TX_FIXTURE["result"]),
                                              ok(fake_tx("migSig9", 300))],
})
r12 = make_router(t12)
sample = [
    {"event": {"kind": "routine", "signature": "rout1"},
     "primary_tx": TX_FIXTURE["result"]},
    {"event": {"kind": "token_creation", "signature": TX_SIG},
     "primary_tx": TX_FIXTURE["result"]},
    {"event": {"kind": "token_migration", "signature": "migSig9"},
     "primary_tx": fake_tx("migSig9", 300)},
]
summary = CW.verify_sample(r12, sample, budget=10)
check("verify_sample: only the 2 risky events verified",
      summary["selected"] == 2 and summary["matched"] == 2)
check("verify_sample: routine event never fetched for verification",
      len(t12.calls) == 2)
check("verify_sample: no conflicts, none unverified",
      summary["conflicts"] == [] and summary["unverified"] == [])

# ================= event_backfill Phase 6: checkpoints ==================
store_home = tempfile.mkdtemp(prefix="acq_p6_ckpt_")
cs = EB.BackfillCheckpointStore(home=store_home)
check("checkpoint: none on first read",
      cs.get_checkpoint("k1") is None)
cs.set_checkpoint("k1", slot=100, signature="sig100")
got = cs.get_checkpoint("k1")
check("checkpoint: slot + signature stored",
      got["last_slot"] == 100 and got["last_signature"] == "sig100")
cs2 = EB.BackfillCheckpointStore(home=store_home)
check("checkpoint: persists across instances",
      cs2.get_checkpoint("k1")["last_signature"] == "sig100")
gap = cs.record_unrecovered_gap("k1", 101, 110, "tx fetch failed")
check("unrecovered gap: recorded with reason + attempts=1",
      gap["from_slot"] == 101 and gap["to_slot"] == 110
      and gap["attempts"] == 1)
gap2 = cs.record_unrecovered_gap("k1", 101, 110, "tx fetch failed")
check("unrecovered gap: same range bumps attempts, not duplicated",
      gap2["attempts"] == 2 and len(cs.unrecovered_gaps("k1")) == 1)
check("unrecovered gap: listed per key",
      len(cs.unrecovered_gaps("k1")) == 1
      and len(cs.unrecovered_gaps("other")) == 0)
check("unrecovered gap: clears once recovered",
      cs.clear_unrecovered_gap("k1", 101, 110) is True
      and cs.unrecovered_gaps("k1") == [])

# ================= dedup sink + order reconciliation ====================
sink = EB.DedupEventSink()
ev_a = {"signature": "s1", "slot": 10, "transactionIndex": 0}
ev_b = {"signature": "s2", "slot": 9, "transactionIndex": 5}
ev_a2 = {"signature": "s1", "slot": 10, "transactionIndex": 0}
check("sink: first add is new", sink.add(ev_a) is True)
check("sink: second distinct event is new", sink.add(ev_b) is True)
check("sink: same signature from a second provider is dropped",
      sink.add(ev_a2) is False
      and sink.duplicates_dropped == 1 and len(sink) == 2)
check("sink: seen() reports stored signatures",
      sink.seen("s1") is True and sink.seen("s9") is False)
check("sink: ordered() reconciles by (slot, tx index)",
      [e["signature"] for e in sink.ordered()] == ["s2", "s1"])
messy = [{"signature": "m3", "slot": 30, "transactionIndex": 1},
         {"signature": "m1", "slot": 30, "transactionIndex": 0},
         {"signature": "m2", "slot": 29, "transactionIndex": 9},
         {"signature": "m9"}]  # no slot: sorts last, never guessed
check("reconcile: (slot, tx index) order, slot-less last",
      [e["signature"] for e in EB.reconcile_event_order(messy)]
      == ["m2", "m1", "m3", "m9"])

# ================= signature-range recovery =============================
# Two pages (limit 3): newest-first pages -> chronological rows.
pages = [[sig_row("s8", 108), sig_row("s7", 107), sig_row("s6", 106)],
         [sig_row("s5", 105)]]
t13 = MethodTransport({
    (None, "getSignaturesForAddress"): [ok(pages[0]), ok(pages[1])],
})
r13 = make_router(t13)
rpc13 = EB.router_rpc_call(r13)
rows13, pages13, stats13 = EB.recover_signature_range(
    PUMP, rpc13, None, limit=3, max_pages=4)
check("recover_signature_range: 2 pages, 4 rows",
      pages13 == 2 and len(rows13) == 4)
check("recover_signature_range: rows chronological (oldest first)",
      [r["signature"] for r in rows13] == ["s5", "s6", "s7", "s8"])
check("recover_signature_range: no failures",
      stats13["failed"] is None and stats13["rows_seen"] == 4)

# With until=sig5: only newer signatures come back (short page
# stops paging after one call).
t14 = MethodTransport({
    (None, "getSignaturesForAddress"):
        [ok([sig_row("s8", 108), sig_row("s7", 107), sig_row("s6", 106)])],
})
r14 = make_router(t14)
rpc14 = EB.router_rpc_call(r14)
rows14, _, stats14 = EB.recover_signature_range(
    PUMP, rpc14, "sig5", limit=5, max_pages=4)
check("recover_signature_range with until: only newer rows",
      [r["signature"] for r in rows14] == ["s6", "s7", "s8"])
t14b = MethodTransport({
    (None, "getSignaturesForAddress"):
        [ok([sig_row("s8", 108), sig_row("s7", 107), sig_row("s6", 106)])],
})
r14b = make_router(t14b)
check("recover_signature_range: seen rows counted as duplicates",
      EB.recover_signature_range(
          PUMP, EB.router_rpc_call(r14b), "sig5", seen=lambda s: s == "s7",
          limit=5, max_pages=4)[2]["duplicates_dropped"] == 1)

# ================= the acceptance test: deliberately missed range =======
# Processed: sig1..sig5 (slots 1001..1005), checkpoint at sig5/1005.
# Missed: sig6..sig10 (slots 1006..1010). The backfill must close it.
missed_rows = [sig_row("sig%d" % i, 1000 + i, i - 6) for i in range(10, 5, -1)]
missed_txs = [ok(fake_tx("sig%d" % i, 1000 + i, fee=5000 + i))
              for i in range(6, 11)]
t15 = MethodTransport({
    ("g.alchemy.com", "getSignaturesForAddress"): [ok(missed_rows)],
    ("g.alchemy.com", "getTransaction"): missed_txs,
})
r15 = make_router(t15)
home15 = tempfile.mkdtemp(prefix="acq_p6_drv_")
cs15 = EB.BackfillCheckpointStore(home=home15)
sink15 = EB.DedupEventSink()
for i in range(1, 6):
    sink15.add({"signature": "sig%d" % i, "slot": 1000 + i,
                "transactionIndex": i - 1, "blockTime": 1795341600,
                "fee": 5000, "err": None, "result": {}})
cs15.set_checkpoint("pump", slot=1005, signature="sig5")
driver15 = EB.BackfillDriver(r15, checkpoints=cs15, sink=sink15)
gap15 = {"key": "pump", "from_slot": 1006, "to_slot": 1010}
report15 = driver15.recover_gap("pump", PUMP, gap15)
check("backfill missed range: 5 signatures found",
      report15["signatures_found"] == 5)
check("backfill missed range: 5 new events",
      report15["events_new"] == 5)
check("backfill missed range: sink now holds all 10, no duplicates",
      len(sink15) == 10 and sink15.duplicates_dropped == 0)
check("backfill missed range: checkpoint advanced to sig10/1010",
      report15["checkpoint"]["last_signature"] == "sig10"
      and report15["checkpoint"]["last_slot"] == 1010)
check("backfill missed range: no unrecovered gap left",
      report15["unrecovered_gap"] is None
      and cs15.unrecovered_gaps() == [])
check("backfill missed range: sink reconciles in slot order",
      [e["slot"] for e in sink15.ordered()]
      == list(range(1001, 1011)))
check("backfill: signature history went to Alchemy, never Chainstack",
      "chainstack.example" not in
      t15.hosts_for("getSignaturesForAddress"))

# Second run over the same gap: everything already seen -> 0 new,
# store still exactly 10 (duplicate elimination across runs).
report15b = driver15.recover_gap("pump", PUMP, gap15)
check("backfill re-run: 0 new events, store unchanged",
      report15b["events_new"] == 0 and len(sink15) == 10)

# recover_from_checkpoint (no explicit gap) catches up from the
# stored checkpoint alone.
t16 = MethodTransport({
    ("g.alchemy.com", "getSignaturesForAddress"):
        [ok([sig_row("sig11", 1011, 0)])],
    ("g.alchemy.com", "getTransaction"): [ok(fake_tx("sig11", 1011))],
})
r16 = make_router(t16)
driver16 = EB.BackfillDriver(r16, checkpoints=cs15, sink=sink15)
report16 = driver16.recover_from_checkpoint("pump", PUMP)
check("recover_from_checkpoint: 1 new event past the checkpoint",
      report16["events_new"] == 1 and len(sink15) == 11)
check("recover_from_checkpoint: checkpoint advanced to sig11",
      report16["checkpoint"]["last_signature"] == "sig11")

# ================= unrecovered gaps are tracked + retried ===============
# Signature page fails: gap recorded, checkpoint unmoved.
t17 = MethodTransport({
    (None, "getSignaturesForAddress"):
        [rpc_err(-32002, "archive not available")],
})
r17 = make_router(t17)
home17 = tempfile.mkdtemp(prefix="acq_p6_gap_")
cs17 = EB.BackfillCheckpointStore(home=home17)
cs17.set_checkpoint("pump", slot=2000, signature="sigA")
driver17 = EB.BackfillDriver(r17, checkpoints=cs17)
rep17 = driver17.recover_gap(
    "pump", PUMP, {"key": "pump", "from_slot": 2001, "to_slot": 2010})
check("failed signature page: unrecovered gap recorded",
      rep17["unrecovered_gap"] is not None
      and len(cs17.unrecovered_gaps("pump")) == 1)
check("failed signature page: checkpoint NOT advanced past the gap",
      cs17.get_checkpoint("pump")["last_signature"] == "sigA"
      and cs17.get_checkpoint("pump")["last_slot"] == 2000)
check("failed signature page: 0 new events",
      rep17["events_new"] == 0)

# Retry once service returns: the gap closes and is cleared.
t18 = MethodTransport({
    ("g.alchemy.com", "getSignaturesForAddress"):
        [ok([sig_row("sigB", 2005, 0)])],
    ("g.alchemy.com", "getTransaction"): [ok(fake_tx("sigB", 2005))],
})
r18 = make_router(t18)
driver18 = EB.BackfillDriver(r18, checkpoints=cs17)
reps18 = driver18.retry_unrecovered_gaps({"pump": PUMP})
check("retry unrecovered gap: range recovered on retry",
      len(reps18) == 1 and reps18[0]["events_new"] == 1)
check("retry unrecovered gap: gap cleared after recovery",
      cs17.unrecovered_gaps("pump") == [])
check("retry unrecovered gap: checkpoint advanced past the range",
      cs17.get_checkpoint("pump")["last_signature"] == "sigB")

# Transaction fetch failure: the failed slot range is recorded as
# unrecovered, the checkpoint does NOT advance past it, but the
# transactions that DID fetch are still stored.
# Newest-first page: sigD (3002) first, sigC (3001) second; the
# tx handler fails the FIRST fetch (sigC, oldest) and serves sigD.
rows19 = [sig_row("sigD", 3002, 1), sig_row("sigC", 3001, 0)]
t19 = MethodTransport({
    ("g.alchemy.com", "getSignaturesForAddress"): [ok(rows19)],
    ("g.alchemy.com", "getTransaction"):
        [rpc_err(-32005, "rate limited"), ok(fake_tx("sigD", 3002))],
})
r19 = make_router(t19)
home19 = tempfile.mkdtemp(prefix="acq_p6_txfail_")
cs19 = EB.BackfillCheckpointStore(home=home19)
sink19 = EB.DedupEventSink()
cs19.set_checkpoint("pump", slot=3000, signature="sig0")
driver19 = EB.BackfillDriver(r19, checkpoints=cs19, sink=sink19)
rep19 = driver19.recover_gap(
    "pump", PUMP, {"key": "pump", "from_slot": 3001, "to_slot": 3002})
check("tx fetch failure: 1 event stored, 1 failed",
      rep19["events_new"] == 1 and len(rep19["fetch_failed"]) == 1)
check("tx fetch failure: failed slot range recorded unrecovered",
      rep19["unrecovered_gap"] is not None
      and rep19["unrecovered_gap"]["from_slot"] == 3001
      and rep19["unrecovered_gap"]["to_slot"] == 3001)
check("tx fetch failure: checkpoint NOT advanced past the failed slot",
      cs19.get_checkpoint("pump")["last_signature"] == "sig0")

# ================= mocked-transport backfill timing ======================
t20 = MethodTransport({
    ("g.alchemy.com", "getSignaturesForAddress"): [ok(missed_rows)],
    ("g.alchemy.com", "getTransaction"): missed_txs,
}, latency_s=0.002)
r20 = make_router(t20)
home20 = tempfile.mkdtemp(prefix="acq_p6_perf_")
cs20 = EB.BackfillCheckpointStore(home=home20)
sink20 = EB.DedupEventSink()
cs20.set_checkpoint("pump", slot=1005, signature="sig5")
driver20 = EB.BackfillDriver(r20, checkpoints=cs20, sink=sink20)
perf = CW.measure_backfill_performance(
    lambda: driver20.recover_gap(
        "pump", PUMP, {"key": "pump", "from_slot": 1006,
                       "to_slot": 1010}))
check("perf: 5 events recovered in the measured run",
      perf["events_new"] == 5 and perf["signatures_found"] == 5)
check("perf: elapsed + events/s reported",
      perf["elapsed_s"] > 0 and perf["events_per_s"] is not None)
check("perf: timing labelled MOCKED TRANSPORT, not provider latency",
      perf["timing"].startswith("MOCKED TRANSPORT")
      and "NOT provider latency" in perf["timing"])

# ================= workload entry point =================================
t21 = MethodTransport({
    ("g.alchemy.com", "getSignaturesForAddress"): [ok(missed_rows)],
    ("g.alchemy.com", "getTransaction"): missed_txs,
})
r21 = make_router(t21)
home21 = tempfile.mkdtemp(prefix="acq_p6_wl_")
wl = CW.ChainstackWorkload(r21, home=home21)
check("workload: completeness + yellowstone agree with module fns",
      wl.completeness()["chainstack"]["deep_history"] is False
      and wl.yellowstone()["supported_by_plan"] is False)
wl.driver.checkpoints.set_checkpoint("pump", slot=1005,
                                     signature="sig5")
rep21 = wl.backfill_gap("pump", PUMP, {"key": "pump", "from_slot": 1006,
                                       "to_slot": 1010})
check("workload: backfill_gap closes the missed range",
      rep21["events_new"] == 5
      and len(wl.driver.sink) == 5)

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
