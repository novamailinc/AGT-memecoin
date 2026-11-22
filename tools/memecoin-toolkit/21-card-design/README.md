# Signal-Card Design (lab)

A from-scratch, pure-stdlib lab for the completeness contract of a signal card — the message a human reads before gambling their own money on a token. The rule under test: no bare contract addresses, ever; a card that cannot be judged cannot be sent. Everything here is synthetic — no network, no live bot code, no real token data — and every required element has a fixture that removes exactly that element and fails with exactly that element named.

## What it does

Module `cardcheck.py` — a `Card` dataclass plus dict support (cards may be either; `validate` reads both):

- `validate(card) -> list[str]` — the list of problems with the card; an empty list means valid. Raises `ValueError` on `None`. Every card must have:
  - *mcap/FDV* — `mcap_usd` or `fdv_usd` a positive number (either one alone satisfies it; booleans do not count as numbers) → `"missing mcap/FDV"`
  - *liquidity* — `liquidity_usd` positive → `"missing liquidity"`
  - *context line* — non-empty `context_line` → `"missing context line"`
  - *safety status line* — non-empty `safety_line` → `"missing safety status line"`
  - *CA* — `mint` (or a `ca` key on dict cards): absent/blank → `"missing CA"`; present but failing the synthetic mint check → `"invalid CA"`
  - *copy-CA button* — a button containing the `📋 Copy CA` marker → `"missing copy-CA button"`
  - *total supply line* — non-empty `supply_line` → `"missing total supply line"`
  - *chain provenance* — `chain` one of `solana` / `base` / `ethereum` / `hyperliquid` *and* a non-empty `provenance_line`; an unknown chain is a provenance failure → `"missing chain provenance"`
  - *per-chain exit hint* — non-empty `exit_hint` → `"missing per-chain exit hint"`; a hint that names a *different* chain's venues/terms → `"exit hint does not match chain"`
  - *track button (picks only)* — cards with `is_pick=True` must carry a button containing the `📍 I bought this` marker → `"missing 📍 I bought this track button"`. Non-pick cards need no track button.
- `is_valid_ca(ca) -> bool` — the synthetic mint check: a string of 32–44 characters, all in the base58 alphabet (which excludes `0`, `O`, `I`, and `l`).
- `has_copy_button(card) -> bool` / `has_track_button(card) -> bool` — marker detection in the button list (a single string is treated as one button).
- `exit_hint_matches_chain(chain, exit_hint) -> bool` — the hint must mention a chain-specific word for its own chain (e.g. Raydium/SOL for solana, Aerodrome for base, gas/Uniswap for ethereum, mark price/HL for hyperliquid).
- `is_valid(card) -> bool` — `validate(card) == []`.
- `required_fields() -> tuple` — the nine always-required elements, by name.
- `summarize(card) -> dict` — `{"valid", "problems", "chain", "is_pick"}` for logging.

Problems are reported in a stable field order, and each single-element break produces exactly one problem naming that element — a bare-CA card produces the full list of everything else it is missing.

`demo.py` validates a complete pick card, then breaks one element at a time.

## Test suite

38 tests, all passing (`pytest -q`: "38 passed"). Covered: one fully-valid card fixture per chain (solana, base, ethereum, hyperliquid) plus a fully-valid *pick* card per chain and a valid `Card` dataclass instance; mcap-only and FDV-only cards both passing; CA rules at the boundaries (32 and 44 chars pass, 31 and 45 fail; each non-base58 character `0 O I l` rejected; non-strings rejected; the dict `ca` key accepted); one fixture per missing element — mcap/FDV, liquidity, context, safety, copy button, supply, provenance (blank line, blank chain, and unknown chain), exit hint, and the pick track button — each failing with *exactly* that one named problem; a wrong-chain exit hint failing distinctly; zero/negative/boolean amounts rejected as missing; a non-pick card needing no track button; an empty card reporting every element; multiple simultaneous problems all reported in order; and `summarize` / `required_fields` behaviour.

## Captured output

Verbatim output of `python demo.py`:

```
=== Card design lab (synthetic signal cards) ===

-- A complete pick card --
validate -> [] (empty means valid)

-- Cards each missing one element --
no safety line: ['missing safety status line']
no copy-CA button: ['missing copy-CA button']
no supply line: ['missing total supply line']
pick without the track button: ['missing 📍 I bought this track button']
a bare CA and nothing else: ['missing chain provenance', 'missing mcap/FDV', 'missing liquidity', 'missing context line', 'missing safety status line', 'missing copy-CA button', 'missing total supply line', 'missing per-chain exit hint']

-- An invalid CA --
validate -> ['invalid CA']

No bare CAs: a card that cannot be judged cannot be sent.
```

## What this mirrors in the live bot

This mirrors the live degen-lane card contract, which exists because the user gambles their own real money manually on these cards: every card carries mcap/liquidity/context, a safety status line, and a total-supply line, plus a one-tap 📋 Copy CA button under it — never a bare CA — with chain provenance (⛓ Solana / Base / ETH / HL) and a per-chain exit hint, since exiting a Hyperliquid listing has nothing in common with exiting a Solana runner. Pick cards additionally carry the 📍 "I bought this" tap, which journals the user's per-chain outcomes and drives the per-chain feed kill switches — a missing track button is not a cosmetic gap, it is lost measurement. A validator like this one is the fail-closed gate that keeps a half-built card from ever reaching the chat. It is a teaching/checking model of that contract only; it shares no code with the live bot.

## Self-exam

1. Why is a pick card without the 📍 track button a measurement failure, not just a UX gap — what downstream decision loses its data?
2. The validator accepts *either* mcap or FDV for the valuation element. When would a card legitimately have one but not the other, and when would showing both disagreeing be a warning sign?
3. Why does an *unknown chain* report as a provenance problem rather than getting its own problem string — and what would you check before adding a fifth chain?
4. A card's exit hint is present and well-written but names another chain's DEX. Why is that worse than a missing hint?
5. The CA check is purely syntactic (base58, 32–44 chars). Name two things it cannot tell you about a mint, and which other card element is supposed to carry that information instead.
6. Which single required element would you drop last if card space were cut in half, and why does the bare-CA fixture show what happens when *all* of them are dropped?
