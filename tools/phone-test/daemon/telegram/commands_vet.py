"""The tip-line: /vet <mint> (2026-10-10).

The user hears noise about a coin — groups, X, the streets — and drops
the mint here. The bot cross-checks everything it knows on-chain and
reports whether the chain agrees with the tip.

READ-ONLY, always. /vet never enters, never publishes alerts, never
touches the wallet registry, never trades. It only answers the user's
question: do my ears and the chain agree?

Checks (all fail-soft; unknown is reported as unknown, never guessed):
1. Safety — safety.check_token, the same gate the bot uses. A hard fail
   (honeypot / can't-sell / insider cluster) ends the verdict at
   CHAIN SAYS NO, no matter what the tip says.
2. Smart money — tracked wallets holding/buying this mint, from the
   live scanner state's smart_activity plus the grades ledger,
   grade-weighted (a proven picker's buy counts more).
3. Deployer — resolved via deployers.resolve_deployer; blacklisted =
   red flag, hot (proven runner deployer) = green flag.
4. Market — DexScreener mcap / liquidity / 24h change for context.

Verdict:
  🔴 CHAIN SAYS NO — safety hard fail. Walk away.
  🟢 CHAIN AGREES  — safety pass + grade-weighted smart buying >= 1.0.
  🟡 CHAIN NEUTRAL — safety pass, no smart-money signal either way.
  ⚠️ UNVETTED      — safety report unknown/failed. The bot's own rule:
                     unknown safety never earns a green.

Solana mints only — the smart-money and deployer intelligence is
Solana. EVM mints get a plain "not covered yet" rather than a guess.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("forex_agent.daemon.telegram.vet")

_MINT_MIN_LEN = 32
_MINT_MAX_LEN = 48
_SMART_LOOKBACK_DAYS = 7.0
_AGREE_MIN_EFF = 1.0


def _valid_mint(mint: str) -> bool:
    return bool(mint) and _MINT_MIN_LEN <= len(mint) <= _MINT_MAX_LEN \
        and not mint.startswith("0x")


def _short(addr: str) -> str:
    a = str(addr or "")
    return a[:6] + "…" + a[-4:] if len(a) > 12 else a


def _f(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _smart_buyers(mint: str) -> Tuple[List[str], float, Dict[str, float]]:
    """Tracked wallets with recent buys of this mint.

    Returns (labels, effective_weighted_buys, {wallet: weight}).
    Merges the live scanner state's smart_activity with the grades
    ledger (catches buys that aged out of the activity window).
    Never raises.
    """
    wallets: Dict[str, float] = {}
    try:
        from daemon.common import get_daemon_state, get_store
        state = get_daemon_state(get_store(), "launch_scanner") or {}
        for w in (state.get("smart_activity") or {}).get(mint) or []:
            wallets[str(w)] = wallets.get(str(w), 0.0)  # present
    except Exception as exc:
        logger.warning("vet: scanner state read failed: %s", exc)
    try:
        from daemon.launch_scanner import smartgrade
        grades = smartgrade.load_grades()
        cutoff = time.time() - _SMART_LOOKBACK_DAYS * 86400.0
        for w, rec in (grades or {}).items():
            for b in (rec or {}).get("buys") or []:
                if (b.get("mint") == mint
                        and _f(b.get("buy_ts")) >= cutoff):
                    wallets[str(w)] = 1.0
                    break
    except Exception as exc:
        logger.warning("vet: grades read failed: %s", exc)
    if not wallets:
        return [], 0.0, {}
    try:
        from daemon.launch_scanner import smartmoney, smartgrade
        registry = smartmoney.load_registry()
        weights = smartgrade.wallet_weights()
    except Exception:
        registry, weights = {}, {}
    labels = []
    eff = 0.0
    per_wallet: Dict[str, float] = {}
    for w in wallets:
        wt = float(weights.get(w, 1.0))
        per_wallet[w] = wt
        eff += wt
        info = (registry or {}).get(w) or {}
        labels.append(info.get("label") or _short(w))
    return labels, round(eff, 2), per_wallet


def _safety(mint: str) -> Dict[str, Any]:
    """Run the bot's own safety gate. Never raises."""
    try:
        from daemon.launch_scanner import safety
        report = safety.check_token("solana", mint, timeout=25.0) or {}
        return report if isinstance(report, dict) else {}
    except Exception as exc:
        logger.warning("vet: safety check failed for %s: %s", mint, exc)
        return {"_error": str(exc)[:80]}


def _deployer(mint: str) -> Tuple[Optional[str], str]:
    """(deployer_address|None, flag) where flag is blacklisted/hot/
    unknown/clean. Never raises."""
    try:
        from daemon.launch_scanner import deployers
        dep = deployers.resolve_deployer(mint, "solana")
        if not dep:
            return None, "unknown"
        if deployers.is_blacklisted(dep):
            return dep, "blacklisted"
        if deployers.is_hot(dep, SimpleNamespace()):
            return dep, "hot"
        return dep, "clean"
    except Exception as exc:
        logger.warning("vet: deployer check failed: %s", exc)
        return None, "unknown"


def _market(mint: str) -> Dict[str, Any]:
    """DexScreener context: mcap / liquidity / 24h change. Never raises."""
    try:
        from daemon.launch_scanner import sources
        stats = sources.dexscreener_token_stats(mint) or {}
        return stats if isinstance(stats, dict) else {}
    except Exception as exc:
        logger.warning("vet: market stats failed: %s", exc)
        return {}


def _fmt_money(v: float) -> str:
    if v >= 1_000_000:
        return "$%.2fM" % (v / 1_000_000)
    if v >= 1_000:
        return "$%.1fK" % (v / 1_000)
    return "$%.0f" % v


def vet_mint(mint: str) -> str:
    """Full vetting report for one mint. Pure logic; the caller sends it.

    Never raises: a Telegram command must not crash the update loop.
    """
    try:
        return _vet_mint_inner(mint)
    except Exception as exc:
        logger.warning("vet_mint top-level guard: %s", exc, exc_info=True)
        return ("⚠️ Vetting hiccup — couldn't complete the checks. "
                "Try again in a minute.")


def _vet_mint_inner(mint: str) -> str:
    mint = (mint or "").strip()
    if not _valid_mint(mint):
        if mint.startswith("0x"):
            return ("⛔ /vet covers Solana mints only for now — the "
                    "smart-money and deployer intel is Solana. "
                    "EVM support is on the list.")
        return ("Usage: /vet <solana-mint-address>\n"
                "Drop the mint you heard noise about; I'll cross-check "
                "the chain and tell you if it agrees.")

    lines = ["🔍 Vetting `%s`" % _short(mint), ""]

    # 1. Safety — the same gate the bot uses. Hard fail ends it here.
    # Each check is called defensively: even a contract-violating
    # crash degrades to "unknown", never to a guess.
    try:
        rep = _safety(mint)
    except Exception:
        rep = {"_error": "check crashed"}
    score = rep.get("score")
    hard_fails = [k for k in ("honeypot", "cannot_sell", "no_sell",
                              "insider", "high_tax")
                  if rep.get(k)]
    if rep.get("_error") or (score is None and not rep):
        verdict = "unvetted"
        lines.append("⚠️ Safety: UNVETTED — no safety report "
                     "(unknown is never a green light).")
    elif hard_fails:
        verdict = "no"
        lines.append("🔴 Safety: HARD FAIL — %s" % ", ".join(hard_fails))
        try:
            lines.append("   score %.0f/100" % _f(score))
        except Exception:
            pass
    else:
        verdict = "pending"
        lines.append("✅ Safety: pass (score %.0f/100)" % _f(score))

    # 2. Smart money — grade-weighted.
    try:
        labels, eff, _ = _smart_buyers(mint)
    except Exception:
        labels, eff = [], 0.0
    if labels:
        lines.append("🧠 Smart money: %s buying (%.1f eff, %s)" % (
            ", ".join(labels[:4]), eff,
            "grade-weighted" if abs(eff - len(labels)) > 0.05
            else "tracked"))
    else:
        lines.append("🧠 Smart money: none of our tracked wallets in "
                     "this (7d).")

    # 3. Deployer.
    try:
        dep, flag = _deployer(mint)
    except Exception:
        dep, flag = None, "unknown"
    if flag == "blacklisted":
        lines.append("🚫 Deployer: BLACKLISTED (%s)" % _short(dep or ""))
        if verdict == "pending":
            verdict = "no"
    elif flag == "hot":
        lines.append("🔥 Deployer: proven runner-maker (%s)"
                     % _short(dep or ""))
    elif flag == "clean":
        lines.append("👤 Deployer: known, no red flags (%s)"
                     % _short(dep or ""))
    else:
        lines.append("👤 Deployer: unknown (couldn't resolve).")

    # 4. Market context.
    try:
        mkt = _market(mint)
    except Exception:
        mkt = {}
    if mkt:
        mcap = _f(mkt.get("mcap") or mkt.get("marketCap"))
        liq = _f(mkt.get("liquidity") or mkt.get("liquidityUsd"))
        chg = mkt.get("priceChange24h") or mkt.get("chg_24h_pct")
        parts = []
        if mcap > 0:
            parts.append("mcap %s" % _fmt_money(mcap))
        if liq > 0:
            parts.append("liq %s" % _fmt_money(liq))
        try:
            if chg is not None:
                parts.append("24h %+.0f%%" % float(chg))
        except (TypeError, ValueError):
            pass
        if parts:
            lines.append("📊 Market: %s" % " · ".join(parts))

    # Verdict.
    lines.append("")
    if verdict == "no":
        lines.append("🔴 **CHAIN SAYS NO** — walk away, whatever the "
                     "tip said.")
    elif verdict == "unvetted":
        lines.append("⚠️ **UNVETTED** — the chain has no safety read on "
                     "this. The bot's own rule: unknown safety never "
                     "earns a green. Your call, eyes open.")
    elif eff >= _AGREE_MIN_EFF:
        lines.append("🟢 **CHAIN AGREES** — your ears and the chain "
                     "point the same way (safety pass + smart money "
                     "in). Still paper-only; the ledger decides.")
    else:
        lines.append("🟡 **CHAIN NEUTRAL** — safety passes but no "
                     "smart-money confirmation. The tip is unconfirmed; "
                     "size like a rumor, not a conviction.")
    lines.append("")
    lines.append("_Read-only vetting — nothing entered, alerted, or "
                 "tracked._")
    return "\n".join(lines)


def cmd_vet_handler(ctx, chat_id, user_id, update_id, args):
    """/vet <mint> — the tip-line. Adapter for the telegram daemon's
    handler convention (ctx, chat_id, user_id, update_id, args)."""
    try:
        mint = (args[0] if args else "").strip()
        ctx.send(chat_id, vet_mint(mint), None)
    except Exception as exc:
        logger.warning("vet handler failed: %s", exc, exc_info=True)
        try:
            ctx.send(chat_id,
                     "⚠️ Vetting hiccup — try again in a minute.", None)
        except Exception:
            pass
    try:
        from daemon.telegram.commands import _audit
        _audit(ctx, chat_id, user_id, update_id, "vet",
               (args[0][:16] if args else "-"), True)
    except Exception:
        pass
