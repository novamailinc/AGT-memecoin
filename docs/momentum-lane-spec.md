# Post-Graduation Momentum Lane — Pre-registered Design Spec

**Status:** DESIGN ONLY. Not built. Pre-registration date: 2026-10-16.
**Rule:** No code is written until this spec is reviewed. No criteria change
after the first paper trade without a new dated amendment.

## Why this lane exists

From the 2026-10-16 deep research:
- Launch sniping (2-10 min) is structurally dead for us: snipers buy in
  0.4-2s, we enter minutes late, delay makes EV 6.7x worse.
- Mean-reversion on dumps has no supporting evidence: 94% of dead tokens
  stay dead.
- Copy-trading as execution is adversarial: we're the exit liquidity.
- The one timescale we CAN compete on: hours, not seconds. Post-graduation
  momentum (tokens that graduated and are holding/growing) doesn't require
  sub-second infrastructure — it requires patience and exclusion discipline.

## Entry criteria (ALL must pass)

1. Token graduated from pump.fun bonding curve (on PumpSwap/Raydium).
2. Age since graduation: 30 min - 6 hours (not fresh, not stale).
3. Price above graduation price (holding gains, not dumping back).
4. Veto engine passes (bundle, wash, deployer, concentration) — hard gate.
5. Min liquidity: $20k (honest fills need depth).
6. Max 3 concurrent positions. $1 per shot (paper).

## Exit criteria

- Trailing stop 12% (research: tight trails beat wide).
- Time stop: 4 hours max hold (own data: 0.5-6h was the only profitable regime).
- Take profit: sell half at 2x, trail remainder.
- Hard stop: -30% (catastrophe guard, not primary exit).

## Pre-registered go/no-go gate

- **Sample:** 200 paper trades. No verdict before 200 closes.
- **GO:** Win rate >= 35% AND expectancy > 0 after honest fills.
- **NO-GO (kill):** Win rate < 30% OR expectancy <= 0 at 200 closes.
- **Amendment rule:** If criteria change mid-test, the counter resets to 0.
  No moving goalposts.

## What this lane does NOT do

- No sub-minute entries. No sniping.
- No copy-trading. No following smart wallets into entries.
- No mean-reversion. No buying dumps.
- No leverage. No perps.

## Measurement

- Every trade logged with: entry timestamp, graduation timestamp, veto
  results, fill prices (honest), exit reason, PnL net of costs.
- Weekly review: win rate, expectancy, exit reason distribution.
- Counterfactual: vetoed coins tracked for 24h (did we veto winners?).

## Kill criteria (lane dies if ANY hit)

1. 200 closes with win rate < 30% or expectancy <= 0.
2. Two consecutive weeks with zero qualifying entries (no opportunity).
3. Veto engine false-negative rate > 50% (vetoes missing obvious rugs).

---

*Pre-registered 2026-10-16. First paper trade starts the 200-count.*
*Amendments require a dated entry below with reason.*

## Amendments

### 2026-10-23 — Lane BUILT under the completion-build order

The momentum lane was built 2026-10-23 (`daemon/launch_scanner/momentum_lane.py` + `scripts/momentum-trader`, killswitch lane "momentum", ledger `~/.forex-agent/momentum_ledger.json`). Reason: the completion-build order directed building this lane under the task standard terms, which win where they differ from the 2026-10-16 text above.

Terms taken from the task standard set (differing from the 2026-10-16 text):
- **Exits:** the user's ladder — 50% @2x, 25% @3x, final 25% on a 30% trailing stop, −50% hard stop, 24h time stop — instead of this spec's trail-12% / −30% stop / 4h time stop / half-at-2x.
- **Verdict sample:** pre-registered at **100 closes** (killswitch {window:100, min_trades:100}; expectancy ≤ 0 at 100 closes halts) instead of 200 closes with the win-rate bands above.
- **Candidates:** the existing momentum feed (GeckoTerminal trending pools qualified by `momentum.MomentumScanner`, lowcap + established) instead of post-graduation tokens only — the feed's candidates are broader runners and normally carry no graduation data, so the graduated-only / 30min–6h-age / above-graduation-price filters apply only where a candidate actually carries graduation info, with per-entry applicability journaled in `entry_features.spec_filters`.
- **Liquidity floor:** **$25k** (standing runner rule, re-checked at entry from fresh tape with $50k mcap + positive momentum) instead of $20k.

Kept from this spec unchanged:
- **Veto engine as a HARD gate** (a veto blocks entry).
- **Counterfactual tracking of vetoed coins for 24h** (vetoed mints journaled with price-at-veto; 24h-later price stamped by the daemon loop).
- **Max 3 concurrent positions**, $1 per shot (paper), $100 paper bankroll, no cards.
