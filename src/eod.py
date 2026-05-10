"""16:05 ET end-of-day recap.

Reads state/today.json, pulls SPX close, computes settlement P&L of the
iron condor (cash-settled European intrinsic), and sends a Telegram recap.
Archives the day's state to history.jsonl.
"""
from __future__ import annotations
import logging
import sys

from .config import load_config, setup_logging
from .pricing import iron_condor_settlement
from .state import (
    archive_to_history, git_commit_state, read_state, write_state,
)
from .telegram_bot import send as tg_send
from .timecheck import is_us_market_weekday, today_et, within_window
from .tradier import TradierClient

log = logging.getLogger(__name__)
CONTRACT_MULT = 100.0
SLIPPAGE_PER_LEG = 0.10
COMMISSION_PER_LEG = 0.65


def main() -> int:
    setup_logging()
    cfg = load_config()
    today = today_et()

    if not is_us_market_weekday():
        log.info("Not a market weekday; exit.")
        return 0
    if not within_window(cfg.times.eod_recap, cfg.time_tolerance_min):
        log.info("Outside EOD window; exit.")
        return 0

    state = read_state()
    if not state or state.get("date") != today.isoformat():
        log.info("No applicable state for %s.  Exit.", today)
        return 0
    if state.get("decision") != "TRADE":
        log.info("Today was %s, no recap needed.", state.get("decision"))
        return 0
    if state["alerts_fired"].get("eod_recap"):
        log.info("EOD recap already sent today.  Exit.")
        return 0

    cli = TradierClient(cfg.tradier_token, cfg.tradier_base_url)
    spx_q = cli.get_quote("SPX")
    spx_close = float(spx_q.get("close") or spx_q.get("last") or 0.0)
    if spx_close <= 0:
        raise RuntimeError(f"Bad SPX close: {spx_q}")

    t = state["trade"]
    settle = iron_condor_settlement(spx_close, t["Kp"], t["Lp"], t["Kc"], t["Lc"])
    pnl_pts = t["credit"] - settle
    pnl_dollars = pnl_pts * CONTRACT_MULT - 8 * COMMISSION_PER_LEG  # entry+exit each leg

    Kp, Lp, Kc, Lc = t["Kp"], t["Lp"], t["Kc"], t["Lc"]
    if Kp <= spx_close <= Kc:
        outcome_emoji = "✅"
        outcome_line = f"Both shorts expired worthless — kept full credit."
    elif spx_close < Lp:
        outcome_emoji = "🔻"
        outcome_line = f"Below long put {Lp:.0f} — full max loss on put side."
    elif spx_close < Kp:
        outcome_emoji = "🔻"
        outcome_line = f"Between {Lp:.0f} and {Kp:.0f} — partial loss on put side."
    elif spx_close > Lc:
        outcome_emoji = "🔺"
        outcome_line = f"Above long call {Lc:.0f} — full max loss on call side."
    elif spx_close > Kc:
        outcome_emoji = "🔺"
        outcome_line = f"Between {Kc:.0f} and {Lc:.0f} — partial loss on call side."
    else:
        outcome_emoji = "⁉️"
        outcome_line = "Unexpected close."

    msg = (
        f"📊 *0DTE Recap* — {today}\n\n"
        f"SPX close:        {spx_close:,.1f}\n"
        f"Win zone:         {Kp:,.0f} – {Kc:,.0f}\n"
        f"{outcome_emoji}  {outcome_line}\n\n"
        f"Initial credit:   ${t['credit']:.2f}\n"
        f"Settlement cost:  ${settle:.2f}\n"
        f"Net P&L:          *${pnl_dollars:+,.2f}* per contract (incl. fees)\n"
    )
    tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id, msg, dry_run=cfg.dry_run)

    state["alerts_fired"]["eod_recap"] = True
    state["result"] = {
        "spx_close": spx_close,
        "settlement_pts": round(settle, 3),
        "pnl_pts": round(pnl_pts, 3),
        "pnl_dollars": round(pnl_dollars, 2),
    }
    write_state(state)
    archive_to_history(state)
    git_commit_state(f"EOD {today.isoformat()}: P&L ${pnl_dollars:+.0f}", dry_run=cfg.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
