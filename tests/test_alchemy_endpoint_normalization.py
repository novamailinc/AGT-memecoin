import pytest
from daemon.intelligence.acquisition.providers.base import alchemy_url, alchemy_wss_url

@pytest.mark.parametrize('value', ['test-key','https://solana-mainnet.g.alchemy.com/v2/test-key','wss://solana-mainnet.streaming.alchemy.com/v2/test-key'])
def test_normalize_key_and_endpoints(monkeypatch,value):
    monkeypatch.setenv('ALCHEMY_API_KEY',value)
    assert alchemy_url()=='https://solana-mainnet.g.alchemy.com/v2/test-key'
    assert alchemy_wss_url()=='wss://solana-mainnet.streaming.alchemy.com/v2/test-key'

@pytest.mark.parametrize('value', ['https://evil.example/v2/key','https://solana-mainnet.g.alchemy.com.evil.example/v2/key','https://user@solana-mainnet.g.alchemy.com/v2/key','http://solana-mainnet.g.alchemy.com/v2/key','https://solana-mainnet.g.alchemy.com/v2/','https://solana-mainnet.g.alchemy.com/v2/key?x=1','https://solana-mainnet.g.alchemy.com/v2/key#x','https://solana-mainnet.g.alchemy.com/v2/key/extra','https://solana-mainnet.g.alchemy.com/v2/a%2Fb','https://solana-mainnet.g.alchemy.com:443/v2/key','bare/key'])
def test_refuse_unrecognized_url(monkeypatch,value):
    monkeypatch.setenv('ALCHEMY_API_KEY',value)
    assert alchemy_url() is None
    assert alchemy_wss_url() is None

def test_disk_lookup_default_home(monkeypatch,tmp_path):
    monkeypatch.delenv('ALCHEMY_API_KEY',raising=False)
    monkeypatch.setenv('HOME',str(tmp_path))
    d=tmp_path/'.forex-agent'/'alchemy';d.mkdir(parents=True)
    (d/'api_key').write_text('https://solana-mainnet.g.alchemy.com/v2/disk-key\n')
    assert alchemy_url()=='https://solana-mainnet.g.alchemy.com/v2/disk-key'
    assert alchemy_wss_url()=='wss://solana-mainnet.streaming.alchemy.com/v2/disk-key'
    assert alchemy_url(home=str(tmp_path/'.forex-agent'))==alchemy_url()
