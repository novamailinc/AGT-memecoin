#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). Needs main's explicit clearance for this exact blob before any run. STDLIB ONLY, NO pytest: runs under any interpreter that can import the repo
(the contained interpreter has no pytest). Not named test_*.py on purpose, so the CI glob never collects it; run by explicit path only.

Handler-level tests of the REAL /account demo path (daemon.telegram.commands._cmd_account) with the read-only entry-intent lines, using the SAME actual formatter contract as the status unit test:
the mint is shown truncated to its first 12 characters as a DISPLAY HINT (not a unique identifier); records are counted by record, never by displayed string.
This file is the pytest handler source (tests/account_entry_intent_status_pytest-SOURCE-ONLY.py) re-expressed without pytest. Fixture parity: the `account` fixture of tests/test_paper_account_instances.py is
reproduced by hand (same paper_account attribute patches, same gate env with TEST-FIXTURE limits, the same install_price_feed(at_cost=True) with a fixture SOL price), set and RESTORED per case.

CONTAINMENT (source-reviewable): tests._guard_fixture.block_network() runs BEFORE any repo import and socket.socket is also replaced by a raiser during each case; every case uses its own temp dir as
FOREX_AGENT_HOME and paper-account folder; NO token, chat id, pair code or config variable is read or set (only FOREX_AGENT_HOME, PAPER_ACCOUNT_* fixture limits, AGT_GRAD_ENTRY_INTENT_ENABLED); commands.control_only
is patched to False; the handler's `ctx` is a local SimpleNamespace whose send() appends to a list (no Telegram send; it has no `tools` attribute, so any use of ctx.tools for the demo selection would fail the case);
only the demo selection is called. Importing daemon.telegram.commands reads no token or config at import time (read: env and file reads there happen inside functions only). The handler's fallback import
`import grad_entry_intent` (when the package import fails) could shadow the module by sys.path order; this file imports the package module and asserts the handler resolved to the SAME module object. Caveat only, no change.
Cases (each asserts specific values): H1 clear adds no entry-intent line, keeps the existing account lines, creates nothing; H2 pending leftover with the flag off: unresolved wording, 12-char display hint present and the
full 13-char mint absent, recorded state shown flag-off, read-only (directory bytes identical), account lines intact; H3 flag on label; H4 malformed record text redacted; H5 read failure is UNKNOWN, only the exception type shown;
H6 unreadable directory (uid-independent stub, not chmod) is UNKNOWN; H7 two mints sharing the 12-char prefix: two records, identical display hint twice, count by record, directory untouched; H8 handler module identity; H9 missing parent directory is UNKNOWN with no writes (H1 clear case creates the home first)."""
import hashlib, json, os, shutil, socket, sys, tempfile, time
from types import SimpleNamespace
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
import tests._guard_fixture as G; G.block_network()      # BEFORE any repo import
from daemon.launch_scanner import paper_account as pa
from daemon.launch_scanner import grad_entry_intent as gei
from daemon.telegram import commands
FAILS = []
def check(n, ok):
    print(("PASS " if ok else "FAIL ") + n)
    if not ok: FAILS.append(n)

_ENV_KEYS = ("FOREX_AGENT_HOME", "PAPER_ACCOUNT_START", "PAPER_ACCOUNT_GATES_ENABLED", "PAPER_ACCOUNT_MAX_DAILY_LOSS_USD", "PAPER_ACCOUNT_MAX_DRAWDOWN_PCT", "PAPER_ACCOUNT_MAX_EXPOSURE_PCT",
             "PAPER_ACCOUNT_MAX_SIMULTANEOUS", "PAPER_ACCOUNT_MAX_POSITIONS", "PAPER_ACCOUNT_MAX_DATA_AGE_S", "PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD", "PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD",
             "PAPER_ACCOUNT_LANE_CAP", "PAPER_ACCOUNT_PROVIDER_HEALTH_GATE_ENABLED", gei.FLAG)
_FIX = {"PAPER_ACCOUNT_START": "10", "PAPER_ACCOUNT_GATES_ENABLED": "1", "PAPER_ACCOUNT_MAX_DAILY_LOSS_USD": "1000000", "PAPER_ACCOUNT_MAX_DRAWDOWN_PCT": "99", "PAPER_ACCOUNT_MAX_EXPOSURE_PCT": "100",
        "PAPER_ACCOUNT_MAX_SIMULTANEOUS": "1000", "PAPER_ACCOUNT_MAX_POSITIONS": "1000", "PAPER_ACCOUNT_MAX_DATA_AGE_S": "100000", "PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD": "1000000",
        "PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD": "1000000"}   # TEST-FIXTURE limits (the existing fixture's values), not owner values

class Case:
    """Hand-written equivalent of the pytest `account` + monkeypatch + tmp_path fixtures for ONE case; __exit__ restores everything it touched."""
    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="geih_"); self.saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
        self.saved_pa = {k: getattr(pa, k) for k in ("ACCOUNT_DIR", "LEDGER_PATH", "JOURNAL_PATH", "LOCK_FILE")}
        folder = os.path.join(self.tmp, "paper_account")
        pa.ACCOUNT_DIR = folder; pa.LEDGER_PATH = os.path.join(folder, "ledger.json"); pa.JOURNAL_PATH = os.path.join(folder, "events.jsonl"); pa.LOCK_FILE = os.path.join(folder, ".lock")
        for k in ("PAPER_ACCOUNT_LANE_CAP", "PAPER_ACCOUNT_PROVIDER_HEALTH_GATE_ENABLED", gei.FLAG): os.environ.pop(k, None)
        os.environ.update(_FIX)
        from daemon.solana_ids import WSOL_MINT
        self.feed = G.install_price_feed(at_cost=True); self.feed.set({WSOL_MINT: (150.0, time.time())})
        self.home = os.path.join(self.tmp, "home"); os.environ["FOREX_AGENT_HOME"] = self.home
        self.saved_cf = commands.control_only; commands.control_only = lambda: False
        self.saved_sock = socket.socket
        def _nosock(*a, **k): raise AssertionError("network socket attempted")
        socket.socket = _nosock
        return self
    def __exit__(self, *exc):
        socket.socket = self.saved_sock; commands.control_only = self.saved_cf
        self.feed.uninstall()
        for k, v in self.saved_pa.items(): setattr(pa, k, v)
        for k, v in self.saved_env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False
    def run(self):
        pa.try_debit_instance("main", "M", 2., 100., "entry")
        msgs = []; ctx = SimpleNamespace(send=lambda chat, text, *a: msgs.append(text))
        commands._cmd_account(ctx, 1, 1, 1, ["demo"])
        return "\n".join(msgs), msgs

def _rec(mint="GradMint1pump", op="op1"):
    pos = {"mint": mint, "tokens": 1.0, "entry_price": 1e-05, "entry_ts": 1.0, "legs": {}, "entry_features": {}, "execution": {"entry_op": op}}
    return gei.build_record("grad", mint, op, 1.0, 1.0, pos, {"qty_tokens": 1.0, "price_usd": 1e-05}, {"entry_day": "2026-11-24"})
def _dsnap(d): return {n: hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest() for n in sorted(os.listdir(d))} if os.path.isdir(d) else None
def _il(body): return [l for l in body.split("\n") if "entry-intent" in l]

def h1():
    with Case() as c:
        os.makedirs(c.home)   # CLEAR state needs an existing readable home (status_readonly: a missing parent of the records dir is UNKNOWN, see H9). v5/1f91800d omitted this.
        body, msgs = c.run(); d = gei.records_dir(gei.default_ledger_path())
        # DIAGNOSTIC split (successor to the single H1 check, which failed as one aggregate at 1f91800d): each condition is its own check, and the captured body is printed (account text only, no secrets).
        print("INFO H1 captured body: %r" % body[:1200])
        check("H1a exactly one message captured locally (count %d)" % len(msgs), len(msgs) == 1)
        check("H1b clear adds NO entry-intent line", "entry-intent" not in body.lower())
        check("H1c Cash and Spendable cash line prefixes present", "Cash: $" in body and "Spendable cash: $" in body)
        # ECONOMIC CORRECTNESS (kept, grounded). Rationale: the fixture debits stake 2.0 from PAPER_ACCOUNT_START 10 via try_debit_instance. The existing tests/test_paper_account_instances.py asserts under the SAME fixture, for stakes 1 then 2, cash == 7 and cost_exposure == 3 (10 - 3, 1 + 2), so one stake of 2 gives cash 8 and cost_exposure 2. Two independent checks: (i) the grounded values on a direct pa.snapshot() taken by the test itself, (ii) the handler's printed lines equal the formatter contract "$%.2f" of that independent snapshot (not a hard-coded string).
        ind = pa.snapshot(); fm = lambda v: "$%.2f" % float(v)
        print("INFO H1 independent snapshot: cash %r cash_spendable %r cost_exposure %r pending_sol_basis %r book_value %r" % (ind.get("cash"), ind.get("cash_spendable"), ind.get("cost_exposure"), ind.get("pending_sol_basis"), ind.get("book_value")))
        check("H1d1 independent account snapshot has the grounded economics: cash 8.0 and cost_exposure 2.0 (existing fixture test: stakes 1+2 -> cash 7, cost 3)", abs(float(ind["cash"]) - 8.0) < 1e-6 and abs(float(ind["cost_exposure"]) - 2.0) < 1e-6)
        check("H1d2 handler Retained cost basis line equals the independent snapshot's cost_exposure formatted (%s)" % fm(ind.get("cost_exposure")), ("Retained cost basis (open + pending): %s" % fm(ind.get("cost_exposure"))) in body)
        check("H1d3 handler Cash and Spendable cash lines equal the independent snapshot (%s, %s)" % (fm(ind.get("cash")), fm(ind.get("cash_spendable"))), ("Cash: %s" % fm(ind.get("cash"))) in body and ("Spendable cash: %s" % fm(ind.get("cash_spendable"))) in body)
        print("INFO H1 retained-basis line: %r" % [l for l in body.split("\n") if "Retained cost basis" in l])
        check("H1e the view created no entry-intent directory (%s)" % d, not os.path.exists(d))
def h9():
    with Case() as c:
        check("H9 precondition: home directory does not exist", not os.path.exists(c.home))
        body, msgs = c.run(); ls = _il(body); line = ls[0] if ls else ""; d = gei.records_dir(gei.default_ledger_path())
        check("H9 missing parent of the entry-intent directory: UNKNOWN (ledger directory unreadable), 'not reported as clear', exactly one entry-intent line, NOT silently clear",
              len(ls) == 1 and "UNKNOWN" in line and "not reported as clear" in line and "ledger directory unreadable" in line)
        check("H9 the view wrote nothing: neither the home directory nor the entry-intent directory exists afterwards", not os.path.exists(c.home) and not os.path.exists(d))
def h2():
    with Case() as c:
        os.makedirs(c.home); gei.write_record(gei.default_ledger_path(), _rec()); d = gei.records_dir(gei.default_ledger_path()); before = _dsnap(d)
        body, msgs = c.run(); ls = _il(body); line = ls[0] if ls else ""
        check("H2 pending, flag OFF: one line, UNRESOLVED wording (may be pre-debit / cleanup / unrecovered, does not classify, not a cycle fault), flag OFF label, 12-char DISPLAY HINT shown and the full 13-char mint absent",
              len(ls) == 1 and "UNRESOLVED entry attempt" in line and "may be a pre-debit leftover" in line and "cleanup leftover" in line and "does not classify" in line and "An unrecovered entry" not in line
              and "not an ordinary cycle fault" in line and "recovery flag in this process: OFF" in line and "GradMint1pum" in line and "GradMint1pump" not in line)
        check("H2 handler left the record directory byte-identical and the account lines intact", _dsnap(d) == before and "Spendable cash: $" in body and "Cash: $" in body)
def h3():
    with Case() as c:
        os.makedirs(c.home); gei.write_record(gei.default_ledger_path(), _rec()); os.environ[gei.FLAG] = "1"
        body, _ = c.run()
        check("H3 flag on: line says recovery flag in this process: ON", "recovery flag in this process: ON" in body)
def h4():
    with Case() as c:
        os.makedirs(c.home); d = gei.records_dir(gei.default_ledger_path()); os.makedirs(d)
        json.dump({"schema": 1, "lane": "grad", "mint": "<b>X</b> /start http://evil.example " + "A" * 500, "op_id": "x" * 400}, open(os.path.join(d, "evil.json"), "w"))
        open(os.path.join(d, "junk.json"), "w").write("{ not json <script>alert(1)</script> " + "B" * 500)
        body, _ = c.run()
        check("H4 malformed record text: line present, INVALID/UNREADABLE label, none of the attacker text or long strings surfaced",
              "entry-intent" in body and "INVALID/UNREADABLE" in body and not any(b in body for b in ("evil.example", "<b>", "<script>", "/start", "AAAA", "BBBB", "xxxx")))
def h5():
    with Case() as c:
        real = gei.status_readonly
        def boom(_): raise OSError("simulated read failure with secret-like text sk-test-123")
        gei.status_readonly = boom
        try: body, _ = c.run()
        finally: gei.status_readonly = real
        ls = _il(body); line = ls[0] if ls else ""
        check("H5 read failure: UNKNOWN and 'not reported as clear', only the exception TYPE shown (no message text)", len(ls) == 1 and "UNKNOWN" in line and "not reported as clear" in line and "sk-test-123" not in body and "simulated read failure" not in body)
def h6():
    with Case() as c:
        os.makedirs(c.home); d = gei.records_dir(gei.default_ledger_path()); os.makedirs(d)
        real = os.listdir
        def deny(path=".", *a, **k):
            if str(path) == d: raise PermissionError("simulated unreadable entry-intent directory")
            return real(path, *a, **k)
        gei.os.listdir = deny
        try: body, _ = c.run()
        finally: gei.os.listdir = real
        check("H6 unreadable directory (stub, uid-independent): UNKNOWN and 'not reported as clear'", "UNKNOWN" in body and "not reported as clear" in body)
def h7():
    with Case() as c:
        os.makedirs(c.home); L = gei.default_ledger_path()
        gei.write_record(L, _rec("GradMint1pump", "opA")); gei.write_record(L, _rec("GradMint1pumq", "opB")); d = gei.records_dir(L); before = _dsnap(d)
        st = gei.status_readonly(L)
        check("H7 same 12-char prefix: status data holds TWO distinct records (full mints differ)", len(st["records"]) == 2 and {r["mint"] for r in st["records"]} == {"GradMint1pump", "GradMint1pumq"})
        body, _ = c.run(); ls = _il(body); line = ls[0] if ls else ""
        check("H7 handler line: counted as 2 record file(s) by record, identical display hint shown twice (cannot tell them apart), neither full mint shown, directory byte-identical",
              len(ls) == 1 and "2 record file(s)" in line and line.count("GradMint1pum") == 2 and "GradMint1pump" not in line and "GradMint1pumq" not in line and _dsnap(d) == before)
def h8():
    import importlib
    m = sys.modules.get("daemon.launch_scanner.grad_entry_intent")
    check("H8 the package module imported here is the one in sys.modules under the package name (the handler's primary import path); bare 'grad_entry_intent' is not loaded", m is gei and "grad_entry_intent" not in sys.modules)

def main():
    for f in (h1, h2, h3, h4, h5, h6, h7, h8, h9):
        try: f()
        except Exception as e:
            import traceback
            check("%s aborted with an uncaught exception: %s: %s" % (f.__name__, type(e).__name__, str(e)[:160]), False); print("INFO traceback %s: %s" % (f.__name__, traceback.format_exc().strip().splitlines()[-3:]))
    print("FAILS: %d" % len(FAILS)); return 1 if FAILS else 0
if __name__ == "__main__":
    sys.exit(main())
