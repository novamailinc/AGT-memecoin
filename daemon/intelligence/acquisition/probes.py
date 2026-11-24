"""Probes FIRST (Phase 2).

The audit's top probes, run before any shadow verdict is claimed:
  1. Chainstack getSignaturesForAddress serving (expect failure on
     Developer — archive; record the ACTUAL error code, do not assume
     -32002).
  2. Chainstack getProgramAccounts error code (expect paid-only;
     record the actual code).
  3. Public getTransaction 403 (repo comments claim the public
     endpoint 403s getTransaction(jsonParsed) — UNVERIFIED; this
     probe settles it with a real call).
  4. Alchemy 25-vs-15 RPS — RESOLVED from Alchemy's own docs
     2026-11-22: Free is 300 CU/s account-level over a 10s rolling
     window, which Alchemy puts at ~15 req/s; "25 RPS" is a
     secondary claim, not official. No live call needed; the
     scheduler's 5/s stays well under either figure.
  5. Alchemy per-method CU officially — RESOLVED from Alchemy's
     published Solana CU table (via the 2026-09-05 verification):
     account reads 10 CU, getSlot/getProgramAccounts/
     getMultipleAccounts/sendTransaction/simulateTransaction 20 CU,
     getTransaction/getBlock/getSignaturesForAddress 40 CU.
     getTokenLargestAccounts CU remains UNVERIFIED (not in the
     published table found).

Probes 1–2 need the Chainstack key (BLOCKED until it lands);
probe 3 needs no key and runs live here. The Alchemy health probe
needs the Alchemy key (BLOCKED — the key returns HTTP 401; the user
is re-entering it via secure entry).

Every probe result is a plain dict: {"probe": ..., "status":
"ok"|"failed"|"blocked", "detail": ..., "http_status": ...,
"error_code": ...}. Nothing here writes a key or logs key material.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from . import rpc_client as rpc_mod
from daemon import solana_ids as _solana_ids

# A real, well-known mainnet mint used only as a read probe target
# (SOL wrapped mint — public, stable, no user data). Canonical value
# lives in daemon/solana_ids.py (Phase 2).
PROBE_MINT = _solana_ids.WSOL_MINT
PUMP_PROGRAM = _solana_ids.PUMP_PROGRAM_ID


def _probe_call(url, method, params, timeout=12):
    """Raw JSON-RPC call returning a probe-shaped dict. Never raises."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", 200)
            parsed = json.loads(raw.decode("utf-8", "replace"))
            return {"http_status": status, "body": parsed,
                    "elapsed_s": round(time.monotonic() - start, 3)}
    except urllib.error.HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        try:
            parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except ValueError:
            parsed = {}
        return {"http_status": exc.code, "body": parsed,
                "elapsed_s": round(time.monotonic() - start, 3)}
    except Exception as exc:
        return {"http_status": None, "body": {},
                "elapsed_s": round(time.monotonic() - start, 3),
                "transport_error": str(exc)[:200]}


def probe_public_get_transaction():
    """Probe 3: does the public endpoint 403 getTransaction
    (jsonParsed)? Uses a real call against the public endpoint —
    no key needed. Returns status ok/failed with the observed
    HTTP status and JSON-RPC error code (or 'no-403' when it
    succeeds)."""
    # getSlot first (cheap liveness check), then getTransaction on a
    # signature that certainly exists is not available without a
    # known signature; use getSignaturesForAddress on the pump
    # program to obtain one, then fetch it.
    sigs = _probe_call(rpc_mod.PUBLIC_URL, "getSignaturesForAddress",
                       [PUMP_PROGRAM, {"limit": 1}])
    if sigs["http_status"] != 200 or not (sigs["body"] or {}).get("result"):
        return {"probe": "public_getTransaction_403",
                "status": "failed",
                "detail": "could not obtain a probe signature from "
                          "public getSignaturesForAddress",
                "http_status": sigs["http_status"],
                "error_code": ((sigs["body"] or {}).get("error") or {}).get("code")}
    sig = sigs["body"]["result"][0]["signature"]
    tx = _probe_call(rpc_mod.PUBLIC_URL, "getTransaction",
                     [sig, {"encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0}])
    err = (tx["body"] or {}).get("error") or {}
    if tx["http_status"] == 200 and (tx["body"] or {}).get("result") is not None:
        return {"probe": "public_getTransaction_403", "status": "ok",
                "detail": "public getTransaction(jsonParsed) SUCCEEDED "
                          "— the 403 claim is FALSE for this call",
                "http_status": 200, "error_code": None,
                "elapsed_s": tx["elapsed_s"]}
    return {"probe": "public_getTransaction_403", "status": "failed",
            "detail": "public getTransaction did not return a result",
            "http_status": tx["http_status"],
            "error_code": err.get("code"),
            "error_message": str(err.get("message", ""))[:200],
            "elapsed_s": tx["elapsed_s"]}


def probe_chainstack_methods(home=None):
    """Probes 1–2: Chainstack getSignaturesForAddress serving and
    getProgramAccounts error code. BLOCKED until the Chainstack
    endpoint is configured."""
    endpoint = rpc_mod.chainstack_endpoint(home=home)
    if not endpoint:
        return [
            {"probe": "chainstack_getSignaturesForAddress",
             "status": "blocked",
             "detail": "no Chainstack endpoint configured "
                       "(CHAINSTACK_ENDPOINT / file); key outstanding"},
            {"probe": "chainstack_getProgramAccounts",
             "status": "blocked",
             "detail": "no Chainstack endpoint configured "
                       "(CHAINSTACK_ENDPOINT / file); key outstanding"},
        ]
    out = []
    sigs = _probe_call(endpoint, "getSignaturesForAddress",
                       [PUMP_PROGRAM, {"limit": 1}])
    err = (sigs["body"] or {}).get("error") or {}
    out.append({
        "probe": "chainstack_getSignaturesForAddress",
        "status": "ok" if sigs["http_status"] == 200 and
        (sigs["body"] or {}).get("result") is not None else "failed",
        "detail": "served" if sigs["http_status"] == 200 else
        "not served on Developer (record the actual error code)",
        "http_status": sigs["http_status"],
        "error_code": err.get("code"),
        "error_message": str(err.get("message", ""))[:200],
        "elapsed_s": sigs["elapsed_s"]})
    prog = _probe_call(
        endpoint, "getProgramAccounts",
        [PUMP_PROGRAM, {"encoding": "base64", "filters": [
            {"dataSize": 100}]}])
    err2 = (prog["body"] or {}).get("error") or {}
    out.append({
        "probe": "chainstack_getProgramAccounts",
        "status": "ok" if prog["http_status"] == 200 else "failed",
        "detail": "served" if prog["http_status"] == 200 else
        "paid-only on Developer (record the actual error code)",
        "http_status": prog["http_status"],
        "error_code": err2.get("code"),
        "error_message": str(err2.get("message", ""))[:200],
        "elapsed_s": prog["elapsed_s"]})
    return out


def probe_alchemy_health(home=None):
    """Alchemy health probe. BLOCKED until a valid key exists — the
    current key returns HTTP 401 and the user is re-entering it via
    secure entry. Do NOT treat Alchemy as verified until this probe
    returns ok."""
    url = rpc_mod.alchemy_url(home=home)
    if not url:
        return {"probe": "alchemy_health", "status": "blocked",
                "detail": "no Alchemy key configured "
                          "(ALCHEMY_API_KEY / $HOME/alchemy/api_key)"}
    res = rpc_mod.rpc("alchemy", "getHealth", [], timeout=12, home=home)
    if res["kind"] == "ok":
        return {"probe": "alchemy_health", "status": "ok",
                "detail": "Alchemy getHealth returned ok",
                "elapsed_s": round(res["elapsed"], 3)}
    if res["kind"] == "rate_limited":
        return {"probe": "alchemy_health", "status": "failed",
                "detail": "Alchemy rate-limited the health probe",
                "http_status": res.get("http_status"),
                "rate_limit_kind": res.get("rate_limit_kind")}
    if res.get("http_status") == 401:
        return {"probe": "alchemy_health", "status": "failed",
                "detail": "Alchemy returned HTTP 401 — key value "
                          "rejected, not the setup; re-enter just the "
                          "key (not the full endpoint URL)",
                "http_status": 401}
    return {"probe": "alchemy_health", "status": "failed",
            "detail": "Alchemy health probe kind=%s" % res["kind"],
            "http_status": res.get("http_status")}


def helius_quiet_active(home=None):
    """True when the Helius auto-quiet flag is active right now.
    Read from ~/.forex-agent/helius_quiet.json (the live pacer's
    own file); any read failure means 'unknown' -> treated as
    active (fail closed: the probe skips the Helius leg rather
    than adding load during a possible quiet)."""
    import os
    home = home or os.environ.get("FOREX_AGENT_HOME") \
        or os.path.expanduser("~/.forex-agent")
    path = os.path.join(home, "helius_quiet.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        return True
    if not isinstance(data, dict):
        return True
    if not data.get("active"):
        return False
    until = data.get("until") or 0
    try:
        return time.time() < float(until)
    except (TypeError, ValueError):
        return True


def probe_slot_commitments(home=None, rounds=5, pause_s=21.0,
                           sleep=time.sleep):
    """D-16 explicit-commitment probe (Phase 3).

    getSlot with EXPLICIT commitments (processed / confirmed /
    finalized) on Alchemy, Helius, and public, in near-simultaneous
    pairs per round: for each round and each commitment, one
    getSlot per provider, back to back, recording slot + wall ts.
    Lag is computed per (round, commitment) pair as
    alchemy_slot - helius_slot (and alchemy - public), so the
    number is a same-moment difference, not a drifted comparison.

    Volume: rounds * 3 commitments * 3 providers calls (5 rounds
    = 45 calls). Helius goes through the live reserves transport
    (pacer 1 call/20s/key); pause_s=21s between rounds keeps the
    probe inside the pacer. When Helius auto-quiet is active the
    Helius leg is SKIPPED (recorded, not failed) — the probe never
    adds load during a quiet. The Helius adapter cannot tell a
    public-fallback serve from a Helius serve, so the actual
    serving leg is read from reserves.last_rpc_source() after each
    Helius call and recorded (a public-served 'helius' call is
    labelled as public, never as Helius).

    Returns {"probe": "slot_commitments", "rounds": [...],
    "lag": {commitment: {"vs_helius": [...], "vs_public": [...],
    "median": ..., "mean": ..., "n": ...}}, "skipped_helius": bool,
    "method": ...}. Never raises; never logs key material.
    """
    commitments = ("processed", "confirmed", "finalized")
    providers = ("alchemy", "helius", "public")
    # Default home is the runtime home (FOREX_AGENT_HOME), where
    # the disk keys live — home=None would make the Alchemy adapter
    # look under ~/alchemy/ and report unconfigured (found live in
    # the first probe run: all 15 Alchemy calls unconfigured).
    home = home or os.environ.get("FOREX_AGENT_HOME") \
        or os.path.expanduser("~/.forex-agent")
    quiet = helius_quiet_active(home=home)
    out_rounds = []
    for rnd in range(rounds):
        round_rec = {"round": rnd, "calls": {}}
        for commitment in commitments:
            for provider in providers:
                if provider == "helius" and quiet:
                    round_rec["calls"][(provider, commitment)] = {
                        "skipped": True,
                        "reason": "helius auto-quiet active"}
                    continue
                try:
                    res = rpc_mod.rpc(
                        provider, "getSlot", [{"commitment": commitment}],
                        timeout=12, home=home)
                except Exception as exc:
                    round_rec["calls"][(provider, commitment)] = {
                        "kind": "error", "slot": None,
                        "note": str(exc)[:120]}
                    continue
                served = provider
                if provider == "helius":
                    try:
                        from daemon.launch_scanner import reserves
                        src = reserves.last_rpc_source()
                        if src == "public-rpc":
                            served = "public (via helius adapter)"
                    except Exception:
                        pass
                round_rec["calls"][(provider, commitment)] = {
                    "kind": res.get("kind"),
                    "slot": res.get("result"),
                    "served": served,
                    "elapsed_s": round(res.get("elapsed", 0.0), 3),
                    "ts": time.time()}
        out_rounds.append(round_rec)
        if rnd < rounds - 1:
            sleep(pause_s)
    lag = {}
    for commitment in commitments:
        vs_h, vs_p = [], []
        for rnd in out_rounds:
            calls = rnd["calls"]
            a = calls.get(("alchemy", commitment), {})
            h = calls.get(("helius", commitment), {})
            p = calls.get(("public", commitment), {})
            if isinstance(a.get("slot"), int) \
                    and isinstance(h.get("slot"), int) \
                    and h.get("served") == "helius":
                vs_h.append(a["slot"] - h["slot"])
            if isinstance(a.get("slot"), int) \
                    and isinstance(p.get("slot"), int):
                vs_p.append(a["slot"] - p["slot"])
        def _stats(vals):
            if not vals:
                return {"n": 0, "median": None, "mean": None,
                        "min": None, "max": None, "values": []}
            s = sorted(vals)
            return {"n": len(vals),
                    "median": s[len(s) // 2],
                    "mean": round(sum(vals) / len(vals), 2),
                    "min": s[0], "max": s[-1], "values": vals}
        lag[commitment] = {"vs_helius": _stats(vs_h),
                           "vs_public": _stats(vs_p)}
    return {"probe": "slot_commitments",
            "commitments": list(commitments),
            "providers": list(providers),
            "rounds": rounds,
            "helius_quiet_active": quiet,
            "skipped_helius": quiet,
            "method": ("per (round, commitment): one getSlot per "
                       "provider back to back; lag = alchemy_slot - "
                       "other_slot on the same pair; Helius leg via "
                       "live reserves transport, serving leg from "
                       "reserves.last_rpc_source()"),
            "lag": lag,
            "raw_rounds": [
                {("%s/%s" % k): v for k, v in r["calls"].items()}
                for r in out_rounds]}


def probe_chainstack_current(home=None):
    """Phase 3 re-probe of Chainstack Developer Free (D-05 task 5).

    Re-probes the methods the plan is claimed to support AND the
    ones refused before — getSlot, getAccountInfo, getTransaction
    (recent, known signature), getSignaturesForAddress,
    getProgramAccounts — and records the ACTUAL current behaviour
    (served / HTTP status / JSON-RPC error code / message). No
    assumption from the Nov 22 probe is carried forward: a method
    that was refused then is re-tested now. The getTransaction
    target signature comes from the public endpoint (no key).
    Returns a list of probe dicts. Never raises."""
    import os
    endpoint = rpc_mod.chainstack_endpoint(home=home)
    if not endpoint:
        return [{"probe": "chainstack_current",
                 "status": "blocked",
                 "detail": "no Chainstack endpoint configured"}]
    out = []

    def _one(name, method, params):
        res = _probe_call(endpoint, method, params)
        err = (res["body"] or {}).get("error") or {}
        served = res["http_status"] == 200 \
            and (res["body"] or {}).get("result") is not None
        out.append({"probe": "chainstack_%s" % name,
                    "method": method,
                    "status": "ok" if served else "failed",
                    "detail": "served" if served else
                    "not served on current plan",
                    "http_status": res["http_status"],
                    "error_code": err.get("code"),
                    "error_message": str(err.get("message", ""))[:200],
                    "elapsed_s": res["elapsed_s"]})
        return res

    _one("getSlot", "getSlot", [])
    _one("getAccountInfo", "getAccountInfo",
         [PROBE_MINT, {"encoding": "jsonParsed"}])
    sig = None
    pub = _probe_call(rpc_mod.PUBLIC_URL, "getSignaturesForAddress",
                      [PUMP_PROGRAM, {"limit": 1}])
    if pub["http_status"] == 200 and (pub["body"] or {}).get("result"):
        sig = pub["body"]["result"][0]["signature"]
    if sig:
        _one("getTransaction_recent", "getTransaction",
             [sig, {"encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0}])
    else:
        out.append({"probe": "chainstack_getTransaction_recent",
                    "method": "getTransaction", "status": "failed",
                    "detail": "no probe signature from public "
                              "getSignaturesForAddress",
                    "http_status": pub["http_status"],
                    "error_code": ((pub["body"] or {}).get("error")
                                   or {}).get("code")})
    _one("getSignaturesForAddress", "getSignaturesForAddress",
         [PUMP_PROGRAM, {"limit": 1}])
    _one("getProgramAccounts", "getProgramAccounts",
         [PUMP_PROGRAM, {"encoding": "base64",
                         "filters": [{"dataSize": 100}]}])
    return out


def run_probes(home=None):
    """Run every probe that can run; blocked ones report blocked.
    Returns a list of probe dicts (never raises)."""
    results = [probe_public_get_transaction(),
               probe_alchemy_health(home=home)]
    results.extend(probe_chainstack_methods(home=home))
    results.append({
        "probe": "alchemy_rps_25_vs_15",
        "status": "ok",
        "detail": "RESOLVED from Alchemy's own docs 2026-11-22: Free "
                  "is 300 CU/s account-level (10s rolling window), "
                  "~15 req/s; '25 RPS' is a secondary claim, not "
                  "official. Scheduler's 5/s is under either figure.",
    })
    results.append({
        "probe": "alchemy_per_method_cu",
        "status": "ok",
        "detail": "RESOLVED from Alchemy's published Solana CU "
                  "table: account reads 10 CU; getSlot/"
                  "getProgramAccounts/getMultipleAccounts/"
                  "sendTransaction/simulateTransaction 20 CU; "
                  "getTransaction/getBlock/getSignaturesForAddress "
                  "40 CU. getTokenLargestAccounts CU remains "
                  "UNVERIFIED (not in the published table found).",
    })
    return results


if __name__ == "__main__":
    for probe in run_probes():
        print(json.dumps(probe))
