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

def test_ordinary_helius_writer_cannot_reseed_corruption(tmp_path):
    s=UsageStore(home=str(tmp_path));record(s)
    path=tmp_path/'acquisition_provider_usage.json';path.write_text('corrupt999credits')
    with pytest.raises(ValueError):s.record('helius_main','helius','getSlot','routine_account','card',cost=1)
    assert path.read_text()=='corrupt999credits'

def test_other_account_writer_cannot_reseed_shared_file(tmp_path):
    s=UsageStore(home=str(tmp_path));record(s)
    path=tmp_path/'acquisition_provider_usage.json';path.write_text('corrupt999credits')
    with pytest.raises(ValueError):s.record('alchemy','alchemy','getSlot','routine_account','card',cost=1)
    assert path.read_text()=='corrupt999credits'

def _lost_counters_with_retained_journal(s,tmp_path,account='alchemy'):
    s.record(account,'alchemy','getSlot','routine_account','card',cost=1,strict=True)
    (tmp_path/'acquisition_provider_usage.json').write_text('{}')

def test_mark_warned_cannot_recreate_lost_account(tmp_path):
    s=UsageStore(home=str(tmp_path));_lost_counters_with_retained_journal(s,tmp_path)
    with pytest.raises(ValueError):s.mark_warned('alchemy')
    assert json.loads((tmp_path/'acquisition_provider_usage.json').read_text())=={}

def test_set_disabled_cannot_recreate_lost_account(tmp_path):
    s=UsageStore(home=str(tmp_path));_lost_counters_with_retained_journal(s,tmp_path)
    with pytest.raises(ValueError):s.set_disabled('alchemy',True,'test')
    assert json.loads((tmp_path/'acquisition_provider_usage.json').read_text())=={}

def test_fresh_account_warn_and_disable_still_allowed(tmp_path):
    s=UsageStore(home=str(tmp_path))
    s.mark_warned('alchemy')
    rec=s.set_disabled('alchemy',True,'test')
    assert rec['disabled'] is True and rec['disabled_reason']=='test'
    s.validate_admission_state('alchemy')

def test_disable_after_legit_record_and_no_reseed(tmp_path):
    s=UsageStore(home=str(tmp_path))
    s.record('alchemy','alchemy','getSlot','routine_account','card',cost=3,strict=True)
    rec=s.set_disabled('alchemy',True,'quota')
    assert rec['disabled'] is True and rec['cost_spent']==3
    s.mark_warned('alchemy')
    assert s.get('alchemy')['cost_spent']==3
