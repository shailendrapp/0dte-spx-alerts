"""Manual health-check workflow.

Run this any time from the Actions tab to verify:
  1. GitHub Secrets are loaded
  2. Tradier token works (cheap clock call)
  3. Telegram bot can deliver

Bypasses all time/weekday gates -- this is the "is everything still wired?" button.
Sends a short Telegram message regardless of the result so the user knows.
"""
from __future__ import annotations
import logging
import sys

from .config import load_config, setup_logging
from .telegram_bot import send as tg_send
from .timecheck import now_et
from .tradier import TradierClient, TradierError

log = logging.getLogger(__name__)


def main() -> int:
    setup_logging()
    log.info("Ping started.")
    cfg = load_config()
    now = now_et()

    tradier_ok = False
    tradier_detail = ""
    try:
        cli = TradierClient(cfg.tradier_token, cfg.tradier_base_url)
        clock = cli.get_clock()
        market_state = clock.get("state") or "unknown"
        tradier_ok = True
        tradier_detail = f"market state = `{market_state}`"
    except TradierError as e:
        tradier_detail = f"❌ Tradier error: {e}"
    except Exception as e:
        tradier_detail = f"❌ Unexpected: {e}"

    status_emoji = "✅" if tradier_ok else "❌"
    text = (
        f"{status_emoji} *0DTE alerter — ping*\n\n"
        f"ET time:       {now.strftime('%Y-%m-%d %H:%M:%S')} ET\n"
        f"Tradier:       {'✅ reachable' if tradier_ok else '❌ FAIL'}\n"
        f"                 {tradier_detail}\n"
        f"Telegram:      ✅ delivered (this message)\n"
        f"Base URL:      `{cfg.tradier_base_url}`\n\n"
        f"_If you got this message, end-to-end wiring is healthy._"
    )
    tg_send(cfg.telegram_bot_token, cfg.telegram_chat_id, text, dry_run=cfg.dry_run)
    log.info("Ping complete.  Tradier=%s", tradier_ok)
    return 0 if tradier_ok else 1


if __name__ == "__main__":
    sys.exit(main())
