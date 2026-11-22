"""Tests for ledger.py -- real temp files only, synthetic entries."""
import json

import pytest

from ledger import (CorruptLedgerError, DuplicateEntryError, Ledger,
                    LedgerError, LockHeldError, OrderingError)


def make(tmp_path, name="ledger.json"):
    return Ledger(tmp_path / name)


def test_fresh_ledger_is_empty(tmp_path):
    ledger = make(tmp_path)
    assert len(ledger) == 0
    assert ledger.entries == []
    assert ledger.recovered_from_backup is False


def test_fresh_ledger_creates_no_file_until_save(tmp_path):
    ledger = make(tmp_path)
    assert not ledger.path.exists()
    ledger.save()
    assert ledger.path.exists()


def test_round_trip_single_entry(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 100, amount=1.0)
    again = Ledger(ledger.path)
    assert len(again) == 1
    assert again.entries[0]["ticket"] == "A"
    assert again.entries[0]["amount"] == 1.0


def test_round_trip_many_entries_preserves_order(tmp_path):
    ledger = make(tmp_path)
    for i in range(5):
        ledger.add_and_save(f"T{i}", "BUY", 100 + i, amount=float(i))
    again = Ledger(ledger.path)
    assert [e["ticket"] for e in again.entries] == [f"T{i}" for i in range(5)]


def test_round_trip_extra_fields_survive(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1, price=0.5, chain="solana", note="x y")
    entry = Ledger(ledger.path).entries[0]
    assert entry["price"] == 0.5 and entry["chain"] == "solana"
    assert entry["note"] == "x y"


def test_saved_file_is_valid_json_with_entries_key(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    data = json.loads(ledger.path.read_text())
    assert data["version"] == 1
    assert isinstance(data["entries"], list) and len(data["entries"]) == 1


def test_no_tmp_file_left_after_save(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    assert not (tmp_path / "ledger.json.tmp").exists()


def test_backup_file_exists_after_second_save(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    ledger.add_and_save("B", "BUY", 2)
    assert ledger.backup_path.exists()
    backup = json.loads(ledger.backup_path.read_text())
    assert len(backup["entries"]) == 2


def test_duplicate_ticket_kind_rejected(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 1)
    with pytest.raises(DuplicateEntryError):
        ledger.add_entry("A", "BUY", 2)


def test_duplicate_rejected_after_reload(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    again = Ledger(ledger.path)
    with pytest.raises(DuplicateEntryError):
        again.add_entry("A", "BUY", 5)


def test_same_ticket_different_kind_allowed(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 1)
    ledger.add_entry("A", "CLOSE", 2)
    assert len(ledger) == 2


def test_same_kind_different_ticket_allowed(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 1)
    ledger.add_entry("B", "BUY", 1)
    assert len(ledger) == 2


def test_failed_duplicate_does_not_append(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 1)
    with pytest.raises(DuplicateEntryError):
        ledger.add_entry("A", "BUY", 2)
    assert len(ledger) == 1


def test_close_before_buy_timestamp_rejected(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 100)
    with pytest.raises(OrderingError):
        ledger.add_entry("A", "CLOSE", 99)


def test_close_at_exact_buy_timestamp_allowed(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 100)
    ledger.add_entry("A", "CLOSE", 100)
    assert len(ledger) == 2


def test_close_after_buy_allowed(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 100)
    ledger.add_entry("A", "CLOSE", 250)
    assert ledger.get_entry("A", "CLOSE")["ts"] == 250


def test_close_without_buy_rejected(tmp_path):
    ledger = make(tmp_path)
    with pytest.raises(OrderingError):
        ledger.add_entry("A", "CLOSE", 100)


def test_buy_after_existing_close_rejected(tmp_path):
    # A CLOSE recorded (for a ticket whose BUY is at ts=10) must not be
    # followed by a second BUY -- duplicates block that path, so use a
    # hand-built state: write entries directly, reload, then try a BUY
    # timestamped after the CLOSE on a fresh kind ordering check.
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 10)
    ledger.add_entry("A", "CLOSE", 20)
    # Duplicate BUY is the guard here; it must raise (Duplicate is a
    # LedgerError, Ordering is checked on the CLOSE path).
    with pytest.raises(LedgerError):
        ledger.add_entry("A", "BUY", 30)


def test_non_close_kinds_have_no_ordering_rule(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "NOTE", 50)
    ledger.add_entry("A", "FLAG", 10)  # earlier ts, different kind: fine
    assert len(ledger) == 2


@pytest.mark.parametrize("bad_ticket", ["", None])
def test_empty_ticket_rejected(tmp_path, bad_ticket):
    ledger = make(tmp_path)
    with pytest.raises(ValueError):
        ledger.add_entry(bad_ticket, "BUY", 1)


@pytest.mark.parametrize("bad_kind", ["", None])
def test_empty_kind_rejected(tmp_path, bad_kind):
    ledger = make(tmp_path)
    with pytest.raises(ValueError):
        ledger.add_entry("A", bad_kind, 1)


@pytest.mark.parametrize("bad_ts", [None, -1])
def test_bad_ts_rejected(tmp_path, bad_ts):
    ledger = make(tmp_path)
    with pytest.raises(ValueError):
        ledger.add_entry("A", "BUY", bad_ts)


def test_zero_ts_allowed(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 0)
    assert ledger.entries[0]["ts"] == 0


def test_lock_blocks_second_holder_same_instance(tmp_path):
    ledger = make(tmp_path)
    with ledger.locked():
        assert ledger.is_locked()
        with pytest.raises(LockHeldError):
            with ledger.locked():
                pass


def test_lock_blocks_second_instance(tmp_path):
    first = make(tmp_path)
    second = Ledger(first.path)
    with first.locked():
        with pytest.raises(LockHeldError):
            with second.locked():
                pass


def test_lock_released_after_block(tmp_path):
    ledger = make(tmp_path)
    with ledger.locked():
        pass
    assert not ledger.is_locked()
    with ledger.locked():  # can re-acquire
        pass


def test_lock_released_on_exception(tmp_path):
    ledger = make(tmp_path)
    with pytest.raises(RuntimeError):
        with ledger.locked():
            raise RuntimeError("boom")
    assert not ledger.is_locked()


def test_lock_error_message_names_lock_path(tmp_path):
    ledger = make(tmp_path)
    with ledger.locked():
        with pytest.raises(LockHeldError) as info:
            with Ledger(ledger.path).locked():
                pass
    assert "lock" in str(info.value).lower()


def test_stale_lock_file_blocks_until_removed(tmp_path):
    ledger = make(tmp_path)
    ledger.lock_path.write_text("99999")
    with pytest.raises(LockHeldError):
        with ledger.locked():
            pass
    ledger.lock_path.unlink()
    with ledger.locked():
        pass


def test_torn_main_recovers_from_backup(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1, amount=3.0)
    ledger.path.write_bytes(b'{"version": 1, "entries": [{"tick')
    recovered = Ledger(ledger.path)
    assert recovered.recovered_from_backup is True
    assert len(recovered) == 1
    assert recovered.entries[0]["amount"] == 3.0


def test_empty_main_recovers_from_backup(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    ledger.path.write_bytes(b"")
    recovered = Ledger(ledger.path)
    assert recovered.recovered_from_backup is True
    assert len(recovered) == 1


def test_garbage_main_recovers_from_backup(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    ledger.add_and_save("B", "BUY", 2)
    ledger.path.write_text("### not json ###")
    recovered = Ledger(ledger.path)
    assert len(recovered) == 2


def test_missing_main_recovers_from_backup(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    ledger.path.unlink()
    recovered = Ledger(ledger.path)
    assert recovered.recovered_from_backup is True
    assert len(recovered) == 1


def test_both_corrupt_raises_loud_error(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    ledger.path.write_text("garbage main")
    ledger.backup_path.write_text("garbage backup")
    with pytest.raises(CorruptLedgerError) as info:
        Ledger(ledger.path)
    assert "both" in str(info.value).lower()


def test_corrupt_main_no_backup_raises_not_empty(tmp_path):
    path = tmp_path / "solo.json"
    path.write_text("{ truncated")
    with pytest.raises(CorruptLedgerError):
        Ledger(path)


def test_structurally_wrong_main_recovers(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    ledger.path.write_text(json.dumps({"no_entries_here": True}))
    recovered = Ledger(ledger.path)
    assert recovered.recovered_from_backup is True
    assert len(recovered) == 1


def test_recovery_then_save_repairs_main(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    ledger.path.write_bytes(b"torn")
    recovered = Ledger(ledger.path)
    recovered.add_and_save("B", "BUY", 2)
    again = Ledger(ledger.path)
    assert again.recovered_from_backup is False
    assert len(again) == 2


def test_has_and_get_entry(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 1, amount=2.0)
    assert ledger.has_entry("A", "BUY") is True
    assert ledger.has_entry("A", "CLOSE") is False
    assert ledger.get_entry("A", "BUY")["amount"] == 2.0
    assert ledger.get_entry("ZZZ", "BUY") is None


def test_tickets_and_entries_for(tmp_path):
    ledger = make(tmp_path)
    ledger.add_entry("A", "BUY", 1)
    ledger.add_entry("A", "CLOSE", 2)
    ledger.add_entry("B", "BUY", 3)
    assert ledger.tickets() == ["A", "B"]
    assert len(ledger.entries_for("A")) == 2
    assert ledger.entries_for("NOPE") == []


def test_returned_entry_is_a_copy(tmp_path):
    ledger = make(tmp_path)
    entry = ledger.add_entry("A", "BUY", 1, amount=1.0)
    entry["amount"] = 999.0
    assert ledger.entries[0]["amount"] == 1.0


def test_nested_directory_created_on_save(tmp_path):
    path = tmp_path / "deep" / "deeper" / "ledger.json"
    ledger = Ledger(path)
    ledger.add_and_save("A", "BUY", 1)
    assert path.exists()
    assert len(Ledger(path)) == 1


def test_reload_picks_up_external_good_write(tmp_path):
    ledger = make(tmp_path)
    ledger.add_and_save("A", "BUY", 1)
    other = Ledger(ledger.path)
    other.add_and_save("B", "BUY", 2)
    ledger.reload()
    assert len(ledger) == 2
