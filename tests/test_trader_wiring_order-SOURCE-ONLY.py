"""UNEXECUTED DRAFT. TEXT-ONLY (ast.parse of script sources; imports and runs NO project code, no child, no thread). Pins, for each launched grad lane script, that
grad_trader_cycle.register(...) is called in main() BEFORE the first call that can start a thread or poller (Thread, .start, ArmedWatcher, ExitPoller) and before the
first poll_exits call, so an abort cannot occur on an unhooked engine or be folded into the registration baseline. Also pins which launched lane scripts register at all
and lists those that do not (informational; tranche traders are NOT wired). Static order only: it proves nothing about runtime behavior."""
import ast, os, sys
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
FAILS = 0
def check(name, ok):
    global FAILS
    print(("PASS " if ok else "FAIL ") + name)
    if not ok: FAILS += 1
def main_calls(path):
    tree = ast.parse(open(os.path.join(ROOT, path)).read())
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"][0]
    reg, early, polls = [], [], []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call): continue
        f = n.func; name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
        base = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
        if name == "register" and base == "_gtc": reg.append(n.lineno)
        elif name in ("Thread", "start", "ArmedWatcher", "ExitPoller"): early.append(n.lineno)
        elif name == "poll_exits": polls.append(n.lineno)
    return reg, early, polls
for path in ("scripts/grad-select-trader", "scripts/grad-raw-trader", "scripts/grad-trader"):
    reg, early, polls = main_calls(path)
    first = min(early + polls) if (early or polls) else None
    check("W-%s: exactly one _gtc.register in main (lines %s)" % (os.path.basename(path), reg), len(reg) == 1)
    check("W-%s: register (line %s) precedes the first thread/poller/poll_exits call (line %s)" % (os.path.basename(path), reg[:1], first), len(reg) == 1 and (first is None or reg[0] < first))

def scan_and_exit_shape(path):
    """Every scan_once call in main() must sit inside a lambda or a nested def (never as a bare main-loop statement), and the main loop must route through
    _gtc.main_cycle (exits first when the flag is on). Returns (bare_scan_lines, main_cycle_lines, entry_scan_lines)."""
    tree = ast.parse(open(os.path.join(ROOT, path)).read())
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"][0]
    bare, mc, es = [], [], []
    def visit(node, inside):
        for ch in ast.iter_child_nodes(node):
            nested = inside or isinstance(ch, (ast.Lambda, ast.FunctionDef))
            if isinstance(ch, ast.Call):
                f = ch.func; nm = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                if nm == "scan_once" and not inside: bare.append(ch.lineno)
                if nm == "main_cycle": mc.append(ch.lineno)
                if nm == "entry_scan": es.append(ch.lineno)
            visit(ch, nested)
    visit(fn, False)
    return bare, mc, es
for path, want_entry in (("scripts/grad-select-trader", True), ("scripts/grad-raw-trader", True), ("scripts/grad-trader", False)):
    bare, mc, es = scan_and_exit_shape(path)
    b = os.path.basename(path)
    check("X-%s: no bare scan_once in main (scan only inside lambda/def, lines %s)" % (b, bare), bare == [])
    check("X-%s: main loop routes through _gtc.main_cycle (lines %s)" % (b, mc), len(mc) == 1)
    check("X-%s: entry thread %s _gtc.entry_scan (lines %s)" % (b, "uses" if want_entry else "does not exist, no", es), (len(es) == 1) == want_entry)
    src = open(os.path.join(ROOT, path)).read()
    check("X-%s: flag-on with missing wiring module refuses entries (log text present) and still polls exits" % b, "wiring missing" in src and "AGT_GRAD_CYCLE_ENABLED" in src)
for path in ("scripts/tranche-trader", "scripts/tranche-protect-trader", "scripts/grad-select-live-trader"):
    src = open(os.path.join(ROOT, path)).read()
    print("INFO %s registers grad_trader_cycle: %s" % (path, "_gtc.register" in src or "grad_trader_cycle" in src))
print("FAILS: %d" % FAILS); sys.exit(1 if FAILS else 0)
