# Temporal Edge Validator

Paper-only honesty machine for "time window" trading edges (the claim behind
viral "my bot found BTC windows with 65% edge" posts). Tests whether specific
time windows really beat the market — on BTC, SOL, ETH, or any symbol with
hourly history.

**No real money. No exchange account. No API keys.** Data comes from Binance's
free public market-data endpoint. It cannot place a trade by design.

## The honesty machinery

1. **Train/test split (70/30, chronological).** Discovery happens only on
   train; validation only on frozen test data. No peeking.
2. **Realistic costs.** Every hypothetical trade pays 0.2% round-trip.
3. **Bonferroni correction** over all 62 candidates (24 hours × long/short +
   7 weekdays × long/short). Testing 62 windows guarantees a lucky-looking
   winner — the correction holds the line.
4. **Permutation test (500 shuffles)** on gross returns: shows what the "best"
   window looks like under pure noise, so a lucky train result can't fool us.
5. **BTC-beta strip** for SOL/ETH: edges measured on residuals, so BTC's
   shadow isn't double-counted as a SOL/ETH edge.
6. **Cost-drag stat**: reports what the average trade nets after costs —
   usually reliably negative, which is itself a finding.
7. **Kelly sizing** from measured (not assumed) win rate and payoff.
   No edge → Kelly says bet 0.

## Files

| File | What it does |
|---|---|
| `fetch_data.py` | Pulls hourly klines into `data/klines.db` (SQLite) |
| `validator.py` | The backtest engine → `results_<SYM>.json` |
| `paper_forward.py` | Out-of-sample tracker: settles last closed candle hourly against frozen windows → `data/paper_trades.db` |
| `report.py` | Human-readable report, `--telegram` to send it |
| `selftest.py` | Proves the engine finds an injected edge and rejects pure noise |

## Usage

```bash
python3 fetch_data.py --symbols=BTCUSDT,SOLUSDT,ETHUSDT --years=2
python3 validator.py --symbol=BTCUSDT
python3 selftest.py
python3 report.py --telegram
# freeze discoveries for forward tracking (empty set if none found):
python3 paper_forward.py --freeze
# then hourly: python3 paper_forward.py   (cron)
```

## Results — 2026-09-30 (2y hourly, 18,000 candles/symbol)

| Symbol | Best gross window | vs noise | Cost drag/trade | Discovered |
|---|---|---|---|---|
| BTC | \|t\|=2.75 | below noise p95 (3.23) | −0.20% (t=−44.9) | 0 |
| SOL (BTC-stripped) | \|t\|=2.21 | below noise p95 (3.32) | −0.20% (t=−39.0) | 0 |
| ETH (BTC-stripped) | \|t\|=1.63 | worse than typical noise | −0.20% (t=−50.3) | 0 |

Verdict: no temporal edge on any major. The best windows are
indistinguishable from noise, and after costs the average hourly trade is a
reliable loser. Kelly: bet 0. Re-run monthly; if anything ever qualifies,
`paper_forward.py --freeze` + an hourly cron tracks it out-of-sample.

## Tier 1 — harder honesty (2026-10-01, user-approved)

Four upgrades, each in its own module, all stdlib-only:

1. `walkforward.py` — 5 rolling folds (50% train / next 10% test). A window
   qualifies only if discovered (Bonferroni-significant, in-sample) in ≥3/5
   training folds AND positive pooled out-of-sample expectancy AND
   Bonferroni-significant pooled OOS. For SOL/ETH, BTC beta is fit on each
   fold's training data only, then frozen for that fold's test data.
2. `deflated_sharpe.py` — Deflated Sharpe Ratio (Bailey & López de Prado):
   adjusts the observed Sharpe for the number of trials and the variance of
   trial Sharpes. DSR < 0.95 = no edge.
3. `blockbootstrap.py` — null distribution of max-|t| built by reshuffling
   24h circular blocks (500 perms, seeded), preserving autocorrelation that
   plain shuffles destroy.
4. `costs.py` — Corwin–Schultz spread estimator on actual OHLC + 0.1%
   taker fee per side → measured per-hour round-trip cost, replacing the
   flat 0.2% assumption.

`tier1.py` runs all four on BTC/SOL/ETH. `tier1_selftest.py` — 24 property
tests, all passing (injected edges detected, pure noise rejected, seeded
bootstrap deterministic).

### Tier 1 results (2026-10-01)

| Symbol | Walk-fwd qualified | DSR | Block-null best\|t\| vs p95 | Avg measured cost | Profitable windows after costs |
|---|---|---|---|---|---|
| BTC | 0 | 0.004 | 2.75 vs 3.19 | 0.21% | 0 |
| SOL | 0 | 0.122 | 2.21 vs 3.28 | 0.22% | 0 |
| ETH | 0 | 0.001 | 1.63 vs 3.25 | 0.22% | 0 |

(The first version of the cost-sensitivity check ranked by |t| regardless of
sign and "found" huge t-stats — those were significant *losers* (cost drag),
not winners. Fixed to count only positive-expectancy windows: zero.)

Verdict: every Tier 1 lens agrees — no temporal edge on the majors.

## Tier 2 — wider net (2026-10-01, user-approved)

- More symbols: XRP/DOGE/ADA/BNB, 2y hourly, BTC-beta stripped.
- Longer history: BTC 4y hourly (36,006 candles), walk-forward + DSR +
  300-perm block bootstrap.
- Regime conditioning: 124 candidates (hourly/weekday × long/short ×
  calm/volatile, regime = trailing-30d vol above/below median).
- 15-minute search: 192 quarter-hour candidates on 2y of 15m BTC data
  (71,000 candles).
- Cross-venue: 2y hourly BTC/USD from Coinbase (17,527 candles, venue
  correlation 0.9995 vs Binance). (Kraken's public OHLC endpoint only serves
  the most recent ~720 hourly candles — no deep history — so Coinbase was
  used instead.)

`tier2.py` runs it all → `results_tier2.json`. Fetchers: `extend_history.py`
(extends Binance history backward; `backfill()` can't), `fetch_coinbase.py`.

### Tier 2 results (2026-10-01)

| Check | Discovered | Notes |
|---|---|---|
| XRP 2y | 0 | DSR 0.107, best gross \|t\| at 94th pct of null |
| DOGE 2y | 0 | DSR 0.120 |
| ADA 2y | 0 | DSR 0.093 |
| BNB 2y | 0 | DSR 0.014 |
| BTC 4y | 0 | WF 0/5 folds, DSR 0.000, block pct 0.770 |
| Regime 124 cands | 0 | DSR 0.233 (highest seen, still far from 0.95) |
| 15m 192 cands | 0 | DSR 0.000 |
| Coinbase cross-venue | 0 | best\|t\| 2.43 vs null p95 3.18 |

Overall verdict (Tiers 0–2): no time-window edge anywhere we looked — 7
symbols, 4 years, 15-minute bars, calm/volatile regimes, two exchanges.
Cost drag (−0.20 to −0.22%/trade, t ≈ −28 to −51) is the only reliable
"edge", and it points down. Kelly: bet 0.

## Reproducing

```bash
python3 tier1_selftest.py     # 24 tests, must all pass
python3 tier1.py              # Tier 1 on BTC/SOL/ETH -> results_tier1_*.json
python3 tier2.py              # Tier 2 (needs datasets below) -> results_tier2.json
```

Datasets (all in `data/klines.db`, Binance public data unless noted):
`backfill()` in fetch_data.py, `extend_history.py --years=4` for deep
history, `fetch_coinbase.py --years=2` for the cross-venue set.
