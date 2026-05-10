from src.config import FilterConfig, StrategyConfig
from src.strategy import (
    build_trade_plan, evaluate_filters, expected_move_from_straddle,
    garman_klass_var, realized_vol_annual, spread_mark,
)


def _bars(seq):
    """Build daily-bar dicts from a list of (o,h,l,c) tuples."""
    return [{"open": o, "high": h, "low": l, "close": c, "date": f"d{i}"}
            for i, (o, h, l, c) in enumerate(seq)]


def test_garman_klass_positive_for_normal_day():
    v = garman_klass_var(100, 102, 99, 101)
    assert v > 0


def test_realized_vol_basic():
    bars = _bars([(100, 101, 99, 100.5)] * 5)
    rv = realized_vol_annual(bars, lookback=5)
    assert 0.0 < rv < 1.0


def test_filter_passes_quiet_day():
    bars = _bars([(100, 100.5, 99.5, 100.1)] * 6 + [(100.0, 100.2, 99.8, 100.05)])
    fc = FilterConfig(vol_skip_threshold=25.0, overnight_gap_threshold=0.0075,
                      trend_skip_threshold=0.04, realized_vol_lookback=5)
    fr = evaluate_filters(spx_open=100.0, prior_close=100.05, daily_bars=bars, cfg=fc)
    assert fr.skip is False


def test_filter_skips_on_gap():
    bars = _bars([(100, 100.5, 99.5, 100)] * 7)
    fc = FilterConfig(vol_skip_threshold=25.0, overnight_gap_threshold=0.005,
                      trend_skip_threshold=0.04, realized_vol_lookback=5)
    fr = evaluate_filters(spx_open=102.0, prior_close=100.0, daily_bars=bars, cfg=fc)
    assert fr.skip is True
    assert "gap" in (fr.reason or "")


def test_filter_skips_on_high_vol():
    # huge intraday ranges -> high RV
    bars = _bars([(100, 110, 90, 100)] * 7)
    fc = FilterConfig(vol_skip_threshold=25.0, overnight_gap_threshold=0.10,
                      trend_skip_threshold=0.10, realized_vol_lookback=5)
    fr = evaluate_filters(spx_open=100.0, prior_close=100.0, daily_bars=bars, cfg=fc)
    assert fr.skip is True
    assert "vol" in (fr.reason or "").lower()


def test_expected_move_from_straddle():
    em = expected_move_from_straddle(atm_call_mid=12, atm_put_mid=10, em_multiplier=0.85)
    assert abs(em - 18.7) < 1e-9


def test_build_trade_plan_strikes():
    sc = StrategyConfig(sigma_multiplier=1.0, wing_width=25, em_multiplier=0.85, strike_round=5)
    plan = build_trade_plan(spot=5500.0, em=58.0, cfg=sc)
    assert plan.Kp == 5440  # 5500-58 → round to nearest 5
    assert plan.Kc == 5560  # 5500+58 → round to nearest 5
    assert plan.Lp == plan.Kp - 25
    assert plan.Lc == plan.Kc + 25


def test_spread_mark_basic():
    # Sell put 0.4, buy put 0.1, sell call 0.5, buy call 0.2 -> mark = 0.3 + 0.3 = 0.6
    m = spread_mark(0.4, 0.1, 0.5, 0.2)
    assert abs(m - 0.6) < 1e-9
