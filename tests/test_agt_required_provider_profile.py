"""Declared deployment provider inventory fails closed before file bridging."""
import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).parents[1]
REQUIRED=('ALCHEMY_API_KEY','CHAINSTACK_ENDPOINT','HELIUS_API_KEY_1','HELIUS_API_KEY_2','HELIUS_API_KEY_3','TELEGRAM_BOT_TOKEN')

def credentials():
    spec=importlib.util.spec_from_file_location('agt_profile_credentials',ROOT/'deploy/agt/credentials.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize('missing',REQUIRED)
@pytest.mark.parametrize('value',[None,'','   '])
def test_missing_profile_member_refuses_before_bridge(tmp_path,monkeypatch,missing,value):
    c=credentials();env={key:'fixture-'+key.lower() for key in REQUIRED}
    if value is None:env.pop(missing)
    else:env[missing]=value
    env['HELIUS_API_KEY']='fixture-legacy-not-a-replacement'
    calls=[];monkeypatch.setattr(c,'bridge_files_from_env',lambda *a,**kw:calls.append(a))
    home=tmp_path/'not-created'
    with pytest.raises(c.CredentialError) as exc:c.preflight(str(home),env)
    assert missing in str(exc.value)
    assert all(v not in str(exc.value) for v in env.values() if v.strip())
    assert calls==[] and not home.exists()

@pytest.mark.parametrize('name',REQUIRED)
def test_profile_placeholder_refused_before_bridge(tmp_path,monkeypatch,name):
    c=credentials();env={key:'fixture-'+key.lower() for key in REQUIRED};env[name]='TEST-never-bridge'
    calls=[];monkeypatch.setattr(c,'bridge_files_from_env',lambda *a,**kw:calls.append(a))
    with pytest.raises(c.CredentialError) as exc:c.preflight(str(tmp_path),env)
    assert name in str(exc.value) and env[name] not in str(exc.value)
    assert calls==[]

def test_profile_inventory_is_not_network_or_activation_proof(monkeypatch):
    c=credentials();env={key:'fixture-'+key.lower() for key in REQUIRED}
    assert set(c.check_credentials(env))==set(REQUIRED)
    assert set(c.REQUIRED_ENVS)==set(REQUIRED)
    import json
    manifest=json.loads((ROOT/'deploy/agt/manifest.json').read_text())
    assert {r['name'] for r in manifest['required_env']}==set(REQUIRED)
    assert not manifest['mainnet_enabled'] and manifest['mode']=='paper'
