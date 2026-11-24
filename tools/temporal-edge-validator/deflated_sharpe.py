#!/usr/bin/env python3
"""Deflated Sharpe Ratio (Lopez de Prado, 2014) — stdlib only.

Problem it solves: when you test K candidate strategies and pick the best,
its Sharpe ratio is inflated by selection bias ("backtest overfitting").
Bonferroni corrects p-values but assumes independent trials; DSR directly
estimates the Sharpe ratio you'd expect to see under the null (no edge)
given K trials, then asks: is the best observed Sharpe significantly above
that null expectation?

    SR0 = sqrt(V) * ((1-gamma) * PhiInv(1 - 1/K) + gamma * PhiInv(1 - 1/(K*e)))
    DSR = Phi( (SR_hat - SR0) * sqrt(T-1)
               / sqrt(1 - skew*SR_hat + (kurt-1)/4 * SR_hat^2) )

where V = variance of the K trial Sharpe ratios, gamma = Euler-Mascheroni,
T = number of observations of the best trial.

DSR is a probability: P(true Sharpe > null-expected Sharpe).
DSR > 0.95  ->  reject "no edge" at the 5% level.

Sharpe ratios here are per-trade (not annualized); DSR is frequency-free
as long as every trial uses the same frequency, which they do.
"""
import math

EULER_MASCHERONI = 0.5772156649015329


def phi(x):
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def phi_inv(p):
    """Inverse standard normal CDF (Acklam's approximation, ~1e-9 accuracy).

    Raises ValueError outside (0, 1).
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"phi_inv needs p in (0,1), got {p}")
    # Coefficients from Peter J. Acklam's approximation.
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
                 ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)


def _moments(xs):
    n = len(xs)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    std = math.sqrt(var) if var > 0 else 0.0
    if std == 0 or n < 4:
        return mean, std, 0.0, 3.0
    m3 = sum((x - mean) ** 3 for x in xs) / n
    m4 = sum((x - mean) ** 4 for x in xs) / n
    skew = m3 / std ** 3
    kurt = m4 / std ** 4  # raw kurtosis (3.0 = normal)
    return mean, std, skew, kurt


def sharpe_ratio(returns):
    """Per-period Sharpe of a return series (0.0 if degenerate)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean, std, _, _ = _moments(returns)
    return mean / std if std > 0 else 0.0


def expected_sharpe_null(trial_sharpes):
    """SR0: best Sharpe you'd expect under the null given K trials."""
    k = len(trial_sharpes)
    if k < 2:
        return 0.0
    mean = sum(trial_sharpes) / k
    var = sum((s - mean) ** 2 for s in trial_sharpes) / (k - 1)
    if var <= 0:
        return 0.0
    g = EULER_MASCHERONI
    return math.sqrt(var) * ((1 - g) * phi_inv(1 - 1.0 / k)
                             + g * phi_inv(1 - 1.0 / (k * math.e)))


def deflated_sharpe(trial_returns, best_idx=None):
    """DSR for the best of K trials.

    trial_returns: list of K return-series (one per candidate strategy).
    best_idx: index of the best trial (default: highest Sharpe).
    Returns dict with dsr, best_sr, sr0_null, and diagnostics.
    """
    k = len(trial_returns)
    if k == 0:
        raise ValueError("need at least one trial")
    sharpes = [sharpe_ratio(r) for r in trial_returns]
    if best_idx is None:
        best_idx = max(range(k), key=lambda i: sharpes[i])
    best_rets = trial_returns[best_idx]
    best_sr = sharpes[best_idx]
    t = len(best_rets)
    sr0 = expected_sharpe_null(sharpes)
    mean, std, skew, kurt = _moments(best_rets)
    denom = math.sqrt(max(1e-12, 1.0 - skew * best_sr
                          + (kurt - 1.0) / 4.0 * best_sr ** 2))
    if t < 2 or denom <= 0:
        dsr = 0.5
    else:
        dsr = phi((best_sr - sr0) * math.sqrt(t - 1) / denom)
    return {
        "dsr": dsr,
        "best_idx": best_idx,
        "best_sr": best_sr,
        "sr0_null": sr0,
        "n_trials": k,
        "n_obs": t,
        "skew": skew,
        "kurtosis": kurt,
        "rejects_null_5pct": dsr > 0.95,
    }
