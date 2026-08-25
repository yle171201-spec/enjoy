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
    database_capacity_mb: int = int(_env("DATABASE_CAPACITY_MB", "1024"))
    data_provider: str = _env("DATA_PROVIDER", "akshare").lower()
    tushare_token: str = _env("TUSHARE_TOKEN", "")
    strategy_start_date: str = _env("STRATEGY_START_DATE", "2022-01-01")
    bootstrap_start_date: str = _env("BOOTSTRAP_START_DATE", "2025-01-01")
    bootstrap_batch_size: int = int(_env("BOOTSTRAP_BATCH_SIZE", "100"))
    bootstrap_stock_timeout_seconds: int = int(_env("BOOTSTRAP_STOCK_TIMEOUT_SECONDS", "60"))
    gap_repair_batch_size: int = int(_env("GAP_REPAIR_BATCH_SIZE", "500"))
    gap_repair_workers: int = int(_env("GAP_REPAIR_WORKERS", "2"))
    latest_audit_batch_size: int = int(_env("LATEST_AUDIT_BATCH_SIZE", "200"))
    live_scan_calendar_days: int = int(_env("LIVE_SCAN_CALENDAR_DAYS", "720"))
    scan_frame_batch_size: int = int(_env("SCAN_FRAME_BATCH_SIZE", "160"))
    live_scan_batch_size: int = int(_env("LIVE_SCAN_BATCH_SIZE", "160"))
    min_scan_bootstrap_coverage: float = float(_env("MIN_SCAN_BOOTSTRAP_COVERAGE", "0.95"))
    min_latest_bar_coverage: float = float(_env("MIN_LATEST_BAR_COVERAGE", "0.95"))
    min_latest_verified_coverage: float = float(_env("MIN_LATEST_VERIFIED_COVERAGE", "0.995"))
    min_scan_history_bars: int = int(_env("MIN_SCAN_HISTORY_BARS", "250"))
    min_scan_stocks: int = int(_env("MIN_SCAN_STOCKS", "3000"))
    calendar_gap_check_days: int = int(_env("CALENDAR_GAP_CHECK_DAYS", "420"))
    default_n_days: int = int(_env("DEFAULT_N_DAYS", "20"))
    slippage_bps: float = float(_env("SLIPPAGE_BPS", "0"))
    web_version: str = "V2.3.4-RC4"
    strategy_version: str = "V18"
    live_strategy_version: str = "V18-LIVE"
    cookie_secure: bool = _env("COOKIE_SECURE", "0").lower() in {"1","true","yes","on"}


settings = Settings()
