#!/usr/bin/env python3
"""Signal Refinery Phase 0 — "I bought this" tap journal (2026-10-17).

The user's MANUAL buys are the ground truth for CARD QUALITY on chains the
paper bot cannot trade honestly. Every tap on "📍 I bought this" journals
{chain, source, mint, entry price}; closed positions (degen_positions.json,
which records peak_mult/last_mult) are matched by mint to score outcomes.

Consumers:
- tap_journal.tap_stats() -> per (chain, source) {n_taps, n_closed,
  win_rate} — feeds signal_bus.evaluate_feed_switches() and the nightly
  learner. The user IS the ledger for non-Solana chains.

Free-only: stdlib only, no network calls in this module.
"""
import json
import os
import time

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
JOURNAL_PATH = os.path.join(HOME, "launch_scanner", "tap_journal.jsonl")
POSITIONS_PATH = os.path.join(HOME, "degen_positions.json")
MAX_LINES = 10000


def journal_tap(mint, chain="solana", source="unknown", entry_price=0.0,
                symbol="?"):
    """Append one tap. Returns True on write. Never raises."""
    try:
        os.makedirs(os.path.dirname(JOURNAL_PATH), exist_ok=True)
        rec = {
            "ts": time.time(),
            "mint": (mint or "")[:64],
            "chain": (chain or "solana").lower()[:32],
            "source": (source or "unknown")[:64],
            "entry_price": float(entry_price or 0),
            "symbol": (symbol or "?")[:32],
        }
        lines = []
        try:
            with open(JOURNAL_PATH) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            pass
        lines.append(json.dumps(rec, separators=(",", ":")))
        lines = lines[-MAX_LINES:]
        tmp = JOURNAL_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, JOURNAL_PATH)
        return True
    except Exception:
        return False


def _load_positions():
    """{mint: position} — closed positions carry peak_mult/last_mult."""
    try:
        with open(POSITIONS_PATH) as f:
            d = json.load(f)
    except Exception:
        return {}
    out = {}
    items = d if isinstance(d, list) else d.values() if isinstance(d, dict) else []
    for p in items:
        if isinstance(p, dict) and p.get("mint"):
            out[p["mint"]] = p
    return out


def tap_stats(win_mult=2.0):
    """Per (chain, source) tap outcomes.

    A tap is a "win" when its position closed with peak_mult >= win_mult
    (the card found a runner the user could have ridden). Open positions
    count as taps but not as decided outcomes.
    Returns {(chain, source): {"n_taps", "n_closed", "n_wins", "win_rate"}}.
    Never raises.
    """
    stats = {}
    try:
        with open(JOURNAL_PATH) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    positions = _load_positions()
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        key = ((rec.get("chain") or "solana").lower(),
               (rec.get("source") or "unknown"))
        s = stats.setdefault(key, {"n_taps": 0, "n_closed": 0, "n_wins": 0})
        s["n_taps"] += 1
        pos = positions.get(rec.get("mint"))
        if isinstance(pos, dict) and pos.get("status") == "closed":
            s["n_closed"] += 1
            try:
                peak = float(pos.get("peak_mult") or 0)
            except (TypeError, ValueError):
                peak = 0
            if peak >= win_mult:
                s["n_wins"] += 1
    for s in stats.values():
        n = s["n_closed"]
        s["win_rate"] = round(s["n_wins"] / n, 3) if n else 0.0
    return stats
