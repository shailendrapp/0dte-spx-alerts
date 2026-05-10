"""Configuration loader: reads config.yaml + environment variables."""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


@dataclass(frozen=True)
class StrategyConfig:
    sigma_multiplier: float
    wing_width: float
    em_multiplier: float
    strike_round: int


@dataclass(frozen=True)
class FilterConfig:
    vol_skip_threshold: float
    overnight_gap_threshold: float
    trend_skip_threshold: float
    realized_vol_lookback: int


@dataclass(frozen=True)
class IntradayConfig:
    pt_fraction: float
    loss_warning_multiple: float
    alert_on_short_breach: bool


@dataclass(frozen=True)
class TimesConfig:
    morning_alert: str   # "HH:MM" in ET
    intraday_first: str
    intraday_last: str
    eod_recap: str


@dataclass(frozen=True)
class Config:
    strategy: StrategyConfig
    filters: FilterConfig
    intraday: IntradayConfig
    times: TimesConfig
    time_tolerance_min: int
    # secrets
    tradier_token: str
    tradier_base_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    dry_run: bool


def _require_env(name: str, allow_missing: bool = False) -> str:
    val = os.environ.get(name, "").strip()
    if not val and not allow_missing:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            f"Set it locally in .env or as a GitHub Secret."
        )
    return val


def load_config() -> Config:
    raw: dict[str, Any] = yaml.safe_load(CONFIG_PATH.read_text())

    cfg = Config(
        strategy=StrategyConfig(**raw["strategy"]),
        filters=FilterConfig(**raw["filters"]),
        intraday=IntradayConfig(**raw["intraday"]),
        times=TimesConfig(**raw["times_et"]),
        time_tolerance_min=int(raw["time_tolerance_min"]),
        tradier_token=_require_env("TRADIER_TOKEN"),
        tradier_base_url=_require_env("TRADIER_BASE_URL"),
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_require_env("TELEGRAM_CHAT_ID"),
        dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
    )
    return cfg


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
