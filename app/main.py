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
from .models import Stock, DailyBar, Signal, ScanRun, DataUpdateRun, BootstrapStock
from .auth import valid_password, make_cookie, is_logged_in, COOKIE
from .services.repository import latest_trade_date, latest_prices
from .services.strategy_service import run_full_scan
from .services.backtest import close_vs_next_open, portfolio_backtest
from .services.chart_service import build_stock_chart
from .services.data_update import (
    data_stats, bootstrap_batch, sync_daily_public, scan_readiness,
    recover_interrupted_runs,
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


def _bg_daily_and_scan():
    db = SessionLocal()
    try:
        update = sync_daily_public(db)
        if update.get("status") == "ok":
            run_full_scan(db)  # integrity gate inside run_full_scan blocks partial-universe scans
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


def _live_data_progress(db) -> dict:
    # Lightweight DB-only status for browser polling.
    stocks = int(db.execute(select(func.count(Stock.code))).scalar_one() or 0)
    bars = int(db.execute(select(func.count(DailyBar.id))).scalar_one() or 0)
    active_nonst = int(db.execute(
        select(func.count(Stock.code)).where(Stock.is_st.is_(False))
    ).scalar_one() or 0)
    bootstrap_done = int(db.execute(
        select(func.count(BootstrapStock.code)).where(BootstrapStock.status == "ok")
    ).scalar_one() or 0)
    bootstrap_errors = int(db.execute(
        select(func.count(BootstrapStock.code)).where(BootstrapStock.status == "error")
    ).scalar_one() or 0)
    coverage = (bootstrap_done / active_nonst) if active_nonst else 0.0

    latest = db.execute(select(func.max(DailyBar.trade_date))).scalar_one_or_none()
    latest_rows = 0
    if latest is not None:
        latest_rows = int(db.execute(
            select(func.count(DailyBar.id)).where(DailyBar.trade_date == latest)
        ).scalar_one() or 0)

    update = db.execute(
        select(DataUpdateRun).order_by(DataUpdateRun.id.desc()).limit(1)
    ).scalar_one_or_none()

    bootstrap_busy = bool(db.execute(
        select(func.count(DataUpdateRun.id)).where(
            DataUpdateRun.status == "running",
            DataUpdateRun.provider.like("%-bootstrap%"),
        )
    ).scalar_one() or 0)

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

    return {
        "stats": {
            "stocks": stocks,
            "bars": bars,
            "bootstrap_done": bootstrap_done,
            "bootstrap_errors": bootstrap_errors,
            "bootstrap_coverage": coverage,
            "latest": str(latest) if latest else None,
            "latest_rows": latest_rows,
        },
        "update": update_payload,
        "bootstrap_busy": bootstrap_busy,
    }


@app.on_event("startup")
def startup():
    init_db()
    db = SessionLocal()
    try:
        recover_interrupted_runs(db)
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
    return {"ok": True, "version": settings.web_version, "strategy": settings.strategy_version}


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
    update = db.execute(select(DataUpdateRun).order_by(DataUpdateRun.id.desc()).limit(1)).scalar_one_or_none()
    sigs = []
    if latest:
        sigs = db.execute(
            select(Signal, Stock)
            .join(Stock, Stock.code == Signal.code, isouter=True)
            .where(Signal.signal_date == latest)
            .order_by(Signal.engine, Signal.target_weight.desc())
        ).all()
    counts = {e: sum(1 for s, _ in sigs if s.engine == e) for e in "ABC"}
    golden_pass = bool(scan and scan.golden_missing == 0 and scan.golden_extra == 0 and scan.combined_count == 198)
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "latest": latest, "scan": scan, "signals": sigs,
        "signal_date": latest, "counts": counts, "golden_pass": golden_pass, "update": update,
    })


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
        .where(Signal.signal_date >= cutoff)
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
def data_progress(db=Depends(db_session)):
    return _live_data_progress(db)


@app.get("/data", response_class=HTMLResponse)
def data_page(request: Request, db=Depends(db_session)):
    stats = data_stats(db)
    storage = _db_storage_stats(db, stats)
    update = db.execute(select(DataUpdateRun).order_by(DataUpdateRun.id.desc()).limit(1)).scalar_one_or_none()
    errors = db.execute(
        select(BootstrapStock).where(BootstrapStock.status == "error").order_by(BootstrapStock.updated_at.desc()).limit(20)
    ).scalars().all()
    bootstrap_busy = bool(db.execute(
        select(func.count(DataUpdateRun.id)).where(
            DataUpdateRun.status == "running",
            DataUpdateRun.provider.like("%-bootstrap%"),
        )
    ).scalar_one() or 0)
    return templates.TemplateResponse("data.html", {
        "request": request, "stats": stats, "update": update, "errors": errors,
        "provider": settings.data_provider, "bootstrap_start": settings.bootstrap_start_date,
        "batch_size": settings.bootstrap_batch_size, "storage": storage,
        "bootstrap_busy": bootstrap_busy,
        "stock_timeout": settings.bootstrap_stock_timeout_seconds,
    })


@app.post("/admin/bootstrap")
def admin_bootstrap(
    background_tasks: BackgroundTasks,
    limit: int = Form(100),
    db=Depends(db_session),
):
    limit = max(1, min(int(limit), 500))
    busy = bool(db.execute(
        select(func.count(DataUpdateRun.id)).where(
            DataUpdateRun.status == "running",
            DataUpdateRun.provider.like("%-bootstrap%"),
        )
    ).scalar_one() or 0)
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
    busy = bool(db.execute(
        select(func.count(DataUpdateRun.id)).where(
            DataUpdateRun.status == "running",
            DataUpdateRun.provider.like("%-bootstrap%"),
        )
    ).scalar_one() or 0)
    if not busy:
        background_tasks.add_task(_bg_bootstrap, limit, True)
    return RedirectResponse("/data", 303)


@app.post("/admin/daily-update")
def admin_daily_update(background_tasks: BackgroundTasks):
    background_tasks.add_task(_bg_daily_and_scan)
    return RedirectResponse("/data", 303)


@app.get("/validation", response_class=HTMLResponse)
def validation(request: Request, db=Depends(db_session)):
    run = db.execute(select(ScanRun).order_by(ScanRun.id.desc()).limit(1)).scalar_one_or_none()
    ready = scan_readiness(db, check_calendar=True)
    return templates.TemplateResponse("validation.html", {"request": request, "run": run, "ready": ready})


@app.post("/admin/scan")
def admin_scan(db=Depends(db_session)):
    run_full_scan(db)
    return RedirectResponse("/validation", 303)
