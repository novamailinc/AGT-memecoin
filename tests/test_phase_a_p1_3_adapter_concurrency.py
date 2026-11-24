#!/usr/bin/env python3
"""Phase A regression (P1-3, 2026-11-22): two processes submitting
the same idempotency key to the paper adapter at the same time must
produce exactly one fill and one ticket. Before the fix the adapter
had no inter-process lock and no fsync (fixed shared `.tmp` name),
so two processes could load the same state, both fill, and the
second save silently dropped the first's fill. Runs 10 trials.
No network: deterministic in-process market-data double.
"""
import json
import multiprocessing as mp
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from broker import OrderRequest
from broker.memecoin.adapter import MemecoinAdapter
from broker.memecoin.transport import MemecoinMarketData

_PASSED = 0
TRIALS = 10
_MINTS = {"BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


class _FakeMarket(MemecoinMarketData):
    """Deterministic market-data double: USD prices, no network
    (same shape as test_broker_memecoin.FakeMarket)."""

    def __init__(self, usd_prices=None, symbols=None):
        super().__init__(symbols)
        self.usd = dict(usd_prices or {"SOL": 100.0})

    def get_quote(self, symbol):
        if not self.mint_for(symbol):
            return None
        px = self.usd.get(symbol.upper())
        if px is None:
            return None
        return {"symbol": symbol.upper(), "mint": "fake-mint",
                "price_usd": px, "price_sol": None,
                "pair": None, "ts": None}

    def get_candles(self, symbol, timeframe, count):
        return []


def _child(data_dir, barrier, out_q):
    try:
        market = _FakeMarket({"SOL": 100.0, "BONK": 0.0001})
        ad = MemecoinAdapter(data_dir=data_dir, mode="paper",
                             market_data=market, symbols=dict(_MINTS))
        ad.connect({})
        barrier.wait(timeout=30)
        res = ad.submit_order(OrderRequest(
            symbol="BONK", direction="BUY", volume=1.0,
            stop_loss=0.5, take_profit=2.0, signal_id="sig-race",
            idempotency_key="race-key"))
        out_q.put({"ticket": res.ticket, "error": None})
    except Exception as e:  # noqa: BLE001 - report to parent
        out_q.put({"ticket": None,
                   "error": "%s: %s" % (type(e).__name__, e)})


def _trial(n):
    tmp = tempfile.mkdtemp(prefix="phase_a_p1_3_")
    data_dir = os.path.join(tmp, "memecoin")
    ctx = mp.get_context("fork")
    barrier = ctx.Barrier(2)
    out_q = ctx.Queue()
    procs = [ctx.Process(target=_child, args=(data_dir, barrier, out_q))
             for _ in range(2)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
    alive = [p for p in procs if p.is_alive()]
    for p in alive:
        p.terminate()
    assert not alive, "trial %d: child hung" % n
    results = [out_q.get(timeout=10) for _ in range(2)]
    errors = [r["error"] for r in results if r["error"]]
    assert not errors, "trial %d: child errors %s" % (n, errors)
    tickets = sorted(r["ticket"] for r in results)
    assert tickets[0] == tickets[1], (
        "trial %d: tickets differ %s (double fill)" % (n, tickets))
    buys = []
    with open(os.path.join(data_dir, "fills.jsonl")) as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("kind") == "BUY":
                buys.append(d)
    assert len(buys) == 1, "trial %d: %d BUY fills" % (n, len(buys))
    with open(os.path.join(data_dir, "paper_state.json")) as fh:
        state = json.load(fh)
    assert state["next_ticket"] == 2, (
        "trial %d: next_ticket %s" % (n, state["next_ticket"]))
    assert len(state["positions"]) == 1, (
        "trial %d: %d positions" % (n, len(state["positions"])))
    assert abs(state["balance_sol"] - 9.0) < 1e-9, (
        "trial %d: balance %s" % (n, state["balance_sol"]))
    with open(os.path.join(data_dir, "idempotency.json")) as fh:
        idem = json.load(fh)
    assert list(idem) == ["race-key"], (
        "trial %d: idempotency keys %s" % (n, list(idem)))
    return tickets[0]


def main():
    for n in range(1, TRIALS + 1):
        ticket = _trial(n)
        check("trial %d: two concurrent same-key submits -> one fill, "
              "ticket %s" % (n, ticket), True)
    print("ALL %d CHECKS PASSED" % _PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
