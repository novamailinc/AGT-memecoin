#!/usr/bin/env python3
"""Signal Refinery Phase 0 — normalized signal bus (2026-10-17).

Plumbing only. No signal producers live on this bus yet (Phase 1/2 add
Base/ETH/HL adapters); the Solana feed scripts publish their fired signals
here so the bus, schema, and consumers are real and tested.

Design (nova-design.md):
- Four chains are SIGNAL SOURCES ONLY: solana, base, ethereum, hyperliquid.
- One destination: the existing degen lane, as cards. No paper trading on
  any chain via this bus; every buy stays manual.
- Every signal carries chain + source so cards can be tagged and the user's
  "I bought this" taps journal per-chain outcomes.
- Per-chain FEED kill switches: base_feed, eth_feed, hl_feed. Solana is
  never killed by this machinery. Fail-closed: a cold chain's feed pauses.

Free-only: stdlib only, no network calls in this module.
"""
import json
import os
import time

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
BUS_PATH = os.path.join(HOME, "launch_scanner", "signal_bus.jsonl")
SWITCHES_PATH = os.path.join(HOME, "launch_scanner", "feed_switches.json")

SCHEMA_VERSION = 1
CHAINS = ("solana", "base", "ethereum", "hyperliquid")
# Chains whose feeds may be paused by the kill-switch machinery.
SWITCHABLE = ("base", "ethereum", "hyperliquid")
MAX_BUS_LINES = 5000

# Short source codes for the "📍 I bought this" tap callback.
# Telegram caps callback_data at 64 bytes; the full
# "track:<chain>:<source>:<mint>" overflows it (BUTTON_DATA_INVALID at
# send time — real bug, 2026-10-17: the first ETH runner card and a
# smart-watcher card never delivered). Feeds send the short code; the
# telegram tap parser resolves it back to the full source name, so the
# tap journal and bus keep full (chain, source) attribution.
# Budget: "track:" (6) + chain + ":" + code + ":" + mint <= 64.
TRACK_SOURCE_CODES = {
    "degen-feed": "df",
    "smart-watcher": "sw",
    "base-runner": "br",
    "eth-runner": "er",
    "hl-listing": "hl",
    "hl-breakout": "hb",
    "locked-liq": "ll",
    "grad-select-confirm": "gs",
    "grad-select-entry": "ge",
    "grad-raw-confirm": "gr",
    "x-feed": "xf",
}


def track_callback(chain, source, mint):
    """64-byte-safe tap callback: track:<chain>:<code>:<mint>.

    Unknown sources fall back to a truncated code (documented risk: keep
    new source codes <= 6 chars). Never raises."""
    code = TRACK_SOURCE_CODES.get((source or "").lower())
    if not code:
        code = (source or "unknown")[:6].lower() or "unknown"
    cb = "track:%s:%s:%s" % ((chain or "solana").lower(), code, mint or "")
    return cb


# Exit-tempo hints per chain, written into the card spec (user-approved
# 2026-10-17): ETH decays slowest so the runner portion can breathe;
# Solana/Base decay fast so profit is taken aggressively.
CHAIN_EXIT_HINT = {
    "solana": "⚡ Fast decay — take profit aggressively, never let a 2x ride back to breakeven",
    "base": "⚡ Fast decay — take profit aggressively, never let a 2x ride back to breakeven",
    "ethereum": "🐢 ETH decays slowest — let the runner portion breathe",
    "hyperliquid": "📋 Listing event — trade the listing, not a meme runner",
}

CHAIN_LABEL = {
    "solana": "Solana",
    "base": "Base",
    "ethereum": "Ethereum",
    "hyperliquid": "Hyperliquid",
}


def make_signal(chain, source, mint, symbol="", score=0.0, tier="",
                age_min=0.0, mcap=0.0, liquidity=0.0,
                buys=0, sells=0, evidence=None, extra=None):
    """Build a normalized signal dict. Never raises."""
    try:
        chain = (chain or "solana").lower()
        if chain not in CHAINS:
            chain = "solana"
        return {
            "v": SCHEMA_VERSION,
            "ts": time.time(),
            "chain": chain,
            "source": (source or "unknown")[:64],
            "mint": mint or "",
            "symbol": (symbol or "?")[:32],
            "score": float(score or 0),
            "tier": (tier or "")[:32],
            "age_min": float(age_min or 0),
            "mcap": float(mcap or 0),
            "liquidity": float(liquidity or 0),
            "buys": int(buys or 0),
            "sells": int(sells or 0),
            "evidence": dict(evidence or {}),
            "extra": dict(extra or {}),
        }
    except Exception:
        return {"v": SCHEMA_VERSION, "ts": time.time(), "chain": "solana",
                "source": "unknown", "mint": mint or "", "symbol": "?",
                "score": 0.0, "tier": "", "age_min": 0.0, "mcap": 0.0,
                "liquidity": 0.0, "buys": 0, "sells": 0,
                "evidence": {}, "extra": {}}


def validate(sig):
    """(ok, errors). Fail-closed shape check for bus consumers."""
    errs = []
    if not isinstance(sig, dict):
        return False, ["not-a-dict"]
    if sig.get("v") != SCHEMA_VERSION:
        errs.append("bad-schema-version")
    if sig.get("chain") not in CHAINS:
        errs.append("bad-chain")
    if not sig.get("mint"):
        errs.append("missing-mint")
    if not sig.get("source"):
        errs.append("missing-source")
    return (len(errs) == 0), errs


def publish(sig):
    """Validate + append to the bus log (bounded). Returns True/False."""
    ok, _ = validate(sig)
    if not ok:
        return False
    try:
        os.makedirs(os.path.dirname(BUS_PATH), exist_ok=True)
        lines = []
        try:
            with open(BUS_PATH) as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            pass
        lines.append(json.dumps(sig, separators=(",", ":")))
        lines = lines[-MAX_BUS_LINES:]
        tmp = BUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, BUS_PATH)
        return True
    except Exception:
        return False


def recent(chain=None, source=None, limit=100):
    """Newest-first signals, optionally filtered. Never raises."""
    out = []
    try:
        with open(BUS_PATH) as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return []
    except Exception:
        return []
    for line in reversed(lines):
        try:
            sig = json.loads(line)
        except Exception:
            continue
        if chain and sig.get("chain") != chain:
            continue
        if source and sig.get("source") != source:
            continue
        out.append(sig)
        if len(out) >= limit:
            break
    return out


# ---- per-chain feed kill switches -----------------------------------------

def _load_switches():
    try:
        with open(SWITCHES_PATH) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_switches(d):
    try:
        tmp = SWITCHES_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SWITCHES_PATH)
        return True
    except Exception:
        return False


def feed_enabled(chain):
    """True unless this chain's feed was paused. Solana is never paused."""
    chain = (chain or "solana").lower()
    if chain not in SWITCHABLE:
        return True
    sw = _load_switches().get(chain, {})
    if isinstance(sw, dict):
        return bool(sw.get("enabled", True))
    return True


def set_feed(chain, enabled, reason=""):
    """Pause/resume a switchable chain feed. Returns True on write."""
    chain = (chain or "").lower()
    if chain not in SWITCHABLE:
        return False
    sw = _load_switches()
    sw[chain] = {"enabled": bool(enabled), "reason": (reason or "")[:200],
                 "ts": time.time()}
    return _save_switches(sw)


def feed_status():
    """{chain: {enabled, reason, ts}} for the switchable chains."""
    sw = _load_switches()
    return {c: {"enabled": bool(sw.get(c, {}).get("enabled", True)),
                "reason": sw.get(c, {}).get("reason", ""),
                "ts": sw.get(c, {}).get("ts", 0)}
            for c in SWITCHABLE}


def evaluate_feed_switches(stats, min_taps=10, min_win_rate=0.20,
                           win_mult=2.0):
    """Auto-pause cold chain feeds from the user's own tap outcomes.

    stats: {(chain, source): {"n_taps": int, "win_rate": float}} as produced
    by tap_journal.tap_stats(win_mult=...). Only pauses (never resumes);
    resuming is a human decision via set_feed(). Fail-open: any error or
    thin data leaves feeds untouched. Returns a report dict.
    """
    report = {"paused": [], "kept": [], "skipped_thin": []}
    try:
        per_chain = {}
        for (chain, _source), s in (stats or {}).items():
            c = (chain or "solana").lower()
            if c not in SWITCHABLE:
                continue
            agg = per_chain.setdefault(c, {"n": 0, "wins": 0.0})
            agg["n"] += int(s.get("n_taps") or 0)
            agg["wins"] += float(s.get("win_rate") or 0) * int(s.get("n_taps") or 0)
        for chain, agg in per_chain.items():
            n = agg["n"]
            if n < min_taps:
                report["skipped_thin"].append(chain)
                continue
            wr = agg["wins"] / n if n else 0
            if wr < min_win_rate and feed_enabled(chain):
                set_feed(chain, False,
                         "auto: user tap win rate %.0f%% over %d taps (<%.0f%%)"
                         % (wr * 100, n, min_win_rate * 100))
                report["paused"].append(
                    {"chain": chain, "win_rate": round(wr, 3), "n_taps": n})
            else:
                report["kept"].append(
                    {"chain": chain, "win_rate": round(wr, 3), "n_taps": n})
    except Exception:
        pass
    return report
