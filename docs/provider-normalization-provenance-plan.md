# Provider normalization provenance correction

Status: proposed private implementation. No live calls, deployment, trading or spending. Current accepted regression checkpoint is 1754. Its provider tests diagnose a gap, not repair it.

## Root cause

`sources._pair_to_stats` normalizes required market fields before `FingerprintTracker.observe`. `_f` converts absent, null, false, malformed strings and explicit zero into indistinguishable floats. Counts use `int(value or 0)`, which can also truncate fractional values. Market capitalization uses `marketCap or fdv`, so explicit numeric zero can select a different field. The tracker correctly records what it receives but cannot reconstruct those decisions.

`_gt_token_to_stats` keeps unavailable transaction and volume windows as None. Its market capitalization and liquidity conversion still loses source distinctions. Price is intentionally read from a decimal string on this provider, so a blanket rule that all strings are invalid would incorrectly exclude normal payloads.

## Correction without changing realtime numbers

1. Add a separately versioned normalization envelope to newly produced stats. Record provider, endpoint family, exact payload-field paths, raw typed field states and values, the selected market-cap path, normalization policy identifier, and the exact normalized numeric output. Bind the envelope to a source observation UUID and payload token/pair identity when supplied. At the provider wrapper, bind the resolved requested token and chain; a conflicting payload token must be unavailable rather than relabeled. This is a local consistency chain, not authenticated vendor data.
2. Preserve existing returned numeric keys and realtime scoring exactly. Distinguish existing truthiness fallback from historical qualification. An explicit marketCap zero with fdv fallback is recorded as that decision, not silently called observed market capitalization. A new historical consumer can refuse the ambiguous semantic substitution without changing realtime behavior.
3. Define provider-specific parsing policy rather than reusing tracker numeric-type rules. Explicit finite nonnegative numeric values are recorded. A strict finite decimal string on a field documented by the adapter as decimal-string-compatible is recorded as parsed-decimal-string, with its original type preserved. Empty strings, booleans, malformed text, nonfinite or negative values are unavailable. Counts require integral nonnegative source evidence; normalization truncation cannot become recorded count truth. Missing versus null remain separate. Zero is never treated as absent for provenance selection.
4. Carry a copy of the normalization envelope inside a new tracker observation envelope version, bound to its exact mint, observation identity, typed observation time, normalized values and input-state map. Do not attach provenance by list index. Version2 tracker-only observations remain version2 and cannot be upgraded. Stats without the new normalization envelope remain explicitly provider-unrecorded, including synthetic/internal producers.
5. Keep tracker-input eligibility and provider-normalization qualification as separate results. Existing tracker-only histories stay diagnostic/legacy-compatible with explicit qualification unavailable; they never become provider-qualified by a new reader. A provider-normalization qualification request must require and validate the new chain. Unknown versions, dropped envelopes, mint/chain/path/value mismatches and mixed old/new history refuse that qualification.
6. Journal/window retention and judge/ordinary/pinned paper consumers retain both scopes. Paper outcome/native PnL remains available when either provenance scope is unavailable. No provider-normalization gate may change economic risk accounting, settlement or exit execution. Record-time capture continues to bind the complete ordered result, including the added scope.

## Evidence before acceptance

- Actual offline provider payload -> single/batch normalizer -> scanner tracker -> journal/window -> actual judge and ordinary/pinned paper consumers, with envelope identity checked at each step.
- Numeric zero, string zero, valid decimal strings, absent/null, booleans, bad strings, nonfinite, negative, fractional counts, giant values and wrong container types. Record expected old arithmetic or old exception behavior separately from provenance qualification.
- Explicit marketCap zero versus absent marketCap/fdv substitution; provider price strings must not be mislabeled missing. Unsupported source/field policies fail closed.
- Requested-mint/chain versus payload identity conflicts; wrong envelope output and dropped/stale/reordered envelopes; mixed versions, restart, pruning, compaction, partial append, permissions and concurrent writers.
- Realtime numerical and score equivalence for valid and partial/malformed supported inputs. Any changed old exception behavior requires an explicit separate review, not a hidden normalization fix.
- Corrupt/unavailable provenance must preserve paper native outcome records, including forced ordinary-builder failure followed by pinned recovery.

## Limits not solved

This captures what the local adapter parsed. It does not authenticate the provider, prove response freshness/latency, complete acquisition, historical representativeness, economic effectiveness, free-tier global usage, abrupt power-loss durability, or phone/runtime survival. It does not reconstruct old payloads. Stage A history/calibration and existing provider/integration blockers remain open.
