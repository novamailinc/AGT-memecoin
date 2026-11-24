"""Test-only fixture for LEDGER-MECHANICS tests (cash conservation, op_id
retries, pos_id pinning, WAL). try_debit now refuses unless the account
gates are on with every mandatory limit set, so such tests run with gates on
and generous TEST-FIXTURE limits (not owner values) and with marks supplied
for open positions. The marks are at-cost and flat; this helper must NOT be
used by tests whose subject is marks trust, staleness, exposure, drawdown or
daily-loss: those build their own adversarial marks and limits. Nothing here
is importable from production code."""
import os, time

LIMITS = {"PAPER_ACCOUNT_GATES_ENABLED": "1",
          "PAPER_ACCOUNT_MAX_DAILY_LOSS_USD": "1000000",
          "PAPER_ACCOUNT_MAX_DRAWDOWN_PCT": "99",
          "PAPER_ACCOUNT_MAX_EXPOSURE_PCT": "100",
          "PAPER_ACCOUNT_MAX_SIMULTANEOUS": "1000",
          "PAPER_ACCOUNT_MAX_POSITIONS": "1000",
          "PAPER_ACCOUNT_MAX_DATA_AGE_S": "100000",
          "PAPER_ACCOUNT_MAX_TOTAL_EXPOSURE_USD": "1000000",
          "PAPER_ACCOUNT_MAX_TOKEN_EXPOSURE_USD": "1000000"}


def set_env():
    os.environ.update(LIMITS)


def at_cost_marks(pa):
    out = {}
    snap = pa.snapshot()
    if not snap:  # no account yet: nothing open, nothing to mark
        return out
    for lane, info in snap["lanes"].items():
        for p in info["positions"]:
            out.setdefault(lane, {})[p["mint"]] = {
                "value": p["basis"], "ts": time.time(),
                "source": "test-fixture-at-cost", "qty": p["qty"],
                "pos_id": p["pos_id"]}
    return out


def install(pa):
    """Call after every reload of paper_account."""
    set_env()
    install_price_feed(at_cost=True)   # real _central_marks/collect_marks pipeline
    return pa


NET_ATTEMPTS = []
_saved = {}


REQUESTERS = set()   # module names that asked for the block (pytest scoping)


def block_network():
    """Refuse every outbound connection at the transport boundary and record
    the attempt (host only) on stderr as 'NETBLOCK <host>'. Idempotent.
    Tests must never reach a provider."""
    import socket, sys, urllib.request
    try:
        REQUESTERS.add(sys._getframe(1).f_globals.get("__name__", ""))
    except Exception:
        pass
    if getattr(socket, "_agt_blocked", False):
        return
    def _rec(target):
        host = str(target)[:80]
        NET_ATTEMPTS.append(host)
        print("NETBLOCK " + host, file=sys.stderr)
    def _urlopen(req, *a, **k):
        url = getattr(req, "full_url", req)
        _rec(str(url).split("?")[0])
        raise OSError("network blocked in tests")
    def _cc(address, *a, **k):
        _rec(address[0] if isinstance(address, tuple) else address)
        raise OSError("network blocked in tests")
    _saved.update(urlopen=urllib.request.urlopen,
                  create_connection=socket.create_connection,
                  connect=socket.socket.connect,
                  connect_ex=socket.socket.connect_ex)
    urllib.request.urlopen = _urlopen
    socket.create_connection = _cc
    _orig = socket.socket.connect
    def _connect(self, address):
        if isinstance(address, tuple):
            _rec(address[0]); raise OSError("network blocked in tests")
        return _orig(self, address)
    socket.socket.connect = _connect
    _orig_ex = socket.socket.connect_ex
    def _connect_ex(self, address):
        if isinstance(address, tuple):
            _rec(address[0]); raise OSError("network blocked in tests")
        return _orig_ex(self, address)
    socket.socket.connect_ex = _connect_ex
    socket._agt_blocked = True


def unblock_network():
    """Restore the exact pre-block transport functions (cross-file hygiene)."""
    import socket, urllib.request
    if not getattr(socket, "_agt_blocked", False):
        return
    urllib.request.urlopen = _saved["urlopen"]
    socket.create_connection = _saved["create_connection"]
    socket.socket.connect = _saved["connect"]
    socket.socket.connect_ex = _saved["connect_ex"]
    socket._agt_blocked = False
    _saved.clear()


def set_limits_env():
    """Gates on plus generous TEST-FIXTURE limits (not owner values)."""
    set_env()


class _Feed:
    def __init__(self):
        self.table = {}
        self._saved = None
    def set(self, table):
        self.table = dict(table)
    def uninstall(self):
        if self._saved:
            from daemon.launch_scanner import jup_price, account_marks
            jup_price.get_prices_observed, account_marks._default_curve_price = self._saved
            self._saved = None


def install_price_feed(at_cost=False):
    """Inject prices at the seam production uses (jup_price.get_prices_observed).
    paper_account._central_marks and account_marks.collect_marks stay REAL, so
    freshness / provenance / qty / pos_id validation run unmodified. The curve
    fallback is denied (raises) so no unpriced mint can reach a provider.
    Returns a feed; feed.set({mint: (usd_price, obs_ts)}); feed.uninstall()."""
    from daemon.launch_scanner import jup_price, account_marks
    feed = _Feed()
    feed._saved = (jup_price.get_prices_observed, account_marks._default_curve_price)
    def _prices(mints, now=None):
        if at_cost:   # MECHANICS ONLY: flat unit price = remaining basis / qty
            snap = account_marks.pa.snapshot() or {"lanes": {}}
            t = {}
            for info in snap["lanes"].values():
                for p in info["positions"]:
                    if p["qty"] > 0 and p["mint"] in mints:
                        t[p["mint"]] = (p["basis"] / p["qty"], time.time())
            t.update(feed.table)
            return {m: t[m] for m in mints if m in t}
        return {m: feed.table[m] for m in mints if m in feed.table}
    def _no_curve(mint):
        raise RuntimeError("curve source denied in tests")
    jup_price.get_prices_observed = _prices
    account_marks._default_curve_price = _no_curve
    return feed


def admit_registry_for_fixture(state="MAINNET_CANARY"):
    """TEST FIXTURE ONLY (disclosed 2026-11-24): executor arming/sending now requires the
    qualification registry to be in a mainnet-admitted state. Tests that exercise the ARMED
    path with fake RPC/Jupiter write that state into a throwaway FOREX_AGENT_HOME. This is not a
    waiver in production code and not an approval mechanism; no real registry can reach it."""
    import os, tempfile
    from core.qualification import Registry
    home = tempfile.mkdtemp(prefix="qual-fixture-")
    os.environ["FOREX_AGENT_HOME"] = home
    Registry(home)._write({"v": 1, "state": state,
                           "binding": {"strategy_version": "fx", "config_hash": "fx", "data_period": "fx"},
                           "evidence": {}, "audit": []})
    # DISCLOSED (2026-11-24): owner admission (core.qualification_admission) now also gates arming/sending;
    # no authorization artifact exists, so armed-path tests with fake RPC/Jupiter replace ONLY that
    # owner-admission step for this process. Registry-label and marker checks stay real.
    from broker.memecoin.execution import mainnet_executor as _M
    _M.MainnetExecutor._require_owner_admission = lambda self, st: None
    return home


def stamped_quote(mod, fn):
    """TEST FIXTURE (disclosed): a quote_price stub gets the observation stamp a real priced quote carries, taken at
    CALL time, so the entry age gate sees a fresh stamped quote. Gate untouched; stale/unstamped cases set their own."""
    import time as _t
    def _q(*a, **k):
        px = fn(*a, **k)
        if hasattr(mod, "_LAST_QUOTE_STAMP"):
            _n = _t.time()
            mod._LAST_QUOTE_STAMP = {"fetched_at": _n, "received_at": _n} if px and px > 0 else None
        return px
    return _q


def stamped_const(mod, price):
    """TEST FIXTURE (disclosed): constant-price quote stub with the call-time observation stamp (see stamped_quote)."""
    return stamped_quote(mod, lambda *a, **k: price)
