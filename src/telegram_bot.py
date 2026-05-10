"""Send Telegram messages via the Bot API.

No third-party SDK -- just `requests`.  Tradier-style: simple HTTP POST.
Telegram docs: https://core.telegram.org/bots/api
"""
from __future__ import annotations
import logging

import requests

log = logging.getLogger(__name__)

BOT_API = "https://api.telegram.org/bot{token}/sendMessage"


def send(token: str, chat_id: str, text: str, *, dry_run: bool = False) -> None:
    """Send a Markdown message to a Telegram chat.

    Telegram has a 4096-char limit; we truncate gracefully.
    """
    if dry_run:
        log.info("DRY_RUN -- would send Telegram message:\n%s", text)
        return

    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    r = requests.post(BOT_API.format(token=token), json=payload, timeout=10)
    if not r.ok:
        # Telegram rejects some Markdown -- retry as plain text rather than crash.
        log.warning("Telegram Markdown send failed (%s); retrying plain text", r.text[:200])
        payload.pop("parse_mode", None)
        r = requests.post(BOT_API.format(token=token), json=payload, timeout=10)
        if not r.ok:
            raise RuntimeError(f"Telegram send failed: HTTP {r.status_code} {r.text[:300]}")

    log.info("Telegram message sent (len=%d)", len(text))
