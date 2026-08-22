from __future__ import annotations

import json
import numpy as np
import pandas as pd
from sqlalchemy import select

from ..models import DailyBar, Signal


def _meta(s: Signal) -> dict:
    try:
        return json.loads(s.metadata_json or "{}")
    except Exception:
        return {}


def _date_at(bars, idx):
    try:
        i = int(idx)
        if 0 <= i < len(bars):
            return bars[i].trade_date.isoformat()
    except Exception:
        pass
    return None


def build_stock_chart(db, code: str, limit: int = 520) -> dict:
    code = str(code).zfill(6)
    all_bars = db.execute(
        select(DailyBar).where(DailyBar.code == code).order_by(DailyBar.trade_date)
    ).scalars().all()
    if not all_bars:
        return {"bars": [], "signals": [], "lines": [], "areas": [], "markers": []}

    full_df = pd.DataFrame({
        "date": [b.trade_date for b in all_bars],
        "open": [b.open for b in all_bars], "high": [b.high for b in all_bars],
        "low": [b.low for b in all_bars], "close": [b.close for b in all_bars],
        "volume": [b.volume for b in all_bars],
    })
    full_df["ma10"] = full_df.close.rolling(10, min_periods=10).mean()
    full_df["ma20"] = full_df.close.rolling(20, min_periods=20).mean()

    start_idx = max(0, len(all_bars) - limit)
    shown = full_df.iloc[start_idx:].copy()

    signals = db.execute(
        select(Signal).where(Signal.code == code).order_by(Signal.signal_date)
    ).scalars().all()
    live_keys = {(s.signal_date, s.engine) for s in signals if s.strategy_version == "V18-LIVE"}
    signals = [s for s in signals if s.strategy_version == "V18-LIVE" or (s.signal_date, s.engine) not in live_keys]

    lines = []
    areas = []
    markers = []
    sig_rows = []

    for s in signals:
        m = _meta(s)
        sdate = s.signal_date.isoformat()
        sig_rows.append({
            "id": s.id, "date": sdate, "engine": s.engine,
            "price": s.signal_close, "fail": s.fail_price, "weight": s.target_weight,
            "risk": s.risk_pct, "h": s.h_daily, "p": s.p_level,
            "exit_date": s.exit_date.isoformat() if s.exit_date else None,
            "exit_ret": s.exit_ret, "exit_reason": s.exit_reason,
        })
        markers.append({"date": sdate, "price": s.signal_close, "label": f"{s.engine}买", "engine": s.engine, "kind": "buy"})
        markers.append({"date": sdate, "price": s.fail_price, "label": "FAIL", "engine": s.engine, "kind": "fail"})
        if s.exit_date and s.exit_ret is not None:
            exit_px = s.signal_close * (1 + s.exit_ret)
            markers.append({"date": s.exit_date.isoformat(), "price": exit_px, "label": "卖", "engine": s.engine, "kind": "sell"})

        if s.engine == "A":
            p = s.p_level if s.p_level is not None else m.get("P")
            break_d = _date_at(all_bars, m.get("break_i"))
            peak_d = _date_at(all_bars, m.get("peak_i"))
            if p is not None:
                lines.append({"name": "A压力P", "value": float(p), "start": peak_d or break_d or sdate, "end": sdate, "engine": "A"})
                if break_d:
                    areas.append({"name": "A回踩确认区", "start": break_d, "end": sdate, "low": float(p) * .96, "high": float(p) * 1.04, "engine": "A"})
                    try:
                        bi = int(m.get("break_i"))
                        markers.append({"date": _date_at(all_bars, bi), "price": float(all_bars[bi].close), "label": "突破", "engine": "A", "kind": "event"})
                    except Exception:
                        pass

        elif s.engine == "B":
            bs = m.get("board_start"); be = m.get("board_end")
            ds = _date_at(all_bars, bs); de = _date_at(all_bars, be)
            if ds and de:
                try:
                    i0, i1 = int(bs), int(be)
                    lo = min(float(all_bars[i].low) for i in range(i0, i1 + 1))
                    hi = max(float(all_bars[i].high) for i in range(i0, i1 + 1))
                    areas.append({"name": "B强启动", "start": ds, "end": de, "low": lo, "high": hi, "engine": "B"})
                    markers.append({"date": ds, "price": float(all_bars[i0].close), "label": "强启动", "engine": "B", "kind": "event"})
                except Exception:
                    pass
            if be is not None:
                try:
                    i0 = int(be) + 1
                    signal_idx = int(m.get("idx"))
                    if i0 < signal_idx:
                        lo = min(float(all_bars[i].low) for i in range(i0, signal_idx))
                        hi = max(float(all_bars[i].high) for i in range(i0, signal_idx))
                        areas.append({"name": "B首次回踩", "start": _date_at(all_bars, i0), "end": sdate, "low": lo, "high": hi, "engine": "B"})
                except Exception:
                    pass

        elif s.engine == "C":
            try:
                t = int(m.get("idx")); L = int(m.get("flag_days"))
                st = max(0, t - L); ed = max(st, t - 1)
                low = m.get("flag_low_level")
                if low is None:
                    low = min(float(all_bars[i].low) for i in range(st, t))
                high = m.get("mini_high")
                if high is None:
                    high = max(float(all_bars[i].high) for i in range(st, t))
                areas.append({"name": "C高位横盘", "start": _date_at(all_bars, st), "end": _date_at(all_bars, ed), "low": float(low), "high": float(high), "engine": "C"})
                lines.append({"name": "C小平台高", "value": float(high), "start": _date_at(all_bars, st), "end": sdate, "engine": "C"})
            except Exception:
                pass

    bars = []
    for r in shown.itertuples(index=False):
        bars.append({
            "date": r.date.isoformat(), "open": float(r.open), "close": float(r.close),
            "low": float(r.low), "high": float(r.high), "volume": float(r.volume),
            "ma10": None if pd.isna(r.ma10) else float(r.ma10),
            "ma20": None if pd.isna(r.ma20) else float(r.ma20),
        })

    # Trim annotations that end before displayed range.
    min_date = shown.iloc[0].date.isoformat()
    sig_rows = [x for x in sig_rows if x["date"] >= min_date or (x.get("exit_date") and x["exit_date"] >= min_date)]
    markers = [x for x in markers if x.get("date") and x["date"] >= min_date]
    lines = [x for x in lines if x.get("end", "9999") >= min_date]
    areas = [x for x in areas if x.get("end", "9999") >= min_date]

    return {"bars": bars, "signals": sig_rows, "lines": lines, "areas": areas, "markers": markers}
