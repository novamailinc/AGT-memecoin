"""Tests for tickets.py — run: python3 tests/test_tickets.py"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advanced_trader.tickets import TicketAllocator


def test_monotonic_allocation():
    with tempfile.TemporaryDirectory() as tmp:
        f = str(Path(tmp) / "tickets.json")
        a = TicketAllocator(f)
        assert [a.next_ticket() for _ in range(3)] == [1, 2, 3]
        assert a.peek() == 4
    print("PASS test_monotonic_allocation")


def test_never_reused_across_restart():
    with tempfile.TemporaryDirectory() as tmp:
        f = str(Path(tmp) / "tickets.json")
        a1 = TicketAllocator(f)
        first_batch = [a1.next_ticket() for _ in range(5)]
        # Simulate a restart: a brand-new allocator on the same file.
        a2 = TicketAllocator(f)
        second_batch = [a2.next_ticket() for _ in range(5)]
        assert first_batch == [1, 2, 3, 4, 5]
        assert second_batch == [6, 7, 8, 9, 10]
        assert not set(first_batch) & set(second_batch)
        # And again, after another restart.
        a3 = TicketAllocator(f)
        assert a3.next_ticket() == 11
    print("PASS test_never_reused_across_restart")


def test_custom_start():
    with tempfile.TemporaryDirectory() as tmp:
        f = str(Path(tmp) / "tickets.json")
        a = TicketAllocator(f, start=665)
        assert a.next_ticket() == 665
        assert a.next_ticket() == 666
    print("PASS test_custom_start")


def test_corrupt_state_fails_closed():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "tickets.json"
        f.write_text("{not json")
        try:
            TicketAllocator(str(f))
        except ValueError as exc:
            assert "corrupt" in str(exc)
        else:
            raise AssertionError("corrupt state file must raise, not reset")
        f.write_text(json.dumps({"next_ticket": 0}))
        try:
            TicketAllocator(str(f))
        except ValueError:
            pass
        else:
            raise AssertionError("next_ticket below start must raise")
    print("PASS test_corrupt_state_fails_closed")


def test_state_file_is_plain_json():
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "tickets.json"
        a = TicketAllocator(str(f))
        a.next_ticket()
        a.next_ticket()
        assert json.loads(f.read_text()) == {"next_ticket": 3}
    print("PASS test_state_file_is_plain_json")


if __name__ == "__main__":
    test_monotonic_allocation()
    test_never_reused_across_restart()
    test_custom_start()
    test_corrupt_state_fails_closed()
    test_state_file_is_plain_json()
    print("test_tickets.py: ALL PASS")
