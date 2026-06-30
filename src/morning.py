"""09:32 ET morning alert.

  1. Check time window + market open (Tradier clock).
  2. Pull SPX spot + 7 days of SPY history.
  3. Pull SPX 0DTE option chain.
  4. Compute Expected Move from ATM straddle (tastytrade convention).
  5. Run regime filters.
  6. Fetch GEX walls from FlashAlpha (NEW — optional, non-blocking).
  7. If TRADE: select strikes with GEX-wall anchor, compute credit.
  8. Send Telegram alert; write & commit state/today.json.

CHANGE vs original:
  • Steps 6 added: _fetch_gex_walls() pulls call_wall / put_wall from
    FlashAlpha.  Failure is non-fatal — falls back to original behaviour.
  • build_trade_plan() receives wall data; adjusts Kc/Kp when the default
    strike sits too far below/above the nearest GEX wall.
  • _format_alert() shows GEX context and adjustment notice when fired.
  • state["trade"] stores call_wall / put_wall for EOD audit trail.
  Everything else (Tradier calls, filters, credit calc) is UNCHANGED.
"""
from __future__ import annotations
import logging
import sys
from datetime import timedelta

import requests

from .config import load_config, setup_logging
from .pricing import iron_condor_credit
from .state import empty_state, git_commit_state, write_state
from .strategy import (
    TradePlan, build_trade_plan, evaluate_filters,
    expected_move_from_straddle,
)
from .telegram_bot import send as tg_send
from .timecheck import is_us_market_weekday, today_et, within_window
from .tradier import (
    TradierClient, find_atm_strike, midpoint, split_chain,
)

log = logging.getLogger(__name__)
UNDERLYING     = "SPX"
HISTORY_SYMBOL = "SPY"


# ── GEX wall fetcher (FlashAlpha) ────────────────────────────────────────────

def _fetch_gex_walls(api_key: str) -> tuple[float | None, float | None]:
    """Return (call_wall, put_wall) from FlashAlpha, or (None, None) on failure.

    Non-blocking: any network/parse error is logged as WARNING and the
    caller falls back to wall-unaware strike placement.
    """
    if not api_key:
        log.info("FLASHALPHA_API_KEY not set — skipping GEX wall fetch.")
        return None, None
    try:
        url = "https://lab.flashalpha.com/v1/exposure/gex/SPX"
        resp = requests.get(
            url,
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        if not resp.ok:
            log.warning("FlashAlpha GEX API %s: %s", resp.status_code, resp.text[:200])
            return None, None

        data = resp.json()
        # FlashAlpha returns levels nested under 'data' or at top level
        levels = data.get("data") or data
        call_wall = levels.get("call_wall") or levels.get("callWall")
        put_wall  = levels.get("put_wall")  or levels.get("putWall")

        if call_wall:
            call_wall = float(call_wall)
        if put_wall:
            put_wall = float(put_wall)

        log.info("GEX walls — call: %s  put: %s", call_wall, put_wall)
        return call_wall, put_wall

    except Exception as exc:
        log.warning("FlashAlpha GEX fetch failed (%s) — proceeding without walls.", exc)
        return None, None


# ── Telegram message formatter ────────────────────────────────────────────────

def _format_alert(
    state: dict,
    fr,
    plan: TradePlan | None,
    credit_pts: float,
) -> str:
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

    # Build GEX context line
    gex_lines = ""
    if plan.call_wall or plan.put_wall:
        cw = f"{plan.call_wall:,.0f}" if plan.call_wall else "n/a"
        pw = f"{plan.put_wall:,.0f}"  if plan.put_wall  else "n/a"
        gex_lines = f"GEX walls:      call {cw}  /  put {pw}\n"

    # Adjustment notices
    adj_lines = ""
    if plan.kc_adjusted:
        adj_lines += (
            f"⚙️ _Kc widened toward call wall "
            f"(default was {t['S0'] + plan.EM:.0f}, "
            f"wall gap > {t.get('wall_gap_buffer', '?')}pts)_\n"
        )
    if plan.kp_adjusted:
        adj_lines += (
            f"⚙️ _Kp widened toward put wall "
            f"(default was {t['S0'] - plan.EM:.0f}, "
            f"wall gap > {t.get('wall_gap_buffer', '?')}pts)_\n"
        )

    return (
        f"🔔 *0DTE SPX Iron Condor* — {state['date']}\n\n"
        f"SPX (open):     {plan.S0:,.1f}\n"
        f"Expected Move:  ±{plan.EM:.0f} pts (from ATM straddle)\n"
        f"σ realized 5d:  {fr.rv_annual*100:.1f}%\n"
        f"Overnight gap:  {fr.overnight_gap*100:+.2f}%\n"
        f"5-day trend:    {fr.trend_5d*100:+.2f}%\n"
        f"{gex_lines}"
        f"Filters:        ✅ all pass\n\n"
        f"*PLACE:*\n"
        f"  SELL  PUT   {plan.Kp:,.0f}\n"
        f"  BUY   PUT   {plan.Lp:,.0f}\n"
        f"  SELL  CALL  {plan.Kc:,.0f}\n"
        f"  BUY   CALL  {plan.Lc:,.0f}\n\n"
        f"{adj_lines}"
        f"Estimated credit:    *${credit_pts:.2f}* (~${credit_pts*100:.0f}/contract)\n"
        f"Max loss / contract: ${t['max_loss_per_contract']:.0f}\n"
        f"Win zone (close):    {plan.Kp:,.0f} – {plan.Kc:,.0f}\n\n"
        f"_Hold to 16:00 ET cash settlement._"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    setup_logging()
    cfg   = load_config()
    today = today_et()

    # 1. Time + weekday gates (UNCHANGED)
    if not is_us_market_weekday():
        log.info("Not a US market weekday; exiting.")
        return 0
    if not within_window(cfg.times.morning_alert, cfg.time_tolerance_min):
        log.info("Outside %s ±%d min window; exiting.",
                 cfg.times.morning_alert, cfg.time_tolerance_min)
        return 0

    cli = TradierClient(cfg.tradier_token, cfg.tradier_base_url)

    # 1b. Holiday check (UNCHANGED)
    clock     = cli.get_clock()
    state_str = (clock.get("state") or "").lower()
    if state_str == "closed":
        log.info("Tradier clock: market CLOSED (holiday). Exiting.")
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id,
                f"ℹ️ 0DTE — market closed today ({today}). No alert.",
                dry_run=cfg.dry_run)
        return 0

    # 2. SPX spot + SPY history (UNCHANGED)
    spx_q = cli.get_quote(UNDERLYING)
    spot  = float(spx_q.get("last") or spx_q.get("close") or 0.0)
    if spot <= 0:
        raise RuntimeError(f"Unusable SPX quote: {spx_q}")
    log.info("SPX spot: %.2f", spot)

    bars = cli.get_history_daily(HISTORY_SYMBOL, today - timedelta(days=14), today)
    daily_bars = [
        {
            "date":  b["date"],
            "open":  float(b["open"])  * 10,
            "high":  float(b["high"])  * 10,
            "low":   float(b["low"])   * 10,
            "close": float(b["close"]) * 10,
        }
        for b in bars
    ]
    if len(daily_bars) < cfg.filters.realized_vol_lookback + 1:
        raise RuntimeError(f"Not enough history bars: {len(daily_bars)}")
    prior_close = daily_bars[-2]["close"]
    log.info("Prior close (SPY×10): %.2f", prior_close)

    # 3. 0DTE expiration check (UNCHANGED)
    expirations = cli.get_option_expirations(UNDERLYING)
    today_iso   = today.isoformat()
    if today_iso not in expirations:
        log.info("No 0DTE chain for %s. Exiting.", today_iso)
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id,
                f"ℹ️ 0DTE — no SPXW expiry on {today_iso}. No alert.",
                dry_run=cfg.dry_run)
        return 0
    expiration = today_iso

    chain       = cli.get_option_chain(UNDERLYING, expiration)
    calls, puts = split_chain(chain)
    log.info("Chain: %d calls, %d puts", len(calls), len(puts))

    # 4. Expected move from ATM straddle (UNCHANGED)
    atm_k    = find_atm_strike(chain, spot)
    atm_call = calls.get(atm_k)
    atm_put  = puts.get(atm_k)
    if not (atm_call and atm_put):
        raise RuntimeError(f"ATM strike {atm_k} missing call or put")
    em = expected_move_from_straddle(
        midpoint(atm_call), midpoint(atm_put), cfg.strategy.em_multiplier
    )
    log.info("ATM straddle = %.2f → EM = %.1f pts", em / cfg.strategy.em_multiplier, em)

    # 5. Regime filters (UNCHANGED)
    fr = evaluate_filters(spot, prior_close, daily_bars, cfg.filters)
    log.info("Filters: skip=%s reason=%s rv=%.3f gap=%.4f trend=%.4f",
             fr.skip, fr.reason, fr.rv_annual, fr.overnight_gap, fr.trend_5d)

    state = empty_state(today)

    if fr.skip:
        state["decision"]    = "SKIP"
        state["skip_reason"] = fr.reason
        msg = _format_alert(state, fr, None, 0.0)
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id, msg, dry_run=cfg.dry_run)
        write_state(state)
        git_commit_state(f"SKIP {today_iso}: {fr.reason}", dry_run=cfg.dry_run)
        return 0

    # 6. GEX walls from FlashAlpha (NEW — non-blocking)
    call_wall, put_wall = _fetch_gex_walls(cfg.flashalpha_api_key)

    # 7. Build trade plan with optional wall anchoring (MODIFIED CALL)
    plan = build_trade_plan(
        spot, em, cfg.strategy,
        call_wall=call_wall,
        put_wall=put_wall,
    )

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

    credit   = iron_condor_credit(sp, lp, sc, lc)
    max_loss = (cfg.strategy.wing_width - credit) * 100

    state["decision"] = "TRADE"
    state["trade"] = {
        "S0":  plan.S0,  "EM": plan.EM,
        "Kp":  plan.Kp,  "Lp": plan.Lp,
        "Kc":  plan.Kc,  "Lc": plan.Lc,
        "credit":               round(credit,   2),
        "max_loss_per_contract": round(max_loss, 0),
        "underlying":  UNDERLYING,
        "expiration":  expiration,
        # GEX audit trail (NEW)
        "call_wall":   call_wall,
        "put_wall":    put_wall,
        "kc_adjusted": plan.kc_adjusted,
        "kp_adjusted": plan.kp_adjusted,
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
    git_commit_state(
        f"TRADE {today_iso}: {plan.Kp:.0f}/{plan.Kc:.0f} ${credit:.2f}"
        + (" [GEX adj]" if plan.kc_adjusted or plan.kp_adjusted else ""),
        dry_run=cfg.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
