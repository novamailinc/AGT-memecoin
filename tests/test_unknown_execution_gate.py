"""Shared unknown-execution gate at production readers, all offline."""
import importlib
import unittest
from unittest.mock import patch
from daemon.launch_scanner import tx_guards,deployers,exit_copy

class TestUnknownExecutionGate(unittest.TestCase):
    def test_missing_status_is_unknown_across_shapes(self):
        for tx in (None,{}, {'meta':None},{'meta':{}},{'meta':{'logMessages':[]}}):
            self.assertFalse(tx_guards.tx_succeeded(tx))
            self.assertEqual(tx_guards.tx_reverted_reason(tx),'tx_unknown')
        self.assertTrue(tx_guards.tx_succeeded({'meta':{'err':None}}))
        self.assertTrue(tx_guards.tx_succeeded({'err':None}))
        for err in ({},False,0,'failed',[]):
            self.assertFalse(tx_guards.tx_succeeded({'meta':{'err':err}}))
            self.assertEqual(tx_guards.tx_reverted_reason({'meta':{'err':err}}),'tx_reverted')

    def test_migration_price_readers_refuse_unknown_before_balance_parse(self):
        for name in ('grad_raw','raw_tight','moon_lane'):
            module=importlib.import_module('daemon.launch_scanner.'+name)
            info,reason=module._ff_parse_tx_price({'meta':{'postTokenBalances':[]}},'M',100)
            self.assertIsNone(info,name)
            self.assertIn('tx_unknown',reason,name)

    def test_deployer_resolution_refuses_unknown_before_naming_signer(self):
        tx={'transaction':{'message':{'accountKeys':[{'pubkey':'invented-deployer','signer':True}]}},'meta':{}}
        with patch.object(deployers,'_rpc',side_effect=[[{'signature':'sig'}],tx]):
            self.assertIsNone(deployers.resolve_deployer('M','solana'))

    def test_wallet_exit_reader_does_not_parse_unknown_balance_as_sell(self):
        with patch.object(exit_copy,'_rpc',side_effect=[[{'signature':'sig','blockTime':123}],{'meta':{}}]),patch.object(exit_copy,'_token_delta',return_value=-10) as delta:
            self.assertIsNone(exit_copy._wallet_sold_after('W','M',100,[1]))
            delta.assert_not_called()
