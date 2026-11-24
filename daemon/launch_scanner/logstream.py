"""Sub-second launch detection via Solana program-log subscription.

Polling pump.fun / DexScreener sees a launch seconds after it lands.
``logsSubscribe`` on the pump.fun program streams new-mint transactions
the moment they are processed. A background daemon thread holds the
websocket; each scan cycle the scanner drains freshly detected mints into
the normal candidate pipeline (instant CA + confirmation judge).

Stdlib only — a minimal RFC 6455 client with HTTP CONNECT proxy support,
so no new dependency is needed on the VPS either. If the websocket is
unreachable (proxy blocks it, no route, handshake fails) the thread backs
off and the scanner keeps working on polling alone: detection degrades,
it never crashes.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

# Transaction-outcome guards (2026-11-17, Batch 3): meta.err
# gating (tx_guards.py). The logs-notification envelope already
# gates on value.err; the fetched tx is gated here too — the tx
# itself is the authority, and a reverted create must never
# yield a mint.
try:
    import tx_guards as _txg
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import tx_guards as _txg
    except Exception:
        raise

# Helius pacer (2026-11-17 pacing fix): every Helius URL in the
# failover pool takes a pacer token first. Public URLs are unpaced.
try:
    import helius_ratelimit as _helius_ratelimit
except Exception:  # pragma: no cover
    try:
        from daemon.launch_scanner import helius_ratelimit \
            as _helius_ratelimit
    except Exception:
        _helius_ratelimit = None  # type: ignore


def _helius_bucket(url: str) -> str:
    """Shared-guard bucket id for a Helius URL's key, "" otherwise.

    The key value is parsed in memory only and never logged or
    returned. Public URLs (no api-key param) get ""."""
    try:
        if "helius-rpc.com" not in (url or ""):
            return ""
        qs = urlsplit(url).query
        for part in qs.split("&"):
            if part.startswith("api-key="):
                key = part[len("api-key="):]
                if _helius_ratelimit is not None and key:
                    return _helius_ratelimit.bucket_id_for_key(key)
                return ""
        return ""
    except Exception:
        return ""

logger = logging.getLogger("forex_agent.daemon.launch_scanner.logstream")

# Canonical Solana program IDs / mints (Phase 2): the single source
# of truth is daemon/solana_ids.py — never redefine these literals.
try:
    from daemon import solana_ids as _solana_ids
except ImportError:  # bare-file import without the repo root on path
    import os as _os, sys as _sys
    _root = _os.path.dirname(_os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from daemon import solana_ids as _solana_ids

PUMP_PROGRAM = _solana_ids.PUMP_PROGRAM_ID
WSOL_MINT = _solana_ids.WSOL_MINT
WS_URL = "wss://api.mainnet-beta.solana.com"
HTTP_RPC_URL = "https://api.mainnet-beta.solana.com"
# Failover pool for transaction fetches: the first reachable endpoint
# wins. Health-scored per URL so a flaky endpoint stops being tried
# first. All are public, no API key needed.
HTTP_RPC_URLS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana.public-rpc.com",
    "https://rpc.ankr.com/solana",
)


def _rpc_label(url: str) -> str:
    """Safe log label: strips query strings (may carry API keys)."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return parts.hostname or 'unconfigured'
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    """Websocket transport failure."""


class WSHandshakeError(WSError):
    """HTTP rejection, without treating a status line as a transient outage."""
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__('websocket handshake rejected: HTTP %s' % status_code)


class WSClient:
    """Minimal RFC 6455 client: text frames, ping/pong, close, proxy CONNECT."""

    def __init__(self, url: str, timeout: float = 20.0):
        self.url = url
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self.read_deadline = None
        self._meter = None
        self._meter_id = None

    def _measure(self,rx=0,tx=0,stage=None,closed=False):
        if self._meter is None:return
        try:self._meter.observe(self._meter_id,rx=rx,tx=tx,stage=stage,closed=closed)
        except Exception:logger.warning('WS transport measurement unavailable')

    # ------------------------------------------------------------------
    # connect
    # ------------------------------------------------------------------
    @staticmethod
    def _proxy_for(secure: bool) -> Optional[Tuple[str, int]]:
        raw = (os.environ.get("https_proxy" if secure else "http_proxy")
               or os.environ.get("HTTPS_PROXY" if secure else "HTTP_PROXY"))
        if not raw:
            return None
        parts = urlsplit(raw)
        if not parts.hostname:
            return None
        return parts.hostname, parts.port or 8080

    def _send_all(self, sock: socket.socket, data: bytes) -> None:
        view = memoryview(data)
        while view:
            if self.read_deadline is not None:
                remaining=self.read_deadline-time.monotonic()
                if remaining<=0:
                    raise WSError('absolute websocket response deadline exceeded')
                sock.settimeout(min(self.timeout,remaining))
            sent = sock.send(view)
            self._measure(tx=max(0,sent))
            if sent<=0:
                raise WSError('websocket write made no progress')
            view = view[sent:]

    def _read_line(self, sock: socket.socket) -> str:
        buf = self._buf
        while b"\r\n" not in buf:
            chunk = sock.recv(4096)
            self._measure(rx=len(chunk))
            if not chunk:
                raise WSError("connection closed during handshake")
            buf += chunk
        line, self._buf = buf.split(b"\r\n", 1)
        return line.decode("latin-1")

    def connect(self) -> None:
        if not self.url:
            raise WSError('Alchemy WebSocket endpoint is not configured')
        parts = urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise WSError("unsupported scheme %s" % parts.scheme)
        secure = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        try:
            if (parts.hostname or '').endswith('.alchemy.com'):
                try:
                    from daemon.intelligence.acquisition.ws_telemetry import AsyncTransportMeter
                    from daemon.intelligence.acquisition.routine import router
                    self._meter=AsyncTransportMeter(router().home)
                    self._meter_id=self._meter.begin('alchemy')
                except Exception:
                    self._meter=None
                    logger.warning('WS transport measurement initialization unavailable')
            proxy = self._proxy_for(secure)
            if proxy:
                ph, pp = proxy
                sock = socket.create_connection((ph, pp), timeout=self.timeout)
                self._sock = sock
                self._measure(stage='transport_open')
                self._send_all(
                    sock,
                    ("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n"
                     % (host, port, host, port)).encode())
                status = self._read_line(sock)
                if not (status.startswith("HTTP/1.1 200")
                        or status.startswith("HTTP/1.0 200")):
                    raise WSError("proxy CONNECT rejected: %s" % status.strip())
                while self._read_line(sock).strip():
                    pass
            else:
                sock = socket.create_connection((host, port), timeout=self.timeout)
                self._sock = sock
                self._measure(stage='transport_open')

            if secure:
                sock = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=host)
                self._sock = sock

            key = base64.b64encode(os.urandom(16)).decode()
            self._send_all(sock, (
                "GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
                "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
                % (path, host, port, key)).encode())
            status = self._read_line(sock)
            status_parts = status.split()
            code = int(status_parts[1]) if len(status_parts)>1 and status_parts[1].isdigit() else None
            if code != 101:
                raise WSHandshakeError(code)
            accept = None
            while True:
                line = self._read_line(sock)
                if not line.strip():
                    break
                if line.lower().startswith("sec-websocket-accept:"):
                    accept = line.split(":", 1)[1].strip()
            expect = base64.b64encode(
                hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
            if accept != expect:
                raise WSError("bad Sec-WebSocket-Accept")
            sock.settimeout(self.timeout)
            self._sock = sock
            self._measure(stage='handshake_ok')
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------------
    # frames
    # ------------------------------------------------------------------
    def _recv_exactly(self, n: int) -> bytes:
        assert self._sock is not None
        while len(self._buf) < n:
            if self.read_deadline is not None:
                remaining=self.read_deadline-time.monotonic()
                if remaining<=0:
                    raise WSError('absolute websocket response deadline exceeded')
                self._sock.settimeout(min(self.timeout,remaining))
            chunk = self._sock.recv(65536)
            self._measure(rx=len(chunk))
            if not chunk:
                raise WSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_text(self, text: str) -> None:
        assert self._sock is not None
        data = text.encode("utf-8")
        n = len(data)
        header = bytearray([0x81])  # FIN + text opcode
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._send_all(self._sock, bytes(header) + masked)

    def _pong(self, payload: bytes) -> None:
        assert self._sock is not None
        n = len(payload)
        if n >= 126:
            return  # oversized ping: ignore
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._send_all(self._sock, bytes([0x8A, 0x80 | n]) + mask + masked)

    def recv_text(self) -> Optional[str]:
        """Next complete text message, or None on clean server close."""
        fragments: List[bytes] = []
        while True:
            if self.read_deadline is not None and time.monotonic()>=self.read_deadline:
                raise WSError('absolute websocket response deadline exceeded')
            head = self._recv_exactly(2)
            b1, b2 = head[0], head[1]
            fin = b1 & 0x80
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exactly(8))[0]
            mask = self._recv_exactly(4) if (b2 & 0x80) else None
            payload = self._recv_exactly(length) if length else b""
            if mask:
                payload = bytes(c ^ mask[i % 4]
                                for i, c in enumerate(payload))
            if opcode == 0x8:
                return None
            if opcode == 0x9:
                self._pong(payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x0):
                fragments.append(payload)
                if fin:
                    return b"".join(fragments).decode("utf-8", "replace")
                continue
            # binary / unknown opcodes: skip

    def close(self) -> None:
        try:
            if self._sock is not None:
                # masked empty close frame
                self._send_all(self._sock, b"\x88\x80" + os.urandom(4))
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            logger.warning('WS socket close failed; ownership and telemetry remain unresolved')
            if self._meter is not None and hasattr(self._meter,'stop'):self._meter.stop()
            return  # retain ownership for a caller cleanup retry
        self._sock = None
        self._measure(closed=True)
        self._meter=None


# ----------------------------------------------------------------------
# mint extraction
# ----------------------------------------------------------------------
def extract_mint(tx: Dict[str, Any]) -> Optional[str]:
    """Find the newly created token mint in a jsonParsed transaction."""
    try:
        msg = (tx.get("transaction") or {}).get("message") or {}
        meta = tx.get("meta") or {}

        def _walk(ixs: Any) -> Optional[str]:
            for ix in ixs or []:
                if not isinstance(ix, dict):
                    continue
                parsed = ix.get("parsed") or {}
                if parsed.get("type") in ("initializeMint", "initializeMint2"):
                    mint = (parsed.get("info") or {}).get("mint")
                    if mint:
                        return str(mint)
            return None

        all_ix: List[Any] = list(msg.get("instructions") or [])
        for inner in meta.get("innerInstructions") or []:
            if isinstance(inner, dict):
                all_ix.extend(inner.get("instructions") or [])
        hit = _walk(all_ix)
        if hit:
            return hit

        # Fallback: mint present in post balances but not pre balances.
        pre = {b.get("mint") for b in meta.get("preTokenBalances") or []
               if isinstance(b, dict) and b.get("mint")}
        for b in meta.get("postTokenBalances") or []:
            if not isinstance(b, dict):
                continue
            mint = b.get("mint")
            if mint and mint not in pre and mint != WSOL_MINT:
                return str(mint)
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------
# background subscriber
# ----------------------------------------------------------------------
class LogStream:
    """Background program-log subscriber.

    ``drain()`` returns unaccepted ``[{mint, signature, ts}]`` snapshots.
    Only explicit scanner commit acknowledgement consumes detections. Never raises out of its thread; when the websocket is down the
    scanner simply keeps polling.
    """

    def __init__(self, ws_url: Optional[str] = None, http_rpc: Optional[str] = None,
                 programs: Tuple[str, ...] = (PUMP_PROGRAM,),
                 http_rpcs: Optional[List[str]] = None,
                 intake_home=None,intake_filename='logstream_intake.json'):
        from daemon.intelligence.acquisition.routine import websocket_url
        self._ws_url = ws_url or websocket_url(purpose="normal_scanning")
        self._central_rpc = not bool(http_rpc or http_rpcs)
        if http_rpcs:
            urls = [u for u in http_rpcs if u]
        elif http_rpc:
            urls = [http_rpc]
        else:
            urls = list(HTTP_RPC_URLS)
        if not urls:
            urls = list(HTTP_RPC_URLS)
        self._http_rpc_url = urls[0]  # legacy single-url attribute
        self._http_rpc_urls = urls
        self._rpc_health = {u: 1.0 for u in urls}
        self._rpc_active = urls[0]
        self._programs = programs
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._healthy = False
        self._useful_evidence_at = None
        from pathlib import Path
        try:
            from .stream_intake import StreamIntake
        except ImportError:
            from stream_intake import StreamIntake
        self._intake=StreamIntake(intake_home or os.environ.get('FOREX_AGENT_HOME',Path.home()/'.forex-agent'),filename=intake_filename)
        self._selective_enricher = None
        if self._central_rpc:
            try:
                from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
                self._selective_enricher = SelectiveEnrichment(home=str(self._intake.path.parent.parent))
            except Exception:
                logger.warning('selective diagnostics unavailable at initialization')
        self._last_retry_ts=0.0
        # RPC politeness / failure accounting (worker thread only).
        self._last_rpc_ts = 0.0
        self._rpc_fail_count = 0
        self._last_rpc_warn = 0.0

    @property
    def rpc_urls(self) -> List[str]:
        """Endpoints ordered by current health (healthiest first)."""
        return sorted(self._http_rpc_urls,
                      key=lambda u: self._rpc_health.get(u, 0.0),
                      reverse=True)

    def _bump_rpc_health(self, url: str, ok: bool) -> None:
        h = self._rpc_health.get(url, 1.0)
        self._rpc_health[url] = (min(1.0, h + 0.3) if ok
                                 else max(0.0, h - 0.4))
        if ok and url != self._rpc_active:
            logger.warning("logstream rpc failover: now using %s",
                           _rpc_label(url))
            self._rpc_active = url

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="degen-logstream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    @property
    def healthy(self) -> bool:
        with self._lock:
            # Silence is not proof of a broken transport, nor proof of fresh
            # useful delivery. Expose unknown/degraded after this local bound.
            return self._healthy and self._useful_evidence_at is not None and time.monotonic()-self._useful_evidence_at<=60.

    def drain(self) -> List[Dict[str, Any]]:
        """Snapshot resolved unaccepted detections; never consumes them."""
        return [dict(rec,signature=sig) for sig,rec in self._intake.pending().items() if rec.get('mint')]

    def acknowledge(self,signatures):
        self._intake.accept(signatures)

    def _retry_pending(self):
        # Existing HTTP source/pacer and per-detail politeness remain intact.
        now=time.monotonic()
        if now-self._last_retry_ts<30:return
        self._last_retry_ts=now
        for sig,rec in [(s,r) for s,r in self._intake.pending().items() if not r.get('mint')][:10]:
            if self._stop.is_set():break
            if rec.get('mint'):continue
            mint=self._mint_for_signature(sig)
            if mint:self._intake.resolve(sig,mint,acquisition_evidence=getattr(self,'_last_tx_evidence',{}))
            else:self._intake.defer(sig)

    # -- internals ------------------------------------------------------
    def _set_healthy(self, value: bool) -> None:
        with self._lock:
            self._healthy = value
            if value:self._useful_evidence_at=time.monotonic()

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._retry_pending()
                self._serve()
            except Exception as exc:
                logger.warning("logstream connection failed: %s", exc)
                if isinstance(exc, WSHandshakeError) and exc.status_code in (401,403):
                    self._set_healthy(False)
                    logger.warning('logstream access denied; stopping WS attempts until explicit restart, no provider substitution')
                    return
            self._set_healthy(False)
            if self._stop.is_set():
                break
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, 120.0)

    def _serve(self) -> None:
        from daemon.intelligence.acquisition import routine
        routine.websocket_admit(self._ws_url)
        ws = WSClient(self._ws_url)
        try:
            try:
                ws.connect()
            except Exception as exc:
                routine.websocket_result(self._ws_url, exc)
                raise
            routine.websocket_result(self._ws_url)
            req_id = 0
            for prog in self._programs:
                req_id += 1
                ws.send_text(json.dumps({
                    "jsonrpc": "2.0", "id": req_id,
                    "method": "logsSubscribe",
                    "params": [{"mentions": [prog]},
                               {"commitment": "processed"}],
                }))
            pending=set(range(1,req_id+1))
            subscriptions=set()
            acknowledgements={}
            ack_deadline=time.monotonic()+20.  # local admission policy, not provider SLA
            # Socket open is not subscription acceptance. Until every requested
            # program has a valid ACK, expose polling-only/degraded health.
            self._set_healthy(False)
            while not self._stop.is_set():
                self._retry_pending()
                if pending and time.monotonic()>=ack_deadline:
                    raise WSError('log subscription acknowledgement deadline exceeded')
                ws.read_deadline=ack_deadline if pending else time.monotonic()+20.
                msg = ws.recv_text()
                if pending and time.monotonic()>=ack_deadline:
                    raise WSError('log subscription acknowledgement deadline exceeded')
                if msg is None:
                    raise WSError("server closed the stream")
                try:envelope=json.loads(msg)
                except (ValueError,TypeError):envelope=None
                if isinstance(envelope,dict):
                    error=envelope.get('error')
                    # A rate-limit response is account-wide even if its ID is
                    # unknown, repeated, or belongs to an accepted subscription.
                    if isinstance(error,dict) and error.get('code')==429:
                        routine.websocket_subscription_throttled(self._ws_url)
                        raise WSError('log subscription rejected: rate limited')
                    rid=envelope.get('id')
                    if type(rid) is int and rid in acknowledgements:
                        if error is not None or type(envelope.get('result')) is not int or envelope.get('result')!=acknowledgements[rid]:
                            raise WSError('log subscription contradictory acknowledgement')
                        continue  # Exact repeated acceptance is harmless.
                    if type(rid) is int and rid in pending:
                        sub=envelope.get('result')
                        if error is not None or type(sub) is not int or sub<0 or sub in subscriptions:
                            raise WSError('log subscription rejected or invalid acknowledgement')
                        pending.remove(rid);subscriptions.add(sub);acknowledgements[rid]=sub
                        if not pending:
                            self._set_healthy(True)
                            logger.warning('logstream acknowledged %d program subscription(s)',len(subscriptions))
                        continue
                    if error is not None:
                        raise WSError('uncorrelated log subscription error')
                    if envelope.get('method')=='logsNotification':
                        sub=(envelope.get('params') or {}).get('subscription')
                        if type(sub) is not int or sub not in subscriptions:
                            continue  # Never route another/old connection's stream.
                        result=(envelope.get('params') or {}).get('result')
                        value=result.get('value') if isinstance(result,dict) else None
                        if isinstance(value,dict) and isinstance(value.get('signature'),str) and value['signature'] and 'err' in value:
                            self._set_healthy(not pending)
                self._handle_message(msg)
        finally:
            self._set_healthy(False)
            try:
                ws.close()
            except Exception:
                pass

    def _handle_message(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except Exception:
            return
        if not isinstance(data,dict) or data.get("method") != "logsNotification":
            return  # subscribe acks etc.
        try:
            value = ((data.get("params") or {}).get("result", {})
                     .get("value", {})) or {}
        except Exception:
            return
        if not isinstance(value,dict) or "err" not in value or value["err"] is not None:
            return  # unknown/failed envelope is not successful receipt
        sig = value.get("signature")
        if not sig or not isinstance(sig, str):
            return
        if not self._intake.receive(sig):return
        mint=self._mint_for_signature(sig)
        if mint:self._intake.resolve(sig,mint,acquisition_evidence=getattr(self,'_last_tx_evidence',{}))
        else:self._intake.defer(sig)

    def _mint_for_signature(self, sig: str) -> Optional[str]:
        # Politeness: at most ~2 transaction fetches/sec so the public RPC
        # doesn't rate-limit us during launch bursts.
        now = time.monotonic()
        delta = now - self._last_rpc_ts
        if delta < 0.5:
            time.sleep(0.5 - delta)
        self._last_rpc_ts = time.monotonic()
        self._last_tx_evidence = {"recorded": False, "completeness": "unrecorded", "method": "getTransaction"}
        tx = self._http_rpc("getTransaction",
                            [sig, {"encoding": "jsonParsed",
                                    "maxSupportedTransactionVersion": 1}])
        if not isinstance(tx, dict):
            return None
        # meta.err gate (2026-11-17, Batch 3): the envelope
        # already refused failed txs, but the fetched tx is the
        # authority — a reverted create names no mint.
        if not _txg.tx_succeeded(tx):
            return None
        signatures=(tx.get('transaction') or {}).get('signatures')
        if not isinstance(signatures,list) or not signatures or signatures[0]!=sig:
            return None
        mint = extract_mint(tx)
        if mint is None and self._central_rpc:
            # Selective investigation only. Never synthesize a raw transaction,
            # infer a mint from provider prose, or promote uncertain evidence.
            try:
                from daemon.intelligence.acquisition.selective_enrichment import SelectiveEnrichment
                enricher = getattr(self, '_selective_enricher', None)
                if enricher is None:
                    return mint
                self._last_enrichment = enricher.submit(sig, tx)
            except Exception:
                logger.warning('selective diagnostic unavailable; candidate remains unresolved')
        return mint

    def _note_rpc_failure(self, method: str, exc: Any) -> None:
        # Loud enough to notice, quiet enough not to flood the log during
        # a flaky-network stretch: one line per minute with a count.
        now = time.monotonic()
        self._rpc_fail_count += 1
        if now - self._last_rpc_warn >= 60.0:
            logger.warning("logstream rpc %s failing (%d failures, "
                           "latest: %s)", method, self._rpc_fail_count, exc)
            self._rpc_fail_count = 0
            self._last_rpc_warn = now

    def _http_rpc(self, method: str, params: List[Any]) -> Any:
        """JSON-RPC with endpoint failover.

        Tries endpoints healthiest-first, two attempts each. A dead
        endpoint is skipped next time; the first success re-arms it.
        Returns the result, or None when every endpoint failed.

        Pacing (2026-11-17): a Helius URL is attempted only with a
        pacer token for its key's bucket (one token per logical
        call; both attempts share it). No token -> that URL is
        skipped and the chain fails over to the next URL (usually
        a public endpoint). A 429 reports to the pacer (progressive
        backoff + single probe); a success clears the cooldown.
        """
        if getattr(self, "_central_rpc", False):
            from daemon.intelligence.acquisition.routine import call
            result = call(method, params, hints={"purpose":"normal_scanning"})
            import copy
            self._last_tx_evidence = {k:copy.deepcopy(result.get(k)) for k in (
                'provider','kind','attempts','fallback','fallback_difference','cached',
                'decision_usable','degraded','completeness','note')}
            self._last_tx_evidence.update(recorded=True,method=method,observed_at=time.time())
            return result.get('result') if result.get('decision_usable',False) else None
        payload = json.dumps({"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params}).encode()
        transient = (http.client.IncompleteRead,
                     http.client.RemoteDisconnected,
                     http.client.BadStatusLine, socket.timeout,
                     urllib.error.URLError, ConnectionError)
        last_exc: Optional[BaseException] = None
        for url in self.rpc_urls:
            bucket = _helius_bucket(url)
            if bucket and _helius_ratelimit is not None:
                if not _helius_ratelimit.pacer_acquire(bucket):
                    continue  # pacer: no token — fail over, no HTTP
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json",
                         "User-Agent": "forex-agent/1.0",
                         "Connection": "close"})
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = json.loads(
                            resp.read().decode("utf-8", "replace"))
                    self._bump_rpc_health(url, True)
                    if bucket and _helius_ratelimit is not None:
                        _helius_ratelimit.pacer_report(bucket, True)
                    return data.get("result")
                except transient as exc:
                    last_exc = exc
                    time.sleep(0.5 * (attempt + 1))
                except Exception as exc:
                    # Non-transient HTTP error (4xx/5xx, e.g. a rate-limited
                    # key): mark this URL down and FAIL OVER to the next URL
                    # instead of abandoning the whole chain. Only when every
                    # URL is exhausted do we return None.
                    if bucket and _helius_ratelimit is not None \
                            and getattr(exc, "code", None) == 429:
                        _helius_ratelimit.pacer_report(bucket, False)
                    self._note_rpc_failure(method, exc)
                    break
            self._bump_rpc_health(url, False)
        self._note_rpc_failure(method, last_exc)
        return None
