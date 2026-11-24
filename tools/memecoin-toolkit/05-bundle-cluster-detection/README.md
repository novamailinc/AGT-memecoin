# Bundle Cluster Detection (lab)

Wallets funded from the same source are often one operator in many hats. This lab clusters wallets by shared funder with union-find, measures each cluster's share of token supply, and then asks the question that actually matters — is the cluster holding or distributing? — before deciding between PASS, FLAG, and VETO. All fixtures are synthetic; pure Python stdlib, no network.

## What it does

`bundle.py` groups wallets transitively: if A and B share funder F1, and B and C share funder F2, all three are one cluster. Each cluster's supply share is computed from a balances map. The verdict rule separates existence from behaviour: a cluster at ≥50% of supply vetoes on share alone; in the 20–50% band it vetoes only when its sampled wallets are net distributing — missing flow data degrades to FLAG, never to VETO (fail-closed against manufacturing a veto, and never a silent PASS either).

## Files

- `bundle.py` — the module:
  - `find_clusters(wallets: list[dict]) -> list[list[str]]` — union-find over `{"wallet": str, "funder": str}` rows (a `"funders"` list is also accepted). Malformed rows are skipped; funder-less wallets form singleton clusters; output is deterministically sorted.
  - `cluster_share(cluster, balances: dict, total_supply: float) -> float | None` — combined balance / supply × 100. Fail-closed: missing/non-positive/non-finite supply → `None`; wallets absent from `balances` contribute 0 rather than an invented balance, and non-numeric/non-finite balances are skipped.
  - `classify_behavior(flows) -> str` — `"distributing"` iff `distributing_count * 2 >= active_count` and `distributing_count > 0` (a wallet is distributing when outflows > inflows, active when in + out > 0); `"holding"` when there is activity but the rule is not met; `"unknown"` when flows is None/empty or no sampled wallet has activity.
  - `verdict_for_share(share_pct: float, behavior: str) -> tuple[str, str]` — the pure band rule: ≥50 → VETO ("extreme share"); 20–50 with distributing → VETO; 20–50 otherwise → FLAG; <20 → PASS.
  - `evaluate_cluster(cluster, balances, total_supply, flows=None) -> ClusterVerdict` — one cluster's verdict; unmeasurable share → `"UNKNOWN"`, which callers must treat as not-a-pass.
  - `evaluate(wallets, balances, total_supply, flows=None) -> ClusterVerdict` — clusters everything, scopes flows to each cluster, and returns the largest-share cluster's verdict; no wallets → `"UNKNOWN"`.
  - `cluster_shares(clusters, balances, total_supply) -> list` — every cluster's share, descending, with unmeasurable (`None`) shares last and first-member tie-breaks.
  - `summarize_clusters(wallets, balances, total_supply, flows=None) -> list[ClusterVerdict]` — a verdict for *every* cluster (not just the largest), largest share first, flows scoped per cluster exactly as in `evaluate`.
  - `worst_verdict(verdicts) -> str` — the most severe verdict among ClusterVerdicts or plain strings; severity VETO > UNKNOWN > FLAG > PASS; empty input or any unrecognised verdict → `"UNKNOWN"` (fail-closed).
  - `UnionFind` — path-compression union-find used by `find_clusters`.
- `demo.py` — a five-wallet, two-funder scenario shown three ways: behaviour unknown, distributing, and extreme share.
- `test_bundle.py` — the pytest suite below.

## Test suite

43 tests, all passing. Coverage: union-find transitivity across two funders (A–B via F1, B–C via F2 → one cluster); single-funder grouping; singleton clusters and skipped malformed rows; cluster share math including missing balances; bad supply → None; behaviour classification at exactly half distributing, as a minority, with zero distributors, in list form with long key names, and as `"unknown"` for None/empty/zero-activity flows; every verdict band (≥50 vetoes for all behaviours, 20–50 distributing vetoes, 20–50 unknown → FLAG not veto, <20 passes); an end-to-end `evaluate` picking the largest cluster; unknown verdicts for empty input and bad supply. Plus: `UnionFind` used directly (idempotent unions, disjoint sets staying separate, lazy add on `find`, a five-node union chain sharing one root); duplicate wallet rows deduplicated; plain-string wallets as singletons; `address`/`owner` keys and the `funded_by` key; tuple and single-string funder forms; empty/`None` input and empty funder strings ignored; deterministic cluster ordering (size, then first member); a five-wallet transitivity chain; share edges (empty cluster → 0.0 not `None`, `None` balances map → 0.0, string balances/supply parsed with junk balances skipped, negative/NaN/infinite supply → `None`); behaviour edges (2-of-4 distributing vs 2-of-5 holding, equal in/out is not distributing, outflow-only is, zero-activity wallets excluded from the denominator, malformed records and non-dict/list flow payloads → `"unknown"`, singular `inflow`/`outflow` key names); verdict pins at 49.999 and exactly 20.0 with holding behaviour; flow scoping proven for both dict and list flow forms (another cluster's selling cannot veto the winning cluster); empty-cluster PASS and first-cluster-wins on a share tie; `cluster_shares` ordering with `None` shares last; `summarize_clusters` covering all clusters in share order including a mid-band distributing VETO, and its empty/bad-supply results; `worst_verdict` precedence (UNKNOWN outranks FLAG) and its fail-closed empty/unrecognised cases.

## Captured output

Verbatim output of `python demo.py` (exit 0):

```
Bundle cluster lab -- one funder, many hats
====================================================
Scenario 1: 25% cluster, flow data missing (FLAG, not veto)
  cluster ['W1', 'W2', 'W3', 'W4']: share=25.0%
  cluster ['W5']: share=1.0%
  largest cluster behaviour: unknown
  verdict: FLAG -- elevated share, behaviour=unknown

Scenario 2: same 25% cluster, wallets net selling (VETO)
  cluster ['W1', 'W2', 'W3', 'W4']: share=25.0%
  cluster ['W5']: share=1.0%
  largest cluster behaviour: distributing
  verdict: VETO -- elevated share and distributing

Scenario 3: 55% cluster (VETO on extreme share alone)
  cluster ['W1', 'W2', 'W3', 'W4']: share=55.0%
  cluster ['W5']: share=1.0%
  largest cluster behaviour: unknown
  verdict: VETO -- extreme share

```

## What this mirrors in the live bot

The live bot's veto engine was changed on 2026-10-20 to exactly this shape: a bundle/insider cluster's existence no longer vetoes by itself — it vetoes at ≥50% supply share, or in the 20–50% band only when the cluster's sampled wallets are net selling; otherwise it is a flag. This lab reproduces that behaviour-classification rule (`distributing_count * 2 >= active_count`) and the principle that missing flow data can flag but never veto.

## Self-exam

1. Wallets A, B, C have funders F1, {F1, F2}, F2 respectively. How many clusters does `find_clusters` return, and why?
2. A cluster holds 30% of supply and its flow data failed to load. What verdict results, and why is it not a VETO?
3. Four sampled wallets have activity; exactly one is distributing. Holding or distributing — show the arithmetic.
4. A cluster at 55% of supply is provably holding. What verdict results, and which part of the rule causes it?
5. Why does `cluster_share` return `None` instead of 0.0 when total supply is missing?
