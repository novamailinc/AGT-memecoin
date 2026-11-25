"""BOUNDED PUBLIC-DATA CAPTURE (SOURCE ONLY, WRITTEN NOT RUN).
Records ACTUAL HTTP responses that the production engine's own data path
requests, so a later replay serves the engine real bytes under real URLs.

Safety contract (enforced in code, reviewed before any run):
 - GET only. https only. Host allowlist below. No credentials: refuses to run
   if any env var name looks like a key/token/secret, and sends no auth header.
 - curl: -q (no curlrc), no redirects, https-only protocol, no proxy, no cookie jar, no auth header,
   15 s per request, 2 MiB body cap, child env = PATH only.
 - Hard cap on total requests (--max-requests, default 12) and on mints (--mints, default 3).
 - Everything the engine asks that is off-allowlist or over the cap is REFUSED
   (returns None to the engine, logged as refused); nothing is faked in its place.
 - Imports project modules read-only, constructs NO live executor, writes ONLY the
   capture file given by --out (must not exist).
 - Never raises a trading path: it calls fetch_candidates / _fresh_tape / quote_price only.
Output: JSONL. Line 1 = header (provenance). Then one record per request:
 {url, status, fetched_utc, body_sha256, body_b64, via:"curl"}.
"""
import argparse, base64, hashlib, json, os, shutil, subprocess, sys, time
from urllib.parse import urlsplit

ALLOW_HOSTS = {"api.dexscreener.com", "api.geckoterminal.com",
               "price.jup.ag", "lite-api.jup.ag", "api.jup.ag"}
BODY_CAP = 2 * 1024 * 1024   # bytes per response; curl aborts above this
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE", "SEED", "MNEMONIC")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-requests", type=int, default=12)
    ap.add_argument("--mints", type=int, default=3)
    ap.add_argument("--repo", default=os.getcwd())
    a = ap.parse_args()
    bad = [k for k in os.environ if any(h in k.upper() for h in SECRET_HINTS)]
    if bad:
        print("REFUSED: secret-like env names present (count=%d); run under env -i" % len(bad)); return 4
    if os.path.exists(a.out):
        print("REFUSED: out exists"); return 4
    curl = shutil.which("curl")
    if not curl:
        print("REFUSED: no curl"); return 4
    sys.dont_write_bytecode = True
    sys.path.insert(0, a.repo)
    from daemon.launch_scanner import sources, tranche
    recs, refused = [], []

    def transport(url, data=None, extra_headers=None, timeout=15):
        sp = urlsplit(url)
        if data is not None or sp.scheme != "https" or sp.hostname not in ALLOW_HOSTS:
            refused.append(("policy", sp.hostname)); return None
        if len(recs) >= a.max_requests:
            refused.append(("cap", sp.hostname)); return None
        t = time.time()
        p = subprocess.run([curl, "-q", "-sS", "-X", "GET", "--proto", "=https", "--max-redirs", "0",
                            "--noproxy", "*", "--max-time", str(min(int(timeout), 15)),
                            "--max-filesize", str(BODY_CAP), "-w", "\n%{http_code}",
                            "-H", "User-Agent: agt-capture/1", url],
                           capture_output=True, timeout=20, check=False,
                           env={"PATH": os.path.dirname(curl)})
        body, _, code = p.stdout.rpartition(b"\n")
        rec = {"url": url, "status": int(code or 0), "fetched_utc": t,
               "body_sha256": hashlib.sha256(body).hexdigest(),
               "body_b64": base64.b64encode(body).decode(), "via": "curl", "curl_rc": p.returncode}
        recs.append(rec)
        if p.returncode != 0 or rec["status"] != 200 or not body:
            return None
        return body

    sources._curl_bytes = transport      # only seam replaced: the HTTP byte transport
    eng = tranche.make_engine()
    t0 = time.time()
    cands = eng.fetch_candidates()
    qual = [c for c in cands if eng.is_graduation_candidate(c, t0) is not None]
    picked = (qual or cands)[: a.mints]
    for c in picked:
        eng._fresh_tape(c["mint"]); eng.quote_price("solana", c["mint"])
    hdr = {"kind": "agt-real-capture", "version": 1, "tool": "tests/realstack/capture_public.py",
           "tool_sha256": hashlib.sha256(open(__file__, "rb").read()).hexdigest(),
           "repo_head": subprocess.run(["git", "-C", a.repo, "rev-parse", "HEAD"], capture_output=True,
                                       text=True).stdout.strip(),
           "t0_utc": t0, "providers": sorted({urlsplit(r["url"]).hostname for r in recs}),
           "n_requests": len(recs), "n_refused": len(refused), "refused": refused,
           "n_candidates_seen": len(cands), "n_qualifying_graduations": len(qual),
           "picked_mints": [c["mint"] for c in picked],
           "note": "responses are as returned by the provider; no edits"}
    with open(a.out, "x") as f:
        f.write(json.dumps(hdr) + "\n")
        for r in recs:
            f.write(json.dumps(r) + "\n")
    print("CAPTURED requests=%d refused=%d candidates=%d qualifying=%d out=%s"
          % (len(recs), len(refused), len(cands), len(qual), a.out))
    return 0 if recs else 3


if __name__ == "__main__":
    sys.exit(main())
