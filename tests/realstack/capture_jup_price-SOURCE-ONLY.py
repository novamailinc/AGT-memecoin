"""ONE-REQUEST PUBLIC GET RECORDER for Jupiter Price API v3 (SOURCE ONLY, WRITTEN NOT RUN, NOT CLEARED). Separate from capture_public.py (whose
tool hash is pinned in the existing capture; that file is NOT modified).
Exactly ONE request: GET https://api.jup.ag/price/v3?ids=<ONE mint> where the URL is BUILT from the repo's own daemon/launch_scanner/jup_price.PRICE_V3_URL
(constant read by AST, the module is NOT imported or run) and the mint is the picked mint of the existing capture file. No API key, no auth header, no cookie,
no redirects, https only, host api.jup.ag only, 15 s timeout, hard body cap 256 KiB (kill on exceed, response discarded), curl -q --proto =https --max-redirs 0
--noproxy '*', child env = PATH only. Keyless access is documented for api.jup.ag at 0.5 requests/second (developers.jup.ag/docs/portal/setup, /docs/price); one
request is far below it. A 401/403/429/missing-mint/non-200/oversized/empty body is recorded HONESTLY as the outcome and blocks mark qualification (no retry, no fallback).
Preconditions (refuses otherwise): env -i style (no secret-like env names), --capture exists with the expected whole-file sha256 given on the command line,
header kind agt-real-capture and exactly one picked mint, --out must not exist, repo tree clean. Output JSONL: header (kind agt-real-capture-jup, tool sha256, repo HEAD,
t0, url, linked_capture_sha256, mint) + one record {url,status,fetched_utc,curl_rc,body_sha256,body_b64,body_len}. Last console line: whole-file sha256.
No RPC, no POST, no other host. Source effects: one outbound GET to Jupiter (public), nothing written outside --out."""
import argparse, ast, base64, hashlib, json, os, shutil, subprocess, sys, time

BODY_CAP = 256 * 1024
SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PRIVATE", "SEED", "MNEMONIC")


def const_from(path, name):
    for n in ast.parse(open(path).read()).body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            return ast.literal_eval(n.value)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--capture", required=True)
    ap.add_argument("--capture-sha256", required=True)
    a = ap.parse_args()
    bad = [k for k in os.environ if any(h in k.upper() for h in SECRET_HINTS)]
    if bad:
        print("REFUSED: secret-like env names present (count=%d); run under env -i" % len(bad)); return 4
    if os.path.exists(a.out):
        print("REFUSED: out exists"); return 4
    st = subprocess.run(["git", "-C", a.repo, "status", "--porcelain"], capture_output=True, text=True)
    if st.returncode != 0 or st.stdout.strip():
        print("REFUSED: repo not clean"); return 4
    raw = open(a.capture, "rb").read()
    if hashlib.sha256(raw).hexdigest() != a.capture_sha256:
        print("REFUSED: capture sha256 mismatch"); return 4
    hdr = json.loads(raw.decode().splitlines()[0])
    mints = hdr.get("picked_mints") or []
    if hdr.get("kind") != "agt-real-capture" or len(mints) != 1 or not isinstance(mints[0], str) or not mints[0].isalnum():
        print("REFUSED: capture header/picked mint not as expected"); return 4
    base = const_from(os.path.join(a.repo, "daemon/launch_scanner/jup_price.py"), "PRICE_V3_URL")
    if base != "https://api.jup.ag/price/v3":
        print("REFUSED: repo PRICE_V3_URL is not the expected url"); return 4
    url = base + "?ids=" + mints[0]
    curl = shutil.which("curl")
    if not curl:
        print("REFUSED: no curl"); return 4
    t0 = time.time()
    p = subprocess.Popen([curl, "-q", "-sS", "-X", "GET", "--proto", "=https", "--max-redirs", "0", "--noproxy", "*", "--max-time", "15",
                          "-w", "\n%{http_code}", "-H", "User-Agent: agt-capture/1", url],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={"PATH": os.path.dirname(curl)})
    buf, over = b"", False
    while True:
        chunk = p.stdout.read(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > BODY_CAP + 16:
            over = True; p.kill(); break
    rc = p.wait()
    body, _, code = buf.rpartition(b"\n")
    status = int(code) if code.strip().isdigit() else 0
    rec = {"url": url, "status": status, "fetched_utc": t0, "curl_rc": rc, "oversize": over,
           "body_sha256": None if over else hashlib.sha256(body).hexdigest(), "body_len": None if over else len(body),
           "body_b64": None if over else base64.b64encode(body).decode()}
    head = {"kind": "agt-real-capture-jup", "tool": os.path.abspath(__file__),
            "tool_sha256": hashlib.sha256(open(__file__, "rb").read()).hexdigest(),
            "repo_head": subprocess.run(["git", "-C", a.repo, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
            "t0_utc": t0, "url": url, "mint": mints[0], "linked_capture_sha256": a.capture_sha256, "requests": 1}
    with open(a.out, "x") as f:
        f.write(json.dumps(head) + "\n" + json.dumps(rec) + "\n")
    print("status=%s curl_rc=%s oversize=%s body_len=%s" % (status, rc, over, rec["body_len"]))
    print("WHOLE-FILE sha256 %s" % hashlib.sha256(open(a.out, "rb").read()).hexdigest())
    return 0 if status == 200 and not over else 1


if __name__ == "__main__":
    sys.exit(main())
