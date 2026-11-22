#!/usr/bin/env python3
"""Nova's card design: 3-second scannable cards for manual gambling.

Design philosophy:
- The user gambles REAL money manually. They need to decide in 3 seconds.
- First line = decision. Second line = money. Rest = context.
- Fewer cards, higher bar. Every card should be worth a look.
- Runners scream. Duds whisper.

Card format:
    🟢 $TOKEN — 2.4x RUNNER
    💰 $0.0042 → $0.0101
    📊 MC $850K · Liq $42K · 45 buyers
    ⚡ 8m old · 24b/4s · 🧠 2 smart in
    🛡️ Clean
    ⛓ Solana · degen-feed
    ⚡ Fast decay — take profit aggressively, never let a 2x ride back to breakeven
    [📋 Copy CA] [📍 I bought this]

Phase 0 (Signal Refinery): every card carries a chain + source tag. The
per-chain exit-tempo hint is written into the card spec (user-approved
2026-10-17): ETH decays slowest so the runner portion can breathe;
Solana/Base decay fast so profit is taken aggressively. A tap nudge
("it trains your feed") reminds the user their taps journal outcomes.

Signal colors:
    🟢 Strong: score 80+, or smart money + safety clean
    🟡 Watch: score 60-79, decent setup
    🔴 Risky: score 50-59, high risk gamble

Status tags:
    🚀 RUNNER: 2x+ from alert price
    ⚡ FRESH: <10 min old launch
    🧠 SMART: smart money detected
    📊 STEADY: mean-reversion setup
"""
from typing import Dict, Optional

def signal_color(score: float, has_smart: bool = False,
                 is_runner: bool = False) -> str:
    """🟢 strong, 🟡 watch, 🔴 risky."""
    if is_runner or score >= 80 or (has_smart and score >= 70):
        return "🟢"
    if score >= 60:
        return "🟡"
    return "🔴"

def status_tag(is_runner: bool = False, is_fresh: bool = False,
               has_smart: bool = False, is_steady: bool = False,
               multiple: float = 0) -> str:
    """Status tag for the headline."""
    if is_runner and multiple >= 2.0:
        return f"— {multiple:.1f}x RUNNER"
    if has_smart:
        return "— 🧠 SMART MONEY"
    if is_fresh:
        return "— ⚡ FRESH"
    if is_steady:
        return "— 📊 STEADY"
    return ""

def fmt_price(p: float) -> str:
    if not p or p <= 0:
        return "?"
    if p >= 1:
        return f"${p:.4f}"
    if p >= 0.01:
        return f"${p:.6f}"
    return f"${p:.8f}"

def fmt_mc(v: float) -> str:
    if not v:
        return "?"
    if v >= 1e6:
        return f"${v/1e6:.1f}M"
    if v >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"

def build_card(
    symbol: str,
    score: float,
    mint: str,
    # Price action
    entry_price: float = 0,
    current_price: float = 0,
    # Market context
    mcap: float = 0,
    liquidity: float = 0,
    buyers: int = 0,
    # Timing
    age_min: float = 0,
    buys_5m: int = 0,
    sells_5m: int = 0,
    # Conviction
    smart_wallets: int = 0,
    safety: str = "unknown",
    # Card type
    is_runner: bool = False,
    is_fresh: bool = False,
    is_steady: bool = False,
    risk_note: str = "",
    # Signal Refinery Phase 0: chain + source tagging
    chain: str = "solana",
    source: str = "",
    # LP-lock badge (EVM runner feeds): pre-rendered badge line from
    # lp_lock.badge(), e.g. "🔒 LP burned 96%". Empty = no line.
    lp_lock: str = "",
    # Total token supply, preformatted ("1.0B") via lp_lock.token_supply().
    # Empty = omitted (chain doesn't expose it or the lookup failed).
    supply: str = "",
    # Ticker saturation line, pre-rendered by saturation.saturation_line()
    # ("🎭 7th $PEPE today — ..."). Empty = omitted (count < 2, non-Solana
    # card, or the census lookup failed soft).
    saturation: str = "",
) -> str:
    """Build a 3-second scannable card."""
    color = signal_color(score, smart_wallets > 0, is_runner)

    multiple = 0
    if entry_price > 0 and current_price > 0:
        multiple = current_price / entry_price

    tag = status_tag(is_runner, is_fresh, smart_wallets > 0,
                     is_steady, multiple)

    lines = []
    # Line 1: DECISION — color + symbol + status
    lines.append(f"{color} ${symbol} {tag}".strip())

    # Line 2: MONEY — entry → current (the numbers that matter)
    if entry_price > 0 and current_price > 0:
        lines.append(f"💰 {fmt_price(entry_price)} → {fmt_price(current_price)}")
    elif current_price > 0:
        lines.append(f"💰 {fmt_price(current_price)}")

    # Line 3: CONTEXT — can I get in and out?
    ctx = []
    if mcap > 0:
        ctx.append(f"MC {fmt_mc(mcap)}")
    if liquidity > 0:
        ctx.append(f"Liq {fmt_mc(liquidity)}")
    if buyers > 0:
        ctx.append(f"{buyers} buyers")
    if supply:
        ctx.append(f"Supply {supply}")
    if ctx:
        lines.append("📊 " + " · ".join(ctx))

    # Line 4: CONVICTION — why should I care?
    conv = []
    if age_min > 0:
        if age_min < 60:
            conv.append(f"{age_min:.0f}m old")
        else:
            conv.append(f"{age_min/60:.1f}h old")
    if buys_5m > 0 or sells_5m > 0:
        conv.append(f"{buys_5m}b/{sells_5m}s")
    if smart_wallets > 0:
        conv.append(f"🧠 {smart_wallets} smart in")
    if conv:
        lines.append("⚡ " + " · ".join(conv))

    # Line 5: SAFETY — one word
    safety_clean = safety.lower() in ("clean", "passed", "ok", "safe")
    lines.append(f"🛡️ {'Clean' if safety_clean else safety}")

    # Line 5b: LP-LOCK — EVM runner feeds only (Solana/HL pass "")
    if lp_lock:
        lines.append(lp_lock)

    # Line 5c: TICKER SATURATION — Solana pump.fun cards only
    if saturation:
        lines.append(saturation)

    # Phase 0: chain + source tag (the card's provenance)
    chain_key = (chain or "solana").lower()
    chain_label, exit_hint = None, ""
    for _mod in ("daemon.launch_scanner.signal_bus", "signal_bus"):
        try:
            _sb = __import__(_mod, fromlist=["CHAIN_LABEL"])
            chain_label = _sb.CHAIN_LABEL.get(chain_key, chain_key.title())
            exit_hint = _sb.CHAIN_EXIT_HINT.get(chain_key, "")
            break
        except Exception:
            continue
    if chain_label is None:
        # Last-resort local fallback (kept in sync with signal_bus.py)
        chain_label = {"solana": "Solana", "base": "Base",
                       "ethereum": "Ethereum",
                       "hyperliquid": "Hyperliquid"}.get(chain_key,
                                                         chain_key.title())
        exit_hint = {"solana": "⚡ Fast decay — take profit aggressively, never let a 2x ride back to breakeven",
                     "base": "⚡ Fast decay — take profit aggressively, never let a 2x ride back to breakeven",
                     "ethereum": "🐢 ETH decays slowest — let the runner portion breathe",
                     "hyperliquid": "📋 Listing event — trade the listing, not a meme runner"}.get(chain_key, "")
    tag_bits = [chain_label]
    if source:
        tag_bits.append(str(source)[:32])
    lines.append("⛓ " + " · ".join(tag_bits))
    if exit_hint:
        lines.append(exit_hint)

    # Risk note (for degen gambles)
    if risk_note:
        lines.append(risk_note)

    # CA (required, never bare)
    lines.append(f"`{mint}`")

    # Tap nudge: the user's taps are the measurement loop
    lines.append("_tap 📍 if you buy — it trains your feed_")

    return "\n".join(lines)


def build_runner_update(
    symbol: str,
    mint: str,
    entry_price: float,
    current_price: float,
    peak_price: float = 0,
) -> str:
    """Compact runner update card — for coins that are moving."""
    multiple = current_price / entry_price if entry_price > 0 else 0
    peak_mult = peak_price / entry_price if peak_price > 0 and entry_price > 0 else 0

    if multiple >= 3:
        emoji = "🚀🚀"
    elif multiple >= 2:
        emoji = "🚀"
    elif multiple >= 1.5:
        emoji = "📈"
    else:
        emoji = "📊"

    lines = [
        f"{emoji} ${symbol} — {multiple:.1f}x",
        f"💰 {fmt_price(entry_price)} → {fmt_price(current_price)}",
    ]
    if peak_mult > multiple:
        lines.append(f"📉 Off peak ({peak_mult:.1f}x high)")
    lines.append(f"`{mint}`")
    return "\n".join(lines)
