# Signal Refinery — study notes

## Core ideas (teach-back)

Four chains produce signals; the human watches one lane. The refinery pattern is the plumbing between
them: many producers, one destination, and a strict border at the entrance — because anything malformed
that gets through lands on a person's phone, where it will be judged (and possibly bought) in seconds.

Three pieces do the work.

**1. A validated signal bus.** Every record crossing the bus is a plain dict that must validate against
schema version 1. Required fields: `schema, chain, source, mint, ts, score`. `validate_record(rec)`
returns `(ok, reasons)` — a boolean plus a list of named rejection reasons. Validation is fail-closed
and nothing is coerced: a record that is almost right is a rejected record.

Chains are a closed set: `solana`, `base`, `ethereum`, `hyperliquid`. Sources are short codes mapped to
feed names in both directions (`source_name` / `code_for_source`):

| code | feed |
|------|------|
| `df` | degen-feed |
| `sw` | smart-watcher |
| `br` | base-runner-feed |
| `er` | eth-runner-feed |
| `hl` | hl-listing-watch |
| `ll` | locked-liq-scan |
| `hb` | heartbeat |

An unknown chain or unknown source code is a rejection, not a warning. The cost asymmetry decides it: a
dropped good record costs one missed signal; a passed garbage record costs trust — and money — at the
destination.

**2. A tap journal.** The human's own behaviour is the quality signal. When the user taps "📍 I bought
this" on a card, that trade is journaled as a `TapOutcome(chain, mint, closed, peak_mult)`. A **win** is
defined once, precisely: closed, with `peak_mult >= 2.0`. Closed below 2.0 is a loss. Still open is
*neither* — counting open positions as losses would punish young chains for trades that have not
finished yet. `TapJournal` aggregates per chain: `tap_count`, and `win_rate`, which returns `None` when
a chain has no closes — never a fake `0.0`, because 0% from zero data and 0% from ten data points are
opposite situations wearing the same number.

**3. Per-chain feed switches.** `FeedSwitches` holds a pause flag per chain. Two hard rules are built
into the machine:

- **Solana can never be paused.** A pause attempt on Solana is refused, returns `False`, and is *recorded in `flags`* — the refusal is auditable, not silent. Solana is the home lane; this machinery does not get to switch it off.
- **There is no auto-resume.** `resume(by="auto")` is refused and flagged. Only `by="human"` resumes. Pausing can be automated because a bad chain costs money while it runs; resuming is a judgement call, because a recovering chain looks identical to a lucky streak until a human weighs it.

On top of those rules sits one automated judgement: `evaluate_auto_pause(journal)` pauses any non-Solana
chain whose journal shows **≥10 closed taps and a win rate strictly below 20%**. It is idempotent —
evaluating twice changes nothing the second time. `publish_allowed(rec)` is the gate at the bus exit:
validate the record, then refuse paused chains with `["chain_paused"]`.

## Key derivations & formulas

### Validation as a conjunction of independent checks

```text
ok(rec) <=> rec is a dict
            AND all required fields present
            AND rec.schema == 1
            AND rec.chain in CHAINS
            AND rec.source in SOURCE_CODES
            AND mint is a str with len >= 4
            AND ts is a non-bool number with ts > 0
            AND score is a non-bool number with 0 <= score <= 100
            AND source/chain consistency holds
```

Reasons accumulate rather than short-circuiting (after the missing-field early return): a record can be
rejected for several named reasons at once. Missing fields report as `missing:<field>`; the value
failures report as `bad_schema_version`, `unknown_chain`, `unknown_source`, `bad_mint`, `bad_ts`,
`bad_score`, and `source_chain_mismatch`.

The consistency check is the subtle one:

```text
expected_chain = {"br": "base", "er": "ethereum", "hl": "hyperliquid"}
if source in expected_chain AND chain != expected_chain[source]:
    reject with "source_chain_mismatch"
```

Both the chain and the source can be individually valid and jointly impossible. A `br` record claiming
chain `solana` means an upstream feed is mislabelling its output — a wiring bug, precisely the class of
bug that only a cross-field check can catch.

Booleans are excluded wherever a number is required (`ts`, `score`), because `True == 1` in Python and a
boolean timestamp is a type bug, not a timestamp.

### Win rate and the auto-pause predicate

```text
is_win(outcome)  <=> outcome.closed AND outcome.peak_mult >= 2.0
is_loss(outcome) <=> outcome.closed AND outcome.peak_mult <  2.0
win_rate(chain)   =  wins / closed_taps        (None if closed_taps == 0)

auto_pause(chain) <=> chain != solana
                      AND NOT already paused
                      AND closed_taps(chain) >= 10
                      AND win_rate(chain) is not None
                      AND win_rate(chain) < 0.20        (strict)
```

Constants: `WIN_PEAK_MULT = 2.0`, `AUTO_PAUSE_MIN_TAPS = 10`, `AUTO_PAUSE_MAX_WIN_RATE = 0.20`.

Each conjunct in the pause predicate blocks a different false pause:

- `>= 10 taps` blocks pausing on a tiny sample — 0 wins from 3 taps is noise, not evidence.
- `< 20%` (strict) blocks pausing a mediocre-but-real chain; exactly 20% — 2 wins from 10 — does *not* pause.
- `chain != solana` blocks the machinery from ever silencing the home lane, whatever the journal says.

`TapOutcome` validates at construction: unknown chain, empty mint, a closed outcome with no `peak_mult`,
or a negative multiple all raise `ValueError`.

### Switch transition rules

```text
pause(chain, by):   solana -> refuse, flag "SOLANA_PAUSE_REFUSED by=<by>", return False
                    other  -> paused = True, return True
resume(chain, by):  by != "human" -> refuse, flag "AUTO_RESUME_REFUSED ...", return False
                    by == "human" -> paused = False, return True
publish_allowed(rec): invalid rec -> (False, validation reasons)
                      paused chain -> (False, ["chain_paused"])
                      else -> (True, [])
```

## Worked example

From `demo.py`:

A good record — `{schema: 1, chain: "base", source: "br", mint: "0xabc123", ts: 1700000000, score: 72}`
— validates as `(True, [])`.

A garbage record — chain `"mars"`, source `"xx"`, mint `"x"`, ts `−1`, score `999` — is rejected with
five reasons at once:

```text
['unknown_chain', 'unknown_source', 'bad_mint', 'bad_ts', 'bad_score']
```

The journal scenario: 10 Base taps close at `peak_mult = 1.1` (all losses), plus 1 winner at 2.5. So
`closed = 11`, `win_rate = 1/11 ≈ 0.091`. Both pause conditions hold (11 ≥ 10, 0.091 < 0.20), so
`evaluate_auto_pause` returns `['base']` and `base_paused=True`.

Then the hard rules, in sequence:

- `pause('solana')` → `False`, Solana stays unpaused, flags record `SOLANA_PAUSE_REFUSED by=human`.
- `resume('base', by='auto')` → `False`, Base stays paused.
- `resume('base', by='human')` → `True`, Base unpaused.

Boundary cases from the test matrix round it out: 9 taps does not pause regardless of win rate; 30% win
rate does not pause regardless of taps; exactly 20% does not pause; and 2.0 itself is a win while 1.99
is a loss.

## Trap log

- T1. **Passing unknown chains through with a warning.** A warning at the bus is a garbage card on the phone. Reject at the border, where rejection is cheap.
- T2. **Individually valid, jointly impossible records.** A `br` source on a Solana record passes every single-field check. Without the cross-field consistency check, a miswired feed silently contaminates the lane.
- T3. **Reporting win rate 0.0 for no data.** An empty journal and a winless journal both printing "0%" makes a brand-new chain look like a proven loser — and the auto-pause logic would act on it. `None` is the honest value.
- T4. **Counting open trades as losses.** A young chain's trades are disproportionately still open; scoring them as losses manufactures a bad win rate and can pause a chain for being new.
- T5. **Pausing on a handful of taps.** Nine straight losses feel damning and prove almost nothing. The ≥10 floor is the minimum sample for the pause to mean anything.
- T6. **Auto-resume symmetry.** "It paused automatically, so it should resume automatically" ignores the asymmetry: a bad chain bleeds while it runs, but a paused chain costs only opportunity. Resumption stays a human decision.
- T7. **Silently refusing forbidden operations.** A refused Solana pause or auto-resume that leaves no trace looks identical to a bug. Flagging the refusal makes the boundary itself auditable.
- T8. **A fuzzy win definition.** "Did well" cannot drive a switch. Win as *closed with peak ≥ 2.0x* is arbitrary in the way all thresholds are — and, unlike a vibe, it is the same bar the exit ladder is built around, so journal and exits measure the same event.
- T9. **Score outside [0, 100] sneaking through.** A score of 999 or a boolean "score" signals upstream corruption of the record, not an exceptional signal. Range-and-type checks are data-integrity checks.

## Source flags

- Constants, source codes, validation reasons, switch rules, and the auto-pause predicate are read from `refinery.py`; `SCHEMA_VERSION = 1`. [S]
- The test matrix — 38 tests passing, including the exact-20% no-pause boundary, 2.0-wins/1.99-loses, Solana and auto-resume refusals being flagged, and idempotent auto-pause — is the README's test-suite account. [S]
- The mirroring of the live `signal_bus.py`, `feed_switches.json`, and `tap_journal.py`, and the statement that Solana is never auto-paused in the live system, are the README's live-bot claims. [M]
- The cost-asymmetry defence of rejecting unknown chains (missed signal vs lost trust) is reasoned here, following the README's self-exam framing. [R]

## One-paragraph verdict

This toolkit proves the refinery's border logic end to end on synthetic records: schema-1 validation that rejects unknown chains, unknown sources, bad values, and source/chain mismatches with named reasons; a tap journal with an exact win definition (closed, peak ≥ 2.0x) and honest `None` win rates; and kill switches whose two hard rules — Solana unpausable, no auto-resume — hold even against the automated judge, which itself fires only at ≥10 closed taps *and* a win rate strictly below 20%. It does not prove that the 2.0x win bar or the 10-tap/20% pause thresholds are the *right* policy numbers, that real feeds produce well-formed records, or that pausing a chain improves outcomes; it proves the machinery enforces whatever policy is registered, exactly at its stated boundaries.
