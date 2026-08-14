"""
execution/greeks_engine.py

Broker-independent Delta calculation. This exists because relying on a
broker's own Greeks feed is fragile — Fyers' availability is unconfirmed,
and even brokers that do provide Greeks may compute them differently or
gate them behind a paid data plan. Computing Delta ourselves from spot,
strike, time-to-expiry, and the option's own live LTP means the strategy
never depends on a broker feature that might not exist.

Two-step process:
1. implied_volatility(): back out IV from the option's live LTP using
   Newton-Raphson on the Black-Scholes price formula.
2. black_scholes_delta(): compute Delta from that IV.

No scipy dependency — the normal CDF/PDF are implemented directly with
math.erf, so this has zero new third-party requirements.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_RISK_FREE_RATE = 0.065  # approx. Indian short-term risk-free rate; override via config if needed
MIN_TIME_TO_EXPIRY_YEARS = 1e-6  # floor to avoid division by zero on expiry day itself
IV_SOLVER_MAX_ITERATIONS = 50
IV_SOLVER_TOLERANCE = 1e-4
IV_INITIAL_GUESS = 0.30  # 30% vol as a reasonable starting point for Indian index options


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(spot: float, strike: float, t_years: float, iv: float, r: float) -> tuple[float, float]:
    t_years = max(t_years, MIN_TIME_TO_EXPIRY_YEARS)
    iv = max(iv, 1e-4)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * math.sqrt(t_years))
    d2 = d1 - iv * math.sqrt(t_years)
    return d1, d2


def black_scholes_price(
    spot: float, strike: float, t_years: float, iv: float, r: float, option_type: str
) -> float:
    d1, d2 = _d1_d2(spot, strike, t_years, iv, r)
    t_years = max(t_years, MIN_TIME_TO_EXPIRY_YEARS)
    if option_type == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t_years) * _norm_cdf(d2)
    return strike * math.exp(-r * t_years) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def black_scholes_delta(
    spot: float, strike: float, t_years: float, iv: float, r: float, option_type: str
) -> float:
    d1, _ = _d1_d2(spot, strike, t_years, iv, r)
    if option_type == "CE":
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0  # PE delta is negative; callers typically use abs()


@dataclass
class ImpliedVolResult:
    iv: float
    converged: bool
    iterations: int


def implied_volatility(
    option_price: float,
    spot: float,
    strike: float,
    t_years: float,
    option_type: str,
    r: float = DEFAULT_RISK_FREE_RATE,
) -> ImpliedVolResult:
    """Newton-Raphson solve for IV. Falls back to the last estimate
    (converged=False) rather than raising, so a noisy quote degrades
    gracefully instead of crashing the signal pipeline."""
    if option_price <= 0 or t_years <= 0:
        return ImpliedVolResult(iv=IV_INITIAL_GUESS, converged=False, iterations=0)

    iv = IV_INITIAL_GUESS
    for i in range(IV_SOLVER_MAX_ITERATIONS):
        price = black_scholes_price(spot, strike, t_years, iv, r, option_type)
        diff = price - option_price
        if abs(diff) < IV_SOLVER_TOLERANCE:
            return ImpliedVolResult(iv=iv, converged=True, iterations=i)

        d1, _ = _d1_d2(spot, strike, t_years, iv, r)
        vega = spot * _norm_pdf(d1) * math.sqrt(max(t_years, MIN_TIME_TO_EXPIRY_YEARS))
        if vega < 1e-8:
            break  # avoid division by ~0; return best estimate so far
        iv = iv - diff / vega
        iv = max(0.01, min(iv, 5.0))  # keep within a sane 1%-500% band

    logger.warning(
        "IV solver did not converge after %d iterations (option_price=%.2f, spot=%.2f, "
        "strike=%.2f, t_years=%.4f) — using last estimate %.4f.",
        IV_SOLVER_MAX_ITERATIONS, option_price, spot, strike, t_years, iv,
    )
    return ImpliedVolResult(iv=iv, converged=False, iterations=IV_SOLVER_MAX_ITERATIONS)


def years_to_expiry(expiry_epoch_seconds: Optional[float], now: Optional[datetime] = None) -> float:
    """Converts an expiry timestamp (epoch seconds, as Fyers' optionchain
    `expiry` field or Upstox-style ISO-derived epoch) into years-to-expiry.
    Returns a small positive floor rather than 0 on expiry day to keep the
    Black-Scholes math well-defined."""
    if not expiry_epoch_seconds:
        return MIN_TIME_TO_EXPIRY_YEARS
    now = now or datetime.now(timezone.utc)
    expiry_dt = datetime.fromtimestamp(float(expiry_epoch_seconds), tz=timezone.utc)
    seconds_remaining = (expiry_dt - now).total_seconds()
    years = seconds_remaining / (365.25 * 24 * 3600)
    return max(years, MIN_TIME_TO_EXPIRY_YEARS)


def compute_delta_from_price(
    option_price: float,
    spot: float,
    strike: float,
    expiry_epoch_seconds: Optional[float],
    option_type: str,
    r: float = DEFAULT_RISK_FREE_RATE,
) -> float:
    """The one function callers actually use: option's own live LTP in,
    Delta (absolute value, 0-1) out. This is what option_selector.py calls
    when a broker doesn't supply Delta — and, by design, it's just as
    valid to call it INSTEAD of trusting a broker's Delta, if you'd rather
    have one consistent calculation across every broker."""
    t_years = years_to_expiry(expiry_epoch_seconds)
    iv_result = implied_volatility(option_price, spot, strike, t_years, option_type, r)
    delta = black_scholes_delta(spot, strike, t_years, iv_result.iv, r, option_type)
    return abs(delta)
