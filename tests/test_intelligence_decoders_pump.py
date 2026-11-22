#!/usr/bin/env python3
"""Tests for the Pump.fun and PumpSwap decoders (Phase 3b, 2026-11-22).

Plain script, run directly with the venv python (never pytest):
    ~/workspace/Forex-agent/.venv/bin/python tests/test_intelligence_decoders_pump.py

Fixtures are real on-chain transactions captured via
daemon/launch_scanner/reserves.py rpc() (public-RPC fallback; Helius
failed closed during capture) plus the sibling Phase 3a NOTAI
fixtures. Every expected value below was verified against the
official program IDLs and the fixture bytes themselves — nothing is
asserted from memory.

Cross-checks against the existing in-repo parsers
(daemon/launch_scanner/reserves.py) prove the new decoders agree
with the code the bot already runs.
"""
import base64
import hashlib
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from daemon.intelligence.decoders import pumpfun_decoder as pf
from daemon.intelligence.decoders import pumpswap_decoder as ps
from daemon.launch_scanner import reserves

FAILS = []
PASSES = [0]


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if cond:
        PASSES[0] += 1
    else:
        FAILS.append(name)


DATA = os.path.join(os.path.dirname(__file__), "data", "intelligence")

ATX_MINT = "BiLAn35QaVqQC8f8BGrE92r2n5dcDEJmqJcx1v9Apump"
ATX_CURVE = "AKw3WbKLds6u6Mu3URSiYtu3K8FNna9Vh4yommtyy8Un"
ATX_POOL = "3Y7Knabn84dpB6yy5LbCFt574E8ifTMeWkBMTPvGZCwE"
ATX_CREATOR = "An7SUk47W5C3Hvoqgw3y4rHafw244vyHq2HXXTJrKm7"
ATX_POOL_CREATOR = "3GhizoyYQiDemJzWja7ZLygzQsW5FgCYkiD6ky7p5CkG"
NOTAI_MINT = "8bGA7uQRGURruJLacPopiTSJtzS49VvLL7C5yDBpump"
NOTAI_CREATOR = "5s51HvzZjAaq9VeZZvWbLgzTV2Npgk3qPQ5DgNb7xsuR"
WSOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def load_fixture(name):
    """Return the getTransaction result dict for a fixture file.

    ATX fixtures wrap as {"capture": ..., "transaction": {...}};
    NOTAI fixtures wrap as {"capture": ..., "rpc_result": {...}}.
    """
    with open(os.path.join(DATA, name)) as f:
        wrap = json.load(f)
    return wrap.get("transaction") or wrap.get("rpc_result") or wrap


def load_accounts():
    with open(os.path.join(DATA, "atx_accounts.json")) as f:
        return json.load(f)


def types(events):
    return sorted(e.get("event_type") for e in events)


def by_type(events, event_type):
    for e in events:
        if e.get("event_type") == event_type:
            return e
    return None


def anchor_disc(kind, name):
    return list(hashlib.sha256(("%s:%s" % (kind, name)).encode())
                .digest()[:8])


# ---------------------------------------------------------------------------
# 1. Discriminators: Anchor rule vs IDL explicit arrays / fixture bytes
# ---------------------------------------------------------------------------
def test_discriminators():
    for name, want in [
            ("create_v2", [214, 144, 76, 236, 95, 139, 49, 180]),
            ("buy_v2", [184, 23, 238, 97, 103, 197, 211, 61]),
            ("buy_v3", [7, 5, 29, 196, 245, 23, 101, 80]),
            ("buy_exact_quote_in_v3", [225, 247, 80, 30, 213, 179, 132, 136]),
            ("sell_v3", [28, 146, 222, 119, 38, 196, 105, 213]),
            ("migrate_v2", [187, 203, 18, 31, 206, 237, 254, 41]),
            ("sell", [51, 230, 133, 164, 1, 127, 131, 173]),
            ("buy_exact_quote_in", [198, 46, 21, 82, 180, 217, 232, 112]),
            ("sell_v2", [93, 246, 130, 60, 231, 233, 64, 178]),
            ("create_pool", [233, 146, 209, 142, 207, 104, 64, 188]),
            ("buy_exact_quote_in_v2", [194, 171, 28, 70, 104, 77, 91, 47]),
            ("init_boost", [140, 233, 33, 94, 132, 90, 194, 143]),
            ("deposit", [242, 35, 198, 137, 82, 225, 242, 182]),
            ("withdraw", [183, 18, 70, 156, 148, 109, 161, 34]),
            ("close_user_volume_accumulator",
             [249, 69, 164, 218, 150, 103, 84, 138]),
    ]:
        check("anchor disc global:%s" % name,
              anchor_disc("global", name) == want)
    for name, want in [
            ("CreateEvent", [27, 114, 169, 77, 222, 235, 99, 118]),
            ("TradeEvent", [189, 219, 127, 211, 78, 230, 97, 238]),
            ("CompleteEvent", [95, 114, 97, 156, 212, 46, 152, 8]),
            ("CompletePumpAmmMigrationEvent",
             [189, 233, 93, 185, 92, 148, 234, 148]),
            ("PostCompleteBuyEvent",
             [111, 176, 109, 139, 49, 108, 213, 251]),
            ("CloseUserVolumeAccumulatorEvent",
             [146, 159, 189, 172, 146, 88, 56, 244]),
            ("CreatePoolEvent", [177, 49, 12, 210, 160, 118, 167, 116]),
            ("BuyEvent", [103, 244, 82, 31, 44, 245, 119, 119]),
            ("SellEvent", [62, 47, 55, 10, 165, 3, 220, 42]),
            ("DepositEvent", [120, 248, 61, 83, 31, 142, 107, 144]),
            ("WithdrawEvent", [22, 9, 133, 26, 160, 44, 71, 192]),
            ("InitBoostEvent", [174, 124, 74, 249, 4, 81, 246, 17]),
    ]:
        check("anchor disc event:%s" % name,
              anchor_disc("event", name) == want)
    # Decoder tables carry the same values.
    check("pf INSTRUCTIONS create_v2 disc",
          list(pf.INSTRUCTIONS["create_v2"][0]) ==
          [214, 144, 76, 236, 95, 139, 49, 180])
    check("ps INSTRUCTIONS sell_v2 disc",
          list(ps.INSTRUCTIONS["sell_v2"][0]) ==
          [93, 246, 130, 60, 231, 233, 64, 178])
    check("pf EVENTS TradeEvent disc",
          list(pf.EVENTS["TradeEvent"]) ==
          [189, 219, 127, 211, 78, 230, 97, 238])
    check("ps EVENTS BuyEvent disc",
          list(ps.EVENTS["BuyEvent"]) ==
          [103, 244, 82, 31, 44, 245, 119, 119])
    check("emit disc is the observed constant, not global:emit",
          list(pf.EMIT_DISC) == [228, 69, 165, 46, 81, 203, 154, 29]
          and list(pf.EMIT_DISC) != anchor_disc("global", "emit"))
    check("program ids",
          pf.PUMP_PROGRAM_ID ==
          "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
          and ps.PUMPSWAP_PROGRAM_ID ==
          "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")


# ---------------------------------------------------------------------------
# 2. Envelope contract: every decoded event carries the required fields
# ---------------------------------------------------------------------------
def check_envelope(events, signature, slot):
    """Shared envelope assertions, called by the test_* cases
    below with explicit args (not itself a pytest test)."""
    required = ("signature", "slot", "block_time", "program_id",
                "event_type", "instruction", "fields", "raw",
                "decoder_version", "validation")
    for e in events:
        for key in required:
            if key not in e:
                check("envelope has %s (%s)" % (key, e.get("event_type")),
                      False)
                return
    check("envelope: all %d events carry required fields"
          % len(events), True)
    check("envelope: signature/slot match fixture",
          all(e.get("signature") == signature
              and e.get("slot") == slot for e in events))


# ---------------------------------------------------------------------------
# 3. Pump.fun: ATX create / buy / sell / failed sell / migrate
# ---------------------------------------------------------------------------
def test_pumpfun_atx():
    tx = load_fixture("atx_create.json")
    sig = "2avAXaepDw4uPC4Ue3khKWx9fpQzXPvm3pNYtZi1VwJfvNsD8GY25WzYaXvAWmKsTgdtAT5NRHxgzmDF67RRfih"
    events = pf.decode_transaction(tx, sig)
    check_envelope(events, sig, 472184636)
    check("atx_create event set",
          types(events) == ["event:CreateEvent", "event:TradeEvent",
                            "instruction:buy_v2",
                            "instruction:create_v2"])
    create_ix = by_type(events, "instruction:create_v2")
    check("create_v2 args name/symbol/uri",
          create_ix["fields"].get("name") == "PROJECT ATX"
          and create_ix["fields"].get("symbol") == "ATX"
          and create_ix["fields"].get("uri") ==
          "https://ipfs.io/ipfs/QmYuq3eSSndVthXQ9gBd47Rjx7AdATZGd1pfFRcwP8arJQ")
    # Correction (2026-11-22): an earlier analysis read the creator
    # arg one byte late and produced a phantom pubkey; the decoder's
    # exact 144-byte consumption plus an independent manual parse
    # prove the arg at offset 101 IS the creator.
    check("create_v2 creator_arg == curve creator",
          create_ix["fields"].get("creator_arg") == ATX_CREATOR)
    check("create_v2 flags all false/zero",
          create_ix["fields"].get("is_mayhem_mode") is False
          and create_ix["fields"].get("is_cashback_enabled") is False
          and create_ix["fields"].get("creator_fee_bps") == 0
          and create_ix["fields"].get("is_holder_reward") is False)
    check("create_v2 instruction verified",
          create_ix["validation"] == "verified")
    buy_ix = by_type(events, "instruction:buy_v2")
    check("buy_v2 args",
          buy_ix["fields"].get("base_amount_out") == 627550232570148
          and buy_ix["fields"].get("max_quote_amount_in") == 2 ** 64 - 1)
    ev = by_type(events, "event:CreateEvent")
    check("CreateEvent fields",
          ev["fields"].get("mint") == ATX_MINT
          and ev["fields"].get("bonding_curve") == ATX_CURVE
          and ev["fields"].get("creator") == ATX_CREATOR
          and ev["fields"].get("user") == ATX_CREATOR
          and ev["fields"].get("timestamp") == 1795273899
          and ev["fields"].get("virtual_token_reserves") == 1073000000000000
          and ev["fields"].get("virtual_sol_reserves") == 4292000000
          and ev["fields"].get("token_total_supply") == 1000000000000000
          and ev["fields"].get("quote_mint") == WSOL
          and ev["fields"].get("depth") == 0)
    check("CreateEvent verified", ev["validation"] == "verified")
    tr = by_type(events, "event:TradeEvent")
    check("TradeEvent create: token_amount == buy_v2 base_amount_out",
          tr["fields"].get("token_amount") ==
          buy_ix["fields"].get("base_amount_out"))
    check("TradeEvent create: is_buy, ix_name, quote_amount",
          tr["fields"].get("is_buy") is True
          and tr["fields"].get("ix_name") == "buy"
          and tr["fields"].get("quote_amount") == 6046575384
          and tr["fields"].get("trailing_u64") == 0)
    check("TradeEvent create verified", tr["validation"] == "verified")
    # First signer of the create tx is the creator.
    keys = tx["transaction"]["message"]["accountKeys"]
    first_signer = keys[0].get("pubkey") if isinstance(keys[0], dict) \
        else keys[0]
    check("create tx first signer == creator",
          first_signer == ATX_CREATOR == ev["fields"]["creator"])

    tx = load_fixture("atx_buy.json")
    sig = "2o4dRrK92ea1xLf68EaGwQpP2aSqVzPGdMTJuo6R5W2BX5CAjYi6h2Rqud8Vj2fcu2dVvPLoNS42tJtDHxywqxRx"
    events = pf.decode_transaction(tx, sig)
    check_envelope(events, sig, 472184744)
    check("atx_buy event set",
          types(events) == [
              "event:CloseUserVolumeAccumulatorEvent",
              "event:CompleteEvent", "event:PostCompleteBuyEvent",
              "event:TradeEvent",
              "instruction:buy_exact_quote_in_v3",
              "instruction:close_user_volume_accumulator"])
    bix = by_type(events, "instruction:buy_exact_quote_in_v3")
    check("buy_exact_quote_in_v3 args",
          bix["fields"].get("spendable_quote_in") == 34793403295
          and bix["fields"].get("min_tokens_out") == 1
          and bix["fields"].get("partial_fill") is True)
    tr = by_type(events, "event:TradeEvent")
    check("TradeEvent buy: ix_name, token_amount, quote_amount",
          tr["fields"].get("ix_name") == "buy_exact_quote_in"
          and tr["fields"].get("token_amount") == 157872506286524
          and tr["fields"].get("quote_amount") == 5933549346
          and tr["fields"].get("trailing_u64") == 0)
    ce = by_type(events, "event:CompleteEvent")
    check("CompleteEvent fields",
          ce["fields"].get("mint") == ATX_MINT
          and ce["fields"].get("bonding_curve") == ATX_CURVE
          and ce["fields"].get("timestamp") == 1795273922)
    pb = by_type(events, "event:PostCompleteBuyEvent")
    check("PostCompleteBuyEvent pool_after == create_pool amounts",
          pb["fields"].get("pool_base_reserves_after") == 61987996154038
          and pb["fields"].get("pool_quote_reserves_after") == 40591739160
          and pb["fields"].get("base_out") == 144912003845962
          and pb["fields"].get("quote_in") == 28430305758)
    cv = by_type(events, "event:CloseUserVolumeAccumulatorEvent")
    check("CloseUserVolumeAccumulatorEvent fields",
          cv["fields"].get("timestamp") == 1795273922
          and cv["fields"].get("total_unclaimed_tokens") == 0)
    check("atx_buy all verified",
          all(e["validation"] == "verified" for e in events))

    tx = load_fixture("atx_sell.json")
    sig = "3oLYGxBsTbC3oazfiyUAM18SW48NZXNB6EPH7qdDXvMHj2wR7KXyMdSUvXSc2tnqeqLx93cTuEyWZkuxsxdzsoJH"
    events = pf.decode_transaction(tx, sig)
    check_envelope(events, sig, 472184670)
    check("atx_sell event set",
          types(events) == ["event:TradeEvent", "instruction:sell_v3"])
    six = by_type(events, "instruction:sell_v3")
    check("sell_v3 args",
          six["fields"].get("base_amount_in") == 287908967496
          and six["fields"].get("min_quote_amount_out") == 1)
    tr = by_type(events, "event:TradeEvent")
    check("TradeEvent sell: is_buy False, token == sell arg",
          tr["fields"].get("is_buy") is False
          and tr["fields"].get("token_amount") == 287908967496
          and tr["fields"].get("ix_name") == "sell"
          and tr["fields"].get("user") ==
          "haqq6qnVBCZt97amh9VUFZRFCkD6NJxoYifacCuqqqq")
    check("atx_sell all verified",
          all(e["validation"] == "verified" for e in events))

    tx = load_fixture("atx_sell_failed.json")
    sig = "CdPBMqHSjSuCnnBLB7akZWDi73HGueweCSZh7fUi1oUiTXGMRaefmwDBdXeyutWs6Lzp5D8aD477Zc85HiG4ebN"
    events = pf.decode_transaction(tx, sig)
    check("failed sell yields only transaction_failed",
          len(events) == 1
          and events[0]["event_type"] == "transaction_failed"
          and events[0]["validation"] == "unverified")
    check("failed sell yields no trade events",
          not [e for e in events if "TradeEvent" in e["event_type"]])

    tx = load_fixture("atx_migrate.json")
    sig = "56zVpuKUKGWBodxDdnrknT3CPV1Zaq5gvhpJjg329dwiK6vF3KTPyVw2a8veVh6hu65nBX77NWRQ4BwRtPGaHDG8"
    events = pf.decode_transaction(tx, sig)
    check_envelope(events, sig, 472184748)
    check("atx_migrate (pumpfun) event set",
          types(events) == ["event:CompletePumpAmmMigrationEvent",
                            "instruction:migrate_v2"])
    mig = by_type(events, "event:CompletePumpAmmMigrationEvent")
    check("migration event fields",
          mig["fields"].get("mint") == ATX_MINT
          and mig["fields"].get("pool") == ATX_POOL
          and mig["fields"].get("mint_amount") == 61987996154038
          and mig["fields"].get("sol_amount") == 40591739160
          and mig["fields"].get("pool_migration_fee") == 15000001
          and mig["fields"].get("bonding_curve") == ATX_CURVE)
    check("migration event verified", mig["validation"] == "verified")
    check("canonical pool from migration (membership check)",
          pf.canonical_pool_from_migration(tx, events) == ATX_POOL)
    check("find_migration helper",
          pf.find_migration(events) is mig)


# ---------------------------------------------------------------------------
# 4. Pump.fun: NOTAI (second coin, SOL-quoted curve, buy_v3)
# ---------------------------------------------------------------------------
def test_pumpfun_notai():
    tx = load_fixture("notai_create.json")
    # Signatures live on the transaction object, not in the message;
    # decode without an explicit signature to exercise extraction.
    sig = (tx["transaction"].get("signatures") or [None])[0]
    events = pf.decode_transaction(tx)
    check("notai_create signature extracted from the tx",
          events and events[0]["signature"] == sig)
    check("notai_create slot extracted from the tx",
          events and events[0]["slot"] == 472082477)
    check("notai_create event set",
          types(events) == ["event:CreateEvent", "event:TradeEvent",
                            "instruction:buy_v3",
                            "instruction:create_v2"])
    cix = by_type(events, "instruction:create_v2")
    check("notai create_v2 creator_arg == creator",
          cix["fields"].get("creator_arg") == NOTAI_CREATOR)
    check("notai create_v2 name/symbol",
          cix["fields"].get("name") == "This Is Not AI"
          and cix["fields"].get("symbol") == "NOTAI")
    bix = by_type(events, "instruction:buy_v3")
    check("buy_v3 args",
          bix["fields"].get("base_amount_out") == 2467071716458
          and bix["fields"].get("max_quote_amount_in") == 77000000)
    tr = by_type(events, "event:TradeEvent")
    check("notai TradeEvent: SOL-quoted (sol_amount nonzero)",
          tr["fields"].get("sol_amount") == 69135801
          and tr["fields"].get("token_amount") == 2467071716458
          and tr["fields"].get("is_buy") is True)
    ev = by_type(events, "event:CreateEvent")
    check("notai CreateEvent mint/curve",
          ev["fields"].get("mint") == NOTAI_MINT
          and ev["fields"].get("creator") == NOTAI_CREATOR)
    # Second PDA test vector, derived from the fixture itself.
    check("derive_curve_pda(NOTAI mint) == CreateEvent curve",
          pf.derive_curve_pda(NOTAI_MINT) ==
          ev["fields"]["bonding_curve"])


# ---------------------------------------------------------------------------
# 5. PumpSwap: ATX migrate / sell / buy
# ---------------------------------------------------------------------------
def test_pumpswap_atx():
    tx = load_fixture("atx_migrate.json")
    sig = "56zVpuKUKGWBodxDdnrknT3CPV1Zaq5gvhpJjg329dwiK6vF3KTPyVw2a8veVh6hu65nBX77NWRQ4BwRtPGaHDG8"
    events = ps.decode_transaction(tx, sig)
    check_envelope(events, sig, 472184748)
    check("atx_migrate (pumpswap) event set",
          types(events) == ["event:CreatePoolEvent",
                            "event:InitBoostEvent",
                            "instruction:create_pool",
                            "instruction:init_boost"])
    cix = by_type(events, "instruction:create_pool")
    check("create_pool args",
          cix["fields"].get("index") == 0
          and cix["fields"].get("base_amount_in") == 61987996154038
          and cix["fields"].get("quote_amount_in") == 40591739160
          and cix["fields"].get("coin_creator") == ATX_CREATOR
          and cix["fields"].get("is_mayhem_mode") is False
          and cix["fields"].get("creator_fee_bps") == 0)
    check("create_pool named accounts",
          (cix["fields"].get("named_accounts") or {}).get("pool")
          == ATX_POOL
          and (cix["fields"].get("named_accounts") or {}).get(
              "pool_base_token_account")
          == "4P8j3pzDfVY797ffYFipPtRe7uu6DkpPvnj45LGUoVZP"
          and (cix["fields"].get("named_accounts") or {}).get(
              "pool_quote_token_account")
          == "AwvRfGX9WAz2E4dVRAJzw7ppHJmBh4Qxw5SXovzUBLNS")
    ev = by_type(events, "event:CreatePoolEvent")
    check("CreatePoolEvent fields",
          ev["fields"].get("pool") == ATX_POOL
          and ev["fields"].get("creator") == ATX_POOL_CREATOR
          and ev["fields"].get("base_mint") == ATX_MINT
          and ev["fields"].get("quote_mint") == WSOL
          and ev["fields"].get("lp_mint") ==
          "94wWNgKNtRuzeuLATaK7KVhvPKnnDTXqfCHVUD5gq8Vu"
          and ev["fields"].get("initial_liquidity") == 1586253627556
          and ev["fields"].get("lp_token_amount_out") == 1586253627456
          and ev["fields"].get("pool_bump") == 255
          and ev["fields"].get("coin_creator") == ATX_CREATOR)
    ib = by_type(events, "event:InitBoostEvent")
    check("InitBoostEvent fields",
          ib["fields"].get("mint") == ATX_MINT
          and ib["fields"].get("bonding_curve") == ATX_CURVE
          and ib["fields"].get("pool") == ATX_POOL
          and ib["fields"].get("virtual_quote_reserves") == 2516200570
          and ib["fields"].get("real_quote_reserves_after") == 38075538590)
    check("atx_migrate (pumpswap) all verified",
          all(e["validation"] == "verified" for e in events))

    tx = load_fixture("atx_pumpswap_sell.json")
    sig = "5eLyyz6ZR1PBGXVAHKWTk2jjg4GK1GdyBaTCPFUJ43BNBBPwWmN4WZbMrxcYyHM7JabNCd49SKoRNoLa6xuRWxVo"
    events = ps.decode_transaction(tx, sig)
    check_envelope(events, sig, 472474446)
    check("atx_pumpswap_sell event set",
          types(events) == ["event:SellEvent", "instruction:sell_v2"])
    six = by_type(events, "instruction:sell_v2")
    check("sell_v2 args",
          six["fields"].get("base_amount_in") == 390917841189
          and six["fields"].get("min_quote_amount_out") == 1)
    se = by_type(events, "event:SellEvent")
    check("SellEvent fields",
          se["fields"].get("base_amount_in") == 390917841189
          and se["fields"].get("quote_amount_out") == 387720869
          and se["fields"].get("user_quote_amount_out") == 383843659
          and se["fields"].get("pool") == ATX_POOL
          and se["fields"].get("coin_creator") == ATX_CREATOR
          and se["fields"].get("trailing_u64") == 0)
    check("SellEvent verified", se["validation"] == "verified")

    tx = load_fixture("atx_pumpswap_buy.json")
    sig = "2BVW5dm6uVn4xkdttb53YzkYxbcvwErixHL17w6v5jQhQR97QfxUFQj6RXEMSvY31FBTpL1UbsnZcFbnZDq5FuKz"
    events = ps.decode_transaction(tx, sig)
    check_envelope(events, sig, 472449694)
    check("atx_pumpswap_buy event set",
          types(events) == ["event:BuyEvent",
                            "instruction:buy_exact_quote_in_v2"])
    bix = by_type(events, "instruction:buy_exact_quote_in_v2")
    check("buy_exact_quote_in_v2 args",
          bix["fields"].get("spendable_quote_in") == 9010000
          and bix["fields"].get("min_base_amount_out") == 1)
    be = by_type(events, "event:BuyEvent")
    check("BuyEvent fields",
          be["fields"].get("quote_amount_in") == 9010000
          and be["fields"].get("base_amount_out") == 8926481617
          and be["fields"].get("ix_name") == "buy_exact_quote_in_v2"
          and be["fields"].get("pool") == ATX_POOL
          and be["fields"].get("coin_creator") == ATX_CREATOR
          and be["fields"].get("trailing_u64") == 0)
    check("BuyEvent verified", be["validation"] == "verified")


# ---------------------------------------------------------------------------
# 6. PumpSwap: NOTAI legacy sell / legacy buy / versioned sells
# ---------------------------------------------------------------------------
def test_pumpswap_notai():
    tx = load_fixture("notai_sell_legacy.json")
    events = ps.decode_transaction(tx)
    check("notai legacy sell signature extracted",
          events and events[0]["signature"] is not None
          and events[0]["slot"] == 472099146)
    check("notai legacy sell event set",
          types(events) == ["event:SellEvent", "instruction:sell"])
    six = by_type(events, "instruction:sell")
    check("legacy sell args",
          six["fields"].get("base_amount_in") == 805527491472
          and six["fields"].get("min_quote_amount_out") == 100)
    check("legacy sell instruction verified (fixture-observed)",
          six["validation"] == "verified")
    se = by_type(events, "event:SellEvent")
    check("legacy SellEvent base_in == sell arg",
          se["fields"].get("base_amount_in") == 805527491472)

    tx = load_fixture("notai_buy.json")
    events = ps.decode_transaction(tx)
    check("notai legacy buy event set",
          types(events) == ["event:BuyEvent",
                            "instruction:buy_exact_quote_in"])
    bix = by_type(events, "instruction:buy_exact_quote_in")
    check("legacy buy_exact_quote_in args",
          bix["fields"].get("spendable_quote_in") == 127504867
          and bix["fields"].get("min_base_amount_out") == 147829755621
          and bix["fields"].get("partial_fill") is True)
    be = by_type(events, "event:BuyEvent")
    check("legacy BuyEvent quote_in == buy arg, body 504 (trailing u64)",
          be["fields"].get("quote_amount_in") == 127504867
          and be["fields"].get("base_amount_out") == 163914309400
          and be["fields"].get("ix_name") == "buy_exact_quote_in"
          and be["fields"].get("trailing_u64") == 0)

    for name, want_ix in (("notai_sell_v0.json", "instruction:sell"),
                          ("notai_sell_v1.json", "instruction:sell_v2")):
        events = ps.decode_transaction(load_fixture(name))
        check("%s decodes on a versioned tx" % name,
              len(events) == 2
              and types(events) == ["event:SellEvent", want_ix]
              and all(e["validation"] == "verified" for e in events))


# ---------------------------------------------------------------------------
# 7. Account decoders + PDA derivation + cross-checks vs reserves.py
# ---------------------------------------------------------------------------
def test_accounts():
    acc = load_accounts()
    curve_raw = base64.b64decode(acc["curve"]["data_base64"])
    curve = pf.decode_bonding_curve(curve_raw)
    check("curve account verified", curve["validation"] == "verified")
    check("curve account fields",
          curve["creator"] == ATX_CREATOR
          and curve["quote_mint"] == WSOL
          and curve["token_total_supply"] == 1000000000000000
          and curve["complete"] is True
          and curve["virtual_token_reserves"] == 0
          and curve["virtual_quote_reserves"] == 0
          and curve["post_complete_base_out"] == 144912003845962
          and curve["post_complete_quote_in"] == 28430305758
          and curve["initial_virtual_quote_reserves"] == 4292000000)
    # Cross-check vs the existing in-repo parser on the same bytes.
    legacy = reserves.parse_bonding_curve(curve_raw)
    check("curve agrees with reserves.parse_bonding_curve",
          legacy == {"complete": True, "v_sol": 0, "v_token": 0}
          and legacy["complete"] == curve["complete"]
          and legacy["v_sol"] == curve["virtual_quote_reserves"]
          and legacy["v_token"] == curve["virtual_token_reserves"])
    check("is_graduated True for the migrated curve",
          pf.is_graduated(curve) is True)
    prog = pf.curve_progress(curve)
    check("curve_progress 100% at completion",
          prog["progress_pct"] == 100.0
          and prog["complete"] is True)
    check("derive_curve_pda(ATX mint) == ledger curve",
          pf.derive_curve_pda(ATX_MINT) == ATX_CURVE)

    pool_raw = base64.b64decode(acc["pool"]["data_base64"])
    pool = ps.decode_pool(pool_raw)
    check("pool account verified", pool["validation"] == "verified")
    check("pool account fields",
          pool["creator"] == ATX_POOL_CREATOR
          and pool["index"] == 0
          and pool["base_mint"] == ATX_MINT
          and pool["quote_mint"] == WSOL
          and pool["lp_mint"] ==
          "94wWNgKNtRuzeuLATaK7KVhvPKnnDTXqfCHVUD5gq8Vu"
          and pool["lp_supply"] == 1586253627556
          and pool["coin_creator"] == ATX_CREATOR
          and pool["trailing_bytes_hex"] == "00" * 14)
    check("pool vaults follow the IDL order (base @139, quote @171)",
          pool["pool_base_token_account"] ==
          "4P8j3pzDfVY797ffYFipPtRe7uu6DkpPvnj45LGUoVZP"
          and pool["pool_quote_token_account"] ==
          "AwvRfGX9WAz2E4dVRAJzw7ppHJmBh4Qxw5SXovzUBLNS")
    # Cross-check vs reserves.parse_pumpswap_vaults on the same bytes:
    # reserves.py labels the two offsets the other way round, so the
    # decoder's base vault is reserves' SECOND element and the
    # decoder's quote vault is reserves' FIRST element. The SET of
    # vaults must agree exactly.
    q_vault, b_vault = reserves.parse_pumpswap_vaults(pool_raw)
    check("pool vaults agree with reserves.py (as a set)",
          {q_vault, b_vault} == {
              pool["pool_base_token_account"],
              pool["pool_quote_token_account"]})
    check("reserves.py vault labels are swapped vs the IDL "
          "(documented finding)",
          q_vault == pool["pool_base_token_account"]
          and b_vault == pool["pool_quote_token_account"])
    check("derive_pool_pda == migrate pool",
          ps.derive_pool_pda(0, ATX_POOL_CREATOR, ATX_MINT, WSOL)
          == ATX_POOL)
    # Vault mints from the migrate tx postTokenBalances prove the
    # IDL order: base vault holds ATX, quote vault holds WSOL.
    tx = load_fixture("atx_migrate.json")
    keys = tx["transaction"]["message"]["accountKeys"]

    def key_of(i):
        k = keys[i]
        return k.get("pubkey") if isinstance(k, dict) else k

    balances = {}
    for bal in (tx.get("meta") or {}).get("postTokenBalances") or []:
        balances[key_of(bal["accountIndex"])] = bal.get("mint")
    check("base vault holds the ATX mint",
          balances.get(pool["pool_base_token_account"]) == ATX_MINT)
    check("quote vault holds WSOL",
          balances.get(pool["pool_quote_token_account"]) == WSOL)
    price = ps.pool_price_from_vaults(61987996154038, 38075538590)
    check("pool price from vault balances",
          price is not None
          and abs(price - 38075538590 / 61987996154038) < 1e-12)


# ---------------------------------------------------------------------------
# 8. Undecodable input: unknown, never an invented event
# ---------------------------------------------------------------------------
def test_unknowns():
    # Unknown instruction discriminator.
    dec = pf.decode_instruction(bytes([9] * 24))
    check("unknown pump instruction disc -> unknown",
          dec["instruction_name"] == "unknown"
          and dec["validation"] == "unknown")
    dec = ps.decode_instruction(bytes([9] * 24))
    check("unknown pumpswap instruction disc -> unknown",
          dec["instruction_name"] == "unknown"
          and dec["validation"] == "unknown")
    # Truncated instruction (buy_v2 needs 16 arg bytes).
    dec = pf.decode_instruction(
        bytes([184, 23, 238, 97, 103, 197, 211, 61]) + b"\x01\x02")
    check("truncated buy_v2 -> unverified, not invented",
          dec["instruction_name"] == "buy_v2"
          and dec["validation"] == "unverified")
    # Unknown event discriminator.
    ev = pf.decode_event(bytes([9] * 40))
    check("unknown pump event disc -> unknown",
          ev["event_name"] == "unknown"
          and ev["validation"] == "unknown")
    ev = ps.decode_event(bytes([9] * 40))
    check("unknown pumpswap event disc -> unknown",
          ev["event_name"] == "unknown"
          and ev["validation"] == "unknown")
    # Truncated TradeEvent body.
    ev = pf.decode_event(bytes(list(pf.EVENTS["TradeEvent"])) + b"\x00" * 10)
    check("truncated TradeEvent -> unverified",
          ev["event_name"] == "TradeEvent"
          and ev["validation"] == "unverified")
    # Non-pump transaction: no events at all.
    fake_tx = {
        "slot": 1,
        "blockTime": 1795273899,
        "meta": {"err": None, "innerInstructions": [], "logMessages": []},
        "transaction": {
            "message": {
                "signatures": ["x" * 88],
                "accountKeys": [
                    {"pubkey": "11111111111111111111111111111111",
                     "signer": True, "writable": True}],
                "instructions": [
                    {"programId": "11111111111111111111111111111111",
                     "parsed": {"type": "transfer"}, "accounts": [0]}],
            },
        },
    }
    check("non-pump tx yields no events (pumpfun)",
          pf.decode_transaction(fake_tx) == [])
    check("non-pump tx yields no events (pumpswap)",
          ps.decode_transaction(fake_tx) == [])
    # Failed tx with an error object: single marker, no trades.
    bad = json.loads(json.dumps(load_fixture("atx_sell.json")))
    bad["meta"]["err"] = {"InstructionError": [3, {"Custom": 6005}]}
    events = pf.decode_transaction(bad)
    check("err tx yields only transaction_failed (pumpfun)",
          len(events) == 1
          and events[0]["event_type"] == "transaction_failed")
    events = ps.decode_transaction(bad)
    check("err tx yields only transaction_failed (pumpswap)",
          len(events) == 1
          and events[0]["event_type"] == "transaction_failed")
    # Not a dict / garbage account data: unknown, no raise.
    check("decode_transaction(None) -> unknown entry",
          pf.decode_transaction(None)[0]["validation"] == "unknown")
    check("decode_bonding_curve garbage -> unknown",
          pf.decode_bonding_curve(b"garbage")["validation"] == "unknown")
    check("decode_bonding_curve wrong length -> unverified",
          pf.decode_bonding_curve(
              bytes(list(pf.BONDING_CURVE_DISC)) + b"\x00" * 10)
          ["validation"] == "unverified")
    check("decode_pool wrong disc -> unknown",
          ps.decode_pool(b"\x00" * 301)["validation"] == "unknown")
    # Legacy (unobserved) pump instruction decodes as unverified.
    raw = bytes(list(pf._ix_disc("buy"))) + struct.pack("<QQ", 100, 200)
    dec = pf.decode_instruction(raw)
    check("legacy pump buy decodes unverified by analogy",
          dec["instruction_name"] == "buy"
          and dec["validation"] == "unverified"
          and dec["args"].get("base_amount_out") == 100)
    raw = bytes(list(ps._ix_disc("buy"))) + struct.pack("<QQ", 100, 200)
    dec = ps.decode_instruction(raw)
    check("legacy AMM buy decodes unverified by analogy",
          dec["instruction_name"] == "buy"
          and dec["validation"] == "unverified")
    # Deposit/withdraw (IDL layout, no fixture) decode unverified.
    raw = bytes(list(ps.INSTRUCTIONS["deposit"][0])) + struct.pack(
        "<QQQ", 1000, 2000, 3000)
    dec = ps.decode_instruction(raw)
    check("deposit decodes unverified (IDL layout, no fixture)",
          dec["instruction_name"] == "deposit"
          and dec["validation"] == "unverified"
          and dec["args"].get("lp_token_amount_out") == 1000)
    # The newer BQFt bonding-curve program is out of scope: its
    # instructions must not be claimed by the pump decoder.
    check("BQFt program is not the pump program id",
          pf.PUMP_PROGRAM_ID_V2 != pf.PUMP_PROGRAM_ID)


# ---------------------------------------------------------------------------
# 9. Log-only fallback: events found from Program data logs when the
#    inner instruction data is absent.
# ---------------------------------------------------------------------------
def test_log_fallback():
    tx = json.loads(json.dumps(load_fixture("atx_buy.json")))
    tx["meta"]["innerInstructions"] = []
    events = pf.decode_transaction(tx)
    got = sorted(e["event_type"] for e in events)
    check("log fallback recovers all 4 events",
          got == ["event:CloseUserVolumeAccumulatorEvent",
                  "event:CompleteEvent", "event:PostCompleteBuyEvent",
                  "event:TradeEvent"])
    check("log fallback events are marked as log-sourced",
          all((e["instruction"] or {}).get("source") == "log"
              for e in events))
    check("log fallback events still verified",
          all(e["validation"] == "verified" for e in events))


def main():
    test_discriminators()
    test_pumpfun_atx()
    test_pumpfun_notai()
    test_pumpswap_atx()
    test_pumpswap_notai()
    test_accounts()
    test_unknowns()
    test_log_fallback()
    print()
    print("PASSED %d  FAILED %d" % (PASSES[0], len(FAILS)))
    if FAILS:
        print("FAILURES:")
        for name in FAILS:
            print("  - " + name)
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
