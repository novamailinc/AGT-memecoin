"""Targeted test: mandatory_entry_refusal binds the PAPER_ACCOUNT_ENABLED branch and
each fail-closed branch, with a positive control. Script style (PASS/FAIL, exit code)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FOREX_AGENT_HOME", __import__("tempfile").mkdtemp(prefix="eg-home-"))
from daemon.launch_scanner import entry_guard as eg
from daemon.launch_scanner import paper_account as pa

fails = 0
def check(name, cond):
    global fails
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails += 1

def with_env(val):
    if val is None:
        os.environ.pop("PAPER_ACCOUNT_ENABLED", None)
    else:
        os.environ["PAPER_ACCOUNT_ENABLED"] = val

saved = (pa.gates_enabled, pa.max_daily_loss_usd, pa.max_drawdown_pct, pa.max_exposure_pct)
def good():
    pa.gates_enabled = lambda: True
    pa.max_daily_loss_usd = lambda: 1.0
    pa.max_drawdown_pct = lambda: 5.0
    pa.max_exposure_pct = lambda: 50.0

good()
with_env("1")
check("positive control: enabled + gates on + all limits set -> no refusal", eg.mandatory_entry_refusal("t") is None)
for v, label in ((None, "unset"), ("0", "0"), ("", "empty"), ("true", "true (only '1' enables)"), (" 1", "' 1'")):
    with_env(v)
    r = eg.mandatory_entry_refusal("t")
    check("PAPER_ACCOUNT_ENABLED %s -> refused (not enabled)" % label, bool(r) and "not enabled" in r)
with_env("1")
pa.gates_enabled = lambda: False
r = eg.mandatory_entry_refusal("t")
check("gates off -> refused", bool(r) and "gates are off" in r)
good(); pa.max_drawdown_pct = lambda: None
r = eg.mandatory_entry_refusal("t")
check("unset limit -> refused naming it", bool(r) and "max_drawdown_pct" in r)
good()
def boom(): raise RuntimeError("x")
pa.max_exposure_pct = boom
r = eg.mandatory_entry_refusal("t")
check("limit resolution error -> refused fail-closed", bool(r) and "fail-closed" in r)
pa.gates_enabled, pa.max_daily_loss_usd, pa.max_drawdown_pct, pa.max_exposure_pct = saved
print("FAILS: %d" % fails)
sys.exit(1 if fails else 0)
