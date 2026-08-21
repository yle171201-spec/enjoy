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
    bootstrap_start_date: str = _env("BOOTSTRAP_START_DATE", "2025-01-01")
    bootstrap_batch_size: int = int(_env("BOOTSTRAP_BATCH_SIZE", "100"))
    live_scan_calendar_days: int = int(_env("LIVE_SCAN_CALENDAR_DAYS", "720"))
    min_scan_bootstrap_coverage: float = float(_env("MIN_SCAN_BOOTSTRAP_COVERAGE", "0.95"))
    min_scan_history_bars: int = int(_env("MIN_SCAN_HISTORY_BARS", "250"))
    min_scan_stocks: int = int(_env("MIN_SCAN_STOCKS", "3000"))
    calendar_gap_check_days: int = int(_env("CALENDAR_GAP_CHECK_DAYS", "420"))
    default_n_days: int = int(_env("DEFAULT_N_DAYS", "20"))
    slippage_bps: float = float(_env("SLIPPAGE_BPS", "0"))
    web_version: str = "V2.1.1"
    strategy_version: str = "V18"
    cookie_secure: bool = _env("COOKIE_SECURE", "0").lower() in {"1","true","yes","on"}


settings = Settings()
