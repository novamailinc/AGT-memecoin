import json
import os
from unittest.mock import patch
import pytest
from daemon.intelligence.acquisition.provider_usage import UsageStore
from daemon.intelligence.acquisition import stream_accounting_guard as guard,helius_streams as hs


def record(s):
    return s.record('helius_main','helius','parsed_streams/event','parsed_tx_stream','card',cost=1,strict=True)

def test_strict_store_real_save_failure_keeps_marker(tmp_path):
    s=UsageStore(home=str(tmp_path))
    c=hs.HeliusParsedStreamClient(usage_store=s,account_home=str(tmp_path))
    with patch.object(s,'_save',side_effect=OSError('disk fixture')):
        with pytest.raises(OSError):c._record_usage('parsed_streams/event','ok',cost=1)
    assert not guard.allowed(str(tmp_path))
    assert c._stop.is_set()

def test_strict_corrupt_counters_not_reseeded(tmp_path):
    s=UsageStore(home=str(tmp_path));record(s)
    path=tmp_path/'acquisition_provider_usage.json';path.write_text('corrupt999credits')
    with pytest.raises(ValueError):record(s)
    assert path.read_text()=='corrupt999credits'

def test_strict_success_durable_readback_and_concurrent_count(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    s=UsageStore(home=str(tmp_path))
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:record(s),range(12)))
    s.validate_admission_state('helius_main')
    assert s.get('helius_main')['cost_spent']==12
    assert len((tmp_path/'acquisition_provider_usage.jsonl').read_text().splitlines())==12

def test_none_result_keeps_pending(tmp_path):
    from unittest.mock import MagicMock
    s=MagicMock();s.record.return_value=None
    c=hs.HeliusParsedStreamClient(usage_store=s,account_home=str(tmp_path))
    with pytest.raises(OSError):c._record_usage('parsed_streams/event','ok',cost=1)
    assert not guard.allowed(str(tmp_path))
