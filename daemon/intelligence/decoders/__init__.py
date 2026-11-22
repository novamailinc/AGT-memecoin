"""Independent Solana decoders (Phase 3a, 2026-11-22).

Pure-stdlib decoding of transactions, instructions and accounts
from standard Solana JSON-RPC responses (getTransaction jsonParsed
/ json / base64, getAccountInfo). No imports of the launch_scanner
monolith and no third-party packages: base58 is implemented locally
in _base58.py.

Every decoded event carries signature, slot, block time (when
available), program id, instruction info, relevant account
addresses, decoded event type, raw or referenced transaction data,
decoder version and validation status. Where a transaction cannot
be decoded confidently it is marked unknown/unverified — an event
is never invented.
"""
