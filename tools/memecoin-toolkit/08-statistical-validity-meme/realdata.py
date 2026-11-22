"""Real-data loader for the statistical validity lab.

Reads the live paper bot's recorded closes (READ-ONLY) and answers this
toolkit's questions on real numbers instead of synthetic ones:

  * expectancy, win rates, rug rate -- overall and per lane/source
  * required_n to distinguish the observed win rate from a 30% null,
    at this toolkit's existing convention (``statslab.required_n``:
    one-sided, alpha 0.05, power 0.8)

Sources (all optional -- a missing file is skipped, never invented):

  * ``backtest_dataset.jsonl`` -- journaled paper closes are the records
    with ``outcome_kind == "paper"``. Each record is one journaled close
    row (two-tier positions journal one row per leg), carrying
    ``realized_mult``, ``peak_mult``, a recorded ``rug`` flag,
    ``exit_reason``, ``source`` and ``leg``.
  * ``degen_mirror_ledger.json`` / ``ceo_ledger.json`` -- $1-shot lane
    ledgers (``closes`` list). Mirror closes record ``pnl_usd`` and
    per-leg ``realized_usd`` but no ``realized_mult``; the multiple is
    reconstructed as sum(leg realized_usd) / $1 notional (the mirror is
    $1/shot by construction; open positions confirm notional_usd == 1.0,
    and the reconstruction cross-checks against 1 + pnl_usd). CEO closes
    record ``realized_mult`` directly. Unquotable mirror closes record
    exit price 0.0 with per-leg realized ~$0 minus fees and pnl ~-$1.001;
    both routes reconstruct to ~-0.001x, kept as recorded (see
    ``n_negative_multiples`` in the summary).
  * ``steady_ledger.json`` -- ``closed`` list with ``pnl`` dollars on
    ``size_usd``: realized_mult = 1 + pnl / size_usd;
    peak_mult = peak_price / entry_price.
  * ``stacker_ledger.json`` -- closes live inside ``positions``: a
    position is a close iff ``frac_remaining == 0`` and it has an
    ``exit_ts``; realized_mult = 1 + realized_pnl / size_usd.

Conventions (fixed here, do not silently move them):

  * expectancy = mean(realized_mult) - 1, via ``statslab.expectancy``
    -- profit per $1 staked.
  * WIN (headline) = realized_mult >= 1.5, matching toolkit 13's fixed
    definition so the labs agree. Also reported: share > 1.0 (this
    toolkit's own ``statslab.win_rate`` -- breakeven is not a win) and
    share >= 1.0.
  * RUG: the recorded ``rug`` flag where the source records one (the
    paper dataset does). Lane ledgers record no rug flag, so there a
    close counts as a rug iff realized_mult <= 0.05 (a near-total loss)
    -- a documented proxy, flagged per-group via ``rug_basis`` so a
    proxy rate is never passed off as a recorded one.
  * required_n: ``statslab.required_n(p1=observed WR, p0=0.30)``. The
    toolkit convention requires p1 > p0; when the observed WR is at or
    below the 30% null there is no edge to power for, so the result is
    ``None`` with an explanatory ``required_n_note`` instead of a
    number. That is a finding, not an error.

Privacy: loaders whitelist fields. Wallet addresses, wallet names /
grades, registry and follower data are never copied into outputs --
only public token mints, lane/source labels and outcome numbers.
"""
from __future__ import annotations

import json
import os

from statslab import expectancy as _expectancy
from statslab import required_n as _required_n
from statslab import wilson_interval
from statslab import win_rate as _statslab_win_rate

RUG_NEAR_ZERO = 0.05
NULL_WR = 0.30

DEFAULT_PATHS = {
    "dataset": "/home/hatch/.forex-agent/launch_scanner/backtest_dataset.jsonl",
    "mirror": "/home/hatch/.forex-agent/degen_mirror_ledger.json",
    "ceo": "/home/hatch/.forex-agent/ceo_ledger.json",
    "steady": "/home/hatch/.forex-agent/steady_ledger.json",
    "stacker": "/home/hatch/.forex-agent/stacker_ledger.json",
}


def _is_rug(realized_mult: float, rug_flag) -> bool:
    """Recorded flag wins; otherwise the near-zero proxy applies."""
    if rug_flag is not None:
        return bool(rug_flag)
    return realized_mult <= RUG_NEAR_ZERO


def _canonical(lane, mint, realized_mult, peak_mult, exit_reason,
               rug_flag, source=""):
    realized = float(realized_mult)
    return {
        "lane": lane,
        "mint": mint,
        "realized_mult": realized,
        "peak_mult": (None if peak_mult is None else float(peak_mult)),
        "exit_reason": exit_reason,
        "was_rug": _is_rug(realized, rug_flag),
        "rug_basis": "recorded" if rug_flag is not None else "proxy",
        "source": source,
    }


# ------------------------------------------------------------------ loaders

def iter_dataset_paper_closes(path: str):
    """Yield canonical closes from a backtest_dataset.jsonl stream.

    Streams line by line (the file is large) and keeps only
    ``outcome_kind == "paper"`` records with a recorded realized
    multiple. The lane label is ``paper`` (the dataset's own closes);
    the recorded ``source`` is preserved for per-source breakdowns.
    """
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("outcome_kind") != "paper":
                continue
            if rec.get("realized_mult") is None:
                continue
            yield _canonical(
                lane="paper",
                mint=rec.get("mint", ""),
                realized_mult=rec["realized_mult"],
                peak_mult=rec.get("peak_mult"),
                exit_reason=rec.get("exit_reason", ""),
                rug_flag=rec.get("rug"),
                source=rec.get("source") or "(blank)",
            )


def _mirror_style_closes(data: dict, default_lane: str):
    """Canonical closes from a mirror/CEO-style ledger dict."""
    out = []
    for rec in data.get("closes", []):
        if rec.get("realized_mult") is not None:
            realized = float(rec["realized_mult"])
        else:
            legs = rec.get("legs") or {}
            leg_sum = sum(
                float(leg.get("realized_usd") or 0.0)
                for leg in legs.values()
                if isinstance(leg, dict)
            )
            # $1 notional by construction; fall back to pnl if no legs.
            realized = leg_sum if legs else 1.0 + float(rec.get("pnl_usd") or 0.0)
        out.append(_canonical(
            lane=rec.get("lane") or default_lane,
            mint=rec.get("mint", ""),
            realized_mult=realized,
            peak_mult=None,  # these ledgers record no peak multiple
            exit_reason=rec.get("closed_reason", ""),
            rug_flag=None,
            source=rec.get("source", ""),
        ))
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
        out.append(_canonical(
            lane="steady",
            mint=rec.get("mint", ""),
            realized_mult=1.0 + float(rec.get("pnl") or 0.0) / size,
            peak_mult=peak,
            exit_reason=rec.get("exit_reason", ""),
            rug_flag=None,
            source="steady",
        ))
    return out


def load_stacker_closes(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    out = []
    for rec in data.get("positions", []):
        # Only fully-exited positions are closes; open ones are skipped.
        if rec.get("frac_remaining") != 0.0 or not rec.get("exit_ts"):
            continue
        size = float(rec.get("size_usd") or 1.0)
        entry = float(rec.get("entry_price") or 0.0)
        peak = (float(rec["peak_price"]) / entry) if entry and rec.get("peak_price") else None
        out.append(_canonical(
            lane="stacker",
            mint=rec.get("mint", ""),
            realized_mult=1.0 + float(rec.get("realized_pnl") or 0.0) / size,
            peak_mult=peak,
            exit_reason=rec.get("exit_reason", ""),
            rug_flag=None,
            source="stacker",
        ))
    return out


def load_all_closes(paths: dict | None = None) -> list[dict]:
    """Every real close from whichever sources exist, in source order:
    dataset paper closes, mirror, CEO, steady, stacker."""
    paths = {**DEFAULT_PATHS, **(paths or {})}
    closes: list[dict] = []
    if os.path.exists(paths["dataset"]):
        closes.extend(iter_dataset_paper_closes(paths["dataset"]))
    for key, loader in (("mirror", load_mirror_closes), ("ceo", load_ceo_closes),
                        ("steady", load_steady_closes), ("stacker", load_stacker_closes)):
        if os.path.exists(paths[key]):
            closes.extend(loader(paths[key]))
    return closes


# ------------------------------------------------------------------ summary

def summarise_closes(closes: list[dict]) -> dict:
    """Toolkit-08 statistics for one group of canonical closes.

    Empty group -> n 0 with None statistics (fail-closed: no invented
    numbers). ``required_n`` follows the module docstring: None, with
    a note, when the observed >=1.5x win rate does not exceed the 30%
    null.
    """
    if not closes:
        return {"n": 0, "expectancy": None, "mean_mult": None,
                "n_negative_multiples": 0,
                "wr_ge_1_5": None, "wr_gt_1": None, "share_ge_1_0": None,
                "rug_rate": None, "rug_basis": None,
                "wilson95_wr_ge_1_5": None, "required_n": None,
                "required_n_note": "no closes"}
    mults = [c["realized_mult"] for c in closes]
    n = len(mults)
    n_negative = sum(1 for m in mults if m < 0)
    if n_negative:
        # statslab validates multiples >= 0 (a long spot position
        # cannot return less than -100%). At least one real ledger
        # records costs beyond the stake (a stacker close at -0.05x),
        # so fall back to the identical arithmetic and flag it rather
        # than crash or silently clamp the recorded value.
        exp = sum(mults) / n - 1.0
        wr_gt_1 = sum(1 for m in mults if m > 1.0) / n
    else:
        exp = _expectancy(mults)
        wr_gt_1 = _statslab_win_rate(mults)
    wins15 = sum(1 for m in mults if m >= 1.5)
    wr15 = wins15 / n
    if wr15 > NULL_WR:
        req = _required_n(wr15, NULL_WR)
        note = "closes needed to distinguish the observed WR from a 30% null"
    else:
        req = None
        note = (f"observed WR {wr15:.3f} does not exceed the 30% null; "
                "statslab.required_n requires p1 > p0 -- there is no "
                "edge here to power for")
    bases = {c["rug_basis"] for c in closes}
    return {
        "n": n,
        "expectancy": exp,
        "mean_mult": sum(mults) / n,
        "n_negative_multiples": n_negative,
        "wr_ge_1_5": wr15,
        "wr_gt_1": wr_gt_1,
        "share_ge_1_0": sum(1 for m in mults if m >= 1.0) / n,
        "rug_rate": sum(1 for c in closes if c["was_rug"]) / n,
        "rug_basis": bases.pop() if len(bases) == 1 else "mixed",
        "wilson95_wr_ge_1_5": wilson_interval(wins15, n),
        "required_n": req,
        "required_n_note": note,
    }


def full_report(closes: list[dict]) -> dict:
    """Overall summary plus per-lane and (for paper) per-source groups."""
    report = {"overall": summarise_closes(closes), "per_lane": {}, "per_source": {}}
    for lane in sorted({c["lane"] for c in closes}):
        report["per_lane"][lane] = summarise_closes(
            [c for c in closes if c["lane"] == lane])
    paper = [c for c in closes if c["lane"] == "paper"]
    for source in sorted({c["source"] for c in paper}):
        report["per_source"][source] = summarise_closes(
            [c for c in paper if c["source"] == source])
    return report


if __name__ == "__main__":  # pragma: no cover - manual real-data run
    print(json.dumps(full_report(load_all_closes()), indent=2))
