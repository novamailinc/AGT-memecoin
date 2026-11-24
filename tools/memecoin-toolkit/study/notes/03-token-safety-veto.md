# Token Safety Veto — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

Most "safety scores" on token sites are one opaque number. This
lab in `veto.py` does the opposite: every rule about a mint is a
named, checkable fact, each fact contributes either a hard FAIL
or a soft FLAG, and any single hard FAIL blocks the token
outright — no averaging, no compensation [S].

Think of it as a bouncer with a checklist, not a judge with a
vibe. The checklist covers who still controls the token and what
Token-2022 extensions are attached:

Hard FAILs (each blocks, each named) [S]:

- `mint_authority_held` — someone can still print new supply.
- `freeze_authority_held` — someone can freeze your wallet.
- `permanent_delegate` — a delegate can move your tokens forever.
- `transfer_hook` — arbitrary code runs on every transfer.
- `non_transferable` — the token cannot be transferred/sold.
- `default_account_state_frozen` — new accounts start frozen.
- `transfer_fee_excessive` — transfer fee above 1000 bps (10%).

FLAGs only (warn, never block) [S]:

- `transfer_fee_present` — a fee with 0 < bps ≤ 1000.
- `mint_close_authority` extension present.
- `confidential_transfer_mint` extension present.

Authorities count as gone when they are None or a revoked synonym
("" / "none" / "burned", case/whitespace-insensitive); any other
string means a wallet still holds them. `"unknown"` means the
data was never fetched — and that is not treated as clean [S].

Which brings up the second big idea: unknown is its own verdict.
A None report, an `"unknown"` authority, extensions or fee never
fetched, or a malformed fee all return status `"UNKNOWN"`,
passed=False, score=0, with the missing pieces named in fails.
`evaluate` never raises on missing safety data — missing data
*is* the verdict. For pass/fail purposes unknown groups with the
failures: an unchecked token does not get in [S].

Third idea, pinned by the tests as the standing design rule:
**the score is a floor, never a ranker.** The score exists so a
caller can demand "at least this clean" via `meets_score_floor`.
Tokens are never sorted by score to pick the least-bad of a
risky set, and `passed` is decided by the absence of fails,
never by the score. A failed or unknown verdict meets no floor
at all — not even floor 0 [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Score arithmetic — deliberately simple, deliberately secondary [S]:

    score = max(0, 100 − 25 × len(fails) − 5 × len(flags))

Worked values pinned by tests:

- Clean report: 100 − 0 − 0 = 100, PASS.
- One flag (e.g. a 2% transfer fee): 100 − 5 = 95, PASS.
- One hard fail: 100 − 25 = 75, FAIL.
- Hook + permanent delegate: 100 − 50 = 50, FAIL.
- 2 fails + 1 flag: 100 − 50 − 5 = 45.
- All seven hard fails + both flags: raw −90, floored to 0.

The floor matters more than the formula: `meets_score_floor`
returns True iff `passed` AND `score ≥ min_score`, and raises
`ValueError` unless `min_score` is an int in [0, 100] [S].

Transfer-fee boundary logic [S]:

    bps == 0            → no finding
    0 < bps ≤ 1000      → FLAG  transfer_fee_present
    bps > 1000          → FAIL  transfer_fee_excessive
    bps malformed       → UNKNOWN, fail transfer_fee_invalid

Malformed means negative, fractional, a string, or a bool —
`True` is an `int` subclass in Python but is not a fee [S].

Extension matching is shape-tolerant on purpose: entries may be
a plain name, a `"name:value"` string, or a dict keyed by
`"type"` or `"name"`; matching normalises case and separators,
and duplicate entries count once. Unrecognised extensions are
ignored at score 100. A dict-form `defaultAccountState` can also
report frozen, but the report's own `default_account_state`
field takes precedence in both directions [S].

Evaluation pipeline [R, summarising the source]:

    1. Coerce input: TokenReport | dict (via from_dict) | other.
       Non-report objects → UNKNOWN (unknown_report_type).
    2. Check for never-fetched pieces → UNKNOWN with named fails.
    3. Validate the fee's type → UNKNOWN if malformed.
    4. Apply the seven FAIL rules and three FLAG rules.
    5. passed = (fails is empty); status = PASS / FAIL.

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py` [S]:

- Clean token (authorities revoked, no extensions): PASS,
  passed=True, score=100, no fails or flags.
- Benign flag only (2% transfer fee): PASS, score=95,
  flags=['transfer_fee_present'].
- Mint authority still held by a wallet: FAIL, score=75,
  fails=['mint_authority_held'].
- Token-2022 trap bundle (hook + permanent delegate): FAIL,
  score=50, fails=['permanent_delegate', 'transfer_hook'].
- Fee boundary, exactly 1000 bps (10%): PASS, score=95, flag.
- Fee boundary, 1001 bps: FAIL, score=75,
  fails=['transfer_fee_excessive'].
- Frozen by default: FAIL, score=75,
  fails=['default_account_state_frozen'].
- Report never fetched (extensions/fee missing): UNKNOWN,
  score=0, fails=['extensions_unknown', 'transfer_fee_unknown'].
- No report at all: UNKNOWN, score=0, fails=['unknown_report'].

Note the didactic pair: the 95-point flagged token and the
75-point failed token differ by 20 points but by an entire
category — one may pass a floor check, the other never may [R].

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Ranking by score. Picking the "highest score" of a risky
  set lets a 75-with-a-fail compete with passes. Score is a
  floor, never a ranker; fails block regardless of points.
- T2. Treating unknown as clean. Never-fetched safety data is
  UNKNOWN with passed=False — the most dangerous default in
  token screening is assume-fine.
- T3. Treating unknown as dirty either. UNKNOWN names what is
  missing; it is a data verdict, not an accusation — keep it
  distinct from FAIL even though both block.
- T4. Arguing about the 1000/1001 boundary as arbitrary. Every
  fee line sits somewhere; the defensible part is that it is
  named, constant, and tested on both sides.
- T5. Trusting one extension format. Real reports arrive as
  names, `name:value` strings, and dicts; a strict parser that
  misses the dict form misses the trap.
- T6. Letting `True` be a fee. Python bools are ints; without an
  explicit check a malformed report sails through as 1 bp.
- T7. Scoring duplicates twice. The same extension listed in two
  shapes is one finding — dedupe before arithmetic.
- T8. Gating on the creator field. `creator` is carried for
  context and never gates; authority and extensions decide.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Seven named FAILs, three FLAGs, score formula and floor,
  fee boundary 1000/1001 bps, unknown handling in five shapes:
  [S] — pinned by the 41-test suite.
- Demo verdicts/scores listed above: [S] — verbatim output.
- "Score is a floor, never a ranker": [S] as a test-pinned rule
  and [M] as the live bot's standing bar (README + live practice).
- Same component checks behind the live fail-closed choke point
  in `instant_safety.py` / `veto.py`: [M] — lab shares no code.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves a component veto can be made fully explicit:
every block traces to a named rule, every unknown traces to a
named missing input, and the score can never launder a fail into
a pass — verified across 41 tests including boundaries, malformed
inputs, and duplicate/shape permutations [S]. It does not prove
a passing token is safe in any broader sense: the checklist only
covers authorities, extensions, and fees in a synthetic report —
nothing here inspects liquidity, holders, deployer history, or
market behaviour, and a clean PASS is the absence of these
specific traps, not a certificate [R].
