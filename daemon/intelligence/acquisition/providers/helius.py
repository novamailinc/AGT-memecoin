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

    def call(self, method, params, timeout=12, max_transport_attempts=None):
        start = time.monotonic()
        if getattr(self,'revised',False):
            return revised_call(self,method,params,timeout,
                                max_transport_attempts=max_transport_attempts)
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


def _record_key_stat(home, label, field, cost=0):
    """Fail-soft per-key counters (labels + numbers only, NEVER key
    material) in helius_key_stats.json: requests, method-weighted
    credits, per-outcome counts. Local counters are a LOWER BOUND on
    true project usage (the vendor-side credit cycle is authoritative;
    sync via the Admin API once the project id is known)."""
    try:
        import fcntl, json, os
        root = home or os.environ.get('FOREX_AGENT_HOME') or \
            os.path.expanduser('~/.forex-agent')
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, 'helius_key_stats.json')
        lock = open(path + '.lock', 'a+')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                with open(path) as fh:
                    data = json.load(fh)
            except Exception:
                data = {}
            st = data.setdefault(label, {})
            for k, default in (('requests', 0), ('credits', 0.0),
                               ('ok', 0), ('error', 0),
                               ('rate_limited', 0), ('transport_error', 0),
                               ('unconfigured', 0)):
                if k not in st:
                    st[k] = default
            st['requests'] += 1
            st['credits'] += float(cost or 0)
            st[field] = st.get(field, 0) + 1
            tmp = '%s.tmp.%d' % (path, os.getpid())
            with open(tmp, 'w') as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            except Exception:
                pass
            lock.close()
    except Exception:
        pass


def revised_call(adapter,method,params,timeout,max_transport_attempts=None):
    """Cooperative multi-key dispatch over ONE conservative account bucket.

    The three numbered keys COOPERATE via stable home-key workload
    distribution: a method/params shard gives every call a home key, so
    distinct calls spread across all three keys ACROSS CALLS. Within one
    call there is NO retry of an uncertain request: any actual transport
    outcome (answer or unknown post-I/O failure such as a timeout, where
    the request may have reached the vendor) returns IMMEDIATELY, never
    hops to another key, and the caller's credit reservation is retained
    (the budget_reservations contract: no refund/retry of an uncertain
    request). Failover to the next key happens ONLY for proven pre-I/O
    skips: an unconfigured key source, where no request was made and no
    credit could have been billed. Every real attempt acquires from the
    SAME aggregate account bucket (rate pacer) first, and the caller
    holds a credit reservation before the attempt, so failover can never
    bypass project throttling, an exhausted shared budget, or the
    max_attempts transport-attempt bound. Vendor 429s are answered, not
    evaded: a rate_limited result is returned immediately with no retry
    on another key. The bucket's rate is the operator's allocation
    policy and is unchanged by this dispatch layer.
    """
    import hashlib,json
    from .base import http_json_rpc,_parse_rpc_body
    from daemon.launch_scanner import helius_ratelimit
    keys=numbered_keys()
    shard=int.from_bytes(hashlib.sha256(json.dumps([method,params],sort_keys=True).encode()).digest()[:8],'big')%3
    def answer(value,kind,elapsed=0.,**kw):
        out=_result(value,kind,elapsed,'helius',**kw);out['account']='helius_main';return out
    from .. import helius_account
    if not helius_account.allowed(adapter.home):
        return answer(None,'rate_limited',note='shared account hold')
    bucket=helius_ratelimit.bucket_id_for_key('account:helius_main')
    pacer=helius_ratelimit.pacer()
    transport_attempts=0
    for hop in range(3):
        if max_transport_attempts is not None and transport_attempts>=max_transport_attempts:
            out=answer(None,'rate_limited',note='max_attempts transport-attempt budget exhausted')
            out['transport_attempts']=transport_attempts
            return out
        idx=(shard+hop)%3
        key=keys[idx]
        label='key%d'%(idx+1)
        if not key:
            # Proven pre-I/O skip: no request was made, no credit could
            # have been billed; trying the next key is safe failover.
            _record_key_stat(adapter.home,label,'unconfigured',0)
            continue
        if not pacer.acquire(bucket):
            out=answer(None,'rate_limited',note='shared account cooldown')
            out['transport_attempts']=transport_attempts
            return out
        transport_attempts+=1
        start=time.monotonic()
        status,body,err,retry=http_json_rpc('https://mainnet.helius-rpc.com/?api-key={key}'.format(key=key),method,params,timeout=timeout,_opener=adapter._transport)
        value,kind,wait,rl_kind=_parse_rpc_body('helius',status,body,err,retry_after=retry)
        if err is not None and status is None:
            # UNKNOWN post-I/O transport outcome: the request may have
            # reached the vendor and been billed. Return immediately;
            # NEVER hop to another key (that would retry an uncertain
            # request and multiply real spend against one reservation).
            _record_key_stat(adapter.home,label,'transport_error',
                             adapter.cost(method) or 0)
            out=_result(value,kind,time.monotonic()-start,'helius',http_status=status,wait_hint_s=wait,rate_limit_kind=rl_kind)
            out['credential_source']=label
            out['account']='helius_main'
            out['transport_attempts']=transport_attempts
            return out
        if kind=='rate_limited':helius_account.throttled(adapter.home,wait,rl_kind=='monthly_quota')
        if kind in ('ok','rate_limited'):pacer.report(bucket,kind=='ok')
        _record_key_stat(adapter.home,label,kind,
                         adapter.cost(method) or 0)
        out=_result(value,kind,time.monotonic()-start,'helius',http_status=status,wait_hint_s=wait,rate_limit_kind=rl_kind)
        out['credential_source']=label
        out['account']='helius_main'
        out['transport_attempts']=transport_attempts
        return out
    out=answer(None,'unconfigured',note='no fixed numbered source available')
    out['transport_attempts']=transport_attempts
    return out
