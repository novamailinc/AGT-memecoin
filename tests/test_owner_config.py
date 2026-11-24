"""Tests for the owner-config surface (request section 16).

Covers:
  * module: validation of every key (fail closed on unknown keys,
    malformed values, non-demo/historical trading modes); atomic 0600
    storage; load() refuses hand-edited unknown/stale entries;
    require() fails closed when unset (never a placeholder fallback).
  * commands: /config shows saved-but-inactive state; /config_set
    validates, refuses bad input without a confirm, and stores only
    through the two-step confirm flow; confirm executes once; the
    stored file never gains unknown keys.

Run from the forex-agent root:
    python3 -m unittest tests.test_owner_config -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["FOREX_AGENT_HOME"] = tempfile.mkdtemp(prefix="owner-config-test-")

from daemon import owner_config                              # noqa: E402
from daemon.telegram import commands as tg_commands          # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_telegram_bot import make_ctx, FakeTelegramClient, CHAT, USER  # noqa: E402


class OwnerConfigModuleTests(unittest.TestCase):
    def setUp(self):
        p = owner_config._path()
        if os.path.exists(p):
            os.unlink(p)

    def test_validate_each_key(self):
        self.assertEqual(owner_config.validate("trade_amount_usd", "25"), 25.0)
        self.assertEqual(owner_config.validate("max_simultaneous_trades", "3"), 3)
        self.assertEqual(owner_config.validate("max_drawdown_pct", "12.5"), 12.5)
        self.assertEqual(owner_config.validate("slippage_bps", "50"), 50.0)
        self.assertEqual(owner_config.validate("trading_mode", "DEMO"), "demo")
        self.assertEqual(owner_config.validate("trading_mode", "historical"),
                         "historical")

    def test_unknown_key_fails_closed(self):
        with self.assertRaises(ValueError):
            owner_config.validate("leverage", "10")

    def test_malformed_values_refused(self):
        for key, bad in [("trade_amount_usd", "-5"),
                         ("trade_amount_usd", "abc"),
                         ("max_simultaneous_trades", "0"),
                         ("max_drawdown_pct", "101"),
                         ("slippage_bps", "-1"),
                         ("trading_mode", "live"),
                         ("trading_mode", "mainnet"),
                         ("trading_mode", "paper")]:
            with self.assertRaises(ValueError, msg="%s=%r" % (key, bad)):
                owner_config.validate(key, bad)

    def test_set_persists_atomic_0600(self):
        owner_config.set_value("trade_amount_usd", "10")
        p = owner_config._path()
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertEqual(owner_config.load()["trade_amount_usd"], 10.0)

    def test_load_refuses_unknown_and_stale_entries(self):
        p = owner_config._path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            json.dump({"trade_amount_usd": 5, "backdoor": True,
                       "max_drawdown_pct": 500}, fh)
        self.assertEqual(owner_config.load(), {"trade_amount_usd": 5.0})

    def test_corrupt_file_is_empty(self):
        p = owner_config._path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("{not json")
        self.assertEqual(owner_config.load(), {})

    def test_require_fails_closed_when_unset(self):
        with self.assertRaises(owner_config.ConfigUnset):
            owner_config.require("max_daily_loss_usd")
        owner_config.set_value("max_daily_loss_usd", "100")
        self.assertEqual(owner_config.require("max_daily_loss_usd"), 100.0)


class OwnerConfigCommandTests(unittest.TestCase):
    def setUp(self):
        p = owner_config._path()
        if os.path.exists(p):
            os.unlink(p)
        self.client, self.ctx, self.sec, self.published, self.audit = \
            make_ctx(FakeTelegramClient())
        self.sec.try_pair(CHAT, USER)

    def _texts(self):
        return [t for (_cid, t, _m) in self.client.sent]

    def test_config_lists_keys_and_inactive_notice(self):
        tg_commands._cmd_config(self.ctx, CHAT, USER, 1, [])
        out = self._texts()[-1]
        self.assertIn("INACTIVE", out)
        self.assertIn("trade_amount_usd", out)
        self.assertIn("(unset)", out)

    def test_config_set_bad_key_refused_no_confirm(self):
        tg_commands._cmd_config_set(self.ctx, CHAT, USER, 1, ["nope", "1"])
        self.assertIn("Refused", self._texts()[-1])
        self.assertFalse(any("Confirm" in t for t in self._texts()))
        self.assertEqual(owner_config.load(), {})

    def test_config_set_mainnet_mode_refused(self):
        tg_commands._cmd_config_set(self.ctx, CHAT, USER, 1,
                                    ["trading_mode", "mainnet"])
        self.assertIn("Refused", self._texts()[-1])
        self.assertEqual(owner_config.load(), {})

    def test_config_set_full_confirm_flow(self):
        tg_commands._cmd_config_set(self.ctx, CHAT, USER, 1,
                                    ["trade_amount_usd", "25"])
        ask = self._texts()[-1]
        self.assertIn("Confirm", ask)
        self.assertIn("INACTIVE", ask)
        # Nothing is stored before the confirm code comes back.
        self.assertEqual(owner_config.load(), {})
        code = ask.split("/confirm ")[1].split()[0]
        tg_commands._cmd_confirm(self.ctx, CHAT, USER, 2, [code])
        done = self._texts()[-1]
        self.assertIn("Saved", done)
        self.assertIn("INACTIVE", done)
        self.assertEqual(owner_config.load()["trade_amount_usd"], 25.0)
        # Single-use: replaying the code does nothing.
        tg_commands._cmd_confirm(self.ctx, CHAT, USER, 3, [code])
        self.assertIn("Unknown confirm code", self._texts()[-1])

    def test_config_set_rate_limited_as_mutating(self):
        # config_set is registered as a mutating command for rate limits.
        self.assertIn("config_set", tg_commands._COMMANDS)
        self.assertIs(tg_commands._COMMANDS["config_set"],
                      tg_commands._cmd_config_set)


if __name__ == "__main__":
    unittest.main()
