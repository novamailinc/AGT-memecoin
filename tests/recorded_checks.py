"""Bridge legacy accumulating check() recorders to per-test pytest outcomes."""
import pytest


@pytest.fixture(autouse=True)
def recorded_check_guard(request):
    scope = vars(request.module)
    names = [name for name in ('FAILS', 'FAILURES', 'PASSES', 'TOTAL', 'MATRIX')
             if isinstance(scope.get(name), list)]
    previous = {name: list(scope[name]) for name in names}
    for name in names:
        scope[name][:] = [0] if name in ('PASSES', 'TOTAL') else []
    failures = []
    try:
        yield
    finally:
        for name in ('FAILS', 'FAILURES'):
            if name in previous:
                failures.extend(scope[name])
        for name in names:
            scope[name][:] = previous[name]
    assert not failures, 'Recorded check failures: ' + repr(failures)
