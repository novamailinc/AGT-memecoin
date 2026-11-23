import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from daemon.telegram import TelegramDaemon, RealTools
from daemon.telegram.commands import HandlerContext, handle_message
from daemon.telegram.security import SecurityContext


class TestControlOnly(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'TELEGRAM_CONTROL_ONLY':'1',
                   'TELEGRAM_ALLOWED_CHAT_ID':'111','TELEGRAM_ALLOWED_USER_ID':'222'})
        self.env.start();self.addCleanup(self.env.stop)
        self.state={};self.sec=SecurityContext(self.state)
        self.sec.try_pair(111,222)
        self.send=Mock();self.tools=Mock()
        self.ctx=HandlerContext(send=self.send,security=self.sec,tools=self.tools,publish=Mock())

    def test_disabled_commands_cannot_execute_tools(self):
        for i,cmd in enumerate(('/trade BUY SOL 1 1 1','/resume','/confirm abc',
                                '/check mint','/sethelius secret','/config_set a b','/lane x on')):
            handle_message(self.ctx,111,222,i,cmd)
        self.assertEqual(self.tools.mock_calls,[])
        self.assertIn('disabled',self.send.call_args.args[1])

    def test_help_names_limits_not_normal_trading_help(self):
        handle_message(self.ctx,111,222,1,'/help')
        text=self.send.call_args.args[1]
        self.assertIn('Stage A is unpassed',text)
        self.assertNotIn('/resume',text)

    def test_account_uses_local_demo_only(self):
        with patch('daemon.telegram.commands._demo_snapshot',return_value=None):
            handle_message(self.ctx,111,222,1,'/account forex')
        self.tools.forex_get_account.assert_not_called()
        self.assertIn('no demo account ledger',self.send.call_args.args[1])

    def test_unauthorized_owner_id_mismatch_stays_ignored(self):
        handle_message(self.ctx,111,999,1,'/help')
        self.send.assert_not_called()

    def test_status_does_not_call_broker_health(self):
        tools=RealTools()
        with patch.object(tools._misc,'forex_get_health',side_effect=AssertionError('network health')):
            self.assertEqual(tools.forex_get_health()['mode'],'development_control_only')

    def test_callback_never_confirms_in_control_mode(self):
        d=TelegramDaemon();client=Mock()
        with patch('daemon.telegram.handle_callback') as cb:
            d._route_update(self.ctx,client,{'callback_query':{'id':'cb','data':'confirm:abc'}},1)
        cb.assert_not_called()
        client.answer_callback_query.assert_called_once()

    def test_price_side_loops_not_run(self):
        client=Mock();client.get_updates.return_value=[]
        d=TelegramDaemon(client_factory=lambda token:client,tools=self.tools)
        with patch.object(d,'_ensure_client',return_value=client), \
             patch('daemon.telegram.get_store',return_value=Mock()), \
             patch('daemon.telegram.get_daemon_state',return_value={}), \
             patch('daemon.telegram.save_daemon_state'), \
             patch('daemon.telegram.event_bus.configure'), \
             patch.object(d,'_fan_out') as fanout, \
             patch('daemon.telegram._blink.maybe_poll') as blink, \
             patch('daemon.telegram._thesis_watch.maybe_check') as thesis:
            d.run_once()
        blink.assert_not_called();thesis.assert_not_called();fanout.assert_not_called()

    def test_successful_delivery_records_api_acceptance_not_read_receipt(self):
        client=Mock();client.send_message.return_value={'message_id':42}
        state={}
        TelegramDaemon()._make_sender(client,state)(111,'fixture response',None)
        self.assertEqual(state['last_command_delivery']['message_id'],42)
        self.assertEqual(state['last_command_delivery']['status'],
                         'telegram_api_accepted_not_read_receipt')

    def test_send_failure_never_records_acceptance(self):
        from daemon.telegram.client import TelegramError
        client=Mock();client.send_message.side_effect=TelegramError('fixture')
        state={}
        TelegramDaemon()._make_sender(client,state)(111,'fixture response',None)
        self.assertNotIn('last_command_delivery',state)
        self.assertEqual(state['command_send_failures'],1)

    def test_supervisor_control_roster_cannot_start_trading_stack(self):
        import importlib.machinery
        from pathlib import Path
        import sys
        path=Path(__file__).resolve().parents[1]/'scripts/supervisor'
        module=importlib.machinery.SourceFileLoader('control_supervisor_test',str(path)).load_module()
        with patch.object(sys,'argv',['supervisor','--telegram-control-only']):
            roster=module._active_roster()
        self.assertEqual([s.name for s in roster],['telegram'])
        self.assertIn('--control-only',roster[0].argv)

    def test_malformed_send_results_never_record_acceptance(self):
        for result in (None,{}, {'message_id':None},{'message_id':False},
                       {'message_id':0},{'message_id':'123'}):
            client=Mock();client.send_message.return_value=result
            state={}
            TelegramDaemon()._make_sender(client,state)(111,'fixture response',None)
            self.assertNotIn('last_command_delivery',state)
            self.assertEqual(state['command_send_failures'],1)
