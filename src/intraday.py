"""Intraday monitor — runs every 15 min from 10:00 to 15:55 ET.

Conditional alerts:
  • Short-strike BREACH (SPX touches Kp or Kc)
  • 50% PROFIT TARGET hit
  • 150% LOSS heads-up (early warning before max loss)

Each alert fires at most ONCE per day.  Tracking lives in state['alerts_fired'].
"""
from __future__ import annotations
import logging
import sys
from datetime import datetime

from .config import load_config, setup_logging
from .state import git_commit_state, read_state, write_state
from .strategy import spread_mark
from .telegram_bot import send as tg_send
from .timecheck import (
    is_us_market_weekday, now_et, parse_hhmm, today_et,
)
from .tradier import TradierClient, midpoint

log = logging.getLogger(__name__)


def _between(now_hhmm: str, first: str, last: str) -> bool:
    n = parse_hhmm(now_hhmm); a = parse_hhmm(first); b = parse_hhmm(last)
    return a <= n <= b


def main() -> int:
    setup_logging()
    cfg = load_config()
    today = today_et()
    now_hhmm = now_et().strftime("%H:%M")

    if not is_us_market_weekday():
        log.info("Not a market weekday; exit.")
        return 0
    if not _between(now_hhmm, cfg.times.intraday_first, cfg.times.intraday_last):
        log.info("Outside intraday window %s–%s ET; exit.",
                 cfg.times.intraday_first, cfg.times.intraday_last)
        return 0

    state = read_state()
    if not state:
        log.info("No state/today.json yet (morning hasn't run).  Exit.")
        return 0
    if state.get("date") != today.isoformat():
        log.info("State is for %s, not today (%s).  Exit.", state.get("date"), today)
        return 0
    if state.get("decision") != "TRADE":
        log.info("Today is %s, no intraday monitoring needed.", state.get("decision"))
        return 0

    trade = state["trade"]
    Kp, Lp, Kc, Lc = trade["Kp"], trade["Lp"], trade["Kc"], trade["Lc"]
    initial_credit = trade["credit"]
    fired = state["alerts_fired"]

    cli = TradierClient(cfg.tradier_token, cfg.tradier_base_url)

    # current SPX spot
    spot = float(cli.get_quote("SPX").get("last") or 0.0)
    if spot <= 0:
        raise RuntimeError("Bad SPX quote")
    log.info("Intraday spot=%.2f vs Kp=%.0f Kc=%.0f", spot, Kp, Kc)

    # current mid for the four legs (one batched call)
    syms = list(trade["leg_symbols"].values())
    quotes = cli.get_quotes(syms)
    leg_mids = {leg: midpoint(quotes[sym]) for leg, sym in trade["leg_symbols"].items()}
    if None in leg_mids.values():
        raise RuntimeError(f"Missing mid in leg quotes: {leg_mids}")

    mark = spread_mark(leg_mids["Kp"], leg_mids["Lp"], leg_mids["Kc"], leg_mids["Lc"])
    log.info("Spread mark: %.2f  (initial credit %.2f)", mark, initial_credit)

    # log this snapshot
    state["intraday_log"].append({
        "time": now_hhmm,
        "spot": spot,
        "mark": round(mark, 3),
        "leg_mids": {k: round(v, 3) for k, v in leg_mids.items()},
    })

    new_alerts: list[str] = []

    # ---- 50% profit target ------------------------------------------------
    if (not fired["pt_50"]
        and mark <= cfg.intraday.pt_fraction * initial_credit):
        pct = (1 - mark / initial_credit) * 100
        new_alerts.append(
            f"🎯 *50% PROFIT TARGET HIT*  ({today})\n\n"
            f"Spread mark: ${mark:.2f}  (initial credit ${initial_credit:.2f}, +{pct:.0f}% of max profit)\n"
            f"SPX: {spot:,.1f}  | Kp {Kp:,.0f}  Kc {Kc:,.0f}\n\n"
            f"_Suggest closing the iron condor for ~50% of credit._"
        )
        fired["pt_50"] = True

    # ---- 150% loss warning ------------------------------------------------
    if (not fired["loss_150"]
        and mark >= cfg.intraday.loss_warning_multiple * initial_credit):
        loss_pct = (mark / initial_credit - 1) * 100
        new_alerts.append(
            f"⚠️ *LOSS WARNING — spread at {loss_pct:.0f}% of credit*  ({today})\n\n"
            f"Spread mark: ${mark:.2f}  (initial credit ${initial_credit:.2f})\n"
            f"SPX: {spot:,.1f}  | Kp {Kp:,.0f}  Kc {Kc:,.0f}\n\n"
            f"_Approaching 200% stop. Consider closing or adjusting._"
        )
        fired["loss_150"] = True

    # ---- short-strike breaches -------------------------------------------
    if cfg.intraday.alert_on_short_breach:
        if not fired["breach_put"] and spot <= Kp:
            new_alerts.append(
                f"🔻 *SHORT PUT BREACHED* ({today})\n\n"
                f"SPX {spot:,.1f} ≤ short put {Kp:,.0f}\n"
                f"Spread mark: ${mark:.2f}  (initial ${initial_credit:.2f})\n\n"
                f"_Defended by long put at {Lp:,.0f}._"
            )
            fired["breach_put"] = True
        if not fired["breach_call"] and spot >= Kc:
            new_alerts.append(
                f"🔺 *SHORT CALL BREACHED* ({today})\n\n"
                f"SPX {spot:,.1f} ≥ short call {Kc:,.0f}\n"
                f"Spread mark: ${mark:.2f}  (initial ${initial_credit:.2f})\n\n"
                f"_Defended by long call at {Lc:,.0f}._"
            )
            fired["breach_call"] = True

    # ---- send + persist ---------------------------------------------------
    for msg in new_alerts:
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id, msg, dry_run=cfg.dry_run)

    write_state(state)
    if new_alerts:
        git_commit_state(
            f"intraday {today.isoformat()} {now_hhmm}: {len(new_alerts)} alert(s)",
            dry_run=cfg.dry_run,
        )
    else:
        # Still commit the snapshot log periodically (every 30 min) to keep git history light
        if now_hhmm.endswith(":00") or now_hhmm.endswith(":30"):
            git_commit_state(f"intraday {today.isoformat()} {now_hhmm} snapshot",
                             dry_run=cfg.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
