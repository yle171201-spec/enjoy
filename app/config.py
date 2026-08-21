from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    app_name: str = _env("APP_NAME", "A股ABC在线交易系统")
    app_password: str = _env("APP_PASSWORD", "change-me")
    session_secret: str = _env("SESSION_SECRET", "dev-secret-change-me")
    database_url: str = _env("DATABASE_URL", "sqlite:///./data/abc_strategy.db")
    data_provider: str = _env("DATA_PROVIDER", "akshare").lower()
    tushare_token: str = _env("TUSHARE_TOKEN", "")
    strategy_start_date: str = _env("STRATEGY_START_DATE", "2022-01-01")
    default_n_days: int = int(_env("DEFAULT_N_DAYS", "20"))
    slippage_bps: float = float(_env("SLIPPAGE_BPS", "0"))
    web_version: str = "V2"
    strategy_version: str = "V18"
    cookie_secure: bool = _env("COOKIE_SECURE", "0").lower() in {"1","true","yes","on"}


settings = Settings()
