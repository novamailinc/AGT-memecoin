#!/usr/bin/env python3
"""Robust HTTP client for the bot stack.

Replaces fragile one-shot urllib calls with:
- Exponential backoff with jitter (no thundering herd)
- Circuit breaker (stop hammering a dead API)
- Error classification (retry transient, fail fast on permanent)
- Per-host rate-limit tracking
- Connection reuse via a shared opener

Usage:
    from robust_http import get_json
    data = get_json("https://api.geckoterminal.com/...")
"""
import json
import random
import time
import gzip
import shutil
import subprocess
import urllib.request
import urllib.error
import http.client
from collections import defaultdict

# ---- per-host state ----
_last_call = defaultdict(float)   # host -> timestamp of last call
_circuit_open = {}                # host -> timestamp when circuit closes
_circuit_fails = defaultdict(int)

# Tunables
MIN_GAP = {                       # min seconds between calls per host
    "api.geckoterminal.com": 1.0,
    "api.dexscreener.com": 0.5,
    "api.rugcheck.xyz": 1.0,
    "mainnet.helius-rpc.com": 0.3,
}
CIRCUIT_THRESHOLD = 5             # consecutive fails before opening
CIRCUIT_COOLDOWN = 60             # seconds before half-open retry
DEFAULT_TIMEOUT = 20


def _host(url):
    try:
        return url.split("/")[2].lower()
    except Exception:
        return "unknown"


def _sleep_gap(host):
    gap = MIN_GAP.get(host, 0.5)
    elapsed = time.time() - _last_call.get(host, 0)
    if elapsed < gap:
        time.sleep(gap - elapsed)


def _circuit_check(host):
    """Raise if circuit is open for host."""
    until = _circuit_open.get(host, 0)
    if time.time() < until:
        raise RuntimeError("circuit open for %s (%.0fs left)" % (
            host, until - time.time()))


def _record_success(host):
    _circuit_fails[host] = 0
    _circuit_open.pop(host, None)
    _last_call[host] = time.time()


def _record_fail(host):
    _circuit_fails[host] += 1
    _last_call[host] = time.time()
    if _circuit_fails[host] >= CIRCUIT_THRESHOLD:
        _circuit_open[host] = time.time() + CIRCUIT_COOLDOWN


_RETRYABLE = (
    http.client.IncompleteRead,
    http.client.RemoteDisconnected,
    ConnectionResetError,
    ConnectionAbortedError,
    TimeoutError,
)


def _is_retryable(e):
    """Classify exception: retryable transient vs hard failure."""
    if isinstance(e, _RETRYABLE + (ValueError,)):
        return True
    if isinstance(e, urllib.error.URLError):
        # Unwrap: connection refused/timeout are transient for our purposes
        reason = str(e.reason) if hasattr(e, 'reason') else str(e)
        if any(k in reason for k in ("Connection refused", "timed out",
                                     "Temporary failure", "Name or service")):
            return True
    return False


def _curl_json(url, timeout, headers):
    """Fetch JSON via curl (arg list only, no shell). Returns parsed dict or None.

    VM egress quirk (proven in safety.py): Python's urllib through the MITM
    proxy intermittently fails with IncompleteRead / connection reset while
    curl succeeds consistently. Used as the last-resort fallback in get_json
    after urllib exhausts its retries. Never raises.
    """
    curl = shutil.which("curl")
    if not curl:
        return None
    # -w appends the HTTP status on its own line: without it, an error body
    # (eg the JSON payload of a 404 after urllib was rate-limited) parses
    # cleanly and would be returned to callers as if it were data.
    cmd = [curl, "-sS", "--max-time", str(int(timeout)), "-w", "\n%{http_code}"]
    for k, v in headers.items():
        cmd += ["-H", "%s: %s" % (k, v)]
    cmd.append(url)
    try:
        out = subprocess.run(cmd, capture_output=True,
                             timeout=timeout + 5, check=False)
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    try:
        raw, _, status_s = out.stdout.rpartition(b"\n")
        status = int(status_s.strip() or b"0")
    except Exception:
        return None
    if status < 200 or status >= 400:
        return None  # any HTTP error (or a failed transfer) is a miss, not data
    try:
        # curl doesn't auto-decompress unless --compressed; handle gzip magic
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode())
    except Exception:
        return None


def get_json(url, timeout=DEFAULT_TIMEOUT, tries=8,
             headers=None, backoff_base=2.0):
    """GET JSON with full hardening. Raises on final failure."""
    host = _host(url)
    hdrs = {"User-Agent": "forex-agent/1.0", "Accept": "application/json",
            "Accept-Encoding": "gzip"}  # proxy drops large bodies; gzip shrinks ~4x
    if headers:
        hdrs.update(headers)

    _circuit_check(host)
    last_err = None
    hard_fails = 0

    for attempt in range(tries):
        _sleep_gap(host)
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                # decompress if gzipped
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
            if not raw:
                raise ValueError("empty body")
            data = json.loads(raw.decode())
            _record_success(host)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code < 600:
                last_err = e  # retryable
                # 5xx counts as hard-ish
                hard_fails += 1
            else:
                _record_fail(host)
                raise  # 4xx: fail fast, don't retry
        except Exception as e:
            if _is_retryable(e):
                # Transient: retry, do NOT trip circuit
                last_err = e
            else:
                last_err = e
                hard_fails += 1
                if hard_fails >= 2:
                    _record_fail(host)
                    raise

        # Only hard failures trip the circuit
        if hard_fails >= CIRCUIT_THRESHOLD:
            _record_fail(host)
        if attempt < tries - 1:
            # exponential backoff + jitter
            delay = backoff_base * (2 ** min(attempt, 4)) + random.uniform(0, 1.0)
            time.sleep(delay)

    # urllib exhausted: last-resort curl fallback (egress proxy quirk —
    # urllib intermittently drops bodies that curl fetches fine).
    _curl = _curl_json(url, timeout, hdrs)
    if _curl is not None:
        _record_success(host)
        return _curl

    raise RuntimeError("get_json failed after %d tries [%s]: %s" % (
        tries, host, last_err))


def post_json(url, payload, timeout=DEFAULT_TIMEOUT, tries=3, headers=None):
    """POST JSON with same hardening. For RPC endpoints."""
    host = _host(url)
    hdrs = {"User-Agent": "forex-agent/1.0", "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload).encode()

    _circuit_check(host)
    last_err = None
    for attempt in range(tries):
        _sleep_gap(host)
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            if not raw:
                raise ValueError("empty body")
            data = json.loads(raw.decode())
            _record_success(host)
            return data
        except _RETRYABLE + (ValueError,) as e:
            last_err = e
        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code < 600:
                last_err = e
            else:
                _record_fail(host)
                raise
        except Exception as e:
            last_err = e
            _record_fail(host)
            raise
        _record_fail(host)
        if attempt < tries - 1:
            time.sleep(backoff_base * (2 ** attempt) + random.uniform(0, 1.0))
    raise RuntimeError("post_json failed after %d tries [%s]: %s" % (
        tries, host, last_err))


def circuit_status():
    """For health checks: which hosts are circuit-broken."""
    now = time.time()
    return {h: round(until - now, 1)
            for h, until in _circuit_open.items() if until > now}
