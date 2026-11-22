#!/usr/bin/env python3
"""X/Twitter feed parser tests (Phase 3, 2026-11-17).

Covers daemon/launch_scanner/x_feed.py: mint extraction (valid base58
accepted, too-short/invalid strings rejected), cashtag extraction,
dedupe (first post wins, duplicates counted), malformed posts never
raising, timestamp parsing (ISO + epoch + unknown -> 0), and the bus
source code "xf" registered in signal_bus.TRACK_SOURCE_CODES.

Also runs the real fixture (tests/data/x_feed_posts.json) through the
parser and checks the expected candidate counts. No network, no live
bus, no credentials. Plain script — run directly, not via pytest.
"""
import json
import os
import sys

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

import x_feed  # noqa: E402
from daemon.launch_scanner import signal_bus  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


MINT1 = "wA2FDENLEPW41zpPKKXn8ZoSXC9Ljjbezb9gGvcBegmm"  # 44 base58
MINT2 = "YU4wkqDhca683MkdCHBjHdZENf22HfmS"              # 32 base58

# ---------------- mint extraction ----------------
check("mint: valid 44-char base58 accepted",
      x_feed.extract_mints("CA: " + MINT1) == [MINT1])
check("mint: valid 32-char base58 accepted (shortest)",
      x_feed.extract_mints("CA: " + MINT2) == [MINT2])
check("mint: too-short string rejected",
      x_feed.extract_mints("check abc123 out") == [])
check("mint: 31-char string rejected",
      x_feed.extract_mints("x" * 31) == [])
check("mint: 45-char base58 run rejected outright (not truncated)",
      x_feed.extract_mints("1" * 45) == [])
check("mint: string with 0/O/I/l rejected",
      x_feed.extract_mints(
          "0OIl0OIl0OIl0OIl0OIl0OIl0OIl0OIl0OIl0OIl") == [])
check("mint: non-string text returns []",
      x_feed.extract_mints(None) == []
      and x_feed.extract_mints(12345) == [])
check("mint: two mints in one post both extracted, in order",
      x_feed.extract_mints("a %s b %s" % (MINT1, MINT2))
      == [MINT1, MINT2])

# ---------------- cashtag extraction ----------------
check("cashtag: $TICKER extracted and upper-cased",
      x_feed.extract_cashtags("long $pumpit now") == ["PUMPIT"])
check("cashtag: multiple cashtags in order, deduped",
      x_feed.extract_cashtags("$AAA $bbb $AAA") == ["AAA", "BBB"])
check("cashtag: $123 is not a ticker",
      x_feed.extract_cashtags("price $123 up") == [])
check("cashtag: non-string text returns []",
      x_feed.extract_cashtags(None) == [])

# ---------------- timestamp parsing ----------------
check("ts: ISO-8601 Z parsed",
      abs(x_feed.parse_created_at("2026-11-17T10:15:00Z")
          - 1794910500.0) < 2)
check("ts: epoch int passed through",
      x_feed.parse_created_at(1794890100) == 1794890100.0)
check("ts: epoch string passed through",
      x_feed.parse_created_at("1794890100") == 1794890100.0)
check("ts: missing/unknown -> 0",
      x_feed.parse_created_at(None) == 0.0
      and x_feed.parse_created_at("") == 0.0
      and x_feed.parse_created_at("not-a-date") == 0.0
      and x_feed.parse_created_at(-5) == 0.0)

# ---------------- parse_posts: dedupe + malformed ----------------
posts = [
    {"author": "@a", "text": "call $ONE " + MINT1,
     "created_at": "2026-11-17T10:00:00Z", "likes": 10, "reposts": 1,
     "replies": 0, "url": "https://x.com/a/status/1"},
    {"author": "@b", "text": "same coin again " + MINT1,
     "created_at": "2026-11-17T10:05:00Z", "likes": 2, "reposts": 0,
     "replies": 0, "url": "https://x.com/b/status/2"},
    {"author": "@c", "text": "only a cashtag $SOLO here",
     "created_at": 1794890100, "likes": 3, "reposts": 0, "replies": 0,
     "url": "https://x.com/c/status/3"},
    {"author": "@d", "text": "same cashtag again $SOLO",
     "created_at": 1794890200, "likes": 1, "reposts": 0, "replies": 0,
     "url": "https://x.com/d/status/4"},
    "not a dict at all",
    {"author": "@e", "text": 12345, "created_at": None},
    {"author": "@f", "text": "no mint, no cashtag, just noise",
     "created_at": "2026-11-17T10:10:00Z"},
    {"author": "@g", "text": None, "created_at": None},
]
cands, stats = x_feed.parse_posts(posts)
check("parse: 2 candidates from 8 posts",
      len(cands) == 2 and stats["candidates"] == 2)
check("parse: first post wins the mint, duplicate counted",
      cands[0]["mint"] == MINT1
      and cands[0]["extra"]["duplicates"] == 1
      and stats["duplicates_dropped"] == 2)
check("parse: mint candidate carries author + engagement + source",
      cands[0]["extra"]["author"] == "@a"
      and cands[0]["extra"]["likes"] == 10
      and cands[0]["source"] == "x-feed"
      and cands[0]["chain"] == "solana")
check("parse: cashtag-only candidate has mint None, symbol SOLO",
      cands[1]["mint"] is None and cands[1]["symbol"] == "SOLO"
      and stats["cashtag_only"] == 1)
check("parse: malformed posts counted, never raised",
      stats["malformed_skipped"] == 3)  # string + int-text + None-text
check("parse: stats tally posts in / with mint",
      stats["posts_in"] == 8 and stats["with_mint"] == 1)
check("parse: garbage input never raises",
      x_feed.parse_posts(None) == ([], {
          "posts_in": 0, "candidates": 0, "with_mint": 0,
          "cashtag_only": 0, "duplicates_dropped": 0,
          "malformed_skipped": 0}))

# ---------------- bus source code ----------------
check("bus: x-feed registered as short code 'xf'",
      signal_bus.TRACK_SOURCE_CODES.get("x-feed") == "xf")
cb = signal_bus.track_callback("solana", "x-feed", MINT1)
check("bus: x-feed tap callback fits the 64-byte cap",
      cb.startswith("track:solana:xf:") and len(cb) <= 64)

# ---------------- real fixture ----------------
with open(os.path.join(REPO, "tests", "data",
                       "x_feed_posts.json")) as f:
    fixture = json.load(f)
fcands, fstats = x_feed.parse_posts(fixture)
check("fixture: 12 posts in", fstats["posts_in"] == 12)
check("fixture: 10 candidates out", fstats["candidates"] == 10)
check("fixture: 7 with mint, 3 cashtag-only",
      fstats["with_mint"] == 7 and fstats["cashtag_only"] == 3)
check("fixture: 1 duplicate dropped, 1 malformed skipped",
      fstats["duplicates_dropped"] == 1
      and fstats["malformed_skipped"] == 1)
check("fixture: duplicate mint winner counts duplicates=1",
      [c for c in fcands if c["mint"]
       and c["extra"]["duplicates"] == 1] != []
      and len([c for c in fcands if c["mint"]
               and c["extra"]["duplicates"] == 1]) == 1)
check("fixture: no-timestamp post parses with created_ts 0",
      any(c["mint"] and c["created_ts"] == 0.0
          and c["extra"]["author"] == "@no_timestamp"
          for c in fcands))
check("fixture: string engagement coerced to ints",
      any(c["extra"]["author"] == "@string_numbers"
          and c["extra"]["likes"] == 42 for c in fcands))

print("")
if fails:
    print("FAILED: %d check(s): %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL PASS")
sys.exit(0)
