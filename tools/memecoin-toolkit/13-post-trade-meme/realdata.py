"""Real-data loader for the post-trade scorecard.

Reads the live paper bot's recorded closes (READ-ONLY) into this
toolkit's canonical close shape -- ``{lane, mint, realized_mult,
peak_mult, exit_reason, was_rug}`` -- so every existing ``posttrade``
function runs on real numbers: the full per-lane scorecard, expectancy,
win rates, rug rate, and the round-tripper leakage count.

Sources and derivations (missing files are skipped, never invented):

  * ``backtest_dataset.jsonl`` paper records (``outcome_kind ==
    "paper"``, streamed): realized_mult / peak_mult / rug flag /
    exit_reason are recorded directly. Lane label ``paper``.
  * ``degen_mirror_ledger.json`` / ``ceo_ledger.json`` (``closes``):
    mirror closes record ``pnl_usd`` + per-leg ``realized_usd`` on a
    $1 notional, so realized_mult = sum(leg realized_usd) (cross-checks
    against 1 + pnl_usd); CEO closes record ``realized_mult`` directly.
    Unquotable mirror closes reconstruct to ~-0.001x (exit price 0.0,
    fees beyond the stake) and are kept as recorded.
    These ledgers record NO peak multiple: peak_mult is None, the close
    is excluded from round-tripper / leakage math, and the exclusion is
    counted (``n_peak_unknown``) instead of being silently treated as
    "never peaked".
  * ``steady_ledger.json`` (``closed``): realized_mult = 1 + pnl /
    size_usd; peak_mult = peak_price / entry_price.
  * ``stacker_ledger.json``: closes are the ``positions`` with
    ``frac_remaining == 0`` and an ``exit_ts``; realized_mult =
    1 + realized_pnl / size_usd; peak as for steady.

Rug definition: the recorded ``rug`` flag where a source records one
(the paper dataset). Lane ledgers record no flag, so there a close is
a rug iff realized_mult <= 0.05 (near-total loss) -- a documented
proxy, reported per lane via ``rug_basis``.

Privacy: loaders whitelist fields. Wallet addresses, wallet names /
grades, registry and follower data are never copied into outputs --
only public token mints, lane labels and outcome numbers.
"""
from __future__ import annotations

import json
import os

import posttrade

RUG_NEAR_ZERO = 0.05

DEFAULT_PATHS = {
    "dataset": "/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl",
    "mirror": "/home/hatch/.forex-agent/degen_mirror_ledger.json",
    "ceo": "/home/hatch/.forex-agent/ceo_ledger.json",
    "steady": "/home/hatch/.forex-agent/steady_ledger.json",
    "stacker": "/home/hatch/.forex-agent/stacker_ledger.json",
}


def _close(lane, mint, realized_mult, peak_mult, exit_reason, rug_flag):
    realized = float(realized_mult)
    if rug_flag is not None:
        was_rug, basis = bool(rug_flag), "recorded"
    else:
        was_rug, basis = realized <= RUG_NEAR_ZERO, "proxy"
    return {
        "lane": lane,
        "mint": mint,
        "realized_mult": realized,
        "peak_mult": (None if peak_mult is None else float(peak_mult)),
        "exit_reason": exit_reason,
        "was_rug": was_rug,
        "rug_basis": basis,
    }


def _posttrade_view(closes: list[dict]) -> list[dict]:
    """Strip loader metadata so records match posttrade's exact shape.

    peak_mult may be None (peak not recorded); posttrade functions
    that never touch the peak (win rate, expectancy, rug rate, the
    per-lane scorecard) are safe on these, and leakage functions are
    only ever called on the peak-known subset (see full_scorecard).
    """
    keys = ("lane", "mint", "realized_mult", "peak_mult", "exit_reason", "was_rug")
    return [{k: c[k] for k in keys} for c in closes]


# ------------------------------------------------------------------ loaders

def iter_dataset_paper_closes(path: str):
    """Stream canonical closes for outcome_kind == 'paper' records."""
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("outcome_kind") != "paper" or rec.get("realized_mult") is None:
                continue
            yield _close("paper", rec.get("mint", ""), rec["realized_mult"],
                         rec.get("peak_mult"), rec.get("exit_reason", ""),
                         rec.get("rug"))


def _mirror_style_closes(data: dict, default_lane: str):
    out = []
    for rec in data.get("closes", []):
        if rec.get("realized_mult") is not None:
            realized = float(rec["realized_mult"])
        else:
            legs = rec.get("legs") or {}
            realized = (sum(float(leg.get("realized_usd") or 0.0)
                            for leg in legs.values() if isinstance(leg, dict))
                        if legs else 1.0 + float(rec.get("pnl_usd") or 0.0))
        out.append(_close(rec.get("lane") or default_lane, rec.get("mint", ""),
                          realized, None, rec.get("closed_reason", ""), None))
    return out


def load_mirror_closes(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return _mirror_style_closes(json.load(handle), "degen_mirror")


def load_ceo_closes(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return _mirror_style_closes(json.load(handle), "ceo")


def load_steady_closes(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    out = []
    for rec in data.get("closed", []):
        size = float(rec.get("size_usd") or 1.0)
        entry = float(rec.get("entry_price") or 0.0)
        peak = (float(rec["peak_price"]) / entry) if entry and rec.get("peak_price") else None
        out.append(_close("steady", rec.get("mint", ""),
                          1.0 + float(rec.get("pnl") or 0.0) / size,
                          peak, rec.get("exit_reason", ""), None))
    return out


def load_stacker_closes(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    out = []
    for rec in data.get("positions", []):
        if rec.get("frac_remaining") != 0.0 or not rec.get("exit_ts"):
            continue
        size = float(rec.get("size_usd") or 1.0)
        entry = float(rec.get("entry_price") or 0.0)
        peak = (float(rec["peak_price"]) / entry) if entry and rec.get("peak_price") else None
        out.append(_close("stacker", rec.get("mint", ""),
                          1.0 + float(rec.get("realized_pnl") or 0.0) / size,
                          peak, rec.get("exit_reason", ""), None))
    return out


def load_all_closes(paths: dict | None = None) -> list[dict]:
    """Every real close from whichever sources exist, in source order."""
    paths = {**DEFAULT_PATHS, **(paths or {})}
    closes: list[dict] = []
    if os.path.exists(paths["dataset"]):
        closes.extend(iter_dataset_paper_closes(paths["dataset"]))
    for key, loader in (("mirror", load_mirror_closes), ("ceo", load_ceo_closes),
                        ("steady", load_steady_closes), ("stacker", load_stacker_closes)):
        if os.path.exists(paths[key]):
            closes.extend(loader(paths[key]))
    return closes


# ----------------------------------------------------------------- scorecard

def full_scorecard(closes: list[dict]) -> dict:
    """The full real scorecard for a set of canonical closes.

    Per-lane rows come from ``posttrade.per_lane_scorecard`` plus each
    lane's round-tripper count over its peak-known closes. Leakage is
    computed ONLY over closes whose peak was recorded; the count of
    peak-unknown closes is reported alongside so the gap is visible.
    """
    view = _posttrade_view(closes)
    peak_known = [c for c in view if c["peak_mult"] is not None]
    lanes: dict[str, dict] = {}
    for lane, row in posttrade.per_lane_scorecard(view).items():
        lane_rows = [c for c in view if c["lane"] == lane]
        lane_peaked = [c for c in lane_rows if c["peak_mult"] is not None]
        bases = {c["rug_basis"] for c in closes if c["lane"] == lane}
        lanes[lane] = {
            **row,
            "round_trippers": posttrade.round_trippers(lane_peaked)["count"],
            "n_peak_known": len(lane_peaked),
            "n_peak_unknown": len(lane_rows) - len(lane_peaked),
            "rug_basis": bases.pop() if len(bases) == 1 else "mixed",
            "pnl_usd": posttrade.pnl_usd(lane_rows),
        }
    return {
        "n": len(view),
        "wr": posttrade.win_rate(view),
        "breakeven_rate": posttrade.breakeven_rate(view),
        "expectancy": posttrade.expectancy(view),
        "rug_rate": posttrade.rug_rate(view),
        "pnl_usd": posttrade.pnl_usd(view),
        "median_realized": posttrade.median_realized(view),
        "per_lane": lanes,
        "leakage_peak_known_only": posttrade.leakage_report(peak_known),
        "n_peak_known": len(peak_known),
        "n_peak_unknown": len(view) - len(peak_known),
    }


def render_markdown(closes: list[dict]) -> str:
    """Markdown per-lane table + totals + leakage, for local reports."""
    card = full_scorecard(closes)
    lines = [
        "| lane | n | WR (>=1.5x) | >=1.0x | expectancy | rug rate | rug basis "
        "| round-trippers | peak known | P/L $1 shots |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for lane in sorted(card["per_lane"]):
        s = card["per_lane"][lane]
        lines.append(
            f"| {lane} | {s['n']} | {s['wr']:.3f} | {s['breakeven_rate']:.3f} "
            f"| {s['expectancy']:+.3f} | {s['rug_rate']:.3f} | {s['rug_basis']} "
            f"| {s['round_trippers']} | {s['n_peak_known']} | {s['pnl_usd']:+.2f} |")
    lines.append(
        f"| **TOTAL** | {card['n']} | {card['wr']:.3f} | {card['breakeven_rate']:.3f} "
        f"| {card['expectancy']:+.3f} | {card['rug_rate']:.3f} | mixed "
        f"| {card['leakage_peak_known_only']['n_round_tripped']} "
        f"| {card['n_peak_known']} | {card['pnl_usd']:+.2f} |")
    leak = card["leakage_peak_known_only"]
    avg = leak["avg_realized_round_trippers"]
    lines.append("")
    lines.append(
        f"Leakage (peak-known closes only, n={card['n_peak_known']}; "
        f"{card['n_peak_unknown']} closes have no recorded peak and are "
        f"excluded): peaked >=2x: {leak['n_peaked_2x']}, round-tripped: "
        f"{leak['n_round_tripped']} (avg realized "
        f"{'n/a' if avg is None else format(avg, '.3f') + 'x'}), "
        f"closed wins: {leak['n_closed_win']}.")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual real-data run
    print(render_markdown(load_all_closes()))
