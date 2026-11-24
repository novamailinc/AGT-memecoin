# Signal Refinery (lab)

A from-scratch, pure-stdlib lab for the refinery pattern: four chains (Solana, Base, Ethereum, Hyperliquid) feeding *one* destination — a single signal lane — through a validated bus, with per-chain kill switches driven by the user's own tap journal. Everything is synthetic: records are plain dicts, outcomes are hand-built fixtures, and every rejection carries its reasons.

## What it does

Module `refinery.py` — constants `CHAINS = ("solana", "base", "ethereum", "hyperliquid")`, `SCHEMA_VERSION = 1`, `SOURCE_CODES` mapping short codes to feed names (`df` degen-feed, `sw` smart-watcher, `br` base-runner-feed, `er` eth-runner-feed, `hl` hl-listing-watch, `ll` locked-liq-scan, `hb` heartbeat), and the tap thresholds `WIN_PEAK_MULT = 2.0`, `AUTO_PAUSE_MIN_TAPS = 10`, `AUTO_PAUSE_MAX_WIN_RATE = 0.20`:

- `validate_record(rec) -> (bool, list[str])` — schema-v1 validation. Fail-closed: a non-dict, any missing field of `schema, chain, source, mint, ts, score`, a wrong schema version, an **unknown chain**, an unknown source code, a short/non-string mint, a non-positive or boolean `ts`, a score outside `[0, 100]`, or a source/chain mismatch (`br` must be Base, `er` Ethereum, `hl` Hyperliquid) each reject with a named reason (`missing:<field>`, `unknown_chain`, …); nothing is silently coerced.
- `source_name(code) -> str` / `code_for_source(name) -> str` — the source-code map in both directions; unknown codes/names raise `ValueError`.
- `TapOutcome(chain, mint, closed, peak_mult)` — one journal entry. Fail-closed: unknown chain, empty mint, a closed outcome without a `peak_mult`, or a negative multiple raises `ValueError`. `is_win` is true iff closed with `peak_mult >= 2.0`; `is_loss` iff closed below 2.0; an open outcome is neither.
- `TapJournal()` — `add(outcome)`, `closed_for_chain(chain)`, `tap_count(chain)`, `win_rate(chain) -> float | None` (`None` with no closes — never a fake 0.0). Wrong types and unknown chains raise.
- `FeedSwitches()` — per-chain pause state. `pause(chain, by="human") -> bool`, `is_paused(chain) -> bool`, `resume(chain, by="human") -> bool`. Hard rules: **Solana can never be paused** — the attempt is refused, returns `False`, and is recorded in `flags`; **there is no auto-resume** — `resume(by="auto")` is refused and flagged, only `by="human"` resumes. `evaluate_auto_pause(journal) -> list[str]` pauses every non-Solana chain with ≥10 closed taps and win rate strictly <20% (exactly 20% does *not* pause), and is idempotent. `publish_allowed(rec) -> (bool, list[str])` validates the record, then refuses paused chains with `["chain_paused"]`.

`demo.py` validates a good and a garbage record, builds a losing Base journal until the auto-pause fires, then shows the Solana pause refusal and the human-only resume.

## Test suite

38 tests, all passing (`pytest -q`: "38 passed"). Covered: the happy-path record and one valid record per chain; fail-closed validation for non-dict input, each missing field, bad schema version, unknown chain, unknown source, bad mint/ts/score (including boolean traps and the 0/100 score boundaries), and source/chain mismatch; the source-code map round-tripping every code and rejecting unknowns; the win definition at the exact 2.0 boundary (2.0 wins, 1.99 loses, open is neither) and `TapOutcome` validation; journal tap counts, win rates, the empty-journal `None`, and wrong-type rejection; switches starting unpaused, pause + human resume, **Solana pause refused and flagged**, **auto-resume refused and flagged**, unknown-chain raises; the auto-pause matrix (fires at 11 taps / ~9% WR, silent at 9 taps, silent at 30% WR, silent at exactly 20% WR, never touches Solana, idempotent); and `publish_allowed` happy / paused / invalid paths, with Solana publishing unaffected by other chains' pauses.

## Captured output

Verbatim output of `python demo.py`:

```
=== Signal refinery lab (four chains, one destination) ===
Valid record: (True, [])
Invalid record: (False, ['unknown_chain', 'unknown_source', 'bad_mint', 'bad_ts', 'bad_score'])
Source codes: br=base-runner-feed hl=hl-listing-watch ll=locked-liq-scan
Base taps closed=11 win_rate=0.091
Auto-pause evaluation: ['base'] base_paused=True
Solana pause attempt: False paused=False flags=['SOLANA_PAUSE_REFUSED by=human']
Auto-resume attempt: False still_paused=True
Human resume: True paused=False
```

## What this mirrors in the live bot

This mirrors the live Signal Refinery: `daemon/launch_scanner/signal_bus.py` carries schema-tagged records from every chain's feed (degen-feed, smart-watcher, the Base and Ethereum runner feeds, the Hyperliquid listing watcher, the locked-liquidity scanner) into the one degen lane the user actually watches, and a malformed or unknown-chain record must die at the bus, not on the user's phone. The switches mirror `feed_switches.json`: the user's own "📍 I bought this" taps are journaled per chain (`tap_journal.py`), a chain whose tapped calls close below a 20% win rate at ≥10 taps pauses itself, resuming is a human decision only, and Solana — the home lane — is never auto-paused. A "win" in the journal means the position closed having peaked at ≥2.0×, the same bar the user's exit ladder is built around. It is a teaching/checking model only; it shares no code with the live bot.

## Self-exam

1. Why does the validator reject an unknown chain instead of passing the record through with a warning — what is the cost asymmetry at the destination?
2. The auto-pause needs *both* ≥10 taps *and* win rate <20%. What false pause does each condition prevent on its own?
3. A chain sits at exactly 2 wins from 10 taps (20%). It does not pause. Defend that boundary — and then argue the other side.
4. Why can Solana never be paused by this machinery, even with a 0% tap win rate — and why is a refused attempt *flagged* rather than silently ignored?
5. Why is there deliberately no auto-resume, even after the journal later shows winning taps on other chains?
6. An outcome that is still open counts as neither win nor loss. What verdict distortion would counting open positions as losses introduce for a young chain?
7. The `br` source on a `solana` record is rejected as a mismatch even though both the chain and the source are individually valid. What class of bug is that check hunting?
