"""AGT credential resolution — env-var first, fail closed.

Phase 13 (AGT packaging skeleton). This module is the ONLY place the
AGT deployment resolves credentials. It never imports the
launch_scanner monolith and never performs network I/O.

Rules (standing):
  * Every credential comes from an environment variable injected by
    the AGT vault. Names only are documented here; values are never
    written to a file in the repo, never logged, and never printed.
  * The vault-surrogate bridge (daemon/launch_scanner/helius_vault.py)
    does NOT work on AGT: there is no authd socket and no egress
    proxy on the phone. It is replaced here by plain env reads. A
    missing surrogate is expected on AGT, never an error.
  * Missing a REQUIRED credential fails closed: the startup check
    raises CredentialError naming the missing variable(s) and the
    deployment refuses to start. There is never a silent default.
  * A few live readers still read credential FILES only (no env
    fallback): the card senders in grad_select.py / grad_raw.py /
    raw_tight.py, helius_alerts.py, launch_scanner/__init__.py and
    telegram/commands.py. For those, bridge_files_from_env() writes
    the env values into the expected runtime paths (0600, inside the
    deployment's private FOREX_AGENT_HOME) at start time. The files
    exist only on the phone, only while the deployment runs, and are
    never committed anywhere. The readers that already support env
    (reserves.py, smartmoney.py, funding_clusters.py, instant_safety.py,
    daemon/telegram/__init__.py, acquisition/providers/base.py) need
    no bridge and read env directly.
  * HARD GUARDS (2026-11-22, root cause of the live credential
    overwrite): (1) a value starting with "TEST-" is a test
    placeholder, never a real credential — check_credentials()
    refuses it and the bridge never writes it, so a test run can
    never plant a dead key in a live home; (2) the bridge is
    SEED-ONLY — it never overwrites an existing credential file
    (a deployment home is seeded once, fresh; rotation is a
    redeploy, never an overwrite); (3) preflight() requires
    FOREX_AGENT_HOME to be set explicitly and never defaults to
    the live ~/.forex-agent home — every legitimate caller
    (start.sh, restore_drill.py, the tests) sets it.
"""

from __future__ import annotations

import os
import stat
import sys

# ---------------------------------------------------------------------------
# Credential inventory (names only — never values).
# "required" means the paper bot cannot start without it on AGT.
# "required_when" documents the condition for optional credentials.
# ---------------------------------------------------------------------------
CREDENTIALS = (
    {
        "env": "HELIUS_API_KEY",
        "required": True,
        "purpose": "Helius main RPC key (live discovery + curve reads).",
        "file_fallback": "helius/api_key",
        "env_first_in_code": ("daemon/launch_scanner/reserves.py",
                              "daemon/launch_scanner/smartmoney.py"),
    },
    {
        "env": "HELIUS_API_KEY_DISCOVERY",
        "required": False,
        "purpose": "Second Helius key for research/discovery traffic, "
                   "kept off the live signals' quota.",
        "file_fallback": "helius/api_key_discovery",
        "env_first_in_code": ("daemon/launch_scanner/reserves.py",
                              "daemon/launch_scanner/smartmoney.py",
                              "daemon/launch_scanner/funding_clusters.py",
                              "daemon/launch_scanner/instant_safety.py"),
    },
    {
        "env": "ALCHEMY_API_KEY",
        "required": False,
        "required_when": "router enables the Alchemy provider "
                         "(Phases 5-12 cutover; not the live leg today)",
        "purpose": "Alchemy API key (key only, never the endpoint URL).",
        "file_fallback": "alchemy/api_key",
        "env_first_in_code": ("daemon/intelligence/acquisition/"
                              "providers/base.py",),
    },
    {
        "env": "CHAINSTACK_ENDPOINT",
        "required": False,
        "required_when": "router enables the Chainstack provider "
                         "(Phases 5-12 cutover; not the live leg today)",
        "purpose": "Full Chainstack Solana mainnet endpoint URL "
                   "(the key is its final path segment).",
        "file_fallback": ".forex-agent/chainstack/endpoint",
        "env_first_in_code": ("daemon/intelligence/acquisition/"
                              "providers/base.py",),
    },
    {
        "env": "TELEGRAM_BOT_TOKEN",
        "required": True,
        "purpose": "Telegram bot token (alerts + control bot).",
        "file_fallback": "telegram/bot_token",
        "env_first_in_code": ("daemon/telegram/__init__.py",),
    },
    {
        "env": "HELIUS_ALERT_CHAT_ID",
        "required": False,
        "purpose": "Telegram chat id for Helius 429-spike alerts.",
        "file_fallback": None,
        "env_first_in_code": ("daemon/launch_scanner/helius_alerts.py",),
    },
    {
        "env": "GITHUB_TOKEN",
        "required": False,
        "required_when": "github-repo-sync loop is enabled on AGT "
                         "(FINALIZE pass: the VM sync script uses the "
                         "authd vault credential, which does not exist "
                         "on AGT)",
        "purpose": "GitHub personal access token for the repo sync loop.",
        "file_fallback": None,
        "env_first_in_code": (),
    },
)

# Readers that take ONLY a file (no env fallback in the live code).
# The start script bridges these from env at deployment start.
# (env name, relative path under FOREX_AGENT_HOME)
FILE_ONLY_BRIDGE = (
    ("HELIUS_API_KEY", "helius/api_key"),
    ("HELIUS_API_KEY_DISCOVERY", "helius/api_key_discovery"),
    ("TELEGRAM_BOT_TOKEN", "telegram/bot_token"),
    # Chainstack file fallback is read from $HOME/.forex-agent/chainstack/
    # endpoint (providers/base.py); bridged under FOREX_AGENT_HOME, which
    # is $HOME/.forex-agent on AGT.
    ("CHAINSTACK_ENDPOINT", "chainstack/endpoint"),
    # Alchemy file fallback is $HOME/alchemy/api_key; bridged relative to
    # the deployment HOME (one level above FOREX_AGENT_HOME). Handled
    # specially in bridge_files_from_env().
    ("ALCHEMY_API_KEY", "alchemy/api_key"),
)

REQUIRED_ENVS = tuple(c["env"] for c in CREDENTIALS if c["required"])


class CredentialError(Exception):
    """A required credential is missing. Fail closed, name the variable."""


def present(env=None):
    """Names of configured credentials (never values)."""
    env = os.environ if env is None else env
    return [c["env"] for c in CREDENTIALS
            if (env.get(c["env"]) or "").strip()]


def missing_required(env=None):
    """Names of missing REQUIRED credentials (never values)."""
    env = os.environ if env is None else env
    return [name for name in REQUIRED_ENVS
            if not (env.get(name) or "").strip()]


def placeholder_values(env=None):
    """Names of credentials holding a test placeholder value.

    A value starting with "TEST-" is a test fixture, never a real
    credential (real Helius keys are UUIDs, real Telegram tokens
    are digits + 35 characters). Refusing them here is what stops
    a test run from ever planting a dead key in a live home
    (2026-11-22: a preflight run with the drill's TEST- env
    overwrote the live helius/api_key and telegram/bot_token).
    """
    env = os.environ if env is None else env
    return [c["env"] for c in CREDENTIALS
            if (env.get(c["env"]) or "").strip().upper().startswith("TEST-")]


def check_credentials(env=None):
    """Fail-closed startup check.

    Returns the list of configured credential names. Raises
    CredentialError naming every missing required variable when any
    is absent, and naming any placeholder ("TEST-") value when one
    is present. Never logs or returns a value.
    """
    missing = missing_required(env)
    if missing:
        raise CredentialError(
            "AGT startup refused: missing required credential(s): %s. "
            "Set them in the AGT vault (injected as environment "
            "variables) and restart the deployment. No default is "
            "used." % ", ".join(missing))
    placeholders = placeholder_values(env)
    if placeholders:
        raise CredentialError(
            "AGT startup refused: credential(s) %s hold a test "
            "placeholder value. Set the real credential "
            "in the AGT vault and restart the deployment. A "
            "placeholder is never bridged to a file."
            % ", ".join(placeholders))
    return present(env)


def vault_surrogate_available():
    """False on AGT by design: no authd socket, no egress proxy.

    The helius_vault.py surrogate bridge is VM-only. Its absence here
    is expected, not an error — env reads replace it.
    """
    return os.path.exists("/run/hatch/auth/authd.sock")


def _write_0600(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w") as fh:
        fh.write(value.strip() + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    # Verify the mode landed (umask could not weaken it: chmod above
    # sets it explicitly, but verify rather than assume).
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise CredentialError(
            "credential file %s has permissive mode %o" % (path, mode))


def bridge_files_from_env(home, env=None):
    """Write env credentials into the runtime file paths that file-only
    readers expect (0600, inside the deployment's private home).

    SEED-ONLY (2026-11-22): a file that already exists is NEVER
    overwritten — the bridge seeds a fresh deployment home once;
    rotation is a redeploy, never an overwrite. A "TEST-" placeholder
    value is never written either (check_credentials() refuses it
    at preflight; the bridge also skips it when called directly).
    Only writes a file when its env var is present, non-empty, and
    not a placeholder. Existing files are left untouched when the
    env var is absent, so a deployment that was seeded another way
    keeps working. Returns the list of (env name, relative path)
    bridged — names only, no values.
    """
    env = os.environ if env is None else env
    bridged = []
    for name, rel in FILE_ONLY_BRIDGE:
        value = (env.get(name) or "").strip()
        if not value:
            continue
        if value.upper().startswith("TEST-"):
            continue
        if rel == "alchemy/api_key":
            # File fallback is $HOME/alchemy/api_key; FOREX_AGENT_HOME
            # is $HOME/.forex-agent, so the bridge target is one level
            # above home.
            target = os.path.join(os.path.dirname(home.rstrip("/")),
                                  "alchemy", "api_key")
        elif rel == "chainstack/endpoint":
            target = os.path.join(home, "chainstack", "endpoint")
        else:
            target = os.path.join(home, rel)
        if os.path.exists(target):
            continue
        _write_0600(target, value)
        bridged.append((name, os.path.relpath(target, home)))
    return bridged


def preflight(home=None, env=None):
    """Full startup preflight: fail-closed check + file bridge.

    Returns {"configured": [...], "bridged": [...],
             "vault_surrogate": bool}. Raises CredentialError on any
    missing required credential, on any "TEST-" placeholder value,
    and when FOREX_AGENT_HOME is not set explicitly (the home is
    never defaulted to the live ~/.forex-agent — 2026-11-22: that
    default let a test preflight overwrite the live credential
    files). Logs names only.
    """
    env = os.environ if env is None else env
    configured = check_credentials(env)
    if home is None:
        home = env.get("FOREX_AGENT_HOME")
    if not home:
        raise CredentialError(
            "AGT startup refused: FOREX_AGENT_HOME is not set. "
            "Set it to the deployment's private home before "
            "running the preflight; it is never defaulted to "
            "~/.forex-agent.")
    bridged = bridge_files_from_env(home, env)
    return {
        "configured": configured,
        "bridged": [b[0] for b in bridged],
        "vault_surrogate": vault_surrogate_available(),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--preflight"]:
        try:
            result = preflight()
        except CredentialError as exc:
            print("AGT credential preflight FAILED: %s" % exc)
            return 1
        print("AGT credential preflight OK: configured=%s bridged=%s "
              "vault_surrogate=%s" % (
                  ",".join(result["configured"]) or "(none)",
                  ",".join(result["bridged"]) or "(none)",
                  result["vault_surrogate"]))
        return 0
    if argv == ["--list"]:
        for cred in CREDENTIALS:
            print("%s required=%s %s" % (
                cred["env"], cred["required"], cred["purpose"]))
        return 0
    print("usage: credentials.py [--preflight | --list]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
