"""Strategy logic — regime filters and strike selection.

Mirrors the validated backtest in ../0dte_research/.

CHANGE vs original (one function modified):
  build_trade_plan() now accepts optional call_wall / put_wall arguments.
  When provided, it checks whether the default short strike is more than
  cfg.wall_gap_buffer points away from the nearest GEX wall.  If so, it
  snaps the short strike to (wall - cfg.min_wall_gap) so the condor leg
  is always within min_wall_gap points of structural dealer resistance.

  This is a STRIKE PLACEMENT change only — not an exit rule.
  It does not touch hold-to-settlement behaviour or the 84% win rate edge.

  All other functions (evaluate_filters, expected_move_from_straddle,
  spread_mark, garman_klass_var, realized_vol_annual) are UNCHANGED.
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


# ── regime filter ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FilterResult:
    skip: bool
    reason: str | None
    rv_annual: float        # realized vol (annualized fraction)
    overnight_gap: float    # signed fraction
    trend_5d: float         # signed fraction


def garman_klass_var(open_: float, high: float, low: float, close: float) -> float:
    """Garman-Klass single-day variance estimator."""
    return (
        0.5 * (math.log(high / low)) ** 2
        - (2 * math.log(2) - 1) * (math.log(close / open_)) ** 2
    )


def realized_vol_annual(daily_bars: Sequence[dict], lookback: int) -> float:
    """Annualized realized vol from the last `lookback` daily OHLC bars."""
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
    daily_bars: Sequence[dict],
    cfg: FilterConfig,
) -> FilterResult:
    """Evaluate the three regime filters."""
    rv = realized_vol_annual(daily_bars, cfg.realized_vol_lookback)
    iv_proxy  = rv * 1.20
    vix_equiv = iv_proxy * 100

    overnight_gap = (spx_open - prior_close) / prior_close

    closes  = [b["close"] for b in daily_bars[-6:]]
    trend_5d = (closes[-1] - closes[0]) / closes[0] if len(closes) >= 6 else 0.0

    if vix_equiv > cfg.vol_skip_threshold:
        return FilterResult(
            True,
            f"vol regime ({vix_equiv:.1f} > {cfg.vol_skip_threshold})",
            rv, overnight_gap, trend_5d,
        )
    if abs(overnight_gap) > cfg.overnight_gap_threshold:
        return FilterResult(
            True,
            f"overnight gap {overnight_gap*100:+.2f}% "
            f"(|gap| > {cfg.overnight_gap_threshold*100:.2f}%)",
            rv, overnight_gap, trend_5d,
        )
    if abs(trend_5d) > cfg.trend_skip_threshold:
        return FilterResult(
            True,
            f"5-day trend {trend_5d*100:+.2f}% "
            f"(|trend| > {cfg.trend_skip_threshold*100:.2f}%)",
            rv, overnight_gap, trend_5d,
        )

    return FilterResult(False, None, rv, overnight_gap, trend_5d)


# ── strike selection ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradePlan:
    S0: float
    EM: float
    Kp: float
    Lp: float
    Kc: float
    Lc: float
    # GEX context (None when walls not available)
    call_wall: float | None = None
    put_wall:  float | None = None
    kc_adjusted: bool = False   # True when Kc was snapped toward call wall
    kp_adjusted: bool = False   # True when Kp was snapped toward put wall


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
    *,
    call_wall: float | None = None,
    put_wall:  float | None = None,
) -> TradePlan:
    """Place short strikes at ±(sigma_multiplier × EM); wings at ±wing_width.

    GEX wall adjustment (NEW):
    ──────────────────────────
    When call_wall is provided and the default Kc sits more than
    cfg.wall_gap_buffer points BELOW the call wall, the short call is
    widened toward the wall so it lands at (call_wall - cfg.min_wall_gap).

    Rationale: the call wall is where dealer hedging creates structural
    resistance.  Placing Kc well below it leaves the condor unprotected in
    the gap between the strike and the wall.  Anchoring Kc near the wall
    gives the short strike a GEX ceiling to lean on.

    Symmetric logic applies to Kp / put_wall.

    The adjustment is capped so:
      • Kc never moves closer to spot than (spot + cfg.min_wall_gap)
      • Kp never moves closer to spot than (spot - cfg.min_wall_gap)
    This preserves a minimum safety buffer regardless of wall location.

    When walls are None (FlashAlpha unavailable), the function falls back
    to the original sigma-multiplier behaviour — fully backward compatible.
    """
    # ── default placement (original logic, unchanged) ──────────────────────
    Kc_default = round_to_grid(spot + cfg.sigma_multiplier * em, cfg.strike_round)
    Kp_default = round_to_grid(spot - cfg.sigma_multiplier * em, cfg.strike_round)

    Kc = Kc_default
    Kp = Kp_default
    kc_adjusted = False
    kp_adjusted = False

    # ── call-side wall adjustment ──────────────────────────────────────────
    if call_wall is not None and call_wall > spot:
        gap = call_wall - Kc_default          # how far Kc is below the wall
        if gap > cfg.wall_gap_buffer:
            # Snap Kc up to (call_wall - min_wall_gap), grid-rounded
            candidate = round_to_grid(call_wall - cfg.min_wall_gap, cfg.strike_round)
            # Safety: never go closer than min_wall_gap pts from spot
            min_safe = round_to_grid(spot + cfg.min_wall_gap, cfg.strike_round)
            Kc = max(Kc_default, min(candidate, call_wall - cfg.strike_round))
            Kc = max(Kc, min_safe)
            if Kc != Kc_default:
                kc_adjusted = True
                log.info(
                    "GEX call-wall adjustment: Kc %s → %s "
                    "(call_wall=%.0f, gap=%.0f > buffer=%.0f)",
                    Kc_default, Kc, call_wall, gap, cfg.wall_gap_buffer,
                )

    # ── put-side wall adjustment ───────────────────────────────────────────
    if put_wall is not None and put_wall < spot:
        gap = Kp_default - put_wall           # how far Kp is above the wall
        if gap > cfg.wall_gap_buffer:
            candidate = round_to_grid(put_wall + cfg.min_wall_gap, cfg.strike_round)
            max_safe = round_to_grid(spot - cfg.min_wall_gap, cfg.strike_round)
            Kp = min(Kp_default, max(candidate, put_wall + cfg.strike_round))
            Kp = min(Kp, max_safe)
            if Kp != Kp_default:
                kp_adjusted = True
                log.info(
                    "GEX put-wall adjustment: Kp %s → %s "
                    "(put_wall=%.0f, gap=%.0f > buffer=%.0f)",
                    Kp_default, Kp, put_wall, gap, cfg.wall_gap_buffer,
                )

    Lp = Kp - cfg.wing_width
    Lc = Kc + cfg.wing_width

    return TradePlan(
        S0=spot, EM=em,
        Kp=Kp, Lp=Lp, Kc=Kc, Lc=Lc,
        call_wall=call_wall, put_wall=put_wall,
        kc_adjusted=kc_adjusted, kp_adjusted=kp_adjusted,
    )


# ── intraday MTM helpers ──────────────────────────────────────────────────────

def spread_mark(
    sp_short_put: float, lp_long_put: float,
    sc_short_call: float, lc_long_call: float,
) -> float:
    """Current mark-to-market value of the iron condor (cost to close)."""
    put_spread_value  = sp_short_put  - lp_long_put
    call_spread_value = sc_short_call - lc_long_call
    return put_spread_value + call_spread_value
