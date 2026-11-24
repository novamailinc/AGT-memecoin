# Memecoin Toolkit — a Solana memecoin engineering study library

Original, from-scratch Python labs for the mechanics our paper memecoin bot
lives or dies by. Structurally modeled on a classic study-sprint format —
one numbered toolkit per topic, working stdlib-only code, a runnable demo,
a pinned pytest suite, and a README that proves every claim with captured
output — but every line here was written fresh for Solana memecoins.
Nothing in this library reads, imports, or modifies the live bot; every
fixture is synthetic. It is a reference and training ground: when a live
component's behavior is in question, the matching lab is the small,
deterministic version you can reason about first.

**Hard rules for the whole library:** pure Python 3.12 stdlib only · no
network calls · no keys or credentials · no live-bot files · synthetic
fixtures only · every claim proven by a test that actually ran.

**Verified 2026-10-20 (nitro expansion + gap-closing): 30 toolkits ·
19,303 Python LOC · 1,303 tests passed, 0 failed in one full
`run_all_tests.sh` run · 30/30 demos exit 0 · every toolkit ≥ 38 tests.**
Study layer: 30 teach-back notes and 31 exam files (30 per-toolkit exams
+ a 200-question final) under `study/`. Per-toolkit modules, LOC, and
results: see `MANIFEST.md`. Three gap-closing layers were added the same
day: a **real-data layer** (toolkits 08/12/13 read the live paper bot's
actual ledgers — see below), the **final exam was sat closed-book and
graded independently: 199/200 (99.5%), PASS**, and the bot's
**self-audit now runs this suite once per 24h** as a report-only check.

## Toolkit map

| # | Toolkit | What it proves | Mirrors / checks in the live bot |
|---|---------|---------------|----------------------------------|
| 01 | bonding-curve-math | pump.fun-style virtual-reserve curve: price, cost to buy X% of supply, impact, ~85 SOL graduation math | launch_scanner launch/graduation reasoning |
| 02 | amm-and-lp | Raydium/PumpSwap constant-product quotes, min-out guard, ≥90% burned/locked LP verdict, sandwich cost | `lp_lock.py`, locked-liq-scan, honest fills (`fills.py`) |
| 03 | token-safety-veto | Component-flag vetoes incl. Token-2022 traps; score is a floor, never a ranker; unknown fails closed | `instant_safety.py`, `veto.py`, degen-feed/smart-watcher safety gates |
| 04 | holder-concentration | Top-1/top-10 USER-holder math with pool/curve/escrow PDAs excluded; >50/>70 fail, >25/>55 flag | `instant_safety.py` on-chain concentration check |
| 05 | bundle-cluster-detection | Common-funder union-find clusters, share math, buy-vs-dump behavior; ≥50% veto, 20–50%+distributing veto, else flag | `bundle.py` + `veto.py` behavior-based bundle rule (2026-10-20) |
| 06 | rug-taxonomy | LP-pull, mint-and-dump, can't-sell, wash-skew, holder-crash detectors, each with trigger + clean fixtures | veto engine + safety floor design |
| 07 | exit-ladder-replay | No-look-ahead candle replay: user's ladder vs fast-harvest vs hold-to-stop on peaked-≥2x paths | `mirror.py` / `ceo.py` exit ladder (50%@2x, 25%@3x, 25% trail-30%, −50% stop, 24h cap) |
| 08 | statistical-validity-meme | Expectancy on −1x/+Nx outcomes, 500-strategy p-hacking demo, sample-size/power for lane verdicts, base rates | Lane verdict discipline (Oct 22 verdict, killswitch math) |
| 09 | backtesting-rigor-meme | Pre-registration freeze (tamper-proof), walk-forward with embargo, parameter-plateau, deflated expectancy, look-ahead injector | Exit-replay methodology; learner/verdict evidence standards |
| 10 | live-risk-controls-meme | Killswitch semantics (rolling expectancy, min trades, human-only re-arm), 25% cost gate on $1 shots, bankroll-exhaustion state machine with no reset | `killswitch.py`, `chain_costs.py` (Ethereum COST-BLOCKED at ~400%), `ceo.py` $100 account |
| 11 | smart-wallet-grading | Follower lag decay (+15/+50/+100%), pack = 1 actor, shrinkage grade weights, auto-mute (≥10 follows, avg ≤ −20%) | `follower_grader.py`, `smartwatch_filters.py`, smart-registry rotation |
| 12 | funnel-autopsy | Missed-runner classifier: never-met-bar / safety-unknown-lag / burned-one-shot-convergence / invented-ceiling-block / gate-correctly-blocked-rug | `scripts/runner-autopsy`; the DACITY and Doomsday misses as fixtures |
| 13 | post-trade-meme | $1-shot ledger analytics: realized multiples, WR, expectancy, peak-vs-realized leakage (round-trippers), rug rate, per-lane scorecard | Mirror/CEO ledgers, weekly review, lane-bandit inputs |
| 14 | token-lifecycle | Lifecycle phases BONDING → GRADUATED → RUNNER → FADING → DEAD (+UNKNOWN fail-closed), legal-transition table, time-in-phase over synthetic arcs | launch-scanner phase reasoning; why lanes hunt only the RUNNER phase |
| 15 | feed-data-quality | DexScreener-style pair selection: liquidity ranking, quote preference inside a 10% tie band, staleness refusal, >15% cross-pair disagreement = unverifiable | DexScreener pair picking in the live feeds (never just `pairs[0]`) |
| 16 | sniper-window-economics | Latency/age math: entry outcome vs detection delay on exponential launch paths; the 2–10 min copy window is structurally negative (demo: −52.1%) | Why degen_v2's 2–10 min sniper lane was structurally killed (2026-10-16) |
| 17 | deployer-forensics | Repeat-rug deployer fingerprinting: funding-source reuse, launch cadence, name reuse; UNKNOWN ≠ CLEAN; serial bar ≥3 launches + ≥2 rugs | veto engine's repeat-rug deployer check + deployer blacklist |
| 18 | wash-and-botshare | Wash-skew self-trade share, bot-share as a negative-only signal, fail-open fingerprints (missing data = unknown, never pass/veto) | `toxicity.py` fingerprints + veto wash check |
| 19 | liquidity-velocity | SOL inflow velocity as a graduation predictor — calibrated, and honestly below breakeven after the ~4% round-trip cost (+0.030 gross → −0.010 net) | spark-feed ignition metrics; the research finding behind them |
| 20 | copy-trading-economics | Cluster-copy lag simulation on seeded paths: leader +31.2% → 15s −6.2% → 15m −55.7%; delayed copy negative at every lag | Why smart money was demoted to watchlist-only (2026-10-20) |
| 21 | card-design | Signal-card completeness checker: mcap/FDV, liquidity, context, safety, CA, copy-CA button, supply, provenance, exit hint, 📍 track button on picks — missing any element fails | The live degen-lane card contract (no bare CAs, ever) |
| 22 | ledger-accounting | Atomic JSON ledger: tmp+rename saves, lock discipline, duplicate (ticket, kind) rejection, CLOSE-before-BUY rejection, torn-write recovery from backup | Mirror/CEO ledger hardening |
| 23 | daemon-supervision | Watchdog semantics on synthetic process tables: liveness probes, restart backoff budgets, exactly-one-instance invariant, the pgrep self-match trap as a regression fixture | `scripts/forex-watchdog`; the pattern-kill-your-own-shell lesson |
| 24 | cross-chain-costs | Per-chain cost model (SOL $0.001, Base $0.03, ETH $1.00, HL 0.035%/tx; worst case 4 txs) and the 25% gate at $1/$5/$25 notionals | `chain_costs.py` (Ethereum blocked at $1 = 400%, allowed at $25 = 16%) |
| 25 | lottery-sizing | Kelly sizing for −100%/+Nx distributions (lottery f* = 1/28), ruin-probability sims, why overbetting a +EV distribution still loses (f=0.50 growth ≈ −0.416) | The CEO account's flat $1-shot discipline |
| 26 | time-to-peak | Time-to-peak distributions with population tags that refuse to mix: peaked≥2x closes (median ~53 min) vs post-graduation peaks (median ~5 min) | Exit-replay findings; 24h time-stop reasoning |
| 27 | feed-resilience | 429 FloodGuard state machine (park all mutating calls until retry_after + 5s, cooldown survives restart), per-key rate buckets, seeded-jitter backoff, gzip lesson | Telegram `FloodGuard`; dual-Helius-key discipline |
| 28 | signal-refinery | Four-chains-one-destination bus: schema-v1 validation fail-closed, per-chain feed kill switches (auto-pause ≥10 taps & WR <20%, never auto-resume, Solana can NEVER be paused) | `signal_bus.py`, `feed_switches.json`, `tap_journal.py` |
| 29 | momentum-scanner | Trending-pool qualification funnel ($50k liq / $100k vol / +20% gain, NO ceiling, −15% 1h dump guard, 6h cooldown) with reject-reason counters | `momentum.py` after the 2026-10-19 ceiling removal; Doomsday regression fixture (+2540% must qualify) |
| 30 | verdict-protocol | Lane verdict procedure: pre-registered four live gates + decision matrix — FUND-ELIGIBLE / KEEP-FIXING / KEEP-MEASURING / KILL, with sample-size honesty (534 closes for a full verdict) | The Oct 22 lane verdict + Dec 20 CEO-mode verdict procedure |

## Study layer

- `study/notes/<toolkit>.md` — 30 teach-back notes (core ideas,
  derivations, worked example, trap log, [S]/[R]/[M] source flags).
- `study/exams/<toolkit>-exam.md` — 30 graded exams (20–22 questions
  each, answer key + rubric, pass bar ≥85%).
- `study/exams/final-exam-200.md` — cross-toolkit final: 200 questions
  spanning all 30 toolkits, full answer key, pass bar ≥170/200.
- `study/exams/final-exam-200-result.md` — the official record of the
  2026-10-20 sitting (see "Final exam" below).

## Real-data layer (added 2026-10-20)

Toolkits 08, 12 and 13 now carry `realdata.py` adapters that read the
live paper bot's actual records (read-only; the bot is never modified):
paper closes from `backtest_dataset.jsonl`, the mirror/CEO/steady/
stacker ledgers, and the launch-scanner judge journal. Adapter tests run
against small anonymized sample fixtures committed in each toolkit
(field-whitelisted; wallet/grade/registry data — the private moat — is
never included anywhere). Full run outputs live outside this repo
(`~/workspace/memecoin-toolkit-realdata/`); the repo carries only the
aggregate summary at `study/real-data-summary.md`.

Headline real numbers (450 closes: 383 paper + 41 mirror + 1 CEO +
18 steady + 7 stacker): overall expectancy **−0.323** per $1 shot,
win rate ≥1.5x **9.3%**, **22 round-trippers** (peaked ≥2x, closed <1x,
avg realized 0.191x), recorded rug rate **34.5%** on paper closes. The
autopsy over 305,799 judge records classifies 97.7% never-met-bar and
2.1% safety-unknown-lag — and honestly reports all 383 paper closes as
unclassifiable at close level, because no snapshots or gate-log reasons
were recorded for them. The labs' synthetic lessons and the real ledger
tell the same story; that agreement is the point of this layer.

## Final exam — sat and graded (2026-10-20)

The 200-question final was sat closed-book by an independent agent
working from a mechanically produced questions-only extract (the key
was stripped by line cut before the sitting; the candidate was barred
from `study/exams/` and the git history and confirmed it never opened
them), answering from the study notes, READMEs, modules, demos and
tests only, with 0 answers marked as guesses. A separate agent graded
strictly against the key under the exam's rubric: **199/200 = 99.5% —
PASS** (bar ≥170). The single miss was Q149 [22]: the candidate named
the ledger-corruption exception `LedgerCorruptError`; the defined
class is `CorruptLedgerError`. Full record:
`study/exams/final-exam-200-result.md`.

## Self-audit hook (added 2026-10-20)

The paper bot's `scripts/bot-self-audit` now includes a `toolkit_suite`
check: it runs `run_all_tests.sh` at most once per 24h (stamp file
`~/.forex-agent/toolkit_last_check.json`; the suite takes ~16–23s, so
it runs synchronously inside the audit). A failure is reported as an
unfixable-by-audit problem — the audit never edits toolkit code; a pass
is recorded in the audit's ok list. Verified live: first audit run
executed the suite (1,282 passed at the time), the next run skipped it
as not due.

## Run everything

```bash
bash ~/workspace/memecoin-toolkit/run_all_tests.sh
# or per toolkit:
cd ~/workspace/memecoin-toolkit/07-exit-ladder-replay
~/workspace/Forex-agent/.venv/bin/python -m pytest -q
~/workspace/Forex-agent/.venv/bin/python demo.py
```

## Honest notes

- Toolkit 07's trailing tranche arms at 2.0x (a documented modeling
  choice made so the −50% hard stop is reachable on a straight rug path);
  the live mirror trails leg C from the running peak. The headline result
  is unaffected: on the round-tripper path the user's ladder realizes
  1.58x where hold-to-stop realizes 0.50x — the proven leak is ladder
  *execution*, reproduced here in miniature.
- Toolkit 08 pins `required_n(35% vs 30% WR) = 534` closes — the honest
  price of distinguishing a real lane edge from noise. Most lane verdicts
  before ~500 closes are under-powered, and the lab shows why. Toolkit 30
  builds its whole decision matrix around that fact.
- Toolkit 26's prose describes the peaked≥2x population mean as "~74 min"
  (the live-replay finding it mirrors); its seeded fixture actually lands
  at 53.5 min median / 75.7 min mean. The two populations point is the
  lesson, not the decimal.
- These labs prove mechanics, not edge. A green suite means the math
  does what the README says — it says nothing about profitability, which
  only the paper ledgers can judge. The real-data layer above is where
  the two meet, and its verdict is the ledgers' own: expectancy −0.323
  across 450 real closes. No lab result in this repo should ever be
  quoted as evidence of a trading edge.
