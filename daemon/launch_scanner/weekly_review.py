"""Weekly paper-trading review.

Reads the backtest dataset (judge replays) and prints a structured report
for the weekly review: what actually happened, which exit reasons / lanes
produced the results, and whether the data is thick enough to tune
anything yet.

Paper only, no network. Run via scripts/weekly-paper-review.

Real paper closes come from the LIVE lane ledgers (USD paper), NOT from
the backtest dataset: dataset paper-close records stopped in mid-October
2026 while the lanes kept closing hundreds of positions a day, so the
dataset paper section reported empty weeks. The dataset still feeds the
judge-replay section only. Lane ledgers cap stored closes at 500 per
lane, so capped lanes' totals count stored closes only and are a floor
for the week, never a full weekly total — the report states each capped
lane's stored window plainly.
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Tuple

DATASET = os.path.expanduser(
    "~/.forex-agent/launch_scanner/backtest_dataset.jsonl")
WEEK_S = 7 * 24 * 3600
MIN_CLOSES_FOR_TUNING = 30
WIN_MULT = 1.5  # realized >= 1.5x counts as a win
# Live entry lanes whose ledgers feed the paper section. Killed lanes
# (mimic family, moon, ceo) are deliberately excluded: their history
# lives in their own verdict reports, not in this weekly count.
LIVE_LANES = ("grad_select", "grad_raw", "tranche", "tranche_protect",
              "raw_tight", "momentum")
LEDGER_CAP = 500  # lane ledgers keep the last 500 closes


def _load() -> List[Dict[str, Any]]:
    recs = []
    if not os.path.exists(DATASET):
        return recs
    with open(DATASET) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue
    return recs


def _week(recs, now):
    return [r for r in recs
            if (r.get("t_close") or r.get("t0") or 0) >= now - WEEK_S]


def _fmt_pct(n, d):
    return "n/a" if not d else "%.1f%%" % (100.0 * n / d)


def _number(value, nonnegative=False):
    if type(value) not in (int,float):return None
    try:
        n=float(value)
        return n if math.isfinite(n) and (not nonnegative or n>=0) else None
    except (OverflowError,ValueError):return None


def _live_ledger_closes(home=None):
    """Read real paper closes from the live lane ledgers (USD paper).

    Returns (records, coverage, missing):
    - records: adapted closes with t_close, symbol, realized_mult,
      pnl_usd, exit_reason (from closed_reason), lane, mint.
    - coverage: lane -> (n_stored, first_close_ts, last_close_ts, skipped)
    - missing: lane names whose ledger could not be read at all.
    A ledger that cannot be read is skipped, never fatal: the section
    reports it instead of dying or silently dropping the lane.
    """
    if home is None:
        home = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser(
            "~/.forex-agent")
    records = []
    coverage = {}
    missing = []
    for lane in LIVE_LANES:
        path = os.path.join(home, "%s_ledger.json" % lane)
        try:
            with open(path) as fh:
                ledger = json.load(fh)
        except Exception:
            missing.append(lane)
            continue
        if not isinstance(ledger,dict) or ('closes' in ledger and not isinstance(ledger['closes'],list)):
            missing.append(lane)
            continue
        closes = ledger.get("closes",[])
        first_ts = None
        last_ts = None
        skipped = 0
        for c in closes:
            if not isinstance(c,dict):
                skipped+=1
                continue
            ts = _number(c.get("close_ts"),True)
            mult = _number(c.get("realized_mult"),True)
            ru=_number(c.get('realized_usd'),True)
            en=_number(c.get('entry_notional_usd'),True)
            if 'realized_mult' not in c and ru is not None and en is not None and en>0:
                mult=_number(ru/en,True)
            if ts is None:
                skipped+=1
                continue
            pnl=_number(c.get('pnl_usd'))
            if 'pnl_usd' not in c and ru is not None and en is not None:
                pnl=_number(ru-en)
            records.append({
                "t_close": ts,
                "symbol": c.get("symbol"),
                "realized_mult": mult,
                "pnl_usd": pnl,
                "exit_reason": c.get("closed_reason") or "?",
                "lane": lane,
                "mint": c.get("mint"),
                "exit_price_kind": (c.get('exit_price_kind') if isinstance(c.get('exit_price_kind'),str)
                                    and c['exit_price_kind'] else 'unknown'),
            })
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        coverage[lane] = (len(closes), first_ts, last_ts, skipped)
    return records, coverage, missing


def _paper_section_live(papers, coverage, missing):
    """Paper section from live lane ledgers. USD, by lane, with the
    stored-window coverage stated up front. An empty result is reported
    as no stored close falling in the window — never as the bot having
    opened no qualifying positions.
    """
    out = ["== REAL PAPER CLOSES (last 7 days, live lane ledgers) =="]
    out.append("source: live lane ledgers (USD paper) | lanes: "
               + ", ".join(LIVE_LANES))
    for lane in LIVE_LANES:
        if lane not in coverage:
            continue
        n_stored, first_ts, last_ts, skipped = coverage[lane]
        if n_stored >= LEDGER_CAP and first_ts is not None:
            out.append(
                "coverage: %s stored closes start %s "
                "(ledger keeps the last %d only) — earlier closes in "
                "the week are not counted; totals are a floor."
                % (lane,
                   time.strftime("%Y-%m-%d %H:%M WAT",
                                 time.localtime(first_ts)),
                   LEDGER_CAP))
        if skipped:
            out.append("coverage: %s skipped %d stored closes "
                       "without a usable close time; economics of retained rows may still be unavailable."
                       % (lane, skipped))
    if missing:
        out.append("note: unreadable ledgers skipped: "
                   + ", ".join(missing))
    if not papers:
        out.append("No closes from the live lane ledgers fell in this "
                   "window.")
        out.append("This counts stored ledger closes only (see coverage "
                   "above) — it does not mean the bot opened nothing.")
        return out
    n = len(papers)
    mults=[_number(r.get('realized_mult'),True) for r in papers]
    pnls=[_number(r.get('pnl_usd')) for r in papers]
    unknown=sum(v is None for v in mults)
    if unknown:
        out.append(f"closes: {n} | return statistics unavailable ({unknown} missing/invalid multiples)")
    else:
        wins=sum(m>=WIN_MULT for m in mults)
        losses=sum(m<1 for m in mults)
        other=n-wins-losses
        avg_mult=math.fsum(m/n for m in mults)
        out.append(f"closes: {n} | wins(>=1.5x): {wins} | losses(<1x): {losses} | flat: {other}")
        out.append(f"win rate: {_fmt_pct(wins,n)} | avg realized: {avg_mult:.3f}x | expectancy: {avg_mult-1:+.3f}x")
    try:
        pnl=math.fsum(pnls) if all(v is not None for v in pnls) else None
    except (OverflowError,ValueError):pnl=None
    if pnl is None or not math.isfinite(pnl):
        out.append('total realized P&L: unavailable (missing/invalid economics or nonfinite aggregate; no subset total)')
    else:
        out.append(f"total realized P&L: {pnl:+.2f} USD (stored closes only)")
    # Stage C visibility (owner-approved 2026-11-22): simulated
    # performance is never presented as qualified. The expectancy
    # above is reproducibility material only.
    try:
        from daemon.launch_scanner import exit_provenance as _pk
    except ImportError:
        try:
            import exit_provenance as _pk  # type: ignore
        except Exception:
            _pk = None
    if _pk is not None:
        _bd = _pk.sample_breakdown(papers)
        out.append("price provenance of stored closes: "
                   + (" | ".join("%s: %d" % (k, v) for k, v in
                                 sorted(_bd["per_kind"].items()))
                      or "none"))
        out.append(
            "qualification: %s "
            "(fill model unvalidated; %d/%d stored closes not "
            "current-price labelled: %d mixed, %d unknown) - "
            "expectancy above is NOT qualified performance."
            % (_pk.QUALIFICATION_STATUS, _bd["unverified"],
               _bd["total"], _bd["mixed"], _bd["unknown"]))
    by_exit = Counter(r.get("exit_reason") or "?" for r in papers)
    out.append("by exit: " + " | ".join(
        f"{k}: {v}" for k, v in by_exit.most_common()))
    by_lane = defaultdict(list)
    for r in papers:by_lane[r.get('lane') or '?'].append(_number(r.get('realized_mult'),True))
    out.append("by lane (trades, win rate, avg x):")
    for lane,ms in sorted(by_lane.items()):
        if any(m is None for m in ms):
            out.append(f"  {lane}: {len(ms)} trades, statistics unavailable")
        else:
            w=sum(m>=WIN_MULT for m in ms)
            out.append(f"  {lane}: {len(ms)} trades, {_fmt_pct(w,len(ms))}, {math.fsum(m/len(ms) for m in ms):.2f}x avg")
    def detail(r):
        m=_number(r.get('realized_mult'),True);p=_number(r.get('pnl_usd'))
        multiple='unavailable' if m is None else str(m)
        pnl='unavailable' if p is None else f'{p:+.2f} USD'
        return f"  {r.get('symbol')} x{multiple} ({r.get('exit_reason')}, pnl {pnl}, {r.get('lane')})"
    if unknown:
        out.append('top/worst ranking unavailable: incomplete return evidence; all records retained')
        out.extend(detail(r) for r in papers)
    else:
        out.append('top closes:')
        out.extend(detail(r) for r in sorted(papers,key=lambda r:r['realized_mult'],reverse=True)[:5])
        out.append('worst closes:')
        out.extend(detail(r) for r in sorted(papers,key=lambda r:r['realized_mult'])[:5])
    return out


def _scout_section() -> List[str]:
    """Scout engine ledger (Phase 3): closes / winners / avg multiple.

    Measurement only — the kill criterion (~14.5x avg winner) is
    reported, never enforced. A missing journal just yields zeros.
    """
    out = ["== SCOUT LEDGER (last 7 days) =="]
    try:
        from storage.store import DEFAULT_DB_PATH  # noqa: PLC0415
        from daemon.launch_scanner import scouts  # noqa: PLC0415
        home = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser(
            "~/.forex-agent")
        fills = os.path.join(home, "data", "memecoin", "fills.jsonl")
        s = scouts.weekly_summary(str(DEFAULT_DB_PATH), fills, days=7.0)
    except Exception:
        out.append("scout ledger unavailable (read failed).")
        return out
    n = int(s.get("closes") or 0)
    if not n:
        out.append("no scout closes this week.")
        return out
    out.append(f"closes: {n} | wins: {s.get('wins')} | "
               f"losses: {s.get('losses')} | pnl: {s.get('pnl_sol'):+.6f} SOL")
    aw = s.get("avg_winner_mult")
    am = s.get("avg_mult")
    out.append("avg winner multiple: %s | avg multiple: %s | kill bar: %.1fx"
               % ("%.2fx" % aw if aw else "n/a",
                  "%.2fx" % am if am else "n/a",
                  float(s.get("kill_bar_mult") or 14.5)))
    out.append("scouts live or die on the ledger — no auto-kill.")
    return out


def _graduation_section() -> List[str]:
    """Graduation filter holds (Phase 5): full-size entries blocked.

    Reads graduation.blocked journal events — the counter the filter
    promises. A missing journal just yields zeros.
    """
    out = ["== GRADUATION FILTER (last 7 days) =="]
    try:
        from storage.store import DEFAULT_DB_PATH  # noqa: PLC0415
        from daemon.launch_scanner import graduation  # noqa: PLC0415
        s = graduation.block_summary(str(DEFAULT_DB_PATH), days=7.0)
    except Exception:
        out.append("graduation block count unavailable (read failed).")
        return out
    n = int(s.get("blocks") or 0)
    if not n:
        out.append("no full-size entries held by the filter this week.")
        return out
    out.append("full-size entries held: %d (scouts unaffected — tiny size bypasses)" % n)
    mints = s.get("mints") or []
    if mints:
        out.append("held mints: %s" % ", ".join(m[:12] for m in mints[:10]))
    return out


def _exit_clock_section() -> List[str]:
    """Exit-liquidity clock (safe batch): run-shape stats.

    Measurement only — median time-to-peak, round-trip rate, median
    dump speed. Honest zeros on an empty dataset.
    """
    out = ["== EXIT-LIQUIDITY CLOCK (last 7 days) =="]
    try:
        from daemon.launch_scanner import exit_liquidity_clock  # noqa: PLC0415
        out.extend(exit_liquidity_clock.weekly_section()[1:])
    except Exception:
        out.append("exit-clock data unavailable (read failed).")
    return out


def _exit_copy_section() -> List[str]:
    """Exit-copying (safe batch): who exited first, us or the wallets.

    Measurement only — ordering + our realized pnl, never fabricated
    counterfactual profits.
    """
    out = ["== EXIT-COPYING (last 7 days) =="]
    try:
        from daemon.launch_scanner import exit_copy  # noqa: PLC0415
        out.extend(exit_copy.weekly_section()[1:])
    except Exception:
        out.append("exit-copy data unavailable (read failed).")
    return out


def _safe_batch2_section() -> List[str]:
    """Safe batch 2 (2026-10-09): second-wallet, copiers, narratives.

    Measurement only — honest zeros until the datasets accumulate.
    """
    out: List[str] = []
    try:
        from daemon.launch_scanner import (  # noqa: PLC0415
            copy_copiers, narrative_cluster, second_wallet)
        out.append("== SECOND-WALLET CLUSTERS (last 7 days) ==")
        out.extend(second_wallet.weekly_section()[1:])
        out.append("")
        out.append("== COPY-THE-COPIERS (last 7 days) ==")
        out.extend(copy_copiers.weekly_section()[1:])
        out.append("")
        from config.config import load_config  # noqa: PLC0415
        try:
            degen = load_config().degen
        except Exception:
            degen = None
        out.append("== NARRATIVE CLUSTERS (heating now) ==")
        out.extend(narrative_cluster.weekly_section(degen)[1:])
    except Exception:
        out.append("safe-batch-2 data unavailable (read failed).")
    return out


def _judge_section(recs: List[Dict[str, Any]]) -> List[str]:
    out = ["== JUDGE REPLAYS (last 7 days, simulated) =="]
    judge = [r for r in recs if r.get("outcome_kind") == "judge"
             or "outcome_kind" not in r]
    if not judge:
        out.append("no replay histories recorded this week.")
        return out
    out.append(f"histories: {len(judge)} | "
               f"verdicts: {dict(Counter(r.get('verdict') for r in judge))}")
    return out


def _verdict_section(all_papers: List[Dict[str, Any]]) -> List[str]:
    out = ["== DATA SUFFICIENCY =="]
    n = len(all_papers)
    out.append(f"stored real closes in live lane ledgers: {n} "
               f"(capped lanes show their last {LEDGER_CAP}; "
               f"tuning threshold: {MIN_CLOSES_FOR_TUNING})")
    unavailable=sum(_number(r.get('realized_mult'),True) is None
                    or _number(r.get('pnl_usd')) is None for r in all_papers)
    if unavailable:
        out.append(f"VERDICT: unavailable economics in {unavailable}/{n} stored closes; no tuning evidence inferred from this count.")
    elif n < 10:
        out.append("VERDICT: too thin — no tuning yet. Keep collecting; "
                   "the gate stays as-is.")
    elif n < MIN_CLOSES_FOR_TUNING:
        out.append("VERDICT: building — directional reads only, "
                   "no threshold changes yet.")
    else:
        out.append("VERDICT: numeric stored-close count meets the legacy minimum, "
                   "not evidence that tuning is justified. Representative chronology, "
                   "coverage, net economics and untouched evaluation remain unverified.")
    return out


def _parse_as_of(argv: List[str]) -> float:
    """Optional --as-of <epoch|ISO datetime> for reproducible dry runs.

    Defaults to now. An ISO value without a timezone is read in local
    time, matching the report header's localtime rendering.
    """
    now = time.time()
    if "--as-of" not in argv:
        return now
    i = argv.index("--as-of")
    if i + 1 >= len(argv):
        return now
    v = argv[i + 1]
    try:
        return float(v)
    except ValueError:
        pass
    try:
        import datetime  # noqa: PLC0415
        return datetime.datetime.fromisoformat(v).timestamp()
    except Exception:
        return now


def main(argv: List[str]) -> int:
    now = _parse_as_of(argv)
    recs = _load()
    week = _week(recs, now)
    live, coverage, missing = _live_ledger_closes()
    papers_week = [r for r in live
                   if (r.get("t_close") or 0) >= now - WEEK_S
                   and (r.get("t_close") or 0) <= now]
    papers_all = live
    lines = []
    lines.append("WEEKLY PAPER REVIEW")
    lines.append(time.strftime("week ending %Y-%m-%d %H:%M WAT",
                               time.localtime(now)))
    lines.append("")
    lines.extend(_paper_section_live(papers_week, coverage, missing))
    lines.append("")
    lines.extend(_scout_section())
    lines.append("")
    lines.extend(_graduation_section())
    lines.append("")
    lines.extend(_exit_clock_section())
    lines.append("")
    lines.extend(_exit_copy_section())
    lines.append("")
    lines.extend(_safe_batch2_section())
    lines.append("")
    lines.extend(_judge_section(week))
    lines.append("")
    lines.extend(_verdict_section(papers_all))
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
