"""09:32 ET morning alert.

  1. Check time window + market open (Tradier clock).
  2. Pull SPX spot + 7 days of SPY history.
  3. Pull SPX 0DTE option chain.
  4. Compute Expected Move from ATM straddle (tastytrade convention).
  5. Run regime filters.
  6. If TRADE: select strikes, compute credit from real chain mids.
  7. Send Telegram alert; write & commit state/today.json.
"""
from __future__ import annotations
import logging
import sys
from datetime import timedelta

from .config import load_config, setup_logging
from .pricing import iron_condor_credit
from .state import empty_state, git_commit_state, write_state
from .strategy import (
    build_trade_plan, evaluate_filters, expected_move_from_straddle,
)
from .telegram_bot import send as tg_send
from .timecheck import is_us_market_weekday, now_et, today_et, within_window
from .tradier import (
    TradierClient, find_atm_strike, midpoint, split_chain,
)

log = logging.getLogger(__name__)
UNDERLYING = "SPX"
HISTORY_SYMBOL = "SPY"   # Tradier daily history; SPY×10 ≈ SPX, faster than SPX history


def _format_alert(state: dict, fr, plan, credit_pts: float) -> str:
    if state["decision"] == "SKIP":
        return (
            f"🚫 *0DTE SPX — SKIP*  ({state['date']})\n\n"
            f"Reason: {state['skip_reason']}\n"
            f"σ realized (5d): {fr.rv_annual*100:.1f}%\n"
            f"Overnight gap:   {fr.overnight_gap*100:+.2f}%\n"
            f"5-day trend:     {fr.trend_5d*100:+.2f}%\n\n"
            f"_No trade today.  Re-evaluate tomorrow._"
        )

    t = state["trade"]
    return (
        f"🔔 *0DTE SPX Iron Condor* — {state['date']}\n\n"
        f"SPX (open):     {plan.S0:,.1f}\n"
        f"Expected Move:  ±{plan.EM:.0f} pts (from ATM straddle)\n"
        f"σ realized 5d:  {fr.rv_annual*100:.1f}%\n"
        f"Overnight gap:  {fr.overnight_gap*100:+.2f}%\n"
        f"5-day trend:    {fr.trend_5d*100:+.2f}%\n"
        f"Filters:        ✅ all pass\n\n"
        f"*PLACE:*\n"
        f"  SELL  PUT   {plan.Kp:,.0f}\n"
        f"  BUY   PUT   {plan.Lp:,.0f}\n"
        f"  SELL  CALL  {plan.Kc:,.0f}\n"
        f"  BUY   CALL  {plan.Lc:,.0f}\n\n"
        f"Estimated credit:  *${credit_pts:.2f}* (~${credit_pts*100:.0f}/contract)\n"
        f"Max loss / contract: ${t['max_loss_per_contract']:.0f}\n"
        f"Win zone (close):    {plan.Kp:,.0f} – {plan.Kc:,.0f}\n\n"
        f"_Hold to 16:00 ET cash settlement._"
    )


def main() -> int:
    setup_logging()
    cfg = load_config()
    today = today_et()

    # 1. time + weekday gates
    if not is_us_market_weekday():
        log.info("Not a US market weekday; exiting.")
        return 0
    if not within_window(cfg.times.morning_alert, cfg.time_tolerance_min):
        log.info("Outside %s ±%d min window; exiting.",
                 cfg.times.morning_alert, cfg.time_tolerance_min)
        return 0

    cli = TradierClient(cfg.tradier_token, cfg.tradier_base_url)

    # 1b. confirm market is actually open / not a holiday
    clock = cli.get_clock()
    state_str = (clock.get("state") or "").lower()
    if state_str == "closed":
        log.info("Tradier clock reports market CLOSED (holiday?). Exiting.")
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id,
                f"ℹ️ 0DTE — market closed today ({today}). No alert.",
                dry_run=cfg.dry_run)
        return 0

    # 2. pull SPX spot + 7 days of SPY OHLC
    spx_q = cli.get_quote(UNDERLYING)
    spot = float(spx_q.get("last") or spx_q.get("close") or 0.0)
    if spot <= 0:
        raise RuntimeError(f"Unusable SPX quote: {spx_q}")
    log.info("SPX spot: %.2f", spot)

    bars = cli.get_history_daily(HISTORY_SYMBOL, today - timedelta(days=14), today)
    # Convert to a uniform shape; multiply SPY by 10 for SPX scale
    daily_bars = [
        {"date": b["date"], "open": float(b["open"])*10, "high": float(b["high"])*10,
         "low": float(b["low"])*10, "close": float(b["close"])*10}
        for b in bars
    ]
    if len(daily_bars) < cfg.filters.realized_vol_lookback + 1:
        raise RuntimeError(f"Not enough history bars: {len(daily_bars)}")
    prior_close = daily_bars[-2]["close"]      # yesterday's close
    log.info("Prior close (SPY×10): %.2f", prior_close)

    # 3. find today's 0DTE expiration (must be today or earliest)
    expirations = cli.get_option_expirations(UNDERLYING)
    today_iso = today.isoformat()
    if today_iso not in expirations:
        log.info("No 0DTE chain for %s (no SPXW expiry today). Exiting.", today_iso)
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id,
                f"ℹ️ 0DTE — no SPXW expiry on {today_iso}. No alert.",
                dry_run=cfg.dry_run)
        return 0
    expiration = today_iso

    chain = cli.get_option_chain(UNDERLYING, expiration)
    calls, puts = split_chain(chain)
    log.info("Chain has %d strikes (%d calls, %d puts)", len(calls), len(calls), len(puts))

    # 4. expected move from ATM straddle
    atm_k = find_atm_strike(chain, spot)
    atm_call = calls.get(atm_k); atm_put = puts.get(atm_k)
    if not (atm_call and atm_put):
        raise RuntimeError(f"ATM strike {atm_k} missing call or put")
    em = expected_move_from_straddle(midpoint(atm_call), midpoint(atm_put),
                                     cfg.strategy.em_multiplier)
    log.info("ATM straddle = %.2f → EM = %.1f pts", em / cfg.strategy.em_multiplier, em)

    # 5. regime filters
    fr = evaluate_filters(spot, prior_close, daily_bars, cfg.filters)
    log.info("Filters: skip=%s reason=%s rv=%.3f gap=%.4f trend=%.4f",
             fr.skip, fr.reason, fr.rv_annual, fr.overnight_gap, fr.trend_5d)

    state = empty_state(today)

    if fr.skip:
        state["decision"] = "SKIP"
        state["skip_reason"] = fr.reason
        msg = _format_alert(state, fr, None, 0.0)
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id, msg, dry_run=cfg.dry_run)
        write_state(state)
        git_commit_state(f"SKIP {today_iso}: {fr.reason}", dry_run=cfg.dry_run)
        return 0

    # 6. build trade plan and look up the four leg mids from the real chain
    plan = build_trade_plan(spot, em, cfg.strategy)

    def _leg(strike: float, kind: str) -> dict:
        d = (calls if kind == "c" else puts).get(strike)
        if not d:
            raise RuntimeError(f"Strike {strike} {kind} not in chain")
        return d

    sp = midpoint(_leg(plan.Kp, "p"))
    lp = midpoint(_leg(plan.Lp, "p"))
    sc = midpoint(_leg(plan.Kc, "c"))
    lc = midpoint(_leg(plan.Lc, "c"))
    if None in (sp, lp, sc, lc):
        raise RuntimeError(f"Leg mid missing: sp={sp} lp={lp} sc={sc} lc={lc}")

    credit = iron_condor_credit(sp, lp, sc, lc)
    max_loss = (cfg.strategy.wing_width - credit) * 100

    state["decision"] = "TRADE"
    state["trade"] = {
        "S0": plan.S0, "EM": plan.EM,
        "Kp": plan.Kp, "Lp": plan.Lp, "Kc": plan.Kc, "Lc": plan.Lc,
        "credit": round(credit, 2),
        "max_loss_per_contract": round(max_loss, 0),
        "underlying": UNDERLYING,
        "expiration": expiration,
        "leg_symbols": {
            "Kp": _leg(plan.Kp, "p")["symbol"],
            "Lp": _leg(plan.Lp, "p")["symbol"],
            "Kc": _leg(plan.Kc, "c")["symbol"],
            "Lc": _leg(plan.Lc, "c")["symbol"],
        },
    }

    msg = _format_alert(state, fr, plan, credit)
    tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id, msg, dry_run=cfg.dry_run)
    write_state(state)
    git_commit_state(f"TRADE {today_iso}: {plan.Kp:.0f}/{plan.Kc:.0f} ${credit:.2f}",
                     dry_run=cfg.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
