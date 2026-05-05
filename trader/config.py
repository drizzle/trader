"""Config loader. Merges .env (secrets) + config.yaml (strategy/runtime params)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


# Load .env from project root by default. The dashboard service disables this
# so Alpaca trading keys stay out of the dashboard process.
if os.environ.get("TRADER_LOAD_DOTENV", "true").lower() not in {"0", "false", "no", "off"}:
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


class AccountConfig(BaseModel):
    id: str
    label: str | None = None
    type: str = "paper"  # paper, live, ira
    env_prefix: str | None = None
    live: bool | None = None
    data_dir: str | None = None
    enabled_assets: list[str] = Field(default_factory=lambda: ["us_equity"])

    @property
    def display_label(self) -> str:
        return self.label or self.id

    @property
    def is_ira(self) -> bool:
        return self.type.lower() == "ira"


class Config(BaseModel):
    universe: list[str]
    strategy: StrategyConfig
    schedule: ScheduleConfig
    risk: RiskConfig
    storage: StorageConfig
    accounts: list[AccountConfig] = Field(default_factory=list)
    account: AccountConfig
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


def _account_env_name(account: AccountConfig, suffix: str) -> str:
    prefix = account.env_prefix
    if prefix:
        return f"{prefix}_{suffix}"
    return suffix


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.lower() in {"1", "true", "yes", "on"}


def _looks_crypto_symbol(symbol: str) -> bool:
    return "/" in symbol or symbol.upper().endswith("USD")


def _collect_strategy_symbols(raw: dict[str, Any]) -> set[str]:
    symbols = set(str(s) for s in raw.get("universe", []) if s)
    params = raw.get("strategy", {}).get("params", {}) or {}
    for key, value in params.items():
        if "symbol" in key and isinstance(value, str):
            symbols.add(value)
    return symbols


def _default_account(raw: dict[str, Any]) -> AccountConfig:
    live = _env_bool("ALPACA_LIVE", False)
    return AccountConfig(
        id="live_account" if live else "paper_account",
        label="Live Account" if live else "Paper Account",
        type="live" if live else "paper",
        live=live,
        enabled_assets=["us_equity", "crypto"],
    )


def _select_account(raw: dict[str, Any], account_id: str | None) -> tuple[list[AccountConfig], AccountConfig]:
    accounts = [AccountConfig(**a) for a in raw.get("accounts", [])]
    if not accounts:
        account = _default_account(raw)
        return [account], account

    selected = account_id or os.environ.get("TRADER_ACCOUNT") or raw.get("active_account") or accounts[0].id
    for account in accounts:
        if account.id == selected:
            return accounts, account
    available = ", ".join(a.id for a in accounts)
    raise RuntimeError(f"Unknown TRADER_ACCOUNT '{selected}'. Available accounts: {available}")


def _load_alpaca_for_account(account: AccountConfig, require_alpaca: bool) -> AlpacaConfig:
    key_name = _account_env_name(account, "ALPACA_API_KEY")
    secret_name = _account_env_name(account, "ALPACA_SECRET_KEY")
    live_name = _account_env_name(account, "ALPACA_LIVE")
    live = account.live if account.live is not None else _env_bool(live_name, account.type.lower() in {"live", "ira"})

    if require_alpaca:
        return AlpacaConfig(
            api_key=_require_env(key_name),
            secret_key=_require_env(secret_name),
            live=bool(live),
        )
    return AlpacaConfig(
        api_key=os.environ.get(key_name, "dashboard-disabled"),
        secret_key=os.environ.get(secret_name, "dashboard-disabled"),
        live=bool(live),
    )


def _data_dir_for_account(account: AccountConfig, multi_account: bool) -> Path:
    if account.data_dir:
        return Path(account.data_dir).absolute()
    root = Path(os.environ.get("DATA_DIR", "./data")).absolute()
    return root / account.id if multi_account else root


def validate_account_strategy(account: AccountConfig, raw: dict[str, Any]) -> None:
    """Reject strategies that are not appropriate for the selected account."""
    if not account.is_ira:
        return
    crypto_symbols = sorted(s for s in _collect_strategy_symbols(raw) if _looks_crypto_symbol(s))
    if crypto_symbols:
        raise ValueError(
            f"Account '{account.id}' is configured as IRA, but IRA accounts cannot trade crypto. "
            f"Remove crypto symbols from the strategy/universe: {crypto_symbols}"
        )


def load_config(
    yaml_path: str | Path = "config.yaml",
    require_alpaca: bool = True,
    account_id: str | None = None,
    validate_strategy: bool = True,
) -> Config:
    """Load and validate full config from yaml + environment."""
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"Config file not found: {yaml_path.absolute()}")

    raw = yaml.safe_load(yaml_path.read_text())

    accounts, account = _select_account(raw, account_id)
    if validate_strategy:
        validate_account_strategy(account, raw)

    data_dir = _data_dir_for_account(account, multi_account=bool(raw.get("accounts")))
    data_dir.mkdir(parents=True, exist_ok=True)

    alpaca = _load_alpaca_for_account(account, require_alpaca)

    telegram = TelegramConfig(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
    )

    deepseek = DeepSeekConfig(
        api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
    )

    risk = RiskConfig(**raw["risk"])
    if raw.get("accounts"):
        kill_path = Path(risk.kill_switch_path)
        if not kill_path.is_absolute():
            risk = risk.model_copy(update={"kill_switch_path": str(data_dir / kill_path.name)})

    return Config(
        universe=raw["universe"],
        strategy=StrategyConfig(**raw["strategy"]),
        schedule=ScheduleConfig(**raw["schedule"]),
        risk=risk,
        storage=StorageConfig(**raw.get("storage", {})),
        accounts=accounts,
        account=account,
        alpaca=alpaca,
        telegram=telegram,
        deepseek=deepseek,
        data_dir=data_dir,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
