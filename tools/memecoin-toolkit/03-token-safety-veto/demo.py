"""Scenario walkthrough for the token-safety veto lab. Synthetic only."""
from veto import TokenReport, evaluate

WALLET = "SyntheticWallet1111111111111111111111111111"


def show(label, report):
    v = evaluate(report)
    print(f"{label}: status={v.status} passed={v.passed} score={v.score} "
          f"fails={v.fails} flags={v.flags} source={v.source}")


def main() -> None:
    print("=== Token safety veto lab (synthetic reports) ===")
    print("Rule: score is a floor, never a ranker. Any hard FAIL blocks.\n")

    show("Clean token (authorities revoked, no extensions)",
         TokenReport(mint_authority=None, freeze_authority="revoked",
                     extensions=[], transfer_fee_bps=0))
    show("Benign flag only (2% transfer fee)",
         TokenReport(mint_authority=None, freeze_authority=None,
                     extensions=[], transfer_fee_bps=200))
    show("Mint authority still held by a wallet",
         TokenReport(mint_authority=WALLET, freeze_authority=None,
                     extensions=[], transfer_fee_bps=0))
    show("Token-2022 trap bundle (hook + permanent delegate)",
         TokenReport(mint_authority=None, freeze_authority=None,
                     extensions=["transferHook", "permanentDelegate"],
                     transfer_fee_bps=0))
    show("Fee boundary: exactly 1000 bps (10%)",
         TokenReport(extensions=[], transfer_fee_bps=1000))
    show("Fee boundary: 1001 bps",
         TokenReport(extensions=[], transfer_fee_bps=1001))
    show("Frozen by default",
         TokenReport(extensions=["defaultAccountState:frozen"],
                     transfer_fee_bps=0))
    show("Report never fetched (extensions/fee missing)",
         {"mint_authority": None, "freeze_authority": None})
    show("No report at all", None)


if __name__ == "__main__":
    main()
