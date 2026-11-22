#!/usr/bin/env python3
"""Birth dossiers (2026-10-23, P8 — scoped).

The Phase-1 design principle: do the homework DURING the bonding
curve, not at graduation. When the PumpPortal stream reports a
birth that passes a cheap payload filter (meaningful dev buy or
starting market cap), a background worker opens a dossier file at
~/.forex-agent/dossiers/<mint>.json and precomputes:

  * instant safety — safety.check_token (RugCheck report, else the
    instant_safety on-chain fallback), the exact function the grad
    lanes call at entry;
  * deployer — the creating wallet from the birth event, plus how
    many dossiers that deployer has opened in the TTL window;
  * holder-velocity proxy — bonding-curve virtual SOL reserve
    samples (open, then re-sampled on the mint's migration event):
    v_sol growth IS the curve-phase demand curve, readable free.

At migration, the grad lanes READ the dossier: a fresh, conclusive
safety result is reused instead of recomputed; a miss, a stale
dossier, or an inconclusive (unknown) result falls back to the
current live behavior EXACTLY — gates are unchanged, this is a pure
accelerator. TTL: dossiers are deleted 24h after birth; cleanup runs
opportunistically inside the event handlers (no standing timer).

Caps keep the precompute honest: at most MAX_OPEN_DOSSIERS live
dossiers and MAX_PER_DAY new ones per day — the stream sees ~30k
births/day and precomputing all of them would be waste, not signal.
Never raises.
"""
import contextlib
import fcntl
import json
import logging
import os
import queue
import threading
import time

try:
    from daemon.launch_scanner import safety as _safety
    from daemon.launch_scanner import reserves as _res
except Exception:  # pragma: no cover - script-style import path
    try:
        import safety as _safety  # type: ignore
        import reserves as _res  # type: ignore
    except Exception:
        _safety = None  # type: ignore
        _res = None  # type: ignore

logger = logging.getLogger("forex_agent.daemon.launch_scanner.dossiers")

HOME = os.environ.get("FOREX_AGENT_HOME", "/home/hatch/.forex-agent")
DOSSIER_DIR = os.path.join(HOME, "dossiers")

TTL_S = 24 * 3600
SAFETY_FRESH_S = 1200.0     # reuse window for precomputed safety
MAX_OPEN_DOSSIERS = 300
MAX_PER_DAY = 400
MIN_INITIAL_BUY_SOL = 1.0   # cheap birth filter: real dev conviction
MIN_MARKET_CAP_SOL = 30.0   # ...or a starting cap that matters

_lock = threading.Lock()
_queue = queue.Queue(maxsize=500)
_worker = None
_day = {"date": "", "count": 0}
_last_sweep = 0.0


def _path(mint):
    return os.path.join(DOSSIER_DIR, mint + ".json")


def _write(mint, d):
    try:
        os.makedirs(DOSSIER_DIR, exist_ok=True)
        tmp = _path(mint) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, _path(mint))
        return True
    except Exception:
        return False


def read_dossier(mint, now=None):
    """The dossier dict for a mint, or None (miss / expired /
    unreadable). Never raises."""
    try:
        now = time.time() if now is None else now
        with open(_path(mint)) as f:
            d = json.load(f)
        if now - float(d.get("born_ts") or 0) > TTL_S:
            return None
        return d
    except Exception:
        return None


def read_safety(mint, now=None):
    """A safety.check_token-shaped report from the dossier when it
    is fresh AND conclusive; None otherwise (caller falls back to
    the live check — behavior identical on any doubt)."""
    try:
        now = time.time() if now is None else now
        d = read_dossier(mint, now)
        if not d:
            return None
        s = d.get("safety") or {}
        rep = s.get("report")
        if not isinstance(rep, dict) or rep.get("unknown"):
            return None
        if now - float(s.get("ts") or 0) > SAFETY_FRESH_S:
            return None
        out = dict(rep)
        out["source"] = "dossier:" + str(rep.get("source") or "")
        return out
    except Exception:
        return None


def _birth_filter(item):
    """Cheap payload gate for opening a dossier. Pure."""
    try:
        extra = (item or {}).get("extra") or {}
        return float(extra.get("initial_buy_sol") or 0) \
            >= MIN_INITIAL_BUY_SOL or \
            float(extra.get("market_cap_sol") or 0) \
            >= MIN_MARKET_CAP_SOL
    except Exception:
        return False


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _sweep(now):
    """Opportunistic TTL cleanup + deployer census. Runs inside
    event handlers at most once per 300s of wall time."""
    global _last_sweep
    if now - _last_sweep < 300:
        return
    _last_sweep = now
    try:
        os.makedirs(DOSSIER_DIR, exist_ok=True)
        for fn in os.listdir(DOSSIER_DIR):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(DOSSIER_DIR, fn)
            try:
                with open(p) as f:
                    d = json.load(f)
                if now - float(d.get("born_ts") or 0) > TTL_S:
                    os.unlink(p)
            except Exception:
                continue
        # Lever 3 (2026-11-16): clean per-mint lock files whose
        # dossier is gone/expired. Only unlink when no builder
        # holds the lock (flock EX|NB succeeds) — a held lock
        # means a build is in flight right now. The narrow race
        # (a builder opening the path just as it is unlinked)
        # can only drop one best-effort dossier, never corrupt
        # one: dossiers are accelerators, a miss falls back to
        # the live check.
        for fn in os.listdir(DOSSIER_DIR):
            if not fn.endswith(".lock"):
                continue
            mint = fn[:-5]
            p = os.path.join(DOSSIER_DIR, fn)
            try:
                stale = True
                jp = os.path.join(DOSSIER_DIR, mint + ".json")
                if os.path.exists(jp):
                    with open(jp) as f:
                        d = json.load(f)
                    stale = now - float(d.get("born_ts") or 0) > TTL_S
                if not stale:
                    continue
                fd = os.open(p, os.O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (BlockingIOError, OSError):
                    os.close(fd)
                    continue
                try:
                    os.unlink(p)
                finally:
                    os.close(fd)
            except Exception:
                continue
    except Exception:
        pass


def _count_open(now):
    try:
        n = 0
        for fn in os.listdir(DOSSIER_DIR):
            if fn.endswith(".json"):
                n += 1
        return n
    except Exception:
        return MAX_OPEN_DOSSIERS  # unknown -> fail closed on volume


def _deployer_count(deployer, now):
    n = 0
    try:
        for fn in os.listdir(DOSSIER_DIR):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(DOSSIER_DIR, fn)) as f:
                    d = json.load(f)
                if d.get("deployer") == deployer:
                    n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


def _intel_fields(deployer):
    """Display-only intel surfacing (2026-10-23): serial-rugger
    summary + funding cluster for the deployer, read from the
    measurement registries (rugger_registry / funding_clusters).

    These fields are NEVER consulted by the safety precompute or
    any verdict — they exist so a human reading the dossier sees
    the deployer's record. Missing/corrupt registries -> fields
    omitted. Never raises."""
    out = {}
    try:
        if not deployer:
            return out
        try:
            from daemon.launch_scanner import rugger_registry as _rr
            from daemon.launch_scanner import funding_clusters as _fc
        except Exception:
            import rugger_registry as _rr  # type: ignore
            import funding_clusters as _fc  # type: ignore
        try:
            r = _rr.lookup(deployer)
            if r and int(r.get("launches") or 0) > 0:
                out["deployer_rugger"] = {
                    "flagged": bool(r.get("flagged")),
                    "launches": int(r.get("launches") or 0),
                    "dead": int(r.get("dead") or 0),
                }
        except Exception:
            pass
        try:
            c = _fc.cluster_of(deployer)
            if c:
                out["funding_cluster"] = {
                    "cluster_id": c.get("cluster_id"),
                    "size": int(c.get("size") or 0),
                }
        except Exception:
            pass
    except Exception:
        return {}
    return out


@contextlib.contextmanager
def _mint_claim(mint):
    """Cross-process claim for one mint's dossier build (Lever 3,
    2026-11-16). Four trader processes race the same births; without
    a claim, all four build the same dossier (up to ~6 Helius calls
    each). Yields True when this process may build (lock held, or
    locking unavailable — fail-open so a build is never lost to a
    lock error), False when another process is already building this
    mint. flock releases on process death, so a crash cannot wedge
    the mint. Never raises."""
    fd = None
    try:
        os.makedirs(DOSSIER_DIR, exist_ok=True)
        fd = os.open(os.path.join(DOSSIER_DIR, mint + ".lock"),
                     os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        yield False
        return
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        yield True
        return
    try:
        yield True
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def _build(item):
    """Worker body: create one dossier (safety precompute is the
    slow part, ~seconds). Claims the mint cross-process first so
    four racing traders build it once, and skips when another
    process already built it. Never raises."""
    try:
        mint = str(item.get("mint") or "")
        if not mint:
            return
        with _mint_claim(mint) as claimed:
            if not claimed:
                return
            if read_dossier(mint):
                return
            _build_inner(item)
    except Exception as e:
        logger.warning("dossier build failed: %s", e)


def _build_inner(item):
    """Build one dossier file (called once per mint, under the
    cross-process claim). Never raises."""
    try:
        mint = str(item.get("mint") or "")
        if not mint:
            return
        extra = item.get("extra") or {}
        now = time.time()
        d = {
            "mint": mint,
            "symbol": str(item.get("symbol") or "?")[:32],
            "born_ts": float(item.get("created_ts") or now),
            "opened_ts": now,
            "deployer": str(extra.get("trader") or ""),
            "bonding_curve": str(extra.get("bonding_curve") or ""),
            "initial_buy_sol": float(extra.get("initial_buy_sol")
                                     or 0),
            "market_cap_sol": float(extra.get("market_cap_sol")
                                    or 0),
            "safety": {"ts": 0.0, "report": None},
            "curve_samples": [],
            "migrated_ts": 0.0,
        }
        if _res is not None:
            # Lever 3 (2026-11-16): curve_price_usd's return value
            # was discarded — it re-fetched this same curve account
            # (plus a mint-decimals fetch) for nothing. The single
            # get_account below serves the sample.
            _o, data = _res.get_account(
                _res.derive_curve_pda(mint) or "")
            c = _res.parse_bonding_curve(data)
            if c:
                d["curve_samples"].append(
                    {"ts": now, "v_sol": c["v_sol"],
                     "v_token": c["v_token"]})
        _write(mint, d)
        rep = None
        if _safety is not None:
            try:
                rep = _safety.check_token("solana", mint, timeout=10)
            except Exception:
                rep = None
        d2 = read_dossier(mint) or d
        d2["safety"] = {"ts": time.time(),
                        "report": rep if isinstance(rep, dict)
                        else None}
        if d2["deployer"]:
            d2["deployer_dossiers_live"] = _deployer_count(
                d2["deployer"], time.time())
            # Display-only intel (never part of the safety verdict).
            try:
                d2.update(_intel_fields(d2["deployer"]))
            except Exception:
                pass
        _write(mint, d2)
    except Exception as e:
        logger.warning("dossier build failed: %s", e)


def _worker_loop():
    while True:
        try:
            item = _queue.get()
        except Exception:
            return
        if item is None:
            return
        _build(item)


def _ensure_worker():
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop,
                                       name="dossier-build",
                                       daemon=True)
            _worker.start()


def observe_births(items, now=None):
    """Feed drained PumpPortal items; births passing the cheap
    filter get a dossier built in the background. Non-blocking;
    returns how many were queued. Never raises."""
    queued = 0
    try:
        now = time.time() if now is None else now
        _sweep(now)
        for item in items or []:
            try:
                if (item or {}).get("source") != "pumpportal-new":
                    continue
                mint = str(item.get("mint") or "")
                if not mint or read_dossier(mint, now):
                    continue
                if not _birth_filter(item):
                    continue
                day = _today()
                with _lock:
                    if _day["date"] != day:
                        _day.update(date=day, count=0)
                    if _day["count"] >= MAX_PER_DAY:
                        break
                    if _count_open(now) >= MAX_OPEN_DOSSIERS:
                        break
                    _day["count"] += 1
                _ensure_worker()
                try:
                    _queue.put_nowait(item)
                    queued += 1
                except queue.Full:
                    break
            except Exception:
                continue
    except Exception:
        pass
    return queued


def observe_migrations(items, now=None):
    """On a mint's migration event: stamp the dossier and take a
    final curve sample set (velocity = v_sol growth over the curve
    phase). Never raises."""
    try:
        now = time.time() if now is None else now
        for item in items or []:
            try:
                if (item or {}).get("source") \
                        != "pumpportal-migration":
                    continue
                mint = str(item.get("mint") or "")
                d = read_dossier(mint, now)
                if not d:
                    continue
                d["migrated_ts"] = now
                if _res is not None:
                    _o, data = _res.get_account(
                        _res.derive_curve_pda(mint) or "")
                    c = _res.parse_bonding_curve(data)
                    if c:
                        d.setdefault("curve_samples", []).append(
                            {"ts": now, "v_sol": c["v_sol"],
                             "v_token": c["v_token"]})
                samples = d.get("curve_samples") or []
                if len(samples) >= 2:
                    dt = samples[-1]["ts"] - samples[0]["ts"]
                    if dt > 0:
                        d["curve_velocity_sol_s"] = round(
                            (samples[-1]["v_sol"]
                             - samples[0]["v_sol"]) / 1e9 / dt, 6)
                _write(mint, d)
            except Exception:
                continue
    except Exception:
        pass
