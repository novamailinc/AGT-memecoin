#!/usr/bin/env python3
"""Tests for the 2026-11-21 security fixes (audit follow-up).

Plain script — run directly with the venv python, one suite at a time:
    python3 tests/test_security_fixes.py

Covers (no network, no real secrets — every secret-shaped value below
is built at runtime from parts, never written out as a literal, so
this file itself stays clean under the push guard):
  * HIGH-1: push guard patterns match synthetic keypair arrays, UUID
    keys and Telegram tokens; known public placeholders (RFC example
    UUID, RFC 6455 WebSocket GUID) and the fcm.js PEM-header strip do
    NOT match; api_key_discovery is excluded; --extra names are
    excluded too.
  * MEDIUM-1: /repair only re-pairs for the env allowlist; a wrong
    chat is denied and the pairing is untouched; with no allowlist
    configured nobody may repair.
  * MEDIUM-2: a keyed Helius URL never appears in the curl argv; it
    travels in the -K config file, which is deleted after the call.
  * MEDIUM-3: /sethelius and /sethelius2 verify BEFORE saving (a
    rejected key is never written); key files are written 0600 via a
    pid-unique tmp file with no tmp leftovers.
  * LOW-1: _post_json logs strip the query string.
  * LOW-2: confirm codes are 8 chars.
  * LOW-3: killswitch-check redacts the bot token from its failure print.
  * LOW-4: .gitignore lists the key/secret entries.
"""

import contextlib
import importlib.util
import io
import json
import logging
import os
import stat
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_TMP_HOME = tempfile.mkdtemp(prefix="secfixes-home-")
os.environ["FOREX_AGENT_HOME"] = _TMP_HOME

from daemon.telegram import security as tg_security  # noqa: E402
from daemon.telegram import commands as tg_commands  # noqa: E402
from daemon.launch_scanner import reserves  # noqa: E402
from daemon.launch_scanner import sources  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PUSH_REPO = os.path.join(_ROOT, "tools", "github-deploy",
                          "push_repo.py")

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def _load_push_repo():
    spec = importlib.util.spec_from_file_location("push_repo", _PUSH_REPO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _join(parts, sep="-"):
    return sep.join(parts)


# ----------------------------------------------------------------------
# HIGH-1 — push guard
# ----------------------------------------------------------------------
def test_guard():
    pr = _load_push_repo()
    has_secret = pr._contains_secret

    # Solana keypair JSON array: 64 numbers -> secret; 63 -> not.
    arr64 = ("[" + ",".join(str(i % 256) for i in range(64)) + "]").encode()
    arr63 = ("[" + ",".join(str(i % 256) for i in range(63)) + "]").encode()
    check("guard: 64-number keypair array matches", has_secret(arr64))
    check("guard: 63-number array does not match", not has_secret(arr63))

    # UUID key: a non-placeholder UUID matches; known public
    # placeholders do not.
    uuid_real = _join(["11111111", "2222", "3333", "4444",
                       "555555555555"]).encode()
    uuid_example = _join(["123e4567", "e89b", "12d3", "a456",
                          "426614174000"]).encode()
    uuid_ws = _join(["258EAFA5", "E914", "47DA", "95CA",
                     "C5AB0DC85B11"]).encode()
    uuid_nil = _join(["00000000", "0000", "0000", "0000",
                      "000000000000"]).encode()
    check("guard: non-placeholder UUID matches", has_secret(uuid_real))
    check("guard: RFC example UUID does not match",
          not has_secret(uuid_example))
    check("guard: RFC 6455 WebSocket GUID does not match",
          not has_secret(uuid_ws))
    check("guard: nil UUID does not match", not has_secret(uuid_nil))

    # Telegram token shape matches; a short value does not.
    token = (str(123456789) + ":" + "A" * 35).encode()
    check("guard: Telegram token shape matches", has_secret(token))
    check("guard: short token-like value does not match",
          not has_secret(b"123:abc"))

    # PEM block with a body matches; a bare header (the fcm.js strip)
    # does not.
    pem = (b"-----BEGIN PRIVATE KEY-----\n" + bytes([65]) * 70
           + b"\n-----END PRIVATE KEY-----")
    check("guard: PEM block with body matches", has_secret(pem))
    check("guard: bare PEM header does not match",
          not has_secret(b".replace(/-----BEGIN PRIVATE KEY-----/, '')"))

    # The real fcm.js (PEM-header strip only) stays clean.
    fcm = open(os.path.join(_ROOT, "worker", "src", "fcm.js"), "rb").read()
    check("guard: worker/src/fcm.js does not match", not has_secret(fcm))
    # This file itself stays clean under the guard.
    self_bytes = open(os.path.abspath(__file__), "rb").read()
    check("guard: this test file does not match", not has_secret(self_bytes))
    # Deliberate omission (audit 2026-11-21): a bare 88-char base58
    # string is NOT flagged — it is indistinguishable from a public
    # transaction signature, and the tree legitimately holds those.
    check("guard: bare 88-char base58 is not flagged (omitted by design)",
          not has_secret(bytes([65]) * 88))

    # api_key_discovery is excluded, and --extra names are too.
    check("guard: api_key_discovery in EXCLUDE_FILES",
          "api_key_discovery" in pr.EXCLUDE_FILES)
    check("guard: _is_excluded_name(api_key_discovery)",
          pr._is_excluded_name("/x/api_key_discovery"))
    check("guard: _is_excluded_name(api_key)",
          pr._is_excluded_name("/x/api_key"))
    check("guard: _is_excluded_name(normal.py) is False",
          not pr._is_excluded_name("/x/normal.py"))
    check("guard: _is_excluded_name(x.pem) is True",
          pr._is_excluded_name("/x/whatever.pem"))


# ----------------------------------------------------------------------
# MEDIUM-2 — curl argv carries no key
# ----------------------------------------------------------------------
class _FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeRun:
    def __init__(self, proc):
        self.proc = proc
        self.calls = []
        self.config_contents = []
        self.config_modes = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if "-K" in argv:
            cfg = argv[argv.index("-K") + 1]
            try:
                self.config_modes.append(stat.S_IMODE(os.stat(cfg).st_mode))
                with open(cfg, "rb") as fh:
                    self.config_contents.append(fh.read())
            except OSError:
                self.config_contents.append(None)
        return self.proc


class _SubCtx:
    def __init__(self, fake):
        self.fake = fake
        self._real = None

    def __enter__(self):
        self._real = reserves.subprocess
        fake = self.fake

        class _Sub:
            run = staticmethod(fake)

        reserves.subprocess = _Sub
        return self

    def __exit__(self, *exc):
        reserves.subprocess = self._real
        return False


def test_curl_argv_no_key():
    key = "FAKEKEY" + "0" * 28
    keyed_url = "https://mainnet.helius-rpc.com/?api-key=" + key
    body = b'{"jsonrpc":"2.0","id":1,"method":"getSlot"}'
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"slot": 1}}
    out = json.dumps(payload).encode() + b"\n__HTTP_STATUS__:200"
    fake = _FakeRun(_FakeProc(0, out))
    with _SubCtx(fake):
        result, kind, _e = reserves._rpc_attempt(
            keyed_url, body, "getSlot", "mainnet.helius-rpc.com", 12)
    check("curl: keyed url returns ok",
          kind == "ok" and result == {"slot": 1})
    argv, _kw = fake.calls[0]
    joined = " ".join(argv)
    check("curl: key never appears in argv", key not in joined)
    check("curl: keyed url never appears in argv",
          keyed_url not in joined and "api-key" not in joined)
    check("curl: -K present in argv", "-K" in argv)
    check("curl: keyed url is in the -K config file",
          fake.config_contents and fake.config_contents[0] is not None
          and keyed_url.encode() in fake.config_contents[0])
    check("curl: -K config file deleted after the call",
          not os.path.exists(argv[argv.index("-K") + 1]))
    check("curl: -K config file was 0600 while it existed",
          fake.config_modes == [0o600])


# ----------------------------------------------------------------------
# LOW-2 — confirm code length
# ----------------------------------------------------------------------
def test_confirm_code_len():
    check("confirm: CONFIRM_CODE_LEN is 8",
          tg_security.CONFIRM_CODE_LEN == 8)
    sec = tg_security.SecurityContext({})
    code = sec.new_confirm("trade", {}, 111, 222, "req-1", now=1000.0)
    check("confirm: new code is 8 chars", len(code) == 8)
    check("confirm: code is hex", all(c in "0123456789ABCDEF" for c in code))


# ----------------------------------------------------------------------
# MEDIUM-1 — /repair
# ----------------------------------------------------------------------
CHAT, USER = 6883056760, 111222333
WRONG_CHAT, WRONG_USER = 999888777, 444555666


class _FakeCtx:
    def __init__(self, sec):
        self.sent = []
        self.published = []
        self.audit_log = []
        self.security = sec
        self.message_id = None
        self.ctx = tg_commands.HandlerContext(
            send=lambda c, t, m: self.sent.append((c, t, m)),
            security=sec, tools=None,
            publish=lambda e, s=None: self.published.append((e, s)),
            audit_log=self.audit_log,
            delete_message=lambda c, m: None)


def _set_allowlist(chat, user):
    if chat is None:
        os.environ.pop("TELEGRAM_ALLOWED_CHAT_ID", None)
    else:
        os.environ["TELEGRAM_ALLOWED_CHAT_ID"] = str(chat)
    if user is None:
        os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
    else:
        os.environ["TELEGRAM_ALLOWED_USER_ID"] = str(user)


def test_repair():
    saved_chat = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")
    saved_user = os.environ.get("TELEGRAM_ALLOWED_USER_ID")
    try:
        # Wrong chat is denied and the pairing is untouched.
        _set_allowlist(CHAT, USER)
        sec = tg_security.SecurityContext({})
        sec.try_pair(CHAT, USER)
        fx = _FakeCtx(sec)
        tg_commands.handle_message(fx.ctx, WRONG_CHAT, WRONG_USER, 1,
                                   "/repair")
        check("repair: wrong chat denied",
              any("denied" in (t or "").lower() for _, t, _ in fx.sent))
        check("repair: wrong chat leaves pairing untouched",
              sec.is_paired()
              and sec.state.get("paired_chat_id") == CHAT
              and sec.state.get("paired_user_id") == USER)
        check("repair: wrong chat is audited as unauthorized",
              sec.state.get("unauthorized_hits", 0) >= 1)

        # The allowlisted chat repairs (re-pairs to itself here).
        fx2 = _FakeCtx(sec)
        tg_commands.handle_message(fx2.ctx, CHAT, USER, 2, "/repair")
        check("repair: allowlisted chat succeeds",
              any("Repaired" in (t or "") for _, t, _ in fx2.sent))
        check("repair: pairing still held by allowlisted chat",
              sec.state.get("paired_chat_id") == CHAT)

        # Repair from a different chat moves the pairing to the owner.
        sec2 = tg_security.SecurityContext({})
        sec2.try_pair(WRONG_CHAT, WRONG_USER)
        fx3 = _FakeCtx(sec2)
        tg_commands.handle_message(fx3.ctx, CHAT, USER, 3, "/repair")
        check("repair: owner steals pairing back from wrong chat",
              sec2.state.get("paired_chat_id") == CHAT
              and sec2.state.get("paired_user_id") == USER)

        # Unpaired bot: allowlisted chat may /repair to pair.
        sec3 = tg_security.SecurityContext({})
        fx4 = _FakeCtx(sec3)
        tg_commands.handle_message(fx4.ctx, CHAT, USER, 4, "/repair")
        check("repair: unpaired bot pairs via allowlisted /repair",
              sec3.is_paired()
              and sec3.state.get("paired_chat_id") == CHAT)

        # No allowlist configured: nobody may repair, never open.
        _set_allowlist(None, None)
        sec4 = tg_security.SecurityContext({"paired_chat_id": CHAT,
                                            "paired_user_id": USER})
        check("repair: preexisting pairing exists before no-allowlist repair",
              sec4.is_paired())
        fx5 = _FakeCtx(sec4)
        tg_commands.handle_message(fx5.ctx, CHAT, USER, 5, "/repair")
        check("repair: denied with no allowlist configured",
              any("denied" in (t or "").lower() for _, t, _ in fx5.sent))
        check("repair: pairing untouched with no allowlist",
              sec4.state.get("paired_chat_id") == CHAT)
    finally:
        if saved_chat is None:
            os.environ.pop("TELEGRAM_ALLOWED_CHAT_ID", None)
        else:
            os.environ["TELEGRAM_ALLOWED_CHAT_ID"] = saved_chat
        if saved_user is None:
            os.environ.pop("TELEGRAM_ALLOWED_USER_ID", None)
        else:
            os.environ["TELEGRAM_ALLOWED_USER_ID"] = saved_user


# ----------------------------------------------------------------------
# MEDIUM-3 — verify before save; 0600 pid-unique tmp
# ----------------------------------------------------------------------
def test_sethelius_verify_first():
    home = tempfile.mkdtemp(prefix="secfixes-helius-")
    os.environ["FOREX_AGENT_HOME"] = home
    try:
        sec = tg_security.SecurityContext({})
        sec.try_pair(CHAT, USER)
        deleted = []
        fx = _FakeCtx(sec)
        fx.ctx.message_id = 99
        fx.ctx.delete_message = lambda c, m: deleted.append((c, m))

        real_verify = tg_commands._verify_helius_key
        real_write = tg_commands._write_helius_key
        real_write2 = tg_commands._write_helius_discovery_key
        writes = []
        try:
            tg_commands._verify_helius_key = lambda k, timeout=15.0: (
                False, "HTTP 401 from Helius")
            tg_commands._write_helius_key = lambda k: writes.append(
                ("main", k))
            tg_commands._write_helius_discovery_key = lambda k: writes.append(
                ("discovery", k))
            tg_commands._cmd_sethelius(fx.ctx, CHAT, USER, 1, ["k" * 20])
            check("sethelius: rejected key is never written",
                  writes == [])
            check("sethelius: rejected key leaves no key file",
                  not os.path.exists(
                      os.path.join(home, "helius", "api_key")))
            check("sethelius: rejected key reports nothing saved",
                  any("Nothing was saved" in (t or "")
                      for _, t, _ in fx.sent))
            check("sethelius: command message scrubbed even on failure",
                  deleted == [(CHAT, 99)])

            tg_commands._verify_helius_key = lambda k, timeout=15.0: (
                True, "12345")
            fx2 = _FakeCtx(sec)
            tg_commands._cmd_sethelius(fx2.ctx, CHAT, USER, 2, ["k" * 20])
            check("sethelius: verified key is written",
                  writes == [("main", "k" * 20)])
            check("sethelius: verified key reports saved",
                  any("saved" in (t or "") for _, t, _ in fx2.sent))

            tg_commands._verify_helius_key = lambda k, timeout=15.0: (
                False, "HTTP 401 from Helius")
            fx3 = _FakeCtx(sec)
            tg_commands._cmd_sethelius2(fx3.ctx, CHAT, USER, 3, ["k" * 20])
            check("sethelius2: rejected key is never written",
                  writes == [("main", "k" * 20)])
            check("sethelius2: rejected key leaves no discovery file",
                  not os.path.exists(
                      os.path.join(home, "helius", "api_key_discovery")))
        finally:
            tg_commands._verify_helius_key = real_verify
            tg_commands._write_helius_key = real_write
            tg_commands._write_helius_discovery_key = real_write2

        # The real writer: 0600 file, pid-unique tmp, no leftovers.
        tg_commands._write_helius_key("k" * 20)
        key_path = os.path.join(home, "helius", "api_key")
        check("sethelius: key file exists",
              os.path.exists(key_path))
        check("sethelius: key file is 0600",
              stat.S_IMODE(os.stat(key_path).st_mode) == 0o600)
        leftovers = [f for f in os.listdir(os.path.join(home, "helius"))
                     if ".tmp." in f]
        check("sethelius: no tmp leftovers", leftovers == [])
        tg_commands._write_helius_discovery_key("d" * 20)
        disc_path = os.path.join(home, "helius", "api_key_discovery")
        check("sethelius2: discovery file is 0600",
              stat.S_IMODE(os.stat(disc_path).st_mode) == 0o600)
    finally:
        os.environ["FOREX_AGENT_HOME"] = _TMP_HOME


# ----------------------------------------------------------------------
# LOW-1 — _post_json strips the query string from logs
# ----------------------------------------------------------------------
def test_post_json_redacts_query():
    records = []

    class _Cap(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger("forex_agent.daemon.launch_scanner.sources")
    handler = _Cap()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    real_curl = sources._curl_bytes
    real_urlopen = urllib.request.urlopen
    try:
        sources._curl_bytes = lambda *a, **k: None
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            ValueError("boom"))
        out = sources._post_json(
            "https://example.invalid/x?api-key=SECRETQ123",
            {"type": "spotMeta"})
        check("post_json: failure returns None", out is None)
        blob = "\n".join(records)
        check("post_json: log names the path without the query",
              "https://example.invalid/x" in blob)
        check("post_json: query value never logged",
              "SECRETQ123" not in blob and "api-key" not in blob)
    finally:
        sources._curl_bytes = real_curl
        urllib.request.urlopen = real_urlopen
        logger.removeHandler(handler)


# ----------------------------------------------------------------------
# LOW-3 — killswitch-check redacts the token
# ----------------------------------------------------------------------
def test_killswitch_check_redact():
    home = tempfile.mkdtemp(prefix="secfixes-ks-")
    os.environ["FOREX_AGENT_HOME"] = home
    token = "secret-token-value-123"
    os.makedirs(os.path.join(home, "telegram"), exist_ok=True)
    with open(os.path.join(home, "telegram", "bot_token"), "w") as fh:
        fh.write(token + "\n")
    # Extensionless script: needs an explicit loader.
    from importlib.machinery import SourceFileLoader
    spec = importlib.util.spec_from_loader(
        "killswitch_check",
        SourceFileLoader("killswitch_check",
                         os.path.join(_ROOT, "scripts", "killswitch-check")))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        msg = "boom https://api.telegram.org/bot" + token + "/sendMessage"
        redacted = mod.redact(msg, token)
        check("killswitch-check: redact strips the token",
              token not in redacted)
        check("killswitch-check: redact marks the spot",
              "[redacted]" in redacted or "[REDACTED]" in redacted)

        real_urlopen = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            Exception("boom https://api.telegram.org/bot"
                      + token + "/sendMessage"))
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                mod.send_telegram("hi")
            printed = buf.getvalue()
        finally:
            urllib.request.urlopen = real_urlopen
        check("killswitch-check: failure print never carries the token",
              token not in printed)
        check("killswitch-check: failure print still reports the failure",
              "telegram FAIL" in printed)
    finally:
        os.environ["FOREX_AGENT_HOME"] = _TMP_HOME


# ----------------------------------------------------------------------
# LOW-4 — .gitignore
# ----------------------------------------------------------------------
def test_gitignore():
    lines = [ln.strip() for ln in
             open(os.path.join(_ROOT, ".gitignore")).read().splitlines()]
    for entry in ("keypair.json", "devnet/", "helius/", "telegram/",
                  "live/", "*.pem", "*.key", "*.env"):
        check("gitignore: has %s" % entry, entry in lines)


def main():
    test_guard()
    test_curl_argv_no_key()
    test_confirm_code_len()
    test_repair()
    test_sethelius_verify_first()
    test_post_json_redacts_query()
    test_killswitch_check_redact()
    test_gitignore()
    print()
    if FAILS:
        print("FAILURES:", FAILS)
        sys.exit(1)
    print("ALL SECURITY-FIX TESTS PASSED")


if __name__ == "__main__":
    main()


if __name__ != "__main__":
    from tests.recorded_checks import recorded_check_guard
