"""BOUNDED PUBLIC-DATA CAPTURE (SOURCE ONLY, WRITTEN NOT RUN) - revision 2.
Records ACTUAL HTTP responses that the engine's data path (tranche.make_engine -> fetch_candidates, _fresh_tape, quote_price)
requests. COVERAGE LIMIT: it does NOT call _safety_ok / _veto_ok / the demo filler, so rugcheck / gopluslabs / honeypot / Jupiter
inputs are NOT captured; an entry path through those is therefore not replayable from this capture.

Every network route is accounted (all three are redirected to ONE guarded fetch()):
  1. sources._curl_bytes            (curl path, used by sources._get_json / _post_json / GT fetch)
  2. urllib.request.urlopen         (global; used by sources urllib fallback, sources._gt_get_json, grad._http_json)
  3. nothing else: capture imports no safety/veto/filler path. A post-run assertion requires
     urlopen_calls == urlopen_served + urlopen_refused and prints all counters.
Policy inside fetch(): GET only, https only, host in ALLOW_HOSTS, <= --max-requests actual network requests (default 12),
a URL is attempted at most ONCE per run (the urllib fallback of a failed/refused URL is refused, never re-sent),
streamed body read with a HARD byte cap (BODY_CAP, kill on exceed; the response is then discarded, not recorded as data),
15 s per request, curl -q, no redirects (--max-redirs 0), --proto =https, --noproxy '*', no cookie jar, no auth header, child env = PATH only.
Run requirements (refuses otherwise): env -i style (no secret-like env names), FOREX_AGENT_HOME set to an EMPTY existing scratch dir,
--repo given explicitly and git tree clean, --out must not exist. DNS lookups are done by curl and are NOT logged by this shim.
Output JSONL: header (capture-generated: tool path, tool sha256, repo HEAD, t0, counters, refusal log, picked mints) then one record per
actual request {url,status,fetched_utc,curl_rc,body_sha256,body_b64}. The last console line prints the WHOLE-FILE sha256."""
import argparse, base64, hashlib, json, os, shutil, subprocess, sys, time, io
import urllib.request
from urllib.parse import urlsplit

ALLOW_HOSTS = {"api.dexscreener.com", "api.geckoterminal.com"}
BODY_CAP = 2 * 1024 * 1024
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE", "SEED", "MNEMONIC")


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False
    status = 200


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--max-requests", type=int, default=12)
    ap.add_argument("--mints", type=int, default=3)
    a = ap.parse_args()
    bad = [k for k in os.environ if any(h in k.upper() for h in SECRET_HINTS)]
    if bad:
        print("REFUSED: secret-like env names present (count=%d); run under env -i" % len(bad)); return 4
    home = os.environ.get("FOREX_AGENT_HOME")
    if not home or not os.path.isdir(home) or os.listdir(home):
        print("REFUSED: FOREX_AGENT_HOME must be an existing EMPTY scratch dir"); return 4
    if os.path.exists(a.out):
        print("REFUSED: out exists"); return 4
    st = subprocess.run(["git", "-C", a.repo, "status", "--porcelain"], capture_output=True, text=True)
    if st.returncode != 0 or st.stdout.strip():
        print("REFUSED: repo not clean"); return 4
    curl = shutil.which("curl")
    if not curl:
        print("REFUSED: no curl"); return 4
    sys.dont_write_bytecode = True
    sys.path.insert(0, a.repo)
    recs, refused, attempted = [], [], set()
    cnt = {"urlopen_calls": 0, "urlopen_served": 0, "urlopen_refused": 0, "curl_seam_calls": 0}

    def fetch(url, method="GET", has_body=False):
        sp = urlsplit(url)
        if has_body or method != "GET" or sp.scheme != "https" or sp.hostname not in ALLOW_HOSTS:
            refused.append(["policy", sp.hostname]); return None
        if url in attempted:
            refused.append(["repeat", sp.hostname]); return None
        if len(recs) >= a.max_requests:
            refused.append(["cap", sp.hostname]); return None
        attempted.add(url)
        t = time.time()
        p = subprocess.Popen([curl, "-q", "-sS", "-X", "GET", "--proto", "=https", "--max-redirs", "0", "--noproxy", "*",
                              "--max-time", "15", "-w", "\n%{http_code}", "-H", "User-Agent: agt-capture/1", url],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={"PATH": os.path.dirname(curl)})
        buf, over = b"", False
        while True:
            chunk = p.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > BODY_CAP + 16:
                over = True; p.kill(); break
        try:
            p.wait(timeout=20)
        except Exception:
            p.kill(); p.wait()
        if over:
            recs.append({"url": url, "status": 0, "fetched_utc": t, "curl_rc": -1, "body_sha256": "", "body_b64": "", "over_cap": True})
            return None
        body, _, code = buf.rpartition(b"\n")
        rec = {"url": url, "status": int(code or 0), "fetched_utc": t, "curl_rc": p.returncode,
               "body_sha256": hashlib.sha256(body).hexdigest(), "body_b64": base64.b64encode(body).decode()}
        recs.append(rec)
        return body if (p.returncode == 0 and rec["status"] == 200 and body) else None

    def curl_seam(url, data=None, extra_headers=None, timeout=15):
        cnt["curl_seam_calls"] += 1
        return fetch(url, "POST" if data is not None else "GET", data is not None)

    def urlopen_shim(req, data=None, timeout=None, **kw):
        cnt["urlopen_calls"] += 1
        url = req if isinstance(req, str) else req.full_url
        body = data if data is not None else (None if isinstance(req, str) else req.data)
        meth = "GET" if isinstance(req, str) else req.get_method()
        got = fetch(url, meth, body is not None)
        if got is None:
            cnt["urlopen_refused"] += 1
            raise OSError("capture shim refused or failed")
        cnt["urlopen_served"] += 1
        return _Resp(got)

    urllib.request.urlopen = urlopen_shim
    from daemon.launch_scanner import sources, tranche
    sources._curl_bytes = curl_seam
    eng = tranche.make_engine()
    t0 = time.time()
    cands = eng.fetch_candidates()
    qual = [c for c in cands if eng.is_graduation_candidate(c, t0) is not None]
    picked = (qual or cands)[: a.mints]
    for c in picked:
        eng._fresh_tape(c["mint"]); eng.quote_price("solana", c["mint"])
    ok_acct = cnt["urlopen_calls"] == cnt["urlopen_served"] + cnt["urlopen_refused"]
    hdr = {"kind": "agt-real-capture", "version": 2, "tool": "tests/realstack/capture_public.py",
           "tool_sha256": hashlib.sha256(open(__file__, "rb").read()).hexdigest(),
           "repo_head": subprocess.run(["git", "-C", a.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
           "t0_utc": t0, "providers": sorted({urlsplit(r["url"]).hostname for r in recs}),
           "n_requests": len(recs), "n_refused": len(refused), "refused": refused, "counters": cnt, "accounting_ok": ok_acct,
           "n_candidates_seen": len(cands), "n_qualifying_graduations": len(qual),
           "picked_mints": [c["mint"] for c in picked],
           "coverage": "candidates+tape+quote only; NO safety/veto/filler inputs",
           "note": "responses stored as returned; first body per URL only (each URL fetched at most once)"}
    with open(a.out, "x") as f:
        f.write(json.dumps(hdr) + "\n")
        for r in recs:
            f.write(json.dumps(r) + "\n")
    whole = hashlib.sha256(open(a.out, "rb").read()).hexdigest()
    print("CAPTURED requests=%d refused=%d candidates=%d qualifying=%d counters=%s accounting_ok=%s"
          % (len(recs), len(refused), len(cands), len(qual), json.dumps(cnt), ok_acct))
    print("FOREX_AGENT_HOME listing: %s" % sorted(os.listdir(home)))
    print("WHOLE-FILE sha256 %s out=%s" % (whole, a.out))
    return 0 if (recs and ok_acct) else 3


if __name__ == "__main__":
    sys.exit(main())
