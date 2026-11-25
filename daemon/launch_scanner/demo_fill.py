"""Demo fills from the Jupiter-quote simulation, for the supervised lanes.

The lanes used to fill through fills.honest_entry/honest_exit (a model, no quote).
This seam asks broker.memecoin.execution.SimulationExecutor (REAL quote client, simulated
settlement, NO signing, NO submission) for the fill and returns USD-denominated actuals that
the lane books into the ONE shared paper account. The shared account stays the sole economic
authority: the executor runs against a throwaway per-call SOL/token book that exists only so the
executor's own insolvency check passes; nothing is read back from it except the measured fill.

Rules:
  * One fill per op id, journaled durably BEFORE the caller sees it. A replay (crash/retry/restart)
    returns the ORIGINAL economics; it never re-quotes or re-rolls the simulated move.
  * Quote the actual configured notional in lamports: amount = notional/sol_usd - fees, so that
    volume + fee == the notional exactly (the account's exact-size rule holds).
  * Unknown fx, unknown decimals or an unset price-impact cap refuse BEFORE any quote.
  * Skips (no_route, price_impact_over_cap, fee_over_cap, tx_failed, ...) raise DemoSkip, never a
    fill. Landed-failed swaps report the lost fee in lamports; a failed sale leaves the position
    pending (no invented proceeds).
  * Signatures are synthetic ("sim:" prefix, synthetic_signature=True), never presented as onchain.
  * confirm_timeout is UNKNOWN: recorded as unresolved, retry blocked (fail closed) until
    reconciled; reconciliation against the simulated chain memory is NOT wired here yet.
Provenance is kept verbatim. A fake-client test validates wiring only, not live Jupiter availability.
"""
import fcntl
import json
import os
import time

try:
    from broker import BrokerError
    from broker.memecoin.execution.sim_executor import (
        SimulationExecutor, BalanceBook, BASE_FEE_LAMPORTS)
    from broker.memecoin.execution.mainnet_executor import (
        DEFAULT_PRIORITY_FEE_LAMPORTS, SOL_MINT)
    _DEPS = True
except Exception:  # pragma: no cover
    _DEPS = False

LAMPORTS = 1_000_000_000
EXECUTION_SOURCE = "jupiter_quote_sim"


# Executor refusals PROVEN to happen before any swap executes (sim_executor.py raise sites: balance/route/fee-policy/impact checks) plus tx_failed (landed
# but reverted: the fee is lost and booked, no token moved). Everything else (confirm_timeout, zero_output, unknown_signature, unlisted) keeps the claim.
_PROVEN_NO_EXECUTION = frozenset({"sim_insufficient_balance", "jito_not_configured", "fee_policy_required", "no_route", "price_impact_over_cap",
                                  "bundle_stake_unknown", "fee_uneconomic", "fee_over_cap", "insufficient_funds_for_fee", "insufficient_funds", "tx_failed"})


class DemoSkip(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(reason)
        self.reason = reason
        self.detail = dict(detail or {})


class _RecordingJupiter:
    """Wraps the quote client to keep raw quote provenance with request/receipt times."""
    def __init__(self, inner):
        self.inner = inner
        self.last = None

    def get_quote(self, input_mint, output_mint, amount, slippage_bps=100):
        t0 = time.time()
        q = self.inner.get_quote(input_mint, output_mint, amount, slippage_bps)
        t1 = time.time()
        self.last = {"quote": q, "requested_at": t0, "received_at": t1,
                     "input_mint": input_mint, "output_mint": output_mint,
                     "amount": int(amount), "slippage_bps": slippage_bps}
        return q


def _route_labels(q):
    out = []
    try:
        for step in (q or {}).get("routePlan") or []:
            lab = (step.get("swapInfo") or {}).get("label")
            if lab:
                out.append(str(lab))
    except Exception:
        pass
    return out


class DemoFiller:
    def __init__(self, jupiter, sol_usd_fn, decimals_fn, journal_path=None, seed=None,
                 latency_seconds=2.0, hourly_vol=0.12, now_fn=time.time, fee_sink=None, fx_age_cap_fn=None):
        if not _DEPS:
            raise RuntimeError("broker execution package unavailable")
        self._jup = _RecordingJupiter(jupiter)
        self._sol_usd_fn = sol_usd_fn
        self._decimals_fn = decimals_fn
        self._path = journal_path
        self._seed = seed
        self._latency = latency_seconds
        self._vol = hourly_vol
        self._now = now_fn
        self._n = 0
        self._fx_age_cap_fn = fx_age_cap_fn
        self._fee_sink = fee_sink  # callable(usd, op_id) -> (ok, why); the shared account's book_fee

    # ---- durable journal (op id -> record) ---------------------------
    def _jpath(self):
        if self._path:
            return self._path
        return os.path.join(os.environ.get("FOREX_AGENT_HOME", "."), "demo_fill_journal.json")

    def _locked(self, fn):
        p = self._jpath()
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            try:
                try:
                    with open(p) as fh:
                        j = json.load(fh)
                except FileNotFoundError:
                    j = {"fills": {}, "attempts": []}
                res = fn(j)
                tmp = p + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(j, fh, sort_keys=True)
                    fh.flush(); os.fsync(fh.fileno())
                os.replace(tmp, p)
                return res
            finally:
                fcntl.flock(lk, fcntl.LOCK_UN)

    def get_fill(self, op_id):
        return self._locked(lambda j: j["fills"].get(op_id))

    # ---- durable entry intent -----------------------------------------
    def intent_id(self, lane, mint):
        """ONE stable op id per (lane, mint) entry attempt until consumed (position persisted) or
        released (refused before any debit). Survives a crash/restart so a retry replays the
        ORIGINAL fill economics from the journal; a later re-entry gets a NEW id (fresh quote).

        A consume/release that was lost (swallowed error, crash after the position save) must not
        leave a replayable intent: an existing intent is checked against the shared account's own
        durable history and is STALE (dropped, fresh id) when the account already refused that op
        or has ANY credit/write-down for the position that op debited (the position lived and was
        settled). It is RESUMED only when the account holds a debit for the op and nothing settled
        it (crash between debit and the lane save), and is untouched when the account has no event
        for it. Unreadable account history is unknown: DemoSkip (entry refused), never a guess."""
        key = "%s|%s" % (lane, mint)
        old = self._locked(lambda j: (j.get("intents") or {}).get(key))
        if old is not None:
            st = self._intent_state(old, lane)
            if st == "unknown":
                raise DemoSkip("intent_state_unknown", {"op_id": old})
            if st == "stale":
                self._locked(lambda j: j.setdefault("intents", {}).pop(key, None)
                             if j.get("intents", {}).get(key) == old else None)
        def f(j):
            it = j.setdefault("intents", {})
            if key not in it:
                j["intent_seq"] = int(j.get("intent_seq") or 0) + 1
                it[key] = "%s:entry:%s:%d:%d" % (lane, mint, int(self._now() * 1000), j["intent_seq"])
            return it[key]
        return self._locked(f)

    def _intent_state(self, op_id, lane=None):
        """'fresh' | 'resume' | 'stale' | 'unknown'. The account LEDGER (ops + open positions) is
        authoritative; the WAL is only a cross-check, so a clean WAL-tail truncation that removed a
        settlement cannot resurrect a settled position's op. Any ledger/WAL disagreement is unknown."""
        try:
            from daemon.launch_scanner import paper_account as _pa
            with _pa._lock():
                led, healthy, degraded = _pa._load_locked()
                entries, status, _ = _pa._journal_read()
        except Exception:
            return "unknown"
        if not healthy or led is None or degraded or status is not None:
            return "unknown"
        rec = (led.get("ops") or {}).get(op_id)
        wal = [e for e in entries if e.get("op_id") == op_id]
        wal_debit = any(e.get("event") == "debit" for e in wal)
        if rec is None:
            return "unknown" if wal else "fresh"
        ev = rec.get("event")
        if ev == "debit_refused":
            return "stale"
        if ev != "debit":
            return "unknown"
        if not wal_debit:
            return "unknown"
        pid = rec.get("pos_id")
        lanes = led.get("lanes") or {}
        for st in lanes.values():
            for k, pos in (st.get("open") or {}).items():
                if isinstance(pos, dict) and pos.get("pos_id") == pid:
                    return "resume"
        return "stale"   # debited, and no longer an open position in the account: it lived and settled

    def op_committed(self, op_id):
        """True only when the shared account's own durable history shows the debit for this op and nothing settled
        it (the 'resume' classification). A journaled fill alone is NOT commit evidence."""
        try:
            return self._intent_state(op_id) == "resume"
        except Exception:
            return False

    def consume_intent(self, lane, mint):
        self._locked(lambda j: j.setdefault("intents", {}).pop("%s|%s" % (lane, mint), None))

    release_intent = consume_intent

    def unbooked_fees(self):
        return self._locked(lambda j: len(j.get("unbooked_fees") or {}))

    def _retry_unbooked(self, block):
        """Re-try booking incurred-but-unbooked fees. If any remain and `block`, refuse (entries
        only; exits are never blocked)."""
        if self._fee_sink is None:
            return
        def f(j):
            ub = j.get("unbooked_fees") or {}
            for fid, usd in list(ub.items()):
                try:
                    ok, _ = self._fee_sink(usd, fid)
                except Exception:
                    ok = False
                if ok:
                    del ub[fid]
            return len(ub)
        left = self._locked(f)
        if left and block:
            raise DemoSkip("unbooked_fee", {"unbooked": left})

    def failed_attempts(self, since=None):
        def f(j):
            return len([a for a in j["attempts"]
                        if since is None or a.get("ts", 0) >= since])
        return self._locked(f)

    def _record_attempt(self, op_id, side, mint, reason, detail, fx=None):
        def f(j):
            lost = int((detail or {}).get("fee_lamports_lost") or 0)
            if lost and self._fee_sink is not None:
                # Landed-failed tx: the fee is really gone. Book it on the shared account BEFORE
                # journaling the attempt, keyed by (op, attempt index): a crash in between replays
                # the same id (idempotent), a later distinct landed tx gets the next index.
                n = len([a for a in j["attempts"] if a.get("op_id") == op_id])
                px = float(fx or 0.0)  # the VERIFIED preflight FX of this very attempt, never a second read
                fid = "feeloss:%s:%d" % (op_id, n)
                try:
                    ok, why = self._fee_sink(lost / LAMPORTS * px, fid)
                except Exception as _e:
                    ok, why = False, "sink raised %s" % type(_e).__name__
                detail["fee_booked"] = bool(ok)
                detail["fee_op_id"] = fid
                if not ok:
                    detail["fee_book_error"] = str(why)
                    # durable: stays owed until booked; retried before the next call, blocks entries
                    j.setdefault("unbooked_fees", {})[fid] = lost / LAMPORTS * px
            j["attempts"].append({"op_id": op_id, "side": side, "mint": mint,
                                  "reason": reason, "detail": detail, "ts": self._now()})
            if reason == "confirm_timeout":
                _prev = j["fills"].get(op_id) or {}
                j["fills"][op_id] = {"status": "unknown", "op_id": op_id, "side": side, "mint": mint,
                                     "signature": (detail or {}).get("signature"),
                                     "ctx": (detail or {}).get("_ctx"), "detail": detail}
                if _prev.get("intent"):
                    j["fills"][op_id]["intent"] = dict(_prev["intent"])   # what the sale was for survives claim -> unknown
        self._locked(f)

    # ---- inputs, validated BEFORE any quote --------------------------
    def _preflight(self, mint, impact_cap_pct):
        if impact_cap_pct is None or not (impact_cap_pct > 0):
            raise DemoSkip("impact_cap_unset", {"why": "mandatory max price impact not set"})
        try:
            px, asof = self._sol_usd_fn()
        except Exception:
            px, asof = 0.0, None
        if not (isinstance(px, (int, float)) and px == px and 0 < px < float("inf")):
            raise DemoSkip("fx_unavailable", {"why": "SOL/USD unknown"})
        # FX as-of must be known, finite, not in the future and within the owner's mandatory
        # max data age BEFORE it sizes anything (unset cap refuses; no default).
        try:
            if self._fx_age_cap_fn is not None:
                cap = self._fx_age_cap_fn()
            else:
                from daemon.launch_scanner import paper_account as _pa
                cap = _pa.max_data_age_s()
        except Exception:
            cap = None
        if not (isinstance(cap, (int, float)) and cap == cap and 0 < cap < float("inf")):
            raise DemoSkip("fx_age_cap_unset", {"why": "mandatory max data age not set"})
        now = self._now()
        if (not isinstance(asof, (int, float)) or isinstance(asof, bool) or asof != asof
                or asof in (float("inf"), float("-inf")) or asof > now + 1.0
                or (now - asof) > cap):
            raise DemoSkip("fx_stale", {"sol_usd_asof": asof if isinstance(asof, (int, float)) and asof == asof and abs(asof) != float("inf") else None,
                                        "now": now, "max_age_s": cap})
        try:
            dec = self._decimals_fn(mint)
            dec = int(dec)
        except Exception:
            raise DemoSkip("decimals_unknown", {"mint": mint})
        if dec < 0 or dec > 18:
            raise DemoSkip("decimals_unknown", {"mint": mint})
        return float(px), asof, dec

    def _executor(self, sol_lamports, extra=None):
        book = BalanceBook(sol_lamports)
        for m, a in (extra or {}).items():
            book.credit(m, a)
        self._n += 1
        self._jup.last = None  # per-attempt recording state: never journal a prior attempt's quote times
        # The simulated CHAIN memory is durable next to the fill journal: the true outcome of
        # every attempt survives a restart, which is what reconcile() reads.
        os.makedirs(os.path.dirname(os.path.abspath(self._jpath())), exist_ok=True)
        ex = SimulationExecutor(jupiter=self._jup, book=book, latency_seconds=self._latency,
                                hourly_vol=self._vol,
                                seed=None if self._seed is None else self._seed * 100003 + self._n,
                                now_fn=self._now, chain_state_path=self._jpath() + ".chain")
        if getattr(self, "_inject_outage", None):  # test/fault hook; inert in production
            ex.inject_confirm_outage(self._inject_outage)
            self._inject_outage = None
        return ex

    def _existing(self, op_id):
        rec = self.get_fill(op_id)
        if rec is None:
            return None
        if rec.get("status") == "inflight":
            # A swap was STARTED for this op id and no fill was ever saved (crash or save failure between execute and save). The sim book is
            # rebuilt per call, so a blind retry would execute the swap AGAIN: refuse (fail closed, visible); never guess the outcome.
            raise DemoSkip("unresolved_inflight",
                           {"op_id": op_id, "why": "swap started, no fill saved (crash or save failure); retry blocked, needs reconciliation"})
        if rec.get("status") == "unknown":
            raise DemoSkip("unresolved_unknown",
                           {"op_id": op_id, "why": "confirm_timeout not reconciled; retry blocked"})
        return rec

    def _mark_inflight(self, op_id, side, mint, meta=None):
        """ATOMIC check-and-claim under the fill-journal lock, BEFORE the swap executes: when any record already exists for the op id (a filled
        fill, an inflight claim by another executor, an UNKNOWN) it is returned and NOTHING is claimed, so two concurrent executors of the same op
        id cannot both execute. Returns None when this caller now owns the claim. Raises (nothing executes) when the claim cannot be written."""
        def f(j):
            cur = j["fills"].get(op_id)
            if cur is not None:
                return cur
            # MINT-LEVEL guard: a swap that may have executed for this lane+mint under ANOTHER op id (e.g. a leg sale left inflight, then a combined
            # remainder exit or a buyback that overlaps the same units) must block every other buy/sell for the mint until it is resolved.
            _lane = str(op_id).split(":", 1)[0]
            for _k, _v in j["fills"].items():
                if _k != op_id and (_v or {}).get("status") in ("inflight", "unknown") and _v.get("mint") == mint and str(_k).split(":", 1)[0] == _lane:
                    return {"status": _v.get("status"), "op_id": _k, "mint": mint, "mint_guard": True}
            _c = {"status": "inflight", "op_id": op_id, "side": side, "mint": mint, "ts": self._now()}
            if meta:
                _c["intent"] = dict(meta)   # what the sale was FOR survives a crash with the claim itself
            j["fills"][op_id] = _c
            return None
        return self._locked(f)

    def _resolve_claim(self, op_id, cur):
        """The record that beat this caller to the op id: a filled fill is returned (idempotent), anything unresolved refuses."""
        if cur.get("status") == "inflight":
            raise DemoSkip("unresolved_inflight", {"op_id": op_id, "why": "another executor, or a crashed attempt, holds this op id; retry blocked"})
        if cur.get("status") == "unknown":
            raise DemoSkip("unresolved_unknown", {"op_id": op_id, "why": "confirm_timeout not reconciled; retry blocked"})
        return cur

    def fills_by_prefix(self, prefix, suffix=""):
        """Every journal record (any status) whose op id starts with prefix and ends with suffix: lets a caller enumerate ALL records of one position
        generation instead of probing candidate keys."""
        return self._locked(lambda j: {k: dict(v) for k, v in j["fills"].items() if k.startswith(prefix) and k.endswith(suffix)})

    def resolve_inflight(self, op_id, outcome, evidence, operator):
        """DISABLED. Clearing an unresolved inflight claim needs VERIFIED evidence (an on-chain/receipt proof that the swap never landed) and an
        authority route; neither exists in this codebase, and a caller-supplied string is not evidence. It refuses unconditionally, so no caller can
        bypass the claim. The held state stays until a real reconcile (with a signature) settles it."""
        raise DemoSkip("resolution_disabled", {"op_id": op_id, "why": "no verified-evidence/authority route exists; external resolution is disabled"})

    def _clear_inflight(self, op_id):
        def f(j):
            if (j["fills"].get(op_id) or {}).get("status") == "inflight":
                del j["fills"][op_id]
        self._locked(f)

    def _skip_from(self, op_id, side, mint, exc, fx=None, ctx=None):
        det = dict(getattr(exc, "detail", {}) or {})
        reason = det.get("reason") or "executor_error"
        lost = int(det.get("fee_lamports") or 0) if reason == "tx_failed" else 0
        d = {k: v for k, v in det.items() if k != "reason"}
        if lost:
            d["fee_lamports_lost"] = lost
        if ctx is not None:
            _q = getattr(self._jup, "last", None) or {}
            d["_ctx"] = dict(ctx, px=fx, quote={
                "requested_at": _q.get("requested_at"), "received_at": _q.get("received_at"),
                "slippage_bps": _q.get("slippage_bps"), "route": _route_labels(_q.get("quote"))})
        # The attempt (and, for a landed-failed tx, its booked/owed FEE) is made durable FIRST. If that raises, the claim stays (fail closed) and no
        # fee evidence is lost; only then is the claim cleared, and only for a reason PROVEN to precede execution (or a reverted tx).
        self._record_attempt(op_id, side, mint, reason, d, fx)
        # confirm_timeout became an UNKNOWN record inside _record_attempt; zero_output (a landed swap with no measured output), unknown_signature and
        # any reason not on the list KEEP the INFLIGHT claim: an unknown outcome is never cleared.
        if reason in _PROVEN_NO_EXECUTION:
            self._clear_inflight(op_id)
        raise DemoSkip(reason, d)

    # ---- buy / sell ---------------------------------------------------
    def buy(self, op_id, mint, notional_usd, impact_cap_pct, slippage_bps=100):
        rec = self._existing(op_id)
        if rec is not None:
            return rec
        self._retry_unbooked(block=True)
        px, asof, dec = self._preflight(mint, impact_cap_pct)
        total_fee = BASE_FEE_LAMPORTS + DEFAULT_PRIORITY_FEE_LAMPORTS
        amount = int(round(float(notional_usd) / px * LAMPORTS)) - total_fee
        if amount <= 0:
            raise DemoSkip("notional_below_fee", {"notional_usd": notional_usd})
        ex = self._executor(amount + total_fee + 1)
        _cur = self._mark_inflight(op_id, "buy", mint)
        if _cur is not None:
            return self._resolve_claim(op_id, _cur)
        try:
            res = ex.execute_swap(SOL_MINT, mint, amount, slippage_bps=slippage_bps,
                                  price_impact_cap_pct=float(impact_cap_pct))
        except BrokerError as exc:
            self._skip_from(op_id, "buy", mint, exc, px, {"side": "buy", "notional_usd": float(notional_usd), "total_fee": total_fee, "dec": dec, "asof": asof})
        fill = self._build_fill(op_id, "buy", mint, res, {"notional_usd": float(notional_usd),
                                "total_fee": total_fee, "dec": dec, "asof": asof, "px": px},
                                self._jup.last or {})
        return self._save(op_id, fill)

    def sell(self, op_id, mint, qty_tokens, impact_cap_pct, slippage_bps=100, meta=None):
        rec = self._existing(op_id)
        if rec is not None:
            return rec
        self._retry_unbooked(block=False)
        px, asof, dec = self._preflight(mint, impact_cap_pct)
        total_fee = BASE_FEE_LAMPORTS + DEFAULT_PRIORITY_FEE_LAMPORTS
        base_units = int(round(float(qty_tokens) * (10 ** dec)))
        if base_units <= 0:
            raise DemoSkip("amount_too_small", {})
        ex = self._executor(total_fee + 1, {mint: base_units})
        _cur = self._mark_inflight(op_id, "sell", mint, meta)
        if _cur is not None:
            return self._resolve_claim(op_id, _cur)
        try:
            res = ex.execute_swap(mint, SOL_MINT, base_units, slippage_bps=slippage_bps,
                                  price_impact_cap_pct=float(impact_cap_pct))
        except BrokerError as exc:
            self._skip_from(op_id, "sell", mint, exc, px, {"side": "sell", "qty_tokens": float(qty_tokens), "total_fee": total_fee, "dec": dec, "asof": asof})
        fill = self._build_fill(op_id, "sell", mint, res, {"qty_tokens": float(qty_tokens),
                                "total_fee": total_fee, "dec": dec, "asof": asof, "px": px},
                                self._jup.last or {})
        if meta:
            fill["intent"] = dict(meta)   # original reason/kind/legs/ts: durable atomically WITH the fill (no crash window)
        return self._save(op_id, fill)

    def _build_fill(self, op_id, side, mint, res, c, q, reconciled=False):
        """ONE economics path for live fills and for reconcile-adopted fills."""
        px, dec, total_fee = c["px"], c["dec"], c["total_fee"]
        fee_usd = total_fee / LAMPORTS * px
        if side == "buy":
            qty = res["out_amount"] / (10 ** dec)
            spent = (res["in_amount"] + total_fee) / LAMPORTS * px
            stake = c["notional_usd"] if abs(spent - c["notional_usd"]) < 1e-6 else spent
            fill = {"qty_tokens": qty, "price_usd": (res["in_amount"] / LAMPORTS * px) / qty,
                    "fee_usd": fee_usd, "fee_lamports": total_fee, "total_debit_usd": stake}
        else:
            # The simulated book already debited the network fee from the SOL it measured, so
            # out_amount is NET of the fee. The executor's reported "fee" on a sell is NOT used.
            net_usd = res["out_amount"] / LAMPORTS * px
            fill = {"qty_tokens": c["qty_tokens"], "net_proceeds_usd": net_usd,
                    "gross_usd": net_usd + fee_usd, "fee_usd": fee_usd, "fee_lamports": total_fee,
                    "price_usd": (net_usd + fee_usd) / c["qty_tokens"]}
        fill.update({"status": "filled", "op_id": op_id, "side": side, "mint": mint,
                     "execution_source": EXECUTION_SOURCE,
                     "signature": "sim:" + str(res["signature"]), "synthetic_signature": True,
                     "provenance": self._prov(q, res, dec, px, c["asof"])})
        if reconciled:
            fill["provenance"]["reconciled"] = True
        return fill

    def reconcile_pending(self):
        """Resolve every parked UNKNOWN record against the durable simulated-chain memory. A record
        whose outcome cannot be established stays parked (never guessed); one failure never stops the
        others. Returns {op_id: "resolved"|"parked"}."""
        try:
            ids = self._locked(lambda j: [k for k, v in j["fills"].items() if v.get("status") == "unknown"])
        except Exception:
            return {}
        out = {}
        for op_id in ids:
            try:
                self.reconcile(op_id)
                out[op_id] = "resolved"
            except Exception:
                out[op_id] = "parked"
        return out

    def reconcile(self, op_id):
        """Resolve a parked UNKNOWN against the durable simulated-chain memory. settled -> adopt
        the ORIGINAL fill once; failed -> book the lost fee once, no fill, retry allowed; unlanded
        or allocated -> retry allowed (no fee). An unknown signature / lost chain memory stays
        parked (DemoSkip reconcile_unavailable): never a guessed outcome. Returns the fill or None."""
        rec = self._locked(lambda j: j["fills"].get(op_id))
        if rec is None or rec.get("status") != "unknown":
            return rec
        sig, ctx = rec.get("signature"), rec.get("ctx")
        try:
            ex = self._executor(0)
            truth = ex.reconcile(sig)
        except Exception as exc:
            raise DemoSkip("reconcile_unavailable", {"op_id": op_id, "why": type(exc).__name__})
        st = truth.get("status")
        if st == "settled":
            if not ctx:
                raise DemoSkip("reconcile_unavailable", {"op_id": op_id, "why": "no context"})
            fill = self._build_fill(op_id, rec["side"], rec["mint"], truth["result"], ctx, {},
                                    reconciled=True)
            _oq = ctx.get("quote") or {}
            fill["provenance"].update({"quote_requested_at": _oq.get("requested_at"),
                                       "quote_received_at": _oq.get("received_at"),
                                       "slippage_bps": _oq.get("slippage_bps"),
                                       "route": _oq.get("route")})
            def adopt(j):
                j["fills"][op_id] = fill
                return fill
            return self._locked(adopt)
        if st == "failed":
            lost = int(truth.get("fee_lamports") or 0)
            fid = "feeloss:%s:reconcile" % op_id
            usd = lost / LAMPORTS * float((ctx or {}).get("px") or 0.0)
            ok = False
            if self._fee_sink is not None and usd > 0:
                try:
                    ok, _ = self._fee_sink(usd, fid)
                except Exception:
                    ok = False
            if not ok:
                if usd > 0:
                    def owe(j):
                        j.setdefault("unbooked_fees", {})[fid] = usd
                    self._locked(owe)
                    # parked fee stays owed and retried; the UNKNOWN record is cleared below
        elif st not in ("unlanded", "allocated"):
            raise DemoSkip("reconcile_unavailable", {"op_id": op_id, "why": "status %s" % st})
        def clear(j):
            j["fills"].pop(op_id, None)
        self._locked(clear)
        return None

    def _prov(self, q, res, dec, px, asof):
        return {
            "input_mint": res["input_mint"], "output_mint": res["output_mint"],
            "in_amount": int(res["in_amount"]), "out_amount": int(res["out_amount"]),
            "quote_out_amount": res.get("quote_out_amount"), "decimals": dec,
            "price_impact_pct": res.get("price_impact_pct"),
            "price_impact_unit": res.get("price_impact_unit"),
            "price_impact_raw": res.get("price_impact_raw"),
            "route": _route_labels(q.get("quote")),
            "quote_requested_at": q.get("requested_at"), "quote_received_at": q.get("received_at"),
            "slippage_bps": q.get("slippage_bps"),
            "latency_move_pct": res.get("latency_move_pct"),
            "sol_usd": px, "sol_usd_asof": asof,
            "simulated": True,
        }

    def _save(self, op_id, fill):
        def f(j):
            if (j["fills"].get(op_id) or {}).get("status") == "inflight":
                del j["fills"][op_id]
            j["fills"].setdefault(op_id, fill)
            return j["fills"][op_id]
        return self._locked(f)


def mode_enabled():
    """Operator-selected execution mode. When ON, a lane that cannot obtain a quote-sim fill
    REFUSES; it never falls back to the model fill (no silent paper-model P&L)."""
    return os.environ.get("AGT_EXECUTION_MODE") == EXECUTION_SOURCE


_TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
_DEC_CACHE = {}


def sol_usd_from_jupiter():
    """(SOL/USD, observed_ts) from the Jupiter v3 price API with its ACTUAL observation time (a cached
    read keeps its original time, so staleness is visible to the age cap). Any failure, an absent,
    non-finite or non-positive price -> (0.0, None): unknown stays unknown, nothing is invented."""
    try:
        from daemon.launch_scanner import jup_price as _jp
        got = (_jp.get_prices_observed([SOL_MINT]) or {}).get(SOL_MINT)
        if not got:
            return 0.0, None
        px, ts = float(got[0]), float(got[1])
        if not (px == px and 0 < px < float("inf")) or not (ts == ts and abs(ts) != float("inf")):
            return 0.0, None
        return px, ts
    except Exception:
        return 0.0, None


def decimals_onchain(mint):
    """Token decimals from the ON-CHAIN mint account only: the owner must be a token program and the
    account long enough to hold the mint layout (decimals u8 @44, plausible 0..18). There is NO suffix
    guess (reserves.mint_decimals' pump=>6 heuristic is not used). Only known values are cached.
    Anything else -> None (the sim refuses with decimals_unknown)."""
    if mint in _DEC_CACHE:
        return _DEC_CACHE[mint]
    try:
        from daemon.launch_scanner import reserves as _rs
        owner, data = _rs.get_account(mint, exempt=False)
        if owner not in _TOKEN_PROGRAMS or not data or len(data) < 82:
            return None
        dec = int(data[44])
        if not (0 <= dec <= 18):
            return None
        _DEC_CACHE[mint] = dec
        return dec
    except Exception:
        return None


_STARTUP_DONE = False


def default_filler():
    """Production wiring: real quote client, journal under FOREX_AGENT_HOME, SOL/USD from the Jupiter
    v3 price API and decimals from the on-chain mint account (both fail closed). The providers are WIRED
    but NOT QUALIFIED: accuracy/availability/owner acceptance is still open, and the owner's mandatory
    limits (data age, impact cap) must be set or every entry still refuses."""
    if not _DEPS:
        return None
    try:
        from broker.memecoin.execution.jupiter import JupiterClient
        from daemon.launch_scanner import paper_account as _pa
        f = DemoFiller(JupiterClient(), fee_sink=_pa.book_fee, sol_usd_fn=sol_usd_from_jupiter,
                       decimals_fn=decimals_onchain)
        global _STARTUP_DONE
        if not _STARTUP_DONE:
            _STARTUP_DONE = True  # once per process; a failure leaves records parked, never blocks the filler
            f.reconcile_pending()
        return f
    except Exception:
        return None
