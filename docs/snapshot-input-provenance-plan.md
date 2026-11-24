# Snapshot input availability, unresolved implementation boundary

Status: design only. No journal format, realtime score or source-admission
policy is changed by this note. No permission for deployment or trading.

## Reproduced boundary

`FingerprintTracker.observe` converts missing/malformed market fields to zero
and numeric strings to numbers before retaining its observation tuple. Holder
zero becomes unknown. The retained-series guards cannot reconstruct whether
an existing zero was explicitly observed or substituted. The judge recorder
now rejects invalid retained series but cannot certify those earlier inputs.

## Smallest useful contract

1. Newly appended journal rows receive a version and field availability map
   captured before conversion. The map distinguishes explicit numeric zero,
   missing, explicit null, invalid type and nonfinite values. It identifies
   its boundary as tracker-input fields, not authenticated provider payloads.
2. Preserve current realtime tuple arithmetic initially. Historical acceptance
   reads availability separately and never treats an unknown field as zero.
   Any earlier upstream normalizer remains a separately named evidence gap.
3. Persist a matching availability sequence for the in-memory window or refuse
   historical fallback when it is absent. A new journal alone does not repair
   a missing/disabled-journal window path.
4. Legacy rows remain explicitly unrecorded availability. Never infer that
   old finite zeros were raw observed zeros. Retain legacy records and
   economics for diagnostics, with qualification unavailable.
5. Journal compaction retains the complete surviving row verbatim, including
   the version and map. Concurrent append/recovery must preserve alignment.
6. Both judge and paper-close snapshot consumers preserve this contract.
   Independent mint failures cannot poison valid mint evidence. Historical
   corruption cannot become a new source or receive a fabricated timestamp.

## Evidence needed before acceptance

- Actual source-to-tracker-to-journal-to-judge/paper recorder tests for missing,
  null, numeric zero, string zero, boolean, nonfinite and malformed fields.
- Identical realtime scores for unchanged valid inputs and explicit evidence
  of any intended behavior changes, not inferred equivalence.
- Fresh-process recovery, interrupted append, compaction and concurrent writer
  tests, with journal/window availability alignment and no silent reset.
- Legacy/new mixed journal tests proving no retroactive raw-input claims.
- Separate provider normalizer audit. Tracker-input availability cannot prove
  raw on-chain or vendor authenticity, latency or acquisition completeness.

Until implemented and reviewed, retained numeric validation is useful refusal
logic, not raw input authenticity or representative historical qualification.
