from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import BootstrapStock, DailyBar, Stock
import app.services.data_update as data_update


ROOT = Path(__file__).resolve().parents[1]


def _bar(code: str, d: date, px: float = 10.0):
    return DailyBar(
        code=code,
        trade_date=d,
        open=px,
        high=px,
        low=px,
        close=px,
        volume=1_000_000,
        amount=10_000_000,
        turnover=0.02,
    )


def test_daily_cron_never_runs_full_history_scan():
    s = (ROOT / "scripts/run_daily.py").read_text(encoding="utf-8")
    assert "run_smart_maintenance" in s
    assert "run_full_scan" not in s


def test_smart_update_uses_formal_history_gate_not_9999():
    s = (ROOT / "app/services/maintenance_service.py").read_text(encoding="utf-8")
    assert 'if not bool(stats.get("coverage_ok")):' in s
    assert "coverage < 0.9999" not in s
    assert "previous_strategy_rows" in s


def test_cross_day_snapshot_guard_is_still_present():
    s = (ROOT / "app/services/data_update.py").read_text(encoding="utf-8")
    assert "禁止跨日写入快照" in s
    assert "previous_trade_date" in s
    assert "previous_verified_coverage" in s


def test_scan_readiness_reports_previous_day_progress(monkeypatch):
    class FakeProvider:
        def latest_completed_trade_date(self):
            return date(2026, 8, 26)

        def trade_dates(self, start, end):
            days = [
                date(2026, 8, 24),
                date(2026, 8, 25),
                date(2026, 8, 26),
            ]
            return [d for d in days if start <= d <= end]

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        db.add_all([
            Stock(code="000001", name="A", market="SZ", board="主板", is_st=False),
            Stock(code="000002", name="B", market="SZ", board="主板", is_st=False),
            BootstrapStock(code="000001", status="ok", row_count=300, message=""),
            BootstrapStock(code="000002", status="ok", row_count=300, message=""),
            _bar("000001", date(2026, 8, 24)),
            _bar("000002", date(2026, 8, 24)),
            _bar("000001", date(2026, 8, 25)),
        ])
        db.commit()

        monkeypatch.setattr(data_update, "get_provider", lambda: FakeProvider())
        ready = data_update.scan_readiness(db, check_calendar=True)

        assert ready["stale"] is True
        assert ready["previous_trade_date"] == date(2026, 8, 25)
        assert ready["previous_strategy_rows"] == 1
        assert abs(ready["previous_verified_coverage"] - 0.5) < 1e-12
