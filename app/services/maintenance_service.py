from __future__ import annotations

from threading import Lock

from sqlalchemy import select, text

from ..config import settings
from ..db import SessionLocal
from ..models import LiveScanRun
from .data_update import (
    data_stats,
    bootstrap_batch,
    sync_daily_public,
    scan_readiness,
    repair_latest_gaps,
    audit_latest_day,
)
from .live_scan import run_live_scan


_RUN_LOCK = Lock()
_STATE_LOCK = Lock()
_STATE = {
    "active": False,
    "stage": "",
    "percent": 0.0,
    "detail": "",
    "round": 0,
    "status": "idle",
}


def _set(**kwargs):
    with _STATE_LOCK:
        _STATE.update(kwargs)


def smart_progress_snapshot() -> dict:
    with _STATE_LOCK:
        return dict(_STATE)


def smart_active() -> bool:
    return bool(smart_progress_snapshot().get("active"))


def ensure_runtime_indexes(db) -> None:
    statements = (
        "CREATE INDEX IF NOT EXISTS ix_signal_version_date_engine_code "
        "ON signals (strategy_version, signal_date, engine, code)",
        "CREATE INDEX IF NOT EXISTS ix_bootstrap_status "
        "ON bootstrap_stocks (status)",
        "CREATE INDEX IF NOT EXISTS ix_data_update_status "
        "ON data_update_runs (status)",
        "CREATE INDEX IF NOT EXISTS ix_scan_run_status "
        "ON scan_runs (status)",
        "CREATE INDEX IF NOT EXISTS ix_live_scan_run_status "
        "ON live_scan_runs (status)",
    )
    for sql in statements:
        db.execute(text(sql))
    db.commit()


def run_smart_maintenance() -> None:
    if not _RUN_LOCK.acquire(blocking=False):
        return

    db = SessionLocal()
    try:
        _set(
            active=True,
            stage="准备",
            percent=1.0,
            detail="正在检查生产数据状态",
            round=0,
            status="running",
        )
        last_signature = None
        stalled = 0

        for round_no in range(1, 81):
            _set(round=round_no)
            stats = data_stats(db)

            signature = (
                int(stats.get("bootstrap_done") or 0),
                int(stats.get("bootstrap_errors") or 0),
                str(stats.get("latest") or ""),
                int(stats.get("latest_strategy_rows") or 0),
                int(stats.get("latest_suspended") or 0),
                int(stats.get("latest_unresolved") or 0),
                int(stats.get("gap_count") or 0),
                bool(stats.get("scan_ready")),
            )
            stalled = stalled + 1 if signature == last_signature else 0
            last_signature = signature

            pool = max(1, int(stats.get("strategy_pool") or 1))
            coverage = float(stats.get("bootstrap_coverage") or 0.0)

            if coverage < 0.9999:
                _set(
                    stage="历史初始化",
                    percent=min(54.0, 4.0 + 50.0 * coverage),
                    detail=(
                        f"已完成 {int(stats.get('bootstrap_done') or 0)}/{pool}；"
                        "系统自动续下一批，不需要再点500"
                    ),
                )
                retry = bool(
                    int(stats.get("bootstrap_errors") or 0) > 0
                    and int(stats.get("bootstrap_done") or 0)
                    + int(stats.get("bootstrap_errors") or 0) >= pool
                )
                bootstrap_batch(
                    db,
                    limit=max(100, int(settings.bootstrap_batch_size)),
                    retry_errors=retry,
                )
                db.expire_all()
                if stalled >= 4:
                    break
                continue

            ready = scan_readiness(db, check_calendar=True)
            latest = ready.get("latest")

            if ready.get("scan_ready"):
                done = (
                    db.execute(
                        select(LiveScanRun)
                        .where(
                            LiveScanRun.status == "ok",
                            LiveScanRun.data_date == latest,
                        )
                        .order_by(LiveScanRun.id.desc())
                        .limit(1)
                    ).scalar_one_or_none()
                    if latest
                    else None
                )
                if latest and done is None:
                    _set(
                        stage="最新日策略扫描",
                        percent=96.0,
                        detail=f"数据已READY；正在扫描 {latest} 的正式买卖信号",
                    )
                    run_live_scan(db)
                _set(
                    active=False,
                    stage="完成",
                    percent=100.0,
                    detail="数据与最新日扫描均已完成",
                    status="ok",
                )
                return

            if ready.get("stale"):
                _set(
                    stage="更新最新交易日",
                    percent=56.0,
                    detail=ready.get("scan_block_reason") or "尝试安全盘后快照",
                )
                try:
                    sync_daily_public(db)
                except Exception as e:
                    _set(
                        detail=(
                            "最新快照暂不可用，转入历史补齐："
                            f"{type(e).__name__}"
                        )
                    )
                db.expire_all()
                ready = scan_readiness(db, check_calendar=True)

            if (
                ready.get("stale")
                or int(ready.get("gap_count") or 0) > 0
                or not ready.get("latest_coverage_ok")
            ):
                have = int(ready.get("latest_strategy_rows") or 0)
                total = max(1, int(ready.get("latest_tradable_pool") or pool))
                ratio = min(1.0, have / total)
                _set(
                    stage="自动补齐历史缺口",
                    percent=58.0 + 24.0 * ratio,
                    detail=(
                        f"目标日线覆盖 {have}/{total}；内部按"
                        f"{settings.gap_repair_batch_size}只安全分批，自动续跑"
                    ),
                )
                repair_latest_gaps(
                    db,
                    limit=max(100, int(settings.gap_repair_batch_size)),
                    retry_errors=False,
                )
                db.expire_all()

                if stalled >= 3:
                    repair_latest_gaps(
                        db,
                        limit=200,
                        retry_errors=True,
                    )
                    db.expire_all()

                if stalled >= 5:
                    break
                continue

            if (
                not ready.get("latest_verified_ok")
                or int(ready.get("latest_unresolved") or 0) > 0
            ):
                verified = float(
                    ready.get("latest_verified_coverage") or 0.0
                )
                _set(
                    stage="自动核验停牌/异常",
                    percent=82.0 + 12.0 * min(1.0, verified),
                    detail=(
                        f"已核验 {verified:.1%}；仍未核清 "
                        f"{int(ready.get('latest_unresolved') or 0)} 只"
                    ),
                )
                audit_latest_day(
                    db,
                    limit=max(
                        200,
                        int(settings.latest_audit_batch_size),
                    ),
                    retry_errors=False,
                )
                db.expire_all()

                if stalled >= 3:
                    audit_latest_day(
                        db,
                        limit=200,
                        retry_errors=True,
                    )
                    db.expire_all()

                if stalled >= 5:
                    break
                continue

            break

        final = scan_readiness(db, check_calendar=True)
        if final.get("scan_ready"):
            latest = final.get("latest")
            _set(
                stage="最新日策略扫描",
                percent=96.0,
                detail=f"数据已READY；正在扫描 {latest}",
            )
            run_live_scan(db)
            _set(
                active=False,
                stage="完成",
                percent=100.0,
                detail="数据与扫描均已完成",
                status="ok",
            )
        else:
            _set(
                active=False,
                stage="需要人工检查",
                percent=max(
                    float(smart_progress_snapshot().get("percent") or 0),
                    1.0,
                ),
                detail=(
                    final.get("scan_block_reason")
                    or "自动流程没有继续取得进展，请展开高级诊断"
                ),
                status="blocked",
            )
    except Exception as e:
        _set(
            active=False,
            stage="自动流程异常",
            detail=f"{type(e).__name__}: {e}",
            status="error",
        )
    finally:
        db.close()
        _RUN_LOCK.release()
