"""Consumer regression for current-chain v1 transactions (2026-11-23).

Causal evidence: recent pump.fun transactions are now Solana transaction
VERSION 1; fetching them with maxSupportedTransactionVersion:0 fails with
RPC -32015 on Helius, Alchemy and the public node (live keyed census,
preserved in the delta package). The tree-wide fix bumps every
production getTransaction options literal to version 1.

This test proves the DOWNSTREAM consumers of those fetches handle the
real v1 response shape (or refuse it cleanly), using actual captured
mainnet fixtures (public chain data, sanitized: no credentials,
no user data):

- tests/fixtures/tx_v1_succeeded.json  (version 1, meta.err null)
- tests/fixtures/tx_v1_reverted.json   (version 1, meta.err set)
- tests/fixtures/tx_v0_succeeded.json  (version 0 parity control)

The v1 shape delta vs v0 is additive (message.transactionConfig;
loadedAddresses in meta already existed for v0 under "json" encoding);
every consumer path below uses version-agnostic fields
(signatures[0], meta.err, accountKeys[0] fee payer, parsed
instructions, pre/postTokenBalances).
"""
import json
import pathlib
import sys

TREE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TREE))

from daemon.launch_scanner import tx_guards as _txg  # noqa: E402
from daemon.launch_scanner.deployers import (  # noqa: E402
    _extract_funder, _first_signer)
from daemon.launch_scanner.funding_clusters import (  # noqa: E402
    parse_funder_from_tx)
from daemon.launch_scanner.exit_copy import _token_delta  # noqa: E402

FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


def _load(name):
    d = json.loads((FIX / name).read_text())
    return d["signature"], d["result"]


def test_fixture_is_genuinely_v1_shape():
    _, tx = _load("tx_v1_succeeded.json")
    assert tx.get("version") == 1
    msg = (tx.get("transaction") or {}).get("message") or {}
    # The additive v1-only field observed live on 2026-11-23.
    assert "transactionConfig" in msg


def test_v1_succeeded_parses_through_real_consumers():
    sig, tx = _load("tx_v1_succeeded.json")
    assert _txg.tx_succeeded(tx) is True
    assert _txg.response_signature_matches(tx, sig) is True
    # Wrong-signature binding must still refuse.
    assert _txg.response_signature_matches(tx, "1" * 87) is False
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    payer = (keys[0] or {}).get("pubkey") if keys and isinstance(
        keys[0], dict) else (keys[0] if keys else None)
    assert _first_signer(tx) == payer
    wallet = payer
    # Pure parsers must not raise on the v1 shape; results may be None
    # when the fixture contains no qualifying transfer.
    _extract_funder(tx, wallet)
    parse_funder_from_tx(tx, wallet)
    assert isinstance(_token_delta(tx, wallet, "So11111111111111111111111111111111111111112"), float)


def test_v1_reverted_is_refused_by_the_meta_err_gate():
    sig, tx = _load("tx_v1_reverted.json")
    assert tx.get("version") == 1
    assert _txg.tx_succeeded(tx) is False
    # Sig binding is independent of success (identification, not trust).
    assert _txg.response_signature_matches(tx, sig) is True


def test_v0_parity_control():
    sig, tx = _load("tx_v0_succeeded.json")
    assert tx.get("version") == 0
    # Genuine success parity: the fixture must be a SUCCEEDED v0 tx
    # (the first capture was mislabeled: meta.err Custom 6002 = reverted;
    # caught in review, fixture replaced from live).
    assert (tx.get("meta") or {}).get("err") is None
    assert _txg.tx_succeeded(tx) is True
    assert _txg.response_signature_matches(tx, sig) is True
    assert _first_signer(tx) is not None
