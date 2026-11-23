"""Owner-config surface (request section 16): validated, INERT owner settings.

The control room may read and set owner risk/config values through
``/config`` and ``/config_set``. Every value stored here is INERT:
nothing in this module activates trading, changes a threshold used by
any runner, or arms anything. Wiring these values into execution is a
later, separately approved step; until then they are recorded intent,
not behavior.

Setting a value is always a two-step confirmed action (the same
single-use, expiring confirm flow as every other mutation), and
``trading_mode`` accepts only ``historical`` or ``demo`` - never
mainnet: the dispatcher hard-refuses the live/mainnet words before any
handler runs, and this module refuses the value again here.

Storage: ``FOREX_AGENT_HOME/owner_config.json``, mode 0600, atomic
writes (tmp file + ``os.replace``). Unknown keys and malformed values
are refused - fail closed, never coerced.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Callable, Dict, Tuple

_TRADING_MODES = ("historical", "demo")


def _pos_float(v: Any) -> float:
    f = float(v)
    if not f > 0:
        raise ValueError("must be > 0")
    return f


def _pos_int(v: Any) -> int:
    if isinstance(v, bool):
        raise ValueError("must be an integer")
    i = int(v)
    if i < 1:
        raise ValueError("must be >= 1")
    return i


def _drawdown_pct(v: Any) -> float:
    f = float(v)
    if not 0 < f <= 100:
        raise ValueError("must be in (0, 100]")
    return f


def _slippage_bps(v: Any) -> float:
    f = float(v)
    if not 0 <= f < 10000:
        raise ValueError("must be in [0, 10000) bps")
    return f


def _trading_mode(v: Any) -> str:
    s = str(v).strip().lower()
    if s not in _TRADING_MODES:
        raise ValueError("must be one of: %s (never live/mainnet)"
                         % ", ".join(_TRADING_MODES))
    return s


# key -> (coerce/validate, human description). Anything not listed here
# is refused outright.
KEY_SPEC: Dict[str, Tuple[Callable[[Any], Any], str]] = {
    "trade_amount_usd": (_pos_float, "per-trade size in USD (inert)"),
    "demo_starting_balance_usd": (_pos_float, "simulated starting capital (inert)"),
    "max_simultaneous_trades": (_pos_int, "open-position ceiling (inert)"),
    "max_total_exposure_usd": (_pos_float, "total open-exposure ceiling in USD (inert)"),
    "max_daily_loss_usd": (_pos_float, "daily loss limit in USD (inert)"),
    "max_drawdown_pct": (_drawdown_pct, "max drawdown as percent (inert)"),
    "slippage_bps": (_slippage_bps, "assumed slippage in bps (inert)"),
    "trading_mode": (_trading_mode, "operating intent: historical or demo only (inert)"),
}


def _path() -> str:
    home = os.environ.get("FOREX_AGENT_HOME") or os.path.expanduser("~/.forex-agent")
    return os.path.join(home, "owner_config.json")


def load() -> Dict[str, Any]:
    """Read stored owner settings. Missing/corrupt file = no settings."""
    p = _path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    # Refuse to surface keys this module never wrote or values that no
    # longer validate: a hand-edited or stale file fails closed.
    out: Dict[str, Any] = {}
    for key, val in data.items():
        spec = KEY_SPEC.get(key)
        if spec is None:
            continue
        try:
            out[key] = spec[0](val)
        except (TypeError, ValueError):
            continue
    return out


def validate(key: str, value: Any) -> Any:
    """Coerce+validate one setting or raise ValueError. Fail closed."""
    spec = KEY_SPEC.get(key)
    if spec is None:
        raise ValueError("unknown owner-config key %r; allowed: %s"
                         % (key, ", ".join(sorted(KEY_SPEC))))
    return spec[0](value)


def set_value(key: str, value: Any) -> Any:
    """Validate and persist one setting atomically, mode 0600."""
    coerced = validate(key, value)
    data = load()
    data[key] = coerced
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=".owner_config-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return coerced


class ConfigUnset(RuntimeError):
    """Raised when an operation needs an owner-config value that is unset.

    Fail closed: an affected operation must never fall back to a
    placeholder or invented default. Callers that wire enforcement
    refuse the operation and surface this instead.
    """


def require(key: str) -> Any:
    """Return a stored setting or raise ConfigUnset (fail closed)."""
    cur = load()
    if key not in cur:
        raise ConfigUnset(
            "owner-config %r is unset - refusing to proceed rather than "
            "fall back to a placeholder; set it with /config_set" % key)
    return cur[key]


def render() -> str:
    """Human-readable /config output. Saved-but-inactive is explicit."""
    cur = load()
    lines = ["⚙️ Owner config - saved but INACTIVE: values are recorded "
             "intent only. They do not enforce trades, limits, or mode "
             "changes at runtime until enforcement wiring is added, and "
             "activation stays owner-approved."]
    for key in sorted(KEY_SPEC):
        marker = ("%r" % (cur[key],)) if key in cur else "(unset)"
        lines.append("  %s: %s - %s" % (key, marker, KEY_SPEC[key][1]))
    lines.append("Set with /config_set <key> <value> (two-step confirm).")
    return "\n".join(lines)
