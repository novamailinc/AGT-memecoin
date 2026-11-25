"""MemecoinAdapter — BrokerAdapter for Solana memecoin trading.

Self-contained paper trading engine: no external bot, no private keys,
no new dependencies. Two modes:

  * paper (default): virtual SOL balance, fills priced from public
    market data (DexScreener/GeckoTerminal via transport.py), every fill
    journaled to fills.jsonl, idempotency keys honored, adapter-level
    kill-switch file coupling (submit refuses while engaged).
  * live: intentionally fail-closed. connect() raises
    BrokerError(BROKER_UNAVAILABLE) — there is no execution path yet,
    so live can never place an order through this adapter.

Spot-only model: BUY spends SOL to open/increase a token position;
SELL reduces open long exposure (FIFO); naked SELL (no holdings) is
rejected with INVALID_ORDER — shorting is unsupported. `volume` is
denominated in SOL on both sides. Account currency is SOL.

State lives under data_dir (default: $FOREX_AGENT_HOME/data/memecoin):
  paper_state.json  virtual balance + open positions + ticket counter
  fills.jsonl       every paper fill, one JSON object per line
  idempotency.json  idempotency_key -> OrderResult (replay, never re-fill)
  killswitch        file presence = engaged; submit_order refuses while set

See RUNTIME.md for the full contract.
"""

import contextlib
import copy
import uuid
import fcntl
import json
import hashlib
import math
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from broker import (
    MAGIC_NUMBER,
    AccountInfo,
    BrokerAdapter,
    BrokerError,
    BrokerHealth,
    Deal,
    Order,
    OrderRequest,
    OrderResult,
    Position,
    Quote,
    SymbolSpec,
    BROKER_NOT_CONNECTED,
    BROKER_UNAVAILABLE,
    CONFIG_INVALID,
    CREDENTIALS_INVALID,
    INVALID_ORDER,
    INVALID_SYMBOL,
    RISK_LIMIT_EXCEEDED,
)
from broker.memecoin.transport import MemecoinMarketData
from broker.memecoin.wallet import NullKeyProvider
from core.market import Candle
from daemon import solana_ids as _solana_ids

logger = logging.getLogger("broker.memecoin")

# Canonical wSOL mint lives in daemon/solana_ids.py — never redefine
# the literal here (Phase 2, D-04).
SOL_MINT = _solana_ids.WSOL_MINT
DEFAULT_PAPER_START_SOL = 10.0
_SOL_PRICE_TTL = 60.0  # seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, obj) -> None:
    """Atomic JSON write: per-process tmp file + fsync + os.replace
    + directory fsync (killswitch/grad pattern, P1-3 2026-11-22).
    The tmp name carries the pid so two processes can never share
    (and interleave) one tmp file — the old fixed `.tmp` name let
    concurrent writers interleave; the missing fsyncs let a hard
    reset come back with a hollow/stale file."""
    tmp = Path("%s.tmp.%d" % (path, os.getpid()))
    with open(tmp, "w") as fh:
        fh.write(json.dumps(obj, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    dfd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


class MemecoinAdapter(BrokerAdapter):
    adapter_name = "memecoin"

    def __init__(self, data_dir: Optional[str] = None,
                 mode: str = "paper",
                 paper_start_sol: float = DEFAULT_PAPER_START_SOL,
                 live_armed: bool = False,
                 wallet_pubkey: str = "",
                 symbols: Optional[Dict[str, str]] = None,
                 market_data: Optional[MemecoinMarketData] = None,
                 chains: Optional[List[str]] = None,
                 key_provider=None,
                 devnet_rpc_url: str = "", shared_account=None, fx_observer=None,
                 shared_mark_observer=None,
                 demo_executor=None, demo_decimals_fn=None):
        """
        data_dir: state directory (created if missing).
        mode: "paper" | "live". Live is fail-closed (see module docstring).
        paper_start_sol: virtual starting balance for a fresh paper state.
        live_armed / wallet_pubkey: required (but not sufficient) for live.
        symbols: {SYMBOL: mint}; SOL is always registered.
        market_data: injectable transport (tests).
        chains: enabled multi-chain names (registry names); default
            ["solana"]. "solana" is this adapter's native SOL-settled
            engine; every other enabled chain gets a USDC-settled
            ChainAdapter with per-chain state under <data_dir>/chains/.
        key_provider: WalletKeyProvider for the runtime key pattern
            (paper default: NullKeyProvider — no key needed).
        devnet_rpc_url: Solana devnet RPC endpoint for mode="devnet".
            The execution layer refuses non-devnet hosts regardless.
        """
        if mode not in ("paper", "devnet", "live", "demo"):
            raise BrokerError(CONFIG_INVALID,
                              f"memecoin mode must be paper|devnet|live|demo, got {mode!r}")
        home = os.environ.get("FOREX_AGENT_HOME", os.path.expanduser("~/.forex-agent"))
        self._data_dir = Path(data_dir or os.path.join(home, "data", "memecoin"))
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._mode = mode
        # Explicit injection only while integration qualification is incomplete.
        # No environment switch activates the unfinished bridge.
        self._shared_account = shared_account
        self._fx_observer = fx_observer
        self._shared_mark_observer = shared_mark_observer
        if shared_account is not None and mode not in ("paper", "demo"):
            raise BrokerError(CONFIG_INVALID, "shared capital bridge is paper/demo-only")
        self._paper_start_sol = float(paper_start_sol)
        self._live_armed = bool(live_armed)
        self._wallet_pubkey = (wallet_pubkey or "").strip()
        self._market = market_data or MemecoinMarketData()
        self._market.register("SOL", SOL_MINT)
        for sym, mint in (symbols or {}).items():
            self._market.register(sym, mint)
        self._enabled_chains = (
            ["solana"] if not chains
            else [str(c).strip().lower() for c in chains if str(c).strip()]
        ) or ["solana"]
        # Eager validation: unknown chain names fail fast, not at first trade.
        from broker.memecoin.chains.registry import chain_names  # noqa: PLC0415 (lazy)
        valid = set(chain_names())
        unknown = [c for c in self._enabled_chains if c not in valid]
        if unknown:
            raise BrokerError(
                CONFIG_INVALID,
                f"unknown memecoin chain(s): {unknown} — valid: "
                f"{sorted(valid)}")
        self._key_provider = key_provider or NullKeyProvider()
        self._chain_adapters: Dict[str, object] = {}
        self._connected = False
        # Devnet execution (mode="devnet" only). Built lazily on connect()
        # so paper mode never imports the signing stack.
        self._devnet_rpc_url = (devnet_rpc_url or "").strip()
        self._devnet = None  # DevnetExecutor once connected
        # Demo execution (mode="demo" only): live quotes through the same
        # Jupiter client the live path uses, settled by SimulationExecutor
        # on a scratch book that mirrors this adapter's state at fill
        # time. Never authoritative, never on-chain, no keys.
        self._demo_bridge = None
        if self._mode == "demo":
            if demo_decimals_fn is None:
                raise BrokerError(
                    CONFIG_INVALID,
                    "demo mode requires demo_decimals_fn — token decimals "
                    "are never guessed")
            from broker.memecoin.execution.demo_bridge import (  # noqa: PLC0415 (lazy)
                DemoExecutionBridge)
            from broker.memecoin.execution.sim_executor import (  # noqa: PLC0415 (lazy)
                SimulationExecutor)
            executor = demo_executor if demo_executor is not None \
                else SimulationExecutor(
                    chain_state_path=str(
                        self._data_dir / "sim_chain.json"))
            self._demo_bridge = DemoExecutionBridge(
                executor, demo_decimals_fn)
        self._request_fingerprint: Optional[str] = None
        self._sol_price_cache: Optional[tuple] = None  # (ts, price_usd)
        with self._state_lock():
            self._refresh_locked()

    # -- persisted paper state -------------------------------------------
    def _state_path(self) -> Path:
        return self._data_dir / "paper_state.json"

    def _load_state(self) -> None:
        path = self._state_path()
        if path.exists():
            state = json.loads(path.read_text())
            state.setdefault("unresolved_execs", {})
            self._validate_native_state(state)
        else:
            if self._fills_path().exists() and not self._intent_path().exists():
                raise ValueError("native state missing with existing fill history")
            state = {"balance_sol": self._paper_start_sol,
                     "positions": {}, "next_ticket": 1,
                     "unresolved_execs": {}}
        self._balance_sol = float(state["balance_sol"])
        self._positions = state["positions"]
        self._next_ticket = state["next_ticket"]
        self._unresolved = state["unresolved_execs"]
        if not isinstance(self._unresolved, dict):
            raise ValueError("invalid native unresolved-execution state")
        self._loaded_native_state = self._native_snapshot()

    def _native_snapshot(self) -> dict:
        return {"balance_sol": self._balance_sol,
                "positions": copy.deepcopy(self._positions),
                "next_ticket": self._next_ticket,
                "unresolved_execs": copy.deepcopy(self._unresolved)}

    @staticmethod
    def _validate_native_state(state: dict) -> None:
        if not isinstance(state, dict):
            raise ValueError("invalid native state")
        balance, ticket, positions = (state["balance_sol"], state["next_ticket"],
                                      state["positions"])
        if (isinstance(balance, bool) or not math.isfinite(float(balance))
                or type(ticket) is not int or ticket < 1
                or not isinstance(positions, dict)
                or not isinstance(state.get("unresolved_execs", {}), dict)):
            raise ValueError("invalid native state values")
        for key, pos in positions.items():
            if str(int(key)) != key or not 0 < int(key) < ticket:
                raise ValueError("invalid native position ticket")
            for name in ("tokens", "volume_sol", "entry_price_sol"):
                if not math.isfinite(float(pos[name])) or float(pos[name]) < 0:
                    raise ValueError("invalid native position economics")
            for name in ("symbol", "direction", "sl", "tp", "comment", "time_open"):
                pos[name]  # missing required fields fail closed

    def _save_state(self) -> None:
        _write_json_atomic(self._state_path(), {
            "balance_sol": self._balance_sol,
            "positions": self._positions,
            "next_ticket": self._next_ticket,
            "unresolved_execs": self._unresolved,
            "updated_at": _utcnow().isoformat(),
        })
        # A direct save outside _commit_native (pre-call seal, UNKNOWN
        # adoption) IS a durable state transition: the intent
        # before-image must never go stale against disk, or a later
        # crash leaves the pending intent matching neither image and
        # recovery freezes on "conflicts with state".
        self._loaded_native_state = self._native_snapshot()

    def _lock_path(self) -> Path:
        return self._data_dir / "paper_state.lock"

    @contextlib.contextmanager
    def _state_lock(self):
        """Exclusive inter-process lock spanning a read-modify-write
        section on the paper state + idempotency store + fills
        journal (P1-3, 2026-11-22). Same pattern as
        killswitch._lock / grad._ledger_lock: several daemon
        processes (launch_scanner, ceo-trader, mirror-exits, agent
        backend) all submit through adapters sharing one data dir,
        and without the lock two processes load the same state, each
        fill, and the second save silently drops the first's fill
        (and the check-then-put on the idempotency store can
        double-fill one key). The lock fd is released automatically
        if the holder dies, so a crashed writer can never wedge
        the files."""
        fd = os.open(str(self._lock_path()),
                     os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _refresh_locked(self) -> None:
        """Re-read on-disk state + invalidate the fills cache.
        Call immediately after acquiring _state_lock: the in-memory
        copy loaded at __init__ is stale the moment another process
        saves, and the per-process fill-key cache predates the other
        process's journal appends."""
        self._load_state()
        self._fill_keys_cache = None
        self._require_shared_binding()
        self._recover_native_intent()
        self._sweep_orphan_holds_locked()

    def _sweep_orphan_holds_locked(self) -> None:
        """Release THIS book's holds that no sealed record claims: the
        crash gap between the account-wide hold and the record save.
        Runs once per process (before this process can hold anything
        itself), after intent recovery so a pending adoption consumes
        its hold first. Holds with a live record are never touched;
        holds never expire silently."""
        if self._shared_account is None or getattr(
                self, "_holds_swept", False):
            return
        prefix = "native-hold:" + self._shared_book_id + ":"
        for op_id in self._shared_account.list_holds(prefix):
            if op_id[len(prefix):] not in self._unresolved:
                self._shared_account.release_hold(
                    op_id, "orphan hold: no sealed record")
        self._holds_swept = True

    def _require_shared_binding(self):
        marker = self._data_dir / "shared_binding.json"
        if marker.exists():
            binding = json.loads(marker.read_text())
            if self._shared_account is None:
                raise ValueError("native book requires authoritative shared account binding")
            actual = str(Path(self._shared_account.LEDGER_PATH).resolve())
            if binding.get("account_path") != actual:
                raise ValueError("native shared account identity conflict")
            self._shared_account.account_identity(binding["account_id"])
            self._shared_book_id = binding["book_id"]
        elif self._shared_account is not None:
            binding = {"version":1,"book_id":uuid.uuid4().hex,
                       "account_id":self._shared_account.account_identity(),
                       "account_path":str(Path(self._shared_account.LEDGER_PATH).resolve())}
            _write_json_atomic(marker,binding)
            self._shared_book_id = binding["book_id"]

    def _intent_path(self) -> Path:
        return self._data_dir / "native_pending.json"

    def _commit_native(self, fill: dict) -> None:
        """Caller owns the state lock. Persist exact after-image before effects.

        This is native-only durability, not shared-capital admission. No market
        quote, FX, FIFO selection or ticket allocation is repeated on recovery.
        """
        if fill.get("kind") in ("BUY", "SELL"):
            fill = dict(fill, request_fingerprint=self._request_fingerprint)
        fill = dict(fill, native_op_id=uuid.uuid4().hex,
                    ts=_utcnow().isoformat(), mode=self._mode)
        self._validate_fill(fill)
        self._fill_keys_cache = None
        intent = {"version": 1, "fill": fill,
                  "before": self._loaded_native_state,
                  "after": self._native_snapshot()}
        self._validate_native_state(intent["after"])
        intent["sha256"] = hashlib.sha256(json.dumps(
            intent, sort_keys=True, allow_nan=False).encode()).hexdigest()
        if self._shared_account is not None:
            if fill["kind"] == "BUY":
                from broker.memecoin.shared_capital import entry_plan
                if any(not p.get("shared_pos_id") for p in intent["before"]["positions"].values()):
                    raise BrokerError(BROKER_UNAVAILABLE, "unbound legacy native inventory")
                fx = self._entry_pricing_fx
                fill["price_evidence"]={"kind":"USD_ratio","fx":fx,
                                        "token":self._entry_token_evidence}
                identity = fill.get("idempotency_key") or fill["native_op_id"]
                intent["shared_entry"] = entry_plan(
                    "main", self._market.mint_for(fill["symbol"]), fill["ticket"],
                    fill["tokens"],
                    fill["volume_sol"] + float(fill.get("fee_sol") or 0.0),
                    fx,
                    "native-entry:" + self._shared_book_id + ":" + identity,
                    getattr(self, "_fx_eval_now_override", None),
                    hold_op_id=getattr(self, "_pending_hold_op_id", None))
            else:
                fill["price_evidence"] = (getattr(self,"_shared_last_exit_price",None)
                    if fill.get("price_sol") else {"kind":"explicit_zero","reason":fill.get("close_reason")})
                intent["shared_exits"] = []
                for index, allocation in enumerate(fill["allocations"]):
                    pos = intent["before"]["positions"][str(allocation["ticket"])]
                    if not pos.get("shared_pos_id"):
                        raise BrokerError(BROKER_UNAVAILABLE, "unbound native exit requires reconciliation")
                    intent["shared_exits"].append({
                        "lane": pos["shared_lane"],
                        "mint": pos["shared_mint"],
                        "pos_id": pos["shared_pos_id"], "allocation": allocation,
                        "sold_at": datetime.fromisoformat(fill["ts"]).timestamp(),
                        "op_id": "native-pending:" + fill["native_op_id"] + ":" + str(index)})
            self._seal_intent(intent)
        _write_json_atomic(self._intent_path(), intent)
        self._recover_native_intent()

    @staticmethod
    def _seal_intent(intent):
        intent.pop("sha256", None)
        intent["sha256"] = hashlib.sha256(json.dumps(
            intent, sort_keys=True, allow_nan=False).encode()).hexdigest()

    def _observe_entry_fx(self):
        from broker.memecoin.shared_capital import validate_fx
        if self._fx_observer is not None:
            return validate_fx(self._fx_observer())
        q = self._market.get_quote("SOL")
        observed = q.get("ts") if isinstance(q, dict) else None
        if isinstance(observed, datetime):
            observed = observed.timestamp()
        return validate_fx({"usd_per_sol": q.get("price_usd") if q else None,
                            "observed_at": observed,
                            "source": q.get("source") if q else None})

    def _recover_native_intent(self) -> None:
        path = self._intent_path()
        if not path.exists():
            return
        # A corrupt intent is never equivalent to no intent. Admission freezes.
        intent = json.loads(path.read_text())
        if intent.get("version") != 1:
            raise ValueError("unsupported native pending intent")
        digest = intent.pop("sha256")
        if digest != hashlib.sha256(json.dumps(
                intent, sort_keys=True, allow_nan=False).encode()).hexdigest():
            raise ValueError("native pending intent checksum mismatch")
        fill, after = intent["fill"], intent["after"]
        self._validate_native_state(after)
        if self._loaded_native_state not in (intent["before"], after):
            raise ValueError("native pending intent conflicts with state")
        op_id = fill["native_op_id"]
        if not isinstance(op_id, str) or not op_id:
            raise ValueError("invalid native operation identity")
        entries = []
        if self._fills_path().exists():
            entries = [json.loads(line) for line in
                       self._fills_path().read_text().splitlines() if line.strip()]
        matches = [e for e in entries if e.get("native_op_id") == op_id]
        if matches and (len(matches) != 1 or matches[0] != fill):
            raise ValueError("native pending intent conflicts with fill journal")
        # Validate the entire history and the intended fill BEFORE either
        # projection. Even a matching op cannot hide a duplicate elsewhere.
        keys, operations, request_keys = set(), set(), set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid native fill journal record")
            pair = (entry.get("ticket"), entry.get("kind"))
            if pair in keys:
                raise ValueError("duplicate native fill history")
            keys.add(pair)
            operation = entry.get("native_op_id")
            if operation:
                if operation in operations:
                    raise ValueError("duplicate native operation identity")
                operations.add(operation)
            request_key = entry.get("idempotency_key")
            if request_key and entry.get("kind") in ("BUY", "SELL"):
                if request_key in request_keys:
                    raise ValueError("duplicate native request identity")
                request_keys.add(request_key)
        request_key = fill.get("idempotency_key")
        if (not matches and request_key and fill.get("kind") in ("BUY", "SELL")
                and request_key in request_keys):
            raise ValueError("pending native request identity conflicts with history")
        self._fill_keys_cache = {pair for pair in keys if not matches or
                                 pair != (fill["ticket"], fill["kind"])}
        self._fill_buy_ts = {e["ticket"]: e.get("ts") for e in entries
                            if e.get("kind") == "BUY" and e is not (matches[0] if matches else None)}
        self._validate_fill(fill)
        self._fill_keys_cache = None
        if intent.get("shared_entry") is not None:
            from broker.memecoin.shared_capital import apply_entry
            if self._shared_account is None:
                raise ValueError("shared entry intent requires its account binding")
            receipt, refusal = apply_entry(intent["shared_entry"], self._shared_account)
            if refusal is not None:
                # The account owns a durable terminal refusal for this attempt.
                # Delete no evidence of an uncertain commit and never debit SOL.
                self._load_state()
                path.unlink()
                _fsync_dir(path.parent)
                raise BrokerError(RISK_LIMIT_EXCEEDED, "shared capital refused: " + refusal)
            if intent.get("shared_receipt") not in (None, receipt):
                raise ValueError("stored shared entry receipt conflict")
            intent["shared_receipt"] = receipt
            binding = {"shared_pos_id": receipt["pos_id"],
                       "shared_entry_op_id": intent["shared_entry"]["op_id"],
                       "shared_entry_fx": intent["shared_entry"]["fx"],
                       "shared_entry_usd": receipt["stake"], "shared_lane": receipt["lane"],
                       "shared_mint": receipt["mint"], "shared_book_id": self._shared_book_id,
                       # ORIGINAL generation captured atomically by the debit made in THIS process (None when the entry was adopted from a recovered
                       # receipt: unbound, so its exits are refused fail-closed rather than rebound)
                       "shared_account_id": (getattr(self._shared_account, "debit_binding", lambda *_a: None)(intent["shared_entry"]["op_id"], receipt["pos_id"]))}
            after["positions"][str(fill["ticket"])].update(binding)
            fill.update(binding)
            self._seal_intent(intent)
            _write_json_atomic(path, intent)
        if intent.get("shared_exits") is not None:
            if self._shared_account is None:
                raise ValueError("shared exit intent requires its account binding")
            from daemon.launch_scanner import pending_sol
            for item in intent["shared_exits"]:
                allocation = item["allocation"]
                ok, why = pending_sol.reserve(
                    self._shared_account, item["lane"], item["mint"], item["pos_id"],
                    allocation["tokens"], allocation["proceeds_sol"], allocation["ticket"],
                    allocation["basis_sol"], item["sold_at"], item["op_id"], fee_sol=0.)
                if not ok:
                    raise ValueError("shared pending exit conflict: " + why)
                receipt, error = self._shared_account.operation_receipt(item["op_id"])
                if error or receipt is None:
                    raise ValueError("shared pending exit receipt unavailable: " + str(error))
                expected = {"event": "pending_sol", "lane":item["lane"],
                            "mint":item["mint"], "pos_id":item["pos_id"],
                            "qty":allocation["tokens"], "proceeds_sol":allocation["proceeds_sol"],
                            "ticket":allocation["ticket"], "native_basis_sol":allocation["basis_sol"],
                            "sold_at":item["sold_at"], "fee_sol":0., "instance":True}
                if any(receipt.get(k)!=v for k,v in expected.items()):
                    raise ValueError("shared pending exit receipt conflict")
        self._balance_sol = float(after["balance_sol"])
        self._positions = copy.deepcopy(after["positions"])
        self._next_ticket = int(after["next_ticket"])
        # The unresolved-execution map is part of the transaction's
        # after-image too: without this a crash mid-adoption restores
        # the fill but keeps the record sealed, and a later reconcile
        # would apply the same landed fill a second time.
        if "unresolved_execs" in after:
            self._unresolved = copy.deepcopy(after["unresolved_execs"])
        self._save_state()
        self._loaded_native_state = self._native_snapshot()
        if not matches:
            self._journal_fill(fill)
        else:
            # A prior replace can be visible even if its directory fsync failed.
            # Re-establish durability before deleting the recovery source.
            with open(self._fills_path(), "rb") as fh:
                os.fsync(fh.fileno())
            _fsync_dir(self._data_dir)
        # No later mutation can start until every projection is durable.
        path.unlink()
        _fsync_dir(path.parent)

    def retry_pending_usd(self):
        """Existing scanner cadence retries dated FX, never blocking native exits."""
        if self._shared_account is None:
            return []
        snapshot=self._shared_account.snapshot()
        if not any(p.get("lane")=="main" for p in snapshot.get("pending_sol",[])):
            return []
        try:
            fx=self._observe_entry_fx()
        except Exception:
            return [{"ok":False,"reason":"USD settlement pending current FX"}]
        return self.settle_pending_usd(fx)

    def settle_pending_usd(self, fx: dict) -> list:
        """Explicit dated SOL-to-USD settlement; no token exit or native rollback.

        All pending items are already account-owned SOL, unspendable until each
        exact allocation settles. Native->account lock order matches execution.
        An unsettled item remains pending and cannot be called converged.
        """
        self._ensure()
        if self._shared_account is None:
            raise ValueError("no shared account binding")
        from broker.memecoin.shared_capital import validate_fx
        from daemon.launch_scanner import pending_sol
        fx = validate_fx(fx)
        results = []
        with self._state_lock():
            self._refresh_locked()
            snapshot = self._shared_account.snapshot()
            if snapshot.get("degraded"):
                raise ValueError("shared account degraded")
            for item in snapshot.get("pending_sol", []):
                if item["lane"] != "main":
                    continue
                # Each allocation is atomic in the shared WAL; no batch atomicity.
                proceeds = round(item["proceeds_sol"] * fx["usd_per_sol"], 6)
                op_id = "native-settle:" + item["pending_id"]
                ok, why = pending_sol.settle(
                    self._shared_account, item["lane"], item["mint"], item["pos_id"],
                    item["pending_id"], fx, proceeds, op_id)
                if ok:
                    receipt, error = self._shared_account.operation_receipt(op_id)
                    if error or receipt is None:
                        raise ValueError("shared settlement receipt unavailable")
                results.append({"pending_id":item["pending_id"], "ok":ok, "reason":why})
        return results

    def _fills_path(self) -> Path:
        return self._data_dir / "fills.jsonl"

    def _journal_fill(self, entry: dict) -> None:
        entry = dict(entry)
        entry.setdefault("ts", _utcnow().isoformat())
        entry.setdefault("mode", self._mode)
        # Ledger hygiene (2026-10-15 forensics): the fills journal had
        # 16 corrupted records (exit timestamped before entry, incl.
        # EDWARD) and 6 duplicate (ticket, kind) pairs. Validate
        # writer-side so bad records can never be appended:
        #  1. reject duplicate (ticket, kind)
        #  2. reject CLOSE whose ts precedes its BUY's ts
        # Both fail LOUD (raise) — a corrupt ledger is worse than a
        # missed write, and the caller must know.
        self._validate_fill(entry)
        # Atomic replacement avoids a torn append losing the trailing operation
        # boundary. The pending intent remains until this projection is durable.
        path = self._fills_path()
        old = path.read_text() if path.exists() else ""
        tmp = Path("%s.tmp.%d" % (path, os.getpid()))
        with open(tmp, "w") as fh:
            fh.write(old)
            if old and not old.endswith("\n"):
                fh.write("\n")
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    def _seen_fill_keys(self) -> set:
        """(ticket, kind) pairs already in the journal. Cached per process."""
        if getattr(self, "_fill_keys_cache", None) is None:
            keys = set()
            buy_ts = {}
            path = self._fills_path()
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    d = json.loads(line)
                    if not isinstance(d, dict):
                        raise ValueError("invalid native fill journal record")
                    t, k = d.get("ticket"), d.get("kind")
                    if t is not None and k:
                        if (t, k) in keys:
                            raise ValueError("duplicate native fill history")
                        keys.add((t, k))
                    if k == "BUY" and t is not None:
                        buy_ts[t] = d.get("ts")
            self._fill_keys_cache = keys
            self._fill_buy_ts = buy_ts
        return self._fill_keys_cache

    def _validate_fill(self, entry: dict) -> None:
        keys = self._seen_fill_keys()
        t, k = entry.get("ticket"), entry.get("kind")
        if t is not None and k and (t, k) in keys:
            raise ValueError(
                "fills journal: duplicate (ticket=%r, kind=%r) rejected"
                % (t, k))
        if k == "CLOSE" and t is not None:
            buy_ts = getattr(self, "_fill_buy_ts", {}).get(t)
            if buy_ts:
                try:
                    from datetime import datetime
                    bts = datetime.fromisoformat(str(buy_ts))
                    cts = datetime.fromisoformat(str(entry.get("ts")))
                    if cts < bts:
                        raise ValueError(
                            "fills journal: CLOSE ts %s precedes BUY ts %s "
                            "(ticket=%r) rejected" % (entry.get("ts"),
                                                      buy_ts, t))
                except ValueError:
                    raise
                except Exception:
                    pass  # unparseable ts: don't block the write
        # record the key so intra-process duplicates are caught too
        if t is not None and k:
            keys.add((t, k))
            if k == "BUY":
                getattr(self, "_fill_buy_ts", {})[t] = entry.get("ts")

    def _idempotency_path(self) -> Path:
        return self._data_dir / "idempotency.json"

    def _idempotent_get(self, key: str) -> Optional[dict]:
        path = self._idempotency_path()
        store = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(store, dict):
            raise ValueError("invalid native idempotency store")
        return store.get(key)

    def _idempotent_put(self, key: str, result: dict) -> None:
        path = self._idempotency_path()
        store = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(store, dict):
            raise ValueError("invalid native idempotency store")
        store[key] = result
        _write_json_atomic(self._idempotency_path(), store)

    def _idempotent_recover_from_journal(self, key: str) -> Optional[OrderResult]:
        """Rebuild an OrderResult from the fill journal.

        Crash window: the fill is journaled (and state saved) BEFORE the
        idempotency store is written. If the process dies in between, a
        retry would otherwise fill a second time. The journal already
        records the idempotency key, so a retry can recover the original
        fill instead of duplicating it.
        """
        path = self._fills_path()
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):  # full-lifetime identity, not a recent window
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("idempotency_key") != key:
                continue
            if entry.get("kind") not in ("BUY", "SELL"):
                # Auxiliary entries (e.g. DEVNET_EXEC linkage) carry no
                # fill data — skip them during recovery.
                continue
            return OrderResult(
                ticket=int(entry["ticket"]),
                symbol=str(entry["symbol"]),
                direction=str(entry.get("kind", "BUY")),
                volume=float(entry["volume_sol"]),
                price=float(entry["price_sol"]),
                retcode=0,
                message="paper fill (recovered from journal)",
                raw={"mode": "paper",
                     "tokens": float(entry.get("tokens", 0.0)),
                     "request_fingerprint": entry.get("request_fingerprint"),
                     **({"realized_pnl_sol": entry["realized_pnl_sol"],
                         "closed_tickets": entry["closed_tickets"]}
                        if entry.get("kind") == "SELL" else {}),
                     "recovered": True},
            )
        return None

    def _kill_switch_engaged(self) -> bool:
        return (self._data_dir / "killswitch").exists()

    # -- pricing -----------------------------------------------------------
    def _sol_price_usd(self) -> float:
        now = time.monotonic()
        if self._sol_price_cache and now - self._sol_price_cache[0] < _SOL_PRICE_TTL:
            return self._sol_price_cache[1]
        q = self._market.get_quote("SOL")
        if not q or not q.get("price_usd"):
            raise BrokerError(BROKER_UNAVAILABLE,
                              "memecoin market data unreachable (SOL/USD quote "
                              "failed) — refusing to price paper fills",
                              detail={"reason": "market_data_unreachable"})
        price = float(q["price_usd"])
        self._sol_price_cache = (now, price)
        return price

    def _verify_position_mints(self, symbol):
        if self._shared_account is not None:
            mint = self._market.mint_for(symbol)
            for pos in self._positions.values():
                if pos["symbol"] == symbol and not pos.get("shared_mint"):
                    raise BrokerError(INVALID_ORDER, "unbound legacy native inventory")
                if pos["symbol"] == symbol and pos.get("shared_mint") != mint:
                    raise BrokerError(INVALID_ORDER, "symbol mapping differs from immutable entry mint")

    def _validated_usd_quote(self, symbol, quote):
        if not isinstance(quote,dict):
            raise BrokerError(BROKER_UNAVAILABLE,"token USD observation unavailable")
        observed=quote.get("ts")
        if isinstance(observed,datetime): observed=observed.timestamp()
        usd=quote.get("price_usd")
        if (quote.get("mint")!=self._market.mint_for(symbol)
                or not isinstance(quote.get("source"),str) or not quote["source"]
                or isinstance(observed,bool) or not isinstance(observed,(int,float))
                or not math.isfinite(observed) or not 0<=time.time()-observed<=_SOL_PRICE_TTL
                or isinstance(usd,bool) or not isinstance(usd,(int,float))
                or not math.isfinite(usd) or usd<=0):
            raise BrokerError(BROKER_UNAVAILABLE,"token USD observation invalid or stale")
        return {"mint":quote["mint"],"source":quote["source"],"observed_at":observed,
                "price_usd":usd,"pair":quote.get("pair")}

    def _shared_price_sol(self, symbol):
        self._verify_position_mints(symbol)
        quote = self._market.get_quote(symbol)
        if not isinstance(quote,dict):
            raise BrokerError(BROKER_UNAVAILABLE,"shared native token price unavailable")
        native = quote.get("price_sol")
        observed = quote.get("ts")
        if isinstance(observed,datetime):
            observed=observed.timestamp()
        if (quote.get("mint")==self._market.mint_for(symbol)
                and quote.get("price_sol_quote_mint")==SOL_MINT
                and isinstance(native,(int,float)) and not isinstance(native,bool)
                and math.isfinite(native) and native>0
                and isinstance(observed,(int,float)) and not isinstance(observed,bool)
                and 0<=time.time()-observed<=_SOL_PRICE_TTL
                and isinstance(quote.get("source"),str) and quote["source"]):
            self._shared_last_exit_price = {"kind":"native_SOL_pair", "price_sol":native,
                "mint":quote["mint"],"source":quote["source"],"observed_at":observed,
                "quote_mint":quote["price_sol_quote_mint"], "pair":quote.get("pair")}
            return float(native)
        fx = self._observe_entry_fx()
        token_evidence=self._validated_usd_quote(symbol,quote)
        price=token_evidence["price_usd"]/fx["usd_per_sol"]
        self._shared_last_exit_price={"kind":"USD_ratio", "price_sol":price,
                                     "mint":self._market.mint_for(symbol),"fx":fx,"token":token_evidence}
        return price

    def _price_sol(self, symbol: str) -> float:
        """Token price in SOL, via USD legs. Raises BROKER_UNAVAILABLE
        when market data is unreachable — paper fills are never priced
        from stale or missing data."""
        q = self._market.get_quote(symbol)
        if not q or not q.get("price_usd"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                f"no market quote for {symbol!r} — paper fill refused",
                detail={"reason": "market_data_unreachable", "symbol": symbol})
        return float(q["price_usd"]) / self._sol_price_usd()

    def _price_sol_quiet(self, symbol: str) -> Optional[float]:
        try:
            if self._shared_account is not None:
                return self._shared_price_sol(symbol)
            return self._price_sol(symbol)
        except BrokerError:
            return None

    # -- lifecycle ---------------------------------------------------------
    def connect(self, creds: dict) -> None:
        if self._mode == "live":
            # Fail-closed: there is deliberately no execution path yet.
            # The armed/wallet checks run first so misconfiguration is
            # reported as misconfiguration, not as an outage.
            if not (self._live_armed and self._wallet_pubkey):
                raise BrokerError(
                    CREDENTIALS_INVALID,
                    "live memecoin mode requires MEMECOIN_LIVE_ARMED=true and "
                    "MEMECOIN_WALLET_PUBKEY set (public key only — the wallet "
                    "secret must never leave your custody)")
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "live memecoin execution is not implemented: this adapter "
                "is paper-only by design. See broker/memecoin/RUNTIME.md.",
                detail={"reason": "live_not_implemented"})
        if self._mode == "devnet":
            self._connect_devnet()
            return
        self._connected = True
        logger.info("MemecoinAdapter connected (paper mode, data_dir=%s)",
                    self._data_dir)

    def _connect_devnet(self) -> None:
        """Real devnet execution. Fails closed without a key, without a
        configured wallet pubkey, on pubkey mismatch, or when devnet is
        unreachable."""
        from broker.memecoin.execution.devnet_executor import (  # noqa: PLC0415 (lazy)
            DevnetExecutor)
        from broker.memecoin.execution.rpc import (  # noqa: PLC0415 (lazy)
            DEFAULT_DEVNET_RPC)
        if not self._wallet_pubkey:
            raise BrokerError(
                CREDENTIALS_INVALID,
                "devnet memecoin mode requires MEMECOIN_WALLET_PUBKEY set "
                "(public key only)")
        executor = DevnetExecutor(
            key_provider=self._key_provider,
            rpc_url=self._devnet_rpc_url or DEFAULT_DEVNET_RPC,
            expected_pubkey=self._wallet_pubkey)
        wallet = executor.connect()  # raises on any problem
        self._devnet = executor
        self._connected = True
        logger.info("MemecoinAdapter connected (devnet mode, wallet=%s)",
                    wallet)

    def disconnect(self) -> None:
        self._connected = False

    def _ensure(self) -> None:
        if not self._connected:
            raise BrokerError(BROKER_NOT_CONNECTED,
                              "Not connected — call connect() first.")

    # -- multi-chain plumbing --------------------------------------------
    @property
    def enabled_chains(self) -> List[str]:
        """Enabled multi-chain names (registry names)."""
        return list(self._enabled_chains)

    def chain_adapter(self, name: str):
        """Return the ChainAdapter for an enabled non-solana chain.

        "solana" is this adapter's native SOL-settled engine, so it has
        no ChainAdapter — use submit_order(req) without a chain for the
        Solana path. Raises CONFIG_INVALID for unknown/disabled chains
        (and for "solana", to avoid a confusing dual-book).
        """
        from broker.memecoin.chains import get_chain_adapter  # noqa: PLC0415 (lazy)
        key = (name or "").strip().lower()
        if key == "solana":
            raise BrokerError(
                CONFIG_INVALID,
                "'solana' is served by this adapter's native SOL-settled "
                "engine — call submit_order(req) without a chain for the "
                "Solana path")
        if key not in self._enabled_chains:
            raise BrokerError(
                CONFIG_INVALID,
                f"chain {name!r} is not enabled (enabled: "
                f"{self._enabled_chains})")
        if key not in self._chain_adapters:
            self._chain_adapters[key] = get_chain_adapter(
                key, data_dir=str(self._data_dir), mode=self._mode,
                key_provider=self._key_provider, armed=self._live_armed)
        return self._chain_adapters[key]

    def chain_status(self) -> dict:
        """Per-chain status fragments, keyed by chain name. Never raises."""
        self._ensure()
        out = {
            "solana": {
                "chain": "solana",
                "family": "solana",
                "mode": self._mode,
                "note": "native SOL-settled engine",
                "trading_available": (
                    self._mode == "paper" and not self._kill_switch_engaged()),
                "detail": {
                    "balance_sol": round(self._balance_sol, 9),
                    "open_positions": len(self._positions),
                },
            }
        }
        for name in self._enabled_chains:
            if name == "solana":
                continue
            try:
                out[name] = self.chain_adapter(
                    name).broker_status_fragment()
            except Exception as exc:  # defensive: status must never raise
                out[name] = {"chain": name,
                             "detail": {"probe_error": str(exc)[:200]}}
        return out

    # -- account -----------------------------------------------------------
    def _open_value_sol(self) -> float:
        """Current market value of open positions, in SOL."""
        total = 0.0
        for p in self._positions.values():
            px = self._price_sol_quiet(p["symbol"])
            if px is None:
                continue
            total += px * p["tokens"]
        return total

    def account_info(self) -> AccountInfo:
        self._ensure()
        if self._shared_account is not None:
            with self._state_lock():
                self._refresh_locked()
                from daemon.launch_scanner import account_marks
                try:
                    fx = self._observe_entry_fx()
                    marks = (self._shared_mark_observer or account_marks.collect_marks)()
                    snap = self._shared_account.snapshot(marks)
                except Exception as exc:
                    raise BrokerError(BROKER_UNAVAILABLE, "shared SOL sizing inputs unavailable") from exc
                equity = snap.get("marked_equity_conservative")
                if snap.get("degraded") or equity is None:
                    raise BrokerError(BROKER_UNAVAILABLE, "shared SOL sizing marks unavailable")
                rate = fx["usd_per_sol"]
                self._last_shared_sizing_evidence = {"fx":fx, "usd_equity":equity,
                    "usd_spendable":snap["cash_spendable"], "currency":"SOL"}
                return AccountInfo(balance=snap["cash_spendable"]/rate,
                                   equity=equity/rate, currency="SOL", margin=0.,
                                   free_margin=snap["cash_spendable"]/rate, leverage=1,
                                   login=0, server="memecoin-shared-paper-sol-sizing")
        # Equity = cash + current market value of open positions. (BUY
        # deducts the cost from cash, so adding only unrealized P/L would
        # double-count the cost basis.)
        equity = self._balance_sol + self._open_value_sol()
        return AccountInfo(
            balance=self._balance_sol,
            equity=equity,
            currency="SOL",
            margin=0.0,
            free_margin=self._balance_sol,
            leverage=1,
            login=0,
            server="memecoin-paper" if self._mode == "paper" else "memecoin-live",
        )

    def risk_state_identity(self):
        if self._shared_account is None:
            return None
        with self._state_lock():
            self._refresh_locked()
            binding = json.loads((self._data_dir / "shared_binding.json").read_text())
            return {"book_id":binding["book_id"], "account_id":binding["account_id"],
                    "currency":"SOL", "mode":"shared-usd-sol-sizing-v1"}

    def scanner_entry_evidence(self,request,book,publish=None):
        """Read exact durable BUY and current inventory, never submit or quote."""
        self._ensure()
        if self._shared_account is None:return None
        with self._state_lock():
            self._refresh_locked()
            binding=json.loads((self._data_dir/'shared_binding.json').read_text())
            actual={'book_id':binding['book_id'],'account_id':binding['account_id'],
                    'currency':'SOL','mode':'shared-usd-sol-sizing-v1'}
            if actual!=book:raise ValueError('scanner entry book conflict')
            normalized=copy.deepcopy(request)
            normalized['symbol']=normalized['symbol'].upper()
            mint=normalized.pop('mint')
            normalized['mint']=mint
            for field in ('volume','stop_loss','take_profit'):
                if normalized[field] is not None:normalized[field]=float(normalized[field])
            fingerprint=hashlib.sha256(json.dumps(normalized,sort_keys=True,allow_nan=False).encode()).hexdigest()
            entries=[json.loads(line) for line in self._fills_path().read_text().splitlines() if line.strip()] if self._fills_path().exists() else []
            matches=[e for e in entries if e.get('kind')=='BUY' and e.get('idempotency_key')==request['idempotency_key']]
            if not matches:return None
            if len(matches)!=1:raise ValueError('duplicate scanner entry BUY')
            fill=matches[0]
            if (fill.get('request_fingerprint')!=fingerprint or fill.get('shared_book_id')!=book['book_id']
                    or fill.get('shared_mint')!=mint or not fill.get('native_op_id')):
                raise ValueError('scanner entry BUY identity conflict')
            price,tokens,volume=fill['price_sol'],fill['tokens'],fill['volume_sol']
            if (any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (price,tokens,volume))
                    or not math.isclose(price*tokens,volume,rel_tol=1e-10,abs_tol=1e-12)
                    or volume!=normalized['volume']):raise ValueError('scanner entry BUY economics conflict')
            datetime.fromisoformat(fill['ts'])
            receipt,error=self._shared_account.operation_receipt(fill['shared_entry_op_id'])
            expected={'event':'debit','instance':True,'lane':fill.get('shared_lane'),
                'mint':mint,'stake':fill.get('shared_entry_usd'),'qty':tokens,'pos_id':fill.get('shared_pos_id')}
            expected_op='native-entry:'+book['book_id']+':'+request['idempotency_key']
            fx=fill.get('shared_entry_fx')
            if (error or not isinstance(receipt,dict) or fill.get('shared_entry_op_id')!=expected_op
                    or expected['lane']!='main' or not expected['pos_id']
                    or not isinstance(fx,dict) or type(fx.get('usd_per_sol')) not in (int,float)
                    or not math.isfinite(fx['usd_per_sol']) or fx['usd_per_sol']<=0
                    or expected['stake']!=round(volume*fx['usd_per_sol'],6)
                    or any(receipt.get(k)!=v for k,v in expected.items())):
                raise ValueError('scanner entry shared debit receipt conflict')
            pos=self._positions.get(str(fill['ticket']))
            if pos is None:return {'state':'closed','fill':copy.deepcopy(fill)}
            for field in ('symbol','tokens','volume_sol','entry_price_sol'):
                expected=fill['price_sol'] if field=='entry_price_sol' else fill[field]
                if pos[field]!=expected:raise ValueError('scanner entry inventory conflict')
            if pos.get('shared_pos_id')!=fill.get('shared_pos_id'):raise ValueError('scanner entry lot conflict')
            with open(self._fills_path(),'rb') as fh:os.fsync(fh.fileno())
            _fsync_dir(self._data_dir)
            closed_tickets=[]
            for ticket in {e.get('ticket') for e in entries if e.get('kind')=='CLOSE'}:
                identity=dict(book,ticket=ticket)
                self._validated_scanner_close(identity,entries,binding)
                closed_tickets.append(ticket)
            evidence={'state':'open','fill':copy.deepcopy(fill),'position':copy.deepcopy(pos),
                      'closed_tickets':closed_tickets}
            if publish is not None:publish(evidence)
            return evidence

    def scanner_close_identity(self, ticket):
        """Exact shared-book identity for a scanner recovery intent."""
        self._ensure()
        if self._shared_account is None:
            return None
        with self._state_lock():
            self._refresh_locked()
            binding = json.loads((self._data_dir / "shared_binding.json").read_text())
            return {"book_id":binding["book_id"], "account_id":binding["account_id"],
                    "currency":"SOL", "mode":"shared-usd-sol-sizing-v1", "ticket":int(ticket)}

    def scanner_close_receipt(self, identity):
        """Recover only an exact durable CLOSE, never infer it from absence."""
        self._ensure()
        with self._state_lock():
            self._refresh_locked()
            binding = json.loads((self._data_dir / "shared_binding.json").read_text())
            entries=[json.loads(line) for line in self._fills_path().read_text().splitlines() if line.strip()] if self._fills_path().exists() else []
            return self._validated_scanner_close(identity,entries,binding)

    def _validated_scanner_close(self,identity,entries,binding):
        expected={'book_id':binding['book_id'],'account_id':binding['account_id'],
                  'currency':'SOL','mode':'shared-usd-sol-sizing-v1','ticket':identity['ticket']}
        if identity!=expected:raise ValueError('scanner close book identity conflict')
        matches=[e for e in entries if e.get('kind')=='CLOSE' and e.get('ticket')==identity['ticket']]
        if not matches:return None
        if len(matches)!=1:raise ValueError('duplicate scanner CLOSE receipt')
        receipt=matches[0]
        if str(identity['ticket']) in self._positions or not receipt.get('native_op_id'):
            raise ValueError('scanner CLOSE receipt conflicts with inventory')
        allocation=receipt.get('allocations')
        buys=[e for e in entries if e.get('kind')=='BUY' and e.get('ticket')==identity['ticket']]
        if len(buys)!=1 or not isinstance(allocation,list) or len(allocation)!=1:
            raise ValueError('scanner CLOSE source unavailable')
        buy=buys[0];a=allocation[0]
        if (a.get('ticket')!=identity['ticket'] or receipt.get('symbol')!=buy.get('symbol')
                or buy.get('shared_book_id')!=binding['book_id']):raise ValueError('scanner CLOSE allocation identity conflict')
        price,basis,proceeds,tokens,pnl=(receipt['price_sol'],a['basis_sol'],a['proceeds_sol'],a['tokens'],receipt['realized_pnl_sol'])
        if (any(type(x) not in (int,float) or not math.isfinite(x) for x in (price,basis,proceeds,tokens,pnl))
                or min(price,basis,proceeds,tokens)<0
                or tokens>buy['tokens']+1e-10
                or not math.isclose(basis,tokens*buy['price_sol'],rel_tol=1e-10,abs_tol=1e-12)
                or not math.isclose(proceeds,tokens*price,rel_tol=1e-10,abs_tol=1e-12)
                or not math.isclose(pnl,proceeds-basis,rel_tol=1e-10,abs_tol=1e-12)):
            raise ValueError('scanner CLOSE allocation economics conflict')
        pending,error=self._shared_account.operation_receipt('native-pending:'+receipt['native_op_id']+':0')
        expected_pending={'event':'pending_sol','lane':buy.get('shared_lane'),'mint':buy.get('shared_mint'),
            'pos_id':buy.get('shared_pos_id'),'qty':tokens,'proceeds_sol':proceeds,
            'ticket':identity['ticket'],'native_basis_sol':basis,
            'sold_at':datetime.fromisoformat(receipt['ts']).timestamp(),'fee_sol':0.,'instance':True}
        if (error or not isinstance(pending,dict) or a.get('entry_price_sol')!=buy.get('price_sol')
                or any(pending.get(k)!=v for k,v in expected_pending.items())):
            raise ValueError('scanner CLOSE shared pending receipt conflict')
        with open(self._fills_path(),'rb') as fh:os.fsync(fh.fileno())
        _fsync_dir(self._data_dir)
        return copy.deepcopy(receipt)

    def shared_account_snapshot(self):
        """USD reporting only. Never feed native SOL risk sizing."""
        self._ensure()
        if self._shared_account is None:
            raise ValueError("no shared account binding")
        return self._shared_account.snapshot()

    # -- market data ---------------------------------------------------------
    def symbols(self, names: Optional[List[str]] = None) -> List[SymbolSpec]:
        self._ensure()
        known = self._market.known_symbols()
        if names is not None:
            unknown = [n for n in names if n.upper() not in known]
            if unknown:
                raise BrokerError(INVALID_SYMBOL,
                                  f"unknown memecoin symbol(s): {unknown}")
            known = [n.upper() for n in names]
        return [SymbolSpec(
            name=n,
            volume_min=0.001,      # SOL
            volume_max=1000.0,     # SOL
            volume_step=0.001,
            tick_value=1.0,        # account currency is SOL; 1 tick = 1 SOL per unit
            tick_size=1e-9,
            contract_size=1.0,
            digits=9,
            point=1e-9,
        ) for n in known]

    def candles(self, symbol: str, timeframe: str, count: int = 200) -> List[Candle]:
        self._ensure()
        return self._market.get_candles(symbol, timeframe, count)

    def quote(self, symbol: str) -> Optional[Quote]:
        self._ensure()
        px = self._price_sol_quiet(symbol)
        if px is None:
            return None
        now = _utcnow()
        # Spot quote: no spread on the paper book; spread checks see 0.
        return Quote(symbol=symbol.upper(), bid=px, ask=px, time=now)

    def token_stats(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Rich DexScreener token stats for signal cards.

        Best-effort: returns None when the upstream is unreachable.
        Never raises — signal emission must not fail because stats
        are unavailable.
        """
        try:
            return self._market.get_token_stats(symbol)
        except Exception:  # noqa: BLE001 - stats are best-effort
            return None

    # -- positions / orders ---------------------------------------------------
    def _as_position(self, ticket: int, p: dict) -> Position:
        px = self._price_sol_quiet(p["symbol"]) or p["entry_price_sol"]
        profit = (px - p["entry_price_sol"]) * p["tokens"] \
            if p["direction"] == "BUY" else 0.0
        return Position(
            ticket=ticket,
            position_id=ticket,
            symbol=p["symbol"],
            direction=p["direction"],
            volume=p["volume_sol"],
            price_open=p["entry_price_sol"],
            price_current=px,
            sl=p["sl"],
            tp=p["tp"],
            profit=profit,
            swap=0.0,
            magic=MAGIC_NUMBER,
            comment=p["comment"],
            time_open=datetime.fromisoformat(p["time_open"]),
        )

    def positions(self) -> List[Position]:
        self._ensure()
        return [self._as_position(int(t), p)
                for t, p in sorted(self._positions.items(), key=lambda kv: int(kv[0]))]

    def orders(self) -> List[Order]:
        self._ensure()
        return []  # paper book has no pending orders; fills are immediate

    # -- trading (paper) -------------------------------------------------------
    def _next(self) -> int:
        t = self._next_ticket
        self._next_ticket += 1
        return t

    def submit_order(self, req: OrderRequest,
                     chain: Optional[str] = None) -> OrderResult:
        self._ensure()
        target = (chain or "solana").strip().lower()
        if target != "solana":
            # Multi-chain route: the ChainAdapter owns validation,
            # pricing, journaling and the live fail-closed gate. req.volume
            # is interpreted as USDC on chain routes (documented in
            # RUNTIME.md); the native path below stays SOL-denominated.
            if req.direction not in ("BUY", "SELL"):
                raise BrokerError(INVALID_ORDER,
                                  f"direction must be BUY|SELL, got {req.direction!r}")
            if req.volume <= 0:
                raise BrokerError(INVALID_ORDER,
                                  f"volume must be positive, got {req.volume}")
            if req.stop_loss is None:
                raise BrokerError(INVALID_ORDER, "stop_loss is required")
            adapter = self.chain_adapter(target)
            fill = adapter.submit_swap(
                token=req.symbol, side=req.direction,
                amount_usdc=float(req.volume),
                idempotency_key=req.idempotency_key)
            return OrderResult(
                ticket=fill["ticket"], symbol=req.symbol.upper(),
                direction=req.direction, volume=float(req.volume),
                price=float(fill["price_usd"]), retcode=0,
                message=f"paper fill ({target})",
                raw={"mode": "paper", "chain": target, "fill": fill})
        if self._mode == "live":
            # Unreachable in practice (connect() refuses live), but fail
            # closed here too — defense in depth.
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        if self._kill_switch_engaged():
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "memecoin kill-switch file is engaged — paper order refused",
                detail={"reason": "kill_switch_engaged"})
        if req.direction not in ("BUY", "SELL"):
            raise BrokerError(INVALID_ORDER,
                              f"direction must be BUY|SELL, got {req.direction!r}")
        if req.volume <= 0:
            raise BrokerError(INVALID_ORDER,
                              f"volume must be positive, got {req.volume}")
        if req.stop_loss is None:
            raise BrokerError(INVALID_ORDER, "stop_loss is required")
        symbol = req.symbol.upper()
        if not self._market.mint_for(symbol):
            raise BrokerError(INVALID_SYMBOL,
                              f"unknown memecoin symbol {req.symbol!r}")

        # P1-3 (2026-11-22): the idempotency check, fill, and put
        # are one atomic section under the state lock — a second
        # process must not pass the same check while this fill is
        # in flight. The price fetch and the (devnet) on-chain
        # transfer ride inside the lock too: splitting it around
        # the transfer would let a second process pass the same
        # idempotency check and transfer first.
        with self._state_lock():
            self._refresh_locked()
            return self._submit_solana(req, symbol)

    def _submit_solana(self, req: OrderRequest,
                       symbol: str) -> OrderResult:
        """Solana paper/devnet fill. Caller must hold _state_lock
        (and have refreshed state): this is the read-modify-write
        section — idempotency check, fill, journal, idempotency put
        — that the lock serializes across processes."""
        request = asdict(req)
        request["symbol"] = symbol
        request["mint"] = self._market.mint_for(symbol)
        for field in ("volume", "stop_loss", "take_profit"):
            if request[field] is not None:
                request[field] = float(request[field])
        request_fingerprint = hashlib.sha256(json.dumps(
            request, sort_keys=True, allow_nan=False).encode()).hexdigest()
        self._request_fingerprint = request_fingerprint
        # Idempotency: a repeated key replays the original fill — the
        # position is never opened twice.
        if req.idempotency_key:
            prior = self._idempotent_get(req.idempotency_key)
            if prior is not None:
                if prior.get("raw", {}).get("request_fingerprint") != request_fingerprint:
                    raise BrokerError(INVALID_ORDER, "idempotency request conflict or unbound legacy request")
                logger.info("idempotent replay for key %s (ticket %s)",
                            req.idempotency_key, prior.get("ticket"))
                return OrderResult(**prior)
            # Crash window: fill journaled but idempotency store never
            # written (process died between the two writes). Recover from
            # the journal instead of filling a second time.
            recovered = self._idempotent_recover_from_journal(
                req.idempotency_key)
            if recovered is not None:
                if recovered.raw.get("request_fingerprint") != request_fingerprint:
                    raise BrokerError(INVALID_ORDER, "idempotency request conflict or unbound legacy request")
                logger.info("idempotent recovery from journal for key %s "
                            "(ticket %s)", req.idempotency_key,
                            recovered.ticket)
                self._idempotent_put(req.idempotency_key, {
                    "ticket": recovered.ticket, "symbol": recovered.symbol,
                    "direction": recovered.direction,
                    "volume": recovered.volume, "price": recovered.price,
                    "retcode": recovered.retcode,
                    "message": recovered.message, "raw": recovered.raw,
                })
                return recovered

        # UNKNOWN guard: a prior attempt whose outcome is unresolved is
        # never blindly re-executed under the same key — reconcile first.
        if (self._mode == "demo" and req.idempotency_key
                and req.idempotency_key in self._unresolved):
            rec = self._unresolved[req.idempotency_key]
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "prior demo execution for this idempotency key is "
                "UNRESOLVED (confirm_timeout) — call "
                "reconcile_demo_execution() before retrying; blind "
                "re-execution could double-fill",
                detail={"reason": "unknown_unresolved",
                        "signature": rec.get("signature")})
        self._verify_position_mints(symbol)
        if self._shared_account is not None and req.direction == "BUY":
            # One FX observation prices native entry and its USD debit. A stale
            # SOL cache must not silently manufacture token units at another rate.
            self._entry_pricing_fx = self._observe_entry_fx()
            quote = self._market.get_quote(symbol)
            self._entry_token_evidence = self._validated_usd_quote(symbol,quote)
            price_sol = self._entry_token_evidence["price_usd"] / self._entry_pricing_fx["usd_per_sol"]
        elif self._shared_account is not None:
            price_sol = self._shared_price_sol(symbol)
        else:
            price_sol = self._price_sol(symbol)  # raises when blind

        if self._mode == "devnet":
            return self._devnet_fill(req, symbol, price_sol)

        if self._mode == "demo":
            return self._demo_fill(req, symbol)

        if req.direction == "BUY":
            result = self._paper_buy(req, symbol, price_sol)
        else:
            result = self._paper_sell(req, symbol, price_sol)

        if req.idempotency_key:
            self._idempotent_put(req.idempotency_key, {
                "ticket": result.ticket, "symbol": result.symbol,
                "direction": result.direction, "volume": result.volume,
                "price": result.price, "retcode": result.retcode,
                "message": result.message, "raw": result.raw,
            })
        return result

    def _devnet_fill(self, req: OrderRequest, symbol: str,
                     price_sol: float) -> OrderResult:
        """Devnet mode fill: real on-chain SOL transfer + paper ledger.

        There is no memecoin DEX liquidity on devnet, so the SOL leg of
        the trade is executed as a real devnet SOL transfer (to self —
        proving sign -> send -> confirm on-chain), while token quantities
        are tracked in the paper ledger exactly like paper mode. Every
        journaled devnet fill carries its real devnet transaction
        signature.

        Ordering: the on-chain transfer happens BEFORE the ledger
        mutation, so a failed transfer never leaves a fill without a
        signature. The light pre-checks below mirror _paper_buy/_paper_sell
        validation for the same reason (they re-run inside those
        methods; duplication is deliberate).
        """
        if self._devnet is None:
            raise BrokerError(BROKER_UNAVAILABLE,
                              "devnet executor not connected")
        sol_amount = float(req.volume)
        if req.direction == "BUY":
            if sol_amount > self._balance_sol:
                raise BrokerError(
                    RISK_LIMIT_EXCEEDED,
                    f"insufficient virtual balance: need {sol_amount:.6f} "
                    f"SOL, have {self._balance_sol:.6f} SOL",
                    detail={"need_sol": sol_amount,
                            "balance_sol": self._balance_sol})
        else:
            tokens_to_sell = sol_amount / price_sol
            available = sum(
                p["tokens"] for p in self._positions.values()
                if p["symbol"] == symbol and p["direction"] == "BUY")
            if tokens_to_sell > available + 1e-12:
                raise BrokerError(
                    INVALID_ORDER,
                    f"SELL {symbol} rejected: trying to sell "
                    f"{tokens_to_sell:.6f} tokens but only {available:.6f} "
                    f"held")
        # Real devnet execution: self-transfer proves the pipeline.
        sig = self._devnet.transfer_sol(self._devnet.wallet_pubkey,
                                        sol_amount)
        if req.direction == "BUY":
            result = self._paper_buy(req, symbol, price_sol)
        else:
            result = self._paper_sell(req, symbol, price_sol)
        self._journal_fill({
            "ticket": result.ticket, "kind": "DEVNET_EXEC",
            "symbol": symbol, "direction": req.direction,
            "volume_sol": sol_amount, "devnet_sig": sig,
            "idempotency_key": req.idempotency_key,
        })
        result.message = f"devnet fill (sig {sig[:12]}…)"
        result.raw["mode"] = "devnet"
        result.raw["devnet_sig"] = sig
        # _paper_buy/_paper_sell wrote the idempotency record with
        # mode="paper" before we annotated it — overwrite with the
        # devnet-annotated result so replays return the signature, not
        # the stale paper record.
        self._idempotent_put(req.idempotency_key, {
            "ticket": result.ticket, "symbol": result.symbol,
            "direction": result.direction, "volume": result.volume,
            "price": result.price, "retcode": result.retcode,
            "message": result.message, "raw": result.raw,
        })
        logger.info("devnet %s %s: %.6f SOL on-chain sig=%s (ticket %s)",
                    req.direction, symbol, sol_amount, sig, result.ticket)
        return result

    def _reserved_exposure_locked(self):
        """Exposure reserved by unresolved (UNKNOWN) executions: SOL for
        pending BUYs, per-mint tokens for pending SELLs. Caller owns
        the state lock. A reserved exposure is spendable by NO other
        key until reconcile resolves it — an ambiguous first attempt
        must never be silently double-spent."""
        sol = 0.0
        tokens: Dict[str, float] = {}
        for rec in self._unresolved.values():
            request = rec.get("request") or {}
            direction = rec.get("direction") or request.get("direction")
            fee_sol = int(rec.get("max_fee_lamports") or 0) / 1e9
            if direction == "BUY":
                stake = rec.get("stake_sol")
                if stake is None:
                    stake = float(request.get("volume") or 0.0)
                sol += float(stake) + fee_sol
            elif direction == "SELL":
                mint, qty = rec.get("mint"), rec.get("qty_tokens")
                if mint and qty:
                    tokens[mint] = tokens.get(mint, 0.0) + float(qty)
        return sol, tokens

    def _price_shared_entry(self, cost_sol: float,
                            max_fee_lamports: int):
        """Pin (stake_usd, max_fee_usd) from the sealed FX evidence.
        No cash check here: admission happens account-wide in
        _seal_shared_entry_hold."""
        fx = getattr(self, "_entry_pricing_fx", None)
        if not isinstance(fx, dict) or not fx.get("usd_per_sol"):
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "shared entry pricing FX unavailable — refusing to "
                "seal the reservation")
        px = float(fx["usd_per_sol"])
        return (round(cost_sol * px, 6),
                round(max_fee_lamports / 1e9 * px, 6))

    def _seal_shared_entry_hold(self, key: str, stake_usd: float,
                                max_fee_usd: float, mint: str = None) -> str:
        """Account-wide seal-time admission: reserve stake + max fee in
        the SHARED ACCOUNT LEDGER itself (not a per-book snapshot
        estimate), so a fill that settles before this process learns
        of it is always adoptable and no second book/lane/adapter can
        spend the reservation. Runs the account's full gate suite NOW
        (lane cap, gates, provider health): adoption later recovers
        recorded truth and is never refused."""
        amount = round(stake_usd + max_fee_usd, 6)
        hold_op_id = "native-hold:" + self._shared_book_id + ":" + key
        ok, why = self._shared_account.hold_stake("main", amount,
                                                  hold_op_id,
                                                  stake=float(stake_usd),
                                                  mint=mint)
        if not ok:
            if "unreadable" in why or "degraded" in why:
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "shared account unavailable — refusing to seal "
                    "the reservation: " + why)
            raise BrokerError(
                RISK_LIMIT_EXCEEDED,
                "shared capital refused entry hold $%.6f: %s"
                % (amount, why),
                detail={"reason": "shared_entry_reserved",
                        "stake_usd": stake_usd,
                        "max_fee_usd": max_fee_usd,
                        "refusal": why})
        return hold_op_id

    def _settle_shared_fee_locked(self, rec: Optional[dict],
                                  fee_lamports: int) -> None:
        """Book a settled FAILED tx's real fee against the shared
        ledger (reviewer gap: native mirror lost the fee, shared USD
        stayed whole). Crash-safe/idempotent via the account's own
        journaling; a failure here leaves the sealed record in place
        so reconcile retries the settlement."""
        if not rec or not rec.get("hold_op_id"):
            return
        hold_op_id = rec["hold_op_id"]
        key = hold_op_id.rsplit(":", 1)[-1]
        fee_usd = 0.0
        fx = rec.get("entry_pricing_fx")
        if fee_lamports > 0 and isinstance(fx, dict) and fx.get("usd_per_sol"):
            fee_usd = round(fee_lamports / 1e9
                            * float(fx["usd_per_sol"]), 6)
        if fee_usd > 0:
            ok, why = self._shared_account.settle_hold_fee(
                hold_op_id, fee_usd,
                "native-fee:" + self._shared_book_id + ":" + key)
        else:
            ok, why = self._shared_account.release_hold(
                hold_op_id, "tx_failed with no fee")
        if not ok:
            raise BrokerError(
                BROKER_UNAVAILABLE,
                "shared fee settlement failed (%s) — sealed record "
                "kept for reconcile retry" % why)

    def _demo_fill(self, req: OrderRequest, symbol: str) -> OrderResult:
        """Demo mode: execute through SimulationExecutor (live Jupiter
        quote, fee/slippage/latency/UNKNOWN semantics) instead of an
        optimistic quote-price fill. The executor's scratch book is
        synced from authoritative state before the call and read back
        after; adapter state and the shared ledger change only through
        the measured fill. Skips and UNKNOWN propagate unchanged."""
        mint = self._market.mint_for(symbol)
        from broker.memecoin.execution.sim_executor import (  # noqa: PLC0415 (lazy)
            LAMPORTS_PER_SOL, SOL_MINT as _SOL)
        executor = self._demo_bridge.executor
        executor.set_book_balance(_SOL, int(round(
            self._balance_sol * LAMPORTS_PER_SOL)))
        shared_reserve = None  # (stake_usd, max_fee_usd) when shared BUY
        if req.direction == "BUY":
            cost_sol = float(req.volume)
            in_mint, out_mint = _SOL, mint
            amount_units = int(round(cost_sol * LAMPORTS_PER_SOL))
            max_fee = int(0.05 * amount_units)
            if self._shared_account is None:
                reserved_sol, _ = self._reserved_exposure_locked()
                available = self._balance_sol - reserved_sol
                if (cost_sol + max_fee / LAMPORTS_PER_SOL
                        > available + 1e-15):
                    raise BrokerError(
                        RISK_LIMIT_EXCEEDED,
                        f"insufficient virtual balance: need "
                        f"{cost_sol + max_fee / LAMPORTS_PER_SOL:.6f} SOL "
                        f"(stake + max fee), have {available:.6f} SOL "
                        f"after {reserved_sol:.6f} SOL reserved by "
                        f"unresolved executions",
                        detail={"need_sol": cost_sol,
                                "max_fee_sol": max_fee / LAMPORTS_PER_SOL,
                                "balance_sol": self._balance_sol,
                                "reserved_sol": reserved_sol})
            else:
                shared_reserve = self._price_shared_entry(
                    cost_sol, max_fee)
        else:
            sell_sol = float(req.volume)
            price_est = (self._shared_price_sol(symbol)
                         if self._shared_account is not None
                         else self._price_sol(symbol))
            holdings = [(int(t), p) for t, p in self._positions.items()
                        if p["symbol"] == symbol and p["direction"] == "BUY"]
            if not holdings:
                raise BrokerError(
                    INVALID_ORDER,
                    f"SELL {symbol} rejected: no open long position to "
                    f"reduce (spot market — shorting is unsupported)")
            available = sum(p["tokens"] for _, p in holdings)
            qty = sell_sol / price_est
            _, reserved_tokens = self._reserved_exposure_locked()
            sellable = available - reserved_tokens.get(mint, 0.0)
            if qty > sellable + 1e-12:
                raise BrokerError(
                    INVALID_ORDER,
                    f"SELL {symbol} for {sell_sol:.6f} SOL needs "
                    f"{qty:.6f} tokens (quote estimate) but only "
                    f"{sellable:.6f} sellable ({available:.6f} held, "
                    f"{reserved_tokens.get(mint, 0.0):.6f} reserved by "
                    f"unresolved executions)")
            max_fee = int(0.05 * qty * price_est * LAMPORTS_PER_SOL)
            dec = self._demo_bridge.decimals(mint)
            executor.set_book_balance(
                mint, int(round(available * (10 ** dec))))
            in_mint, out_mint = mint, _SOL
            amount_units = int(round(qty * (10 ** dec)))
        # Seal the unresolved-execution record BEFORE the chain can
        # settle anything: a crash between a landed send and this
        # bookkeeping must still leave a reconcilable trail. The
        # signature is reserved up front and verified against the swap.
        key = req.idempotency_key or ("native:" + uuid.uuid4().hex)
        sig = executor.allocate_signature(in_mint, out_mint, amount_units)
        hold_op_id = None
        if shared_reserve is not None:
            # ADMISSION BEFORE EFFECT: the account-wide hold runs every
            # entry gate and reserves the cash BEFORE the sealed record
            # exists and long before the chain can settle. A crash
            # between hold and seal leaves an orphan hold the next
            # startup sweep releases by book prefix; a crash after the
            # seal leaves a record whose hold adoption consumes.
            hold_op_id = self._seal_shared_entry_hold(
                key, *shared_reserve, mint=mint)
            self._pending_hold_op_id = hold_op_id
        record = {
            "signature": sig,
            "request_fingerprint": getattr(
                self, "_request_fingerprint", None),
            "request": asdict(req),
            "mint": mint,
            "direction": req.direction,
            "max_fee_lamports": max_fee,
            "sealed_at": _utcnow().isoformat(),
        }
        if req.direction == "BUY":
            record["stake_sol"] = cost_sol
        else:
            record["qty_tokens"] = qty
        if shared_reserve is not None:
            record["stake_usd"], record["max_fee_usd"] = shared_reserve
            record["hold_op_id"] = hold_op_id
            record["entry_pricing_fx"] = getattr(
                self, "_entry_pricing_fx", None)
            record["entry_token_evidence"] = getattr(
                self, "_entry_token_evidence", None)
        self._unresolved[key] = record
        try:
            self._save_state()
        except Exception:
            # The chain effect has NOT happened yet, so this
            # reservation is provably unbacked by any execution:
            # release it inline instead of stranding spendable cash
            # until the next process sweep, then resync from the
            # durable state (atomic save: either the old state or the
            # new one, never a torn mix).
            if hold_op_id is not None:
                try:
                    self._shared_account.release_hold(
                        hold_op_id, "seal write failed")
                except Exception:
                    logger.exception(
                        "shared hold release after seal-write "
                        "failure failed; restart sweep owns it")
            self._load_state()
            raise
        try:
            if req.direction == "BUY":
                fill = self._demo_bridge.buy(mint, cost_sol)
            else:
                fill = self._demo_bridge.sell(mint, qty)
        except BrokerError as exc:
            det = getattr(exc, "detail", {}) or {}
            if det.get("reason") == "confirm_timeout":
                # Keep the record; adopt the actual signature (it
                # normally matches the reserved one) and persist.
                self._unresolved[key]["signature"] = det.get(
                    "signature", sig)
                self._save_state()
            elif det.get("reason") == "tx_failed" and det.get("signature"):
                # LANDED failure: the network fee is gone for real.
                # Book it against authoritative state before unsealing —
                # a scratch-book-only debit would be erased by the next
                # call's sync. Crash-atomic: the state write is tmp +
                # replace, so a crash here leaves either the sealed old
                # state (reconcile books the fee exactly once) or the
                # booked new state (no record, no double deduction).
                fee_lost = int(det.get("fee_lamports") or 0)
                try:
                    self._balance_sol = max(
                        0.0, self._balance_sol - fee_lost / 1e9)
                    self._settle_shared_fee_locked(
                        self._unresolved.get(key), fee_lost)
                    self._unresolved.pop(key, None)
                    self._save_state()
                except Exception:
                    self._load_state()
                    raise
            else:
                # Skips and clean failures settle nothing: unseal and
                # free the seal-time reservation.
                rec = self._unresolved.get(key)
                if rec is not None and rec.get("hold_op_id"):
                    self._shared_account.release_hold(
                        rec["hold_op_id"], "execution did not settle")
                self._unresolved.pop(key, None)
                self._save_state()
            raise
        self._unresolved.pop(key, None)
        # Read back the scratch book BEFORE journaling: the measured
        # SOL delta (stake + fee on BUY, net proceeds on SELL) becomes
        # the authoritative balance — no double-counted or capped-fee
        # drift.
        self._balance_sol = executor.book_balance(_SOL) / LAMPORTS_PER_SOL
        try:
            if req.direction == "BUY":
                return self._demo_buy(req, symbol, fill)
            return self._demo_sell(req, symbol, fill, qty)
        except Exception:
            # The bridge settled but the commit failed: in-memory
            # mutations (popped record, read-back balance) may diverge
            # from disk. Reload the durable state before propagating;
            # the sealed record survives for reconciliation.
            self._load_state()
            raise
        finally:
            self.__dict__.pop("_pending_hold_op_id", None)

    def reconcile_demo_execution(self, key: str) -> Dict[str, Any]:
        """Resolve an UNKNOWN (confirm_timeout) demo execution against
        the simulated chain's durable memory. Adopts a landed fill
        through the normal journaling path, books a landed failure's
        fee loss, or clears an unlanded attempt. Refuses to guess."""
        self._ensure()
        if self._mode != "demo":
            raise BrokerError(CONFIG_INVALID,
                              "reconcile_demo_execution is demo-mode only")
        with self._state_lock():
            self._refresh_locked()
            rec = self._unresolved.get(key)
            if rec is None:
                raise BrokerError(
                    INVALID_ORDER,
                    "no unresolved demo execution under this key",
                    detail={"reason": "no_unknown_execution"})
            truth = self._demo_bridge.executor.reconcile(rec["signature"])
            status = truth.get("status")
            if status in ("unlanded", "allocated"):
                # Nothing happened on-chain; no fee, no fill. An
                # "allocated" record is a pre-send crash: the signature
                # was reserved durably but provably never executed.
                # Safe to retry after this resolution.
                if rec.get("hold_op_id"):
                    self._shared_account.release_hold(
                        rec["hold_op_id"], "execution never landed")
                self._unresolved.pop(key, None)
                self._save_state()
                return {"status": "unlanded", "settled": False,
                        "fee_lamports": 0,
                        "chain_status": status,
                        "signature": rec["signature"]}
            if status == "failed":
                # Landed but reverted: the fee is gone for real — in
                # the native mirror AND authoritatively in the shared
                # USD ledger (settle first, then unseal, so a crash
                # never loses the fee).
                fee = int(truth.get("fee_lamports") or 0)
                self._balance_sol = max(
                    0.0, self._balance_sol - fee / 1_000_000_000)
                self._settle_shared_fee_locked(rec, fee)
                self._unresolved.pop(key, None)
                self._save_state()
                return {"status": "failed", "settled": False,
                        "fee_lamports": fee,
                        "signature": rec["signature"]}
            if status != "settled" or not isinstance(
                    truth.get("result"), dict):
                raise BrokerError(
                    BROKER_UNAVAILABLE,
                    "simulated chain record incomplete — refusing to "
                    "guess the outcome",
                    detail={"reason": "reconcile_incomplete",
                            "signature": rec["signature"]})
            result = truth["result"]
            req = OrderRequest(**rec["request"])
            dec = self._demo_bridge.decimals(rec["mint"])
            from broker.memecoin.execution.sim_executor import (  # noqa: PLC0415 (lazy)
                LAMPORTS_PER_SOL, SOL_MINT as _SOL)
            # Adoption replays the SEALED request's own context: its
            # request fingerprint and, for shared entries, the pinned
            # FX/token evidence — never whatever request happened to
            # run last on this adapter instance.
            _missing = object()
            prev_fp = getattr(self, "_request_fingerprint", _missing)
            prev_fx = getattr(self, "_entry_pricing_fx", _missing)
            prev_ev = getattr(self, "_entry_token_evidence", _missing)
            if rec.get("request_fingerprint") is not None:
                self._request_fingerprint = rec["request_fingerprint"]
            if rec.get("entry_pricing_fx") is not None:
                self._entry_pricing_fx = rec["entry_pricing_fx"]
            if rec.get("entry_token_evidence") is not None:
                self._entry_token_evidence = rec["entry_token_evidence"]
            # The pinned FX was fresh when the intent was SEALED; an
            # adoption committed later must validate it as of the seal,
            # not as of now — a settled fill must never be stranded by
            # the 60s quote-freshness window.
            prev_fxnow = getattr(self, "_fx_eval_now_override", _missing)
            if rec.get("sealed_at"):
                self._fx_eval_now_override = datetime.fromisoformat(
                    rec["sealed_at"]).timestamp()
            prev_hold = getattr(self, "_pending_hold_op_id", _missing)
            if rec.get("hold_op_id") is not None:
                self._pending_hold_op_id = rec["hold_op_id"]
            try:
                if result["input_mint"] == _SOL:
                    fill = {
                        "qty_tokens": result["out_amount"] / (10 ** dec),
                        "fee_lamports": result["priority_fee_lamports"],
                        "signature": result["signature"],
                        "latency_move_pct": result.get("latency_move_pct"),
                        "price_impact_pct": result.get("price_impact_pct"),
                        "price_impact_unit": result.get("price_impact_unit"),
                        "price_impact_raw": result.get("price_impact_raw"),
                    }
                    # Apply the settled swap's MEASURED SOL delta so other
                    # orders interleaved before the reconcile stay exact.
                    self._balance_sol += (
                        (result["sol_after"] - result["sol_before"])
                        / LAMPORTS_PER_SOL)
                    # Pop BEFORE the commit so the native intent's
                    # after-image carries fill and unseal as ONE
                    # transition: recovery can never restore one
                    # without the other (no restart double-apply).
                    self._unresolved.pop(key, None)
                    order = self._demo_buy(req, req.symbol.upper(), fill)
                else:
                    # The sold quantity comes from the recorded swap amount,
                    # not from the request's SOL-denominated volume.
                    qty = result["in_amount"] / (10 ** dec)
                    fill = {
                        "proceeds_sol": result["out_amount"] / LAMPORTS_PER_SOL,
                        "price_sol": (result["out_amount"] / LAMPORTS_PER_SOL
                                      ) / qty,
                        "fee_lamports": result["priority_fee_lamports"],
                        "signature": result["signature"],
                        "latency_move_pct": result.get("latency_move_pct"),
                        "price_impact_pct": result.get("price_impact_pct"),
                        "price_impact_unit": result.get("price_impact_unit"),
                        "price_impact_raw": result.get("price_impact_raw"),
                    }
                    # out_amount is the measured net SOL delta (fee included)
                    self._balance_sol += (result["out_amount"]
                                          / LAMPORTS_PER_SOL)
                    self._unresolved.pop(key, None)
                    order = self._demo_sell(req, req.symbol.upper(), fill, qty)
            except Exception:
                # Commit failed mid-adoption: reload the durable state
                # (the native intent machinery recovers or rolls back
                # to a consistent image) before propagating.
                self._load_state()
                raise
            finally:
                for name, prev in (("_request_fingerprint", prev_fp),
                                   ("_entry_pricing_fx", prev_fx),
                                   ("_entry_token_evidence", prev_ev),
                                   ("_fx_eval_now_override", prev_fxnow),
                                   ("_pending_hold_op_id", prev_hold)):
                    if prev is _missing:
                        self.__dict__.pop(name, None)
                    else:
                        setattr(self, name, prev)
            self._save_state()
            return {"status": "settled", "settled": True,
                    "ticket": order.ticket,
                    "signature": rec["signature"]}

    def _demo_buy(self, req: OrderRequest, symbol: str,
                  fill: dict) -> OrderResult:
        cost_sol = float(req.volume)
        fee_sol = fill["fee_lamports"] / 1_000_000_000
        tokens = fill["qty_tokens"]
        price_sol = cost_sol / tokens
        ticket = self._next()
        comment = req.order_comment()
        self._positions[str(ticket)] = {
            "symbol": symbol,
            "direction": "BUY",
            "volume_sol": cost_sol,
            "tokens": tokens,
            "entry_price_sol": price_sol,
            "fee_sol": fee_sol,
            "sl": req.stop_loss,
            "tp": req.take_profit,
            "comment": comment,
            "signal_id": req.signal_id,
            "time_open": _utcnow().isoformat(),
        }
        self._commit_native({
            "ticket": ticket, "kind": "BUY", "symbol": symbol,
            "volume_sol": cost_sol, "tokens": tokens,
            "price_sol": price_sol, "fee_sol": fee_sol,
            "comment": comment, "signal_id": req.signal_id,
            "idempotency_key": req.idempotency_key,
            "balance_sol_after": self._balance_sol,
            "exec_signature": fill["signature"],
            **({"features": req.features} if req.features else {}),
        })
        logger.info("demo BUY %s: %.6f SOL -> %.6f tokens @ %.9f SOL "
                    "(fee %.9f SOL, sig %s, ticket %s)",
                    symbol, cost_sol, tokens, price_sol, fee_sol,
                    fill["signature"][:12], ticket)
        return OrderResult(ticket=ticket, symbol=symbol, direction="BUY",
                           volume=cost_sol, price=price_sol, retcode=0,
                           message="demo fill",
                           raw={"mode": "demo", "tokens": tokens,
                                "fee_lamports": fill["fee_lamports"],
                                "signature": fill["signature"],
                                "latency_move_pct":
                                    fill.get("latency_move_pct"),
                                "price_impact_pct":
                                    fill.get("price_impact_pct"),
                                "price_impact_unit": fill.get("price_impact_unit"),
                                "price_impact_raw": fill.get("price_impact_raw"),
                                "request_fingerprint":
                                    self._request_fingerprint})

    def _demo_sell(self, req: OrderRequest, symbol: str, fill: dict,
                   qty_tokens: float) -> OrderResult:
        """Spot reduce-only demo sell: FIFO against measured proceeds."""
        price_sol = fill["price_sol"]
        proceeds_sol = fill["proceeds_sol"]
        holdings = [(int(t), p) for t, p in self._positions.items()
                    if p["symbol"] == symbol and p["direction"] == "BUY"]
        holdings.sort(key=lambda tp: tp[0])  # FIFO
        remaining = qty_tokens
        realized = 0.0
        closed_tickets = []
        allocations = []
        for ticket, p in holdings:
            if remaining <= 1e-12:
                break
            take = min(p["tokens"], remaining)
            basis = p["volume_sol"] * (take / p["tokens"])
            allocations.append({"ticket": ticket, "tokens": take,
                                "basis_sol": basis,
                                "proceeds_sol": take * price_sol,
                                "entry_price_sol": p["entry_price_sol"]})
            realized += (price_sol - p["entry_price_sol"]) * take
            p["tokens"] -= take
            if p["tokens"] <= 1e-12:
                closed_tickets.append(ticket)
                del self._positions[str(ticket)]
            else:
                frac = p["tokens"] / (p["tokens"] + take)
                p["volume_sol"] *= frac
            remaining -= take
        ticket = self._next()
        comment = req.order_comment()
        self._commit_native({
            "ticket": ticket, "kind": "SELL", "symbol": symbol,
            "volume_sol": proceeds_sol, "tokens": qty_tokens,
            "price_sol": price_sol, "fee_sol": fill["fee_lamports"] / 1e9,
            "realized_pnl_sol": realized,
            "closed_tickets": closed_tickets, "allocations": allocations,
            "comment": comment, "signal_id": req.signal_id,
            "idempotency_key": req.idempotency_key,
            "balance_sol_after": self._balance_sol,
            "exec_signature": fill["signature"],
        })
        logger.info("demo SELL %s: %.6f tokens @ %.9f SOL, realized %+.9f "
                    "SOL (sig %s, ticket %s)", symbol, qty_tokens,
                    price_sol, realized, fill["signature"][:12], ticket)
        return OrderResult(ticket=ticket, symbol=symbol, direction="SELL",
                           volume=proceeds_sol, price=price_sol, retcode=0,
                           message="demo fill",
                           raw={"mode": "demo", "tokens": qty_tokens,
                                "fee_lamports": fill["fee_lamports"],
                                "signature": fill["signature"],
                                "latency_move_pct":
                                    fill.get("latency_move_pct"),
                                "price_impact_pct":
                                    fill.get("price_impact_pct"),
                                "price_impact_unit": fill.get("price_impact_unit"),
                                "price_impact_raw": fill.get("price_impact_raw"),
                                "request_fingerprint":
                                    self._request_fingerprint,
                                "realized_pnl_sol": realized,
                                "closed_tickets": closed_tickets})

    def _paper_buy(self, req: OrderRequest, symbol: str,
                   price_sol: float) -> OrderResult:
        cost_sol = float(req.volume)
        if self._shared_account is None and cost_sol > self._balance_sol:
            raise BrokerError(
                RISK_LIMIT_EXCEEDED,
                f"insufficient virtual balance: need {cost_sol:.6f} SOL, "
                f"have {self._balance_sol:.6f} SOL",
                detail={"need_sol": cost_sol,
                        "balance_sol": self._balance_sol})
        tokens = cost_sol / price_sol
        ticket = self._next()
        comment = req.order_comment()
        self._positions[str(ticket)] = {
            "symbol": symbol,
            "direction": "BUY",
            "volume_sol": cost_sol,
            "tokens": tokens,
            "entry_price_sol": price_sol,
            "sl": req.stop_loss,
            "tp": req.take_profit,
            "comment": comment,
            "signal_id": req.signal_id,
            "time_open": _utcnow().isoformat(),
        }
        self._balance_sol -= cost_sol
        self._commit_native({
            "ticket": ticket, "kind": "BUY", "symbol": symbol,
            "volume_sol": cost_sol, "tokens": tokens,
            "price_sol": price_sol, "comment": comment,
            "signal_id": req.signal_id,
            "idempotency_key": req.idempotency_key,
            "balance_sol_after": self._balance_sol,
            # v2 rich features for the win classifier (when provided)
            **({"features": req.features} if req.features else {}),
        })
        logger.info("paper BUY %s: %.6f SOL -> %.6f tokens @ %.9f SOL (ticket %s)",
                    symbol, cost_sol, tokens, price_sol, ticket)
        return OrderResult(ticket=ticket, symbol=symbol, direction="BUY",
                           volume=cost_sol, price=price_sol, retcode=0,
                           message="paper fill",
                           raw={"mode": "paper", "tokens": tokens,
                                "request_fingerprint": self._request_fingerprint})

    def _paper_sell(self, req: OrderRequest, symbol: str,
                    price_sol: float) -> OrderResult:
        """Spot reduce-only: SELL closes/reduces open BUY exposure FIFO.
        Naked SELL is rejected — shorting is unsupported."""
        sell_sol = float(req.volume)
        holdings = [(int(t), p) for t, p in self._positions.items()
                    if p["symbol"] == symbol and p["direction"] == "BUY"]
        if not holdings:
            raise BrokerError(
                INVALID_ORDER,
                f"SELL {symbol} rejected: no open long position to reduce "
                f"(spot market — shorting is unsupported)")
        holdings.sort(key=lambda tp: tp[0])  # FIFO
        tokens_to_sell = sell_sol / price_sol
        available = sum(p["tokens"] for _, p in holdings)
        if tokens_to_sell > available + 1e-12:
            raise BrokerError(
                INVALID_ORDER,
                f"SELL {symbol} rejected: trying to sell "
                f"{tokens_to_sell:.6f} tokens but only {available:.6f} held")
        remaining = tokens_to_sell
        realized = 0.0
        closed_tickets = []
        allocations = []
        for ticket, p in holdings:
            if remaining <= 1e-12:
                break
            take = min(p["tokens"], remaining)
            basis = p["volume_sol"] * (take / p["tokens"])
            allocations.append({"ticket": ticket, "tokens": take,
                                "basis_sol": basis,
                                "proceeds_sol": take * price_sol,
                                "entry_price_sol": p["entry_price_sol"]})
            realized += (price_sol - p["entry_price_sol"]) * take
            p["tokens"] -= take
            # volume_sol tracks remaining cost basis proportionally
            if p["tokens"] <= 1e-12:
                closed_tickets.append(ticket)
                del self._positions[str(ticket)]
            else:
                frac = p["tokens"] / (p["tokens"] + take)
                p["volume_sol"] *= frac
            remaining -= take
        proceeds_sol = tokens_to_sell * price_sol
        self._balance_sol += proceeds_sol
        # Allocate the ticket BEFORE persisting state: if the process dies
        # between the state write and the journal write, the ticket is
        # never reused on restart.
        ticket = self._next()
        comment = req.order_comment()
        self._commit_native({
            "ticket": ticket, "kind": "SELL", "symbol": symbol,
            "volume_sol": proceeds_sol, "tokens": tokens_to_sell,
            "price_sol": price_sol, "realized_pnl_sol": realized,
            "closed_tickets": closed_tickets, "allocations": allocations,
            "comment": comment, "signal_id": req.signal_id,
            "idempotency_key": req.idempotency_key,
            "balance_sol_after": self._balance_sol,
        })
        logger.info("paper SELL %s: %.6f tokens @ %.9f SOL, realized %+.9f SOL "
                    "(ticket %s)", symbol, tokens_to_sell, price_sol,
                    realized, ticket)
        return OrderResult(ticket=ticket, symbol=symbol, direction="SELL",
                           volume=proceeds_sol, price=price_sol, retcode=0,
                           message="paper fill",
                           raw={"mode": "paper", "tokens": tokens_to_sell,
                                "request_fingerprint": self._request_fingerprint,
                                "realized_pnl_sol": realized,
                                "closed_tickets": closed_tickets})

    def modify_order(self, ticket: int, sl: float, tp: float) -> None:
        self._ensure()
        if self._mode == "live":
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        # P1-3: same lock + refresh as submit_order — the position
        # read and the save are one atomic section.
        with self._state_lock():
            self._refresh_locked()
            p = self._positions.get(str(ticket))
            if p is None:
                raise BrokerError(
                    INVALID_ORDER,
                    f"no open paper position with ticket {ticket}")
            p["sl"], p["tp"] = sl, tp
            self._save_state()
        logger.info("paper modify ticket %s: SL=%.9f TP=%.9f", ticket, sl, tp)

    def close_position(self, ticket: int) -> float:
        """Market close of a paper position at the live quote. Returns the
        close price in SOL.

        Risk-reducing closes are NOT blocked by the kill-switch file —
        only new orders are. An emergency brake must never trap open
        positions (core KillSwitch.close_all() relies on this).
        """
        self._ensure()
        if self._mode == "live":
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        # P1-3: the position read, balance mutation, save, and
        # journal are one atomic section under the state lock.
        with self._state_lock():
            self._refresh_locked()
            p = self._positions.get(str(ticket))
            if p is None:
                raise BrokerError(
                    INVALID_ORDER,
                    f"no open paper position with ticket {ticket}")
            # Price FIRST: if the quote fails the position stays untouched.
            self._verify_position_mints(p["symbol"])
            price_sol = (self._shared_price_sol(p["symbol"]) if self._shared_account is not None
                         else self._price_sol(p["symbol"]))
            del self._positions[str(ticket)]
            proceeds = p["tokens"] * price_sol
            realized = (price_sol - p["entry_price_sol"]) * p["tokens"]
            self._balance_sol += proceeds
            self._commit_native({
                "ticket": ticket, "kind": "CLOSE", "symbol": p["symbol"],
                "volume_sol": proceeds, "tokens": p["tokens"],
                "price_sol": price_sol, "realized_pnl_sol": realized,
                "balance_sol_after": self._balance_sol,
                "allocations": [{"ticket": ticket, "tokens": p["tokens"],
                                 "basis_sol": p["volume_sol"],
                                 "proceeds_sol": proceeds,
                                 "entry_price_sol": p["entry_price_sol"]}],
            })
        logger.info("paper CLOSE ticket %s (%s): %+.9f SOL realized",
                    ticket, p["symbol"], realized)
        return price_sol

    def close_position_at(self, ticket: int, price_sol: float,
                          reason: str = "") -> float:
        """Close a paper position at an explicit price. Returns the close
        price in SOL.

        Only for dead-pool accounting: the market quote is confirmed
        gone (no pool to sell into, feed healthy for other coins), so
        the position is worthless. Normal closes must use
        close_position() (live quote) — paper fills are never priced
        from stale or missing data.
        """
        self._ensure()
        if self._mode == "live":
            raise BrokerError(BROKER_UNAVAILABLE,
                              "live memecoin execution is not implemented")
        # P1-3: the position read, balance mutation, save, and
        # journal are one atomic section under the state lock.
        with self._state_lock():
            self._refresh_locked()
            p = self._positions.get(str(ticket))
            if p is None:
                raise BrokerError(
                    INVALID_ORDER,
                    f"no open paper position with ticket {ticket}")
            price_sol = float(price_sol)
            if self._shared_account is not None and price_sol != 0.0:
                raise BrokerError(INVALID_ORDER, "shared explicit close is restricted to confirmed dead-pool zero")
            del self._positions[str(ticket)]
            proceeds = p["tokens"] * price_sol
            realized = (price_sol - p["entry_price_sol"]) * p["tokens"]
            self._balance_sol += proceeds
            self._commit_native({
                "ticket": ticket, "kind": "CLOSE", "symbol": p["symbol"],
                "volume_sol": proceeds, "tokens": p["tokens"],
                "price_sol": price_sol, "realized_pnl_sol": realized,
                "balance_sol_after": self._balance_sol,
                "allocations": [{"ticket": ticket, "tokens": p["tokens"],
                                 "basis_sol": p["volume_sol"],
                                 "proceeds_sol": proceeds,
                                 "entry_price_sol": p["entry_price_sol"]}],
                "close_reason": reason or "explicit",
            })
        logger.info("paper CLOSE@%s ticket %s (%s): %+.9f SOL realized",
                    price_sol, ticket, p["symbol"], realized)
        return price_sol

    # -- history -----------------------------------------------------------
    def deal_history(self, from_: datetime, to: datetime,
                     position_id: Optional[int] = None) -> List[Deal]:
        self._ensure()
        deals: List[Deal] = []
        try:
            lines = self._fills_path().read_text().splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = e.get("ts", "")
            try:
                t = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            lo, hi = from_, to
            if lo.tzinfo is None:
                lo = lo.replace(tzinfo=timezone.utc)
            if hi.tzinfo is None:
                hi = hi.replace(tzinfo=timezone.utc)
            if not (lo <= t <= hi):
                continue
            if position_id is not None and e.get("ticket") != position_id:
                continue
            kind = e.get("kind", "")
            deals.append(Deal(
                ticket=int(e.get("ticket", 0)),
                position_id=int(e.get("ticket", 0)),
                symbol=e.get("symbol", ""),
                direction="BUY" if kind in ("BUY",) else "SELL",
                entry="IN" if kind == "BUY" else "OUT",
                volume=float(e.get("volume_sol", 0.0)),
                price=float(e.get("price_sol", 0.0)),
                profit=float(e.get("realized_pnl_sol", 0.0)),
                commission=0.0,
                swap=0.0,
                magic=MAGIC_NUMBER,
                comment=e.get("comment", ""),
                time=t,
            ))
        return deals

    # -- health / status -----------------------------------------------------
    def health(self) -> BrokerHealth:
        if self._mode == "live":
            return BrokerHealth(
                connected=False, adapter=self.adapter_name,
                server_time=_utcnow(),
                message="live memecoin execution is not implemented "
                        "(paper-only adapter).")
        return BrokerHealth(
            connected=self._connected,
            adapter=self.adapter_name,
            server_time=_utcnow(),
            message="" if self._connected else "Not connected (paper).",
        )

    def _market_data_ok(self) -> bool:
        """Best-effort market-data probe with a short timeout. Never raises."""
        try:
            return self._market.get_quote("SOL") is not None
        except Exception:
            return False

    def broker_status(self) -> dict:
        """Structured status. Never raises. Field docs: RUNTIME.md."""
        try:
            return {"broker": self._status()}
        except Exception as exc:  # defensive: status must never raise
            return {"broker": {
                "provider": "memecoin", "mode": self._mode,
                "configured": True, "reachable": False, "connected": False,
                "account_available": False, "market_data_available": False,
                "trading_available": False,
                "detail": {"probe_error": str(exc)[:200]},
            }}

    def _status(self) -> dict:
        status = {
            "provider": "memecoin",
            "mode": self._mode,
            "configured": True,
            "reachable": False,
            "connected": False,
            "account_available": False,
            "market_data_available": False,
            "trading_available": False,
            "detail": {"chains": self._enabled_chains},
        }
        if self._mode == "live":
            status["detail"]["reason"] = (
                "live memecoin execution is not implemented — this adapter "
                "is paper-only by design (see broker/memecoin/RUNTIME.md)")
            return status
        # Paper engine is local: it always "answers".
        status["reachable"] = True
        status["connected"] = self._connected
        status["account_available"] = True
        try:
            bal = self._balance_sol
            status["detail"]["balance_sol"] = round(bal, 9)
            status["detail"]["open_positions"] = len(self._positions)
        except Exception:
            pass
        if self._market_data_ok():
            status["market_data_available"] = True
        else:
            status["detail"]["market_data"] = (
                "public market-data endpoints unreachable — paper fills "
                "will be refused until quotes return")
        if self._kill_switch_engaged():
            status["detail"]["kill_switch"] = "engaged (killswitch file present)"
        else:
            status["trading_available"] = True
        if not self._connected:
            status["detail"]["reason"] = "paper engine ready, not connected (call connect())"
        return status
