# Signal-Card Design — study notes

## Core ideas (teach-back)

A signal card is not a notification. It is the whole evidence pack a human sees before gambling their
own money on a token. If the human cannot judge the trade from the card alone, the card must not be
sent.

The governing slogan is:

> No bare contract addresses, ever.
> A card that cannot be judged cannot be sent.

A bare CA — just a mint address — fails that test completely. It gives an address to copy and nothing to
think with: no size of market, no liquidity, no safety verdict, no supply, no chain, and no exit plan.

`cardcheck.py` turns that slogan into a completeness contract. A card is either a `dict` or a `Card`
dataclass. `validate(card)` returns a list of problem strings. An empty list means valid. `None` is not
an empty card — it raises `ValueError`.

The nine always-required elements, from `required_fields()`, are:

1. mcap / FDV
2. liquidity
3. context line
4. safety status line
5. CA
6. copy-CA button
7. total supply line
8. chain provenance
9. per-chain exit hint

A tenth element is conditional: pick cards (`is_pick=True`) must also carry the `📍 I bought this` track
button. Non-pick cards do not need it.

Each element earns its place:

- **Valuation:** `mcap_usd` *or* `fdv_usd` must be a positive number. Either one alone satisfies the
  element. Zero, negative values, and booleans do not count as numbers.
- **Liquidity:** `liquidity_usd`
  must be positive. Without it, the reader cannot tell a tradable pool from dust.
- **Context line:** a
  non-empty `context_line`. This is the compressed market story — mcap, liquidity, holders, motion.
- **Safety line:** a non-empty `safety_line`. This carries the safety verdict, which the CA syntax check
  cannot.
- **Supply line:** a non-empty `supply_line`, e.g. `Supply 1.0B`. Supply lets the reader
  sanity-check valuation claims.
- **Provenance:** `chain` must be one of `solana`, `base`, `ethereum`,
  `hyperliquid`, *and* `provenance_line` must be non-empty. An unknown chain is reported as a provenance
  failure, not given its own problem string.
- **Exit hint:** a non-empty `exit_hint` that matches its
  own chain. Exiting is chain-specific, so a generic or wrong-chain hint is a defect.

Buttons are detected by markers inside the button list: `📋 Copy CA` for the copy button and `📍 I bought
this` for the track button. A single string in place of a button list is treated as one button.

The track button is not decoration. In the live practice this mirrors, tapping it journals the user's
per-chain outcomes, and that journal drives per-chain feed kill switches. A missing track button is lost
measurement, not a cosmetic gap.

## Key derivations & formulas

There is no arithmetic theorem here; the derivation is a decision procedure.

Validation proceeds in a stable field order:

1. provenance: `chain_ok AND provenance_line non-empty`
2. valuation: `positive(mcap_usd) OR positive(fdv_usd)`
3. liquidity: `positive(liquidity_usd)`
4. context: `non-empty(context_line)`
5. safety: `non-empty(safety_line)`
6. CA: missing, invalid, or valid — see below
7. copy button: marker present in buttons
8. supply: `non-empty(supply_line)`
9. exit hint: missing, wrong-chain, or matching
10. track button: required only if `is_pick`

where:

```text
positive(v)  =  isinstance(v, (int, float)) AND NOT isinstance(v, bool) AND v > 0
non-empty(v) =  isinstance(v, str) AND v.strip() != ""
valid(card)  <=> validate(card) == []
```

The CA check, `is_valid_ca(ca)`, is purely syntactic:

```text
is_valid_ca(ca) <=> ca is a str
                    AND 32 <= len(ca) <= 44
                    AND every character is in the base58 alphabet
```

The base58 alphabet excludes `0`, `O`, `I`, and `l` — the four look-alike characters most likely to be
mistyped or misread. On dict cards a `ca` key is accepted as a fallback for `mint`. Missing or blank CA
gives `"missing CA"`; present but failing the check gives `"invalid CA"`. Those are two different
defects and they are named differently.

The exit-hint check is a membership test:

```text
exit_hint_matches_chain(chain, hint)
  <=> hint (lowercased) contains at least one word
      from _CHAIN_HINT_WORDS[chain]
```

Chain vocabularies in the source include, for example:

- solana: `solana`, `sol`, `raydium`, `pumpswap`, `pump.fun`
- base: `base`, `aerodrome`, `coinbase`
- ethereum: `ethereum`, `eth`, `gas`, `uniswap`
- hyperliquid: `hyperliquid`, `hl`, `mark price`, `perp`

Precedence inside the exit check matters: an empty hint reports `"missing per-chain exit hint"`; a
non-empty hint on a known chain with no matching word reports the distinct `"exit hint does not match
chain"`.

Invariants the tests enforce:

- Each single-element break produces *exactly one* problem, naming exactly that element.
- Multiple simultaneous breaks are all reported, in order.
- `summarize(card)` returns only `{"valid",
  "problems", "chain", "is_pick"}` for logging.

## Worked example

From `demo.py`, the complete Solana pick card:

- chain `solana`
- mcap and FDV both $250,000
- liquidity $60,000
- context: `📊 $250k mcap · $60k liq · 120 holders, rising 5m`
- safety: `🛡 Safety: PASS · score 42/100 · no hard fails`
- supply: `Supply 1.0B`
- provenance: `⛓ Solana runner feed`
- exit hint: `Exit: Solana ladder, sell on Raydium — 50% at 2x`
- buttons: `📋 Copy CA` and `📍 I bought this`
- `is_pick=True`

Result: `validate -> []`.

Then one element is broken at a time:

- no safety line → `['missing safety status line']`
- no copy-CA button → `['missing copy-CA button']`
- no supply line → `['missing total supply line']`
- pick without track button → `['missing 📍 I bought this track button']`

A bare CA and nothing else produces eight problems at once: missing chain provenance, mcap/FDV,
liquidity, context line, safety status line, copy-CA button, total supply line, and per-chain exit hint.
That list *is* the definition of "cannot be judged."

Finally, the mint `0OIl-not-base58` gives `['invalid CA']`: it is too short and contains exactly the
forbidden look-alikes.

## Trap log

- T1. **The bare-CA drop.** Sending just an address outsources all judgement to the reader and
  guarantees some readers judge with nothing.
- T2. **Booleans as numbers.** In Python `True` is an
  `int`; without an explicit bool exclusion, `mcap_usd=True` would pass as a positive valuation.
- T3. **Zero or negative "present" values.** A field that exists but is 0 or negative is not evidence; the
  validator treats it as missing.
- T4. **Confusing syntax with safety.** A base58, 32–44-char CA can
  still be a honeypot or a rug. Syntax lives in the CA check; danger lives in the safety line.
- T5. **Wrong-chain exit hint.** A well-written hint naming another chain's DEX is worse than a missing
  hint: a missing hint is noticed, a wrong one is followed.
- T6. **Unknown chain waved through.** A
  fifth, unrecognised chain has no known exit vocabulary or provenance; reporting it as a provenance
  failure keeps the gate closed.
- T7. **Treating the track button as UX polish.** On pick cards its
  absence silently breaks outcome journalling and the kill switches fed by it.
- T8. **Either/or read as
  both-required.** Demanding mcap *and* FDV would reject legitimate cards that honestly have only one;
  accepting either is deliberate.
- T9. **Aggregating problems into one vague error.** "Card invalid"
  teaches nothing. One named problem per broken element is what makes fixtures — and debugging —
  precise.

## Source flags

- The nine required elements, the conditional track button, and every problem string above are as
  implemented in `cardcheck.py` and listed in the README. [S]
- The CA boundaries (32 and 44 pass; 31 and 45 fail; `0 O I l` rejected) are covered by the toolkit's own tests — 38 tests, all passing per the README. [S]
- The claim that the track button journals per-chain outcomes and drives feed kill switches in the live bot is the README's "mirrors" section, not something this lab demonstrates. [M]
- The reading that a wrong-chain hint is *worse* than a missing one is a reasoned interpretation of why
  the two failures get distinct strings. [R]

## One-paragraph verdict

This toolkit proves that a signal card's completeness can be made into a fail-closed, testable contract: nine always-required elements plus a conditional track button, each with a named failure, a syntactic CA check with sharp boundaries, and a chain-matching exit hint — so a half-built card can be stopped before it is ever sent. It does not prove that a complete card is a *good* signal, that the safety line's contents are correct, or that any real card in production actually passed this gate; it models the contract only, with synthetic cards and no live code.
