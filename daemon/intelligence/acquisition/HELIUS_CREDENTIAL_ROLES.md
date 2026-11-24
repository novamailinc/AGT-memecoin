# Helius credential roles, revised November 23, 2026

The owner revised the allocation: Helius is primary for routine RPC reads,
WebSockets and transaction reads, with selective parsed intelligence and relevant
events. Alchemy handles normal scanning. Chainstack complements supported recent
reads and independent verification. All remain free-only; no service activation
or spending approval follows from this document.

Numbered sources HELIUS_API_KEY_1, _2 and _3 are fixed sources sharing one
helius_main allowance. Legacy HELIUS_API_KEY is slot1 fallback only. Routine
requests use a deterministic method/params shard, never another key on denial.
Routine WSS uses slot1. Missing sources remain missing. Source identity is not
an independent billing account. Combined traffic qualification remains open.

Older disk/vault rotation paths remain compatibility code for historical modes,
not the revised routine facade. The old frozen phone-test tree is unqualified.
Do not infer current installation, valid keys, or permission from this file.
