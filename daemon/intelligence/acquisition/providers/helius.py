"""Helius provider adapter (Phases 3-4).

Live leg TODAY. READ-ONLY use of the existing transport in
``daemon/launch_scanner/reserves.py`` — this adapter adds the
(result, kind, elapsed) contract around it and NOTHING else: no
re-implementation of Helius pacing, no second pacer, no key handling
here. If the reserves module cannot be imported, the adapter reports
unconfigured rather than failing.

NULL VS ERROR (2026-11-22, resolved): the transport now exposes
``reserves.rpc_typed`` alongside ``reserves.rpc``, so a JSON-null
answer (e.g. getTransaction for a not-yet-indexed signature) is
reported as kind "ok" with a null payload instead of being
conflated with an outage. Finer error classes (429 vs timeout)
remain collapsed into kind "error" — the live path's own logs
carry the per-endpoint detail.

Usage attribution: the reserves transport rotates across the
operator's Helius key sources internally (main disk key, discovery
disk key, vault surrogate when available); this layer cannot see
which key served a given call. Usage is therefore recorded against
the ``helius_main`` account with a rotation caveat in the usage
journal — per-key attribution is NOT visible from here, and no
second allowance is assumed (the two disk keys may belong to one
account; see the capability audit §1).
"""

from __future__ import annotations

import logging
import time

from .base import BaseProviderAdapter, _result

logger = logging.getLogger(
    "forex_agent.daemon.intelligence.acquisition.providers.helius")


class HeliusAdapter(BaseProviderAdapter):
    """Live leg, delegating to the existing reserves transport."""

    name = "helius"
    account = "helius_main"

    def configured(self):
        if getattr(self,'revised',False):
            return any(numbered_keys())
        try:
            from daemon.launch_scanner import reserves
            return any('helius-rpc.com' in url for url,_ in reserves.rpc_url_pairs())
        except Exception:
            return False

    def wss_url(self):
        # The live transport owns the Helius keys; this layer cannot
        # build a WSS URL without handling key material. armed_watch
        # owns the Helius WSS URL. Return None (do not fabricate).
        return None

    def call(self, method, params, timeout=12):
        start = time.monotonic()
        if getattr(self,'revised',False):
            return revised_call(self,method,params,timeout)
        try:
            from daemon.launch_scanner import reserves
        except Exception:
            return _result(None, "unconfigured", time.monotonic() - start,
                           self.name, note="reserves module unavailable")
        try:
            out, rkind = reserves.helius_rpc_typed(method, params, timeout=timeout)
        except Exception:
            logger.exception("helius adapter: reserves.rpc_typed "
                             "raised (%s)", method)
            return _result(None, "error", time.monotonic() - start,
                           self.name)
        elapsed = time.monotonic() - start
        if rkind == "ok":
            return _result(out, "ok", elapsed, self.name)
        if rkind == "null":
            # The provider answered JSON null (e.g. getTransaction
            # for a not-yet-indexed signature): an ANSWER, not an
            # outage. Report kind "ok" with a null payload, the
            # same contract the Alchemy leg has always had; the
            # router never caches null payloads and callers
            # re-poll (2026-11-22 null-vs-error fix).
            return _result(None, "ok", elapsed, self.name,
                           note="reserves.rpc answered null")
        return _result(None, "error", elapsed, self.name,
                       note="reserves.rpc failed")


def numbered_keys():
    """Fixed numbered sources; legacy main is slot1 compatibility only."""
    import os
    return [os.environ.get('HELIUS_API_KEY_%d'%i) or
            (os.environ.get('HELIUS_API_KEY') if i==1 else None) for i in (1,2,3)]


def revised_call(adapter,method,params,timeout):
    """Stable shard, one conservative account bucket, no key failover on denial."""
    import hashlib,json
    from .base import http_json_rpc,_parse_rpc_body
    from daemon.launch_scanner import helius_ratelimit
    keys=numbered_keys()
    shard=int.from_bytes(hashlib.sha256(json.dumps([method,params],sort_keys=True).encode()).digest()[:8],'big')%3
    key=keys[shard]
    def answer(value,kind,elapsed=0.,**kw):
        out=_result(value,kind,elapsed,'helius',**kw);out['credential_source']='key%d'%(shard+1);out['account']='helius_main';return out
    if not key:return answer(None,'unconfigured',note='fixed numbered source unavailable')
    from .. import helius_account
    if not helius_account.allowed(adapter.home):return answer(None,'rate_limited',note='shared account hold')
    bucket=helius_ratelimit.bucket_id_for_key('account:helius_main')
    pacer=helius_ratelimit.pacer()
    if not pacer.acquire(bucket):return answer(None,'rate_limited',note='shared account cooldown')
    start=time.monotonic()
    status,body,err,retry=http_json_rpc('https://mainnet.helius-rpc.com/?api-key={key}'.format(key=key),method,params,timeout=timeout,_opener=adapter._transport)
    value,kind,wait,rl_kind=_parse_rpc_body('helius',status,body,err,retry_after=retry)
    if kind=='rate_limited':helius_account.throttled(adapter.home,wait,rl_kind=='monthly_quota')
    if kind in ('ok','rate_limited'):pacer.report(bucket,kind=='ok')
    out=_result(value,kind,time.monotonic()-start,'helius',http_status=status,wait_hint_s=wait,rate_limit_kind=rl_kind)
    out['credential_source']='key%d'%(shard+1)
    return out
