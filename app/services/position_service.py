
from __future__ import annotations
from datetime import date
import numpy as np
import pandas as pd
from sqlalchemy import select
from ..config import settings
from ..models import LivePosition, Signal, Stock
from .repository import load_frames_for_codes, latest_trade_date
from .execution_engine import _ensure_indicators, _exit_a, _exit_b, _exit_c
from .rc4_final import decision_index_for_date, rc4_a_runner_eligibility, rc4_a_tail_technical_exit

OFFICIAL_MAX_POSITIONS = 6

def _idx(df, d):
    a = pd.to_datetime(df["date"]).dt.date.to_numpy()
    hit = np.flatnonzero(a == d)
    return int(hit[0]) if len(hit) else None

def _timing(decision, latest, planned):
    if decision == latest:
        return "下一交易日开盘"
    if planned is not None and planned <= latest:
        return "已触发未执行 · 尽快处理"
    return "下一交易日开盘"

def evaluate_position(db, pos):
    stock = db.get(Stock, pos.code)
    raw = load_frames_for_codes(db, [pos.code]).get(pos.code)
    base = {"position":pos,"stock":stock,"action":"CHECK","action_label":"待检查","reason":"数据不足",
            "current_price":None,"pnl":None,"timing":"","decision_date":None,"planned_open":None,
            "proof_h":None,"ret5":None,"suggested_remaining":None}
    if raw is None or raw.empty:
        return base
    raw = raw.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    latest = pd.Timestamp(raw["date"].iloc[-1]).date()
    current = float(raw["close"].iloc[-1])
    base.update(current_price=current, pnl=current/pos.entry_price-1 if pos.entry_price>0 else None)
    sig = db.get(Signal, pos.signal_id)
    if sig is None:
        base["reason"]="原正式信号不存在"; return base

    if pos.stage == "TAIL":
        bi = decision_index_for_date(raw, pos.tail_decision_date) if pos.tail_decision_date else None
        if bi is None:
            base["reason"]="尾仓缺少原卖出决策日"; return base
        tx = rc4_a_tail_technical_exit(raw, bi)
        if tx.decision_date is None:
            base.update(action="HOLD",action_label="继续持有",reason="15% RC4尾仓：尚未连续3日收盘低于MA30")
            return base
        decision=tx.decision_date.date()
        planned=tx.exit_date.date() if tx.exit_date is not None else None
        base.update(action="SELL_ALL",action_label="卖出全部尾仓",reason="RC4：连续3日收盘低于MA30",
                    timing=_timing(decision,latest,planned),decision_date=decision,planned_open=tx.exit_open)
        return base

    df = _ensure_indicators(raw, pos.code)
    ei = _idx(df, pos.entry_date)
    if ei is None:
        base["reason"]="实际买入日在K线中找不到"; return base
    fn = _exit_a if pos.engine=="A" else _exit_b if pos.engine=="B" else _exit_c
    exi, _, reason, mfe = fn(sig, df, ei, pos.entry_price, True)
    if reason == "数据末端":
        H=float(sig.h_daily or 0)
        base.update(action="HOLD",action_label="继续持有",reason="正式动态卖出条件尚未触发",
                    proof_h=(mfe/H if H>0 and np.isfinite(mfe) else None))
        return base
    decision=df["date"].iloc[exi].date()
    planned = df["date"].iloc[exi+1].date() if exi+1<len(df) else None
    planned_open = float(df["open"].iloc[exi+1]) if exi+1<len(df) else None
    if pos.engine=="A":
        elig=rc4_a_runner_eligibility(float(sig.h_daily or 0), float(mfe), raw, exi)
        if elig.eligible:
            base.update(action="SELL85_KEEP15",action_label="卖85% · 留15%尾仓",
                        reason=f"{reason}；满足RC4成熟深洗条件",
                        timing=_timing(decision,latest,planned),decision_date=decision,
                        planned_open=planned_open,proof_h=elig.proof_h,ret5=elig.ret5,
                        suggested_remaining=max(1,int(round(pos.shares*.15))))
            return base
    base.update(action="SELL_ALL",action_label="全部卖出",reason=reason,
                timing=_timing(decision,latest,planned),decision_date=decision,planned_open=planned_open)
    return base

def position_dashboard(db):
    ps=db.execute(select(LivePosition).where(LivePosition.status=="OPEN").order_by(LivePosition.entry_date)).scalars().all()
    rows=[evaluate_position(db,p) for p in ps]
    latest=latest_trade_date(db)
    if latest and rows:
        fresh=db.execute(select(Signal).where(Signal.strategy_version==settings.live_strategy_version,
            Signal.signal_date==latest,Signal.engine.in_(("A","B")))).scalars().all()
        held={r["position"].code for r in rows}
        need=max(0,len(rows)+len({s.code for s in fresh if s.code not in held})-OFFICIAL_MAX_POSITIONS)
        tails=sorted([r for r in rows if r["position"].stage=="TAIL" and r["action"]=="HOLD"],
                     key=lambda r:r["pnl"] if r["pnl"] is not None else -999)
        for r in tails[:need]:
            r.update(action="SELL_ALL",action_label="尾仓让位 · 全部卖出",
                     reason="新A/B正式买点需要容量，RC4尾仓优先让位",timing="下一交易日开盘",decision_date=latest)
        need-=min(need,len(tails))
        if need>0:
            cs=sorted([r for r in rows if r["position"].engine=="C" and r["action"]=="HOLD"],
                      key=lambda r:r["pnl"] if r["pnl"] is not None else -999)
            for r in cs[:need]:
                r.update(action="SELL_ALL",action_label="C让位 · 全部卖出",
                         reason="新A/B正式买点需要容量，C卫星仓让位",timing="下一交易日开盘",decision_date=latest)
    return {"rows":rows,"open_count":len(rows),
            "action_count":sum(r["action"] in {"SELL_ALL","SELL85_KEEP15"} for r in rows),
            "market_value":sum((r["current_price"] or 0)*r["position"].shares for r in rows)}

def closed_positions(db, limit=50):
    return db.execute(select(LivePosition).where(LivePosition.status=="CLOSED")
        .order_by(LivePosition.exit_date.desc(),LivePosition.id.desc()).limit(limit)).scalars().all()
