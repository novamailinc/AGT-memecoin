"""Account-wide holds: seal-time reservation across books and lanes.

Reviewer gaps closed here (2026-11-23):
A. Two adapters on one shared account: a fill that settles before this
   process learns of it was invisible to a second book's admission
   check, so both spent the same dollars. The hold moves admission to
   SEAL time and makes the reservation account-wide state.
C. A settled FAILED tx's fee was mirrored in the native SOL mirror but
   never reduced the shared USD ledger. settle_hold_fee books it
   authoritatively, crash-safe and idempotent.
"""
import json
import time
from pathlib import Path

import pytest
from daemon.launch_scanner import paper_account as pa


@pytest.fixture
def account(tmp_path, monkeypatch):
    folder = tmp_path / 'paper_account'
    monkeypatch.setattr(pa, 'ACCOUNT_DIR', str(folder))
    monkeypatch.setattr(pa, 'LEDGER_PATH', str(folder / 'ledger.json'))
    monkeypatch.setattr(pa, 'JOURNAL_PATH', str(folder / 'events.jsonl'))
    monkeypatch.setattr(pa, 'LOCK_FILE', str(folder / '.lock'))
    for key in ('PAPER_ACCOUNT_LANE_CAP', 'PAPER_ACCOUNT_GATES_ENABLED',
                'PAPER_ACCOUNT_PROVIDER_HEALTH_GATE_ENABLED'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('PAPER_ACCOUNT_START', '10')
    return pa


class TestHoldBasics:
    def test_hold_reserves_spendable_not_cash_and_retries(self, account):
        ok, why = pa.hold_stake('main', 5.0015, 'native-hold:bookA:k1')
        assert ok, why
        snap = pa.snapshot()
        assert snap['cash'] == 10.0
        assert snap['cash_spendable'] == round(10.0 - 5.0015, 6)
        assert snap['held_cash'] == 5.0015
        assert snap['holds']['native-hold:bookA:k1']['amount'] == 5.0015
        # Idempotent retry; conflicting reuse refused.
        assert pa.hold_stake('main', 5.0015, 'native-hold:bookA:k1')[0]
        assert not pa.hold_stake('main', 5.0, 'native-hold:bookA:k1')[0]
        assert not pa.hold_stake('other', 5.0015, 'native-hold:bookA:k1')[0]

    def test_second_book_cannot_spend_reserved_cash(self, account):
        # Reviewer probe P7: book A's execution settles but is not yet
        # adopted; book B must see the reservation account-wide.
        assert pa.hold_stake('main', 5.0015, 'native-hold:bookA:k1')[0]
        ok, why = pa.hold_stake('main', 5.0, 'native-hold:bookB:k9')
        assert not ok and 'spendable' in why
        # A plain entry through the legacy path also sees the hold.
        assert not pa.try_debit_instance('main', 'X', 5.0, 100., 'bookB-entry')[0]
        # What remains spendable is exactly the unreserved rest.
        assert pa.hold_stake('main', 4.9985, 'native-hold:bookB:k9')[0]

    def test_hold_counts_against_lane_cap(self, account, monkeypatch):
        monkeypatch.setenv('PAPER_ACCOUNT_LANE_CAP', '6')
        assert pa.hold_stake('main', 5.0, 'h1')[0]
        ok, why = pa.hold_stake('main', 1.5, 'h2')
        assert not ok and 'lane cap' in why
        assert pa.hold_stake('other', 1.5, 'h3')[0]  # other lane unaffected


class TestConsumeHold:
    def test_consume_opens_position_and_releases_remainder(self, account):
        assert pa.hold_stake('main', 5.0015, 'native-hold:bookA:k1')[0]
        ok, why, pid = pa.consume_hold_for_entry(
            'native-hold:bookA:k1', 'main', 'M', 5.0, 100., 'native-entry:bookA:k1')
        assert ok and pid, why
        snap = pa.snapshot()
        assert snap['cash'] == 5.0
        assert snap['cash_spendable'] == 5.0
        assert snap['held_cash'] == 0.0
        assert snap['lanes']['main']['open_count'] == 1
        assert snap['lanes']['main']['positions'][0]['basis'] == 5.0
        # Retry returns the same lot, never double-debits.
        ok, why, pid2 = pa.consume_hold_for_entry(
            'native-hold:bookA:k1', 'main', 'M', 5.0, 100., 'native-entry:bookA:k1')
        assert ok and pid2 == pid and 'retry' in why
        assert pa.snapshot()['cash'] == 5.0

    def test_consume_requires_matching_covering_hold(self, account):
        assert pa.hold_stake('main', 5.0, 'h1')[0]
        assert not pa.consume_hold_for_entry('nope', 'main', 'M', 1., 1., 'e1')[0]
        ok, why, _ = pa.consume_hold_for_entry('h1', 'other', 'M', 1., 1., 'e1')
        assert not ok and 'lane' in why
        ok, why, _ = pa.consume_hold_for_entry('h1', 'main', 'M', 5.5, 1., 'e1')
        assert not ok and 'does not cover' in why
        # Hold still intact after failed consumes.
        assert pa.snapshot()['held_cash'] == 5.0

    def test_consume_distinct_books_do_not_alias(self, account):
        # The accepted multi-book semantics survive: two books on one
        # account, each holding and consuming its own reservation.
        assert pa.hold_stake('main', 5.0, 'native-hold:bookA:k1')[0]
        assert pa.hold_stake('main', 5.0, 'native-hold:bookB:k1')[0]
        ok, _, pa_pid = pa.consume_hold_for_entry(
            'native-hold:bookA:k1', 'main', 'M', 5.0, 100., 'native-entry:bookA:k1')
        assert ok
        ok, _, pb_pid = pa.consume_hold_for_entry(
            'native-hold:bookB:k1', 'main', 'M', 5.0, 100., 'native-entry:bookB:k1')
        assert ok and pb_pid != pa_pid
        snap = pa.snapshot()
        assert snap['cash'] == 0.0 and snap['lanes']['main']['open_count'] == 2


class TestFeeSettlement:
    def test_settled_failed_tx_fee_reduces_shared_cash(self, account):
        # Reviewer probe P9: a tx_failed settles with a real fee; the
        # shared USD ledger must lose it, not just the native mirror.
        assert pa.hold_stake('main', 5.0015, 'native-hold:bookA:k1')[0]
        ok, why = pa.settle_hold_fee('native-hold:bookA:k1', 0.0015,
                                     'native-fee:bookA:k1')
        assert ok, why
        snap = pa.snapshot()
        assert snap['cash'] == 9.9985
        assert snap['cash_spendable'] == 9.9985
        assert snap['held_cash'] == 0.0
        assert pa.settle_hold_fee('native-hold:bookA:k1', 0.0015,
                                  'native-fee:bookA:k1')[0]  # idempotent
        assert pa.snapshot()['cash'] == 9.9985
        # And the remainder never became double-spendable.
        assert not pa.hold_stake('main', 9.9986, 'h2')[0]

    def test_fee_cannot_exceed_hold_and_missing_hold_refused(self, account):
        assert pa.hold_stake('main', 5.0, 'h1')[0]
        assert not pa.settle_hold_fee('h1', 5.5, 'f1')[0]
        assert not pa.settle_hold_fee('nope', 0.1, 'f2')[0]
        assert pa.snapshot()['held_cash'] == 5.0


class TestReleaseHold:
    def test_unlanded_release_restores_spendable(self, account):
        assert pa.hold_stake('main', 5.0015, 'h1')[0]
        ok, why = pa.release_hold('h1', 'never landed')
        assert ok, why
        snap = pa.snapshot()
        assert snap['cash_spendable'] == 10.0 and snap['held_cash'] == 0.0
        assert pa.release_hold('h1', 'never landed')[0]  # idempotent

    def test_consumed_hold_cannot_be_released(self, account):
        assert pa.hold_stake('main', 5.0, 'h1')[0]
        assert pa.consume_hold_for_entry('h1', 'main', 'M', 5.0, 1., 'e1')[0]
        before = pa.snapshot()['cash_spendable']
        # The consume already journaled the release: a late release
        # call is an idempotent no-op, NEVER a second credit.
        ok, why = pa.release_hold('h1', 'late unknown')
        assert ok and 'retry' in why
        assert pa.snapshot()['cash_spendable'] == before
        assert not pa.release_hold('never-held', 'x')[0]


class TestHoldCrashReplay:
    def test_crash_after_journal_before_save_replays_hold(self, account):
        assert pa.hold_stake('main', 5.0015, 'h1')[0]
        # State as it was after the hold; the journal carries the
        # consume that followed. Restoring the older state simulates a
        # crash after the consume's WAL writes but before its save.
        path = pa.LEDGER_PATH
        with open(path) as fh:
            pre = fh.read()
        ok, _, pid = pa.consume_hold_for_entry('h1', 'main', 'M', 5.0, 100., 'e1')
        assert ok
        with open(path, 'w') as fh:
            fh.write(pre)
        snap = pa.snapshot()
        assert snap['cash'] == 5.0 and snap['held_cash'] == 0.0
        assert snap['lanes']['main']['open_count'] == 1
        assert snap['lanes']['main']['positions'][0]['pos_id'] == pid

    def test_crash_between_consume_events_repairs_release(self, account):
        assert pa.hold_stake('main', 5.0015, 'h1')[0]
        # Forge the torn middle: entry applied, release not yet.
        with open(pa.LEDGER_PATH) as fh:
            led = json.load(fh)
        led['seq'] += 1
        pid = 'main/M#%d' % led['seq']
        pa._apply_debit(led, 'main', 'M', 5.0, 100., pid, None, instance=True)
        led['ops']['e1'] = {'event': 'debit', 'lane': 'main', 'mint': 'M',
                            'pos_id': pid, 'stake': 5.0, 'qty': 100.,
                            'instance': True}
        with open(pa.LEDGER_PATH, 'w') as fh:
            json.dump(led, fh)
        ok, why, got = pa.consume_hold_for_entry('h1', 'main', 'M', 5.0, 100., 'e1')
        assert ok and got == pid and 'retry' in why
        snap = pa.snapshot()
        assert snap['held_cash'] == 0.0
        assert snap['cash_spendable'] == 5.0

    def test_release_for_unknown_hold_is_divergence_not_credit(
            self, account):
        # LOW fix: a corrupt journal releasing a hold that never
        # existed must DEGRADE the account, never mint spendable cash.
        assert pa.hold_stake('main', 2.0, 'h1')[0]
        # Generation binding (2026-11-27): the forged entry must
        # carry the CURRENT account generation, so replay reaches the
        # unknown-hold check instead of stopping at the earlier
        # mixed-generation divergence. The corruption under test is
        # the unknown hold, not the generation.
        gen = json.loads(Path(pa.LEDGER_PATH).read_text())['account_id']
        with open(pa.JOURNAL_PATH, 'a') as fh:
            fh.write(json.dumps({
                'event': 'hold_released', 'event_id': 'ev#2',
                'op_id': 'ghost:release', 'hold_op_id': 'ghost',
                'amount': 9.0, 'seq': 2, 'ts': time.time(),
                'account_id': gen}) + '\n')
        snap = pa.snapshot()
        assert snap['degraded'] and 'unknown hold' in snap['degraded']
        assert snap['cash_spendable'] == 8.0  # never credited the ghost
        ok, why = pa.hold_stake('main', 1.0, 'h2')
        assert not ok and 'degraded' in why

    def test_orphan_hold_visible_for_owner_sweep(self, account):
        # Crash between hold and the caller's own seal record: the
        # reservation is listable by book prefix so its owner releases
        # it; it never expires silently.
        assert pa.hold_stake('main', 5.0, 'native-hold:bookA:k1')[0]
        assert pa.hold_stake('main', 4.0, 'native-hold:bookB:k2')[0]
        mine = pa.list_holds('native-hold:bookA:')
        assert list(mine) == ['native-hold:bookA:k1']
        assert len(pa.list_holds()) == 2
