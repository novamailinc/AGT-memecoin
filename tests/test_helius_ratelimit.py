"""Tests for the shared Helius rate-limit guard.

2026-11-17 (pacing fix): the legacy in-process guard below is
unchanged, but smartmoney/deployers no longer gate on it — they
take tokens from the fleet-wide pacer (helius_ratelimit.SharedPacer),
which also mirrors outcomes into the shared guard file. The four
RPC tests at the bottom drive the pacer (temp home per test, so no
live state file is touched)."""
import os
import tempfile
import time
import urllib.error

from daemon.launch_scanner import helius_ratelimit as rl


def setup_function(_):
    rl.reset_for_tests()
    home = tempfile.mkdtemp(prefix="rl-test-")
    os.environ["FOREX_AGENT_HOME"] = home
    rl._PACER = None
    rl._SHARED_GUARD = None


def _expire_pacer_cooldown(bucket):
    """Set one pacer bucket's cooldown into the past (simulates the
    backoff lapsing without waiting 30s+ of wall time).

    Also expires the guard mirror: in production the pacer mirrors
    the same `until` into helius_guard.json (set_cooldown), so the
    two lapse together — expiring only the pacer file would leave
    a stale guard cooldown no real run can produce, and the
    auto-shed (which reads the guard) would rightly block."""
    import json
    home = os.environ["FOREX_AGENT_HOME"]
    path = os.path.join(home, "helius_pacer.json")
    with open(path) as fh:
        data = json.load(fh)
    data["buckets"][bucket]["cooldown_until"] = time.time() - 1.0
    with open(path, "w") as fh:
        json.dump(data, fh)
    gpath = os.path.join(home, "helius_guard.json")
    try:
        with open(gpath) as fh:
            gdata = json.load(fh)
        if bucket in (gdata.get("buckets") or {}):
            gdata["buckets"][bucket]["until"] = time.time() - 1.0
            with open(gpath, "w") as fh:
                json.dump(gdata, fh)
    except Exception:
        pass


def test_clear_by_default():
    assert rl.cooldown_remaining() == 0.0


def test_ratelimit_engages_cooldown():
    wait = rl.report_ratelimited()
    assert wait == 60.0
    assert rl.cooldown_remaining() > 50.0


def test_backoff_doubles_to_cap():
    waits = [rl.report_ratelimited() for _ in range(6)]
    assert waits == [60.0, 120.0, 240.0, 480.0, 600.0, 600.0]


def test_success_resets_backoff():
    rl.report_ratelimited()
    rl.report_ratelimited()
    rl.report_success()
    assert rl.report_ratelimited() == 60.0


def test_cooldown_expires():
    rl.report_ratelimited(now=time.time() - 61.0)
    assert rl.cooldown_remaining() == 0.0


def _fake_429(*a, **k):
    raise urllib.error.HTTPError("http://x", 429, "Too Many Requests",
                                 {}, None)


def _fake_ok(*a, **k):
    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"result": {"ok": true}}'
    return R()


def test_smartmoney_rpc_respects_pacer_cooldown(monkeypatch):
    import daemon.launch_scanner.reserves as sm
    monkeypatch.setattr(sm, "rpc_url_pairs", lambda: [("https://mainnet.helius-rpc.com/?api-key=k", rl.bucket_id_for_key("k"))])
    bucket = rl.bucket_id_for_key("k")
    rl.pacer_report(bucket, False)  # engage pacer cooldown (~30s)
    calls = []
    monkeypatch.setattr(sm.urllib.request, "urlopen",
                        lambda *a, **k: calls.append(1) or _fake_ok())
    assert sm.helius_rpc_typed("getSignaturesForAddress", []) == (None, "error")
    assert calls == []  # no HTTP during pacer cooldown


def test_smartmoney_rpc_429_engages_pacer_cooldown(monkeypatch):
    import daemon.launch_scanner.reserves as sm
    monkeypatch.setattr(sm, "rpc_url_pairs", lambda: [("https://mainnet.helius-rpc.com/?api-key=k", rl.bucket_id_for_key("k"))])
    monkeypatch.setattr(sm, "_rpc_attempt", lambda *a,**k: (None,"429",.001))
    assert sm.helius_rpc_typed("getSignaturesForAddress", []) == (None, "error")
    bucket = rl.bucket_id_for_key("k")
    st = rl.pacer()._read_state()[bucket]
    assert st["cooldown_until"] > time.time()  # pacer backoff engaged
    assert rl.cooldown_remaining_for(bucket) > 20.0  # mirrored to guard


def test_smartmoney_rpc_success_clears_pacer_backoff(monkeypatch):
    import daemon.launch_scanner.reserves as sm
    monkeypatch.setattr(sm, "rpc_url_pairs", lambda: [("https://mainnet.helius-rpc.com/?api-key=k", rl.bucket_id_for_key("k"))])
    bucket = rl.bucket_id_for_key("k")
    rl.pacer_report(bucket, False)  # 30s ladder
    _expire_pacer_cooldown(bucket)
    rl.pacer_report(bucket, False)  # doubles to 60s (probe 429)
    assert rl.pacer()._read_state()[bucket]["backoff_s"] == 60.0
    _expire_pacer_cooldown(bucket)
    monkeypatch.setattr(sm, "_rpc_attempt", lambda *a,**k: ({"ok":True},"ok",.001))
    assert sm.helius_rpc_typed("getSignaturesForAddress", []) == ({"ok": True}, "ok")
    # ladder was reset by success: the next 429 engages base 30s again
    rl.pacer_report(bucket, False)
    assert rl.pacer()._read_state()[bucket]["backoff_s"] == 30.0


def test_deployer_ordinary_rpc_uses_central_budget_not_helius_bucket(monkeypatch):
    import daemon.launch_scanner.deployers as dp
    from daemon.intelligence.acquisition import routine
    calls=[]
    monkeypatch.setattr(routine,'read',lambda *a,**k: calls.append((a,k)) or {'ok':True})
    assert dp._rpc('getSignaturesForAddress',[])=={'ok':True}
    assert calls[0][1]['background'] is True
