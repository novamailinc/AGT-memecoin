"""Durable ENTRY-INTENT records and orphan-entry recovery for the grad lane (SOURCE CANDIDATE, written, NOT run, NOT reviewed).

Purpose: close the window between a durable paper-account debit and the lane ledger write (grad.consider_candidate). A record holding the exact original
pos dict, op id, stake, qty and the verbatim demo fill is made durable BEFORE try_debit; the lane _write is the single commit point; the record is unlinked AFTER
(idempotent). A recovery pass, called by grad under the lane ledger lock, classifies surviving records from ORIGINAL evidence only and ADOPTS the saved position
only when every cross-check passes. It never mutates the account, never refunds, never invents a trade, price or fill, never bypasses a gate.

SCOPE: demo mode only (a verbatim fill exists); flag AGT_GRAD_ENTRY_INTENT_ENABLED, default OFF (read by grad.py before this module is imported, so flag-off behavior and I/O are unchanged).
CRASH SCOPE: process-crash (SIGKILL) ordering only. No power-loss claim: the lane _write has no required directory fsync today. The record's own directory fsync is REQUIRED here only so a
refused fsync stops the entry BEFORE the debit; it does not make the whole scheme power-loss safe.
Stdlib only. All I/O under paths derived from the lane ledger path; no network, no secrets, record bodies are never logged in full (op id, mint, class only).

CLASSES (see classify): A lane already holds it (unlink only); B adopt; C debit never happened (drop); D-refused / D-settled (drop with positive proof); E ambiguity (FREEZE: entries refused, record and evidence untouched).
D-settled proof (exact): account has the debit for the op in ledger ops AND WAL, the account position is no longer open, a settle event (credit or write_down) for that pos_id is in the WAL,
AND the LANE ledger holds a closes record with pa_pos_id == that pos_id and mint == the record mint. Missing any one of these is E (the lane closes list is truncated to MAX_CLOSES by grad, so absence is unknown, not proof).
"""
import hashlib, json, logging, math, os, re, time

logger = logging.getLogger(__name__)
SCHEMA = 1
FLAG = "AGT_GRAD_ENTRY_INTENT_ENABLED"
MAX_PER_PASS = 8          # TECHNICAL bound on records classified per pass (not an owner threshold); entries stay refused while any record is unresolved
REQ_POS = ("tokens", "entry_price", "entry_ts", "legs", "entry_features", "mint")
FREEZE_CLASSES = ("E", "BACKLOG", "UNREADABLE")

def enabled():
    return os.environ.get(FLAG) == "1"

def records_dir(ledger_path):
    return str(ledger_path) + ".entry-intents"

def _fname(op_id):
    return hashlib.sha256(str(op_id).encode()).hexdigest()[:40] + ".json"

def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)

def _sha(obj):
    return hashlib.sha256(_canon(obj).encode()).hexdigest()

def _strict(obj):
    return json.loads(json.dumps(obj, allow_nan=False))      # raises on non-serializable or non-finite: the entry is refused, nothing is stringified silently

def build_record(lane, mint, op_id, stake, qty, pos, fill, cand_ident):
    """pos: the exact dict about to be inserted (before pa_pos_id). fill: the verbatim demo fill dict. Raises on anything not JSON-serializable or non-finite."""
    body = {"schema": SCHEMA, "lane": lane, "mint": mint, "op_id": op_id, "stake": float(stake), "qty": float(qty),
            "pos": _strict(pos), "fill": _strict(fill), "fill_sha256": _sha(_strict(fill)),
            "cand": _strict(dict(cand_ident or {})), "written_ts": time.time()}
    body["checksum"] = _sha({k: v for k, v in body.items() if k != "checksum"})
    return body

def _fsync_dir(d, required):
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        if required:
            raise

def write_record(ledger_path, rec):
    """tmp + fsync + rename + REQUIRED directory fsync. Raises on any failure (the caller then refuses the entry BEFORE try_debit). Returns the final path."""
    d = records_dir(ledger_path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    final = os.path.join(d, _fname(rec["op_id"])); tmp = final + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, sort_keys=True); f.flush(); os.fsync(f.fileno())
    os.chmod(tmp, 0o600); os.replace(tmp, final); _fsync_dir(d, True)
    return final

def remove_record(path):
    """Idempotent cleanup AFTER the lane commit. Failures are tolerated (class A on the next pass)."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("entry-intent cleanup failed (%r)", e); return False
    _fsync_dir(os.path.dirname(path), False); return True

def scan(ledger_path):
    """-> (records, unreadable). records: sorted list of (path, rec_or_None, err). Missing directory with a readable parent == no records (knowable). Any other listing/parse error is UNKNOWN.
    Stale *.tmp files are removed ONLY because the caller holds the lane ledger lock and every writer writes its tmp under that same lock: a tmp present while we hold it belongs to a dead writer. No age heuristic."""
    d = records_dir(ledger_path)
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        return ([], False) if os.path.isdir(os.path.dirname(os.path.abspath(d)) or ".") else ([], True)
    except Exception:
        return [], True
    out = []
    for n in names:
        p = os.path.join(d, n)
        if n.endswith(".tmp"):
            try: os.unlink(p)
            except Exception: out.append((p, None, "stale tmp not removable"))
            continue
        if not n.endswith(".json"):
            out.append((p, None, "unrecognized file")); continue
        try:
            with open(p) as f:
                out.append((p, json.load(f), None))
        except Exception as e:
            out.append((p, None, "unreadable record (%s)" % type(e).__name__))
    return out, False

def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))

def _close(a, b):
    return _num(a) and _num(b) and math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)

def validate(rec, lane):
    """Structural + checksum + cross-field identity of the record against ITSELF. Returns None when valid, else a reason."""
    if not isinstance(rec, dict) or rec.get("schema") != SCHEMA:
        return "schema mismatch"
    if rec.get("checksum") != _sha({k: v for k, v in rec.items() if k != "checksum"}):
        return "checksum mismatch (torn or altered record)"
    if rec.get("lane") != lane:
        return "lane mismatch"
    for k in ("op_id", "mint"):
        if not isinstance(rec.get(k), str) or not rec[k]:
            return "missing %s" % k
    if not (_num(rec.get("stake")) and rec["stake"] > 0 and _num(rec.get("qty")) and rec["qty"] > 0):
        return "stake/qty not positive finite"
    pos, fill = rec.get("pos"), rec.get("fill")
    if not isinstance(pos, dict) or not isinstance(fill, dict):
        return "pos/fill missing"
    for k in REQ_POS:
        if k not in pos:
            return "pos missing key %s" % k
    if pos.get("mint") != rec["mint"]:
        return "pos mint differs from record mint"
    if not isinstance(pos.get("legs"), dict) or not isinstance(pos.get("entry_features"), dict):
        return "pos legs/entry_features wrong type"
    if not (_num(pos.get("tokens")) and _num(pos.get("entry_price")) and pos["entry_price"] > 0 and _num(pos.get("entry_ts"))):
        return "pos numeric fields invalid"
    if _sha(fill) != rec.get("fill_sha256"):
        return "fill sha256 mismatch"
    if not (_close(pos["tokens"], fill.get("qty_tokens")) and _close(pos["tokens"], rec["qty"])):
        return "tokens != fill.qty_tokens != record qty"
    if not _close(pos["entry_price"], fill.get("price_usd")):
        return "entry_price != fill.price_usd"
    if (pos.get("execution") or {}).get("entry_op") != rec["op_id"]:
        return "pos execution.entry_op != op_id"
    if pos.get("pa_pos_id"):
        return "pos already carries pa_pos_id"
    if not isinstance(((rec.get("cand") or {}).get("entry_day")), str) or not rec["cand"]["entry_day"]:
        return "cand.entry_day missing (daily-entry attribution unknown)"
    return None

def account_evidence(pa, op_id, wait_s):
    """Read-only account evidence for one op, under the account lock acquired with a BOUND (AccountBusy => unavailable, never a hang). Returns dict:
    ok (bool: healthy, not degraded, journal status None), why, op (ledger ops record or None), wal (events for op_id), open_pos (account open position dict for the op's pos_id or None),
    settle (WAL credit/write_down events for that pos_id). Reads only; never writes."""
    try:
        bw = getattr(pa, "bounded_lock_wait", None)
        import contextlib
        cm = bw(wait_s) if bw is not None else contextlib.nullcontext()
        with cm:
            with pa._lock():
                led, healthy, degraded = pa._load_locked()
                entries, status, _ = pa._journal_read()
    except Exception as e:
        return {"ok": False, "why": "account evidence unavailable (%s)" % type(e).__name__}
    if not healthy or led is None or degraded or status is not None:
        return {"ok": False, "why": "account unhealthy/degraded or journal status %r" % (status,)}
    op = (led.get("ops") or {}).get(op_id)
    wal = [e for e in entries if e.get("op_id") == op_id]
    pid = (op or {}).get("pos_id") if isinstance(op, dict) else None
    matches = []
    for lname, st in (led.get("lanes") or {}).items():
        for k, p in (st.get("open") or {}).items():
            if isinstance(p, dict) and pid and p.get("pos_id") == pid:
                matches.append((lname, k, p))
    open_pos, open_key, open_lane = (matches[0][2], matches[0][1], matches[0][0]) if matches else (None, None, None)
    settle = [e for e in entries if pid and e.get("pos_id") == pid and e.get("event") in ("credit", "write_down")]
    return {"ok": True, "op": op, "wal": wal, "open_pos": open_pos, "open_key": open_key, "open_lane": open_lane, "open_n": len(matches), "settle": settle}

def _settle_bad(settle, rec, mint, pid):
    """Settlement WAL evidence must be bound field by field (lane, mint, pos_id, op_id), unique, non-instance, economically bounded, and contain a TERMINAL event. Returns a reason or None."""
    seen, qsum, terminal = set(), 0.0, False
    for e in settle:
        if not isinstance(e, dict) or e.get("event") not in ("credit", "write_down"):
            return "settle evidence has a non-settle event"
        if e.get("pos_id") != pid or e.get("mint") != mint or e.get("lane") != rec.get("lane"):
            return "settle WAL event lane/mint/pos_id differs from the record/ledger op"
        if e.get("instance") is True:
            return "settle WAL event is an instance lot; the lane never opens instance lots"
        if not isinstance(e.get("op_id"), str) or not e["op_id"] or e["op_id"] in seen:
            return "settle WAL event has no op_id or the op_id is duplicated"
        seen.add(e["op_id"])
        q = e.get("qty")
        if q is not None:
            if not (_num(q) and q > 0):
                return "settle WAL event qty is not positive finite"
            qsum += float(q)
        if e["event"] == "write_down" or e.get("final") is True:
            terminal = True
    if not terminal:
        return "no terminal settle event (final credit or write_down) in the WAL"
    if qsum > float(rec["qty"]) * (1 + 1e-9) + 1e-12:
        return "settled quantity %r exceeds the recorded entry quantity" % (qsum,)
    return None

def classify(rec, lane_d, ev):
    """Pure. -> (class, reason, pos_id_or_None). lane_d: the lane ledger dict read under the lane lock. ev: account_evidence result."""
    mint, op_id = rec["mint"], rec["op_id"]
    if not ev.get("ok"):
        return "E", ev.get("why", "account evidence unavailable"), None
    op, wal = ev.get("op"), ev.get("wal") or []
    lane_pos = (lane_d.get("positions") or {}).get(mint)
    wal_ev = {e.get("event") for e in wal}
    if op is None:
        if wal:
            return "E", "WAL has events for the op but the account ledger has none", None
        if lane_pos is not None:
            return "E", "no account event for the op but the lane holds a position for the mint", None
        return "C", "no account event in ledger or WAL (clean, healthy): the debit never happened", None
    kind = op.get("event")
    if kind == "debit_refused":
        if "debit_refused" not in wal_ev or "debit" in wal_ev:
            return "E", "refusal not confirmed by the WAL (or a debit also exists)", None
        if len(wal) != 1:
            return "E", "WAL evidence for the refused op is duplicated or conflicting (%d events)" % len(wal), None
        return "D-refused", "account refused the op (ledger and WAL agree), no debit", None
    if kind != "debit":
        return "E", "unexpected account op kind %r" % (kind,), None
    if "debit" not in wal_ev:
        return "E", "account ledger shows a debit but the WAL does not", None
    pid = op.get("pos_id")
    if not pid or not _close(op.get("stake"), rec["stake"]) or not _close(op.get("qty"), rec["qty"]):
        return "E", "account debit stake/qty/pos_id differ from the record", None
    # EXPLICIT BINDING (no reliance on identifier shape): record -> ledger op, and ledger op -> the ONE WAL debit event, field by field.
    if op.get("instance") is True:
        return "E", "ledger op is an instance lot; the lane never opens instance lots", None
    if op.get("lane") != rec.get("lane"):
        return "E", "ledger op lane differs from the record lane", None
    if op.get("mint") != mint:
        return "E", "account ledger op mint differs from the record", pid
    if len(wal) != 1:
        return "E", "WAL evidence for the op is duplicated or conflicting (%d events)" % len(wal), pid
    _w = wal[0]
    for _k, _want in (("op_id", op_id), ("lane", rec.get("lane")), ("mint", mint), ("pos_id", pid)):
        if _w.get(_k) != _want:
            return "E", "WAL debit %s differs from the ledger/record" % _k, pid
    if _w.get("instance") is True:
        return "E", "WAL debit is an instance lot; the lane never opens instance lots", pid
    if not (_num(_w.get("stake")) and _num(_w.get("qty")) and _close(_w["stake"], op.get("stake")) and _close(_w["stake"], rec["stake"]) and _close(_w["qty"], op.get("qty")) and _close(_w["qty"], rec["qty"])):
        return "E", "WAL debit stake/qty differ from the ledger op or the record", pid
    op_pos = ev.get("open_pos")
    closes = [c for c in (lane_d.get("closes") or []) if isinstance(c, dict) and c.get("pa_pos_id") == pid and c.get("mint") == mint]
    if op_pos is not None:
        # Account schema (paper_account._apply_debit): a non-instance open position is KEYED BY MINT and carries no "mint" field; only instance positions store "mint".
        # The mint is therefore proven from (a) the ledger op record (always carries mint) AND (b) the position's own mint field when present, else its open-dict key.
        if ev.get("open_n") != 1:
            return "E", "account holds %r open positions for the pos_id (expected exactly one)" % (ev.get("open_n"),), pid
        if ev.get("open_lane") != rec.get("lane"):
            return "E", "account open position is in a different lane than the record", pid
        _pm = op_pos.get("mint") if "mint" in op_pos else ev.get("open_key")
        if _pm != mint or not _close(op_pos.get("qty"), rec["qty"]):
            return "E", "account open position mint/qty differ from the record", pid
        if lane_pos is not None:
            if lane_pos.get("pa_pos_id") == pid and _close(lane_pos.get("tokens"), rec["qty"]):
                return "A", "lane already holds the position", pid
            return "E", "lane holds a different position for the mint", pid
        if closes or mint in (lane_d.get("dead_mints") or {}):
            return "E", "lane has a close/dead record for this position while the account still holds it", pid
        return "B", "account debit durable, position open, all fields match, lane has no record: adopt", pid
    # account position no longer open
    if ev.get("settle") and closes and lane_pos is None:
        _bad = _settle_bad(ev["settle"], rec, mint, pid)
        if _bad:
            return "E", _bad, pid
        return "D-settled", "settled in the account WAL and closed in the lane closes list (pa_pos_id match)", pid
    return "E", "account position not open but no complete settle+lane-close proof", pid

def recover_pass(ledger_path, lane, read_lane, adopt, evidence, drop_intent):
    """Call with the lane ledger lock HELD. Callbacks: read_lane() -> lane dict; adopt(rec, pos_id) -> None, performs the lane write (raises on failure);
    evidence(op_id) -> account_evidence dict; drop_intent(op_id, mint) -> best-effort consume of the demo intent for that op.
    Returns (blocked_reason_or_None, actions). Never raises: any internal failure is a BLOCK reason (entries only); the caller keeps exits running regardless."""
    actions = []
    try:
        recs, unreadable = scan(ledger_path)
        if unreadable:
            return "entry-intent directory unreadable (state unknown); entries blocked fail-closed", actions
        if not recs:
            return None, actions
        blocked = None
        for i, (path, rec, err) in enumerate(recs):
            if i >= MAX_PER_PASS:
                blocked = blocked or "entry-intent backlog beyond %d records; entries blocked until resolved" % MAX_PER_PASS
                break
            if rec is None:
                blocked = blocked or "entry-intent record %s: %s; entries blocked fail-closed" % (os.path.basename(path), err); continue
            bad = validate(rec, lane)
            op_id = rec.get("op_id", "?"); mint = rec.get("mint", "?")
            if bad:
                actions.append((op_id, mint, "E", bad)); blocked = blocked or "entry-intent op %s mint %s frozen: %s" % (op_id, mint, bad); continue
            cls, why, pid = classify(rec, read_lane(), evidence(op_id))
            actions.append((op_id, mint, cls, why))
            if cls == "B":
                try:
                    adopt(rec, pid); remove_record(path); drop_intent(op_id, mint)
                except Exception as e:
                    blocked = blocked or "entry-intent op %s mint %s adoption failed (%s); entries blocked" % (op_id, mint, type(e).__name__)
            elif cls in ("A", "C", "D-refused", "D-settled"):
                remove_record(path)
                if cls in ("A", "C", "D-refused"):
                    drop_intent(op_id, mint)
            else:
                blocked = blocked or "entry-intent op %s mint %s frozen: %s" % (op_id, mint, why)
        return blocked, actions
    except Exception as e:
        return "entry-intent recovery failed (%s); entries blocked fail-closed" % type(e).__name__, actions


# ---- read-only visibility (status/account views) ----------------------------------------------------------------------------------------------------
def default_ledger_path(env=None):
    """The grad lane ledger path WITHOUT importing the engine: mirrors grad.py's own derivation (HOME = FOREX_AGENT_HOME or ~/.forex-agent, file grad_ledger.json).
    Valid for the grad lane only; other lanes have their own ledgers and are out of scope."""
    env = os.environ if env is None else env
    home = env.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent")
    return os.path.join(home, "grad_ledger.json")

def status_readonly(ledger_path):
    """READ-ONLY, bounded, no locks, no repair, no file removal (scan() would delete stale tmp files, so it is NOT used here).
    -> {"state": "clear"|"pending"|"unknown", "records": [{"file","op_id","mint","valid"}], "why": str|None}.
    clear only when the directory is absent with a readable parent, or present and empty. Any listing/parse problem is unknown or a pending-invalid record, never clear."""
    d = records_dir(ledger_path)
    try:
        names = sorted(os.listdir(d))
    except FileNotFoundError:
        ok = os.path.isdir(os.path.dirname(os.path.abspath(d)) or ".")
        return {"state": "clear", "records": [], "why": None} if ok else {"state": "unknown", "records": [], "why": "ledger directory unreadable"}
    except Exception as e:
        return {"state": "unknown", "records": [], "why": "entry-intent directory unreadable (%s)" % type(e).__name__}
    recs = []
    for n in names[:MAX_PER_PASS + 1]:
        p = os.path.join(d, n)
        if n.endswith(".tmp"):
            recs.append({"file": n, "op_id": None, "mint": None, "valid": False}); continue
        try:
            with open(p) as f:
                r = json.load(f)
            lane_ok = isinstance(r, dict) and validate(r, r.get("lane")) is None
            # REDACTION: only identifier-shaped values are ever surfaced; anything else (malformed text, markup, long strings) becomes None and marks the record invalid. Record bodies are never echoed.
            mint = (r or {}).get("mint") if isinstance(r, dict) else None
            opid = (r or {}).get("op_id") if isinstance(r, dict) else None
            mint_ok = isinstance(mint, str) and re.fullmatch(r"[A-Za-z0-9]{1,64}", mint) is not None
            op_ok = isinstance(opid, str) and re.fullmatch(r"[A-Za-z0-9:|#/._\-]{1,200}", opid) is not None
            recs.append({"file": n, "op_id": opid if op_ok else None, "mint": mint if mint_ok else None, "valid": bool(lane_ok and mint_ok and op_ok)})
        except Exception:
            recs.append({"file": n, "op_id": None, "mint": None, "valid": False})
    if not recs and not names:
        return {"state": "clear", "records": [], "why": None}
    more = len(names) > MAX_PER_PASS + 1
    return {"state": "pending", "records": recs[:MAX_PER_PASS], "why": "%d record file(s)%s" % (len(names), " (list truncated)" if more else "")}

def format_status_lines(st, flag_on):
    """Pure. Lines for the account view. Clear -> NO lines (the default output is unchanged). Pending/unknown are labeled as recorded state, and distinct from an ordinary cycle fault.
    The flag reported is THIS process's environment; the trader process's flag may differ, said in the text."""
    if not isinstance(st, dict) or st.get("state") == "clear":
        return []
    flag = ("recovery flag in this process: ON" if flag_on else "recovery flag in this process: OFF (records are recorded state; they are processed only by a trader process with AGT_GRAD_ENTRY_INTENT_ENABLED=1)")
    if st.get("state") == "pending":
        items = ", ".join("%s%s" % ((r.get("mint") if isinstance(r.get("mint"), str) and re.fullmatch(r"[A-Za-z0-9]{1,64}", r.get("mint")) else "?")[:12], "" if r.get("valid") else " (INVALID/UNREADABLE)") for r in st.get("records") or [])
        return ["⚠️ Grad entry-intent: %d record file(s) present [%s] (%s). A leftover record is evidence of an UNRESOLVED entry attempt: it may be a pre-debit leftover, a post-commit cleanup leftover, or an unrecovered entry. This view does not classify which (that needs the account and lane ledgers). It is not an ordinary cycle fault; %s." % (len(st.get("records") or []), items, st.get("why"), flag)]
    return ["⚠️ Grad entry-intent state: UNKNOWN (%s); not reported as clear. %s." % (st.get("why") or "read failed", flag)]
