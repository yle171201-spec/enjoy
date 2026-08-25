from datetime import date

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.db import Base
from app.models import DailyBar
from app.services.repository import latest_prices
from app.services.maintenance_service import smart_progress_snapshot


def test_latest_prices_is_single_select():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([
            DailyBar(
                code="000001", trade_date=date(2026, 1, 1),
                open=10, high=10, low=10, close=10,
                volume=1, amount=1, turnover=.01,
            ),
            DailyBar(
                code="000001", trade_date=date(2026, 1, 2),
                open=11, high=11, low=11, close=11,
                volume=1, amount=1, turnover=.01,
            ),
            DailyBar(
                code="000002", trade_date=date(2026, 1, 2),
                open=20, high=20, low=20, close=20,
                volume=1, amount=1, turnover=.01,
            ),
        ])
        db.commit()

        statements = []

        def before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        try:
            out = latest_prices(db, ["000001", "000002"])
        finally:
            event.remove(
                engine, "before_cursor_execute", before_cursor_execute
            )

        selects = [
            s for s in statements
            if str(s).lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) == 1
        assert out["000001"][1] == 11.0
        assert out["000002"][1] == 20.0


def test_smart_progress_shape():
    p = smart_progress_snapshot()
    for key in ("active", "stage", "percent", "detail", "round", "status"):
        assert key in p
