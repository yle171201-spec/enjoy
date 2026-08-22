from __future__ import annotations

from datetime import date, timedelta, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing as mp
import pandas as pd
from sqlalchemy import select, func, distinct, or_

from ..config import settings
from ..models import DataUpdateRun, Stock, DailyBar, BootstrapStock
from ..providers import get_provider
from ..providers.public_provider import DailySnapshotNotReady
from .repository import upsert_stocks, upsert_bars, upsert_snapshot, latest_trade_date


def _new_run(db, provider: str, start: date | None, end: date | None) -> DataUpdateRun:
    run = DataUpdateRun(provider=provider, start_date=start, end_date=end, status="running")
    db.add(run); db.commit(); db.refresh(run)
    return run


def _bootstrap_coverage(db) -> dict:
    total_nonst = db.execute(
        select(func.count(Stock.code)).where(Stock.is_st.is_(False))
    ).scalar_one()
    tracked = db.execute(select(func.count(BootstrapStock.code))).scalar_one()
    done = db.execute(
        select(func.count(BootstrapStock.code)).where(BootstrapStock.status == "ok")
    ).scalar_one()
    errors = db.execute(
        select(func.count(BootstrapStock.code)).where(BootstrapStock.status == "error")
    ).scalar_one()
    coverage = (float(done) / float(total_nonst)) if total_nonst else 0.0
    return {
        "active_nonst": int(total_nonst or 0),
        "bootstrap_tracked": int(tracked or 0),
        "bootstrap_done": int(done or 0),
        "bootstrap_errors": int(errors or 0),
        "bootstrap_coverage": coverage,
    }


def scan_readiness(db, check_calendar: bool = True) -> dict:
    # Historical coverage and latest-day cross-sectional coverage are separate gates.
    provider = get_provider()
    latest = latest_trade_date(db)
    cov = _bootstrap_coverage(db)

    imported_ready_codes = 0
    if cov["bootstrap_tracked"] == 0:
        sub = (
            select(DailyBar.code)
            .group_by(DailyBar.code)
            .having(func.count(DailyBar.id) >= settings.min_scan_history_bars)
            .subquery()
        )
        imported_ready_codes = int(
            db.execute(select(func.count()).select_from(sub)).scalar_one() or 0
        )

    coverage_ok = (
        cov["bootstrap_coverage"] >= settings.min_scan_bootstrap_coverage
        if cov["bootstrap_tracked"] > 0
        else imported_ready_codes >= settings.min_scan_stocks
    )

    expected_latest = None
    stale = True
    gaps: list[date] = []
    calendar_error = ""

    if latest is not None and check_calendar:
        try:
            expected_latest = provider.latest_completed_trade_date()
            stale = latest < expected_latest
            start = max(
                expected_latest - timedelta(days=settings.calendar_gap_check_days),
                date.fromisoformat(settings.bootstrap_start_date),
            )
            expected = set(provider.trade_dates(start, expected_latest))
            stored = set(db.execute(
                select(distinct(DailyBar.trade_date)).where(
                    DailyBar.trade_date >= start,
                    DailyBar.trade_date <= expected_latest,
                )
            ).scalars().all())
            gaps = sorted(expected - stored)
        except Exception as e:
            calendar_error = str(e)
            stale = True
    elif latest is not None:
        expected_latest = latest
        stale = False

    target_day = expected_latest or latest
    latest_strategy_rows = 0
    latest_bar_coverage = 0.0
    if target_day is not None and cov["active_nonst"] > 0:
        latest_strategy_rows = int(db.execute(
            select(func.count(distinct(DailyBar.code)))
            .join(Stock, Stock.code == DailyBar.code)
            .where(
                DailyBar.trade_date == target_day,
                Stock.is_st.is_(False),
            )
        ).scalar_one() or 0)
        latest_bar_coverage = (
            float(latest_strategy_rows) / float(cov["active_nonst"])
        )

    latest_coverage_ok = (
        latest_bar_coverage >= settings.min_latest_bar_coverage
        if target_day is not None and cov["active_nonst"] > 0
        else False
    )

    ready = bool(
        latest
        and coverage_ok
        and latest_coverage_ok
        and not stale
        and not gaps
        and not calendar_error
    )

    reasons = []
    if not latest:
        reasons.append("数据库尚无日线")
    if not coverage_ok:
        if cov["bootstrap_tracked"] > 0:
            reasons.append(
                f"历史池覆盖率 {cov['bootstrap_coverage']:.1%} < "
                f"{settings.min_scan_bootstrap_coverage:.0%}"
            )
        else:
            reasons.append(
                f"具备≥{settings.min_scan_history_bars}根日线的股票仅 "
                f"{imported_ready_codes} < {settings.min_scan_stocks}"
            )
    if stale and latest and expected_latest:
        reasons.append(f"数据库最大日线 {latest} 落后于应完成交易日 {expected_latest}")
    if target_day is not None and not latest_coverage_ok:
        reasons.append(
            f"{target_day} 策略股日线覆盖 "
            f"{latest_strategy_rows}/{cov['active_nonst']}="
            f"{latest_bar_coverage:.1%} < {settings.min_latest_bar_coverage:.0%}"
        )
    if gaps:
        sample = ", ".join(map(str, gaps[:5]))
        reasons.append(f"近期开市日存在 {len(gaps)} 个全局日期缺口：{sample}")
    if calendar_error:
        reasons.append(f"交易日历核验失败：{calendar_error}")

    return {
        **cov,
        "strategy_pool": cov["active_nonst"],
        "latest": latest,
        "expected_latest": expected_latest,
        "latest_target": target_day,
        "latest_strategy_rows": latest_strategy_rows,
        "latest_bar_coverage": latest_bar_coverage,
        "latest_coverage_ok": latest_coverage_ok,
        "latest_coverage_threshold": settings.min_latest_bar_coverage,
        "imported_ready_codes": imported_ready_codes,
        "coverage_ok": coverage_ok,
        "stale": stale,
        "gap_count": len(gaps),
        "gap_sample": gaps[:5],
        "scan_ready": ready,
        "scan_block_reason": "；".join(reasons) if reasons else "READY",
    }


def data_stats(db) -> dict:
    stocks = int(db.execute(select(func.count(Stock.code))).scalar_one() or 0)
    bars = int(db.execute(select(func.count(DailyBar.id))).scalar_one() or 0)
    earliest = db.execute(select(func.min(DailyBar.trade_date))).scalar_one_or_none()
    latest = db.execute(select(func.max(DailyBar.trade_date))).scalar_one_or_none()

    ready = scan_readiness(db, check_calendar=True)
    strategy_pool = int(ready.get("strategy_pool") or 0)

    return {
        "stocks": stocks,
        "bars": bars,
        "earliest": earliest,
        "latest": latest,
        "strategy_pool": strategy_pool,
        "excluded_metadata": max(0, stocks - strategy_pool),
        "latest_rows": int(ready.get("latest_strategy_rows") or 0),
        **ready,
    }


def recover_interrupted_runs(db) -> int:
    # Mark stale running update rows left by a previous web process as interrupted.
    runs = db.execute(
        select(DataUpdateRun).where(DataUpdateRun.status == "running")
    ).scalars().all()
    if not runs:
        return 0

    for run in runs:
        old = (run.message or "").strip()
        note = "服务重启后检测到未完成任务；已标记 interrupted，可安全续传。"
        run.message = f"{old}\n{note}".strip()
        run.status = "interrupted"
        run.finished_at = datetime.utcnow()
    db.commit()
    return len(runs)


def _history_child(send_conn, code: str, start: date, end: date) -> None:
    # Fetch one stock in an isolated child process.
    try:
        provider = get_provider()
        frame = provider.history(code, start, end)
        send_conn.send(("ok", frame, ""))
    except Exception as e:
        try:
            send_conn.send(("error", None, f"{type(e).__name__}: {e}"))
        except Exception:
            pass
    finally:
        try:
            send_conn.close()
        except Exception:
            pass


def _history_with_timeout(code: str, start: date, end: date, timeout_seconds: int):
    # Use spawn because FastAPI sync background tasks run from a threadpool.
    timeout_seconds = max(10, int(timeout_seconds))
    ctx = mp.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_history_child,
        args=(send_conn, code, start, end),
        daemon=True,
    )
    proc.start()
    send_conn.close()

    try:
        if not recv_conn.poll(timeout_seconds):
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join(2)
            raise TimeoutError(f"单股历史抓取超过 {timeout_seconds}s")

        try:
            status, frame, message = recv_conn.recv()
        except EOFError:
            proc.join(1)
            raise RuntimeError(f"历史抓取子进程异常退出 exitcode={proc.exitcode}")

        proc.join(5)
        if proc.is_alive():
            proc.terminate()
            proc.join(2)

        if status != "ok":
            raise RuntimeError(message or "历史抓取失败")
        return frame
    finally:
        try:
            recv_conn.close()
        except Exception:
            pass
        if proc.is_alive():
            proc.terminate()
            proc.join(2)


def _active_bootstrap(db):
    return db.execute(
        select(DataUpdateRun)
        .where(
            DataUpdateRun.status == "running",
            DataUpdateRun.provider.like("%-bootstrap%"),
        )
        .order_by(DataUpdateRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def bootstrap_batch(db, start=None, end=None, limit=None, retry_errors: bool = False):
    # Backfill without letting one bad stock block the normal queue.
    active = _active_bootstrap(db)
    if active is not None:
        return []

    provider = get_provider()
    start = start or date.fromisoformat(settings.bootstrap_start_date)
    end = end or provider.latest_completed_trade_date()
    limit = int(limit or settings.bootstrap_batch_size)
    timeout_seconds = max(10, int(settings.bootstrap_stock_timeout_seconds))
    pname = f"{getattr(provider, 'name', 'provider')}-bootstrap"
    if retry_errors:
        pname += "-retry"
    run = _new_run(db, pname, start, end)

    try:
        stocks = provider.stock_list()
        upsert_stocks(db, stocks)
        rows = stocks[~stocks.is_st].copy()
        rows["code"] = rows.code.astype(str).str.zfill(6)

        if retry_errors:
            error_codes = set(db.execute(
                select(BootstrapStock.code).where(BootstrapStock.status == "error")
            ).scalars().all())
            rows = rows[rows.code.isin(error_codes)].head(limit)
        else:
            quarantined = set(db.execute(
                select(BootstrapStock.code).where(
                    BootstrapStock.status.in_(("ok", "error"))
                )
            ).scalars().all())
            rows = rows[~rows.code.isin(quarantined)].head(limit)

        run.stock_count = len(rows)
        run.success_count = 0
        run.failed_count = 0
        db.commit()

        if rows.empty:
            run.status = "complete"
            run.message = (
                "当前没有待初始化股票"
                if not retry_errors
                else "当前没有需要重试的错误股票"
            )
            run.finished_at = datetime.utcnow()
            db.commit()
            return []

        results = []
        errors = []
        total = len(rows)

        for idx, r in enumerate(rows.itertuples(index=False), start=1):
            code = str(r.code).zfill(6)
            run.message = (
                f"进度 {idx - 1}/{total}；当前 {code}；"
                f"单股硬超时 {timeout_seconds}s"
            )
            db.commit()

            state = db.get(BootstrapStock, code) or BootstrapStock(code=code)
            if state not in db:
                db.add(state)

            try:
                frame = _history_with_timeout(code, start, end, timeout_seconds)
                n = upsert_bars(db, code, frame)
                if n <= 0:
                    raise RuntimeError("历史接口返回0条有效日线")

                state.status = "ok"
                state.row_count = n
                state.message = ""
                results.append((code, n))
                run.success_count += 1
                run.message = (
                    f"进度 {idx}/{total}；刚完成 {code}；"
                    f"成功 {run.success_count}；失败 {run.failed_count}"
                )
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"[:1000]
                state.status = "error"
                state.message = msg
                errors.append(f"{code}: {msg}")
                run.failed_count += 1
                run.message = (
                    f"进度 {idx}/{total}；{code} 已隔离到错误队列；"
                    f"成功 {run.success_count}；失败 {run.failed_count}；{msg[:240]}"
                )

            state.updated_at = datetime.utcnow()
            db.commit()

        run.status = "ok" if not errors else "partial"
        summary = (
            f"批次完成：成功 {run.success_count}/{total}，失败 {run.failed_count}。"
            "失败股票已隔离，不会阻塞下一批。"
        )
        if errors:
            summary += "\n" + "\n".join(errors[:50])
        run.message = summary
        run.finished_at = datetime.utcnow()
        db.commit()
        return results
    except Exception as e:
        run.status = "error"
        run.message = f"{type(e).__name__}: {e}"
        run.finished_at = datetime.utcnow()
        db.commit()
        raise


def repair_latest_gaps(db, limit=None, retry_errors: bool = False):
    """Fast resumable latest-day repair without re-processing no-trade stocks.

    V2.1.5 selected rows only by last_date < target. Suspended stocks returned 0
    rows, so their last_date never advanced and they reoccupied the next batch.
    V2.1.6 records a target-date marker in BootstrapStock.message so every code is
    attempted once per target date; errors are quarantined for explicit retry.
    """
    active = db.execute(select(func.count(DataUpdateRun.id)).where(DataUpdateRun.status == "running")).scalar_one()
    if active:
        return []

    provider = get_provider()
    target = provider.latest_completed_trade_date()
    limit = int(limit or settings.gap_repair_batch_size)
    workers = max(1, min(int(settings.gap_repair_workers), 8))
    ok_prefix = f"gap_checked:{target}:"
    err_prefix = f"gap_checked:{target}:error:"
    pname = f"{getattr(provider, 'name', 'provider')}-gap-repair" + ("-retry" if retry_errors else "")
    run = _new_run(db, pname, None, target)

    latest_by_code = (
        select(DailyBar.code.label("code"), func.max(DailyBar.trade_date).label("last_date"))
        .group_by(DailyBar.code).subquery()
    )
    try:
        q = (
            select(Stock.code, latest_by_code.c.last_date)
            .join(BootstrapStock, BootstrapStock.code == Stock.code)
            .outerjoin(latest_by_code, latest_by_code.c.code == Stock.code)
            .where(Stock.is_st.is_(False))
        )
        if retry_errors:
            q = q.where(BootstrapStock.message.like(err_prefix + "%"))
        else:
            q = q.where(
                (latest_by_code.c.last_date.is_(None) | (latest_by_code.c.last_date < target)),
                or_(BootstrapStock.message == "", ~BootstrapStock.message.like(ok_prefix + "%")),
            )
        rows = db.execute(q.order_by(Stock.code).limit(limit)).all()
        run.stock_count = len(rows); run.success_count = 0; run.failed_count = 0
        run.message = f"极速缺口修复准备：{len(rows)}只；目标{target}；并发{workers}"
        db.commit()
        if not rows:
            run.status = "complete"; run.message = "当前没有新的待修复股票" if not retry_errors else "当前没有待重试的修复错误"
            run.finished_at = datetime.utcnow(); db.commit(); return []

        def one(code, last_date):
            code = str(code).zfill(6)
            start = last_date + timedelta(days=1) if last_date else date.fromisoformat(settings.bootstrap_start_date)
            frame = provider.repair_history(code, start, target) if hasattr(provider, "repair_history") else provider.history(code, start, target)
            return code, start, frame

        total = len(rows); done = 0; inserted = 0; no_trade = 0; errors = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(one, code, last_date): str(code).zfill(6) for code,last_date in rows}
            for fut in as_completed(futs):
                code = futs[fut]
                state = db.get(BootstrapStock, code)
                try:
                    _, start, frame = fut.result()
                    n = upsert_bars(db, code, frame)
                    inserted += int(n or 0); run.success_count += 1
                    if n > 0:
                        state.message = ""
                    else:
                        no_trade += 1
                        state.message = f"{ok_prefix}no_trade"
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"[:700]
                    run.failed_count += 1; errors.append(f"{code}: {msg}")
                    state.message = err_prefix + msg
                state.updated_at = datetime.utcnow(); done += 1
                run.message = f"极速缺口修复 {done}/{total}；刚处理 {code}；成功 {run.success_count}；失败 {run.failed_count}；补入 {inserted} 行；无交易 {no_trade}"
                db.commit()

        run.status = "ok" if not errors else "partial"
        run.message = f"极速修复完成：处理{total}只；成功{run.success_count}；失败{run.failed_count}；补入{inserted}行；无交易/停牌{no_trade}。"
        if errors: run.message += "\n错误已隔离，可单独重试：\n" + "\n".join(errors[:30])
        run.finished_at = datetime.utcnow(); db.commit(); return []
    except Exception as e:
        run.status = "error"; run.message = f"{type(e).__name__}: {e}"; run.finished_at = datetime.utcnow(); db.commit(); raise


def sync_daily_public(db, now: datetime | None = None):
    """Safe one-shot EOD update.

    A live spot snapshot is accepted only on the current A-share trading day and
    only after 16:10 Asia/Shanghai. Before close, weekends and holidays are a safe
    no-op: no OHLCV row is written and no historical date is relabeled.
    """
    provider = get_provider()
    pname = f"{getattr(provider, 'name', 'provider')}-snapshot"

    try:
        if hasattr(provider, "snapshot_trade_date"):
            target = provider.snapshot_trade_date(now)
        else:
            target = provider.latest_completed_trade_date(now)
    except DailySnapshotNotReady as e:
        run = _new_run(db, pname, None, None)
        run.status = "skipped"
        run.message = str(e)
        run.finished_at = datetime.utcnow(); db.commit()
        return {"status": "skipped", "target": None, "rows": 0, "message": str(e)}

    before = latest_trade_date(db)
    run = _new_run(db, pname, target, target)
    try:
        if getattr(provider, "name", "") == "public":
            snap = provider.daily_snapshot(target, now=now)
        else:
            snap = provider.daily_snapshot(target)

        meta = snap[["code", "name", "market", "board", "is_st"]].drop_duplicates("code")
        upsert_stocks(db, meta)
        valid = snap[~snap.is_st].copy()
        n = upsert_snapshot(db, valid)
        run.stock_count = len(valid)
        run.success_count = n
        run.failed_count = max(0, len(valid) - n)
        snapshot_coverage = (float(n) / float(len(valid))) if len(valid) else 0.0
        run.status = "ok" if snapshot_coverage >= settings.min_latest_bar_coverage else "partial"

        note = f"EOD snapshot {target}: {n}/{len(valid)} rows ({snapshot_coverage:.1%})"
        if before:
            try:
                missed = provider.trade_dates(before + timedelta(days=1), target)
                if len(missed) > 1:
                    note += f"; WARNING: DB可能缺失{len(missed)-1}个中间交易日，正式扫描会被完整性门阻止"
            except Exception as e:
                note += f"; calendar-check warning: {e}"
        run.message = note
        run.finished_at = datetime.utcnow(); db.commit()
        return {"status": run.status, "target": target, "rows": n, "message": note}
    except DailySnapshotNotReady as e:
        run.status = "skipped"; run.message = str(e); run.finished_at = datetime.utcnow(); db.commit()
        return {"status": "skipped", "target": None, "rows": 0, "message": str(e)}
    except Exception as e:
        run.status = "error"; run.message = str(e); run.finished_at = datetime.utcnow(); db.commit()
        raise


def sync_market(db, start=None, end=None, workers=None, limit=None):
    """Per-stock historical repair/update path."""
    provider = get_provider()
    end = end or provider.latest_completed_trade_date()
    latest = latest_trade_date(db)
    if start is None:
        start = (latest - timedelta(days=7)) if latest else date.fromisoformat(settings.bootstrap_start_date)

    run = _new_run(db, getattr(provider, "name", settings.data_provider), start, end)
    errors = []
    results = []
    try:
        stocks = provider.stock_list()
        upsert_stocks(db, stocks)
        rows = stocks[~stocks.is_st].copy()
        rows = rows.head(limit) if limit else rows
        run.stock_count = len(rows); db.commit()

        if workers is None:
            workers = getattr(provider, "max_workers", 1)

        def one(r):
            code = str(r.code).zfill(6)
            return code, provider.history(code, start, end)

        with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = {ex.submit(one, r): str(r.code).zfill(6) for r in rows.itertuples(index=False)}
            for fut in as_completed(futs):
                code = futs[fut]
                try:
                    c, frame = fut.result()
                    results.append((c, upsert_bars(db, c, frame)))
                except Exception as e:
                    errors.append(f"{code}: {e}")

        run.success_count = len(results)
        run.failed_count = len(errors)
        run.status = "ok" if not errors else "partial"
        run.message = "\n".join(errors[:100])
        run.finished_at = datetime.utcnow(); db.commit()
        return results
    except Exception as e:
        run.status = "error"; run.message = str(e); run.finished_at = datetime.utcnow(); db.commit()
        raise
