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

COMBOS = (
    ("A", "A", ("A",)),
    ("B", "B", ("B",)),
    ("C", "C", ("C",)),
    ("AB", "A+B", ("A", "B")),
    ("AC", "A+C", ("A", "C")),
    ("BC", "B+C", ("B", "C")),
    ("ALL", "A+B+C", ("A", "B", "C")),
)

DIAGNOSES = (
    ("DIRECT_FAIL", "买后直接失败", "MFE≤3% 且最终亏损"),
    ("GIVEBACK", "赚过但没守住", "MFE≥12%，最终兑现不足35%"),
    ("SOLD_RALLY", "卖后继续大涨", "退出后20日再涨≥12%"),
    ("EXCELLENT", "优秀趋势单", "最终≥8%、MFE≥12%、MAE>-8%"),
    ("HIGH_VOL", "高波动幸存", "MAE≤-12% 但最终盈利"),
)


def _combo_engines(combo: str):
    combo = (combo or "ALL").upper()
    mapping = {key: engines for key, _, engines in COMBOS}
    return mapping.get(combo, mapping["ALL"])


def _review_key(signal: Signal):
    return (
        signal.strategy_version,
        str(signal.code).zfill(6),
        signal.signal_date,
        signal.engine,
    )


def _signal_metrics(signal: Signal, bars: list[DailyBar]) -> dict:
    """Post-signal path metrics anchored to the historical V18 signal close.

    The V18 historical signal price is the signal-day close. MFE/MAE therefore
    begin on the next trading day, so signal-day intraday extremes that happened
    before the close-entry signal existed are not counted.
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
        "giveback": None,
        "capture": None,
        "post_exit_r10": None,
        "post_exit_r20": None,
        "spark_points": "",
    }
    if not bars:
        return out

    idx_by_date = {b.trade_date: i for i, b in enumerate(bars)}
    i = idx_by_date.get(signal.signal_date)
    if i is None:
        return out

    entry = float(signal.signal_close)
    exit_ret = getattr(signal, "exit_ret", None)

    if i + 1 < len(bars):
        out["next_open"] = float(bars[i + 1].open)
        out["next_open_gap"] = out["next_open"] / entry - 1

    for n, key in ((5, "r5"), (10, "r10"), (20, "r20")):
        j = i + n
        if j < len(bars):
            out[key] = float(bars[j].close) / entry - 1

    exit_i = None
    if signal.exit_date is not None and signal.exit_date in idx_by_date:
        exit_i = idx_by_date[signal.exit_date]
        end_i = exit_i
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

    if exit_ret is not None and out["mfe"] is not None:
        out["giveback"] = out["mfe"] - float(exit_ret)
        if out["mfe"] > 0 and float(exit_ret) > 0:
            out["capture"] = float(exit_ret) / out["mfe"]

    if exit_i is not None and exit_ret is not None:
        exit_price = entry * (1 + float(exit_ret))
        for n, key in ((10, "post_exit_r10"), (20, "post_exit_r20")):
            j = exit_i + n
            if j < len(bars) and exit_price > 0:
                out[key] = float(bars[j].close) / exit_price - 1

    # Tiny post-signal path for the list card. It is deliberately lightweight
    # SVG data rather than 62 ECharts instances.
    closes = [entry]
    for j in range(i + 1, min(len(bars), i + 21)):
        closes.append(float(bars[j].close))
    if len(closes) >= 2:
        vals = [v / entry - 1 for v in closes]
        lo, hi = min(vals), max(vals)
        span = max(hi - lo, 0.01)
        pts = []
        denom = max(1, len(vals) - 1)
        for k, v in enumerate(vals):
            x = 100 * k / denom
            y = 24 - 20 * (v - lo) / span
            pts.append(f"{x:.1f},{y:.1f}")
        out["spark_points"] = " ".join(pts)

    return out


def _diagnosis_flags(row: dict) -> list[str]:
    ret = row.get("exit_ret")
    m = row.get("metrics") or {}
    mfe = m.get("mfe")
    mae = m.get("mae")
    post20 = m.get("post_exit_r20")
    flags = []

    if ret is not None and ret < 0 and mfe is not None and mfe <= 0.03:
        flags.append("DIRECT_FAIL")
    if ret is not None and mfe is not None and mfe >= 0.12 and ret < 0.35 * mfe:
        flags.append("GIVEBACK")
    if post20 is not None and post20 >= 0.12:
        flags.append("SOLD_RALLY")
    if (
        ret is not None and ret >= 0.08
        and mfe is not None and mfe >= 0.12
        and (mae is None or mae > -0.08)
    ):
        flags.append("EXCELLENT")
    if ret is not None and ret > 0 and mae is not None and mae <= -0.12:
        flags.append("HIGH_VOL")
    return flags


def _diagnosis_labels(flags: list[str]) -> list[str]:
    names = {key: label for key, label, _ in DIAGNOSES}
    return [names[x] for x in flags if x in names]


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

    row = {
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
    row["diagnosis"] = _diagnosis_flags(row)
    row["diagnosis_labels"] = _diagnosis_labels(row["diagnosis"])
    row["visual"] = {
        "mfe_bar": min(100.0, abs(float(row["metrics"].get("mfe") or 0.0)) * 300.0),
        "mae_bar": min(100.0, abs(float(row["metrics"].get("mae") or 0.0)) * 300.0),
        "exit_bar": min(100.0, abs(float(row["exit_ret"] or 0.0)) * 300.0),
    }
    return row


def _summary(rows) -> dict:
    closed = [r for r in rows if r["exit_ret"] is not None]
    rets = [float(r["exit_ret"]) for r in closed]
    wins = [r for r in closed if r["exit_ret"] > 0]
    losses = [r for r in closed if r["exit_ret"] <= 0]
    pos_sum = sum(float(r["exit_ret"]) for r in wins)
    neg_sum = abs(sum(float(r["exit_ret"]) for r in losses))
    avg_win = sum(float(r["exit_ret"]) for r in wins) / len(wins) if wins else None
    avg_loss = sum(float(r["exit_ret"]) for r in losses) / len(losses) if losses else None

    mfes = [r["metrics"].get("mfe") for r in rows if r["metrics"].get("mfe") is not None]
    maes = [r["metrics"].get("mae") for r in rows if r["metrics"].get("mae") is not None]
    r5s = [r["metrics"].get("r5") for r in rows if r["metrics"].get("r5") is not None]
    r10s = [r["metrics"].get("r10") for r in rows if r["metrics"].get("r10") is not None]
    r20s = [r["metrics"].get("r20") for r in rows if r["metrics"].get("r20") is not None]

    mfe_pool = sum(max(0.0, float(x)) for x in mfes)
    capture = pos_sum / mfe_pool if mfe_pool > 0 else None

    sorted_rets = sorted(rets)
    if not sorted_rets:
        median = None
    elif len(sorted_rets) % 2:
        median = sorted_rets[len(sorted_rets)//2]
    else:
        k = len(sorted_rets)//2
        median = (sorted_rets[k-1] + sorted_rets[k]) / 2

    n = len(rows)
    if n < 5:
        sample_note = "样本极少"
    elif n < 15:
        sample_note = "样本偏少"
    else:
        sample_note = ""

    reviewed = [r for r in rows if r["reviewed"]]
    excellent_reviews = [r for r in reviewed if r["rating"] == "优秀"]

    return {
        "total": n,
        "A": sum(1 for r in rows if r["engine"] == "A"),
        "B": sum(1 for r in rows if r["engine"] == "B"),
        "C": sum(1 for r in rows if r["engine"] == "C"),
        "closed": len(closed),
        "wins": len(wins),
        "win_rate": len(wins) / len(closed) if closed else None,
        "avg_ret": sum(rets) / len(rets) if rets else None,
        "median_ret": median,
        "profit_factor": pos_sum / neg_sum if neg_sum > 0 else None,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "payoff": (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0) else None,
        "avg_mfe": sum(mfes) / len(mfes) if mfes else None,
        "avg_mae": sum(maes) / len(maes) if maes else None,
        "capture_rate": capture,
        "avg_r5": sum(r5s) / len(r5s) if r5s else None,
        "avg_r10": sum(r10s) / len(r10s) if r10s else None,
        "avg_r20": sum(r20s) / len(r20s) if r20s else None,
        "best": max(rets) if rets else None,
        "worst": min(rets) if rets else None,
        "net_signal_return": sum(rets) if rets else 0.0,
        "gross_profit": pos_sum,
        "gross_loss": neg_sum,
        "reviewed": len(reviewed),
        "excellent_review_rate": len(excellent_reviews) / len(reviewed) if reviewed else None,
        "sample_note": sample_note,
    }


def _filter_rows(rows, outcome="ALL", rating="ALL", diagnosis="ALL", engine="ALL"):
    outcome = (outcome or "ALL").upper()
    rating = rating or "ALL"
    diagnosis = (diagnosis or "ALL").upper()
    engine = (engine or "ALL").upper()

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

        if diagnosis != "ALL" and diagnosis not in r["diagnosis"]:
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


def _combo_matrix(all_rows):
    matrix = []
    for key, label, engines in COMBOS:
        subset = [r for r in all_rows if r["engine"] in engines]
        row = _summary(subset)
        row.update({"key": key, "label": label, "engines": "".join(engines)})
        matrix.append(row)
    return matrix


def _engine_contribution(all_rows):
    rows = []
    for e in "ABC":
        subset = [r for r in all_rows if r["engine"] == e]
        s = _summary(subset)
        rows.append({
            "engine": e,
            "total": s["total"],
            "net": s["net_signal_return"],
            "gross_profit": s["gross_profit"],
            "gross_loss": s["gross_loss"],
        })
    scale = max([abs(r["net"]) for r in rows] + [0.01])
    for r in rows:
        r["bar_pct"] = min(100.0, abs(r["net"]) / scale * 100)
    return rows


def _diagnosis_summary(rows):
    out = []
    for key, label, desc in DIAGNOSES:
        n = sum(1 for r in rows if key in r["diagnosis"])
        out.append({
            "key": key,
            "label": label,
            "desc": desc,
            "count": n,
            "rate": n / len(rows) if rows else 0.0,
        })
    return out


def review_index_data(
    db,
    combo="ALL",
    outcome="ALL",
    rating="ALL",
    diagnosis="ALL",
    sort="DATE_DESC",
):
    signals, stock_map, bar_map, review_map = _load_review_universe(db)
    all_rows = [_row_from_signal(s, stock_map, bar_map, review_map) for s in signals]

    matrix = _combo_matrix(all_rows)
    combo = (combo or "ALL").upper()
    valid_combo = {x[0] for x in COMBOS}
    if combo not in valid_combo:
        combo = "ALL"
    engines = set(_combo_engines(combo))
    combo_rows = [r for r in all_rows if r["engine"] in engines]

    filtered = _sort_rows(
        _filter_rows(combo_rows, outcome=outcome, rating=rating, diagnosis=diagnosis),
        sort=sort,
    )
    selected = next((x for x in matrix if x["key"] == combo), _summary(combo_rows))

    return {
        "rows": filtered,
        "all_summary": _summary(all_rows),
        "summary": selected,
        "matrix": matrix,
        "contribution": _engine_contribution(all_rows),
        "diagnosis_summary": _diagnosis_summary(combo_rows),
        "filtered_count": len(filtered),
        "ratings": RATINGS,
        "diagnoses": DIAGNOSES,
        "combos": COMBOS,
        "tag_options": TAG_OPTIONS,
        "filters": {
            "combo": combo,
            "outcome": outcome,
            "rating": rating,
            "diagnosis": diagnosis,
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

    chart["verticals"] = [{
        "date": signal.signal_date.isoformat(),
        "label": f"{signal.engine}信号",
        "engine": signal.engine,
        "kind": "buy",
    }]
    if signal.exit_date is not None:
        chart["verticals"].append({
            "date": signal.exit_date.isoformat(),
            "label": "退出",
            "engine": signal.engine,
            "kind": "sell",
        })
        chart["periods"] = [{
            "start": signal.signal_date.isoformat(),
            "end": signal.exit_date.isoformat(),
            "name": "策略持有期",
            "kind": "holding",
        }]
    else:
        chart["periods"] = []

    chart["focus"] = {
        "signal_id": signal.id,
        "code": signal.code,
        "engine": signal.engine,
        "signal_date": signal.signal_date.isoformat(),
    }
    return chart
