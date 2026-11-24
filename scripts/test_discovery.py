"""Shared static pytest/script classifier. Never import test modules to classify."""
import ast
from pathlib import Path
BUILTINS={'monkeypatch','tmp_path','tmp_path_factory','capsys','capfd','caplog','request','pytestconfig','record_property','recwarn','cache','doctest_namespace'}


def fixture_names(path,seen=None):
    path=Path(path).resolve();seen=set() if seen is None else seen
    if path in seen:return set()
    seen.add(path)
    try:tree=ast.parse(path.read_text())
    except (OSError,SyntaxError):return set()
    found=set()
    for n in tree.body:
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                d=d.func if isinstance(d,ast.Call) else d
                if (isinstance(d,ast.Attribute) and d.attr=='fixture') or (isinstance(d,ast.Name) and d.id=='fixture'):found.add(n.name)
        elif isinstance(n,ast.ImportFrom) and n.module:
            root=next((p for p in path.parents if (p/'tests').is_dir() and (p/'scripts').is_dir()),path.parent.parent)
            target=root.joinpath(*n.module.split('.')).with_suffix('.py')
            imported=fixture_names(target,set(seen))
            found.update(a.asname or a.name for a in n.names if a.name in imported)
    return found


def required_arguments(node):
    positional=node.args.posonlyargs+node.args.args
    required=positional[:len(positional)-len(node.args.defaults)]
    if required and required[0].arg in ('self','cls'):required=required[1:]
    return [p.arg for p in required]+[p.arg for p,d in zip(node.args.kwonlyargs,node.args.kw_defaults) if d is None]


def parametrized_arguments(node):
    found=set()
    for d in node.decorator_list:
        if not isinstance(d,ast.Call) or not d.args:continue
        f=d.func
        if not isinstance(f,ast.Attribute) or f.attr!='parametrize':continue
        arg=d.args[0]
        if isinstance(arg,ast.Constant) and isinstance(arg.value,str):
            found.update(x.strip() for x in arg.value.split(',') if x.strip())
        elif isinstance(arg,(ast.List,ast.Tuple)):
            found.update(x.value for x in arg.elts if isinstance(x,ast.Constant) and isinstance(x.value,str))
    return found


def is_script_test(path):
    try:tree=ast.parse(Path(path).read_text())
    except (OSError,SyntaxError):return False
    fixtures=BUILTINS|fixture_names(path)|fixture_names(Path(path).parent/'conftest.py')
    for n in ast.walk(tree):
        if isinstance(n,ast.ClassDef) and n.name.startswith('Test'):return False
        if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_') and set(required_arguments(n))<=fixtures|parametrized_arguments(n):return False
    return True
