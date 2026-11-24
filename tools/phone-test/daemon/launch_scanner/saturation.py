#!/usr/bin/env python3
"""Ticker saturation: how many pump.fun coins used this ticker today.

Idea adopted 2026-10-21 from the GitHub ecosystem sweep (gem-search's
Narrative Hunter): when a narrative pops, the ticker gets farmed —
clones launch within the hour, and the 7th $PEPE of the day is a very
different gamble from the 1st. The card line is display-only: it never
gates, scores, or delays a card materially, and it fails soft to ""
on ANY error (API down, rate-limited, garbage payload).

Counting design (why it looks like this):
The pump.fun frontend API (frontend-api-v3) has NO working symbol
search — ``searchTerm`` is silently ignored and there is no /search
route (verified live 2026-10-21). A full-day census by paging is also
impossible at card time (~35k launches/day, ~70-100 coins/page). So
this module keeps a persistent daily tally with a watermark: each
refresh pages the newest-first launch list only back to the last
seen timestamp and counts the delta, bucketing every coin by the UTC
date of its own ``created_timestamp``. Refreshes are lazy (card time),
throttled, and flock-guarded across the feed processes.

Honest limits:
- Counts are exact only from when tracking first started; the first
  partial day undercounts (cold start backfills MAX_PAGES pages —
  roughly 15-25 minutes of firehose — then accrues exactly).
- If no card fires for longer than the page cap covers (~25-35 min
  of firehose), the refresh jumps the watermark forward and the gap
  window is simply uncounted — the tally records ``gap_jumps``.
- The card's own coin is counted when a refresh pages over its
  creation window, which is the normal case while the tally is live.

Free-only: pump.fun public frontend API, stdlib + curl (the VM's
urllib-through-proxy path intermittently truncates large responses —
the same quirk sources.py documents — so curl goes first).
"""
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request

logger = logging.getLogger("forex_agent.daemon.launch_scanner.saturation")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
STATE_PATH = os.path.join(HOME, "ticker_saturation.json")

_API = ("https://frontend-api-v3.pump.fun/coins?offset=%d&limit=%d"
        "&sort=created_timestamp&order=DESC&includeNsfw=true")
PAGE_LIMIT = 100          # API effectively returns ~70-100/page
MAX_PAGES = 6             # per refresh: ~25-35 min of firehose
PAGE_TIMEOUT_S = 2.5      # per page, curl --max-time / urllib timeout
REFRESH_DEADLINE_S = 6.0  # whole-refresh budget at card time
REFRESH_MIN_INTERVAL_S = 45   # file-level: another process just did it
CACHE_TTL_S = 600         # per-symbol in-process cache (>= 10 min)
FAIL_TTL_S = 60           # after a failed refresh, don't retry at once
_WM_MINTS_CAP = 2000      # boundary dedupe set ceiling

_cache = {}               # sym -> (epoch, count)
_last_fail = 0.0


def _norm(symbol):
    """Canonical ticker: upper, no leading $, trimmed. Applied to both
    census symbols and query symbols so the two sides always agree."""
    return str(symbol or "").strip().upper().lstrip("$").strip()


def _ordinal(n):
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "%d%s" % (n, suf)


def format_line(count, symbol):
    """The card line. Pure. "" below 2 (a first-of-day ticker is not a
    story); ordinal 'farmed' wording from 5 up."""
    sym = _norm(symbol)
    if not sym or not count or count < 2:
        return ""
    if count >= 5:
        return "🎭 %s $%s today — narrative getting farmed" % (
            _ordinal(count), sym)
    return "🎭 %d coins named $%s today" % (count, sym)


# ---- transport -----------------------------------------------------------

def _fetch_page(offset, limit):
    """One page of the newest-first launch list. None on any failure."""
    url = _API % (offset, limit)
    raw = None
    curl = shutil.which("curl")
    if curl:
        try:
            out = subprocess.run(
                [curl, "-sS", "--compressed", "--max-time",
                 str(PAGE_TIMEOUT_S), "-H", "User-Agent: Mozilla/5.0",
                 url], capture_output=True, timeout=PAGE_TIMEOUT_S + 3)
            if out.returncode == 0 and out.stdout:
                raw = out.stdout
        except Exception:
            raw = None
    if raw is None:
        try:
            import gzip
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0",
                              "Accept-Encoding": "gzip"})
            with urllib.request.urlopen(req,
                                        timeout=PAGE_TIMEOUT_S) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
        except Exception:
            return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None
    return data if isinstance(data, list) else None


# ---- persistent tally ----------------------------------------------------

def _empty_state():
    return {"wm_ts": 0, "wm_mints": [], "days": {}, "gap_jumps": 0,
            "updated": 0.0}


def _load_state():
    try:
        with open(STATE_PATH) as f:
            d = json.load(f)
        if isinstance(d, dict) and "days" in d:
            s = _empty_state()
            s.update(d)
            return s
    except Exception:
        pass
    return _empty_state()


def _save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def _day_key(ts_ms):
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000.0))


def _refresh(state):
    """Page the delta since the watermark and count it into the tally.

    Mutates ``state``; returns True when the watermark advanced (or was
    already current). On a fetch failure the state is left untouched
    and False is returned — the caller keeps serving stale counts.
    """
    wm_ts = int(state.get("wm_ts") or 0)
    boundary = set(state.get("wm_mints") or [])
    deadline = time.time() + REFRESH_DEADLINE_S
    collected = []
    collected_mints = set()
    seen_ts_max = wm_ts
    seen_at_max = set(boundary) if wm_ts else set()
    offset = 0
    reached_wm = wm_ts == 0
    pages_ok = 0
    for _ in range(MAX_PAGES):
        if time.time() > deadline:
            break
        try:
            page = _fetch_page(offset, PAGE_LIMIT)
        except Exception:
            page = None
        if page is None:
            # A failed page discards the refresh ONLY when nothing was
            # learned yet. After >= 1 good page, apply what we have:
            # the watermark still advances to the newest coin seen and
            # the uncounted window is recorded as a gap (this API
            # rate-limits intermittently — all-or-nothing refreshes
            # would starve the tally).
            if pages_ok == 0:
                logger.warning("saturation: first page fetch failed; "
                               "keeping stale tally")
                return False
            logger.warning("saturation: page fetch failed after %d "
                           "page(s); applying partial delta", pages_ok)
            break
        pages_ok += 1
        if not page:
            reached_wm = True
            break
        page_ts = []
        for coin in page:
            if not isinstance(coin, dict):
                continue
            try:
                cts = int(coin.get("created_timestamp") or 0)
            except (TypeError, ValueError):
                continue
            if cts <= 0:
                continue
            page_ts.append(cts)
            mint = str(coin.get("mint") or "")
            if cts > seen_ts_max:
                seen_ts_max = cts
                seen_at_max = {mint} if mint else set()
            elif cts == seen_ts_max and mint:
                seen_at_max.add(mint)
            if (cts > wm_ts or (cts == wm_ts and mint not in boundary)) \
                    and mint not in collected_mints:
                collected.append((cts, coin.get("symbol")))
                if mint:
                    collected_mints.add(mint)
        if page_ts and min(page_ts) < wm_ts:
            reached_wm = True
            break
        # NOTE: no "short page = end of data" stop — the API returns
        # ~70 coins even when asked for 100, so a short page is a FULL
        # page. Paging stops on an empty page, on crossing the
        # watermark, or at MAX_PAGES.
        offset += len(page)
    days = state.setdefault("days", {})
    for cts, sym in collected:
        s = _norm(sym)
        if not s:
            continue
        bucket = days.setdefault(_day_key(cts), {})
        bucket[s] = int(bucket.get(s) or 0) + 1
    if not reached_wm:
        state["gap_jumps"] = int(state.get("gap_jumps") or 0) + 1
        logger.warning("saturation: refresh gap-jumped the watermark "
                       "(uncounted window); gap_jumps=%d",
                       state["gap_jumps"])
    state["wm_ts"] = seen_ts_max
    state["wm_mints"] = sorted(seen_at_max)[:_WM_MINTS_CAP]
    # Keep today + yesterday only.
    keep = {_day_key(int(time.time() * 1000)),
            _day_key(int(time.time() * 1000) - 86400000)}
    state["days"] = {k: v for k, v in days.items() if k in keep}
    state["updated"] = time.time()
    return True


def _refresh_if_stale():
    """Refresh under an inter-process flock when the tally is stale.

    Returns the freshest state available (possibly stale on failure).
    """
    global _last_fail
    state = _load_state()
    if time.time() - float(state.get("updated") or 0) \
            < REFRESH_MIN_INTERVAL_S:
        return state
    if time.time() - _last_fail < FAIL_TTL_S:
        return state
    import fcntl
    try:
        lf = open(STATE_PATH + ".lock", "w")
    except Exception:
        return state
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        state = _load_state()  # re-read under the lock
        if time.time() - float(state.get("updated") or 0) \
                < REFRESH_MIN_INTERVAL_S:
            return state
        if _refresh(state):
            try:
                _save_state(state)
            except Exception as e:
                logger.warning("saturation: state save failed: %s", e)
        else:
            _last_fail = time.time()
        return state
    except Exception as e:
        logger.warning("saturation: refresh failed: %s", e)
        return state
    finally:
        try:
            lf.close()
        except Exception:
            pass


# ---- public API ----------------------------------------------------------

def today_count(symbol):
    """Coins with this ticker launched today (UTC), per the tally.

    Never raises; 0 means none counted OR unknown (API dark) — callers
    treat both the same way: no line on the card.
    """
    sym = _norm(symbol)
    if not sym:
        return 0
    try:
        hit = _cache.get(sym)
        if hit and time.time() - hit[0] < CACHE_TTL_S:
            return hit[1]
        state = _refresh_if_stale()
        count = int((state.get("days") or {}).get(
            _day_key(int(time.time() * 1000)), {}).get(sym) or 0)
        _cache[sym] = (time.time(), count)
        return count
    except Exception:
        return 0


def saturation_line(symbol):
    """Pre-rendered card line, or "" — the fail-soft contract."""
    try:
        return format_line(today_count(symbol), symbol)
    except Exception:
        return ""
