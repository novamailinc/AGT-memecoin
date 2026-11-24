"""Mimic curve-direct pricing — reserve parsing, price math, PDA
derivation, and the engine quote override. Plain script: run with
the repo venv python (never pytest). Pure tests are offline; the
override tests stub the curve module and the base quote so no
network is touched. Uses a temp FOREX_AGENT_HOME so no live state
is ever touched."""

import importlib.util
import os
import struct
import sys
import tempfile

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="mimic-curve-")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
_LS = os.path.join(_ROOT, "daemon", "launch_scanner")
sys.path.insert(0, _LS)
sys.path.insert(0, _ROOT)


def _load(name, path):
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__["__file__"] = path
    with open(path) as f:
        exec(compile(f.read(), path, "exec"), mod.__dict__)
    return mod


C = _load("mimic_curve_mod", os.path.join(_LS, "mimic_curve.py"))
M = _load("mimic_mod", os.path.join(_LS, "mimic.py"))

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# ---- reserve parsing (crafted account bytes) -------------------------------
def _curve_bytes(v_token, v_sol, complete=False):
    b = bytearray(49)
    struct.pack_into("<Q", b, 8, v_token)
    struct.pack_into("<Q", b, 16, v_sol)
    struct.pack_into("<Q", b, 24, 0)
    struct.pack_into("<Q", b, 32, 0)
    struct.pack_into("<Q", b, 40, 1000000000 * 10 ** 6)
    b[48] = 1 if complete else 0
    return bytes(b)


c = C.parse_bonding_curve(_curve_bytes(1073000000000000, 30000000000))
check("curve: parse v_token", c is not None
      and c["v_token"] == 1073000000000000)
check("curve: parse v_sol", c is not None
      and c["v_sol"] == 30000000000)
check("curve: parse complete False", c is not None
      and c["complete"] is False)
c2 = C.parse_bonding_curve(_curve_bytes(1, 1, complete=True))
check("curve: parse complete True", c2 is not None
      and c2["complete"] is True)
check("curve: parse short bytes rejected",
      C.parse_bonding_curve(b"\x00" * 20) is None)

# ---- price math (pure) ------------------------------------------------------
px = C.curve_price_from_reserves(1073000000000000, 30000000000, 6, 200.0)
check("curve: launch price math",
      px is not None and abs(px - 200.0 * 30.0 / 1073000000.0) < 1e-12)
check("curve: launch mcap lands near 5K (the observed call level)",
      px is not None and 4000.0 < px * 1000000000.0 < 7000.0)
check("curve: zero v_token rejected",
      C.curve_price_from_reserves(0, 30000000000, 6, 200.0) is None)
check("curve: zero sol/usd rejected",
      C.curve_price_from_reserves(1073000000000000, 30000000000,
                                  6, 0.0) is None)

# ---- PDA derivation (pure, deterministic) ------------------------------------
# Real 44-char pump mints (traced coins from the user's own history);
# short fake strings correctly decode to fewer than 32 bytes and are
# rejected, so they must not be used as PDA fixtures.
pda1 = C.derive_curve_pda("7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump")
pda1b = C.derive_curve_pda("7hfxEzFMAhdZ747772R7bpc7yhhfNYegmvSkHHo8pump")
pda2 = C.derive_curve_pda("9Ro2AeikFisoMqyE2HjcPhgUnXvZqRwZgRxtsgsLpump")
check("curve: PDA derives", pda1 is not None)
check("curve: PDA deterministic", pda1 == pda1b)
check("curve: PDA differs per mint", pda1 != pda2)
check("curve: PDA decodes to 32 bytes",
      pda1 is not None and len(C.b58decode(pda1)) == 32)
check("curve: PDA rejects garbage mint",
      C.derive_curve_pda("not-a-mint!!!") is None)

# ---- engine quote override (stubbed, offline) --------------------------------
check("mimic: curve module imported", M._curve is not None)
eng = M.make_engine()
check("mimic: quote override installed",
      getattr(eng.quote_price, "__name__", "") == "quote_with_curve")
check("mimic: base quote kept for fallback",
      callable(getattr(eng, "_base_quote_price", None)))


class _StubCurve:
    def __init__(self, px):
        self.px = px

    def curve_price_usd(self, mint):
        return self.px, "stub"


_real_curve = M._curve
try:
    M._curve = _StubCurve(0.00001)
    check("mimic: curve price wins over base quote",
          eng.quote_price("solana", "AbC123pump") == 0.00001)
    M._curve = _StubCurve(0.0)
    eng._base_quote_price = lambda c, m, s=None: 0.00002
    check("mimic: curve miss falls back to base quote",
          eng.quote_price("solana", "AbC123pump") == 0.00002)
finally:
    M._curve = _real_curve

# Actual central router, no Helius fallback even with a configured old key.
import json as _json
import tempfile as _tempfile
import urllib.error as _urlerr
from daemon.intelligence.acquisition import routine as _routine
from daemon.intelligence.acquisition.provider_router import ProviderRouter as _Router
class _Resp:
    def __init__(self,result): self.result=result
    def read(self): return _json.dumps({"jsonrpc":"2.0","id":1,"result":self.result}).encode()
    def __enter__(self): return self
    def __exit__(self,*a): return False
_calls=[]
def _transport(req,timeout=None):
    _calls.append(req.full_url.split('?')[0])
    if 'alchemy.com' in req.full_url:
        raise _urlerr.URLError('fixture outage')
    return _Resp({"value":"FALLBACK"})
os.environ['ALCHEMY_API_KEY']='fixture-not-real'
os.environ['HELIUS_API_KEY']='fixture-not-real'
os.environ.pop('CHAINSTACK_ENDPOINT',None)
_router=_Router(home=_tempfile.mkdtemp(),helius_enabled=False,transport=_transport)
_previous=getattr(_routine._local,'router',None)
_routine._local.router=_router
_routine.clear_evidence()
try:
    _result=C.rpc("getAccountInfo", ["X"])
    check("central router serves fallback account read",_result=={"value":"FALLBACK"})
    check("Alchemy primary, no Helius request",len(_calls)==2 and 'alchemy.com' in _calls[0] and all('helius' not in x for x in _calls))
    _ev=_routine.evidence_since()[-1]
    check("fallback retains degraded consumer evidence",_ev['provider']=='public' and _ev['degraded'] and _ev['decision_usable'])
finally:
    if _previous is None: del _routine._local.router
    else: _routine._local.router=_previous

# ---- wiring (file text, no side effects) --------------------------------------
with open(os.path.join(_LS, "mimic.py")) as f:
    mimic_text = f.read()
check("mimic: engine wires curve override",
      "quote_with_curve" in mimic_text and "mimic_curve" in mimic_text)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL MIMIC CURVE TESTS PASSED")
