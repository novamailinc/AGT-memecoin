import ast
from pathlib import Path
from scripts.test_discovery import is_script_test,required_arguments


def test_fixture_only_modules_are_collected():
    root=Path(__file__).parent
    for name in ('gateway_idempotency','gateway_lifecycle','lifecycle','memecoin_transport','alchemy_endpoint_normalization','logstream_provenance','learning_effects','scanner_wallet_learning_recovery','revised_provider_allocation'):
        assert not is_script_test(root/('test_'+name+'.py')),name
    assert is_script_test(root/'test_armed_watch_guard.py')


def test_imported_account_fixture_and_keyword_defaults():
    from scripts.test_discovery import fixture_names
    assert 'account' in fixture_names(Path(__file__).parent/'test_scanner_wallet_learning_recovery.py')
    node=ast.parse('def test_x(a=1,*,required,optional=None): pass').body[0]
    assert required_arguments(node)==['required']


def test_parametrized_arguments_are_pytest_inputs(tmp_path):
    from scripts.test_discovery import is_script_test
    path=tmp_path/'test_param.py'
    for spec in ("'mode'", "['mode']", "('mode',)"):
        path.write_text('import pytest\n@pytest.mark.parametrize('+spec+", ['a'])\ndef test_case(mode):pass\n")
        assert not is_script_test(path)
