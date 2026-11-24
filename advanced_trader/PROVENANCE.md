# advanced_trader provenance

- Source: archive sent by the repo owner (Zenas Ayansipe) via WhatsApp, 2026-11-23 21:22 PST, captioned "This could help";
  integration authorized by the owner 21:25 ("Use it and fix it and add it to your project").
- Archive sha256: 1e7c429463bad7f54ea1a91a5a735431d38257c7f8d65ea4a9a06e82d3bf771b (gzip tar, 174KB).
- Quarantine copy: ~/quarantine/advanced-trader (pristine). This directory is the integrated, owner-authorized working copy.
- Static safety scan (2026-11-23): stdlib + intra-package imports only; no eval/exec/subprocess/socket/urllib; env reads
  limited to ADV_TRADER_* knobs and provider endpoint URLs; state limited to two small atomic JSON files.
- Isolated validation (env-scrubbed, offline, temp HOME): 9/9 script tests pass; mechanics checks pass; the synthetic
  backtest reports NEGATIVE expectancy (see validation.py header note and tests/advanced_trader_scripts/).
- Semantics per owner (2026-11-23): operational trades target DEMO mode = live market data + shared simulated $10,
  no real funds, no on-chain trades, closest-to-live parity. Paper/synthetic harness is TEST-ONLY.
- Numeric thresholds in config.py are the archive's proposed defaults, NOT owner-approved risk policy. Changing or
  adopting them for demo operation requires explicit grounding.
- This package decides only; it never executes, never holds keys, never makes network calls.
