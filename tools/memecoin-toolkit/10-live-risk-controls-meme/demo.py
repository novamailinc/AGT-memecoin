"""Demo walkthrough for risk.py -- run: python demo.py"""

import tempfile
from pathlib import Path

from risk import Bankroll, KillSwitch, cost_gate


def main() -> None:
    print("=== Live risk controls demo (synthetic, paper only) ===")

    print("\n-- Cost gate at $1 notional, worst case = 4 transactions --")
    for chain in ("solana", "base", "ethereum", "hyperliquid"):
        allowed, cost, pct = cost_gate(chain, 1.0)
        verdict = "ALLOWED" if allowed else "BLOCKED"
        print(f"{chain:<12} cost ${cost:.4f} = {pct:.2f}% of $1 -> {verdict}")

    print("\n-- Kill switch: 15 losing paper closes --")
    with tempfile.TemporaryDirectory() as tmp:
        ks = KillSwitch(state_path=Path(tmp) / "killswitch.json")
        print(f"fresh state file missing -> halted={ks.is_halted()} (fail-closed)")
        ks.rearm("demo-operator", "first boot, state reviewed")
        print(f"after human re-arm -> halted={ks.is_halted()}")
        for i in range(1, 16):
            halted = ks.record_close(0.5)
            if i in (14, 15):
                print(f"close {i}: realized 0.5x -> halted={halted}")
        print(f"expectancy after 15 closes: {ks.expectancy():+.2f}")
        try:
            ks.rearm("", "no operator")
        except ValueError as exc:
            print(f"anonymous re-arm refused: {exc}")
        ks.rearm("demo-operator", "ledger reviewed, lane probation")
        print(f"after named re-arm -> halted={ks.is_halted()}")

    print("\n-- Bankroll: $100, $1 shots, no top-up, no reset --")
    bank = Bankroll()
    bank.open_position()
    bank.settle(3.0)
    print(f"one 3.0x winner: cash ${bank.cash:.2f}, state={bank.state}")
    for _ in range(10):
        bank.open_position()
    print(f"10 shots open at once: cash ${bank.cash:.2f}, open={bank.open_count}")
    try:
        bank.open_position()
    except RuntimeError as exc:
        print(f"11th shot refused: {exc}")
    while bank.open_count:
        bank.settle(0.0)
    print(f"all 10 lost: cash ${bank.cash:.2f}, state={bank.state}")
    print(f"has top_up={hasattr(bank, 'top_up')}, has reset={hasattr(bank, 'reset')}")
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
