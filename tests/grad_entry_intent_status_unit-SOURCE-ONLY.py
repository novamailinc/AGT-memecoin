#!/usr/bin/env python3
"""SOURCE ONLY, UNEXECUTED (written, not run). Needs main's explicit clearance for this exact blob before any run. Stdlib only; loads daemon/launch_scanner/grad_entry_intent.py by path; temp dirs; no network, no children.
Unit evidence for the READ-ONLY status/account visibility helpers (status_readonly, default_ledger_path, format_status_lines). The Telegram handler wiring (daemon/telegram/commands.py _entry_intent_lines) is a thin
call of these and is NOT exercised here. Asserts: clear adds no lines; pending and unknown are labeled and distinct from a cycle fault; a read failure is never "clear"; no file is created, removed or altered by status_readonly."""
import hashlib, importlib.util, os, sys, tempfile
HERE = os.path.dirname(os.path.abspath(__file__)); REPO = os.path.dirname(HERE)
_s = importlib.util.spec_from_file_location("gei", os.path.join(REPO, "daemon", "launch_scanner", "grad_entry_intent.py"))
M = importlib.util.module_from_spec(_s); _s.loader.exec_module(M)
FAILS = []
def check(n, ok):
    print(("PASS " if ok else "FAIL ") + n)
    if not ok: FAILS.append(n)
def home():
    d = tempfile.mkdtemp(prefix="geis_"); return os.path.join(d, "grad_ledger.json")
def snap(d):
    out = {}
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        out[n] = hashlib.sha256(open(os.path.join(d, n), "rb").read()).hexdigest()
    return out
def rec():
    pos = {"mint": "GradMint1pump", "tokens": 1.0, "entry_price": 1e-05, "entry_ts": 1.0, "legs": {}, "entry_features": {}, "execution": {"entry_op": "op1"}}
    return M.build_record("grad", "GradMint1pump", "op1", 1.0, 1.0, pos, {"qty_tokens": 1.0, "price_usd": 1e-05}, {"entry_day": "2026-11-24"})
check("default_ledger_path: FOREX_AGENT_HOME honored", M.default_ledger_path({"FOREX_AGENT_HOME": "/x"}) == "/x/grad_ledger.json")
check("default_ledger_path: fallback is ~/.forex-agent/grad_ledger.json", M.default_ledger_path({}) == os.path.join(os.path.expanduser("~/.forex-agent"), "grad_ledger.json"))
L = home(); s0 = M.status_readonly(L)
check("status: missing directory with readable parent is clear, and nothing is created", s0["state"] == "clear" and not os.path.exists(M.records_dir(L)))
check("format: clear adds NO lines (default output unchanged)", M.format_status_lines(s0, True) == [] and M.format_status_lines(s0, False) == [])
M.write_record(L, rec()); d = M.records_dir(L); before = snap(d); s1 = M.status_readonly(L)
check("status: one valid record -> pending, op/mint reported, valid True", s1["state"] == "pending" and s1["records"][0]["op_id"] == "op1" and s1["records"][0]["mint"] == "GradMint1pump" and s1["records"][0]["valid"] is True)
check("status: read-only (directory bytes identical after the call)", snap(d) == before)
open(os.path.join(d, "x.tmp"), "w").write("{"); b2 = snap(d); s2 = M.status_readonly(L)
check("status: a stale tmp is REPORTED (invalid) and NOT removed (scan() would remove it)", any(r["file"] == "x.tmp" and r["valid"] is False for r in s2["records"]) and snap(d) == b2)
open(os.path.join(d, "bad.json"), "w").write("{nope"); s3 = M.status_readonly(L)
check("status: unparseable record is pending-invalid, never clear", s3["state"] == "pending" and any(r["file"] == "bad.json" and r["valid"] is False for r in s3["records"]))
L2 = home(); os.makedirs(M.records_dir(L2)); s4 = M.status_readonly(L2)
check("status: present and empty directory is clear", s4["state"] == "clear")
L3 = home(); os.makedirs(M.records_dir(L3)); os.chmod(M.records_dir(L3), 0)
try: s5 = M.status_readonly(L3)
finally: os.chmod(M.records_dir(L3), 0o700)
check("status: unreadable directory is UNKNOWN, not clear", s5["state"] == "unknown" and "unreadable" in (s5["why"] or ""))
check("status: unreadable ledger parent is UNKNOWN", M.status_readonly("/nonexistent-root-xyz/sub/grad_ledger.json")["state"] == "unknown")
lines = M.format_status_lines(s1, False)
check("format pending, flag off: one labeled line, says unrecovered entry NOT an ordinary cycle fault, says flag OFF here and that a trader process may differ", len(lines) == 1 and "unrecovered entry" in lines[0] and "not an ordinary cycle fault" in lines[0] and "OFF" in lines[0] and "trader process" in lines[0])
check("format pending shows recorded state even though the flag is off (not hidden)", "GradMint1pump" in lines[0])
check("format pending, flag on: says ON", "ON" in M.format_status_lines(s1, True)[0] and "OFF" not in M.format_status_lines(s1, True)[0])
bad = M.format_status_lines(s3, True)[0]
check("format: invalid/unreadable record labeled INVALID/UNREADABLE", "INVALID/UNREADABLE" in bad)
u = M.format_status_lines(s5, True)
check("format unknown: UNKNOWN and 'not reported as clear'", len(u) == 1 and "UNKNOWN" in u[0] and "not reported as clear" in u[0])
check("format: non-dict input is treated as no lines (the handler wraps failures separately as UNKNOWN)", M.format_status_lines(None, True) == [])
print("FAILS: %d" % len(FAILS)); sys.exit(1 if FAILS else 0)
