# Bundle Cluster Detection — study notes

## Core ideas (teach-back: explain it as if teaching a smart beginner; the actual concepts from THIS toolkit, with its real numbers/thresholds)

Ten wallets can be one person. The tell is funding: wallets that
were all funded from the same source are often a single operator
wearing many hats — a "bundle" or insider cluster. This lab in
`bundle.py` clusters wallets by shared funder, measures each
cluster's share of token supply, and then asks the question the
whole design turns on: is the cluster holding, or distributing?
[S]

Clustering is transitive, via union-find. If A and B share funder
F1, and B and C share funder F2, then A, B, and C are one cluster
— even though A and C share no funder directly. Wallets arrive as
`{"wallet": ..., "funder": ...}` rows (a `"funders"` list and
several key aliases are also accepted); malformed rows are
skipped, funder-less wallets form singleton clusters, and output
is deterministically sorted [S].

Then the lab's central distinction: **existence is not behaviour.**
A cluster merely existing — even a big one — is not proof of
harm. Airdrop farmers, market makers, and one careful buyer with
many wallets all cluster. What justifies the strongest verdict
is either overwhelming share or share *plus* dumping. The band
rule encodes exactly that [S]:

- Share ≥ 50% → VETO on share alone ("extreme share").
- Share in 20–50% → VETO only if the cluster is distributing;
  otherwise FLAG.
- Share < 20% → PASS.

Behaviour is classified from sampled wallet flows: a wallet is
distributing when outflows > inflows, active when in + out > 0.
The cluster counts as `"distributing"` iff at least half of its
active sampled wallets are distributing and at least one is:
`distributing_count * 2 >= active_count` and
`distributing_count > 0`. Otherwise `"holding"` when there is
activity, `"unknown"` when flows are missing, empty, or show no
activity at all [S].

The fail-closed subtlety here runs in *both* directions, and it
is the lesson of the lab: missing flow data degrades a mid-band
cluster to FLAG — never to VETO (you may not manufacture a veto
from absent evidence) and never to a silent PASS either (a 25%
cluster you cannot observe is still flagged) [S].

## Key derivations & formulas (the math the toolkit implements, shown step by step where non-trivial)

Union-find clustering [S]:

    union(wallet, funder) for every funding edge
    clusters = wallets grouped by find(wallet) root
    (path compression; unions idempotent; lazy add on find)

Cluster share [S]:

    share % = 100 × Σ balances[wallet in cluster] / total_supply

Fail-closed: missing / non-positive / non-finite supply → None
(an unmeasurable share, not 0.0). Wallets absent from the
balances map contribute 0 rather than an invented balance;
non-numeric or non-finite balances are skipped. An empty cluster
has share exactly 0.0 [S].

Behaviour classification [S]:

    active        = wallets with inflows + outflows > 0
    distributing  = wallets with outflows > inflows
                    (equal in/out is NOT distributing;
                     outflow-only IS)
    behaviour = "distributing" if d > 0 and 2d ≥ a
                "holding"      if a > 0 but rule not met
                "unknown"      if a == 0 or flows missing

Verdict bands — `verdict_for_share(share, behaviour)` [S]:

    share ≥ 50            → VETO   (any behaviour, incl. holding)
    20 ≤ share < 50:
        distributing      → VETO
        holding / unknown → FLAG
    share < 20            → PASS
    share is None         → UNKNOWN (callers: not-a-pass)

Aggregation: `evaluate` clusters everything, scopes flows to
each cluster (another cluster's selling cannot veto this one —
pinned by tests in both dict and list flow forms), and returns
the largest-share cluster's verdict. `summarize_clusters`
returns a verdict for *every* cluster, largest first.
`worst_verdict` ranks severity VETO > UNKNOWN > FLAG > PASS,
and returns UNKNOWN on empty or unrecognised input [S].

## Worked example (concrete numbers taken from the toolkit's demo/fixtures)

Verbatim from `demo.py` — five wallets, two funders [S]:

Scenario 1 — 25% cluster, flow data missing:

- Cluster ['W1','W2','W3','W4']: share 25.0%; ['W5']: 1.0%.
- Behaviour: unknown → verdict FLAG ("elevated share,
  behaviour=unknown"). Not a veto: the evidence is absent.

Scenario 2 — same 25% cluster, wallets net selling:

- Same shares; behaviour: distributing → verdict VETO
  ("elevated share and distributing").
- Same wallets, same share — only the behaviour flips it.

Scenario 3 — 55% cluster:

- Share 55.0% → verdict VETO ("extreme share"), behaviour
  unknown and irrelevant: ≥50% vetoes on share alone.

Arithmetic check from the test suite [S]: four sampled wallets
have activity, exactly one distributing → 2×1 = 2 < 4, so the
cluster is holding, not distributing. Two of four distributing
→ 2×2 ≥ 4 → distributing. Two of five → 4 < 5 → holding.
Boundary pins: share 49.999 with distributing → VETO via the
band; exactly 20.0 with holding → FLAG [S].

## Trap log (numbered T1, T2, … — the failure modes/misconceptions this toolkit exists to catch, each 1–3 lines)

- T1. Vetoing on existence. A shared funder proves coordination
  of funding, not dumping. The 20–50% band needs behaviour.
- T2. The reverse error: passing what you cannot observe. A
  mid-band cluster with missing flows is FLAG, never PASS.
- T3. Non-transitive clustering. Checking only direct funder
  matches splits one operator into fake "independent" groups;
  union-find exists precisely to chain A–B–C.
- T4. Cross-cluster contamination. Flows must be scoped per
  cluster; another cluster's selling must not veto this one.
- T5. Reading share None as 0%. Missing supply makes the share
  unmeasurable → UNKNOWN verdict, not a tiny safe share.
- T6. Counting quiet wallets in the behaviour denominator.
  Zero-activity wallets are excluded; otherwise dormant hats
  dilute a distributing majority into "holding".
- T7. Calling equal in/out "distributing". The rule is strict
  outflows > inflows per wallet; net-zero flow is not dumping.
- T8. Judging only the largest cluster. `evaluate` returns the
  largest-share verdict, but a smaller distributing mid-band
  cluster is visible only via `summarize_clusters`.

## Source flags (mark key claims [S] = proven in the toolkit's own tests/source, [R] = reasoned/derived here, [M] = mirrors live-bot practice per the README)

- Union-find transitivity, share math and None-on-bad-supply,
  behaviour rule `2d ≥ a`, all verdict bands incl. 49.999 /
  20.0 pins, per-cluster flow scoping: [S] — 43 tests.
- Demo scenarios (25% FLAG → VETO on behaviour; 55% VETO):
  [S] — verbatim captured output.
- The live veto engine's 2026-10-20 change to this exact shape
  (≥50% vetoes; 20–50% vetoes only when net selling; else
  flag): [M] — per README; lab shares no code with the bot.

## One-paragraph verdict (what this toolkit proves and what it does NOT prove)

This toolkit proves the existence-vs-behaviour rule can be
implemented precisely: transitive funder clustering, fail-closed
share measurement, a half-of-active-wallets behaviour test, and
band verdicts that refuse to veto on missing evidence — all
pinned across 43 tests including scoping and tie-breaking [S].
It does not prove any real cluster is one operator: shared
funders also arise from exchanges and public faucets, flow
samples can misrepresent a cluster, and on synthetic fixtures
the lab validates the *decision rule*, not the quality of
real-world funder/flow data fed into it [R].
