# Frozen historical evaluation intake

Status: proposed offline implementation boundary. Not performance evidence, qualification, a trading gate or authority for data collection. Current accepted affected checkpoint is 1826 scenario-input validation; broad 1819 is historical for later replay changes.

## Evidence gap verified

The current repository and default runtime data directory did not contain a backtest/fills/calibration/holdout JSONL artifact in the bounded search. The old sniper calibration note cites `/tmp/calib_sniper.py`, `/tmp/calib_dataset.jsonl`, `/tmp/calib_rows.json` and `/tmp/calib_mints.json`; those files are absent. This does not prove no history exists elsewhere. It means that note cannot be independently reproduced from its cited artifacts here.

The current registry is explicitly caller-evidence bookkeeping. Neither its status nor a fresh synthetic replay is measured strategy qualification. Current chronological splitting also does not enforce an untouched evaluation set.

## Smallest useful integrated correction

Add a separate offline frozen evaluation runner using the existing BacktestEngine, not a second strategy simulator. It takes an explicit manifest, with no network or production state mutation.

- Exact schema/version and role for every local artifact: dataset, strategy source set, effective replay configuration, calibration evidence and optional prior precommit manifest. Each entry names a path, SHA256, byte count and purpose. Required paths must be regular files; missing, unreadable, symlink escape, changed bytes, duplicate/colliding roles and unknown schema refuse. Hash exact bytes before parsing. Read each artifact once into a retained byte snapshot so a concurrent writer cannot create a hash/read mismatch. A local hash is consistency, not provenance or authorization.
- Parse JSON/JSONL strictly. Duplicate JSON keys, invalid UTF-8, malformed lines and nonfinite literal values produce unavailable evaluation, with input line counts/locations, not a selected clean subset. No silent dedup by symbol or richest snapshot. The file being present never proves authentic acquisition.
- Require explicit strategy/config binding, exact chronological segment membership by stable history identity and full record digest, and a typed fixed cutoff/embargo. Every supplied history has one disposition. Original-cohort provider qualification must carry across every segment, preserving paper economics even when simulation is refused. A duplicate outcome/history identity or conflicting row cannot receive independent sample weight by accident.
- Bind the complete source/data/config/calibration/segment manifest to a run ID and report. Run uses timestamp-correct replay and explicit provider-input requirement, plus explicit validated cost scenario assumptions. Other modes remain separate diagnostics, not qualified historical execution. Do not inherit a permissive default from absent manifest keys.
- Calibration file records measured-source artifact references, observed period, sample/dependence limitations and fee/fill/failure model version. The runner verifies local referenced bytes/schema and exact config linkage, not that a person actually measured them honestly. Unsupported calibration evidence reports unavailable. Scenario-only assumptions remain labeled scenario-only, never realistic-costs=true.
- A precommit artifact may bind strategy/config/data partition identity before evaluation. Without an independently established earlier source/time and access controls, it cannot prove the evaluation was untouched. Filesystem modification times and caller-provided timestamps are not evidence of tuning isolation. Report this as unverified even when a digest matches.
- Publication is an atomic local result bundle containing complete input manifest, exact engine/source hashes, complete refusal diagnostics, economic accounting and reproducible command/config. Never set Registry criteria, advance state or authorize execution automatically. A malformed artifact results in a refusal report, not a fake zero-trade performance success.

## Deliberately unresolved policy

No universal minimum sample, embargo duration, confidence level, acceptable return/drawdown or regime diversity is invented. Original targets are not empirical calibration. A future measured evaluator needs versioned, reviewed policy with dependence/uncertainty and whole-cohort risk accounting before a pass/fail claim. This runner can make an evaluation reproducible and explicit about missing evidence, not create the missing representative history or show an advantage.

## Required causal tests

Actual CLI manifest -> artifact verification -> existing strict replay/holdout -> report bundle. Missing or changed artifact, wrong hash/size/schema/role, concurrent mutation, duplicate JSON keys/row identity, malformed/nonfinite values, wrong cutoff/order/overlap/membership and unknown provider evidence refuse. Valid synthetic fixtures reproduce exact results but stay synthetic/unqualified. Rejected histories retain all paper losses. Restart and repeated execution reuse exact input bytes and never read production defaults or mutate registry/account/risk state. Source/config/calibration changes require a new binding, not a reused favorable report.

No live provider acquisition, data purchase, account change, deployment, trading or charges are part of this proposal. Provider history entitlements, real calibrated fills, continuous demo operation and AGT phone/reboot proof remain separate work.
