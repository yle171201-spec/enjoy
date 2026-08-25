
from datetime import date
from app.models import LivePosition
from app.services.position_service import _timing, OFFICIAL_MAX_POSITIONS

def test_official_k6():
    assert OFFICIAL_MAX_POSITIONS == 6

def test_tail_model():
    p=LivePosition(signal_id=1,code="600000",engine="A",signal_date=date(2026,1,1),
        entry_date=date(2026,1,2),entry_price=10,initial_shares=1000,shares=150,
        fail_price=9,stage="TAIL",status="OPEN")
    assert p.stage=="TAIL" and p.shares==150

def test_timing():
    assert _timing(date(2026,1,2),date(2026,1,5),date(2026,1,3)).startswith("已触发")
