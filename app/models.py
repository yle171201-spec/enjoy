from __future__ import annotations

from datetime import date, datetime
from sqlalchemy import String, Float, Integer, Date, DateTime, Boolean, Text, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base


class Stock(Base):
    __tablename__ = "stocks"
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    market: Mapped[str] = mapped_column(String(8), default="")
    board: Mapped[str] = mapped_column(String(24), default="")
    is_st: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DailyBar(Base):
    __tablename__ = "daily_bars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    amount: Mapped[float] = mapped_column(Float)
    turnover: Mapped[float] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("code", "trade_date", name="uq_bar_code_date"),
        Index("ix_bar_date_code", "trade_date", "code"),
    )


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="V18")
    code: Mapped[str] = mapped_column(String(6), index=True)
    signal_date: Mapped[date] = mapped_column(Date, index=True)
    engine: Mapped[str] = mapped_column(String(1), index=True)
    signal_close: Mapped[float] = mapped_column(Float)
    fail_price: Mapped[float] = mapped_column(Float)
    risk_pct: Mapped[float] = mapped_column(Float)
    target_weight: Mapped[float] = mapped_column(Float)
    h_daily: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    exit_ret: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("strategy_version", "code", "signal_date", "engine", name="uq_signal"),)


class ScanRun(Base):
    __tablename__ = "scan_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    a_count: Mapped[int] = mapped_column(Integer, default=0)
    b_count: Mapped[int] = mapped_column(Integer, default=0)
    c_count: Mapped[int] = mapped_column(Integer, default=0)
    combined_count: Mapped[int] = mapped_column(Integer, default=0)
    golden_matched: Mapped[int | None] = mapped_column(Integer, nullable=True)
    golden_missing: Mapped[int | None] = mapped_column(Integer, nullable=True)
    golden_extra: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, default="")


class DataUpdateRun(Base):
    __tablename__ = "data_update_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provider: Mapped[str] = mapped_column(String(24), default="")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    stock_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="running")
    message: Mapped[str] = mapped_column(Text, default="")


class BootstrapStock(Base):
    __tablename__ = "bootstrap_stocks"
    code: Mapped[str] = mapped_column(String(6), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class LatestDayAudit(Base):
    # Per-stock verification for a specific latest trading day.
    # status: repaired / suspended / unknown / invalid / error
    __tablename__ = "latest_day_audits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(6), index=True)
    target_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(24), default="unknown", index=True)
    source: Mapped[str] = mapped_column(String(24), default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("code", "target_date", name="uq_latest_day_audit"),
        Index("ix_latest_day_audit_target_status", "target_date", "status"),
    )

class LiveMarketState(Base):
    __tablename__ = "live_market_states"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="V18")
    trade_date: Mapped[date] = mapped_column(Date, index=True)
    q40: Mapped[float | None] = mapped_column(Float, nullable=True)
    above20: Mapped[float | None] = mapped_column(Float, nullable=True)
    mom20: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("strategy_version", "trade_date", name="uq_live_market_state"),
        Index("ix_live_market_version_date", "strategy_version", "trade_date"),
    )


class LivePeerEvent(Base):
    __tablename__ = "live_peer_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_version: Mapped[str] = mapped_column(String(32), default="V18")
    code: Mapped[str] = mapped_column(String(6), index=True)
    event_date: Mapped[date] = mapped_column(Date, index=True)
    buy: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("strategy_version", "code", "event_date", name="uq_live_peer_event"),
        Index("ix_live_peer_version_date", "strategy_version", "event_date"),
    )


class LiveScanRun(Base):
    __tablename__ = "live_scan_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="running")
    a_count: Mapped[int] = mapped_column(Integer, default=0)
    b_count: Mapped[int] = mapped_column(Integer, default=0)
    c_count: Mapped[int] = mapped_column(Integer, default=0)
    combined_count: Mapped[int] = mapped_column(Integer, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
