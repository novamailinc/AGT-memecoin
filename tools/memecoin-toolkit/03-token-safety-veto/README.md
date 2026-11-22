# Token Safety Veto (lab)

A from-scratch, pure-stdlib lab for component-based token safety evaluation: instead of one opaque safety number, each rule about a mint's authorities and Token-2022 extensions contributes a named fail or flag, and any single hard FAIL blocks the token outright. All reports are synthetic fixtures — no network, no live bot code, no real mints.

The standing design rule, pinned by the tests: **the score is a floor, never a ranker.** A token clears a minimum bar or it does not pass; tokens are never sorted by score to pick the "best" of a risky set, and no score outranks a hard FAIL.

## What it does

Module `veto.py`:

- `TokenReport` — a dataclass of safety facts: `mint_authority` and `freeze_authority` (`None`/`"revoked"` = gone, any other string = a wallet still holds it, `"unknown"` = never fetched), `extensions` (list of names, `"name:value"` strings, or `{"type": ...}` dicts; `None` = never fetched), `transfer_fee_bps` (`None` = never fetched), `default_account_state`, `creator`, and `source`. `TokenReport.from_dict(data)` builds one from a dict, turning absent keys into unknown sentinels.
- `Verdict` — the result dataclass: `passed: bool`, `fails: list`, `flags: list`, `score: int`, `source: str`, plus `status` (`"PASS"` / `"FAIL"` / `"UNKNOWN"`).
- `evaluate(report) -> Verdict` — the single entry point; accepts a `TokenReport`, a dict, or `None`. Hard FAILs: mint authority held by a wallet (`mint_authority_held`); freeze authority held by a wallet (`freeze_authority_held`); `permanentDelegate` extension (`permanent_delegate`); `transferHook` (`transfer_hook`); `nonTransferable` (`non_transferable`); default account state frozen (`default_account_state_frozen`); transfer fee > 1000 bps (`transfer_fee_excessive`). FLAGs only: transfer fee with `0 < bps <= 1000` (`transfer_fee_present`); `mintCloseAuthority` (`mint_close_authority`); `confidentialTransferMint` (`confidential_transfer_mint`). Score convention: `100 - 25 * len(fails) - 5 * len(flags)`, floored at 0; `passed` is decided by the absence of fails, never by the score.
- Fail-closed unknown handling: a `None` report, a non-report object, an `"unknown"` authority, or `extensions`/`transfer_fee_bps` never fetched all return `status="UNKNOWN"`, `passed=False`, `score=0`, with the missing pieces named in `fails`. Unknown is never quietly treated as clean, and `evaluate` never raises on missing safety data — missing data *is* the verdict. A malformed fee (negative, fractional, a string, or a `bool` — `True` is an `int` subclass but not a fee) is likewise `UNKNOWN` with fail `transfer_fee_invalid`.
- `meets_score_floor(verdict, min_score) -> bool` — the one sanctioned use of the score: True iff the verdict passed *and* its score is at least `min_score`. A failed or unknown verdict meets no floor, not even 0 — the floor can only tighten a PASS, never loosen a FAIL. Raises `ValueError` on a `min_score` that is not an int in `[0, 100]`.
- `evaluate_many(reports) -> list[Verdict]` — evaluate a batch, verdicts in input order. Raises `ValueError` on `None` instead of a sequence.
- `summarize(verdicts) -> dict` — `{"total", "passed", "pass", "fail", "unknown"}` counts for a batch.
- `explain(verdict) -> str` — the verdict as plain lines: a `STATUS score=N source=…` header, then one `fail: name` / `flag: name` line per finding (or `no fails or flags`).

`demo.py` walks through a clean token, a flag-only token, authority and Token-2022 trap failures, the 1000/1001 bps fee boundary, a frozen-by-default mint, and two shapes of unknown report.

## Test suite

41 tests, all passing (`pytest -q`: "41 passed"). Covered: a clean token passing at score 100 and `"revoked"` strings counting as clean; a benign flag passing at 95 (the pinned rule: flags cost points, never block); each of the nine hard-fail conditions individually blocking at score 75, including dict-style and `"name:value"`-style extension entries; a non-frozen default account state staying clean; the transfer-fee boundary at 0 / 1 / 1000 (flag) vs 1001 bps (fail); flag-only extension pairs scoring 90; unknown reports of five different shapes all failing closed at score 0; score arithmetic (2 fails + 1 flag = 45) and the floor at 0; the score-never-outranks-a-fail property; and the report's `source` being carried through to the verdict. Added in the deepening pass: authority spellings — every revoked synonym (`None`, `""`, `"none"`, `"burned"`, case/whitespace variants) passing, and every `"unknown"` spelling (either authority) failing closed with the right named fail; the `creator` field never gating; extension permutations — case/separator variants of `transferHook` all matching, duplicate entries (mixed string/dict shapes) counted once at score 75, unrecognized extensions ignored at score 100, dict entries keyed by `"name"`, dict-form `defaultAccountState` frozen/initialized, and the report's own `default_account_state` taking precedence over the extension entry in both directions; combined findings — all seven hard fails plus both flags at once scoring 0 with every finding named, and a boundary-fee flag combined with one fail scoring exactly 70; malformed inputs — negative/fractional/string/`bool` fees all `UNKNOWN` (`transfer_fee_invalid`), wrong-type reports (int, string, list) all `UNKNOWN` (`unknown_report_type`), `from_dict({})` naming all four never-fetched pieces with the default source, `from_dict` rejecting a non-dict, `evaluate` accepting a clean dict directly, and a `default_account_state="unknown"` report failing closed while keeping its source; and the new helpers — `meets_score_floor` (a flagged PASS meeting its exact score but not one above, a FAIL/UNKNOWN meeting no floor at all, invalid floors raising), `evaluate_many` preserving order with `summarize` counting the mixed batch exactly (and `None`/empty batches handled), and `explain` rendering header plus per-finding lines.

## Captured output

Verbatim output of `python demo.py`:

```
=== Token safety veto lab (synthetic reports) ===
Rule: score is a floor, never a ranker. Any hard FAIL blocks.

Clean token (authorities revoked, no extensions): status=PASS passed=True score=100 fails=[] flags=[] source=synthetic-fixture
Benign flag only (2% transfer fee): status=PASS passed=True score=95 fails=[] flags=['transfer_fee_present'] source=synthetic-fixture
Mint authority still held by a wallet: status=FAIL passed=False score=75 fails=['mint_authority_held'] flags=[] source=synthetic-fixture
Token-2022 trap bundle (hook + permanent delegate): status=FAIL passed=False score=50 fails=['permanent_delegate', 'transfer_hook'] flags=[] source=synthetic-fixture
Fee boundary: exactly 1000 bps (10%): status=PASS passed=True score=95 fails=[] flags=['transfer_fee_present'] source=synthetic-fixture
Fee boundary: 1001 bps: status=FAIL passed=False score=75 fails=['transfer_fee_excessive'] flags=[] source=synthetic-fixture
Frozen by default: status=FAIL passed=False score=75 fails=['default_account_state_frozen'] flags=[] source=synthetic-fixture
Report never fetched (extensions/fee missing): status=UNKNOWN passed=False score=0 fails=['extensions_unknown', 'transfer_fee_unknown'] flags=[] source=synthetic-dict
No report at all: status=UNKNOWN passed=False score=0 fails=['unknown_report'] flags=[] source=unknown
```

## What this mirrors in the live bot

This mirrors the live paper bot's safety gate: the hard-fail component checks in `daemon/launch_scanner/instant_safety.py` and the veto layer in `daemon/launch_scanner/veto.py` — mint/freeze authority still held, Token-2022 traps (permanent delegate, transfer hook, non-transferable, frozen-by-default, excessive transfer fee) — sitting behind the single fail-closed choke point that decides whether a coin may be carded at all, where an unknown safety report blocks instead of passing. The lab reproduces that shape — named component fails, flag-only warnings, unknown = blocked, score as a floor — with synthetic fixtures only; it shares no code with the live bot.

## Self-exam

1. Why must "safety data was never fetched" produce a different verdict from both "clean" and "dirty" — and why is it grouped with the failures for pass/fail purposes?
2. Why is a transfer fee of exactly 1000 bps only a flag, while 1001 bps is a hard fail — what makes a boundary like that defensible?
3. A token scores 95 with one flag and another scores 75 with one fail. Why is it wrong to describe the first as "safer" in a way that could ever let the second through?
4. Which is more dangerous to leave enabled after launch, a held mint authority or a held freeze authority — and what different attacks does each one enable?
5. Why are extension entries accepted in three different shapes (plain name, `name:value`, dict) instead of one strict format?
