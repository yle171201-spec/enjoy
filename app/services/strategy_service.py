from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
import json
import pandas as pd

from ..engine.strategy_reference_v18 import run_close_reference, compare_to_golden
from ..models import ScanRun, DailyBar
from .repository import load_all_frames, replace_signals, latest_trade_date
from .data_update import scan_readiness
from ..config import settings
from sqlalchemy import select, func

GOLDEN = Path(__file__).resolve().parents[2] / "golden" / "ABC_V18_历史信号载荷.csv"
_PROGRESS_PREFIX = "__SCAN_PROGRESS__:"


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


def _set_progress(db, run: ScanRun, percent: float, stage: str, detail: str = "") -> None:
    payload = {
        "percent": round(float(max(0.0, min(100.0, percent))), 1),
        "stage": str(stage),
        "detail": str(detail),
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    run.message = _PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False)
    db.commit()


def scan_progress_payload(run: ScanRun | None) -> dict:
    if run is None:
        return {
            "exists": False,
            "status": "none",
            "percent": 0.0,
            "stage": "尚未扫描",
            "detail": "",
            "elapsed_seconds": 0,
        }

    now = datetime.utcnow()
    end = run.finished_at or now
    elapsed = max(0, int((end - run.started_at).total_seconds())) if run.started_at else 0

    progress = {}
    if run.status == "running" and (run.message or "").startswith(_PROGRESS_PREFIX):
        try:
            progress = json.loads((run.message or "")[len(_PROGRESS_PREFIX):])
        except Exception:
            progress = {}

    default_pct = 100.0 if run.status == "ok" else 0.0
    if run.status in {"error", "blocked", "interrupted"}:
        default_pct = float(progress.get("percent") or 0.0)

    return {
        "exists": True,
        "id": run.id,
        "status": run.status,
        "percent": float(progress.get("percent", default_pct)),
        "stage": progress.get("stage", "扫描完成" if run.status == "ok" else run.status),
        "detail": progress.get("detail", "" if run.status == "running" else (run.message or "")),
        "updated_at": progress.get("updated_at"),
        "started_at": run.started_at.isoformat(timespec="seconds") if run.started_at else None,
        "finished_at": run.finished_at.isoformat(timespec="seconds") if run.finished_at else None,
        "elapsed_seconds": elapsed,
        "data_date": str(run.data_date) if run.data_date else None,
        "a_count": int(run.a_count or 0),
        "b_count": int(run.b_count or 0),
        "c_count": int(run.c_count or 0),
        "combined_count": int(run.combined_count or 0),
        "golden_matched": run.golden_matched,
        "golden_missing": run.golden_missing,
        "golden_extra": run.golden_extra,
    }


def recover_interrupted_scans(db) -> int:
    runs = db.execute(select(ScanRun).where(ScanRun.status == "running")).scalars().all()
    if not runs:
        return 0

    for run in runs:
        progress = scan_progress_payload(run)
        stage = progress.get("stage") or "未知阶段"
        pct = progress.get("percent") or 0
        run.status = "interrupted"
        run.message = f"服务重启中断扫描；中断前阶段：{stage}（{pct:.1f}%）。可安全重新运行。"
        run.finished_at = datetime.utcnow()
    db.commit()
    return len(runs)


def run_full_scan(db, force: bool = False):
    active = db.execute(
        select(ScanRun).where(ScanRun.status == "running").order_by(ScanRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    if active is not None:
        return pd.DataFrame(), {}, {
            "blocked": True,
            "reason": f"已有扫描任务 #{active.id} 正在运行",
            "existing_run_id": active.id,
        }

    run = ScanRun(status="running", data_date=latest_trade_date(db))
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        _set_progress(db, run, 2, "完整性检查", "核验历史池、最新交易日、停牌与交易日历")

        if not force:
            ready = scan_readiness(db, check_calendar=True)
            if not ready["scan_ready"]:
                run.status = "blocked"
                run.message = "数据完整性门：" + ready["scan_block_reason"]
                run.finished_at = datetime.utcnow()
                db.commit()
                return pd.DataFrame(), {}, {"blocked": True, "reason": ready["scan_block_reason"]}

        latest = latest_trade_date(db)
        run.data_date = latest
        db.commit()

        cutoff = latest - timedelta(days=settings.live_scan_calendar_days) if latest else None
        _set_progress(db, run, 8, "加载全市场日线", f"读取 {cutoff or '-'} → {latest or '-'} 的策略历史窗口")
        frames = load_all_frames(db, start_date=cutoff)
        if not frames:
            raise RuntimeError("数据库没有日线数据，请先更新/导入数据")

        _set_progress(db, run, 22, "加载全市场日线", f"已载入 {len(frames)} 只股票，准备进入 V18")

        def engine_progress(stage: str, fraction: float, detail: str = "") -> None:
            overall = 24.0 + 64.0 * float(max(0.0, min(1.0, fraction)))
            _set_progress(db, run, overall, stage, detail)

        sig, diag = run_close_reference(frames, run_exits=True, progress_cb=engine_progress)

        _set_progress(db, run, 90, "整理信号", f"V18 返回 {len(sig)} 条历史事件")
        sig = _attach_exit_dates(sig, frames)

        _set_progress(db, run, 94, "写入信号", "替换数据库中的 V18 信号结果")
        replace_signals(db, sig, "V18")

        _set_progress(db, run, 97, "结果校验", "统计 A/B/C；检查是否具备 Golden 历史窗口")
        earliest_db = db.execute(select(func.min(DailyBar.trade_date))).scalar_one_or_none()
        can_validate_golden = bool(earliest_db and earliest_db <= datetime(2022, 1, 5).date())
        cmp = compare_to_golden(sig, str(GOLDEN)) if GOLDEN.exists() and can_validate_golden else {}

        run.a_count = int((sig.engine == "A").sum())
        run.b_count = int((sig.engine == "B").sum())
        run.c_count = int((sig.engine == "C").sum())
        run.combined_count = len(sig)
        run.golden_matched = cmp.get("matched_n")
        run.golden_missing = cmp.get("missing_n")
        run.golden_extra = cmp.get("extra_n")

        _set_progress(
            db, run, 99, "保存扫描结果",
            f"A={run.a_count} B={run.b_count} C={run.c_count} Combined={run.combined_count}",
        )

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
