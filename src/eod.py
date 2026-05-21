"""16:05 ET end-of-day recap.

FIXES vs original:
  1. Removed within_window() time gate — the eod_recap.yml cron already
     controls timing. The secondary check was silently killing runs when
     GitHub Actions runner queue jitter pushed execution past ±30 min.
  2. SPX close: falls back to yfinance ^GSPC when Tradier returns 0.
     Tradier after-hours quotes are unreliable; yfinance always has the
     official 4 PM cash close.
  3. Sends a message even on SKIP days / missing state — Telegram is
     never silent at EOD.
  4. Richer Telegram message — week-to-date P&L dots from history.jsonl.
  5. Always marks eod_recap=True and commits, so dedup guard works.
"""
from __future__ import annotations
import json
import logging
import sys
from datetime import date

from .config import load_config, setup_logging
from .pricing import iron_condor_settlement
from .state import (
    HISTORY_FILE, archive_to_history, git_commit_state,
    read_state, write_state,
)
from .telegram_bot import send as tg_send
from .timecheck import is_us_market_weekday, today_et
from .tradier import TradierClient

log = logging.getLogger(__name__)
CONTRACT_MULT    = 100.0
COMMISSION_PER_LEG = 0.65   # per leg per contract


# ── SPX close helpers ─────────────────────────────────────────────────────────

def _spx_close_tradier(cli: TradierClient) -> float:
    try:
        q = cli.get_quote("SPX")
        v = float(q.get("close") or q.get("last") or 0.0)
        if v > 0:
            log.info("SPX close from Tradier: %.2f", v)
            return v
        log.warning("Tradier returned zero/null for SPX: %s", q)
    except Exception as exc:
        log.warning("Tradier quote failed: %s", exc)
    return 0.0


def _spx_close_yfinance() -> float:
    try:
        import yfinance as yf
        hist = yf.Ticker("^GSPC").history(period="1d", interval="1m")
        if not hist.empty:
            v = round(float(hist["Close"].iloc[-1]), 2)
            log.info("SPX close from yfinance: %.2f", v)
            return v
        log.warning("yfinance returned empty history for ^GSPC")
    except Exception as exc:
        log.warning("yfinance fallback failed: %s", exc)
    return 0.0


# ── Week-to-date summary ──────────────────────────────────────────────────────

def _week_summary() -> str:
    if not HISTORY_FILE.exists():
        return ""
    monday = today_et()
    monday = monday.replace(day=monday.day - monday.weekday())

    wins = losses = scratches = 0
    weekly_pnl = 0.0
    dots: list[str] = []

    try:
        for line in HISTORY_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("date", "") < monday.isoformat():
                continue
            if rec.get("decision") == "SKIP":
                dots.append("🚫")
                continue
            result = rec.get("result")
            if not result:
                dots.append("⚪")
                continue
            pnl = float(result.get("pnl_dollars", 0.0))
            weekly_pnl += pnl
            if pnl > 0:
                wins += 1; dots.append("✅")
            elif pnl < 0:
                losses += 1; dots.append("❌")
            else:
                scratches += 1; dots.append("➖")
    except Exception as exc:
        log.warning("Could not parse history.jsonl: %s", exc)
        return ""

    total = wins + losses + scratches
    if total == 0 and not dots:
        return ""

    wr   = int(wins / total * 100) if total else 0
    sign = "+" if weekly_pnl >= 0 else ""
    dot_str = "  ".join(dots) if dots else "–"

    return (
        f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Week to date:*\n"
        f"{dot_str}\n"
        f"{wins}W / {losses}L  |  Win rate: {wr}%\n"
        f"Week P&L: *${sign}{weekly_pnl:,.0f}*"
    )


# ── Message builders ──────────────────────────────────────────────────────────

def _msg_no_state(today: date) -> str:
    return (
        f"📊 *0DTE EOD — {today}*\n\n"
        f"⚠️ No morning alert state found for today.\n"
        f"Morning script may have been skipped or state commit failed.\n\n"
        f"_Check GitHub Actions → morning\\_alert for details._"
    )


def _msg_skip(today: date, reason: str) -> str:
    return (
        f"📊 *0DTE EOD — {today}*\n\n"
        f"🚫 No trade today\n"
        f"Reason: _{reason}_\n\n"
        f"_System running normally. Re-evaluates tomorrow at 09:32 ET._"
        f"{_week_summary()}"
    )


def _msg_trade(today: date, t: dict, spx_close: float,
               settle: float, pnl_pts: float, pnl_dollars: float) -> str:
    Kp, Lp, Kc, Lc = t["Kp"], t["Lp"], t["Kc"], t["Lc"]
    credit   = t["credit"]
    max_loss = t["max_loss_per_contract"]

    if Kp <= spx_close <= Kc:
        emoji, label = "✅", "WIN — full credit kept"
        detail = f"SPX closed inside win zone ({Kp:,.0f}–{Kc:,.0f})."
    elif spx_close < Lp:
        emoji, label = "🔻", "LOSS — max loss (put side)"
        detail = f"SPX closed below long put {Lp:,.0f}."
    elif spx_close < Kp:
        emoji, label = "🔻", "LOSS — partial (put side)"
        detail = f"SPX closed between {Lp:,.0f} and {Kp:,.0f}."
    elif spx_close > Lc:
        emoji, label = "🔺", "LOSS — max loss (call side)"
        detail = f"SPX closed above long call {Lc:,.0f}."
    elif spx_close > Kc:
        emoji, label = "🔺", "LOSS — partial (call side)"
        detail = f"SPX closed between {Kc:,.0f} and {Lc:,.0f}."
    else:
        emoji, label, detail = "⁉️", "UNEXPECTED", "Check manually."

    sign = "+" if pnl_dollars >= 0 else ""

    return (
        f"📊 *0DTE EOD Recap — {today}*\n\n"
        f"SPX close:        *{spx_close:,.2f}*\n"
        f"Win zone:          {Kp:,.0f} – {Kc:,.0f}\n\n"
        f"{emoji}  *{label}*\n"
        f"_{detail}_\n\n"
        f"Initial credit:    ${credit:.2f}  (~${credit*100:.0f}/contract)\n"
        f"Settlement cost:   ${settle:.2f}\n"
        f"Net P&L:           *${sign}{pnl_dollars:,.0f}* per contract\n"
        f"Max risk was:      ${max_loss:,.0f}/contract\n"
        f"{_week_summary()}"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    setup_logging()
    cfg   = load_config()
    today = today_et()

    if not is_us_market_weekday():
        log.info("Not a market weekday — exit.")
        return 0

    # NOTE: within_window() gate intentionally removed.
    # The eod_recap.yml cron controls timing. A secondary time-window check
    # caused silent drops due to GitHub Actions queue jitter + dual-cron DST.

    state = read_state()

    # No state for today
    if not state or state.get("date") != today.isoformat():
        log.info("No valid state for %s — sending notice.", today)
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id,
                _msg_no_state(today), dry_run=cfg.dry_run)
        return 0

    # Already sent
    if state["alerts_fired"].get("eod_recap"):
        log.info("EOD recap already sent today — exit.")
        return 0

    # SKIP day
    if state.get("decision") != "TRADE":
        reason = state.get("skip_reason") or "filters triggered"
        tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id,
                _msg_skip(today, reason), dry_run=cfg.dry_run)
        state["alerts_fired"]["eod_recap"] = True
        write_state(state)
        git_commit_state(f"EOD {today.isoformat()}: SKIP", dry_run=cfg.dry_run)
        return 0

    # TRADE day — fetch close
    cli = TradierClient(cfg.tradier_token, cfg.tradier_base_url)
    spx_close = _spx_close_tradier(cli)
    if spx_close <= 0:
        log.info("Tradier gave no close — trying yfinance.")
        spx_close = _spx_close_yfinance()
    if spx_close <= 0:
        raise RuntimeError(
            "Could not fetch SPX close from Tradier or yfinance. "
            "Check TRADIER_TOKEN secret."
        )

    # Compute P&L
    t          = state["trade"]
    settle     = iron_condor_settlement(spx_close, t["Kp"], t["Lp"], t["Kc"], t["Lc"])
    pnl_pts    = t["credit"] - settle
    pnl_dollars = pnl_pts * CONTRACT_MULT - 8 * COMMISSION_PER_LEG

    # Send
    msg = _msg_trade(today, t, spx_close, settle, pnl_pts, pnl_dollars)
    tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id, msg, dry_run=cfg.dry_run)

    # Persist
    state["alerts_fired"]["eod_recap"] = True
    state["result"] = {
        "spx_close":      spx_close,
        "settlement_pts": round(settle, 3),
        "pnl_pts":        round(pnl_pts, 3),
        "pnl_dollars":    round(pnl_dollars, 2),
    }
    write_state(state)
    archive_to_history(state)
    git_commit_state(
        f"EOD {today.isoformat()}: P&L ${pnl_dollars:+.0f}",
        dry_run=cfg.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
