# Stage A replay audit, current status November 23, 2026

This is an engineering audit, not Stage A qualification. The original findings
below are preserved as a historical audit. Their proposed-remediation section
is not the current implementation status and is not an execution grant.

## Current implementation and evidence boundaries

- Explicit `v1-legacy` preserves the old replay model. Default
  `v2-timestamp-correct` refuses malformed, missing, boolean, string,
  nonfinite, negative or numerically unrepresentable safety timestamps when
  a safety score would otherwise admit entry. Numeric zero is retained for
  explicit relative-time fixtures. Entries cannot precede that validated time.
- V2 uses rug labels to block entry only when validated label availability
  proves the label existed at the first snapshot. Invalid or future labels
  remain out of eligibility, not a reason to remove later losses. An explicit
  malformed recorded time cannot fall back to a different outcome time.
- V2 validates dictionary-observation timestamps before replay: finite numeric
  nonnegative times in original nondecreasing order. It refuses invalid series,
  never silently sorts or repairs them. Equal times remain as recorded.
  Non-dictionary placeholders retain the existing skip behavior, which does
  not authenticate missing-observation coverage.
- Holdout splitting now excludes unknown snapshot chronology before calculating
  the valid split and reports rejected records separately, including their
  paper accounting. Invalid records cannot move valid records across a split.
  Sorting history starts is not enough: retained-window overlap or touching,
  unknown chronology, and empty segments are diagnosed. Even separated
  retained windows are always Stage A unqualified.
- Snapshot-window diagnostics do not establish safety/label/source availability
  embargoes, immutable evaluation sets or tuning isolation. Repeatedly tuning
  against evaluation results can overfit. Representative histories, calibrated
  fills and execution failures, and effectiveness evidence remain absent.
- Snapshot journal recovery and truncation/gap diagnostics exist. A label such
  as journal-whole-no-detected-gaps is not proof that every required source
  observation was acquired, recorded or available at its recorded timestamp.

The timing, chronology, cohort and overflow fixes have independently reviewed
focused evidence. The refreshed scoped checkpoint is 3,384 unique pytest nodes
in nine isolated partitions and 179 script processes, including an explicit
zero-check opt-in live skip. Those are code-validation evidence, not completed
live provider, phone, reboot, signing, execution or trading qualification.
The subsequent paper-summary correction is separately bounded accepted after
159 independent focused passes for numeric reporting only; that change makes
earlier broad coverage historical for its affected source. `realized_mult` is a
recorded price ratio, not verified net account return. `economics_complete`
means typed numeric availability only, not basis/fee/partial-close/FX consistency.
Source-level receipt provenance and legacy missing-basis handling are under
further correction and are not qualified by those 159 passes.
No trading, charges or automatic deployment follow from these checks.

## Historical audit, preserved below

# Stage A lookahead audit — backtest dataset + replay (2026-11-23)

Scope: `daemon/launch_scanner/backtest.py` (replay engine), the history
recorders in `daemon/launch_scanner/__init__.py` (`_backtest_record`,
`_backtest_record_paper_close`), and the snapshot source
(`fingerprints.FingerprintTracker._series`). Question: can any field used
by a replay decision carry information from after the simulated decision
time?

## Findings

### F1 — CONFIRMED LOOKAHEAD: confirm-time safety gates earlier entries

- Judge records (`outcome_kind="judge"`) store
  `safety = cand.get("safety")` at confirm/reject time
  (`_backtest_record`, `__init__.py` ~3440).
- On the CONFIRM path, `cand["safety"]` is refreshed by
  `_confirm_safety_ok` -> `safety.check_token(...)` — a FRESH lookup made
  at confirm time, immediately before the record is written
  (`__init__.py` ~3666-3739).
- The replay (`BacktestEngine._find_entry`) may enter at the FIRST
  retained snapshot, which predates the verdict by up to
  `fp_window_polls` (default 6 polls).
- `BacktestEngine._history_gate` applies that confirm-time safety score
  (`min_safety_score`, `require_known_safety`) to the WHOLE history,
  including entries simulated minutes earlier.
- Net effect: the simulated entry decision uses safety information that
  did not exist at the simulated entry time. Direction of bias: entries
  that real-time logic could not have made are admitted (score known
  later), and some real-time-enterable coins are excluded. Unquantified.
- Asymmetry: REJECT records are written BEFORE the confirm-time safety
  refresh (`_backtest_record(..., "reject")` at ~3661), so their stored
  safety is the earlier candidate state (often None/unknown). Confirm
  and reject records therefore carry safety from different times.

### F2 — Latent hazard: `outcome.rug` gate structure

- `_history_gate` blocks histories with `outcome.rug` before simulation.
- Today this is INERT for judge records: their outcome is hardcoded
  `rug: False` at record time. Paper records
  (`outcome_kind="paper"`) are tallied as-reported and never pass
  through `_history_gate`.
- Hazard: if judge-record outcomes ever carry realized rug labels
  (computed at/after close), the same gate would inject hindsight —
  simulated entries would skip coins that only later proved to be rugs.
- Guard recommendation: `_history_gate` must refuse any non-paper
  record whose outcome timestamp (`end_t`) is after its first snapshot
  time, rather than trusting the field.

### F3 — Not lookahead, but a representativeness limit: truncated window

- Snapshots come from `FingerprintTracker._series`, a deque with
  `maxlen = fp_window_polls` (default 6). Records contain only the last
  <=6 polls before the verdict/close, not the full series since first
  detection.
- `_find_entry` therefore "enters" at the earliest RETAINED observation,
  not the true detection/alert time. Replay entries are later than real
  entries by the truncation gap. This is coverage loss, not hindsight;
  it understates hold time and can move entries to different prices.

### F4 — Clean: snapshot fields themselves

- Each observation tuple is `(ts, mcap, price, vol_m5, buys_m5,
  sells_m5, liq, holders?)` appended by `FingerprintTracker.observe()`
  at poll time — all contemporaneous per-poll DexScreener stats.
  Append-only, chronological by construction. No field is revised after
  the fact.
- Paper records are realized outcomes recorded at close and tallied
  as-reported (never re-simulated); no replay decision consumes them.

### F5 — Grouping fields, not decision fields

- `sentiment`, `narratives`, `narrative_heat` are verdict-time values,
  but the engine uses them only for per-narrative tally grouping, never
  for entry/exit decisions. No decision lookahead; verdict-time
  narrative tags can misattribute a coin whose narrative shifted
  mid-window (reporting nuance only).

## Remediation (proposed, not yet implemented)

1. Recorder: add `safety_eval_t` (timestamp of the safety report) to
   judge records; paper records already carry real `t_entry`.
2. Engine: with cost-model/timestamp-correct mode, do not enter before
   `safety_eval_t` (skip earlier snapshots); label records lacking the
   field as legacy-window entries, never silently mixed.
3. Engine guard per F2: refuse non-paper records whose `outcome.end_t`
   postdates the last snapshot when gating on `outcome`.
4. Recorder: persist the FULL series (not the truncated deque) so
   entries can be simulated from true detection time; label pre-fix
   records window-truncated.

Status: findings only. No replay semantics changed in this commit;
verdict math and registered paper outcomes untouched.
