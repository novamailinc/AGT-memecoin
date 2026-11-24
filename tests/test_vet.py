"""Tip-line /vet tests (2026-10-10).

The tip-line is READ-ONLY: it never enters, alerts, or tracks. These
tests pin the verdict logic and the fail-soft behavior — unknown is
reported as unknown, never guessed, and nothing ever raises.
"""

import unittest
import unittest.mock

from daemon.telegram import commands_vet as vet

_MINT = "ERkmk5rs8KKD9u2fxoDZUmwwuVc7rT3sSeowGTrQuBit"


def _patch(**kw):
    from unittest.mock import Mock
    return unittest.mock.patch.multiple(
        vet,
        _safety=Mock(return_value=kw.get("safety", {})),
        _smart_buyers=Mock(
            return_value=kw.get("smart", ([], 0.0, {}))),
        _deployer=Mock(
            return_value=kw.get("deployer", (None, "unknown"))),
        _market=Mock(return_value=kw.get("market", {})))


class VetInputTest(unittest.TestCase):
    def test_empty_mint_shows_usage(self):
        self.assertIn("Usage: /vet", vet.vet_mint(""))

    def test_short_mint_shows_usage(self):
        self.assertIn("Usage: /vet", vet.vet_mint("abc"))

    def test_evm_mint_declined_plainly(self):
        out = vet.vet_mint("0x735c1234abcd")
        self.assertIn("Solana", out)
        self.assertNotIn("CHAIN AGREES", out)

    def test_never_raises_on_garbage(self):
        self.assertIsInstance(vet.vet_mint(None), str)


class VetVerdictTest(unittest.TestCase):
    def test_hard_fail_says_no(self):
        with _patch(safety={"score": 12, "honeypot": True}):
            out = vet.vet_mint(_MINT)
        self.assertIn("CHAIN SAYS NO", out)
        self.assertIn("HARD FAIL", out)

    def test_blacklisted_deployer_says_no(self):
        with _patch(safety={"score": 80},
                    deployer=("DEP123", "blacklisted")):
            out = vet.vet_mint(_MINT)
        self.assertIn("CHAIN SAYS NO", out)
        self.assertIn("BLACKLISTED", out)

    def test_safety_unknown_is_unvetted_never_green(self):
        with _patch(safety={"_error": "timeout"},
                    smart=(["sniper-1"], 1.4, {})):
            out = vet.vet_mint(_MINT)
        # Even with smart money in, unknown safety can't be green.
        self.assertIn("UNVETTED", out)
        self.assertNotIn("CHAIN AGREES", out)

    def test_agree_needs_safety_pass_plus_smart_money(self):
        with _patch(safety={"score": 85},
                    smart=(["sniper-1"], 1.4, {}),
                    deployer=("DEP1", "clean"),
                    market={"mcap": 500000, "liquidity": 60000,
                            "priceChange24h": 120}):
            out = vet.vet_mint(_MINT)
        self.assertIn("CHAIN AGREES", out)
        self.assertIn("grade-weighted", out)

    def test_neutral_without_smart_money(self):
        with _patch(safety={"score": 70},
                    deployer=("DEP1", "clean")):
            out = vet.vet_mint(_MINT)
        self.assertIn("CHAIN NEUTRAL", out)

    def test_hot_deployer_flagged(self):
        with _patch(safety={"score": 75},
                    deployer=("DEP9", "hot")):
            out = vet.vet_mint(_MINT)
        self.assertIn("runner-maker", out)

    def test_all_checks_failing_still_answers(self):
        with unittest.mock.patch.multiple(
                vet,
                _safety=unittest.mock.Mock(side_effect=Exception("x")),
                _smart_buyers=unittest.mock.Mock(
                    side_effect=Exception("x")),
                _deployer=unittest.mock.Mock(side_effect=Exception("x")),
                _market=unittest.mock.Mock(side_effect=Exception("x"))):
            out = vet.vet_mint(_MINT)
        # Fail-soft: still returns a report, never raises.
        self.assertIsInstance(out, str)
        self.assertIn("UNVETTED", out)

    def test_readonly_disclaimer_present(self):
        with _patch(safety={"score": 80}):
            out = vet.vet_mint(_MINT)
        self.assertIn("Read-only", out)


class VetHandlerTest(unittest.TestCase):
    def test_handler_sends_report(self):
        sent = []

        class Ctx:
            def send(self, chat_id, text, _):
                sent.append((chat_id, text))

        with _patch(safety={"score": 80}):
            vet.cmd_vet_handler(Ctx(), 123, 456, 789, [_MINT])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], 123)
        self.assertIn("Vetting", sent[0][1])

    def test_handler_never_raises(self):
        class BadCtx:
            def send(self, *a):
                raise RuntimeError("send broken")

        # Must not propagate.
        vet.cmd_vet_handler(BadCtx(), 1, 2, 3, [_MINT])


if __name__ == "__main__":
    unittest.main()
