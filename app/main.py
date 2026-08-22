from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import json

from fastapi import FastAPI, Request, Depends, Form, Query, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, func, distinct, text

from .config import settings
from .db import init_db, db_session, SessionLocal
from .models import Stock, DailyBar, Signal, ScanRun, DataUpdateRun, BootstrapStock, LatestDayAudit, LiveScanRun, SignalReview
from .auth import valid_password, make_cookie, is_logged_in, COOKIE
from .services.repository import latest_trade_date, latest_prices
from .services.strategy_service import (
    run_full_scan, scan_progress_payload, recover_interrupted_scans,
)
from .services.live_scan import (
    run_live_scan, live_progress_payload, live_state_status,
    recover_interrupted_live_scans,
)
from .services.backtest import close_vs_next_open, portfolio_backtest
from .services.chart_service import build_stock_chart
from .services.review_service import (
    review_index_data, review_case_data, save_signal_review, build_review_chart,
)
from .services.data_update import (
    data_stats, bootstrap_batch, sync_daily_public, scan_readiness,
    recover_interrupted_runs, repair_latest_gaps, audit_latest_day,
)

BASE = Path(__file__).resolve().parent
app = FastAPI(title=settings.app_name, version=settings.web_version)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _bg_bootstrap(limit: int, retry_errors: bool = False):
    db = SessionLocal()
    try:
        bootstrap_batch(db, limit=limit, retry_errors=retry_errors)
    finally:
        db.close()


def _bg_gap_repair(limit: int, retry_errors: bool = False):
    db = SessionLocal()
    try:
        repair_latest_gaps(db, limit=limit, retry_errors=retry_errors)
    finally:
        db.close()


def _bg_latest_audit(limit: int, retry_errors: bool = False):
    db = SessionLocal()
    try:
        audit_latest_day(db, limit=limit, retry_errors=retry_errors)
    finally:
        db.close()


def _bg_smart_update():
    db = SessionLocal()
    try:
        stats = data_stats(db)

        if float(stats.get("bootstrap_coverage") or 0.0) < 0.9999:
            retry_errors = bool(
                int(stats.get("bootstrap_errors") or 0) > 0
                and int(stats.get("bootstrap_done") or 0)
                + int(stats.get("bootstrap_errors") or 0)
                >= int(stats.get("strategy_pool") or 0)
            )
            bootstrap_batch(
                db,
                limit=max(100, int(settings.bootstrap_batch_size)),
                retry_errors=retry_errors,
            )
            return

        update = sync_daily_public(db)
        if update.get("status") == "ok":
            run_live_scan(db)
            return

        stats = data_stats(db)
        if stats.get("scan_ready"):
            return

        unresolved = int(stats.get("latest_unresolved") or 0)
        problems = int(stats.get("latest_audit_problem") or 0)
        unattempted = max(0, unresolved - problems)

        if unattempted > 0:
            audit_latest_day(db, limit=min(500, max(200, unattempted)), retry_errors=False)
            return

        if problems > 0:
            audit_latest_day(db, limit=min(200, max(50, problems)), retry_errors=True)
    finally:
        db.close()


def _data_job_busy(db) -> bool:
    return bool(db.execute(
        select(func.count(DataUpdateRun.id)).where(DataUpdateRun.status == "running")
    ).scalar_one() or 0)


def _scan_job_busy(db) -> bool:
    return bool(db.execute(
        select(func.count(ScanRun.id)).where(ScanRun.status == "running")
    ).scalar_one() or 0)


def _live_scan_busy(db) -> bool:
    return bool(db.execute(
        select(func.count(LiveScanRun.id)).where(LiveScanRun.status == "running")
    ).scalar_one() or 0)


def _bg_live_scan(force: bool = False):
    db = SessionLocal()
    try:
        run_live_scan(db, force=force)
    finally:
        db.close()


def _bg_full_scan():
    db = SessionLocal()
    try:
        run_full_scan(db)
    finally:
        db.close()


def _bg_daily_and_scan():
    db = SessionLocal()
    try:
        update = sync_daily_public(db)
        if update.get("status") == "ok":
            run_live_scan(db)  # daily path: latest-only; full scan is validation-only
    finally:
        db.close()


def _db_storage_stats(db, stats: dict) -> dict:
    # Measure current DB usage and estimate full-universe bootstrap size.
    capacity_mb = max(0, int(settings.database_capacity_mb))
    capacity_bytes = capacity_mb * 1024 * 1024 if capacity_mb else 0
    dialect = db.get_bind().dialect.name
    used_bytes = 0
    daily_bytes = 0
    error = ""

    try:
        if dialect == "postgresql":
            used_bytes = int(
                db.execute(text("SELECT pg_database_size(current_database())")).scalar_one() or 0
            )
            daily_bytes = int(
                db.execute(text("SELECT pg_total_relation_size('daily_bars')")).scalar_one() or 0
            )
        elif dialect == "sqlite":
            page_size = int(db.execute(text("PRAGMA page_size")).scalar_one() or 0)
            page_count = int(db.execute(text("PRAGMA page_count")).scalar_one() or 0)
            used_bytes = page_size * page_count
    except Exception as e:
        error = str(e)

    projected_bytes = None
    projected_rows = None
    done = int(stats.get("bootstrap_done") or 0)
    total = int(stats.get("active_nonst") or 0)
    bars = int(stats.get("bars") or 0)

    if used_bytes > 0 and done > 0 and total > done and bars > 0:
        projected_rows = int(round(bars * total / done))
        if daily_bytes > 0:
            bytes_per_bar = daily_bytes / bars
            projected_bytes = used_bytes + max(0, projected_rows - bars) * bytes_per_bar
        else:
            projected_bytes = used_bytes * total / done
    elif used_bytes > 0 and done > 0 and total and done >= total:
        projected_bytes = float(used_bytes)
        projected_rows = bars

    used_pct = (used_bytes / capacity_bytes) if capacity_bytes else None
    projected_pct = (
        projected_bytes / capacity_bytes
        if (capacity_bytes and projected_bytes is not None)
        else None
    )

    level = "UNKNOWN"
    level_cn = "等待估算"
    if projected_pct is not None:
        if (used_pct is not None and used_pct >= 0.85) or projected_pct > 0.95:
            level, level_cn = "HIGH", "高风险"
        elif projected_pct > 0.80:
            level, level_cn = "CAUTION", "注意"
        else:
            level, level_cn = "SAFE", "安全"

    return {
        "available": used_bytes > 0,
        "dialect": dialect,
        "capacity_mb": capacity_mb,
        "used_mb": used_bytes / (1024 * 1024) if used_bytes else 0.0,
        "used_pct": used_pct,
        "daily_mb": daily_bytes / (1024 * 1024) if daily_bytes else 0.0,
        "projected_mb": projected_bytes / (1024 * 1024) if projected_bytes is not None else None,
        "projected_pct": projected_pct,
        "projected_rows": projected_rows,
        "level": level,
        "level_cn": level_cn,
        "error": error,
    }


def _live_data_progress(db, target: date | None = None, full: bool = False) -> dict:
    update = db.execute(
        select(DataUpdateRun).order_by(DataUpdateRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    update_payload = None
    if update is not None:
        update_payload = {
            "status": update.status,
            "provider": update.provider,
            "start_date": str(update.start_date) if update.start_date else None,
            "end_date": str(update.end_date) if update.end_date else None,
            "stock_count": int(update.stock_count or 0),
            "success_count": int(update.success_count or 0),
            "failed_count": int(update.failed_count or 0),
            "message": update.message or "",
        }

    payload = {"update": update_payload, "data_busy": _data_job_busy(db)}
    if not full:
        return payload

    stocks = int(db.execute(select(func.count(Stock.code))).scalar_one() or 0)
    bars = int(db.execute(select(func.count(DailyBar.id))).scalar_one() or 0)
    pool = int(db.execute(
        select(func.count(Stock.code)).where(Stock.is_st.is_(False))
    ).scalar_one() or 0)
    done = int(db.execute(
        select(func.count(BootstrapStock.code))
        .where(BootstrapStock.status == "ok")
    ).scalar_one() or 0)
    errs = int(db.execute(
        select(func.count(BootstrapStock.code))
        .where(BootstrapStock.status == "error")
    ).scalar_one() or 0)

    target_day = target or db.execute(
        select(func.max(DailyBar.trade_date))
    ).scalar_one_or_none()

    latest_rows = 0
    suspended = 0
    problems = 0
    if target_day is not None and pool:
        day_codes = (
            select(DailyBar.code.label("code"))
            .where(DailyBar.trade_date == target_day)
            .subquery()
        )
        latest_rows = int(db.execute(
            select(func.count(distinct(DailyBar.code)))
            .join(Stock, Stock.code == DailyBar.code)
            .where(
                DailyBar.trade_date == target_day,
                Stock.is_st.is_(False),
            )
        ).scalar_one() or 0)
        suspended = int(db.execute(
            select(func.count(distinct(LatestDayAudit.code)))
            .join(Stock, Stock.code == LatestDayAudit.code)
            .outerjoin(day_codes, day_codes.c.code == LatestDayAudit.code)
            .where(
                LatestDayAudit.target_date == target_day,
                LatestDayAudit.status == "suspended",
                Stock.is_st.is_(False),
                day_codes.c.code.is_(None),
            )
        ).scalar_one() or 0)
        problems = int(db.execute(
            select(func.count(distinct(LatestDayAudit.code)))
            .join(Stock, Stock.code == LatestDayAudit.code)
            .outerjoin(day_codes, day_codes.c.code == LatestDayAudit.code)
            .where(
                LatestDayAudit.target_date == target_day,
                LatestDayAudit.status.in_(("unknown", "invalid", "error")),
                Stock.is_st.is_(False),
                day_codes.c.code.is_(None),
            )
        ).scalar_one() or 0)

    unresolved = max(0, pool - latest_rows - suspended)
    tradable_pool = max(0, pool - suspended)
    tradable_cov = latest_rows / tradable_pool if tradable_pool else 1.0
    verified_cov = min(1.0, (latest_rows + suspended) / pool) if pool else 0.0

    payload["stats"] = {
        "stocks": stocks,
        "bars": bars,
        "strategy_pool": pool,
        "bootstrap_done": done,
        "bootstrap_errors": errs,
        "bootstrap_coverage": done / pool if pool else 0.0,
        "latest_strategy_rows": latest_rows,
        "latest_suspended": suspended,
        "latest_audit_problem": problems,
        "latest_unresolved": unresolved,
        "latest_tradable_pool": tradable_pool,
        "latest_tradable_coverage": tradable_cov,
        "latest_verified_coverage": verified_cov,
        "history_threshold": settings.min_scan_bootstrap_coverage,
        "latest_threshold": settings.min_latest_bar_coverage,
        "verified_threshold": settings.min_latest_verified_coverage,
    }
    return payload


@app.on_event("startup")
def startup():
    init_db()
    db = SessionLocal()
    try:
        recover_interrupted_runs(db)
        recover_interrupted_scans(db)
        recover_interrupted_live_scans(db)
    finally:
        db.close()


@app.middleware("http")
async def auth_middleware(request, call_next):
    public = {"/login", "/health"}
    if request.url.path not in public and not request.url.path.startswith("/static") and not is_logged_in(request):
        return RedirectResponse("/login", 303)
    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True, "version": settings.web_version, "strategy": settings.strategy_version, "live_strategy": settings.live_strategy_version}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "app_name": settings.app_name})


@app.post("/login")
def login(password: str = Form(...)):
    if not valid_password(password):
        return RedirectResponse("/login?error=1", 303)
    r = RedirectResponse("/", 303)
    r.set_cookie(COOKIE, make_cookie(), httponly=True, samesite="lax", secure=settings.cookie_secure, max_age=2592000)
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", 303)
    r.delete_cookie(COOKIE)
    return r


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db=Depends(db_session)):
    latest = latest_trade_date(db)
    scan = db.execute(select(ScanRun).order_by(ScanRun.id.desc()).limit(1)).scalar_one_or_none()
    live_run = db.execute(select(LiveScanRun).order_by(LiveScanRun.id.desc()).limit(1)).scalar_one_or_none()
    update = db.execute(select(DataUpdateRun).order_by(DataUpdateRun.id.desc()).limit(1)).scalar_one_or_none()

    live_current = bool(latest and live_run and live_run.status == "ok" and live_run.data_date == latest)
    sigs = []
    if latest and live_current:
        sigs = db.execute(
            select(Signal, Stock)
            .join(Stock, Stock.code == Signal.code, isouter=True)
            .where(
                Signal.strategy_version == settings.live_strategy_version,
                Signal.signal_date == latest,
            )
            .order_by(Signal.engine, Signal.target_weight.desc())
        ).all()

    counts = {e: sum(1 for s, _ in sigs if s.engine == e) for e in "ABC"}
    if scan is None:
        golden_status = "未扫描"
    elif scan.golden_matched is None:
        golden_status = "未校验（历史不足）"
    elif scan.golden_missing == 0 and scan.golden_extra == 0:
        golden_status = "PASS"
    else:
        golden_status = "FAIL"

    state = live_state_status(db, latest)
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "latest": latest, "scan": scan,
        "live_run": live_run, "live_progress": live_progress_payload(live_run),
        "live_current": live_current, "live_busy": _live_scan_busy(db),
        "scan_busy": _scan_job_busy(db), "data_busy": _data_job_busy(db),
        "signals": sigs, "signal_date": latest, "counts": counts,
        "golden_status": golden_status, "update": update, "state": state,
        "web_version": settings.web_version,
    })

@app.get("/review", response_class=HTMLResponse)
def review_center(
    request: Request,
    engine: str = Query("ALL"),
    outcome: str = Query("ALL"),
    rating: str = Query("ALL"),
    sort: str = Query("DATE_DESC"),
    db=Depends(db_session),
):
    data = review_index_data(
        db,
        engine=engine,
        outcome=outcome,
        rating=rating,
        sort=sort,
    )
    return templates.TemplateResponse("review.html", {
        "request": request,
        **data,
    })


@app.get("/review/{signal_id}", response_class=HTMLResponse)
def review_case(
    request: Request,
    signal_id: int,
    saved: int = Query(0),
    db=Depends(db_session),
):
    case = review_case_data(db, signal_id)
    if case is None:
        raise HTTPException(404, "historical V18 signal not found")
    return templates.TemplateResponse("review_case.html", {
        "request": request,
        "case": case,
        "saved": bool(saved),
    })


@app.post("/review/{signal_id}/save")
def save_review_case(
    signal_id: int,
    rating: str = Form(""),
    tags: list[str] = Form(default=[]),
    note: str = Form(""),
    db=Depends(db_session),
):
    try:
        save_signal_review(db, signal_id, rating, tags, note)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return RedirectResponse(f"/review/{signal_id}?saved=1", 303)


@app.get("/api/review/{signal_id}/chart")
def review_case_chart(
    signal_id: int,
    pre: int = Query(60, ge=20, le=120),
    post: int = Query(40, ge=20, le=120),
    db=Depends(db_session),
):
    chart = build_review_chart(db, signal_id, pre=pre, post=post)
    if not chart.get("bars"):
        raise HTTPException(404, "review chart not found")
    return chart


@app.get("/screener", response_class=HTMLResponse)
def screener(
    request: Request,
    n: int = Query(20, ge=1, le=500),
    engine: str = Query("ALL"),
    db=Depends(db_session),
):
    days = db.execute(
        select(distinct(DailyBar.trade_date)).order_by(DailyBar.trade_date.desc()).limit(n)
    ).scalars().all()
    cutoff = min(days) if days else date.today() - timedelta(days=n * 2)
    latest_day = latest_trade_date(db)
    q = (
        select(Signal, Stock)
        .join(Stock, Stock.code == Signal.code, isouter=True)
        .where(
            Signal.strategy_version == settings.live_strategy_version,
            Signal.signal_date >= cutoff,
        )
    )
    if engine != "ALL":
        q = q.where(Signal.engine.in_(tuple(engine.replace("+", ""))))
    rows = db.execute(q.order_by(Signal.signal_date.desc(), Signal.engine)).all()
    latest = {}
    for s, st in rows:
        latest.setdefault(s.code, (s, st))

    prices = latest_prices(db, latest.keys())
    trade_days_desc = db.execute(select(distinct(DailyBar.trade_date)).order_by(DailyBar.trade_date.desc())).scalars().all()
    day_pos = {d: i for i, d in enumerate(reversed(trade_days_desc))}
    final_rows = []
    for s, st in latest.values():
        px_date, px = prices.get(s.code, (None, None))
        ret = px / s.signal_close - 1 if px else None
        age = (day_pos.get(latest_day, 0) - day_pos.get(s.signal_date, 0)) if latest_day and s.signal_date in day_pos else None
        if s.exit_date and latest_day and s.exit_date <= latest_day:
            lifecycle = f"已退出 · {s.exit_reason or '-'}"
        else:
            lifecycle = "持仓生命周期中/数据末端"
        final_rows.append({
            "signal": s, "stock": st, "current_date": px_date, "current_price": px,
            "since_ret": ret, "age": age, "lifecycle": lifecycle,
        })
    final_rows.sort(key=lambda x: (x["signal"].signal_date, x["signal"].target_weight), reverse=True)
    return templates.TemplateResponse("screener.html", {
        "request": request, "rows": final_rows, "n": n, "engine": engine,
        "cutoff": cutoff, "latest_day": latest_day,
    })


@app.get("/stock/{code}", response_class=HTMLResponse)
def stock_page(code: str, request: Request, db=Depends(db_session)):
    code = code.zfill(6)
    stock = db.get(Stock, code)
    sigs = db.execute(select(Signal).where(Signal.code == code).order_by(Signal.signal_date)).scalars().all()
    return templates.TemplateResponse("stock.html", {"request": request, "stock": stock, "code": code, "signals": sigs})


@app.get("/api/stock/{code}/chart")
def stock_chart(code: str, limit: int = Query(520, ge=120, le=1500), db=Depends(db_session)):
    return build_stock_chart(db, code, limit)


@app.get("/execution", response_class=HTMLResponse)
def execution_page(request: Request, signal_id: int | None = None, db=Depends(db_session)):
    signal = db.get(Signal, signal_id) if signal_id else None
    stock = db.get(Stock, signal.code) if signal else None
    return templates.TemplateResponse("execution.html", {"request": request, "signal": signal, "stock": stock, "result": None})


@app.post("/execution", response_class=HTMLResponse)
def execution_calc(
    request: Request,
    signal_id: int = Form(...),
    entry_price: float = Form(...),
    db=Depends(db_session),
):
    s = db.get(Signal, signal_id)
    if not s:
        raise HTTPException(404)
    stock = db.get(Stock, s.code)
    if entry_price <= s.fail_price:
        result = {"skip": True, "reason": "实际开盘/成交价已经不高于结构失效价，结构失效，跳过。"}
    else:
        risk = (entry_price - s.fail_price) / entry_price
        budget = .015 if s.engine == "C" else .025
        result = {
            "skip": False, "risk": risk, "weight": min(.20, budget / risk),
            "entry": entry_price, "budget": budget,
            "gap_vs_signal": entry_price / s.signal_close - 1,
        }
    return templates.TemplateResponse("execution.html", {"request": request, "signal": s, "stock": stock, "result": result})


@app.get("/backtest", response_class=HTMLResponse)
def backtest_page(request: Request):
    return templates.TemplateResponse("backtest.html", {"request": request, "result": None})


@app.post("/backtest", response_class=HTMLResponse)
def backtest_run(
    request: Request,
    engines: str = Form("ABC"),
    start: str = Form(""),
    end: str = Form(""),
    slippage_bps: float = Form(0),
    commission_bps: float = Form(0),
    stamp_tax_bps: float = Form(0),
    db=Depends(db_session),
):
    eng = tuple(engines)
    sd = date.fromisoformat(start) if start else None
    ed = date.fromisoformat(end) if end else None
    result = close_vs_next_open(db, eng, sd, ed, slippage_bps, commission_bps, stamp_tax_bps)
    return templates.TemplateResponse("backtest.html", {
        "request": request, "result": result, "engines": engines, "start": start, "end": end,
        "slippage_bps": slippage_bps, "commission_bps": commission_bps, "stamp_tax_bps": stamp_tax_bps,
    })


@app.get("/portfolio", response_class=HTMLResponse)
def portfolio_page(request: Request):
    return templates.TemplateResponse("portfolio.html", {"request": request, "result": None})


@app.post("/portfolio", response_class=HTMLResponse)
def portfolio_run(
    request: Request,
    engines: str = Form("ABC"),
    execution: str = Form("next_open"),
    k: int = Form(5),
    ab_risk: float = Form(2.5),
    c_risk: float = Form(1.5),
    max_weight: float = Form(20.0),
    start: str = Form(""),
    end: str = Form(""),
    slippage_bps: float = Form(0),
    commission_bps: float = Form(0),
    stamp_tax_bps: float = Form(0),
    monte_carlo_seeds: int = Form(0),
    db=Depends(db_session),
):
    sd = date.fromisoformat(start) if start else None
    ed = date.fromisoformat(end) if end else None
    result = portfolio_backtest(
        db, tuple(engines), execution, sd, ed, k,
        ab_risk / 100.0, c_risk / 100.0, max_weight / 100.0,
        slippage_bps, commission_bps, stamp_tax_bps,
        True, 1, monte_carlo_seeds,
    )
    chart = json.dumps({
        "dates": [str(x["date"]) for x in result.get("equity", [])],
        "equity": [x["equity"] for x in result.get("equity", [])],
        "positions": [x["positions"] for x in result.get("equity", [])],
    }, ensure_ascii=False)
    return templates.TemplateResponse("portfolio.html", {
        "request": request, "result": result, "chart_json": chart,
        "engines": engines, "execution": execution, "k": k, "ab_risk": ab_risk,
        "c_risk": c_risk, "max_weight": max_weight, "start": start, "end": end,
        "slippage_bps": slippage_bps, "commission_bps": commission_bps,
        "stamp_tax_bps": stamp_tax_bps, "monte_carlo_seeds": monte_carlo_seeds,
    })


@app.get("/api/data/progress")
def data_progress(
    target: date | None = Query(None),
    full: bool = Query(False),
    db=Depends(db_session),
):
    return _live_data_progress(db, target=target, full=full)


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request, db=Depends(db_session)):
    stats = data_stats(db)
    storage = _db_storage_stats(db, stats)
    update = db.execute(select(DataUpdateRun).order_by(DataUpdateRun.id.desc()).limit(1)).scalar_one_or_none()
    errors = db.execute(
        select(BootstrapStock).where(BootstrapStock.status == "error").order_by(BootstrapStock.updated_at.desc()).limit(20)
    ).scalars().all()
    bootstrap_busy = _data_job_busy(db)
    return templates.TemplateResponse("data.html", {
        "request": request, "stats": stats, "update": update, "errors": errors,
        "provider": settings.data_provider, "bootstrap_start": settings.bootstrap_start_date,
        "batch_size": settings.bootstrap_batch_size, "storage": storage,
        "bootstrap_busy": bootstrap_busy,
        "stock_timeout": settings.bootstrap_stock_timeout_seconds,
        "gap_batch_size": settings.gap_repair_batch_size,
        "audit_batch_size": settings.latest_audit_batch_size,
    })


@app.post("/admin/smart-update")
def admin_smart_update(
    background_tasks: BackgroundTasks,
    db=Depends(db_session),
):
    if not _data_job_busy(db) and not _scan_job_busy(db) and not _live_scan_busy(db):
        background_tasks.add_task(_bg_smart_update)
    return RedirectResponse("/data", 303)


@app.post("/admin/bootstrap")
def admin_bootstrap(
    background_tasks: BackgroundTasks,
    limit: int = Form(100),
    db=Depends(db_session),
):
    limit = max(1, min(int(limit), 500))
    busy = _data_job_busy(db)
    if not busy:
        background_tasks.add_task(_bg_bootstrap, limit, False)
    return RedirectResponse("/data", 303)


@app.post("/admin/bootstrap-errors")
def admin_bootstrap_errors(
    background_tasks: BackgroundTasks,
    limit: int = Form(20),
    db=Depends(db_session),
):
    limit = max(1, min(int(limit), 100))
    busy = _data_job_busy(db)
    if not busy:
        background_tasks.add_task(_bg_bootstrap, limit, True)
    return RedirectResponse("/data", 303)


@app.post("/admin/repair-latest")
def admin_repair_latest(
    background_tasks: BackgroundTasks,
    limit: int = Form(500),
    db=Depends(db_session),
):
    limit = max(1, min(int(limit), 500))
    if not _data_job_busy(db):
        background_tasks.add_task(_bg_gap_repair, limit, False)
    return RedirectResponse("/data", 303)


@app.post("/admin/repair-latest-errors")
def admin_repair_latest_errors(background_tasks: BackgroundTasks, limit: int = Form(100), db=Depends(db_session)):
    limit = max(1, min(int(limit), 200))
    if not _data_job_busy(db):
        background_tasks.add_task(_bg_gap_repair, limit, True)
    return RedirectResponse("/data", 303)


@app.post("/admin/audit-latest")
def admin_audit_latest(
    background_tasks: BackgroundTasks,
    limit: int = Form(200),
    db=Depends(db_session),
):
    limit = max(1, min(int(limit), 500))
    if not _data_job_busy(db):
        background_tasks.add_task(_bg_latest_audit, limit, False)
    return RedirectResponse("/data", 303)


@app.post("/admin/audit-latest-errors")
def admin_audit_latest_errors(
    background_tasks: BackgroundTasks,
    limit: int = Form(100),
    db=Depends(db_session),
):
    limit = max(1, min(int(limit), 200))
    if not _data_job_busy(db):
        background_tasks.add_task(_bg_latest_audit, limit, True)
    return RedirectResponse("/data", 303)


@app.post("/admin/daily-update")
def admin_daily_update(background_tasks: BackgroundTasks, db=Depends(db_session)):
    if not _data_job_busy(db) and not _scan_job_busy(db) and not _live_scan_busy(db):
        background_tasks.add_task(_bg_daily_and_scan)
    return RedirectResponse("/data", 303)


@app.get("/api/live-scan/progress")
def live_scan_progress(db=Depends(db_session)):
    run = db.execute(
        select(LiveScanRun).order_by(LiveScanRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    p = live_progress_payload(run)
    p["live_busy"] = _live_scan_busy(db)
    p["scan_busy"] = _scan_job_busy(db)
    p["data_busy"] = _data_job_busy(db)
    return p


@app.post("/admin/live-scan")
def admin_live_scan(
    background_tasks: BackgroundTasks,
    db=Depends(db_session),
):
    if not _live_scan_busy(db) and not _scan_job_busy(db) and not _data_job_busy(db):
        background_tasks.add_task(_bg_live_scan, True)
    return RedirectResponse("/", 303)

@app.get("/api/scan/progress")
def scan_progress(db=Depends(db_session)):
    run = db.execute(
        select(ScanRun).order_by(ScanRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    payload = scan_progress_payload(run)
    payload["scan_busy"] = _scan_job_busy(db)
    payload["data_busy"] = _data_job_busy(db)
    return payload


@app.get("/validation", response_class=HTMLResponse)
def validation(request: Request, db=Depends(db_session)):
    run = db.execute(
        select(ScanRun).order_by(ScanRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    ready = scan_readiness(db, check_calendar=True)
    progress = scan_progress_payload(run)
    return templates.TemplateResponse("validation.html", {
        "request": request, "run": run, "ready": ready,
        "progress": progress,
        "scan_busy": _scan_job_busy(db),
        "data_busy": _data_job_busy(db),
    })


@app.post("/admin/scan")
def admin_scan(
    background_tasks: BackgroundTasks,
    db=Depends(db_session),
):
    if _scan_job_busy(db) or _live_scan_busy(db) or _data_job_busy(db):
        return RedirectResponse("/validation", 303)
    ready = scan_readiness(db, check_calendar=True)
    if not ready["scan_ready"]:
        return RedirectResponse("/validation", 303)
    background_tasks.add_task(_bg_full_scan)
    return RedirectResponse("/validation", 303)
