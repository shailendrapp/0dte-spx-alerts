"""Strategy logic — regime filters and strike selection.

Mirrors the validated backtest in ../0dte_research/.
"""
from __future__ import annotations
import logging
import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .config import FilterConfig, StrategyConfig
from .pricing import round_to_grid

log = logging.getLogger(__name__)


# ---------- regime filter ----------------------------------------------------
@dataclass(frozen=True)
class FilterResult:
    skip: bool
    reason: str | None
    rv_annual: float            # realized vol (annualized fraction)
    overnight_gap: float        # signed fraction
    trend_5d: float             # signed fraction


def garman_klass_var(open_: float, high: float, low: float, close: float) -> float:
    """Garman-Klass single-day variance estimator."""
    return (
        0.5 * (math.log(high / low)) ** 2
        - (2 * math.log(2) - 1) * (math.log(close / open_)) ** 2
    )


def realized_vol_annual(daily_bars: Sequence[dict], lookback: int) -> float:
    """Compute annualized realized vol from the last `lookback` daily OHLC bars.

    `daily_bars` is the most recent block of bars (each: o/h/l/c).  Uses the
    Garman-Klass intraday-range estimator, then sqrt(mean_var * 252).
    """
    if len(daily_bars) < lookback:
        raise ValueError(f"Need {lookback} bars for realized vol; have {len(daily_bars)}")
    vars_ = [
        garman_klass_var(b["open"], b["high"], b["low"], b["close"])
        for b in daily_bars[-lookback:]
    ]
    return math.sqrt(max(0.0, np.mean(vars_)) * 252)


def evaluate_filters(
    spx_open: float,
    prior_close: float,
    daily_bars: Sequence[dict],   # >= 6 days incl. today
    cfg: FilterConfig,
) -> FilterResult:
    """Evaluate the three regime filters."""
    rv = realized_vol_annual(daily_bars, cfg.realized_vol_lookback)
    iv_proxy = rv * 1.20    # same VRP-multiplier as the backtest
    vix_equiv = iv_proxy * 100   # rendered as VIX-style %

    overnight_gap = (spx_open - prior_close) / prior_close

    # 5-day trend uses the most recent 6 closes (today vs 5 days ago)
    closes = [b["close"] for b in daily_bars[-6:]]
    trend_5d = (closes[-1] - closes[0]) / closes[0] if len(closes) >= 6 else 0.0

    if vix_equiv > cfg.vol_skip_threshold:
        return FilterResult(True, f"vol regime ({vix_equiv:.1f} > {cfg.vol_skip_threshold})",
                            rv, overnight_gap, trend_5d)
    if abs(overnight_gap) > cfg.overnight_gap_threshold:
        return FilterResult(True, f"overnight gap {overnight_gap*100:+.2f}% (|gap| > {cfg.overnight_gap_threshold*100:.2f}%)",
                            rv, overnight_gap, trend_5d)
    if abs(trend_5d) > cfg.trend_skip_threshold:
        return FilterResult(True, f"5-day trend {trend_5d*100:+.2f}% (|trend| > {cfg.trend_skip_threshold*100:.2f}%)",
                            rv, overnight_gap, trend_5d)

    return FilterResult(False, None, rv, overnight_gap, trend_5d)


# ---------- strike selection ------------------------------------------------
@dataclass(frozen=True)
class TradePlan:
    S0: float                # SPX spot at entry
    EM: float                # expected move (in SPX points)
    Kp: float; Lp: float
    Kc: float; Lc: float


def expected_move_from_straddle(
    atm_call_mid: float,
    atm_put_mid: float,
    em_multiplier: float,
) -> float:
    """tastytrade convention: EM ≈ 0.85 × ATM straddle price."""
    return em_multiplier * (atm_call_mid + atm_put_mid)


def build_trade_plan(
    spot: float,
    em: float,
    cfg: StrategyConfig,
) -> TradePlan:
    """Place short strikes at +/- (sigma_multiplier × EM); wings at +/- wing_width."""
    Kp = round_to_grid(spot - cfg.sigma_multiplier * em, cfg.strike_round)
    Kc = round_to_grid(spot + cfg.sigma_multiplier * em, cfg.strike_round)
    Lp = Kp - cfg.wing_width
    Lc = Kc + cfg.wing_width
    return TradePlan(S0=spot, EM=em, Kp=Kp, Lp=Lp, Kc=Kc, Lc=Lc)


# ---------- intraday MTM helpers --------------------------------------------
def spread_mark(
    sp_short_put: float, lp_long_put: float,
    sc_short_call: float, lc_long_call: float,
) -> float:
    """Current mark-to-market value of the iron condor (cost to close)."""
    put_spread_value = sp_short_put - lp_long_put
    call_spread_value = sc_short_call - lc_long_call
    return put_spread_value + call_spread_value
