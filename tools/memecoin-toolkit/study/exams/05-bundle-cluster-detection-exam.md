# Exam — Bundle Cluster Detection

- **Total questions:** 22
- **Points:** 1 per question (22 points total)
- **PASS BAR: ≥85% — minimum 19 of 22 correct** (0.85 × 22 = 18.7, rounded up)
- **Conditions:** Closed-book. Synthetic data only. Show working on compute-it questions.
- **Source:** Answerable from the `05-bundle-cluster-detection` README and `bundle.py` alone.

---

## QUESTIONS

**Q1 (MCQ).** Wallets are clustered by:
A. Buying the same token
B. Shared funder, transitively (union-find)
C. Similar balance size
D. Appearing in the same block

**Q2 (Compute).** Wallets A, B, C have funders: A → F1; B → {F1, F2}; C → F2. (a) How many clusters does `find_clusters` return for {A, B, C}, and which wallets are in the non-singleton cluster? (b) If D has no funder at all, what cluster is D in?

**Q3 (MCQ).** The verdict rule for a cluster at ≥50% of supply is:
A. FLAG — behaviour decides everything
B. VETO on share alone, for every behaviour (including provably holding)
C. PASS if holding
D. UNKNOWN

**Q4 (MCQ).** A cluster in the 20–50% band with behaviour `"unknown"` (flow data missing) gets:
A. VETO
B. PASS
C. FLAG — missing flow data degrades to FLAG, never to VETO and never to a silent PASS
D. UNKNOWN verdict

**Q5 (Compute).** Behaviour rule: distributing iff `distributing_count × 2 ≥ active_count` AND `distributing_count > 0`. Classify: (a) 4 active, 2 distributing; (b) 5 active, 2 distributing; (c) 4 active, 1 distributing; (d) 4 active, 0 distributing (all with activity); (e) no sampled wallet has any activity.

**Q6 (True/False + justification).** "A wallet whose inflows exactly equal its outflows counts as distributing." True or false? Justify. What about a wallet with outflows only?

**Q7 (MCQ).** `verdict_for_share(49.999, "holding")` and `verdict_for_share(20.0, "holding")` are, respectively:
A. PASS, PASS
B. FLAG, FLAG (20.0 is inside the 20–50 band — the band's lower edge is inclusive)
C. VETO, FLAG
D. FLAG, PASS

**Q8 (Compute).** Balances: W1 = 100, W2 = 100, W3 = 50 (one cluster), W4 = 10 (singleton); total supply = 1000. (a) Cluster share of {W1, W2, W3}. (b) The verdict if that cluster's behaviour is distributing. (c) The verdict if its behaviour is holding.

**Q9 (MCQ).** `cluster_share` returns `None` (not 0.0) when:
A. A wallet is absent from `balances`
B. Total supply is missing, non-positive, or non-finite
C. The cluster is empty
D. Balances are strings that parse

**Q10 (Short answer).** Why does `cluster_share` return `None` instead of 0.0 when total supply is missing — what wrong conclusion would 0.0 invite?

**Q11 (MCQ).** Wallets absent from the `balances` map contribute:
A. An invented average balance
B. 0 to the cluster share
C. They make the share `None`
D. They are removed from the cluster

**Q12 (True/False + justification).** "Another cluster's net selling can veto the largest cluster in `evaluate`." True or false? Justify (flow scoping).

**Q13 (MCQ).** `evaluate` with no wallets at all returns verdict:
A. PASS
B. FLAG
C. `"UNKNOWN"` — callers must treat it as not-a-pass
D. It raises

**Q14 (MCQ).** Severity order used by `worst_verdict`, most severe first:
A. VETO > FLAG > UNKNOWN > PASS
B. UNKNOWN > VETO > FLAG > PASS
C. VETO > UNKNOWN > FLAG > PASS
D. FLAG > VETO > UNKNOWN > PASS

**Q15 (MCQ).** `worst_verdict([])` (empty input) or any unrecognised verdict string gives:
A. PASS
B. `"UNKNOWN"` (fail-closed)
C. FLAG
D. It raises

**Q16 (Short answer).** The design principle "missing flow data can flag but never veto" — state the two failures it simultaneously avoids.

**Q17 (Compute).** Demo scenarios: cluster {W1..W4} share 25.0%, singleton 1.0%. State the overall `evaluate` verdict for: (a) behaviour unknown; (b) behaviour distributing; (c) the same wallets rebalanced so the big cluster holds 55.0%, behaviour unknown.

**Q18 (True/False + justification).** "Malformed wallet rows in `find_clusters` raise an error and abort clustering." True or false? Justify.

**Q19 (MCQ).** `cluster_shares` orders clusters:
A. By size, then alphabetically
B. By share descending, with unmeasurable (`None`) shares last and first-member tie-breaks
C. In input order
D. By share ascending

**Q20 (MCQ).** Zero-activity wallets in `classify_behavior` are:
A. Counted as distributing
B. Counted as holding
C. Excluded from the denominator (active means in + out > 0)
D. They make behaviour `"distributing"`

**Q21 (Short answer).** This lab mirrors a live-bot change dated 2026-10-20. State the change in one sentence.

**Q22 (Compute).** An empty cluster's share is: (a) `None` or (b) 0.0 — pick one and state `verdict_for_share` for that share with any behaviour. Also: a `None` balances map gives a non-empty cluster what share?

---

## ANSWER KEY

- **Q1: B** — Shared funder, closed transitively by union-find.
- **Q2: (a) One cluster for {A, B, C}** (plus none other) — A–B share F1, B–C share F2, transitivity merges all three. **(b) A singleton cluster containing only D** — funder-less wallets form singletons.
- **Q3: B** — ≥50% vetoes on extreme share alone, for all behaviours.
- **Q4: C** — 20–50 with unknown/holding behaviour → FLAG; only distributing upgrades the band to VETO.
- **Q5: (a) Distributing** (2×2 = 4 ≥ 4, and > 0). **(b) Holding** (2×2 = 4 < 5). **(c) Holding** (1×2 = 2 < 4). **(d) Holding** (0 distributors — activity exists but the rule is not met; count > 0 fails). **(e) `"unknown"`** (no activity in any sampled wallet).
- **Q6: False** — Distributing requires outflows STRICTLY greater than inflows; equal in/out is not distributing. Outflow-only (out > 0 = in) IS distributing.
- **Q7: B** — Both lie in the 20–50 band; with holding behaviour both are FLAG. (49.999 is pinned in tests; 20.0 is the inclusive band edge.)
- **Q8: (a) 25.0%** ((100+100+50)/1000 × 100). **(b) VETO** (20–50 band + distributing). **(c) FLAG** (20–50 band, not distributing).
- **Q9: B** — Unmeasurable supply → `None`. (Absent wallets contribute 0 → A is wrong; an empty cluster → 0.0; parseable strings parse.)
- **Q10:** 0.0 would assert "this cluster holds nothing" — a measured PASS-grade fact — when in fact nothing was measured. `None` forces callers into the UNKNOWN / not-a-pass path instead of silently clearing a possibly-huge cluster.
- **Q11: B** — Absent wallets contribute 0 rather than an invented balance; non-numeric/non-finite balances are likewise skipped.
- **Q12: False** — Flows are scoped to each cluster in both `evaluate` and `summarize_clusters`; another cluster's selling cannot veto the winning (largest-share) cluster.
- **Q13: C** — Empty input → `"UNKNOWN"`, treated as not-a-pass.
- **Q14: C** — VETO > UNKNOWN > FLAG > PASS.
- **Q15: B** — Empty or unrecognised → `"UNKNOWN"`, fail-closed.
- **Q16:** It avoids (1) manufacturing a VETO from missing evidence (fail-closed against a false veto) and (2) silently PASSing an elevated-share cluster just because behaviour data failed to load.
- **Q17: (a) FLAG** — elevated share, behaviour unknown. **(b) VETO** — elevated share and distributing. **(c) VETO** — extreme share alone.
- **Q18: False** — Malformed rows are skipped; clustering proceeds over the well-formed rows (duplicates deduplicated; plain-string wallets become singletons).
- **Q19: B** — Share descending, `None` shares last.
- **Q20: C** — Only active wallets (in + out > 0) enter the denominator; if none are active the behaviour is `"unknown"`.
- **Q21:** The live veto engine stopped vetoing on a bundle/insider cluster's EXISTENCE alone: it now vetoes only at ≥50% supply share, or in the 20–50% band when sampled wallets are net selling; otherwise the cluster is a flag.
- **Q22: (b) 0.0** — an empty cluster has a measurable zero share, and `verdict_for_share(0.0, anything)` = PASS (< 20 band). A `None` balances map likewise yields 0.0 for a non-empty cluster (every wallet absent → contributes 0).

---

## GRADING RUBRIC

- **MCQ (Q1, Q3, Q4, Q7, Q9, Q11, Q13–Q15, Q19, Q20):** 1 point for the correct letter only.
- **True/False + justification (Q6, Q12, Q18):** 1 point only if verdict AND justification are both correct.
- **Short answer (Q10, Q16, Q21):** 1 point if the key mechanism is stated.
- **Compute-it (Q2, Q5, Q8, Q17, Q22):** 1 point per question; ALL sub-parts must be correct — the arithmetic in Q5 and the band edges in Q7/Q17 are the point, no tolerance.
- **Pass rule:** ≥85% → **at least 19 of 22 correct** passes.
- **On a fail:** Re-study the toolkit note/README and `bundle.py` (the band rule and the behaviour inequality especially), then retake the full exam.
