from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd

from ..engine.strategy_reference_v18 import run_close_reference, compare_to_golden
from ..models import ScanRun, DailyBar
from .repository import load_all_frames, replace_signals, latest_trade_date
from .data_update import scan_readiness
from ..config import settings
from sqlalchemy import select, func

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "ABC_V18_历史信号载荷.csv"


def _attach_exit_dates(sig: pd.DataFrame, frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    if sig.empty or "exit_idx" not in sig.columns:
        return sig
    z = sig.copy()
    out = []
    for r in z.itertuples(index=False):
        code = str(r.code).zfill(6)
        frame = frames.get(code)
        ex = getattr(r, "exit_idx", None)
        if frame is None or ex is None or pd.isna(ex):
            out.append(pd.NaT)
            continue
        i = int(ex)
        if i < 0 or i >= len(frame):
            out.append(pd.NaT)
        else:
            out.append(pd.Timestamp(frame["date"].iloc[i]))
    z["exit_date"] = out
    return z


def run_full_scan(db, force: bool = False):
    run = ScanRun(status="running", data_date=latest_trade_date(db))
    db.add(run); db.commit(); db.refresh(run)
    try:
        if not force:
            ready = scan_readiness(db, check_calendar=True)
            if not ready["scan_ready"]:
                run.status = "blocked"
                run.message = "数据完整性门：" + ready["scan_block_reason"]
                run.finished_at = datetime.utcnow()
                db.commit()
                return pd.DataFrame(), {}, {"blocked": True, "reason": ready["scan_block_reason"]}
        latest = latest_trade_date(db)
        cutoff = latest - timedelta(days=settings.live_scan_calendar_days) if latest else None
        frames = load_all_frames(db, start_date=cutoff)
        if not frames:
            raise RuntimeError("数据库没有日线数据，请先更新/导入数据")
        sig, diag = run_close_reference(frames, run_exits=True)
        sig = _attach_exit_dates(sig, frames)
        replace_signals(db, sig, "V18")
        earliest_db = db.execute(select(func.min(DailyBar.trade_date))).scalar_one_or_none()
        # Golden spans the historical research period; a live-only rolling database must not
        # be mislabeled as a failed reproduction.
        can_validate_golden = bool(earliest_db and earliest_db <= datetime(2022, 1, 5).date())
        cmp = compare_to_golden(sig, str(GOLDEN)) if GOLDEN.exists() and can_validate_golden else {}
        run.a_count = int((sig.engine == "A").sum())
        run.b_count = int((sig.engine == "B").sum())
        run.c_count = int((sig.engine == "C").sum())
        run.combined_count = len(sig)
        run.golden_matched = cmp.get("matched_n")
        run.golden_missing = cmp.get("missing_n")
        run.golden_extra = cmp.get("extra_n")
        run.status = "ok"
        run.message = str(diag)
        run.finished_at = datetime.utcnow()
        db.commit()
        return sig, cmp, diag
    except Exception as e:
        run.status = "error"
        run.message = str(e)
        run.finished_at = datetime.utcnow()
        db.commit()
        raise
