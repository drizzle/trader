"""Config loader. Merges .env (secrets) + config.yaml (strategy/runtime params)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


# Load .env from project root if present. Safe to call multiple times.
load_dotenv()


class AlpacaConfig(BaseModel):
    api_key: str
    secret_key: str
    live: bool = False  # paper by default

    @property
    def base_url(self) -> str:
        return "https://api.alpaca.markets" if self.live else "https://paper-api.alpaca.markets"


class TelegramConfig(BaseModel):
    bot_token: str | None = None
    chat_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


class DeepSeekConfig(BaseModel):
    """LLM provider for the Advisors tab and Market Summary.

    Optional — if no key is configured, advisor calls return a clear
    'unconfigured' message instead of crashing.
    """
    api_key: str | None = None
    base_url: str = "https://api.deepseek.com/v1"
    default_model: str = "deepseek-chat"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


class RiskConfig(BaseModel):
    max_position_pct: float = 1.0
    daily_loss_limit_pct: float = 0.03
    min_cash_buffer_pct: float = 0.02
    kill_switch_path: str = "./data/STOP"


class ScheduleConfig(BaseModel):
    interval_minutes: int = 15


class StrategyConfig(BaseModel):
    name: str
    params: dict[str, Any] = Field(default_factory=dict)


class StorageConfig(BaseModel):
    db_filename: str = "trader.db"


class Config(BaseModel):
    universe: list[str]
    strategy: StrategyConfig
    schedule: ScheduleConfig
    risk: RiskConfig
    storage: StorageConfig
    alpaca: AlpacaConfig
    telegram: TelegramConfig
    deepseek: DeepSeekConfig
    data_dir: Path
    log_level: str = "INFO"

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.storage.db_filename


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required env var {name}. "
            f"Copy .env.example to .env and fill it in."
        )
    return val


def load_config(yaml_path: str | Path = "config.yaml") -> Config:
    """Load and validate full config from yaml + environment."""
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"Config file not found: {yaml_path.absolute()}")

    raw = yaml.safe_load(yaml_path.read_text())

    data_dir = Path(os.environ.get("DATA_DIR", "./data")).absolute()
    data_dir.mkdir(parents=True, exist_ok=True)

    alpaca = AlpacaConfig(
        api_key=_require_env("ALPACA_API_KEY"),
        secret_key=_require_env("ALPACA_SECRET_KEY"),
        live=os.environ.get("ALPACA_LIVE", "false").lower() == "true",
    )

    telegram = TelegramConfig(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
    )

    deepseek = DeepSeekConfig(
        api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
    )

    return Config(
        universe=raw["universe"],
        strategy=StrategyConfig(**raw["strategy"]),
        schedule=ScheduleConfig(**raw["schedule"]),
        risk=RiskConfig(**raw["risk"]),
        storage=StorageConfig(**raw.get("storage", {})),
        alpaca=alpaca,
        telegram=telegram,
        deepseek=deepseek,
        data_dir=data_dir,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
