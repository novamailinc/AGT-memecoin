#!/usr/bin/env python3
"""Shared helpers for the snapshot harness (tests/test_snapshots.py).

Only pure helpers belong here. Importing this module pulls in
NOTHING from the bot (stdlib only at module level): the helpers
that build bot types import them lazily, inside the function,
after the caller has put the repo on sys.path — so the harness
stays importable even when the bot is broken (the
pumpfun-bonkfun-bot _fakes.py rule).

Contents: fixture loading, the fake Helius client (serves
recorded responses by method), _first_difference (first diff
path, like the reference), and snapshot load/save.
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
SNAPSHOTS = os.path.join(HERE, "snapshots")


def load_fixture(name):
    """Parsed JSON fixture by filename."""
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


def load_snapshot(name):
    """Parsed committed snapshot by filename, or None."""
    path = os.path.join(SNAPSHOTS, name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_snapshot(name, data):
    """Write a snapshot (sorted keys, 2-space indent)."""
    os.makedirs(SNAPSHOTS, exist_ok=True)
    path = os.path.join(SNAPSHOTS, name)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    return path


def first_difference(a, b, path=""):
    """First path where a and b differ, or None when equal.

    Reports the shallowest, leftmost difference ("parsers[0].x:
    got 1, want 2") so a snapshot mismatch is diagnosable from
    one line instead of a wall of JSON. Dict keys are compared
    sorted; lists element-wise; anything else by !=."""
    if type(a) is not type(b):
        # int/float cross-compare numerically; everything else
        # differing in type is a difference.
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if float(a) != float(b):
                return "%s: got %r, want %r" % (path or "$", a, b)
            return None
        return "%s: got %s, want %s" % (
            path or "$", type(a).__name__, type(b).__name__)
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            sub = "%s.%s" % (path, key) if path else str(key)
            if key not in a:
                return "%s: missing (want %r)" % (sub, b[key])
            if key not in b:
                return "%s: extra (got %r)" % (sub, a[key])
            diff = first_difference(a[key], b[key], sub)
            if diff is not None:
                return diff
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return "%s: length got %d, want %d" % (
                path or "$", len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            diff = first_difference(x, y, "%s[%d]" % (path, i))
            if diff is not None:
                return diff
        return None
    if a != b:
        return "%s: got %r, want %r" % (path or "$", a, b)
    return None


class FakeReserves:
    """A reserves module stand-in serving recorded responses.

    rpc(method, params) returns the fixture for that method, and
    records every call (method + params) so the harness can
    assert the call budget (e.g. exactly one signatures call).
    derive_curve_pda is a stub: the fixtures carry explicit
    curve addresses. Never touches the network."""

    def __init__(self, by_method):
        self.by_method = dict(by_method)
        self.calls = []

    def rpc(self, method, params=None, **kw):
        self.calls.append((method, params))
        if method not in self.by_method:
            raise AssertionError(
                "FakeReserves: no fixture for %s" % method)
        return self.by_method[method]

    def derive_curve_pda(self, mint):
        return "Curve11111111111111111111111111111111111"


def parse_new_token_fixture():
    """Parse the recorded new-token payload (lazy bot import)."""
    from daemon.launch_scanner import pumpportal
    return pumpportal.parse_new_token(
        load_fixture("pumpportal_new_token.json"))


def parse_migration_fixture():
    """Parse the recorded migration payload (lazy bot import)."""
    from daemon.launch_scanner import pumpportal
    return pumpportal.parse_migration(
        load_fixture("pumpportal_migration.json"))


def curve_stats_fixture():
    """_curve_stats over the recorded signatures page.

    Returns (stats, calls): the stats dict plus the RPC calls
    the fake served (assert the one-call budget from these)."""
    from daemon.launch_scanner import grad_select as gs
    fake = FakeReserves({
        "getSignaturesForAddress":
            load_fixture("helius_signatures_page.json"),
    })
    old = gs._reserves
    gs._reserves = fake
    try:
        stats = gs._curve_stats(
            "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsg")
    finally:
        gs._reserves = old
    return stats, list(fake.calls)


def creator_and_tx_price_fixture():
    """_creator_from_oldest_tx + _ff_parse_tx_price over the
    recorded migration tx. Returns (creator, info, why, calls)."""
    from daemon.launch_scanner import grad_raw as gr
    from daemon.launch_scanner import grad_select as gs
    tx = load_fixture("helius_migration_tx.json")
    page = load_fixture("helius_signatures_page.json")
    oldest_sig = str((page[-1] or {}).get("signature") or "")
    fake = FakeReserves({"getTransaction": tx})
    old = gs._reserves
    gs._reserves = fake
    try:
        creator = gs._creator_from_oldest_tx(oldest_sig)
    finally:
        gs._reserves = old
    mint = "H8DX2KJNV3c9Dg4L7sZ85qS5Mr9ZZ4y36tgsEFMopump"
    info, why = gr._ff_parse_tx_price(tx, mint, sol_usd=70.0)
    return creator, info, why, list(fake.calls)
