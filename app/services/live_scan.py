from __future__ import annotations

from datetime import date, datetime, timedelta
import gc
import json

import numpy as np
import pandas as pd
from sqlalchemy import select, func, delete

from ..config import settings
from ..engine.strategy_reference_v18 import (
    MarketContext,
    PARAM,
    attach_a_risk,
    attach_b_risk,
    attach_c_risk,
    combine_abc,
    enrich_close_entry,
    mainstream_ok,
    prepare_stock,
    scan_a_window,
    scan_b_final,
    scan_c_final,
)
from ..models import DailyBar, LiveMarketState, LivePeerEvent, LiveScanRun
from .data_update import scan_readiness
from .repository import (
    close_asof_for_codes,
    iter_frame_batches,
    latest_trade_date,
    replace_signals_for_date,
)

_LIVE_PROGRESS_PREFIX = "__LIVE_PROGRESS__:"


def _rss_mb() -> float | None:
    try:
        import resource
        import sys
        value = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value / (1024.0 * 1024.0) if sys.platform == "darwin" else value / 1024.0
    except Exception:
        return None


def _set_live_progress(db, run: LiveScanRun, percent: float, stage: str, detail: str = "") -> None:
    payload = {
        "percent": round(float(max(0.0, min(100.0, percent))), 1),
        "stage": str(stage),
        "detail": str(detail),
        "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    run.message = _LIVE_PROGRESS_PREFIX + json.dumps(payload, ensure_ascii=False)
    db.commit()


def live_progress_payload(run: LiveScanRun | None) -> dict:
    if run is None:
        return {
            "exists": False, "status": "none", "percent": 0.0,
            "stage": "尚未运行日常扫描", "detail": "", "elapsed_seconds": 0,
        }
    progress = {}
    if (run.message or "").startswith(_LIVE_PROGRESS_PREFIX):
        try:
            progress = json.loads((run.message or "")[len(_LIVE_PROGRESS_PREFIX):])
        except Exception:
            progress = {}
    now = datetime.utcnow()
    end = run.finished_at or now
    elapsed = max(0, int((end - run.started_at).total_seconds())) if run.started_at else 0
    default_pct = 100.0 if run.status == "ok" else float(progress.get("percent") or 0.0)
    return {
        "exists": True,
        "id": run.id,
        "status": run.status,
        "percent": float(progress.get("percent", default_pct)),
        "stage": progress.get("stage", "日常扫描完成" if run.status == "ok" else run.status),
        "detail": progress.get("detail", "" if run.status == "running" else (run.message or "")),
        "updated_at": progress.get("updated_at"),
        "elapsed_seconds": elapsed,
        "data_date": str(run.data_date) if run.data_date else None,
        "a_count": int(run.a_count or 0),
        "b_count": int(run.b_count or 0),
        "c_count": int(run.c_count or 0),
        "combined_count": int(run.combined_count or 0),
        "candidate_count": int(run.candidate_count or 0),
    }


def recover_interrupted_live_scans(db) -> int:
    runs = db.execute(
        select(LiveScanRun).where(LiveScanRun.status == "running")
    ).scalars().all()
    if not runs:
        return 0
    for run in runs:
        p = live_progress_payload(run)
        detail = (
            f"服务重启中断日常扫描；中断前阶段：{p.get('stage') or '-'}"
            f"（{float(p.get('percent') or 0):.1f}%）。"
        )
        prev = p.get("detail") or ""
        if prev:
            detail += f" 中断前记录：{prev}。"
        detail += " 可安全重新运行。"
        run.status = "interrupted"
        run.message = _LIVE_PROGRESS_PREFIX + json.dumps({
            "percent": float(p.get("percent") or 0),
            "stage": "interrupted",
            "detail": detail,
            "updated_at": datetime.utcnow().isoformat(timespec="seconds"),
        }, ensure_ascii=False)
        run.finished_at = datetime.utcnow()
    db.commit()
    return len(runs)


def live_state_status(db, target: date | None = None) -> dict:
    version = settings.strategy_version
    count = int(db.execute(
        select(func.count(LiveMarketState.id))
        .where(LiveMarketState.strategy_version == version)
    ).scalar_one() or 0)
    max_date = db.execute(
        select(func.max(LiveMarketState.trade_date))
        .where(LiveMarketState.strategy_version == version)
    ).scalar_one_or_none()
    peers = int(db.execute(
        select(func.count(LivePeerEvent.id))
        .where(LivePeerEvent.strategy_version == version)
    ).scalar_one() or 0)

    target = target or latest_trade_date(db)
    if target is None:
        ready = False
    else:
        prev = db.execute(
            select(func.max(DailyBar.trade_date))
            .where(DailyBar.trade_date < target)
        ).scalar_one_or_none()
        needed = prev or target
        ready = bool(count >= 120 and max_date is not None and max_date >= needed)

    return {
        "ready": ready,
        "market_rows": count,
        "market_max_date": max_date,
        "peer_rows": peers,
        "target": target,
    }


def _finite_or_none(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except Exception:
        return None


def _replace_seed_state(db, market: MarketContext, peer_pool: pd.DataFrame) -> None:
    version = settings.strategy_version
    db.execute(delete(LiveMarketState).where(LiveMarketState.strategy_version == version))
    db.execute(delete(LivePeerEvent).where(LivePeerEvent.strategy_version == version))
    db.commit()

    states = []
    for d in market.calendar:
        ts = pd.Timestamp(d)
        states.append(LiveMarketState(
            strategy_version=version,
            trade_date=ts.date(),
            q40=_finite_or_none(market.q40.get(ts, np.nan)),
            above20=_finite_or_none(market.above20.get(ts, np.nan)),
            mom20=_finite_or_none(market.mom20.get(ts, np.nan)),
            updated_at=datetime.utcnow(),
        ))
    db.add_all(states)
    db.commit()

    if peer_pool is not None and not peer_pool.empty:
        seen = set()
        rows = []
        for r in peer_pool.itertuples(index=False):
            key = (str(r.code).zfill(6), pd.Timestamp(r.date).date())
            if key in seen:
                continue
            seen.add(key)
            rows.append(LivePeerEvent(
                strategy_version=version,
                code=key[0],
                event_date=key[1],
                buy=float(r.buy),
                updated_at=datetime.utcnow(),
            ))
        db.add_all(rows)
        db.commit()


def _market_context_from_state(db, target: date) -> MarketContext:
    rows = db.execute(
        select(LiveMarketState)
        .where(
            LiveMarketState.strategy_version == settings.strategy_version,
            LiveMarketState.trade_date <= target,
        )
        .order_by(LiveMarketState.trade_date)
    ).scalars().all()
    if not rows:
        raise RuntimeError("日常扫描状态尚未初始化，请先运行一次完整V18扫描")

    calendar = pd.DatetimeIndex([pd.Timestamp(r.trade_date) for r in rows])
    q40 = pd.Series([np.nan if r.q40 is None else float(r.q40) for r in rows], index=calendar)
    above20 = pd.Series([np.nan if r.above20 is None else float(r.above20) for r in rows], index=calendar)
    mom20 = pd.Series([np.nan if r.mom20 is None else float(r.mom20) for r in rows], index=calendar)
    return MarketContext(
        calendar=calendar,
        cal_pos={pd.Timestamp(d): i for i, d in enumerate(calendar)},
        q40=q40,
        above20=above20,
        mom20=mom20,
    )


def _latest_raw_metrics(raw: pd.DataFrame, target: date) -> dict | None:
    if raw is None or raw.empty:
        return None
    dates = pd.to_datetime(raw["date"])
    if pd.Timestamp(dates.iloc[-1]).date() != target:
        return None

    o = raw["open"].to_numpy(dtype=float, copy=False)
    h = raw["high"].to_numpy(dtype=float, copy=False)
    c = raw["close"].to_numpy(dtype=float, copy=False)
    amount = raw["amount"].to_numpy(dtype=float, copy=False)
    turn = raw["turnover"].to_numpy(dtype=float, copy=False)
    t = len(raw) - 1

    def baseline(a: np.ndarray) -> float:
        if t < 139:
            return np.nan
        lo = max(0, t - 249)
        hi = t - 19  # exclusive => original t-20 is included
        w = a[lo:hi]
        finite = w[np.isfinite(w)]
        if len(finite) < 120:
            return np.nan
        return float(np.median(finite))

    abase = baseline(amount)
    tbase = baseline(turn)
    ma10 = float(np.mean(c[t-9:t+1])) if t >= 9 else np.nan
    ma20 = float(np.mean(c[t-19:t+1])) if t >= 19 else np.nan
    stock20 = float(c[t] / c[t-20] - 1) if t >= 20 and c[t-20] > 0 else np.nan
    above20 = float(c[t] > ma20) if np.isfinite(ma20) else np.nan
    mom20 = float(stock20 > 0) if np.isfinite(stock20) else np.nan

    trigger_common = bool(
        t >= 1
        and np.isfinite(abase)
        and np.isfinite(tbase)
        and np.isfinite(ma10)
        and abase >= PARAM.amount_floor
        and c[t] > o[t]
        and c[t] > h[t-1]
        and c[t] > ma10
    )
    # Canonical A60 peer pool is BROADER than final A:
    # build_a_peer_pool() only needs mainstream_ok(), whose turnover floor is
    # base_turn_min (0.5%), while final A additionally needs a_turn_min (1.8%).
    peer_common = bool(trigger_common and tbase >= PARAM.base_turn_min)
    final_common = bool(trigger_common and tbase >= PARAM.a_turn_min)
    return {
        "abase": abase, "tbase": tbase,
        "above20": above20, "mom20": mom20,
        "peer_common": peer_common,
        "final_common": final_common,
    }


def _upsert_target_market_state(db, target: date, q40: float, above20: float, mom20: float) -> None:
    version = settings.strategy_version
    obj = db.execute(
        select(LiveMarketState).where(
            LiveMarketState.strategy_version == version,
            LiveMarketState.trade_date == target,
        )
    ).scalar_one_or_none()
    if obj is None:
        obj = LiveMarketState(strategy_version=version, trade_date=target)
        db.add(obj)
    obj.q40 = _finite_or_none(q40)
    obj.above20 = _finite_or_none(above20)
    obj.mom20 = _finite_or_none(mom20)
    obj.updated_at = datetime.utcnow()
    db.commit()


def _peer_stats(db, market: MarketContext, target: date) -> tuple[int, float]:
    d0 = pd.Timestamp(target)
    if d0 not in market.cal_pos:
        return 0, np.nan
    pos = market.cal_pos[d0]
    if pos <= 0:
        return 0, np.nan
    lo_pos = max(0, pos - 20)
    hi_pos = max(0, pos - 3)
    if hi_pos < lo_pos:
        return 0, np.nan
    lo = pd.Timestamp(market.calendar[lo_pos]).date()
    hi = pd.Timestamp(market.calendar[hi_pos]).date()
    prevdate = pd.Timestamp(market.calendar[pos-1]).date()

    events = db.execute(
        select(LivePeerEvent).where(
            LivePeerEvent.strategy_version == settings.strategy_version,
            LivePeerEvent.event_date >= lo,
            LivePeerEvent.event_date <= hi,
        )
    ).scalars().all()
    if not events:
        return 0, np.nan

    px = close_asof_for_codes(db, [e.code for e in events], prevdate)
    rr = []
    for e in events:
        p = px.get(e.code)
        if p is not None and np.isfinite(p) and e.buy > 0:
            rr.append(float(p) / float(e.buy) - 1)
    if not rr:
        return 0, np.nan
    a = np.asarray(rr, dtype=float)
    return len(a), float(np.mean(a > 0))


def _pad_prepared_for_live(prepared: pd.DataFrame, days: int = 35) -> pd.DataFrame:
    """Open canonical B/C's historical n-35 guard without changing the target row."""
    if prepared.empty:
        return prepared.copy()
    last = prepared.iloc[-1].copy()
    last_date = pd.Timestamp(last["date"])
    pads = []
    for i in range(1, days + 1):
        r = last.copy()
        r["date"] = last_date + pd.Timedelta(i, unit="D")
        if "board" in r.index:
            r["board"] = False
        if "weekly_up" in r.index:
            r["weekly_up"] = False
        pads.append(r)
    return pd.concat([prepared, pd.DataFrame(pads)], ignore_index=True)


def _a_latest_for_one(
    prepared: pd.DataFrame,
    code: str,
    market: MarketContext,
    target: date,
    peer_n: int,
    peer_pos: float,
) -> tuple[pd.DataFrame, list[tuple[str, date, float]]]:
    rows = []
    new_peer = []
    for W in PARAM.pressure_windows:
        z = scan_a_window(prepared, code, W, market, PARAM)
        if z.empty:
            continue
        zt = z[pd.to_datetime(z["date"]).dt.date == target].copy()
        if zt.empty:
            continue

        if W == 60:
            for r in zt.itertuples(index=False):
                new_peer.append((str(r.code).zfill(6), target, float(r.buy)))

        zt = zt[
            (zt["pre20_atr"] <= PARAM.a_pre_atr_max)
            & (zt["turn_base"] >= PARAM.a_turn_min)
        ].copy()
        if not zt.empty:
            rows.append(zt)

    if not rows:
        return pd.DataFrame(), new_peer
    if peer_n < PARAM.a_peer_min_n or not np.isfinite(peer_pos) or peer_pos < PARAM.a_peer_pos_min:
        return pd.DataFrame(), new_peer

    q = pd.concat(rows, ignore_index=True)
    q["peer_n"] = peer_n
    q["peer_pos"] = peer_pos

    out = []
    for (c0, d0), g in q.groupby(["code", "date"], sort=False):
        if g["W"].nunique() < PARAM.a_min_window_consensus:
            continue
        gg = g.sort_values("W").reset_index(drop=True)
        rep = gg.iloc[len(gg)//2].copy()
        rep["window_count"] = g["W"].nunique()
        rep["windows"] = ",".join(map(str, sorted(g["W"].unique())))
        out.append(rep.to_dict())

    if not out:
        return pd.DataFrame(), new_peer
    a = pd.DataFrame(out)
    return attach_a_risk(a, {str(code).zfill(6): prepared}), new_peer


def _bc_latest_for_one(
    prepared: pd.DataFrame,
    code: str,
    market: MarketContext,
    target: date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    padded = _pad_prepared_for_live(prepared)

    b = scan_b_final({str(code).zfill(6): padded}, market, PARAM)
    if not b.empty:
        b = b[pd.to_datetime(b["date"]).dt.date == target].copy()
    if not b.empty:
        b = attach_b_risk(b, {str(code).zfill(6): prepared})

    c = scan_c_final({str(code).zfill(6): padded}, market, PARAM)
    if not c.empty:
        c = c[pd.to_datetime(c["date"]).dt.date == target].copy()
    if not c.empty:
        c = attach_c_risk(c, {str(code).zfill(6): prepared})

    return b, c


def _combine_live(a_parts, b_parts, c_parts) -> pd.DataFrame:
    a = pd.concat(a_parts, ignore_index=True) if a_parts else pd.DataFrame()
    b = pd.concat(b_parts, ignore_index=True) if b_parts else pd.DataFrame()
    c = pd.concat(c_parts, ignore_index=True) if c_parts else pd.DataFrame()
    if a.empty and b.empty and c.empty:
        return pd.DataFrame()
    sig = enrich_close_entry(combine_abc(a, b, c), PARAM)
    for col in ("exit_idx", "exit_ret", "exit_reason", "exit_date"):
        if col in sig.columns:
            sig = sig.drop(columns=col)
    return sig


def _replace_target_peer_events(db, target: date, events) -> None:
    version = settings.strategy_version
    db.execute(delete(LivePeerEvent).where(
        LivePeerEvent.strategy_version == version,
        LivePeerEvent.event_date == target,
    ))
    db.commit()
    seen = set()
    objs = []
    for code, d, buy in events:
        key = (str(code).zfill(6), d)
        if key in seen:
            continue
        seen.add(key)
        objs.append(LivePeerEvent(
            strategy_version=version,
            code=key[0],
            event_date=d,
            buy=float(buy),
            updated_at=datetime.utcnow(),
        ))
    if objs:
        db.add_all(objs)
        db.commit()


def _live_latest_from_prepared_seed(
    db,
    prepared_frames,
    market,
    historical_signals,
    target,
) -> pd.DataFrame:
    a_parts = []
    if historical_signals is not None and not historical_signals.empty:
        a = historical_signals[
            (historical_signals["engine"] == "A")
            & (pd.to_datetime(historical_signals["date"]).dt.date == target)
        ].copy()
        for col in ("exit_idx", "exit_ret", "exit_reason", "exit_date"):
            if col in a.columns:
                a = a.drop(columns=col)
        if not a.empty:
            a_parts.append(a)

    b_parts, c_parts = [], []
    q = float(market.q40.get(pd.Timestamp(target), np.nan))
    candidate_count = 0
    for code, prepared in prepared_frames.items():
        if prepared is None or prepared.empty:
            continue
        if pd.Timestamp(prepared["date"].iloc[-1]).date() != target:
            continue
        t = len(prepared) - 1
        if t < 1:
            continue
        o = float(prepared["open"].iloc[t])
        c = float(prepared["close"].iloc[t])
        hprev = float(prepared["high"].iloc[t-1])
        ma10 = float(prepared["ma10"].iloc[t])
        abase = float(prepared["abase"].iloc[t])
        tbase = float(prepared["tbase"].iloc[t])
        wup = bool(prepared["weekly_up"].iloc[t])

        if not (
            wup and np.isfinite(q) and np.isfinite(ma10)
            and np.isfinite(abase) and np.isfinite(tbase)
            and abase >= max(PARAM.amount_floor, q)
            and tbase >= PARAM.a_turn_min
            and c > o and c > hprev and c > ma10
        ):
            continue

        candidate_count += 1
        b, cdf = _bc_latest_for_one(prepared, code, market, target)
        if not b.empty:
            b_parts.append(b)
        if not cdf.empty:
            c_parts.append(cdf)

    sig = _combine_live(a_parts, b_parts, c_parts)
    replace_signals_for_date(db, sig, target, version=settings.live_strategy_version)

    run = LiveScanRun(
        data_date=target,
        status="ok",
        a_count=int((sig["engine"] == "A").sum()) if not sig.empty else 0,
        b_count=int((sig["engine"] == "B").sum()) if not sig.empty else 0,
        c_count=int((sig["engine"] == "C").sum()) if not sig.empty else 0,
        combined_count=len(sig),
        candidate_count=candidate_count,
        message="seed-from-full",
        finished_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    return sig


def persist_full_seed(
    db,
    market: MarketContext,
    peer_pool: pd.DataFrame,
    expected_peer_n: int | None = None,
) -> None:
    # Persist state only. Do not add live work while 4400 prepared frames remain resident.
    if market is None:
        raise RuntimeError("完整扫描没有捕获 MarketContext，不能初始化 V18 Live")
    peer_pool = peer_pool if peer_pool is not None else pd.DataFrame()
    if expected_peer_n is not None and int(expected_peer_n) != len(peer_pool):
        raise RuntimeError(
            f"A60 peer 捕获不一致：callback={len(peer_pool)}，engine={expected_peer_n}"
        )
    _replace_seed_state(db, market, peer_pool)


def run_live_scan(db, force: bool = False):
    active = db.execute(
        select(LiveScanRun)
        .where(LiveScanRun.status == "running")
        .order_by(LiveScanRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if active is not None:
        return pd.DataFrame(), {"blocked": True, "reason": f"已有日常扫描 #{active.id} 正在运行"}

    target = latest_trade_date(db)
    run = LiveScanRun(status="running", data_date=target)
    db.add(run)
    db.commit()
    db.refresh(run)

    try:
        _set_live_progress(db, run, 3, "完整性检查", "核验最新交易日与 V18 Live 状态")
        ready = scan_readiness(db, check_calendar=True)
        if not ready["scan_ready"]:
            run.status = "blocked"
            run.message = "数据完整性门：" + ready["scan_block_reason"]
            run.finished_at = datetime.utcnow()
            db.commit()
            return pd.DataFrame(), {"blocked": True, "reason": run.message}
        if target is None:
            raise RuntimeError("数据库没有最新交易日")

        state = live_state_status(db, target)
        if not state["ready"]:
            run.status = "blocked"
            run.message = (
                "V18 Live 状态未初始化或落后。请先运行一次完整V18扫描；"
                "之后每天只运行 Latest-only。"
            )
            run.finished_at = datetime.utcnow()
            db.commit()
            return pd.DataFrame(), {"blocked": True, "reason": run.message}

        if not force:
            done_run = db.execute(
                select(LiveScanRun)
                .where(
                    LiveScanRun.status == "ok",
                    LiveScanRun.data_date == target,
                    LiveScanRun.id != run.id,
                )
                .order_by(LiveScanRun.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if done_run is not None:
                run.status = "skipped"
                run.message = f"{target} V18 Live 已完成，无需重复运行"
                run.finished_at = datetime.utcnow()
                db.commit()
                return pd.DataFrame(), {"skipped": True, "reason": run.message}

        start_date = target - timedelta(days=settings.live_scan_calendar_days)
        _set_live_progress(db, run, 8, "市场横截面", f"计算 {target} q40 / breadth + 安全预筛")

        abase_values = []
        above_sum = 0.0
        above_n = 0
        mom_sum = 0.0
        mom_n = 0
        loose_peer = {}
        loose_final = {}

        for done, total, frames in iter_frame_batches(
            db,
            start_date=start_date,
            end_date=target,
            batch_size=settings.live_scan_batch_size,
            nonst_only=True,
        ):
            for code, raw in frames.items():
                m = _latest_raw_metrics(raw, target)
                if m is None:
                    continue
                if np.isfinite(m["abase"]):
                    abase_values.append(float(m["abase"]))
                if np.isfinite(m["above20"]):
                    above_sum += float(m["above20"])
                    above_n += 1
                if np.isfinite(m["mom20"]):
                    mom_sum += float(m["mom20"])
                    mom_n += 1

                code6 = str(code).zfill(6)
                if m["peer_common"]:
                    loose_peer[code6] = float(m["abase"])
                if m["final_common"]:
                    loose_final[code6] = float(m["abase"])

            pct = 8.0 + 32.0 * (done / max(1, total))
            rss = _rss_mb()
            mem = f"；峰值RSS {rss:.0f} MB" if rss is not None else ""
            _set_live_progress(
                db, run, pct, "市场横截面",
                f"已检查 {done}/{total}；peer宽筛 {len(loose_peer)}；"
                f"正式宽筛 {len(loose_final)}{mem}",
            )
            del frames
            gc.collect()

        if not abase_values:
            raise RuntimeError("目标日没有可用于 q40 的 ABASE 横截面")

        q40 = float(np.quantile(np.asarray(abase_values), 0.40, method="linear"))
        above20 = above_sum / above_n if above_n else np.nan
        mom20 = mom_sum / mom_n if mom_n else np.nan
        _upsert_target_market_state(db, target, q40, above20, mom20)

        market = _market_context_from_state(db, target)
        peer_n, peer_pos = _peer_stats(db, market, target)
        peer_codes = sorted([
            code for code, abase in loose_peer.items()
            if np.isfinite(abase) and abase >= max(PARAM.amount_floor, q40)
        ])
        candidate_codes = sorted([
            code for code, abase in loose_final.items()
            if np.isfinite(abase) and abase >= max(PARAM.amount_floor, q40)
        ])
        candidate_set = set(candidate_codes)
        run.candidate_count = len(candidate_codes)
        db.commit()

        _set_live_progress(
            db, run, 45, "Latest-only V18",
            f"q40={q40:,.0f}；A60 peer精算 {len(peer_codes)} 只；"
            f"正式A/B/C精算 {len(candidate_codes)} 只；历史peer={peer_n}",
        )

        a_parts, b_parts, c_parts, peer_events = [], [], [], []
        processed = 0
        total_work = max(1, len(peer_codes))

        for done, total, frames in iter_frame_batches(
            db,
            start_date=start_date,
            end_date=target,
            batch_size=max(20, min(settings.live_scan_batch_size, 80)),
            codes=peer_codes,
            nonst_only=True,
        ):
            for code, raw in frames.items():
                processed += 1
                prepared = prepare_stock(raw, code)
                if prepared.empty or pd.Timestamp(prepared["date"].iloc[-1]).date() != target:
                    continue
                if not bool(prepared["weekly_up"].iloc[-1]):
                    continue
                if not mainstream_ok(prepared, len(prepared)-1, market, PARAM):
                    continue

                # Keep the BROAD canonical A60 peer pool even when a stock fails
                # the stricter final A/B/C turnover gate.
                z60 = scan_a_window(prepared, code, 60, market, PARAM)
                if not z60.empty:
                    z60t = z60[pd.to_datetime(z60["date"]).dt.date == target]
                    for r in z60t.itertuples(index=False):
                        peer_events.append((str(r.code).zfill(6), target, float(r.buy)))

                if str(code).zfill(6) not in candidate_set:
                    continue

                a, _ = _a_latest_for_one(
                    prepared, code, market, target, peer_n, peer_pos
                )
                if not a.empty:
                    a_parts.append(a)

                b, c = _bc_latest_for_one(prepared, code, market, target)
                if not b.empty:
                    b_parts.append(b)
                if not c.empty:
                    c_parts.append(c)

                if processed % 10 == 0:
                    pct = 45.0 + 47.0 * (processed / total_work)
                    rss = _rss_mb()
                    mem = f"；峰值RSS {rss:.0f} MB" if rss is not None else ""
                    _set_live_progress(
                        db, run, pct, "Latest-only V18",
                        f"已精算 {processed}/{len(peer_codes)}；正式候选 {len(candidate_codes)}{mem}",
                    )
            del frames
            gc.collect()

        sig = _combine_live(a_parts, b_parts, c_parts)
        _set_live_progress(db, run, 94, "写入明日候选", f"正式最新日信号 {len(sig)} 条")
        replace_signals_for_date(db, sig, target, version=settings.live_strategy_version)
        _replace_target_peer_events(db, target, peer_events)

        run.a_count = int((sig["engine"] == "A").sum()) if not sig.empty else 0
        run.b_count = int((sig["engine"] == "B").sum()) if not sig.empty else 0
        run.c_count = int((sig["engine"] == "C").sum()) if not sig.empty else 0
        run.combined_count = len(sig)
        run.status = "ok"
        run.message = (
            f"Latest-only完成；q40={q40:,.0f}；candidate={len(candidate_codes)}；"
            f"peer={peer_n}/{peer_pos if np.isfinite(peer_pos) else 'nan'}；"
            f"A/B/C={run.a_count}/{run.b_count}/{run.c_count}"
        )
        run.finished_at = datetime.utcnow()
        db.commit()
        return sig, {
            "q40": q40,
            "candidate_count": len(candidate_codes),
            "peer_n": peer_n,
            "peer_pos": peer_pos,
            "A": run.a_count,
            "B": run.b_count,
            "C": run.c_count,
            "combined": run.combined_count,
        }
    except Exception as e:
        run.status = "error"
        run.message = str(e)
        run.finished_at = datetime.utcnow()
        db.commit()
        raise
