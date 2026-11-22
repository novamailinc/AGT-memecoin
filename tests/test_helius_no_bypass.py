#!/usr/bin/env python3
"""Guard: no Helius HTTP RPC call may bypass the fleet pacer.

2026-11-18: the Nov 16-17 penalty box came from UNPACED hammering
(~3,000 429s/hour). The pacer (2026-11-17) is the ONE admission path
for every Helius call: 1 token / 20s / key, shared cross-process.
This test fails if any file under daemon/ or scripts/ gains a Helius
URL outside the allowlist below, or if a paced caller loses its
pacer_acquire. The allowlist is the human-verified verdict of
2026-11-18 — a new Helius usage must be added deliberately, never
by accident.

Dormant files stay allowlisted ONLY as dormant: reviving one
requires pacing it first and flipping its entry to calls=True.

Plus unit tests for scripts/nightly-grader's paced rpc_call (zero
network, zero live state): fail-fast on no token, success reports
True, 429 reports False + raises QuotaExhausted, pacer-unavailable
refuses all calls.

Run: python3 tests/test_helius_no_bypass.py
"""
import importlib.util
import io
import json
import os
import sys
import urllib.error

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# relpath -> (makes_helius_http_calls, reason)
# calls=True means the file itself issues HTTP requests to Helius and
# MUST contain a pacer_acquire. calls=False means it only builds a
# Helius URL (handed to a paced caller), holds config, uses WSS only,
# or is dormant.
ALLOWLIST = {
    "daemon/intelligence/acquisition/helius_parsed_events.py": (
        False, "NOT LIVE — Parsed Events REST client calls Helius "
        "HTTPS directly via its injected opener with NO pacer token "
        "and NO pacer_report on 429; gated by selective_use_check "
        "and imported by no running daemon (three-provider program, "
        "shadow-only). MUST be paced through the shared pacer "
        "before any live use"),
    "daemon/intelligence/acquisition/helius_preprocessed.py": (
        False, "pure decoding/state, opens no socket and makes no "
        "HTTP calls; WSS host appears in docstring only"),
    "daemon/intelligence/acquisition/helius_streams.py": (
        False, "filter builders + notification parsing only; NOT "
        "LIVE, no Helius connection made; WSS host appears in "
        "templates/docstrings only"),
    "daemon/intelligence/acquisition/provider_capabilities.py": (
        False, "capability matrix data only; endpoint strings are "
        "data, no calls"),
    "daemon/intelligence/acquisition/providers/base.py": (
        False, "generic JSON-RPC transport for all providers "
        "(Helius/Alchemy/Chainstack/public); pacing is the caller's "
        "job — the Helius adapter goes through paced reserves.rpc"),
    "daemon/launch_scanner/reserves.py": (
        True, "paced choke point: rpc() acquires a token before "
        "_rpc_attempt"),
    "daemon/launch_scanner/logstream.py": (
        True, "paced _http_rpc"),
    "daemon/launch_scanner/deployers.py": (
        True, "paced _rpc"),
    "daemon/launch_scanner/funding_clusters.py": (
        True, "paced _rpc"),
    "daemon/launch_scanner/instant_safety.py": (
        True, "paced _rpc"),
    "daemon/launch_scanner/mimic_curve.py": (
        True, "paced rpc"),
    "daemon/launch_scanner/smartmoney.py": (
        True, "paced _rpc / discovery_rpc"),
    "daemon/telegram/commands.py": (
        True, "paced _verify_helius_key"),
    "daemon/launch_scanner/helius_vault.py": (
        False, "builds a surrogate Helius URL only; makes no HTTP "
        "calls itself. WIRED into the live path 2026-11-20 "
        "(re-probe returned HTTP 200 — the 2026-11-18 401 is "
        "gone); reserves.rpc_url_pairs appends it as a third "
        "Helius key source, fail-soft to disk keys"),
    "daemon/launch_scanner/__init__.py": (
        False, "builds the Helius URL only to hand it to the paced "
        "LogStream; its own urlopen calls are Telegram/DexScreener"),
    "daemon/launch_scanner/armed_watch.py": (
        True, "WSS handshake paced since 2026-11-22 "
        "(pacer_acquire before connect, pacer_report on a 429 "
        "handshake); no HTTP RPC urlopen"),
    "daemon/launch_scanner/robust_http.py": (
        False, "per-host timeout config only; no Helius calls"),
    "scripts/nightly-grader": (
        True, "paced rpc_call (2026-11-18 fix); cron currently "
        "disabled, last run 2026-11-14"),
    "scripts/spark-feed": (
        False, "hands Helius URLs to the paced LogStream; makes no "
        "HTTP calls itself"),
    "scripts/smart-watcher": (
        False, "DORMANT — killed 2026-10-29, not scheduled; must be "
        "paced before any revival"),
    "scripts/smart-discover": (
        False, "DORMANT — research script, not scheduled; must be "
        "paced before any revival"),
    "scripts/launch-radar": (
        False, "DORMANT — last run 2026-10-18, no pidfile/process/"
        "cron; carries its OWN local 429 guard, not the shared "
        "pacer — revival requires routing it through the shared "
        "pacer first"),
    "scripts/degen-ws-listener": (
        False, "DORMANT — degen killed 2026-10-30, not scheduled; "
        "must be paced before any revival"),
}

CALL_PATTERNS = ("urlopen", "httpx.", "requests.", "curl ")


def _iter_files():
    for base in ("daemon", "scripts"):
        root = os.path.join(REPO_ROOT, base)
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(".pyc") or fn == "__pycache__":
                    continue
                yield os.path.join(dirpath, fn)


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="strict") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return ""


def test_allowlist_complete():
    """Every file mentioning the Helius RPC host is allowlisted."""
    found = {}
    for path in _iter_files():
        text = _read(path)
        if "helius-rpc.com" in text:
            rel = os.path.relpath(path, REPO_ROOT)
            found[rel] = text
    unknown = sorted(set(found) - set(ALLOWLIST))
    check("no unallowlisted file mentions helius-rpc.com "
          "(unknown: %s)" % unknown, not unknown)
    for rel, (calls, _reason) in ALLOWLIST.items():
        path = os.path.join(REPO_ROOT, rel)
        text = _read(path)
        check("allowlisted file still exists + mentions host: %s" % rel,
              bool(text) and "helius-rpc.com" in text)
        if calls:
            # Paced callers acquire a token either via the module
            # helper pacer_acquire(...) or via the SharedPacer
            # method pacer.acquire(...) (reserves.py uses the latter
            # through its _pacer() helper).
            check("paced caller still acquires a token: %s" % rel,
                  "pacer_acquire" in text or "pacer.acquire(" in text)
        else:
            # Tripwire: a calls=False file must not gain a Helius
            # HTTP call on a single line (URL + urlopen together).
            bad = [ln for ln in text.splitlines()
                   if "helius" in ln.lower()
                   and any(p in ln for p in CALL_PATTERNS)]
            check("non-caller has no same-line Helius call: %s" % rel,
                  not bad)
    # The grader regression, stated plainly.
    grader = _read(os.path.join(REPO_ROOT, "scripts", "nightly-grader"))
    check("nightly-grader rpc_call is paced (regression 2026-11-18)",
          "pacer_acquire" in grader and "pacer_report" in grader)


def _load_grader():
    # No .py extension, so importlib cannot infer a loader — pass one.
    from importlib.machinery import SourceFileLoader
    path = os.path.join(REPO_ROOT, "scripts", "nightly-grader")
    loader = SourceFileLoader("nightly_grader", path)
    spec = importlib.util.spec_from_loader("nightly_grader", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeResp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_grader_rpc_call():
    """Grader rpc_call: paced, fail-fast, reports outcomes. No network."""
    import urllib.request
    mod = _load_grader()
    real_open = urllib.request.urlopen
    real_acquire = mod.pacer_acquire
    real_report = mod.pacer_report
    real_bucket = mod.bucket_id_for_key
    real_shed = mod.background_call_allowed
    real_shed_log = mod.log_backoff_episode_once
    try:
        # The shed gate must not read live guard state in tests.
        mod.background_call_allowed = lambda bucket: True
        mod.log_backoff_episode_once = lambda *a, **k: None
        # --- A: no token -> QuotaExhausted, zero HTTP attempts ---
        called = []
        mod.pacer_acquire = lambda bucket, wait_s=None: False
        mod.bucket_id_for_key = lambda key: "test-bucket"
        urllib.request.urlopen = lambda *a, **k: called.append(1) or (_ for _ in ()).throw(  # noqa: E501
            AssertionError("urlopen must not be called without a token"))
        try:
            mod.rpc_call("K" * 10, "getSignaturesForAddress", [])
            check("grader: no token raises QuotaExhausted", False)
        except mod.QuotaExhausted:
            check("grader: no token raises QuotaExhausted", True)
        check("grader: no token makes zero HTTP attempts", called == [])

        # --- B: token + 200 -> returns result, reports True ---
        reports = []
        mod.pacer_acquire = lambda bucket, wait_s=None: True
        mod.pacer_report = lambda bucket, ok: reports.append((bucket, ok))
        urllib.request.urlopen = lambda *a, **k: _FakeResp(
            {"result": {"ok": 1}})
        got = mod.rpc_call("K" * 10, "getSignaturesForAddress", [])
        check("grader: success returns result", got == {"ok": 1})
        check("grader: success reports (bucket, True)",
              reports == [("test-bucket", True)])

        # --- C: token + HTTP 429 -> QuotaExhausted, reports False ---
        reports.clear()
        mod.pacer_acquire = lambda bucket, wait_s=None: True

        def _raise_429(*a, **k):
            raise urllib.error.HTTPError(
                "https://example.invalid", 429, "Too Many Requests",
                {}, None)

        urllib.request.urlopen = _raise_429
        try:
            mod.rpc_call("K" * 10, "getSignaturesForAddress", [])
            check("grader: HTTP 429 raises QuotaExhausted", False)
        except mod.QuotaExhausted:
            check("grader: HTTP 429 raises QuotaExhausted", True)
        check("grader: HTTP 429 reports (bucket, False)",
              reports == [("test-bucket", False)])

        # --- D: pacer unavailable -> refuse all calls ---
        mod.pacer_acquire = real_acquire
        mod.pacer_report = real_report
        mod.bucket_id_for_key = real_bucket
        mod._PACER_OK = False
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("urlopen must not be called, pacer down"))
        try:
            mod.rpc_call("K" * 10, "getSignaturesForAddress", [])
            check("grader: pacer down refuses calls", False)
        except mod.QuotaExhausted:
            check("grader: pacer down refuses calls", True)
        mod._PACER_OK = True

        # --- E: auto-shed (2026-11-18): bucket in backoff -> the
        # grader stops attempting entirely, zero HTTP attempts ---
        mod.pacer_acquire = lambda bucket, wait_s=None: True
        mod.bucket_id_for_key = lambda key: "test-bucket"
        mod.background_call_allowed = lambda bucket: False
        shed_logged = []
        mod.log_backoff_episode_once = \
            lambda *a, **k: shed_logged.append(1)
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("urlopen must not be called while shed"))
        try:
            mod.rpc_call("K" * 10, "getSignaturesForAddress", [])
            check("grader: shed raises QuotaExhausted", False)
        except mod.QuotaExhausted:
            check("grader: shed raises QuotaExhausted", True)
        check("grader: shed logs the episode once",
              shed_logged == [1])
    finally:
        urllib.request.urlopen = real_open
        mod.pacer_acquire = real_acquire
        mod.pacer_report = real_report
        mod.bucket_id_for_key = real_bucket
        mod.background_call_allowed = real_shed
        mod.log_backoff_episode_once = real_shed_log
        mod._PACER_OK = True


def main():
    test_allowlist_complete()
    test_grader_rpc_call()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL NO-BYPASS TESTS PASSED")


if __name__ == "__main__":
    main()
