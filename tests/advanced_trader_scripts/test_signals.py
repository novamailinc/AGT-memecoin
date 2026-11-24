"""Tests for signals.py — run: python3 tests/test_signals.py"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.signals import (
    AlchemySignalAdapter,
    ChainstackSignalAdapter,
    HeliusSignalAdapter,
    SignalAggregator,
)


def test_helius_normalizes_valid_payload():
    sig = HeliusSignalAdapter().normalize(
        {"mint": "MINT1", "deployer": "DEP1", "priceSol": 0.001}, 1000.0
    )
    assert sig is not None
    assert sig.token == "MINT1" and sig.source == "helius"
    assert sig.price == 0.001 and sig.received_ts == 1000.0
    print("PASS test_helius_normalizes_valid_payload")


def test_chainstack_and_alchemy_shapes():
    sig = ChainstackSignalAdapter().normalize(
        {"token": "MINT2", "deployer": "DEP2", "price": 0.002}, 1001.0
    )
    assert sig is not None and sig.source == "chainstack"
    sig = AlchemySignalAdapter().normalize(
        {"mint": "MINT3", "creator": "DEP3", "price": 0.003}, 1002.0
    )
    assert sig is not None and sig.source == "alchemy" and sig.deployer == "DEP3"
    print("PASS test_chainstack_and_alchemy_shapes")


def test_adapters_fail_closed():
    for adapter in (
        HeliusSignalAdapter(),
        ChainstackSignalAdapter(),
        AlchemySignalAdapter(),
    ):
        assert adapter.normalize({}, 1000.0) is None  # missing fields
        assert adapter.normalize("not a dict", 1000.0) is None
        assert adapter.normalize({"mint": "X", "price": 0}, 1000.0) is None
    # Helius needs mint+priceSol; a Chainstack-shaped payload fails it.
    assert (
        HeliusSignalAdapter().normalize(
            {"token": "MINT2", "price": 0.002}, 1000.0
        )
        is None
    )
    print("PASS test_adapters_fail_closed")


def test_aggregator_corroboration():
    agg = SignalAggregator()
    h = HeliusSignalAdapter().normalize(
        {"mint": "TOK", "deployer": "D", "priceSol": 0.001}, 1000.0
    )
    c = ChainstackSignalAdapter().normalize(
        {"token": "TOK", "deployer": "D", "price": 0.001}, 1002.0
    )
    assert agg.add(h) == 1
    count, sources = agg.corroboration("TOK", 1003.0)
    assert count == 1 and sources == {"helius"}
    assert agg.add(c) == 2
    count, sources = agg.corroboration("TOK", 1003.0)
    assert count == 2 and sources == {"helius", "chainstack"}
    # Same provider twice does not count as corroboration.
    h2 = HeliusSignalAdapter().normalize(
        {"mint": "TOK2", "deployer": "D", "priceSol": 0.001}, 1003.0
    )
    agg.add(h2)
    agg.add(h2)
    count, _ = agg.corroboration("TOK2", 1003.0)
    assert count == 1
    print("PASS test_aggregator_corroboration")


def test_aggregator_window_and_failover_view():
    agg = SignalAggregator()  # default window 5s
    h = HeliusSignalAdapter().normalize(
        {"mint": "TOK", "deployer": "D", "priceSol": 0.001}, 1000.0
    )
    agg.add(h)
    assert agg.sources_seen(1002.0) == {"helius"}
    assert agg.sources_seen(1006.0) == set()  # window expired: provider silent
    assert agg.latest("TOK", 1006.0) is None
    agg.prune(1006.0)
    assert agg.corroboration("TOK", 1006.0) == (0, set())
    print("PASS test_aggregator_window_and_failover_view")


def test_no_api_keys_in_code():
    # Adapters expose endpoint env-var names only; no key material.
    for adapter in (
        HeliusSignalAdapter,
        ChainstackSignalAdapter,
        AlchemySignalAdapter,
    ):
        assert adapter.env_var.endswith("_RPC_URL")
        assert "KEY" not in adapter.env_var
    print("PASS test_no_api_keys_in_code")


if __name__ == "__main__":
    test_helius_normalizes_valid_payload()
    test_chainstack_and_alchemy_shapes()
    test_adapters_fail_closed()
    test_aggregator_corroboration()
    test_aggregator_window_and_failover_view()
    test_no_api_keys_in_code()
    print("test_signals.py: ALL PASS")
