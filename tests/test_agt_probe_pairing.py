"""AGT provider probe matches the deploy contract; pairing preflight status.

Offline only: urlopen is made to raise in every test, the live-mode
tests inject a fake opener. Fake values are the public example UUIDs
the repo's secret guard already allowlists, never real credentials.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
U1 = "123e4567-e89b-12d3-a456-426614174000"
U2 = "00000000-0000-0000-0000-000000000000"
U3 = "123e4567-e89b-12d3-a456-426614174001"
CS = "https://chainstack.example.invalid/fixture-node-secret"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PROBE = _load("agt_key_probe", "tools/agt_key_test.py")
CREDS = _load("agt_creds_pairing", "deploy/agt/credentials.py")


def full_env(**over):
    env = {"HELIUS_API_KEY_1": U1, "HELIUS_API_KEY_2": U2,
           "HELIUS_API_KEY_3": U3, "ALCHEMY_API_KEY": "alchemyFixtureKey1",
           "CHAINSTACK_ENDPOINT": CS}
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call attempted")
    monkeypatch.setattr(PROBE.urllib.request, "urlopen", boom)


def test_contract_has_exactly_the_five_deploy_slots():
    probes = PROBE.build_probes(full_env())
    assert [p[0] for p in probes] == list(PROBE.SLOTS)
    assert all(p[1] for p in probes)
    assert probes[0][1] == "https://mainnet.helius-rpc.com/?api-key=" + U1
    assert probes[3][1].startswith("https://solana-mainnet.g.alchemy.com/v2/")
    assert probes[4][1] == CS


def test_legacy_helius_key_does_not_fill_a_numbered_slot():
    env = full_env(HELIUS_API_KEY_2=None, HELIUS_API_KEY_3=None,
                   HELIUS_API_KEY=U1)
    probes = PROBE.build_probes(env)
    assert probes[1][1] is None and probes[2][1] is None
    assert "HELIUS_API_KEY_2" in probes[1][3]


def test_old_probe_variable_is_not_the_contract():
    # the previous probe read CHAINSTACK_API_KEY; the contract is the endpoint
    env = full_env(CHAINSTACK_ENDPOINT=None, CHAINSTACK_API_KEY="somekey")
    assert PROBE.build_probes(env)[4][1] is None


@pytest.mark.parametrize("name,val", [
    ("HELIUS_API_KEY_1", "TEST-fixture"),
    ("HELIUS_API_KEY_2", "https://mainnet.helius-rpc.com/?api-key=" + U2),
    ("ALCHEMY_API_KEY", "TEST-fixture"),
    ("ALCHEMY_API_KEY", "https://evil.example.invalid/v2/key"),
    ("CHAINSTACK_ENDPOINT", "http://insecure.example.invalid/x"),
    ("CHAINSTACK_ENDPOINT", "TEST-fixture"),
])
def test_bad_values_fail_the_contract(name, val):
    probes = PROBE.build_probes(full_env(**{name: val}))
    assert sum(1 for p in probes if p[1] is None) == 1


def test_supported_alchemy_url_is_accepted_like_the_provider():
    url = "https://solana-mainnet.g.alchemy.com/v2/fixtureKey"
    p = PROBE.build_probes(full_env(ALCHEMY_API_KEY=url))[3]
    assert p[1] == url and p[2] == "fixtureKey"


def test_default_mode_is_offline_and_prints_no_secret(capsys):
    rc = PROBE.main([], env=full_env())
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT PROBED" in out and "no provider was called" in out
    for secret in (U1, U2, U3, "alchemyFixtureKey1", CS,
                   "fixture-node-secret"):
        assert secret not in out


def test_offline_default_fails_when_a_slot_is_missing(capsys):
    rc = PROBE.main([], env=full_env(HELIUS_API_KEY_3=None))
    assert rc == 1
    assert "helius-3: CONTRACT NOT MET" in capsys.readouterr().out


def test_identical_helius_values_are_noted_without_printing_them(capsys):
    PROBE.main([], env=full_env(HELIUS_API_KEY_2=U1))
    out = capsys.readouterr().out
    assert "identical values" in out and U1 not in out


def _opener(results):
    calls = []

    def op(url, secrets):
        calls.append(url)
        return results[len(calls) - 1]
    op.calls = calls
    return op


OK = (200, json.dumps({"jsonrpc": "2.0", "result": "ok", "id": 1}))


def test_live_passes_only_when_every_slot_is_healthy(capsys):
    op = _opener([OK] * 5)
    assert PROBE.main(["--live"], env=full_env(), opener=op) == 0
    assert len(op.calls) == 5
    out = capsys.readouterr().out
    assert out.count("PASS") == 5 and U1 not in out and CS not in out


@pytest.mark.parametrize("bad", [
    (401, '{"error":"unauthorized"}'),
    (200, json.dumps({"jsonrpc": "2.0", "error": {"code": -32000}})),
    (200, "not json"),
    (None, "URLError"),
])
def test_live_fails_on_any_unhealthy_slot(bad, capsys):
    op = _opener([OK, OK, bad, OK, OK])
    assert PROBE.main(["--live"], env=full_env(), opener=op) == 1
    assert "helius-3: FAIL" in capsys.readouterr().out


def test_live_does_not_probe_slots_that_miss_the_contract(capsys):
    op = _opener([OK] * 4)
    rc = PROBE.main(["--live"], env=full_env(HELIUS_API_KEY_2=None),
                    opener=op)
    assert rc == 1 and len(op.calls) == 4


def test_error_bodies_are_redacted():
    assert "<redacted>" in PROBE.redact("bad key " + U1, [U1])
    assert U1 not in PROBE.redact("bad key " + U1, [U1])


def test_unknown_argument_is_a_usage_error():
    assert PROBE.main(["--probe-everything"], env=full_env()) == 2


# ---------------- Telegram pairing status ----------------
@pytest.mark.parametrize("env,want", [
    ({}, "unconfigured"),
    ({"TELEGRAM_ALLOWED_CHAT_ID": "1"}, "incomplete_allowlist"),
    ({"TELEGRAM_ALLOWED_USER_ID": "2"}, "incomplete_allowlist"),
    ({"TELEGRAM_ALLOWED_CHAT_ID": "1", "TELEGRAM_ALLOWED_USER_ID": "2"},
     "allowlist"),
    ({"TELEGRAM_PAIR_CODE": "x"}, "pair_code"),
    ({"TELEGRAM_PAIR_CODE_SHA256": "ab"}, "pair_code"),
    ({"TELEGRAM_PAIR_CODE": "x", "TELEGRAM_PAIR_CODE_EXPIRES": "100"},
     "pair_code_expired"),
    ({"TELEGRAM_PAIR_CODE": "x", "TELEGRAM_PAIR_CODE_EXPIRES": "junk"},
     "pair_code_expired"),
])
def test_pairing_status_mirrors_try_pair_rules(env, want):
    assert CREDS.telegram_pairing_status(env, now=1000.0) == want


def test_pairing_status_agrees_with_the_real_security_layer(monkeypatch):
    sys.path.insert(0, str(ROOT))
    from daemon.telegram.security import SecurityContext
    cases = [
        ({}, False),
        ({"TELEGRAM_ALLOWED_CHAT_ID": "1"}, False),
        ({"TELEGRAM_ALLOWED_CHAT_ID": "1",
          "TELEGRAM_ALLOWED_USER_ID": "2"}, True),
    ]
    for env, can_pair in cases:
        for k in ("TELEGRAM_ALLOWED_CHAT_ID", "TELEGRAM_ALLOWED_USER_ID",
                  "TELEGRAM_PAIR_CODE", "TELEGRAM_PAIR_CODE_SHA256"):
            monkeypatch.delenv(k, raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        ok, _ = SecurityContext({}).try_pair("1", "2")
        assert ok is can_pair
        status = CREDS.telegram_pairing_status(env)
        assert (status == "allowlist") is can_pair


def test_preflight_reports_pairing_status_and_no_value(tmp_path):
    env = {c["env"]: U1 for c in CREDS.CREDENTIALS if c["required"]}
    env["TELEGRAM_BOT_TOKEN"] = "1234567:fixtureTokenNotReal"
    env["FOREX_AGENT_HOME"] = str(tmp_path / "h")
    res = CREDS.preflight(home=env["FOREX_AGENT_HOME"], env=env)
    assert res["telegram_pairing"] == "unconfigured"
    env["TELEGRAM_ALLOWED_CHAT_ID"] = "111"
    env["TELEGRAM_ALLOWED_USER_ID"] = "222"
    env["FOREX_AGENT_HOME"] = str(tmp_path / "h2")
    res = CREDS.preflight(home=env["FOREX_AGENT_HOME"], env=env)
    assert res["telegram_pairing"] == "allowlist"
    assert "111" not in json.dumps(res) and "222" not in json.dumps(res)


def test_pairing_envs_stay_out_of_the_manifest_inventory():
    names = {c["env"] for c in CREDS.CREDENTIALS}
    assert not names & set(CREDS.PAIRING_ENVS)
    manifest = json.loads((ROOT / "deploy/agt/manifest.json").read_text())
    listed = {e["name"] for e in manifest["required_env"]
              + manifest["optional_env"]}
    assert listed == names


@pytest.mark.parametrize("url", [
    "https://user:pw-secret@node.example.invalid/path?key=qs-secret",
    "https://pw-secret@node.example.invalid:8443/x",
    "https://:pw-secret@node.example.invalid",
])
def test_host_of_never_returns_userinfo_port_path_or_query(url):
    h = PROBE.host_of(url)
    assert h == "node.example.invalid" and "secret" not in h and "@" not in h


@pytest.mark.parametrize("url", ["not a url", "https://[bad", ""])
def test_host_of_unparseable_is_placeholder(url):
    assert PROBE.host_of(url) == "?"


def test_userinfo_endpoint_secret_not_in_live_or_offline_output(capsys):
    ep = "https://user:pw-secret@chainstack.example.invalid/tok-secret"
    env = full_env(CHAINSTACK_ENDPOINT=ep)
    PROBE.main([], env=env)
    PROBE.main(["--live"], env=env, opener=_opener([OK] * 5))
    out = capsys.readouterr().out
    assert "pw-secret" not in out and "tok-secret" not in out and "user:" not in out


def test_check_survives_non_utf8_http_error_body(monkeypatch):
    import io, urllib.error
    def err(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 502, "bad", {}, io.BytesIO(b"\xff\xfe bad key " + U1.encode()))
    monkeypatch.setattr(PROBE.urllib.request, "urlopen", err)
    status, text = PROBE.check("https://h.example.invalid/x", [U1])
    assert status == 502 and U1 not in text


def test_check_survives_non_utf8_200_body(monkeypatch):
    import io
    class R(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(PROBE.urllib.request, "urlopen", lambda req, timeout=0: R(b"\xff\xfe"))
    status, text = PROBE.check("https://h.example.invalid/x", [])
    assert status == 200 and not PROBE.healthy(status, text)
