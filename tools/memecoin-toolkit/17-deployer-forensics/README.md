# Deployer Forensics (lab)

A from-scratch, pure-stdlib lab for creator-history analysis: what a token deployer's launch record actually supports saying about them. Everything here is synthetic — no network, no live bot code, no real token data — so serial ruggers, clean creators, and total unknowns can be built as fixtures and every verdict boundary can be exercised safely.

## What it does

Module `deployer.py` — an enum `Verdict` (`UNKNOWN`, `CLEAN`, `WATCH`, `SERIAL_RUGGER`) and a frozen dataclass `Launch(deployer, mint, name, timestamp_hours, funded_by, outcome)` where `outcome` is `"rug"`, `"runner"`, `"flat"`, or `"unknown"`. Construction is fail-closed: `Launch` raises `ValueError` on an empty deployer or mint, a negative timestamp, or an outcome outside that set.

- `verdict_for_records(records) -> Verdict` / `verdict_for(deployer, records) -> Verdict` — the verdict a record supports. Fail-closed semantics: no history at all, or a history whose outcomes are all unknown, is `UNKNOWN` — unknown history is **not** clean. `SERIAL_RUGGER` requires ≥ 3 launches **and** ≥ 2 rugs. `WATCH` covers any single rug below the serial bar, heavy funding-source or name-pattern reuse, and any record too thin or too incomplete to clear. `CLEAN` is earned only with ≥ 2 launches, every outcome known, zero rugs, and no reuse flags. `verdict_for` raises `ValueError` on an empty deployer string.
- `is_serial_rugger(records) -> bool` — the ≥ 3 launches / ≥ 2 rugs bar, exactly.
- `rug_rate(records) -> float | None` — rugs over *known* outcomes only; `None` when nothing is known, so no caller can read missing evidence as a 0% rug rate. Also `rug_count(records) -> int` and `known_count(records) -> int`.
- `fingerprint(records) -> Fingerprint` — per-deployer features: launch count, known outcomes, rug count, distinct funding sources, `funding_reuse_ratio` (share of launches whose funding wallet funded another of this creator's launches), `name_reuse_ratio` (1 − distinct name patterns / launches), `median_interval_hours` (launch cadence; `None` below two launches), and distinct name patterns. Order-insensitive: records are sorted by time first.
- `name_pattern(name) -> str` — name normalisation for reuse detection: lowercased, punctuation stripped, trailing digits/separators removed, so `"MoonCat"`, `"MoonCat 2"` and `"MoonCat 2!!"` collide; an unusable name becomes `"<empty>"`.
- `launches_by_deployer(records) -> dict[str, list[Launch]]` — records grouped per deployer, each group time-sorted.
- `analyze(records) -> dict[str, DeployerReport]` — verdict, fingerprint, and rug rate per deployer, in one pass.
- `blacklist_candidates(records) -> list[str]` — the deployers whose verdict is `SERIAL_RUGGER`, sorted. `should_veto(deployer, records) -> bool` — the repeat-rug veto: only `SERIAL_RUGGER` vetoes; `WATCH` and `UNKNOWN` inform without convicting (and a non-veto must never be read as "clean").
- Synthetic fixtures: `serial_rugger_history()` (one funder, one name pattern, four launches, three rugs), `clean_creator_history()` (three launches, all known, no rugs, fresh funding and names), `mixed_history()` (serial + clean + one-rug `WATCH` + a ghost whose only launch has an unknown outcome).

`demo.py` prints the per-deployer reports for the mixed history, the blacklist candidates, and the veto contrast between the serial rugger and a total stranger — whose verdict is `UNKNOWN`, not `CLEAN`.

## Test suite

46 tests, all passing (`pytest -q`: "46 passed"). Covered: launch-record validation (parametrised invalid deployer/mint/timestamp/outcome); rug and known-outcome detection; grouping by deployer with time-sorting; rug/known counting; rug rate over known outcomes only, `None` when nothing is known, and 0.0 for a clean known history; the serial bar met, and failing on launch count alone and on rug count alone; fail-closed verdicts — no history `UNKNOWN`, all-unknown outcomes `UNKNOWN`, a stranger `UNKNOWN` rather than clean; the serial and clean fixtures' verdicts; a single clean launch not being `CLEAN`; two clean launches being `CLEAN`; a partly-unknown history blocked from `CLEAN`; one rug and two-rugs-in-two-launches both landing on `WATCH` rather than a thin-data serial conviction; name-pattern normalisation including the empty cases; fingerprint values on both fixtures (funding reuse 1.0 vs 0.0, median intervals 6h vs 130h); empty and single-launch fingerprints; name-reuse and funding-reuse ratio arithmetic; funding reuse alone and name reuse alone each triggering `WATCH`; cadence median on evenly spaced launches; fingerprint order-insensitivity; the mixed history's four verdicts and rug rates through `analyze()`; blacklist candidates containing only the serial rugger; and `should_veto` firing only for `SERIAL_RUGGER`.

## Captured output

Verbatim output of `python demo.py`:

```
=== Deployer forensics lab (synthetic creator histories) ===

GhostGary: verdict UNKNOWN
  launches 1, rugs 0, rug rate unknown
  funding sources 1, funding reuse 0%, name reuse 0%, median interval Noneh

HonestAda: verdict CLEAN
  launches 3, rugs 0, rug rate 0%
  funding sources 3, funding reuse 0%, name reuse 0%, median interval 130.0h

OneRugRita: verdict WATCH
  launches 2, rugs 1, rug rate 50%
  funding sources 1, funding reuse 100%, name reuse 50%, median interval 30.0h

SerialSam: verdict SERIAL_RUGGER
  launches 4, rugs 3, rug rate 75%
  funding sources 1, funding reuse 100%, name reuse 50%, median interval 6.0h

Blacklist candidates (serial ruggers): ['SerialSam']
Veto SerialSam on a new launch? True
Veto a total stranger? False -- but their verdict is UNKNOWN, not CLEAN: unknown history is never read as clean.
```

## What this mirrors in the live bot

This mirrors the live veto engine's repeat-rug deployer check and the deployer blacklist behind it: a creator who has launched repeatedly and rugged repeatedly is vetoed outright — no score overrides it — while a creator with one rug, a recycled funding wallet, or a habit of re-issuing the same name pattern is a warning to weigh, not a conviction. It also mirrors the live system's fail-closed stance on identity: a deployer with no history, or a history whose outcomes were never recorded, is `UNKNOWN`, and unknown is never quietly promoted to clean — the same reason unknown safety is never treated as safe elsewhere in the live bot. It is a teaching/checking model of that reasoning only; it shares no code with the live veto engine.

## Self-exam

1. Why is a deployer with three launches and three unknown outcomes `UNKNOWN` rather than `CLEAN`, and what bad trade does the alternative allow?
2. A deployer has two launches and both rugged. Why is the verdict `WATCH` and not `SERIAL_RUGGER` — what does the launch-count half of the bar protect against?
3. Why does `rug_rate` return `None` instead of 0.0 when no outcomes are known?
4. Funding-source reuse alone can trigger `WATCH` with zero rugs. Why is a recycled funding wallet evidence at all, and why is it not enough to veto?
5. What does name-pattern normalisation have to strip before `"MoonCat 2!!"` and `"mooncat"` can be recognised as the same pattern, and what legitimate creator behaviour could it falsely flag?
6. Why does `should_veto` refuse to veto a `WATCH` deployer, and who or what is supposed to act on a `WATCH` instead?
