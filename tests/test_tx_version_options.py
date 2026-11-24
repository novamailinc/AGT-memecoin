"""Static guard: every production getTransaction/getBlock options literal
must accept current-chain transaction versions.

Causal evidence (2026-11-23, live keyed probe, evidence JSON preserved):
recent pump.fun transactions are now version 1; requesting them with
"maxSupportedTransactionVersion": 0 fails with RPC -32015 ("Transaction
version (1) is not supported by the requesting client") on Helius AND
Alchemy AND the public node. Before this fix 15 production sites
(launch_scanner/*, acquisition/probes.py) fetched with version-0
options, so each would misreport a real current-chain transaction as a
fetch failure / unavailable. After: zero version-0 literals remain.
The guard matches BOTH dict quoting styles (the first pass missed two
single-quoted sites in smartmoney.py / exit_copy.py, caught in review;
true pre-fix total was 15 sites).

Version 1 options are strictly more permissive (they still serve legacy
and v0 transactions; verified live: v0 txs answer fine under the v1
parameter), and the in-tree acquisition paths (chainstack_workload
TX_FETCH_OPTIONS, event_backfill, event_recovery, grad_select,
alchemy_workload, benchmark) already used 1.

This test is a source-level guard: it fails if any non-test production
file reintroduces a version-0 options literal.
"""
import pathlib
import re

TREE = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ("daemon", "broker")
PATTERN = re.compile(r'["\']maxSupportedTransactionVersion["\']\s*:\s*0\b')


def _violations():
    hits = []
    for d in SCAN_DIRS:
        for p in sorted((TREE / d).rglob("*.py")):
            if "/tests/" in str(p):
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if PATTERN.search(line):
                    hits.append("%s:%d: %s" % (p.relative_to(TREE), i,
                                               line.strip()))
    return hits


def test_no_version0_transaction_fetch_options_in_production():
    hits = _violations()
    assert not hits, (
        "version-0 getTransaction options reintroduced (fails current "
        "v1-chain txs with RPC -32015; both quote styles guarded):\n"
        + "\n".join(hits))


def test_control_pattern_matches_version0_literal():
    # Negative controls: the guard pattern must match the bad literal in
    # BOTH dict quoting styles (the single-quote shape is what evaded the
    # first pass) and must not match the fixed literal.
    assert PATTERN.search('"maxSupportedTransactionVersion": 0}])')
    assert PATTERN.search("'maxSupportedTransactionVersion':0}])")
    assert PATTERN.search("'maxSupportedTransactionVersion' : 0 }])")
    assert not PATTERN.search('"maxSupportedTransactionVersion": 1}])')
    assert not PATTERN.search("'maxSupportedTransactionVersion': 1}])")
