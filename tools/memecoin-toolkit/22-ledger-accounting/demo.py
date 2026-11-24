"""Scenario walkthrough for the ledger-accounting lab. Synthetic only."""
import tempfile
from pathlib import Path

from ledger import (CorruptLedgerError, DuplicateEntryError, Ledger,
                    LockHeldError, OrderingError)


def main() -> None:
    print("=== Ledger accounting lab (synthetic JSON paper ledger) ===")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        ledger = Ledger(path)
        print(f"Fresh ledger at temp path: {len(ledger)} entries")

        print("\n-- Recording a round trip --")
        ledger.add_and_save("T-001", "BUY", 1000, amount=1.0, price=0.50)
        ledger.add_and_save("T-001", "CLOSE", 1600, amount=1.0, price=1.50)
        ledger.add_and_save("T-002", "BUY", 2000, amount=1.0, price=0.25)
        print(f"Entries on disk after reload: {len(Ledger(path))}")
        for entry in Ledger(path).entries:
            print(f"  {entry['ticket']} {entry['kind']} ts={entry['ts']} "
                  f"price={entry['price']}")

        print("\n-- Duplicate rejection (idempotency) --")
        try:
            ledger.add_entry("T-001", "BUY", 1100, amount=1.0)
        except DuplicateEntryError as exc:
            print(f"Duplicate correctly rejected: {exc}")

        print("\n-- Ordering rejection --")
        try:
            ledger.add_entry("T-002", "CLOSE", 1500, amount=1.0)
        except OrderingError as exc:
            print(f"CLOSE-before-BUY correctly rejected: {exc}")

        print("\n-- Lock discipline --")
        with ledger.locked():
            print(f"Lock held inside block: is_locked={ledger.is_locked()}")
            try:
                with Ledger(path).locked():
                    pass
            except LockHeldError:
                print("Second writer correctly blocked: LockHeldError")
        print(f"Lock released after block: is_locked={ledger.is_locked()}")

        print("\n-- Torn-write recovery --")
        good = Ledger(path)
        good.add_and_save("T-003", "BUY", 3000, amount=2.0, price=0.10)
        path.write_bytes(b'{"version": 1, "entries": [{"ticket": "T-0')
        recovered = Ledger(path)
        print(f"Main file truncated; recovered_from_backup="
              f"{recovered.recovered_from_backup}, entries={len(recovered)}")

        print("\n-- Both copies corrupt: loud failure --")
        recovered.backup_path.write_bytes(b"not json at all")
        try:
            Ledger(path)
        except CorruptLedgerError as exc:
            print(f"Both-corrupt correctly raised: {str(exc)[:72]}...")


if __name__ == "__main__":
    main()
