"""Acquisition evidence failures must not produce wallet buy sightings."""
import copy
import importlib.machinery
from pathlib import Path
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sw = importlib.machinery.SourceFileLoader('smart_tx_validation', str(ROOT/'scripts/smart-watcher')).load_module()


class TransactionEvidence(unittest.TestCase):
    def setUp(self):
        self.tx = {'blockTime': int(time.time()),
                   'transaction': {'message': {'accountKeys': ['W']}},
                   'meta': {'err': None, 'preBalances': [2000000000],
                            'postBalances': [1000000000], 'preTokenBalances': [],
                            'postTokenBalances': [{'owner': 'W', 'mint': 'Mpump',
                                                  'uiTokenAmount': {'uiAmount': 10}}]}}

    def parse(self, tx):
        with patch.object(sw, 'rpc', return_value=tx):
            return sw.get_buy_details('W', 'fixture-signature', None)

    def test_valid_success_preserves_provenance(self):
        buy = self.parse(self.tx)[0]
        self.assertEqual(buy['tokens_bought'], 10)
        self.assertEqual(buy['sol_spent'], 1)
        self.assertEqual(buy['signature'], 'fixture-signature')

    def test_failed_and_unknown_execution_never_buy(self):
        self.tx['meta']['err'] = {'InstructionError': [0, 'fixture']}
        self.assertEqual(self.parse(self.tx), [])
        del self.tx['meta']['err']
        self.assertEqual(self.parse(self.tx), [])

    def test_invalid_amounts_cannot_leak_partial_buys(self):
        for amount in (True, float('nan'), float('inf'), -1, None, '10'):
            tx = copy.deepcopy(self.tx)
            tx['meta']['postTokenBalances'].append(
                {'owner': 'W', 'mint': 'BADpump', 'uiTokenAmount': {'uiAmount': amount}})
            self.assertEqual(self.parse(tx), [], repr(amount))

    def test_invalid_native_balance_and_time(self):
        for field, value in (('blockTime', None), ('blockTime', True), ('blockTime', -1)):
            tx=copy.deepcopy(self.tx);tx[field]=value
            self.assertEqual(self.parse(tx), [])
        self.tx['meta']['preBalances']=[float('inf')]
        self.assertEqual(self.parse(self.tx), [])

    def test_same_mint_account_transfer_not_false_buy(self):
        self.tx['meta']['preTokenBalances'] = [
            {'owner': 'W', 'mint': 'Mpump', 'uiTokenAmount': {'uiAmount': 10}}]
        self.tx['meta']['postTokenBalances'] = [
            {'owner': 'W', 'mint': 'Mpump', 'uiTokenAmount': {'uiAmount': 4}},
            {'owner': 'W', 'mint': 'Mpump', 'uiTokenAmount': {'uiAmount': 6}}]
        self.assertEqual(self.parse(self.tx), [])

    def test_missing_wallet_or_malformed_status(self):
        self.tx['transaction']['message']['accountKeys'] = ['OTHER']
        self.assertEqual(self.parse(self.tx), [])
        self.assertEqual(self.parse({'meta': []}), [])


if __name__ == '__main__': unittest.main()
