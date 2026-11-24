"""Packaging inventory records every fixed Helius source without billing split."""
import importlib.util
from pathlib import Path
import json

def test_numbered_source_slots_inventory_and_placeholder_guard():
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('agt_credentials_slots',root/'deploy/agt/credentials.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    manifest=json.loads((root/'deploy/agt/manifest.json').read_text())
    names={c['env'] for c in module.CREDENTIALS}
    for i in (1,2,3):
        name='HELIUS_API_KEY_%d'%i
        assert name in names
        assert name in {r['name'] for r in manifest['required_env']}
        assert module.placeholder_values({name:'TEST-fixture'})==[name]
    assert set(module.REQUIRED_ENVS)=={'ALCHEMY_API_KEY','TELEGRAM_BOT_TOKEN','CHAINSTACK_ENDPOINT',*(f'HELIUS_API_KEY_{i}' for i in (1,2,3))}
    assert manifest['repo_url']=='https://github.com/novamailinc/AGT-memecoin'

def test_chainstack_bridge_runtime_and_user_home_resolution(tmp_path,monkeypatch):
    from daemon.intelligence.acquisition.providers.base import chainstack_endpoint
    root=Path(__file__).parents[1]
    spec=importlib.util.spec_from_file_location('agt_credentials_paths',root/'deploy/agt/credentials.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    user_home=tmp_path/'user';runtime=user_home/'.forex-agent'
    endpoint='https://chainstack.example.invalid/fixture-node'
    module.bridge_files_from_env(str(runtime),{'CHAINSTACK_ENDPOINT':endpoint})
    monkeypatch.delenv('CHAINSTACK_ENDPOINT',raising=False)
    assert chainstack_endpoint(home=str(runtime))==endpoint
    assert chainstack_endpoint(home=str(user_home))==endpoint
    monkeypatch.setenv('CHAINSTACK_ENDPOINT',endpoint+'/env')
    assert chainstack_endpoint(home=str(runtime))==endpoint+'/env'
