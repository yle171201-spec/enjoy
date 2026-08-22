from __future__ import annotations

from datetime import datetime
import json
import math

from sqlalchemy import select

from ..models import DailyBar, Signal, SignalReview, Stock
from .chart_service import build_stock_chart


RATINGS = ("优秀", "合格", "勉强", "不该买")
TAG_OPTIONS = (
    "买点晚",
    "压力太近",
    "回踩不充分",
    "启动弱",
    "位置偏高",
    "卖早",
    "卖晚",
    "正常失败",
    "走势符合预期",
)


def _review_key(signal: Signal):
    return (
        signal.strategy_version,
        str(signal.code).zfill(6),
        signal.signal_date,
        signal.engine,
    )


def _signal_metrics(signal: Signal, bars: list[DailyBar]) -> dict:
    """Post-signal path metrics anchored to the historical V18 signal close.

    MFE/MAE begin on the NEXT trading day because the signal price is the
    signal-day close. This avoids using signal-day intraday extremes that
    occurred before the close-entry signal existed.
    """
    out = {
        "next_open": None,
        "next_open_gap": None,
        "r5": None,
        "r10": None,
        "r20": None,
        "mfe": None,
        "mae": None,
        "mfe_price": None,
        "mae_price": None,
        "mfe_date": None,
        "mae_date": None,
        "mfe_day": None,
        "mae_day": None,
        "hold_bars": None,
    }
    if not bars:
        return out

    idx_by_date = {b.trade_date: i for i, b in enumerate(bars)}
    i = idx_by_date.get(signal.signal_date)
    if i is None:
        return out

    entry = float(signal.signal_close)

    if i + 1 < len(bars):
        out["next_open"] = float(bars[i + 1].open)
        out["next_open_gap"] = out["next_open"] / entry - 1

    for n, key in ((5, "r5"), (10, "r10"), (20, "r20")):
        j = i + n
        if j < len(bars):
            out[key] = float(bars[j].close) / entry - 1

    if signal.exit_date is not None and signal.exit_date in idx_by_date:
        end_i = idx_by_date[signal.exit_date]
        out["hold_bars"] = max(0, end_i - i)
    else:
        end_i = min(len(bars) - 1, i + 20)

    path_start = i + 1
    if path_start <= end_i:
        window = bars[path_start:end_i + 1]
        if window:
            max_bar = max(window, key=lambda b: float(b.high))
            min_bar = min(window, key=lambda b: float(b.low))
            max_i = idx_by_date[max_bar.trade_date]
            min_i = idx_by_date[min_bar.trade_date]
            out.update({
                "mfe": float(max_bar.high) / entry - 1,
                "mae": float(min_bar.low) / entry - 1,
                "mfe_price": float(max_bar.high),
                "mae_price": float(min_bar.low),
                "mfe_date": max_bar.trade_date,
                "mae_date": min_bar.trade_date,
                "mfe_day": max_i - i,
                "mae_day": min_i - i,
            })

    return out


def _load_review_universe(db):
    signals = db.execute(
        select(Signal)
        .where(Signal.strategy_version == "V18")
        .order_by(Signal.signal_date, Signal.code, Signal.engine)
    ).scalars().all()

    if not signals:
        return [], {}, {}, {}

    codes = sorted({str(s.code).zfill(6) for s in signals})
    stocks = db.execute(
        select(Stock).where(Stock.code.in_(codes))
    ).scalars().all()
    stock_map = {s.code: s for s in stocks}

    all_bars = db.execute(
        select(DailyBar)
        .where(DailyBar.code.in_(codes))
        .order_by(DailyBar.code, DailyBar.trade_date)
    ).scalars().all()
    bar_map = {}
    for b in all_bars:
        bar_map.setdefault(str(b.code).zfill(6), []).append(b)

    reviews = db.execute(
        select(SignalReview).where(SignalReview.strategy_version == "V18")
    ).scalars().all()
    review_map = {
        (r.strategy_version, r.code, r.signal_date, r.engine): r
        for r in reviews
    }
    return signals, stock_map, bar_map, review_map


def _row_from_signal(signal, stock_map, bar_map, review_map):
    code = str(signal.code).zfill(6)
    review = review_map.get(_review_key(signal))
    try:
        tags = json.loads(review.tags_json or "[]") if review else []
        if not isinstance(tags, list):
            tags = []
    except Exception:
        tags = []

    return {
        "id": signal.id,
        "signal": signal,
        "code": code,
        "name": stock_map.get(code).name if stock_map.get(code) else "",
        "engine": signal.engine,
        "signal_date": signal.signal_date,
        "signal_close": signal.signal_close,
        "fail_price": signal.fail_price,
        "risk_pct": signal.risk_pct,
        "target_weight": signal.target_weight,
        "exit_date": signal.exit_date,
        "exit_ret": signal.exit_ret,
        "exit_reason": signal.exit_reason,
        "metrics": _signal_metrics(signal, bar_map.get(code, [])),
        "rating": review.rating if review else "",
        "tags": tags,
        "note": review.note if review else "",
        "reviewed": bool(review and review.rating),
        "review_updated_at": review.updated_at if review else None,
    }


def _filter_rows(rows, engine="ALL", outcome="ALL", rating="ALL"):
    engine = (engine or "ALL").upper()
    outcome = (outcome or "ALL").upper()
    rating = rating or "ALL"

    out = []
    for r in rows:
        if engine in {"A", "B", "C"} and r["engine"] != engine:
            continue

        ret = r["exit_ret"]
        if outcome == "WIN" and not (ret is not None and ret > 0):
            continue
        if outcome == "LOSS" and not (ret is not None and ret <= 0):
            continue
        if outcome == "OPEN" and ret is not None:
            continue

        if rating == "UNREVIEWED" and r["reviewed"]:
            continue
        if rating in RATINGS and r["rating"] != rating:
            continue
        out.append(r)
    return out


def _sort_rows(rows, sort="DATE_DESC"):
    sort = (sort or "DATE_DESC").upper()
    if sort == "RET_DESC":
        return sorted(rows, key=lambda r: (r["exit_ret"] is not None, r["exit_ret"] or -999), reverse=True)
    if sort == "RET_ASC":
        return sorted(rows, key=lambda r: (r["exit_ret"] is None, r["exit_ret"] if r["exit_ret"] is not None else 999))
    if sort == "MFE_DESC":
        return sorted(rows, key=lambda r: (r["metrics"].get("mfe") is not None, r["metrics"].get("mfe") or -999), reverse=True)
    if sort == "MAE_ASC":
        return sorted(rows, key=lambda r: (r["metrics"].get("mae") is None, r["metrics"].get("mae") if r["metrics"].get("mae") is not None else 999))
    if sort == "UNREVIEWED":
        return sorted(rows, key=lambda r: (r["reviewed"], -r["signal_date"].toordinal()))
    return sorted(rows, key=lambda r: (r["signal_date"], r["code"], r["engine"]), reverse=True)


def _summary(rows) -> dict:
    closed = [r for r in rows if r["exit_ret"] is not None]
    wins = [r for r in closed if r["exit_ret"] > 0]
    losses = [r for r in closed if r["exit_ret"] <= 0]
    pos_sum = sum(float(r["exit_ret"]) for r in wins)
    neg_sum = abs(sum(float(r["exit_ret"]) for r in losses))
    mfes = [r["metrics"].get("mfe") for r in rows if r["metrics"].get("mfe") is not None]
    maes = [r["metrics"].get("mae") for r in rows if r["metrics"].get("mae") is not None]

    return {
        "total": len(rows),
        "A": sum(1 for r in rows if r["engine"] == "A"),
        "B": sum(1 for r in rows if r["engine"] == "B"),
        "C": sum(1 for r in rows if r["engine"] == "C"),
        "closed": len(closed),
        "wins": len(wins),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_ret": sum(float(r["exit_ret"]) for r in closed) / len(closed) if closed else None,
        "profit_factor": pos_sum / neg_sum if neg_sum > 0 else None,
        "avg_mfe": sum(mfes) / len(mfes) if mfes else None,
        "avg_mae": sum(maes) / len(maes) if maes else None,
        "reviewed": sum(1 for r in rows if r["reviewed"]),
    }


def review_index_data(db, engine="ALL", outcome="ALL", rating="ALL", sort="DATE_DESC"):
    signals, stock_map, bar_map, review_map = _load_review_universe(db)
    all_rows = [_row_from_signal(s, stock_map, bar_map, review_map) for s in signals]
    filtered = _sort_rows(
        _filter_rows(all_rows, engine=engine, outcome=outcome, rating=rating),
        sort=sort,
    )
    return {
        "rows": filtered,
        "summary": _summary(all_rows),
        "filtered_count": len(filtered),
        "ratings": RATINGS,
        "tag_options": TAG_OPTIONS,
        "filters": {
            "engine": engine,
            "outcome": outcome,
            "rating": rating,
            "sort": sort,
        },
    }


def review_case_data(db, signal_id: int):
    signals, stock_map, bar_map, review_map = _load_review_universe(db)
    rows = [_row_from_signal(s, stock_map, bar_map, review_map) for s in signals]
    rows = sorted(rows, key=lambda r: (r["signal_date"], r["code"], r["engine"]))
    pos = next((i for i, r in enumerate(rows) if r["id"] == signal_id), None)
    if pos is None:
        return None
    return {
        "row": rows[pos],
        "position": pos + 1,
        "total": len(rows),
        "prev_id": rows[pos - 1]["id"] if pos > 0 else None,
        "next_id": rows[pos + 1]["id"] if pos + 1 < len(rows) else None,
        "reviewed": sum(1 for r in rows if r["reviewed"]),
        "ratings": RATINGS,
        "tag_options": TAG_OPTIONS,
    }


def save_signal_review(db, signal_id: int, rating: str, tags: list[str], note: str):
    signal = db.execute(
        select(Signal).where(
            Signal.id == signal_id,
            Signal.strategy_version == "V18",
        )
    ).scalar_one_or_none()
    if signal is None:
        raise ValueError("historical signal not found")

    rating = rating if rating in RATINGS else ""
    tags = [t for t in tags if t in TAG_OPTIONS]
    tags = list(dict.fromkeys(tags))
    note = (note or "").strip()[:4000]

    key = _review_key(signal)
    obj = db.execute(
        select(SignalReview).where(
            SignalReview.strategy_version == key[0],
            SignalReview.code == key[1],
            SignalReview.signal_date == key[2],
            SignalReview.engine == key[3],
        )
    ).scalar_one_or_none()

    if obj is None:
        obj = SignalReview(
            strategy_version=key[0],
            code=key[1],
            signal_date=key[2],
            engine=key[3],
        )
        db.add(obj)

    obj.rating = rating
    obj.tags_json = json.dumps(tags, ensure_ascii=False)
    obj.note = note
    obj.updated_at = datetime.utcnow()
    db.commit()
    return obj


def build_review_chart(db, signal_id: int, pre: int = 60, post: int = 40) -> dict:
    signal = db.execute(
        select(Signal).where(
            Signal.id == signal_id,
            Signal.strategy_version == "V18",
        )
    ).scalar_one_or_none()
    if signal is None:
        return {"bars": [], "signals": [], "lines": [], "areas": [], "markers": []}

    chart = build_stock_chart(
        db,
        signal.code,
        limit=max(120, pre + post + 1),
        focus_signal_id=signal.id,
        pre=max(20, min(120, int(pre))),
        post=max(20, min(120, int(post))),
    )

    bars = db.execute(
        select(DailyBar)
        .where(DailyBar.code == signal.code)
        .order_by(DailyBar.trade_date)
    ).scalars().all()
    m = _signal_metrics(signal, bars)

    if m.get("next_open") is not None:
        idx = next((i for i, b in enumerate(bars) if b.trade_date == signal.signal_date), None)
        if idx is not None and idx + 1 < len(bars):
            chart["markers"].append({
                "date": bars[idx + 1].trade_date.isoformat(),
                "price": m["next_open"],
                "label": "次开",
                "engine": signal.engine,
                "kind": "next",
            })
    if m.get("mfe_date") is not None:
        chart["markers"].append({
            "date": m["mfe_date"].isoformat(),
            "price": m["mfe_price"],
            "label": "MFE",
            "engine": signal.engine,
            "kind": "mfe",
        })
    if m.get("mae_date") is not None:
        chart["markers"].append({
            "date": m["mae_date"].isoformat(),
            "price": m["mae_price"],
            "label": "MAE",
            "engine": signal.engine,
            "kind": "mae",
        })

    chart["focus"] = {
        "signal_id": signal.id,
        "code": signal.code,
        "engine": signal.engine,
        "signal_date": signal.signal_date.isoformat(),
    }
    return chart
