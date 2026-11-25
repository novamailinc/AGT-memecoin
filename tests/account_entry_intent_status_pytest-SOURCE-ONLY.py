"""SOURCE ONLY, UNEXECUTED (written, not run). Needs main's explicit clearance for this exact blob before any run, and a pytest-capable interpreter (the freeze annotation applies). Not named test_*.py on purpose:
it is NOT collected by the CI glob; run it by explicit path only. Handler-level tests of the existing /account demo path with the read-only entry-intent lines, using the existing fixture `account` (tests/test_paper_account_instances.py)
exactly as tests/test_shared_pending_telegram.py does: ctx.send is a local capture list (no Telegram send), network sockets are blocked, no token or config env is read or set, temp dirs only.
Covers: clear adds nothing; pending (valid, leftover) uses the 'may be pre-debit / cleanup / unrecovered, not classified' wording; malformed record text is redacted; unknown on read failure (never clear); recorded state shown with the flag off;
the handler leaves the record directory byte-identical; the existing account lines are still present."""
import hashlib, importlib.util, json, os, socket
from types import SimpleNamespace
from daemon.launch_scanner import paper_account as pa
from daemon.telegram import commands
from daemon.launch_scanner import grad_entry_intent as gei
from tests.test_paper_account_instances import account  # noqa: F401  (existing fixture)

def _run(monkeypatch, tmp_path):
    monkeypatch.setattr(commands, 'control_only', lambda: False)
    monkeypatch.setenv('FOREX_AGENT_HOME', str(tmp_path / 'home'))
    def _nosock(*a, **k): raise AssertionError("network socket attempted")
    monkeypatch.setattr(socket, 'socket', _nosock)
    pa.try_debit_instance('main', 'M', 2., 100., 'entry')
    msgs = []; ctx = SimpleNamespace(send=lambda chat, text, *a: msgs.append(text))
    commands._cmd_account(ctx, 1, 1, 1, ['demo'])
    return "\n".join(msgs)

def _rec(mint="GradMint1pump", op="op1"):
    pos = {"mint": mint, "tokens": 1.0, "entry_price": 1e-05, "entry_ts": 1.0, "legs": {}, "entry_features": {}, "execution": {"entry_op": op}}
    return gei.build_record("grad", mint, op, 1.0, 1.0, pos, {"qty_tokens": 1.0, "price_usd": 1e-05}, {"entry_day": "2026-11-24"})

def _dsnap(d):
    return {n: hashlib.sha256(open(os.path.join(d, n), 'rb').read()).hexdigest() for n in sorted(os.listdir(d))} if os.path.isdir(d) else None

class TestAccountEntryIntentStatus:
    def test_clear_adds_no_entry_intent_line_and_keeps_account_lines(self, account, monkeypatch, tmp_path):
        body = _run(monkeypatch, tmp_path)
        assert 'entry-intent' not in body.lower()
        assert 'Spendable cash: $8.00' in body and 'Retained cost basis (open + pending): $2.00' in body
        assert not os.path.exists(gei.records_dir(gei.default_ledger_path()))   # the view created nothing

    def test_pending_leftover_uses_unresolved_wording_not_unrecovered_fact(self, account, monkeypatch, tmp_path):
        monkeypatch.setenv('FOREX_AGENT_HOME', str(tmp_path / 'home')); os.makedirs(str(tmp_path / 'home'))
        gei.write_record(gei.default_ledger_path(), _rec())
        d = gei.records_dir(gei.default_ledger_path()); before = _dsnap(d)
        monkeypatch.delenv(gei.FLAG, raising=False)
        body = _run(monkeypatch, tmp_path)
        line = [l for l in body.split("\n") if 'entry-intent' in l][0]
        assert 'GradMint1pum' in line and 'GradMint1pump' not in line and 'UNRESOLVED entry attempt' in line   # 12-char display hint, not a unique identifier
        assert 'may be a pre-debit leftover' in line and 'cleanup leftover' in line and 'does not classify' in line
        assert 'An unrecovered entry' not in line and 'not an ordinary cycle fault' in line
        assert 'recovery flag in this process: OFF' in line      # recorded state shown with the flag off, labeled
        assert _dsnap(d) == before                                  # read-only
        assert 'Spendable cash: $8.00' in body

    def test_same_prefix_mints_are_two_records_with_identical_display_hint(self, account, monkeypatch, tmp_path):
        monkeypatch.setenv('FOREX_AGENT_HOME', str(tmp_path / 'home')); os.makedirs(str(tmp_path / 'home'))
        gei.write_record(gei.default_ledger_path(), _rec("GradMint1pump", "opA")); gei.write_record(gei.default_ledger_path(), _rec("GradMint1pumq", "opB"))
        d = gei.records_dir(gei.default_ledger_path()); before = _dsnap(d)
        st = gei.status_readonly(gei.default_ledger_path())
        assert len(st["records"]) == 2 and {r["mint"] for r in st["records"]} == {"GradMint1pump", "GradMint1pumq"}
        body = _run(monkeypatch, tmp_path)
        line = [l for l in body.split("\n") if 'entry-intent' in l][0]
        assert '2 record file(s)' in line and line.count('GradMint1pum') == 2 and 'GradMint1pump' not in line and 'GradMint1pumq' not in line
        assert _dsnap(d) == before

    def test_flag_on_label(self, account, monkeypatch, tmp_path):
        monkeypatch.setenv('FOREX_AGENT_HOME', str(tmp_path / 'home')); os.makedirs(str(tmp_path / 'home'))
        gei.write_record(gei.default_ledger_path(), _rec()); monkeypatch.setenv(gei.FLAG, '1')
        assert 'recovery flag in this process: ON' in _run(monkeypatch, tmp_path)

    def test_malformed_record_text_is_redacted(self, account, monkeypatch, tmp_path):
        monkeypatch.setenv('FOREX_AGENT_HOME', str(tmp_path / 'home')); os.makedirs(str(tmp_path / 'home'))
        d = gei.records_dir(gei.default_ledger_path()); os.makedirs(d)
        json.dump({"schema": 1, "lane": "grad", "mint": "<b>X</b> /start http://evil.example " + "A" * 500, "op_id": "x" * 400}, open(os.path.join(d, "evil.json"), "w"))
        open(os.path.join(d, "junk.json"), "w").write("{ not json <script>alert(1)</script> " + "B" * 500)
        body = _run(monkeypatch, tmp_path)
        assert 'entry-intent' in body and 'INVALID/UNREADABLE' in body
        for bad in ('evil.example', '<b>', '<script>', '/start', 'AAAA', 'BBBB', 'xxxx'):
            assert bad not in body

    def test_read_failure_is_unknown_never_clear(self, account, monkeypatch, tmp_path):
        def boom(_): raise OSError("simulated read failure with secret-like text sk-test-123")
        monkeypatch.setattr(gei, 'status_readonly', boom)
        body = _run(monkeypatch, tmp_path)
        line = [l for l in body.split("\n") if 'entry-intent' in l][0]
        assert 'UNKNOWN' in line and 'not reported as clear' in line
        assert 'sk-test-123' not in body and 'simulated read failure' not in body   # only the exception TYPE is shown

    def test_unreadable_directory_is_unknown(self, account, monkeypatch, tmp_path):
        monkeypatch.setenv('FOREX_AGENT_HOME', str(tmp_path / 'home')); os.makedirs(str(tmp_path / 'home'))
        d = gei.records_dir(gei.default_ledger_path()); os.makedirs(d)
        real = os.listdir
        def deny(path=".", *a, **k):
            if str(path) == d: raise PermissionError("simulated unreadable entry-intent directory")
            return real(path, *a, **k)
        monkeypatch.setattr(gei.os, 'listdir', deny)
        body = _run(monkeypatch, tmp_path)
        assert 'UNKNOWN' in body and 'not reported as clear' in body
