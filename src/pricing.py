"""Black-Scholes + iron-condor pricing helpers."""
from __future__ import annotations
import math

from scipy.stats import norm


def bs_price(S: float, K: float, T: float, r: float, sigma: float, kind: str) -> float:
    """European Black-Scholes price.  kind in {'c', 'p'}."""
    if T <= 0 or sigma <= 0:
        return max(0.0, S - K) if kind == "c" else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "c":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def iron_condor_credit(
    short_put: float, long_put: float,
    short_call: float, long_call: float,
) -> float:
    """Net credit of the iron condor in option-points.  All inputs are mids."""
    return (short_put - long_put) + (short_call - long_call)


def iron_condor_settlement(
    Sc: float, Kp: float, Lp: float, Kc: float, Lc: float,
) -> float:
    """Cash-settled intrinsic value of the four legs at expiration.
    Positive => the short side owes this many points."""
    put_spread = max(0.0, Kp - Sc) - max(0.0, Lp - Sc)
    call_spread = max(0.0, Sc - Kc) - max(0.0, Sc - Lc)
    return put_spread + call_spread


def round_to_grid(x: float, grid: int = 5) -> float:
    return float(grid) * round(x / grid)
