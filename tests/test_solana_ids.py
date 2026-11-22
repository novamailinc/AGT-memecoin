#!/usr/bin/env python3
"""Phase 2 regression tests — canonical Solana program IDs / mints
(D-01, D-04, D-05).

daemon/solana_ids.py is the ONE authoritative definition of the
Token program, Token-2022 program, wSOL mint and USDC mint. Every
module that needs one of these values must import it from there.
This suite pins each value against the known-correct address, pins
every importing module to the identical value, and scans the whole
repo tree to prove the wrong values are gone:

- wrong Token-2022 ID ``TokenzQdBNbLQZWM1GjTbvYDbPknHBzqALDFpHHFmDWd``
  (used to live in reserves.py:72 and funding_clusters.py:104) must
  appear in NO .py file anywhere (the needle itself lives only in
  this test file, which the scan excludes).
- the USDC mint must never again be labelled WSOL_MINT.

Plain script — run directly with the venv python, not pytest.
"""
import os
import re
import sys

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, REPO)

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


from daemon import solana_ids as S

# -- canonical values pinned against the known-correct addresses --
check("TOKEN2022_PROGRAM_ID is the real Token-2022 program",
      S.TOKEN2022_PROGRAM_ID
      == "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
check("TOKEN_PROGRAM_ID is the SPL token program",
      S.TOKEN_PROGRAM_ID
      == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
check("WSOL_MINT is wrapped SOL, not USDC",
      S.WSOL_MINT == "So11111111111111111111111111111111111111112")
check("USDC_MINT is the USDC mint",
      S.USDC_MINT == "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
check("WSOL_MINT != USDC_MINT (the D-04 confusion, pinned)",
      S.WSOL_MINT != S.USDC_MINT)
check("PUMP_PROGRAM_ID is the pump.fun program",
      S.PUMP_PROGRAM_ID == "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
check("PUMPSWAP_PROGRAM_ID is the PumpSwap program",
      S.PUMPSWAP_PROGRAM_ID
      == "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA")
check("SYSTEM_PROGRAM_ID is the system program",
      S.SYSTEM_PROGRAM_ID == "11111111111111111111111111111111")
check("ASSOCIATED_TOKEN_PROGRAM_ID is the ATA program",
      S.ASSOCIATED_TOKEN_PROGRAM_ID
      == "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")

# -- every importing module exposes the identical value ------------
from daemon.launch_scanner import reserves as R
check("reserves.TOKEN2022_PROGRAM is the correct ID (D-01, live path)",
      R.TOKEN2022_PROGRAM == S.TOKEN2022_PROGRAM_ID)
check("reserves.WSOL is wSOL",
      R.WSOL == S.WSOL_MINT)
check("reserves.TOKEN_PROGRAM is the SPL token program",
      R.TOKEN_PROGRAM == S.TOKEN_PROGRAM_ID)

from daemon.launch_scanner import funding_clusters as FC
check("funding_clusters excludes the REAL Token-2022 program (D-05)",
      S.TOKEN2022_PROGRAM_ID in FC._NON_ACTORS)
check("funding_clusters excludes the SPL token program",
      S.TOKEN_PROGRAM_ID in FC._NON_ACTORS)

from daemon.launch_scanner import instant_safety as INS
check("instant_safety.TOKEN2022_PROGRAM is the correct ID",
      INS.TOKEN2022_PROGRAM == S.TOKEN2022_PROGRAM_ID)

from daemon.intelligence.decoders import pumpfun_decoder as PF
from daemon.intelligence.decoders import pumpswap_decoder as PS
check("pumpfun_decoder.WSOL_MINT is wSOL, not USDC (D-04)",
      PF.WSOL_MINT == S.WSOL_MINT)
check("pumpfun_decoder.USDC_MINT is USDC",
      PF.USDC_MINT == S.USDC_MINT)
check("pumpswap_decoder.WSOL_MINT is wSOL, not USDC (D-04)",
      PS.WSOL_MINT == S.WSOL_MINT)
check("pumpswap_decoder.USDC_MINT is USDC",
      PS.USDC_MINT == S.USDC_MINT)
check("both decoders agree on the Token-2022 program",
      PF.TOKEN_2022_PROGRAM == PS.TOKEN_2022_PROGRAM
      == S.TOKEN2022_PROGRAM_ID)

from daemon.intelligence.decoders import instruction_decoder as ID
from daemon.intelligence.decoders import token_decoder as TD
from daemon.intelligence.decoders import account_decoder as AD
check("instruction_decoder Token-2022 ID matches canonical",
      ID.TOKEN_2022_PROGRAM_ID == S.TOKEN2022_PROGRAM_ID)
check("token_decoder Token-2022 ID matches canonical",
      TD.TOKEN_2022_PROGRAM_ID == S.TOKEN2022_PROGRAM_ID)
check("account_decoder Token-2022 ID matches canonical",
      AD.TOKEN_2022_PROGRAM_ID == S.TOKEN2022_PROGRAM_ID)

from daemon.intelligence.acquisition import helius_streams as HS
from daemon.intelligence.acquisition import websocket_manager as WM
check("helius_streams Token-2022 ID matches canonical",
      HS.TOKEN_2022_PROGRAM_ID == S.TOKEN2022_PROGRAM_ID)
check("websocket_manager program IDs match canonical",
      WM.PUMP_PROGRAM_ID == S.PUMP_PROGRAM_ID
      and WM.PUMPSWAP_PROGRAM_ID == S.PUMPSWAP_PROGRAM_ID
      and WM.TOKEN_PROGRAM_ID == S.TOKEN_PROGRAM_ID)

from daemon.launch_scanner import logstream as LS
check("logstream.WSOL_MINT is wSOL",
      LS.WSOL_MINT == S.WSOL_MINT)

from broker.memecoin.execution import tx as TX
from broker.memecoin.execution import mainnet_executor as ME
from broker.memecoin import adapter as ADAP
check("tx allowlist Token-2022 ID matches canonical",
      TX._TOKEN_2022_PROGRAM_ID == S.TOKEN2022_PROGRAM_ID)
check("mainnet_executor SOL/USDC mints match canonical",
      ME.SOL_MINT == S.WSOL_MINT and ME.USDC_MINT == S.USDC_MINT)
check("adapter SOL_MINT is wSOL",
      ADAP.SOL_MINT == S.WSOL_MINT)

# -- repo-wide scan: the wrong values must be gone ------------------
WRONG_T2022 = "TokenzQdBNbLQZWM1GjTbvYDbPknHBzqALDFpHHFmDWd"
WSOL_AS_USDC = re.compile(r'WSOL_MINT\s*=\s*"EPjFWdd5')
hits_wrong = []
hits_wsol_usdc = []
for root, dirs, files in os.walk(REPO):
    dirs[:] = [d for d in dirs
               if d not in ("__pycache__", ".venv", ".git")]
    for fn in files:
        if not fn.endswith(".py"):
            continue
        path = os.path.join(root, fn)
        # realpath: the suite may itself be reached via a symlink
        # (e.g. run-test-suites --dir with a symlinked tree), so
        # compare resolved paths, not the spelling used to invoke it.
        if os.path.realpath(path) == os.path.realpath(__file__):
            continue  # the needle itself lives in this test file
        try:
            with open(path, "r", encoding="utf-8",
                      errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        if WRONG_T2022 in text:
            hits_wrong.append(os.path.relpath(path, REPO))
        if WSOL_AS_USDC.search(text):
            hits_wsol_usdc.append(os.path.relpath(path, REPO))
check("wrong Token-2022 ID appears in no .py file",
      not hits_wrong)
if hits_wrong:
    print("  still present in: %s" % ", ".join(hits_wrong))
check("USDC mint is never again labelled WSOL_MINT",
      not hits_wsol_usdc)
if hits_wsol_usdc:
    print("  still present in: %s" % ", ".join(hits_wsol_usdc))

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
