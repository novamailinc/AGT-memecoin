"""Central routine acquisition routing used by actual scanner callers.

No service starts here. Revised owner allocation: Alchemy normal scanning,
Helius routine RPC/WSS and selective intelligence, Chainstack complementary
recent reads and independent verification, all within shared plan limits.
"""
import logging
import threading
import copy
from .provider_router import ProviderRouter, ROUTING_MODE_REVISED
from .request_scheduler import PRIORITY_CARD, PRIORITY_CRITICAL, PRIORITY_BACKGROUND

logger = logging.getLogger(__name__)
_local = threading.local()


def router():
    result = getattr(_local, 'router', None)
    if result is None:
        result = ProviderRouter(helius_enabled=True, mode=ROUTING_MODE_REVISED)
        _local.router = result
    return result


def call(method, params, timeout=12, critical=False, background=False, hints=None):
    """Typed source/attempt provenance retained. No hidden Helius substitution."""
    priority = PRIORITY_CRITICAL if critical else (PRIORITY_BACKGROUND if background else PRIORITY_CARD)
    result = router().call(method, params, timeout=timeout, priority=priority, hints=hints)
    result = dict(result)
    uncertain = bool(result.get('support_unverified')) or 'UNVERIFIED' in str(result.get('note') or '') or 'UNVERIFIED' in str(result.get('fallback_difference') or '')
    history_limited = method in ('getTransaction','getBlock','getSignaturesForAddress') and result.get('provider') in ('chainstack','public')
    result['decision_usable'] = result.get('kind') == 'ok' and not uncertain and not (critical and history_limited)
    result['degraded'] = result.get('kind') != 'ok' or bool(result.get('fallback')) or uncertain
    result['completeness'] = 'unverified-support' if uncertain else ('retention-limited' if history_limited else 'standard-rpc-response')
    evidence = {k:copy.deepcopy(result.get(k)) for k in ('provider','kind','attempts','fallback','fallback_difference','cached','decision_usable','degraded','completeness','note','credential_source','account')}
    evidence['method'] = method
    _local.evidence = getattr(_local,'evidence',[]) + [evidence]
    _local.evidence = _local.evidence[-100:]
    if result['degraded']:
        logger.warning('routine acquisition degraded method=%s provider=%s kind=%s completeness=%s usable=%s',method,result.get('provider'),result.get('kind'),result['completeness'],result['decision_usable'])
    return result


def read(method, params, timeout=12, critical=False, background=False, hints=None):
    try:
        result = call(method, params, timeout, critical, background, hints)
        if result.get('decision_usable', result['kind'] == 'ok'):
            return result['result']
        logger.warning('routine acquisition unavailable method=%s provider=%s kind=%s',
                       method,result.get('provider'),result.get('kind'))
    except Exception:
        logger.exception('routine acquisition failed method=%s',method)
    return None


def websocket_url(purpose='routine'):
    """Normal-scanning logs stay Alchemy; routine position pushes use Helius."""
    if purpose == 'normal_scanning':
        from .providers.base import alchemy_wss_url
        return alchemy_wss_url()
    from .providers.helius import numbered_keys
    keys=numbered_keys()
    if purpose == 'parsed_streams':
        # Helius Parsed Streams (assigned: relevant streams/events)
        # live on the Gatekeeper host only; see
        # providers/base.HELIUS_PARSED_STREAMS_WSS_TMPL.
        from .providers.base import HELIUS_PARSED_STREAMS_WSS_TMPL
        return HELIUS_PARSED_STREAMS_WSS_TMPL.format(key=keys[0]) if keys[0] else None
    return 'wss://mainnet.helius-rpc.com/?api-key='+keys[0] if keys[0] else None


def evidence_since(start=0):
    return copy.deepcopy(getattr(_local,'evidence',[])[start:])


def clear_evidence():
    _local.evidence = []


class WebsocketAdmissionError(RuntimeError):
    pass


def websocket_admit(url, critical=False):
    """Shared Alchemy reconnect admission. Explicit diagnostic URLs stay separate."""
    if not url:
        raise WebsocketAdmissionError('WebSocket endpoint unavailable: no configured endpoint')
    from urllib.parse import urlsplit
    host=urlsplit(url).hostname or ''
    if host=='mainnet.helius-rpc.com':
        r=router();account='helius_main'
        from . import helius_account
        if not helius_account.allowed(r.home):raise WebsocketAdmissionError('Helius shared account hold')
        if r.usage.account_disabled(account):raise WebsocketAdmissionError('Helius account disabled')
        allowed,reason=r.usage.nonessential_allowed(account,PRIORITY_CRITICAL if critical else PRIORITY_BACKGROUND,workload='routine_websocket',provider='helius')
        if not allowed:raise WebsocketAdmissionError('Helius WebSocket budget admission refused: '+str(reason))
        from daemon.launch_scanner import helius_ratelimit
        bucket=helius_ratelimit.bucket_id_for_key('account:helius_main')
        if not helius_ratelimit.pacer().acquire(bucket,priority=critical):raise WebsocketAdmissionError('Helius shared account cooldown')
        return True
    if host!='alchemy.com' and not host.endswith('.alchemy.com'):
        return False
    r = router()
    if r.usage.account_disabled('alchemy'):
        raise WebsocketAdmissionError('Alchemy WebSocket unavailable: account disabled')
    allowed, reason = r.usage.nonessential_allowed('alchemy', PRIORITY_CRITICAL if critical else PRIORITY_BACKGROUND, workload='exit_websocket' if critical else 'routine_websocket')
    if not allowed:
        raise WebsocketAdmissionError('Alchemy WebSocket budget admission refused: '+str(reason))
    from .request_scheduler import SharedQuota
    granted, waited, reason = SharedQuota('alchemy',home=r.home).acquire(PRIORITY_CRITICAL if critical else PRIORITY_CARD)
    if not granted:
        raise WebsocketAdmissionError('Alchemy WebSocket reconnect admission refused: '+str(reason))
    return True


def websocket_result(url, error=None):
    from urllib.parse import urlsplit
    if url and urlsplit(url).hostname=='mainnet.helius-rpc.com':
        from daemon.launch_scanner import helius_ratelimit
        bucket=helius_ratelimit.bucket_id_for_key('account:helius_main')
        r=router()
        throttled=error is not None and '429' in str(error)
        if throttled:
            from . import helius_account
            helius_account.throttled(r.home,getattr(error,'retry_after',None),getattr(error,'rate_limit_kind',None)=='monthly_quota')
            helius_ratelimit.pacer().report(bucket,False)
            r.record_wss_handshake_429('helius')
        elif error is None:
            r.record_wss_handshake_ok('helius')
        # One observed handshake outcome, not delivered-message billing.
        # Cost is unknown, never fabricated as zero or a standard RPC cost.
        r.usage.record('helius_main','helius','websocket/handshake',
                       'routine_websocket',PRIORITY_BACKGROUND,cost=None,
                       kind='rate_limited' if throttled else ('ok' if error is None else 'transport'))
        r.health.persist()
        return
    if not url or 'alchemy.com' not in (urlsplit(url).hostname or ''):
        return
    r = router()
    from .request_scheduler import SharedQuota
    if error is not None:
        if '429' in str(error):
            SharedQuota('alchemy',home=r.home).report_429()
            r.record_wss_handshake_429('alchemy')
        else:
            r.health.record_rpc({'provider':'alchemy','kind':'transport','elapsed':0.})
    else:
        r.record_wss_handshake_ok('alchemy')
    r.health.persist()


def websocket_subscription_throttled(url):
    """Subscription rejection is not a failed HTTP upgrade handshake."""
    from urllib.parse import urlsplit
    if url and urlsplit(url).hostname=='mainnet.helius-rpc.com':
        from daemon.launch_scanner import helius_ratelimit
        bucket=helius_ratelimit.bucket_id_for_key('account:helius_main')
        from . import helius_account
        helius_account.throttled(router().home)
        helius_ratelimit.pacer().report(bucket,False)
        return
    if not url or 'alchemy.com' not in (urlsplit(url).hostname or ''):
        return
    from .request_scheduler import SharedQuota
    r=router()
    SharedQuota('alchemy',home=r.home).report_429()
    r.health.record_wss_subscription_429('alchemy')
    r.health.persist()
