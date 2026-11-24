#!/usr/bin/env python3
"""Phase 7 tests — Helius specialized streaming/parsing
(helius_streams.py, helius_parsed_events.py, helius_preprocessed.py).

Plain script, no live network: the WebSocket transport is a fake
factory, the Parsed Events HTTP transport is a fake opener, and
verification runs against the REAL on-chain fixture
tests/data/intelligence/acquisition/live_getTransaction_pump.json
(captured 2026-11-22 via reserves.rpc()).

Parsed-stream payloads are DOCUMENTED SAMPLE structures (labelled
SAMPLE in this file): shapes taken from the Helius Parsed Streams
Quickstart / Parsed Events docs read 2026-11-22, with values copied
from the real fixture where the test needs agreement with the own
decoder. They are NEVER presented as live data.

Covers (spec §6): filter building + documented limits, preset
filters, notification parsing (decoded/recognized/undecoded),
transfer extraction, verification against the own decoder
(agree -> verified, tamper -> CONFLICT flagged, unconfident own
decoder -> unverifiable), Parsed Events selective-use refusal,
preprocessed two-stage lifecycle with the stream DISABLED
(preliminary data can NEVER reach confirmed state or authorize a
trade), LaserStream evaluation (WSS standard is raw-only, gRPC
paid — not built), and router P0–P3 enforcement for the Helius
workloads (preprocessed P0 refused, parsed stream P1/P0, complex
parse P2, P3 routed elsewhere).
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import base64
import io
import json
import os
import struct
import sys
import tempfile
import urllib.error as _urlerr

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import helius_streams as HS
from daemon.intelligence.acquisition import helius_parsed_events as HPE
from daemon.intelligence.acquisition import helius_preprocessed as HPP
from daemon.intelligence.acquisition import provider_router as PR
from daemon.intelligence.acquisition.request_scheduler import (
    PRIORITY_BACKGROUND, PRIORITY_CARD, PRIORITY_CRITICAL)
from daemon.intelligence.decoders import solana_transaction as ST
from daemon.intelligence.decoders._base58 import b58decode

fails = []
total = [0]

FIXDIR = os.path.join(REPO, "tests", "data", "intelligence",
                      "acquisition")
with open(os.path.join(FIXDIR, "live_getTransaction_pump.json")) as fh:
    FIX_TX = json.load(fh)["result"]
FIX_SIG = FIX_TX["transaction"]["signatures"][0]
FIX_SLOT = FIX_TX["slot"]
FIX_FEE = FIX_TX["meta"]["fee"]
OWN = ST.decode_transaction(FIX_TX)
OWN_PROGRAMS = sorted(p for p in OWN["program_ids"] if p)


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


def sample_notification(fee=None, status="error", programs=None,
                        decoded=True, summary_type="swap"):
    """SAMPLE parsed-stream notification (doc-shaped, NOT live
    data). Values mirror the real fixture so the own decoder can
    agree with it. ``decoded=False`` makes the matched
    instruction undecoded."""
    programs = programs if programs is not None else OWN_PROGRAMS
    instructions = []
    for i, prog in enumerate(programs):
        ix = {"instructionIndex": i, "innerInstructionIndex": None,
              "stackHeight": 1, "programId": prog}
        if prog == HS.JUPITER_PROGRAM_ID:
            if decoded:
                ix["decoded"] = {
                    "name": "route",
                    "args": {"in_amount": "1000000"},
                    "accounts": {"user_transfer_authority": "7jjEEgTx3xjGMHrykHQmjcWQjzXmZx83SQJWnYwREK5H"}}
                ix["summary"] = {"type": summary_type,
                                 "description": "SAMPLE: wallet swapped "
                                 "via Jupiter (doc-shaped sample)",
                                 "parsedData": {"protocol": "jupiter",
                                                "in_amount": "1000000"}}
            else:
                ix["decoded"] = None
                ix["rawData"] = "3Bxs8eYk9Q"
                ix["rawAccounts"] = ["7jjEEgTx3xjGMHrykHQmjcWQjzXmZx83SQJWnYwREK5H"]
        else:
            ix["decoded"] = {"name": "transfer",
                             "args": {},
                             "accounts": {}}
        instructions.append(ix)
    matched = [programs.index(HS.JUPITER_PROGRAM_ID)]
    return {
        "jsonrpc": "2.0",
        "method": "parsedTransactionNotification",
        "params": {
            "subscription": 7,
            "result": {
                "value": {
                    "signature": FIX_SIG,
                    "slot": FIX_SLOT,
                    "status": status,
                    "error": ({"InstructionError": [6, {"Custom": 6013}]}
                              if status == "error" else None),
                    "fee": FIX_FEE if fee is None else fee,
                    "feePayer": OWN["account_keys"][0],
                    "accountKeys": OWN["account_keys"],
                    "instructions": instructions,
                    "matchedIndexes": matched,
                    "summary": {"type": summary_type,
                                "description": "SAMPLE transaction "
                                "summary (doc-shaped sample)",
                                "parsedData": {}},
                    "nativeTransfers": [
                        {"fromUserAccount": OWN["account_keys"][0],
                         "toUserAccount": OWN["account_keys"][1],
                         "amount": 5010}],
                    "tokenTransfers": [
                        {"fromUserAccount": OWN["account_keys"][0],
                         "toUserAccount": OWN["account_keys"][1],
                         "tokenAmount": "1000000",
                         "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"}],
                    "blockTime": None,
                }
            }
        }
    }


# ---------------- filters ----------------
f = HS.build_filter(programs=[HS.PUMP_PROGRAM_ID])
check("build_filter: pump program only",
      f["programs"] == [HS.PUMP_PROGRAM_ID]
      and f["includeCpi"] is True and f["includeFailed"] is False)
try:
    HS.build_filter()
    check("build_filter: empty filter refused", False)
except ValueError:
    check("build_filter: empty filter refused", True)
try:
    HS.build_filter(programs=["p%d" % i for i in range(11)])
    check("build_filter: 11 programs refused (limit 10)", False)
except ValueError:
    check("build_filter: 11 programs refused (limit 10)", True)
try:
    HS.build_filter(accounts_include=["a%d" % i for i in range(101)])
    check("build_filter: 101 accounts refused (limit 100)", False)
except ValueError:
    check("build_filter: 101 accounts refused (limit 100)", True)
try:
    HS.build_filter(instruction_names=["n%d" % i for i in range(51)])
    check("build_filter: 51 instruction names refused (limit 50)",
          False)
except ValueError:
    check("build_filter: 51 instruction names refused (limit 50)",
          True)
try:
    HS.build_filter(programs=[""], accounts_include=["x"])
    check("build_filter: empty program string refused", False)
except ValueError:
    check("build_filter: empty program string refused", True)
check("validate_filter: clean filter has no problems",
      HS.validate_filter(f) == [])
check("validate_filter: unknown field is a problem",
      any("unknown filter field" in p
          for p in HS.validate_filter(dict(f, surprise=True))))
check("validate_filter: empty filter is a problem",
      HS.validate_filter({}) != [])
check("pumpfun_filter targets the pump program",
      HS.pumpfun_filter()["programs"] == [HS.PUMP_PROGRAM_ID])
check("pumpswap_filter targets the pumpswap program",
      HS.pumpswap_filter()["programs"] == [HS.PUMPSWAP_PROGRAM_ID])
check("dex_filter covers 5 high-priority DEX programs, no more",
      HS.dex_filter()["programs"] == list(HS.DEX_PROGRAMS)
      and len(HS.DEX_PROGRAMS) == 5)
check("wallet_filter requires accounts.include",
      HS.wallet_filter(["WALLET1"])["accounts"]["include"]
      == ["WALLET1"])
check("liquidity_filter is DEX-scoped, not the full stream",
      HS.liquidity_filter()["programs"] == list(HS.DEX_PROGRAMS))
check("filter limits match the documented Quickstart limits",
      HS.MAX_PROGRAMS_PER_FILTER == 10
      and HS.MAX_INSTRUCTION_NAMES_PER_FILTER == 50
      and HS.MAX_ACCOUNTS_INCLUDE_PER_FILTER == 100
      and HS.MAX_ACCOUNT_ROLES_PER_FILTER == 20
      and HS.MAX_SUBSCRIPTIONS_PER_CONNECTION == 25
      and HS.MAX_CONCURRENT_CONNECTIONS_FREE == 5)

# ---------------- notification parsing ----------------
kind, event = HS.parse_notification(sample_notification())
check("parse_notification: SAMPLE notification parses",
      kind == "notification" and event is not None
      and event["signature"] == FIX_SIG
      and event["slot"] == FIX_SLOT and event["fee"] == FIX_FEE
      and event["status"] == "error")
check("parsed event starts parsed_unverified, never verified",
      event["verification_status"] == "parsed_unverified")
check("parsed event carries matched indexes + summary type",
      event["matched_indexes"]
      == [OWN_PROGRAMS.index(HS.JUPITER_PROGRAM_ID)]
      and event["summary_type"] == "swap")
check("parsed event credits 1 credit/event (capability config)",
      event["credits"] == 1)
check("blockTime is null in the SAMPLE (docs: always null)",
      sample_notification()["params"]["result"]["value"]["blockTime"]
      is None)
kind_u, event_u = HS.parse_notification(
    sample_notification(decoded=False))
check("undecoded matched instruction -> verification undecoded",
      kind_u == "notification"
      and event_u["verification_status"] == "undecoded"
      and "no decoded claim" in event_u["verification_notes"][0])
kind_e, payload_e = HS.parse_notification(
    {"jsonrpc": "2.0", "id": 3,
     "error": {"code": -32602, "message": "unknown field"}})
check("JSON-RPC error frame parses with code",
      kind_e == "error" and payload_e["code"] == -32602)
kind_a, payload_a = HS.parse_notification(
    {"jsonrpc": "2.0", "id": 1, "result": 42})
check("subscribe ack parses (server id int)",
      kind_a == "subscribe_ack" and payload_a["server_id"] == 42)
check("non-dict frame is ignored",
      HS.parse_notification("not a dict") == ("ignored", None))
check("notification without signature is ignored",
      HS.parse_notification(
          {"method": "parsedTransactionNotification",
           "params": {"result": {"value": {"slot": 1}}}})
      == ("ignored", None))

transfers = HS.extract_transfers(event)
check("extract_transfers: SOL + token, venue-reported amounts",
      len(transfers) == 2
      and transfers[0]["kind"] == "sol"
      and transfers[0]["amount"] == 5010
      and transfers[1]["kind"] == "token"
      and transfers[1]["amount"] == "1000000"
      and transfers[1]["mint"].startswith("EPjF"))
check("extract_transfers drops entries with no amount",
      HS.extract_transfers(
          {"native_transfers": [{"fromUserAccount": "a"}],
           "token_transfers": []}) == [])

# ---------------- verification vs own decoder ----------------
verified = HS.verify_with_own_decoder(event, FIX_TX)
check("verification: SAMPLE agreeing with own decoder -> verified",
      verified["verification_status"] == "verified")
check("verification does not mutate the input event",
      event["verification_status"] == "parsed_unverified")
tampered = HS.verify_with_own_decoder(
    dict(event, fee=FIX_FEE + 1), FIX_TX)
check("verification: tampered fee -> CONFLICT flagged",
      tampered["verification_status"] == "conflict"
      and any("fee mismatch" in n
              for n in tampered["verification_notes"]))
tampered2 = HS.verify_with_own_decoder(
    dict(event, slot=FIX_SLOT + 1), FIX_TX)
check("verification: tampered slot -> CONFLICT flagged",
      tampered2["verification_status"] == "conflict"
      and any("slot mismatch" in n
              for n in tampered2["verification_notes"]))


def _unknown_decoder(_tx):
    return {"signature": FIX_SIG, "validation_status": "unknown",
            "notes": ["cannot decode confidently"], "meta": {}}


unverifiable = HS.verify_with_own_decoder(
    event, FIX_TX, decoder=_unknown_decoder)
check("verification: unconfident own decoder -> unverifiable, "
      "never verified on a guess",
      unverifiable["verification_status"] == "unverifiable")


def _raising_decoder(_tx):
    raise RuntimeError("decoder blew up")


check("verification: decoder raising -> unverifiable, no crash",
      HS.verify_with_own_decoder(event, FIX_TX,
                                 decoder=_raising_decoder)
      ["verification_status"] == "unverifiable")

# ---------------- Parsed Events client (fake HTTP) ----------------
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


class FakeOpener:
    def __init__(self, behaviour):
        self.behaviour = behaviour  # path-substring -> (status, body)
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append(req.full_url)
        for key, (status, body) in self.behaviour.items():
            if key in req.full_url:
                return FakeResp(body, status)
        raise AssertionError("unexpected url %s" % req.full_url)


import urllib.error as _urlerr
_orig_http_error = _urlerr.HTTPError


class Real429Opener:
    def __call__(self, req, timeout=None):
        raise _orig_http_error(req.full_url, 429, "Too Many Requests",
                               {}, io.BytesIO(b"{}"))


sample_value = sample_notification()["params"]["result"]["value"]
opener = FakeOpener({
    "/transactions": (200, [sample_value]),
    "/transaction-history": (200, {"transactions": [sample_value],
                                   "paginationToken": None}),
    "/scan": (200, {"transactions": [sample_value],
                    "paginationToken": "tok"}),
    "/idl/": (200, {"versions": [1, 2]}),
})
client = HPE.HeliusParsedEventsClient(api_key="testkey-not-real",
                                      _opener=opener)
check("parsed-events client configured with a supplied key",
      client.configured() is True)
check("parsed-events client unconfigured without a key",
      HPE.HeliusParsedEventsClient(api_key=None,
                                   _opener=opener).configured()
      is False or os.environ.get("HELIUS_API_KEY") is not None)
res = client.parse_transactions([FIX_SIG], "complex_investigation")
check("parse_transactions: selective reason accepted, normalized",
      res["kind"] == "ok" and len(res["result"]) == 1
      and res["result"][0]["signature"] == FIX_SIG
      and res["result"][0]["verification_status"]
      == "parsed_unverified"
      and res["credits"] == 10)
check("parse_transactions charges 10 credits/request",
      client.stats()["credits_spent"] == 10
      and client.stats()["calls"] == 1)
check("parse_transactions: routine reason REFUSED (never default)",
      client.parse_transactions([FIX_SIG], "routine")["kind"]
      == "refused")
check("parse_transactions: no reason REFUSED",
      client.parse_transactions([FIX_SIG], None)["kind"] == "refused")
check("parse_transactions: >100 signatures REFUSED",
      client.parse_transactions(["s"] * 101,
                                "complex_investigation")["kind"]
      == "refused")
check("parse_transactions: empty list REFUSED",
      client.parse_transactions([], "complex_investigation")["kind"]
      == "refused")
hist = client.transaction_history("WALLET1", "high_value_wallet")
check("transaction_history: selective reason accepted",
      hist["kind"] == "ok" and len(hist["result"]["transactions"]) == 1)
scan = client.scan_history("WALLET1", "disputed_event",
                           program_id=HS.PUMP_PROGRAM_ID)
check("scan_history: program-scoped scan accepted",
      scan["kind"] == "ok"
      and scan["result"]["paginationToken"] == "tok")
check("scan_history: no program/mint REFUSED",
      client.scan_history("WALLET1", "disputed_event")["kind"]
      == "refused")
idl = client.list_idl_versions(HS.PUMP_PROGRAM_ID,
                               "historical_interpretation")
check("list_idl_versions works",
      idl["kind"] == "ok" and idl["result"]["versions"] == [1, 2])
check("selective_use_check: all five reasons allowed",
      all(HPE.selective_use_check(r)[0] for r in HPE.ALLOWED_REASONS)
      and len(HPE.ALLOWED_REASONS) == 5)
check("selective_use_check: routine refused",
      HPE.selective_use_check("routine")[0] is False)
check("parsed-events auth: key travels as the api-key query "
      "param (docs), one call per request",
      all("api-key=testkey-not-real" in u for u in opener.calls))
client429 = HPE.HeliusParsedEventsClient(
    api_key="testkey-not-real", _opener=Real429Opener())
res429 = client429.parse_transactions([FIX_SIG],
                                      "complex_investigation")
check("parsed-events 429 -> rate_limited, not ok",
      res429["kind"] == "rate_limited")
# verify() shares the stream verdict
verified_ev = client.verify(res["result"][0], FIX_TX)
check("parsed-events verify() agrees with own decoder",
      verified_ev["verification_status"] == "verified")

# ---------------- preprocessed lifecycle (DISABLED) ----------------
refusal = HPP.request_stream()
check("preprocessed stream request ALWAYS REFUSED (paid-gated)",
      refusal["refused"] is True
      and "PAID-GATED" in refusal["refusal_reason"]
      and refusal["stream_enabled"] is False)
check("preprocessed credit cost recorded as unknown, never "
      "invented", HPP.PREPROCESSED_CREDIT_COST is None)
check("preprocessed frame: too short -> None",
      HPP.decode_preprocessed_frame(b"\x01\x00") is None)
check("preprocessed frame: wrong version -> None",
      HPP.decode_preprocessed_frame(
          b"\x02" + b"\x00" * 80) is None)


def synth_frame(slot, signature, tx_bytes=b"\x01\x02\x03"):
    raw = (bytes([1]) + struct.pack("<Q", slot)
           + b58decode(signature) + tx_bytes)
    assert len(raw) == 73 + len(tx_bytes)
    return raw


frame = synth_frame(FIX_SLOT, FIX_SIG)
decoded_frame = HPP.decode_preprocessed_frame(frame)
check("preprocessed frame decodes per documented layout (SYNTHETIC)",
      decoded_frame is not None
      and decoded_frame["version"] == 1
      and decoded_frame["slot"] == FIX_SLOT
      and decoded_frame["signature"] == FIX_SIG)
life = HPP.PreprocessedLifecycle()
rec, note = life.observe(frame)
check("stage 1: observation is preliminary + unverified",
      rec is not None and rec["stage"] == "preliminary"
      and rec["verified"] is False and note is None)
check("stage 1: preliminary record lives ONLY in preliminary "
      "store", life.preliminary(FIX_SIG) is rec
      and life.verified(FIX_SIG) is None)
try:
    life.apply_to_confirmed_state(rec)
    check("PRELIMINARY DATA CANNOT REACH CONFIRMED STATE", False)
except ValueError as exc:
    check("PRELIMINARY DATA CANNOT REACH CONFIRMED STATE",
          "must NEVER update confirmed market reserves" in str(exc))
try:
    life.authorize_trade(rec)
    check("PRELIMINARY DATA CANNOT AUTHORIZE A TRADE", False)
except ValueError as exc:
    check("PRELIMINARY DATA CANNOT AUTHORIZE A TRADE",
          "must NEVER authorize a trade" in str(exc))
check("verify_execution with no observation is refused",
      life.verify_execution("NoSuchSignature123", FIX_TX)[0] is None)
verified_rec, note2 = life.verify_execution(FIX_SIG, FIX_TX)
check("stage 2: confirmed getTransaction promotes the record",
      verified_rec is not None and verified_rec["stage"] == "verified"
      and verified_rec["verified"] is True and note2 is None)
check("stage 2: promotion removes the preliminary entry",
      life.preliminary(FIX_SIG) is None
      and life.verified(FIX_SIG) is verified_rec)
applied = life.apply_to_confirmed_state(verified_rec)
check("verified record MAY update confirmed state",
      applied["applied"] is True
      and applied["execution"]["succeeded"] is False)
try:
    life.authorize_trade(verified_rec)
    check("verified-but-FAILED tx cannot authorize a trade", False)
except ValueError as exc:
    check("verified-but-FAILED tx cannot authorize a trade",
          "executed with error" in str(exc))
# Conflict path: slot mismatch
life2 = HPP.PreprocessedLifecycle()
life2.observe(synth_frame(FIX_SLOT, FIX_SIG))
wrong_slot_tx = dict(FIX_TX, slot=FIX_SLOT + 5)
promoted, note3 = life2.verify_execution(FIX_SIG, wrong_slot_tx)
check("stage 2: slot mismatch -> conflict, never promoted",
      promoted is None and "slot" in (note3 or "")
      and len(life2.conflicts()) == 1
      and life2.verified(FIX_SIG) is None)
check("preprocessed stats report the stream disabled",
      life.stats()["stream_enabled"] is False
      and life.stats()["credit_cost"] is None)
# A synthetic SUCCEEDED tx authorizes a trade after verification
ok_tx = json.loads(json.dumps(FIX_TX))
ok_tx["meta"] = dict(ok_tx["meta"], err=None)
life3 = HPP.PreprocessedLifecycle()
life3.observe(synth_frame(FIX_SLOT, FIX_SIG))
v3, _ = life3.verify_execution(FIX_SIG, ok_tx)
check("verified + succeeded record authorizes a trade",
      life3.authorize_trade(v3)["authorized"] is True)

# ---------------- LaserStream evaluation ----------------
ev = HS.laserstream_evaluation()
check("LaserStream verdict: parsed-streams endpoint, not gRPC",
      ev["verdict"] == "parsed_streams_endpoint")
check("LaserStream WSS standard is Free but RAW-only (no "
      "decoding)", ev["laserstream_wss_standard"]["included_on_free"]
      is True and "RAW" in ev["laserstream_wss_standard"]["delivers"])
check("LaserStream WSS extensions are PAID (Developer+)",
      ev["laserstream_wss_extensions"]["included_on_free"] is False)
check("LaserStream gRPC is PAID-GATED — not built",
      ev["laserstream_grpc"]["included_on_free"] is False
      and "no grpc client built" in ev["laserstream_grpc"]["note"].lower())
check("LaserStream evaluation agrees with capability config",
      ev["capability_config_agrees"] is True)

# ---------------- router P0–P3 enforcement ----------------
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
os.environ["CHAINSTACK_ENDPOINT"] = "https://chainstack.example.invalid/abc"
HOME = tempfile.mkdtemp(prefix="acq_phase7_")
router = PR.ProviderRouter(home=HOME, helius_enabled=True)
d = router.route_stream(PR.PARSED_TX_STREAM, priority=PRIORITY_CARD)
check("router: parsed stream CARD -> P1, Helius only, not refused",
      d["refused"] is False and d["helius_priority"] == "P1"
      and d["provider"] == "helius"
      and d["stream"] == "parsed_streams")
d0 = router.route_stream(PR.PARSED_TX_STREAM,
                         priority=PRIORITY_CRITICAL)
check("router: parsed stream CRITICAL -> P0",
      d0["helius_priority"] == "P0" and d0["refused"] is False)
d2 = router.route_stream(PR.COMPLEX_TX_PARSE, priority=PRIORITY_CARD)
check("router: complex parse CARD -> P2, Helius only (parsed "
      "events)", d2["refused"] is False
      and d2["helius_priority"] == "P2"
      and d2["stream"] == "parsed_events")
dp = router.route("getTransaction", hints={
    "workload": PR.PREPROCESSED_OBSERVATION})
check("router: preprocessed observation ALWAYS REFUSED (P0)",
      dp["refused"] is True and dp["helius_priority"] == "P0"
      and "PAID-GATED" in (dp["refusal_reason"] or ""))
dps = router.route_stream(PR.PREPROCESSED_OBSERVATION)
check("router: route_stream preprocessed also refused",
      dps["refused"] is True)
router_off = PR.ProviderRouter(home=HOME, helius_enabled=False)
check("router: parsed stream refused when Helius disabled",
      router_off.route_stream(PR.PARSED_TX_STREAM)["refused"] is True)
check("router: complex parse refused when Helius disabled",
      router_off.route_stream(PR.COMPLEX_TX_PARSE)["refused"] is True)
# P3: background routine work routes elsewhere; Helius is last
# resort in shadow phase, absent in the end state.
dec_p3 = router.route("getAccountInfo", ["x"],
                      priority=PRIORITY_BACKGROUND)
check("router: P3 background routine work -> Helius last resort",
      dec_p3["helius_priority"] == "P3"
      and dec_p3["candidates"][-1] == "helius"
      and dec_p3["primary"] != "helius")
dec_end = router_off.route("getAccountInfo", ["x"],
                           priority=PRIORITY_BACKGROUND)
check("router: P3 end state (Helius off) -> Helius absent",
      "helius" not in dec_end["candidates"])
check("router: classify rejects a stream workload on call()",
      router.classify("getTransaction", None,
                      {"workload": PR.PARSED_TX_STREAM})[2] is not None)

# ---------------- stream client lifecycle (fake socket) ----------------
class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(json.loads(text))

    def close(self):
        self.closed = True

    def run_forever(self):
        return None


def fake_factory(url, on_message, on_error, on_close, on_open):
    ws = FakeWS()
    fake_factory.last = ws
    fake_factory.handlers = (on_message, on_error, on_close, on_open)
    return ws


received = []
stream = HS.HeliusParsedStreamClient(
    wss_url="wss://fs-beta.helius-rpc.com/?api-key=testkey-not-real",
    router=router, ws_factory=fake_factory,
    on_event=received.append)
check("stream client configured with a supplied URL",
      stream.configured() is True)
check("stream client unconfigured without a URL",
      HS.HeliusParsedStreamClient(wss_url=None).configured() is False)
ok, note = stream.subscribe(HS.pumpfun_filter(), "pumpfun")
check("stream subscribe: valid filter + router P1 accepted",
      ok is True and note is None)
ok, note = stream.subscribe({}, "bad")
check("stream subscribe: invalid filter refused",
      ok is False and "invalid filter" in (note or ""))
ok, note = stream.subscribe(HS.pumpfun_filter(), "bad-details",
                            details="bogus")
check("stream subscribe: bad details value refused",
      ok is False)
check("stream connect constructs the transport",
      stream.connect() is True)
check("stream sends nothing before the handshake opens (D-02)",
      fake_factory.last.sent == [] and stream.connected is False)
fake_factory.handlers[3](fake_factory.last)  # handshake -> on_open
check("stream connected only after open", stream.connected is True)
check("stream client sends subscribe exactly once after open",
      len(fake_factory.last.sent) == 1
      and fake_factory.last.sent[0]["method"]
      == "parsedTransactionSubscribe"
      and fake_factory.last.sent[0]["params"][0]["programs"]
      == [HS.PUMP_PROGRAM_ID])
# Drive a notification through the client's own handler.
on_message = fake_factory.handlers[0]
on_message(None, json.dumps(sample_notification()))
check("stream client delivers parsed events to on_event",
      len(received) == 1 and received[0]["signature"] == FIX_SIG
      and received[0]["verification_status"] == "parsed_unverified")
check("stream client records last_slot for RPC backfill resume",
      stream.last_slot == FIX_SLOT
      and stream.resume_info()["gap_recovery"]
      == "rpc_backfill_from_last_slot"
      and stream.resume_info()["replay_available"] is False)
check("stream client stats count the event",
      stream.stats()["events_received"] == 1)
# Subscribe ack
on_message(None, json.dumps({"jsonrpc": "2.0", "id": 1,
                             "result": 99}))
check("stream client records server subscription id",
      stream.wanted_subscriptions()[0]["server_id"] == 99)
# Error frame with rate-limit code counts as a 429 signal
on_message(None, json.dumps({"jsonrpc": "2.0", "id": 2,
                             "error": {"code": -32002,
                                       "message": "rate limited"}}))
check("stream client counts -32002 rate-limit errors",
      stream.handshake_429s == 1)
# Reconnect backoff doubles and caps at 60s
check("stream reconnect backoff starts at 2s",
      stream.next_reconnect_wait_s() == 2.0)
stream._schedule_reconnect()
check("stream reconnect backoff doubles to 4s",
      stream.next_reconnect_wait_s() == 4.0)
for _ in range(10):
    stream._schedule_reconnect()
check("stream reconnect backoff caps at 60s",
      stream.next_reconnect_wait_s() == 60.0)
# 25-subscription limit
big = HS.HeliusParsedStreamClient(
    wss_url="wss://fs-beta.helius-rpc.com/?api-key=testkey-not-real",
    router=router, ws_factory=fake_factory)
for i in range(25):
    ok, _ = big.subscribe(HS.pumpfun_filter(), "sub-%d" % i)
    assert ok, "sub %d should fit" % i
ok, note = big.subscribe(HS.pumpfun_filter(), "sub-25")
check("stream client enforces 25 subscriptions per connection",
      ok is False and "too many subscriptions" in (note or ""))
check("stream unsubscribe removes the wanted subscription",
      big.unsubscribe("sub-0") is True
      and len(big.wanted_subscriptions()) == 24)
# Router refusal: Helius disabled -> no subscribe, no connect
off_client = HS.HeliusParsedStreamClient(
    wss_url="wss://fs-beta.helius-rpc.com/?api-key=testkey-not-real",
    router=router_off, ws_factory=fake_factory)
ok, note = off_client.subscribe(HS.pumpfun_filter(), "pumpfun")
check("stream subscribe refused when router refuses (Helius off)",
      ok is False and "unavailable" in (note or ""))
check("stream connect refused when router refuses (Helius off)",
      off_client.connect() is False)
# Unconfigured client refuses to connect
check("stream connect refused without a URL",
      HS.HeliusParsedStreamClient(wss_url=None, router=router,
                                  ws_factory=fake_factory).connect()
      is False)
stream.stop()
check("stream stop closes the socket",
      fake_factory.last.closed is True)

del os.environ["ALCHEMY_API_KEY"]
del os.environ["CHAINSTACK_ENDPOINT"]

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
