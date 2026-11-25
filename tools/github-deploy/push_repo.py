#!/usr/bin/env python3
"""Deploy / sync a local project tree to a GitHub repo via the git-database API.

Uses the stored custom.github credential through the authd surrogate
(bearer header, approved hosts api.github.com / uploads.github.com).
The secret value is never read, printed, or stored.

Sync logic: compares local git blob SHAs (computed locally, no API calls)
against the remote main tree. Pushes a new commit only when files changed,
added, or deleted. Exits quietly with code 0 when nothing changed.

Usage: push_repo.py <project_dir> [--repo NAME] [--owner NAME] [--private] [--branch BRANCH] [--message MSG] [--description DESC] [--extra SRC:DEST ...] [--extra-skip-dir NAME ...] [--dry-run]

  <project_dir> is pushed to the repo root. Each --extra adds more code:
  a directory SRC lands under repo prefix DEST; a single file SRC lands at
  exact repo path DEST; a glob SRC (e.g. dir/*.py) lands each match under
  DEST by basename. --extra-skip-dir names a directory skipped inside
  extra sources only (never the root -- the bot repo keeps its own
  tests/data and worker/ trees). --dry-run compares and lists what would
  change, pushes nothing. Files whose contents match a secret shape
  (private-key block, keypair array, UUID key, Telegram token) are never
  pushed, whatever their filename; --extra files/globs are subject to
  the same filename exclusions as directory collections.
"""
from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/opt/hatch/skills/skill-creator/bin")


def _add_surrogate_to_request(req):
    # Imported lazily: dynamic_credentials only exists on the agent
    # runtime, but the secret guard below is unit-tested standalone
    # (tests/test_security_fixes.py) where that runtime is absent.
    from dynamic_credentials import add_surrogate_to_request
    return add_surrogate_to_request(req, CRED, allowed_hosts=ALLOWED)

API = "https://api.github.com"
ALLOWED = ("api.github.com", "uploads.github.com")
CRED = "custom.github"

EXCLUDE_DIRS = {".venv", "__pycache__", ".pytest_cache", "node_modules", ".git"}
EXCLUDE_SUFFIXES = (".db", ".db-journal", ".pid", ".pyc", ".pyo",
                    ".log", ".bak", ".env", ".key", ".pem", ".p12", ".pfx")
EXCLUDE_FILES = {"install-state.json", "secrets.env", ".DS_Store",
                 "keypair.json", "bot_token", "api_key", "api_key_discovery",
                 "reported.json",
                 "agentlink-recovery-lean.txt", "dry-run-list.txt"}
# Content guard: never push a real secret, whatever its filename.
# PEM: requires the BEGIN marker plus a long base64 body, so code that
# merely mentions or strips a PEM header (e.g. fcm.js) does not trip it.
# Keypair array: a 64-number JSON array (a Solana keypair file's content).
# UUID: a Helius-style API key. Telegram token: bot-token shape.
# Deliberately NOT a bare 88-char base58 string: a Solana private key
# and a transaction signature are both 88-char base58 and
# indistinguishable by shape, and the tree legitimately holds
# signatures (a snapshot fixture and a public-RPC cursor) — that
# pattern would delete real files on the next sync, so it stays out
# (audit 2026-11-21). This file's own guard definition does not
# match itself.
SECRET_RE = re.compile(
    rb"-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----.{0,200}?[0-9A-Za-z+/]{64,}"
    rb"|\[\s*\d{1,3}(,\s*\d{1,3}){63}\s*\]"
    rb"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    rb"|\d{6,10}:[A-Za-z0-9_-]{30,}",
    re.DOTALL,
)
# Well-known public constants that LOOK like UUIDs but are not secrets:
# the RFC 4122 example UUID (used in help text / tests) and the RFC 6455
# WebSocket GUID (a fixed protocol constant). A match equal to one of
# these is not a secret. The nil UUID is a common placeholder, too.
_SECRET_PLACEHOLDERS = frozenset({
    b"123e4567-e89b-12d3-a456-426614174000",
    b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11",
    b"00000000-0000-0000-0000-000000000000",
})


def _contains_secret(data: bytes) -> bool:
    """True when data holds a secret-shaped value that is not a known
    public placeholder (see _SECRET_PLACEHOLDERS)."""
    return any(m.group(0) not in _SECRET_PLACEHOLDERS
               for m in SECRET_RE.finditer(data))


def _is_excluded_name(filename: str) -> bool:
    """True when a filename is never pushed (EXCLUDE_FILES / suffixes /
    .bak), mirroring collect_files(). Applies to --extra additions too."""
    base = os.path.basename(filename)
    return (base in EXCLUDE_FILES or base.endswith(EXCLUDE_SUFFIXES)
            or ".bak" in base)


def api(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "User-Agent": "nova-deploy-script",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    _add_surrogate_to_request(req)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            if not raw.strip():
                return {}  # e.g. 204 No Content on DELETE
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {body}") from e


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def collect_files(root: str, prefix: str = "", skip_dirs: frozenset = frozenset()) -> dict[str, tuple[str, str]]:
    """Return {repo_path: (fs_path, mode)} for a directory tree.

    prefix: repo path the tree lands under ("" = repo root).
    skip_dirs: extra directory names to skip, in addition to EXCLUDE_DIRS
    (applies to this source only -- the bot repo itself keeps its own
    tests/data and worker/ trees).
    """
    out: dict[str, tuple[str, str]] = {}
    skip = EXCLUDE_DIRS | set(skip_dirs)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn in EXCLUDE_FILES or fn.endswith(EXCLUDE_SUFFIXES) or ".bak" in fn:
                continue
            fs = os.path.join(dirpath, fn)
            rel = os.path.relpath(fs, root).replace(os.sep, "/")
            if prefix:
                rel = prefix.rstrip("/") + "/" + rel
            mode = "100755" if os.access(fs, os.X_OK) else "100644"
            out[rel] = (fs, mode)
    return out


def remote_tree(owner: str, repo: str, branch: str):
    """Return (entries {path: (sha, mode)}, tree_sha, commit_sha) or None if no branch."""
    try:
        ref = api("GET", f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
    except RuntimeError as e:
        if "HTTP 404" in str(e):
            return None
        raise
    commit_sha = ref["object"]["sha"]
    commit = api("GET", f"/repos/{owner}/{repo}/git/commits/{commit_sha}")
    tree_sha = commit["tree"]["sha"]
    tree = api("GET", f"/repos/{owner}/{repo}/git/trees/{tree_sha}?recursive=1")
    entries = {
        e["path"]: (e["sha"], e.get("mode", "100644"))
        for e in tree["tree"]
        if e["type"] == "blob"
    }
    return entries, tree_sha, commit_sha


# tools/phone-test is a FROZEN, UNQUALIFIED live-wallet (Termux) bundle that is maintained outside this repo and carries older execution
# semantics (e.g. quote price impact compared as percent without ratio conversion). It must never be synced by default, and because this
# push MIRRORS (remote paths absent locally are deleted) omitting it must not delete or silently replace the remote copy either.
UNQUALIFIED_PREFIXES = ("tools/phone-test/",)
ALLOW_UNQUALIFIED_ENV = "AGT_ALLOW_UNQUALIFIED_PHONE_BUNDLE"


def _norm_dest(path: str):
    """Normalize a repo path/destination: backslashes -> '/', collapse '//' and '.', resolve '..' (a path that escapes or climbs is returned as
    None = refuse), strip leading '/', casefold. Returns the normalized string ('' for the repo root) or None when unresolvable."""
    import posixpath
    p = str(path).replace("\\", "/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if ".." in parts:
        return None
    return posixpath.normpath("/".join(parts)).casefold() if parts else ""


def _is_unqualified(path: str) -> bool:
    n = _norm_dest(path)
    if n is None:
        return True            # unresolvable path: treat as unqualified (refuse / preserve)
    return any(n == x.rstrip("/") or n.startswith(x) for x in UNQUALIFIED_PREFIXES)


def _is_ancestor_of_unqualified(path: str) -> bool:
    n = _norm_dest(path)
    return n is not None and any(x.rstrip("/").startswith(n + "/") or n == "" for x in UNQUALIFIED_PREFIXES) and not _is_unqualified(path)


def guard_extras(extras, environ):
    """Refuse an --extra whose destination is under an unqualified prefix unless explicitly allowed. Returns None, or an error string."""
    if environ.get(ALLOW_UNQUALIFIED_ENV) == "1":
        return None
    for spec in extras:
        src, _, dest = spec.partition(":")
        if _norm_dest(dest) is None:
            return "refusing --extra %r: destination cannot be normalized (contains '..')" % spec
        if _is_ancestor_of_unqualified(dest) and os.path.isdir(src):
            for _dp, _dn, _fn in os.walk(src):
                if any(d.casefold() == "phone-test" for d in _dn):
                    return "refusing --extra %r: destination is a parent of tools/phone-test and the source contains a phone-test directory" % spec
        if dest and _is_unqualified(dest):
            return ("refusing --extra %r: %s is an unqualified frozen live-wallet bundle; set %s=1 only after it is qualified"
                    % (spec, dest, ALLOW_UNQUALIFIED_ENV))
    return None


def guard_mirror(deleted, changed, environ):
    """Under the default guard, remote paths under an unqualified prefix are left untouched: never deleted, never overwritten.
    Returns (deleted, changed, preserved_count)."""
    if environ.get(ALLOW_UNQUALIFIED_ENV) == "1":
        return deleted, changed, 0
    keep = [p for p in deleted if _is_unqualified(p)]
    return ([p for p in deleted if not _is_unqualified(p)], [p for p in changed if not _is_unqualified(p)], len(keep))


def main() -> int:
    args = sys.argv[1:]
    if not args or "-h" in args or "--help" in args:
        print(__doc__)
        return 2
    root = os.path.abspath(args[0])
    repo = "meme-project"
    owner = "olamoreA"
    branch = "main"
    private = False
    message = "Sync all project code"
    description = "Solana memecoin paper-trading bot (paper-only): launch discovery, safety gates, two-tier exits, Telegram alerts."
    extras: list[str] = []
    extra_skip_dirs: set[str] = set()
    dry_run = False
    i = 1
    while i < len(args):
        if args[i] == "--repo":
            repo = args[i + 1]; i += 2
        elif args[i] == "--owner":
            owner = args[i + 1]; i += 2
        elif args[i] == "--branch":
            branch = args[i + 1]; i += 2
        elif args[i] == "--message":
            message = args[i + 1]; i += 2
        elif args[i] == "--description":
            description = args[i + 1]; i += 2
        elif args[i] == "--extra":
            extras.append(args[i + 1]); i += 2
        elif args[i] == "--extra-skip-dir":
            extra_skip_dirs.add(args[i + 1]); i += 2
        elif args[i] == "--dry-run":
            dry_run = True; i += 1
        elif args[i] == "--private":
            private = True; i += 1
        else:
            i += 1

    me = api("GET", "/user")
    if me["login"] != owner:
        print(f"warning: authenticated as {me['login']}, pushing to {owner}/{repo}")

    if not dry_run:
        try:
            created = api(
                "POST", "/user/repos",
                {"name": repo, "private": private, "auto_init": True,
                 "description": description},
            )
            print(f"created repo: {created['full_name']}")
        except RuntimeError as e:
            if "HTTP 422" not in str(e):
                raise
            print(f"repo {owner}/{repo} exists")

    _err = guard_extras(extras, os.environ)
    if _err:
        raise SystemExit(_err)
    local = collect_files(root)
    for spec in extras:
        src, _, dest = spec.partition(":")
        if any(c in src for c in "*?["):
            matches = sorted(glob.glob(src))
            if not matches:
                raise SystemExit(f"--extra glob matched nothing: {spec}")
            for m in matches:
                if not os.path.isfile(m):
                    continue
                if _is_excluded_name(m):
                    print(f"SKIPPED (excluded name, not pushed): {m}")
                    continue
                rel = dest.rstrip("/") + "/" + os.path.basename(m)
                if rel in local:
                    raise SystemExit(f"--extra collision at {rel}")
                local[rel] = (m, "100755" if os.access(m, os.X_OK) else "100644")
            continue
        src = os.path.abspath(os.path.expanduser(src))
        if os.path.isfile(src):
            if not dest:
                raise SystemExit(f"--extra file source needs a dest path: {spec}")
            if _is_excluded_name(src):
                print(f"SKIPPED (excluded name, not pushed): {src}")
                continue
            if dest in local:
                raise SystemExit(f"--extra collision at {dest}")
            local[dest] = (src, "100755" if os.access(src, os.X_OK) else "100644")
        elif os.path.isdir(src):
            if not dest:
                raise SystemExit(f"--extra dir source needs a dest prefix: {spec}")
            new = collect_files(src, prefix=dest, skip_dirs=frozenset(extra_skip_dirs))
            clash = set(new) & set(local)
            if clash:
                raise SystemExit(f"--extra collision at {sorted(clash)[0]}")
            local.update(new)
        else:
            raise SystemExit(f"--extra source not found: {src}")

    local_meta: dict[str, tuple[str, str]] = {}  # path -> (blob_sha, mode)
    skipped_secrets: list[str] = []
    for rel, (fs, mode) in local.items():
        with open(fs, "rb") as fh:
            data = fh.read()
        if _contains_secret(data):
            skipped_secrets.append(rel)
            continue
        local_meta[rel] = (blob_sha(data), mode)
    for rel in skipped_secrets:
        print(f"SKIPPED (secret marker, not pushed): {rel}")

    rt = remote_tree(owner, repo, branch)
    if rt is None:
        changed = sorted(local_meta)          # empty repo: push everything
        deleted: list[str] = []
        base_tree = None
        parent = None
    else:
        remote_entries, base_tree, parent = rt
        changed = [p for p in local_meta if local_meta[p] != remote_entries.get(p)]
        deleted = [p for p in remote_entries if p not in local_meta]
        if not changed and not deleted:
            print(f"no changes vs {owner}/{repo}@{branch}; nothing to push")
            return 0

    deleted, changed, _kept = guard_mirror(deleted, changed, os.environ)
    if _kept or not os.environ.get(ALLOW_UNQUALIFIED_ENV):
        print(f"NOTICE: tools/phone-test is NOT part of this sync (unqualified frozen live-wallet bundle); remote copy left untouched ({_kept} remote paths preserved). This is NOT a complete phone release.")
    if not changed and not deleted:
        print("nothing to push after guard"); return 0
    print(f"{len(changed)} changed/new, {len(deleted)} deleted")
    if dry_run:
        for p in sorted(changed):
            print(f"  + {p}")
        for p in sorted(deleted):
            print(f"  - {p}")
        print("dry run: nothing pushed")
        return 0

    def make_blob(path: str) -> tuple[str, str]:
        fs, _ = local[path]
        with open(fs, "rb") as fh:
            content = base64.b64encode(fh.read()).decode("ascii")
        b = api("POST", f"/repos/{owner}/{repo}/git/blobs",
                {"content": content, "encoding": "base64"})
        return path, b["sha"]

    new_shas: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for n, (path, sha) in enumerate(ex.map(make_blob, changed), 1):
            new_shas[path] = sha
            if n % 100 == 0:
                print(f"  blobs {n}/{len(changed)}")

    entries = [
        {"path": p, "mode": local[p][1], "type": "blob", "sha": new_shas[p]}
        for p in changed
    ] + [{"path": p, "mode": "100644", "type": "blob", "sha": None} for p in deleted]
    tree_payload: dict = {"tree": entries}
    if base_tree:
        tree_payload["base_tree"] = base_tree
    tree = api("POST", f"/repos/{owner}/{repo}/git/trees", tree_payload)

    commit_payload: dict = {"message": message, "tree": tree["sha"]}
    if parent:
        commit_payload["parents"] = [parent]
    commit = api("POST", f"/repos/{owner}/{repo}/git/commits", commit_payload)

    if parent:
        api("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}",
            {"sha": commit["sha"], "force": False})
        print(f"updated {branch}: {parent[:7]} -> {commit['sha'][:7]}")
    else:
        api("POST", f"/repos/{owner}/{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        print(f"created {branch} at {commit['sha'][:7]}")
    print(f"https://github.com/{owner}/{repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
