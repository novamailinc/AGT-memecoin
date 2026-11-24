import io
import urllib.error
from unittest.mock import patch
from daemon.intelligence.acquisition.helius_parsed_events import HeliusParsedEventsClient
from daemon.intelligence.acquisition import helius_account
from daemon.launch_scanner import helius_ratelimit


def test_routine_hold_refuses_selective_transport(tmp_path):
    c=HeliusParsedEventsClient(api_key='fixture',_opener=lambda *a,**k:(_ for _ in ()).throw(AssertionError('network during hold')))
    c.account_home=str(tmp_path)
    helius_account.throttled(str(tmp_path),900)
    assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='rate_limited'


def test_selective_retry_after_holds_routine_account(tmp_path):
    def denied(req,timeout=None):raise urllib.error.HTTPError(req.full_url,429,'fixture',{'Retry-After':'900'},io.BytesIO(b'{}'))
    c=HeliusParsedEventsClient(api_key='fixture',_opener=denied);c.account_home=str(tmp_path)
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='rate_limited'
        p.return_value.report.assert_called_once()
    assert not helius_account.allowed(str(tmp_path))


def test_selective_and_routine_use_identical_bucket(tmp_path):
    from daemon.intelligence.acquisition.providers.helius import HeliusAdapter
    class Response:
        status=200
        def read(self):return b'[]'
        def __enter__(self):return self
        def __exit__(self,*a):pass
    c=HeliusParsedEventsClient(api_key='fixture',_opener=lambda *a,**k:Response());c.account_home=str(tmp_path)
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=False
        assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='rate_limited'
        assert p.return_value.acquire.call_args.args[0]==helius_ratelimit.bucket_id_for_key('account:helius_main')


def test_actual_production_client_string_monthly_429(tmp_path,monkeypatch):
    from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
    monkeypatch.setenv('HELIUS_API_KEY_1','fixture')
    s=SelectiveEnrichment(tmp_path)
    c=s.clients[0][1]
    def denied(req,timeout=None):raise urllib.error.HTTPError(req.full_url,429,'fixture',{'Retry-After':'900'},io.BytesIO(b'{"error":"monthly quota exceeded"}'))
    c._opener=denied
    with patch.object(helius_ratelimit,'pacer') as p:
        p.return_value.acquire.return_value=True
        assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='rate_limited'
    from daemon.intelligence.acquisition.usage_tracker import UsageTracker
    assert UsageTracker(home=str(tmp_path)).account_disabled('helius_main')
    assert not helius_account.allowed(str(tmp_path))


def test_admission_storage_failure_typed_no_http(tmp_path):
    c=HeliusParsedEventsClient(api_key='fixture',_opener=lambda *a,**k:(_ for _ in ()).throw(AssertionError('must not call')));c.account_home=str(tmp_path)
    with patch.object(helius_account,'allowed',side_effect=OSError('storage unavailable')):
        assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='error'


def test_rest_error_body_shapes_preserve_minimum(tmp_path):
    import json
    for i,body in enumerate(({'error':'slow down'},['slow down'],'slow down',{'error':{'message':'slow down'}})):
        home=tmp_path/str(i)
        def denied(req,timeout=None):raise urllib.error.HTTPError(req.full_url,429,'fixture',{'Retry-After':'900'},io.BytesIO(json.dumps(body).encode()))
        c=HeliusParsedEventsClient(api_key='fixture',_opener=denied);c.account_home=str(home)
        with patch.object(helius_ratelimit,'pacer') as p,patch('time.time',return_value=1000):
            p.return_value.acquire.return_value=True
            assert c.parse_transactions(['s'],'decoder_unconfident')['kind']=='rate_limited'
        assert not helius_account.allowed(str(home),now=1899)
        assert helius_account.allowed(str(home),now=1900)
