"""Single authoritative source for Solana program IDs and mint addresses.

Phase 2 (Master Engineering Directive, D-01/D-04/D-05): the same
constants were defined independently in a dozen modules, and two of
them were WRONG — ``reserves.py`` and ``funding_clusters.py`` carried
a wrong Token-2022 program ID (``...QZWM1GjTbv...``), and both Pump
decoders carried the USDC mint as ``WSOL_MINT``. Every module that
needs one of these values MUST import it from here and MUST NOT
redefine the literal. The regression suite ``tests/test_solana_ids.py``
asserts each value against the known-correct address and asserts
every importing module exposes the identical value.

Verified values (mainnet):
- Token-2022 program: ``TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb``
  (the value that used to live in reserves.py/funding_clusters.py
  was a transposed-middle variant of this address — never use it).
- wSOL mint: ``So11111111111111111111111111111111111111112``.
- USDC mint: ``EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`` — this is
  USDC, NOT wSOL; the decoders used to label it ``WSOL_MINT``.

Pure constants, stdlib-free: importing this module has no side
effects and never triggers the launch_scanner package ``__init__``.
"""

SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ASSOCIATED_TOKEN_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
ADDRESS_LOOKUP_TABLE_PROGRAM_ID = "AddressLookupTab1e1111111111111111111111111"
COMPUTE_BUDGET_PROGRAM_ID = "ComputeBudget111111111111111111111111111111"
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPSWAP_PROGRAM_ID = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
