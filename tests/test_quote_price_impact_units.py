"""Unit contract for broker.memecoin.execution.jupiter.quote_price_impact_pct (SOURCE ONLY, WRITTEN NOT RUN). run: python3 tests/test_quote_price_impact_units.py
Jupiter documents quote priceImpactPct as a DECIMAL RATIO in [0, 1] (0.01 = 1%): https://developers.jup.ag/docs/guides/how-to-build-a-custom-swap-with-metis .
LABEL: these are parser-contract unit checks over documented-format inputs, NOT market evidence and NOT a live quote. They pin the unit conversion (ratio -> percent)
and the fail-closed refusals, and the executor cap comparison through the real SimulationExecutor on canned quotes (no network)."""
import os, sys, math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from broker import BrokerError
from broker.memecoin.execution import sim_executor
from broker.memecoin.execution.jupiter import quote_price_impact_pct
from broker.memecoin.execution.sim_executor import BalanceBook, SimulationExecutor

FAILS = 0


def check(name, ok, detail=""):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if not ok else ""), flush=True)
    if not ok:
        FAILS += 1


def reason_of(q):
    try:
        quote_price_impact_pct(q)
    except BrokerError as e:
        return (e.detail or {}).get("reason")
    return None


class FJ:
    def __init__(self, q): self._q = q
    def get_quote(self, *a, **k): return self._q


def exec_reason(impact, cap=2.0):
    ex = SimulationExecutor(jupiter=FJ({"outAmount": "2000000", "priceImpactPct": impact}), book=BalanceBook(10**9), seed=7)
    try:
        ex.execute_swap(sim_executor.SOL_MINT, "TokenMint1111111111111111111111111111111111", 10**7, price_impact_cap_pct=cap)
    except BrokerError as e:
        return (e.detail or {}).get("reason")
    return "filled"


def main():
    check("ratio 0.01 -> 1.0 percent", abs(quote_price_impact_pct({"priceImpactPct": "0.01"}) - 1.0) < 1e-12)
    check("ratio 0.0001 -> 0.01 percent", abs(quote_price_impact_pct({"priceImpactPct": "0.0001"}) - 0.01) < 1e-12)
    check("ratio 0 -> 0 percent", quote_price_impact_pct({"priceImpactPct": "0"}) == 0.0)
    check("ratio 1 -> 100 percent (documented total loss)", abs(quote_price_impact_pct({"priceImpactPct": "1"}) - 100.0) < 1e-9)
    check("numeric (not string) ratio accepted", abs(quote_price_impact_pct({"priceImpactPct": 0.004}) - 0.4) < 1e-12)
    for label, q in (("missing field", {"outAmount": "1"}), ("None", {"priceImpactPct": None}), ("non-dict quote", None), ("bool", {"priceImpactPct": True}),
                     ("text", {"priceImpactPct": "zz"}), ("NaN", {"priceImpactPct": "nan"}), ("inf", {"priceImpactPct": "inf"}),
                     ("negative", {"priceImpactPct": "-0.01"}), ("above documented range (1.5)", {"priceImpactPct": "1.5"}),
                     ("percent-form 5.0 contradicts the documented range", {"priceImpactPct": "5.0"})):
        check("fail closed (price_impact_unknown): " + label, reason_of(q) == "price_impact_unknown", reason_of(q))
    # executor cap comparison in PERCENT, through the real SimulationExecutor
    check("executor: ratio 0.004 (0.4%) under cap 2.0 fills", exec_reason("0.004") == "filled", exec_reason("0.004"))
    check("executor: ratio 0.02 (2.0%) equal to cap 2.0 is NOT over cap (strict >)", exec_reason("0.02") == "filled", exec_reason("0.02"))
    check("executor: ratio 0.0201 (2.01%) over cap 2.0 refuses price_impact_over_cap", exec_reason("0.0201") == "price_impact_over_cap", exec_reason("0.0201"))
    check("executor: ratio 0.05 (5%) over cap 2.0 refuses (old code compared 0.05 to 2.0 and would have filled)", exec_reason("0.05") == "price_impact_over_cap", exec_reason("0.05"))
    check("executor: ratio 0.5 (50%) refuses", exec_reason("0.5") == "price_impact_over_cap", exec_reason("0.5"))
    check("executor: percent-form 0.4 is now read as 40% and refused (the unit regression this change closes)", exec_reason("0.4") == "price_impact_over_cap", exec_reason("0.4"))
    check("executor: out-of-range 5.0 refuses price_impact_unknown", exec_reason("5.0") == "price_impact_unknown", exec_reason("5.0"))
    ex = SimulationExecutor(jupiter=FJ({"outAmount": "2000000", "priceImpactPct": "0.004"}), book=BalanceBook(10**9), seed=7)
    res = ex.execute_swap(sim_executor.SOL_MINT, "TokenMint1111111111111111111111111111111111", 10**7)
    check("result carries price_impact_pct in percent and the derived ratio", abs(res["price_impact_pct"] - 0.4) < 1e-9 and abs(res["price_impact_ratio"] - 0.004) < 1e-12, repr((res.get("price_impact_pct"), res.get("price_impact_ratio"))))
    print("FAILS: %d" % FAILS)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
