"""Configuration loader: reads config.yaml + environment variables.

CHANGE vs original:
  • StrategyConfig gets two new fields: wall_gap_buffer, min_wall_gap.
  • Config gets flashalpha_api_key (optional — empty string if not set).
  Everything else is UNCHANGED.
"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT   = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


@dataclass(frozen=True)
class StrategyConfig:
    sigma_multiplier: float
    wing_width:       float
    em_multiplier:    float
    strike_round:     int
    # GEX wall anchor (NEW)
    # If the default short strike is more than wall_gap_buffer pts away
    # from the nearest GEX wall, snap it to (wall - min_wall_gap).
    wall_gap_buffer: float   # default 80 pts; set 0 to disable
    min_wall_gap:    float   # how close to the wall Kc/Kp lands (default 40 pts)


@dataclass(frozen=True)
class FilterConfig:
    vol_skip_threshold:     float
    overnight_gap_threshold: float
    trend_skip_threshold:   float
    realized_vol_lookback:  int


@dataclass(frozen=True)
class IntradayConfig:
    pt_fraction:           float
    loss_warning_multiple: float
    alert_on_short_breach: bool


@dataclass(frozen=True)
class TimesConfig:
    morning_alert:  str
    intraday_first: str
    intraday_last:  str
    eod_recap:      str


@dataclass(frozen=True)
class Config:
    strategy: StrategyConfig
    filters:  FilterConfig
    intraday: IntradayConfig
    times:    TimesConfig
    time_tolerance_min: int
    # secrets
    tradier_token:      str
    tradier_base_url:   str
    telegram_bot_token: str
    telegram_chat_id:   str
    flashalpha_api_key: str   # NEW — optional, empty string = disabled
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

    return Config(
        strategy=StrategyConfig(**raw["strategy"]),
        filters =FilterConfig(**raw["filters"]),
        intraday=IntradayConfig(**raw["intraday"]),
        times   =TimesConfig(**raw["times_et"]),
        time_tolerance_min=int(raw["time_tolerance_min"]),
        tradier_token     =_require_env("TRADIER_TOKEN"),
        tradier_base_url  =_require_env("TRADIER_BASE_URL"),
        telegram_bot_token=_require_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id  =_require_env("TELEGRAM_CHAT_ID"),
        flashalpha_api_key=_require_env("FLASHALPHA_API_KEY", allow_missing=True),
        dry_run=os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes"),
    )


def setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
