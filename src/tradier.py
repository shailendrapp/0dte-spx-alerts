"""Thin Tradier REST client.

Tradier docs: https://documentation.tradier.com
We use only the read-only market-data endpoints; no orders are placed.

Endpoints used:
  GET /v1/markets/quotes
  GET /v1/markets/options/expirations
  GET /v1/markets/options/chains
  GET /v1/markets/history
  GET /v1/markets/clock           (used for early-close detection)
"""
from __future__ import annotations
import logging
from datetime import date, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)


class TradierError(RuntimeError):
    """Raised on a non-2xx Tradier response or an unexpected payload."""


class TradierClient:
    def __init__(self, token: str, base_url: str, timeout: int = 15):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    # ------- low-level -----------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        url = f"{self.base_url}{path}"
        r = self._session.get(url, params=params or {}, timeout=self.timeout)
        if not r.ok:
            raise TradierError(f"GET {path} -> HTTP {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except ValueError as e:
            raise TradierError(f"GET {path} -> invalid JSON: {e}") from e

    # ------- market data ---------------------------------------------------
    def get_quote(self, symbol: str) -> dict:
        """Single-symbol quote.  Returns the quote dict."""
        data = self._get("/v1/markets/quotes", {"symbols": symbol})
        q = (data.get("quotes") or {}).get("quote")
        if isinstance(q, list):
            q = q[0]
        if not q:
            raise TradierError(f"No quote for {symbol}: {data}")
        return q

    def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Batch quotes; returns dict keyed by symbol."""
        data = self._get("/v1/markets/quotes", {"symbols": ",".join(symbols)})
        q = (data.get("quotes") or {}).get("quote")
        if q is None:
            raise TradierError(f"No quotes for {symbols}: {data}")
        if isinstance(q, dict):
            q = [q]
        return {row["symbol"]: row for row in q}

    def get_clock(self) -> dict:
        """Market clock (open/closed/pre/post + next-event details)."""
        data = self._get("/v1/markets/clock")
        return data.get("clock") or {}

    def get_history_daily(self, symbol: str, start: date, end: date) -> list[dict]:
        """Daily OHLC bars (inclusive)."""
        params = {
            "symbol": symbol,
            "interval": "daily",
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        data = self._get("/v1/markets/history", params)
        days = (data.get("history") or {}).get("day") or []
        if isinstance(days, dict):
            days = [days]
        return days

    # ------- options -------------------------------------------------------
    def get_option_expirations(self, underlying: str) -> list[str]:
        """All expirations for an underlying (YYYY-MM-DD strings).

        IMPORTANT: includeAllRoots=true is required to return SPXW (daily/weekly)
        expirations.  Without it, Tradier returns only standard SPX (monthly
        third-Friday AM-settled) -- so the daily 0DTE chain looks empty.
        """
        data = self._get("/v1/markets/options/expirations", {
            "symbol": underlying,
            "includeAllRoots": "true",
        })
        ex = (data.get("expirations") or {}).get("date") or []
        if isinstance(ex, str):
            ex = [ex]
        return ex

    def get_option_chain(
        self,
        underlying: str,
        expiration: str,
        greeks: bool = False,
    ) -> list[dict]:
        """Full option chain for an expiration.  Returns list of contract dicts.

        includeAllRoots=true so SPXW (PM-settled, daily) contracts are included.
        """
        params = {
            "symbol": underlying,
            "expiration": expiration,
            "greeks": str(greeks).lower(),
            "includeAllRoots": "true",
        }
        data = self._get("/v1/markets/options/chains", params)
        opts = (data.get("options") or {}).get("option") or []
        if isinstance(opts, dict):
            opts = [opts]
        return opts


# ---------- helpers atop the client -------------------------------------------
def find_atm_strike(chain: list[dict], spot: float) -> float:
    """Return strike closest to spot from the chain."""
    if not chain:
        raise TradierError("Empty option chain")
    strikes = sorted({c["strike"] for c in chain})
    return min(strikes, key=lambda s: abs(s - spot))


def split_chain(chain: list[dict]) -> tuple[dict[float, dict], dict[float, dict]]:
    """Return (calls_by_strike, puts_by_strike)."""
    calls, puts = {}, {}
    for c in chain:
        strike = float(c["strike"])
        if c["option_type"] == "call":
            calls[strike] = c
        elif c["option_type"] == "put":
            puts[strike] = c
    return calls, puts


def midpoint(contract: dict) -> float | None:
    """Bid/ask mid; falls back to last price if quote unavailable."""
    bid = contract.get("bid")
    ask = contract.get("ask")
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (float(bid) + float(ask)) / 2.0
    last = contract.get("last")
    return float(last) if last is not None else None
