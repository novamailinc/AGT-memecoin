# Regression network boundary

Default regression runs must have no outside network route. On Linux, use a
network namespace, enable loopback for local HTTP/WebSocket tests, then drop
capabilities before running tests. A network namespace alone may map the caller
to root and invalidate permission tests. Check CapEff/CapPrm/CapBnd are zero.

The November 23 source review covered the default pytest/script-runner entry
points, explicit live-smoke strings and literal external URL call sites. It found:

- lessons test: exit polling fetched Jupiter in a batch before consulting its
  fixture price function. The test now disables the Jupiter seam.
- robust_http test: explicit GeckoTerminal/DexScreener smoke calls and a live
  GeckoTerminal rate-gap check. These now use local HTTP fixtures. Opt-in public
  diagnostics remain in scripts/robust-http-live-smoke, never a default test.
- mimic_curve's Helius 429 text is a patched urllib exception with a fake key,
  not a genuine request.

This bounded source audit is not a proof that all dynamic call paths are mocked.
OS isolation is the regression boundary, including subprocesses. Previous
unrestricted runs have no complete request ledger. Successful tests do not prove
absence of external calls or keyed usage in those historical runs.

## Assertions must reach both runners

A module with collectable pytest functions is excluded from run_script_tests.py.
Custom check helpers in such a module must raise on failure. Accumulating a list
checked only by __main__ silently passes pytest. test_dossier_archive now raises
immediately, fixing the false-green result for its sample-count assertion.
