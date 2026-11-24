#!/usr/bin/env python3
"""AGT provider probe that matches the deploy credential contract.

Contract (deploy/agt/credentials.py, secrets_mapping.md,
daemon/intelligence/acquisition/providers/base.py):
  HELIUS_API_KEY_1, HELIUS_API_KEY_2, HELIUS_API_KEY_3  (numbered slots;
      the legacy HELIUS_API_KEY is NOT a substitute for slot 2 or 3)
  ALCHEMY_API_KEY      (bare key, or the exact supported Alchemy URL)
  CHAINSTACK_ENDPOINT  (full endpoint URL)

Default mode is OFFLINE: it reports, per slot, whether the contract is
satisfied (present, not a TEST- placeholder, valid shape, host) and
makes NO network call. It prints "NOT PROBED" and exits 0 only when
every slot is satisfied. A real provider call happens only with the
explicit --live flag, one JSON-RPC getHealth per slot. --live is for
the owner of the live execution to run under their own authorization.

It never prints a key or full URL: only slot labels, hosts and HTTP
status. Any key value appearing in an error body is redacted.

Usage:
    python3 tools/agt_key_test.py            # offline contract check
    python3 tools/agt_key_test.py --live     # real getHealth per slot
"""
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

HELIUS_HTTPS_TMPL = "https://mainnet.helius-rpc.com/?api-key={key}"
ALCHEMY_HTTPS_TMPL = "https://solana-mainnet.g.alchemy.com/v2/{key}"
ALCHEMY_HOST = "solana-mainnet.g.alchemy.com"
ALCHEMY_WSS_HOST = "solana-mainnet.streaming.alchemy.com"
SLOTS = ("helius-1", "helius-2", "helius-3", "alchemy", "chainstack")


def _clean(env, name):
    return (env.get(name) or "").strip()


def _alchemy_key(value):
    """Bare key, or the exact supported Alchemy URL (same rule as
    providers.base._alchemy_credential_key). None when unusable."""
    if not value:
        return None
    if "://" not in value:
        return None if any(c in value for c in "/?#@") else value
    try:
        p = urlsplit(value)
    except ValueError:
        return None
    hosts = {"https": ALCHEMY_HOST, "wss": ALCHEMY_WSS_HOST}
    if (p.scheme not in hosts or p.netloc != hosts[p.scheme] or p.query
            or p.fragment or not p.path.startswith("/v2/")):
        return None
    key = p.path[len("/v2/"):]
    return key if key and not any(c in key for c in "/?#@%") else None


def build_probes(env):
    """Return [(slot, url_or_None, secret_or_None, reason)] for all
    five slots, in SLOTS order. url is None when the contract is not
    met; reason then says which rule failed (a name, never a value).
    Pure function: no I/O."""
    out = []
    for i in (1, 2, 3):
        name = "HELIUS_API_KEY_%d" % i
        v = _clean(env, name)
        slot = "helius-%d" % i
        if not v:
            out.append((slot, None, None, "missing %s" % name))
        elif v.upper().startswith("TEST-"):
            out.append((slot, None, None, "%s is a TEST- placeholder" % name))
        elif any(c in v for c in "/?#@:") or any(c.isspace() for c in v):
            out.append((slot, None, None, "%s is not key-shaped" % name))
        else:
            out.append((slot, HELIUS_HTTPS_TMPL.format(key=v), v, "ok"))
    v = _clean(env, "ALCHEMY_API_KEY")
    if not v:
        out.append(("alchemy", None, None, "missing ALCHEMY_API_KEY"))
    elif v.upper().startswith("TEST-"):
        out.append(("alchemy", None, None,
                    "ALCHEMY_API_KEY is a TEST- placeholder"))
    else:
        k = _alchemy_key(v)
        if k is None:
            out.append(("alchemy", None, None,
                        "ALCHEMY_API_KEY is not a bare key or the "
                        "supported Alchemy endpoint"))
        else:
            out.append(("alchemy", ALCHEMY_HTTPS_TMPL.format(key=k), k, "ok"))
    v = _clean(env, "CHAINSTACK_ENDPOINT")
    if not v:
        out.append(("chainstack", None, None, "missing CHAINSTACK_ENDPOINT"))
    elif v.upper().startswith("TEST-"):
        out.append(("chainstack", None, None,
                    "CHAINSTACK_ENDPOINT is a TEST- placeholder"))
    else:
        try:
            p = urlsplit(v)
        except ValueError:
            p = None
        if p is None or p.scheme != "https" or not p.netloc:
            out.append(("chainstack", None, None,
                        "CHAINSTACK_ENDPOINT is not an https URL"))
        else:
            # The whole endpoint is the secret (the node key is in it).
            out.append(("chainstack", v, v, "ok"))
    return out


def host_of(url):
    # parsed hostname only: userinfo, port, path and query never reach the output
    try:
        return urlsplit(url).hostname or "?"
    except ValueError:
        return "?"


def redact(text, secrets):
    for s in secrets:
        if s:
            text = text.replace(s, "<redacted>")
    return text


def offline_report(env):
    """(lines, ok). No network. ok only when every slot meets the
    contract; also notes (by comparison, no value printed) identical
    values across Helius slots."""
    probes = build_probes(env)
    lines, ok = [], True
    for slot, url, _secret, reason in probes:
        if url is None:
            ok = False
            lines.append("%s: CONTRACT NOT MET (%s)" % (slot, reason))
        else:
            lines.append("%s: contract met, host %s, NOT PROBED"
                         % (slot, host_of(url)))
    hs = [p[2] for p in probes[:3] if p[2]]
    if len(hs) > 1 and len(set(hs)) < len(hs):
        lines.append("note: some Helius slots hold identical values "
                     "(they share one account allowance either way; "
                     "this does not prove three separate keys)")
    return lines, ok


def check(url, secrets, timeout=30):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "getHealth", "params": []}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")[:200]
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        status, text = exc.code, body
    except Exception as exc:  # report, never crash the run
        status, text = None, "%s" % type(exc).__name__
    return status, redact(text, secrets)


def healthy(status, text):
    """HTTP 200 AND a JSON-RPC result without an error member."""
    if status != 200:
        return False
    try:
        d = json.loads(text)
    except ValueError:
        # body was truncated to 200 chars; a 200 with unparsable text
        # is not proof of health
        return False
    return isinstance(d, dict) and "error" not in d and "result" in d


def main(argv=None, env=None, opener=check):
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    if argv not in ([], ["--live"]):
        print("usage: agt_key_test.py [--live]")
        return 2
    probes = build_probes(env)
    if argv == []:
        lines, ok = offline_report(env)
        print("\n".join(lines))
        print("offline contract check only; no provider was called")
        return 0 if ok else 1
    ok = True
    for slot, url, secret, reason in probes:
        if url is None:
            print("%s: CONTRACT NOT MET (%s), not probed" % (slot, reason))
            ok = False
            continue
        status, text = opener(url, [secret])
        good = healthy(status, text)
        ok = ok and good
        print("%s: %s HTTP %s host %s" % (
            slot, "PASS" if good else "FAIL", status, host_of(url)))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
