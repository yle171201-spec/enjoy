from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
import numpy as np
import pandas as pd


# ============================================================
# V18 / V17 Reference Engine
# Purpose: GOLDEN REGRESSION, not optimization.
# Data contract:
#   date, open, high, low, close, volume, amount, turnover
# turnover MUST be decimal ratio: 5% == 0.05
# amount MUST be CNY yuan.
# ============================================================


@dataclass(frozen=True)
class P:
    pressure_windows: Tuple[int, ...] = (60, 80, 100)
    a_min_window_consensus: int = 2
    a_pre_atr_max: float = 0.035
    a_turn_min: float = 0.018
    a_peer_min_n: int = 5
    a_peer_pos_min: float = 0.50

    base_turn_min: float = 0.005
    amount_floor: float = 30_000_000.0

    break_pct: float = 0.01
    break_closepos_min: float = 0.65
    leave_pct: float = 0.03
    pull_depth_max: float = 0.18
    pull_vr_max: float = 1.40
    buy_over_p_max: float = 0.10

    b_turn_min: float = 0.020
    b_pull_depth_max: float = 0.12
    b_pull_vr_max: float = 0.60
    b_above20_min: float = 0.70
    b_mom20_min: float = 0.70

    c_turn_min: float = 0.020
    c_flag_dd_max: float = 0.08
    c_stock20_min: float = 0.10
    c_above20_min: float = 0.70
    c_mom20_min: float = 0.80
    c_buy_closepos_min: float = 0.70
    c_buy_vr20_min: float = 1.50

    evidence_days: int = 20
    max_weight: float = 0.20
    ab_risk_budget: float = 0.025
    c_risk_budget: float = 0.015


PARAM = P()


def norm_code(x) -> str:
    s = str(x).split(".")[0].strip()
    return s.zfill(6)


def eligible_code(code: str) -> bool:
    c = norm_code(code)
    if not c.startswith(("0", "3", "6")):
        return False
    if c.startswith(("688", "689")):
        return False
    return True


def ensure_frame(df: pd.DataFrame) -> pd.DataFrame:
    req = ["date","open","high","low","close","volume","amount","turnover"]
    miss = [x for x in req if x not in df.columns]
    if miss:
        raise ValueError(f"missing columns: {miss}")

    z = df.copy()

    if not np.issubdtype(z["date"].dtype, np.datetime64):
        # Supports integer days since unix epoch or normal date strings.
        if pd.api.types.is_integer_dtype(z["date"]):
            z["date"] = pd.to_datetime(z["date"], unit="D", origin="unix")
        else:
            z["date"] = pd.to_datetime(z["date"])

    z = z.sort_values("date").drop_duplicates("date").reset_index(drop=True)

    for c in ["open","high","low","close","volume","amount","turnover"]:
        z[c] = pd.to_numeric(z[c], errors="coerce")

    # Hard data-contract guard. Do not silently reinterpret percent turnover.
    med_turn = z["turnover"].dropna().median()
    if pd.notna(med_turn) and med_turn > 1.0:
        raise ValueError(
            "turnover appears to be percentage units. "
            "Convert 5% from 5.0 to 0.05 BEFORE running the strategy."
        )

    return z


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev).abs(),
        (df["low"] - prev).abs()
    ], axis=1).max(axis=1)


def weekly_up_series(df: pd.DataFrame) -> np.ndarray:
    """
    EXACT CONCEPT:
    Daily T uses the week strictly BEFORE T's current trading week.
    Weekly close = last daily close in exchange week.
    """
    d = df["date"]
    period = d.dt.to_period("W-FRI")

    tmp = pd.DataFrame({"week": period, "close": df["close"].to_numpy()})
    wk = tmp.groupby("week", sort=True)["close"].last().to_frame("wclose")
    wk["wma10"] = wk["wclose"].rolling(10, min_periods=10).mean()
    wk["wma20"] = wk["wclose"].rolling(20, min_periods=20).mean()
    wk["wup"] = (
        (wk["wclose"] > wk["wma10"]) &
        (wk["wma10"] > wk["wma20"])
    )

    # Use previous period, NEVER current unfinished week.
    prev_period = period - 1
    mapped = pd.Series(prev_period).map(wk["wup"])
    out = mapped.map(lambda x: bool(x) if pd.notna(x) else False).to_numpy(bool)
    return out


def prepare_stock(df: pd.DataFrame, code: str) -> pd.DataFrame:
    z = ensure_frame(df)
    z["code"] = norm_code(code)

    z["ma10"] = z["close"].rolling(10, min_periods=10).mean()
    z["ma20"] = z["close"].rolling(20, min_periods=20).mean()
    z["ma60"] = z["close"].rolling(60, min_periods=60).mean()
    z["atr20"] = true_range(z).rolling(20, min_periods=20).mean()

    z["abase"] = (
        z["amount"].shift(20).rolling(230, min_periods=120).median()
    )
    z["tbase"] = (
        z["turnover"].shift(20).rolling(230, min_periods=120).median()
    )

    z["stock20"] = z["close"] / z["close"].shift(20) - 1
    z["above20_flag"] = z["close"] > z["ma20"]
    z["mom20_flag"] = z["stock20"] > 0

    z["r5"] = z["close"] / z["close"].shift(5) - 1

    delta = z["close"].diff()
    den = delta.abs().rolling(10, min_periods=10).sum()
    z["er10"] = (z["close"] - z["close"].shift(10)) / den.replace(0, np.nan)

    z["weekly_up"] = weekly_up_series(z)
    z["board"] = board_mask(z, code)
    return z


def board_mask(df: pd.DataFrame, code: str) -> np.ndarray:
    c = norm_code(code)
    th = 0.19 if c.startswith("3") else 0.095
    r = df["close"] / df["close"].shift(1) - 1
    return (r >= th).fillna(False).to_numpy(bool)


@dataclass
class MarketContext:
    calendar: pd.DatetimeIndex
    cal_pos: Dict[pd.Timestamp, int]
    q40: pd.Series
    above20: pd.Series
    mom20: pd.Series

    def prev_breadth(self, date) -> Tuple[float,float]:
        d = pd.Timestamp(date)
        p = self.cal_pos[d]
        if p <= 0:
            return np.nan, np.nan
        pd0 = self.calendar[p-1]
        return float(self.above20.get(pd0, np.nan)), float(self.mom20.get(pd0, np.nan))


def build_market_context(stocks: Dict[str,pd.DataFrame]) -> MarketContext:
    """
    stocks must already be prepare_stock() frames.
    q40 is cross-sectional 40th percentile of ABASE on each date.
    """
    rows = []
    for code, df in stocks.items():
        if not eligible_code(code):
            continue
        x = df[["date","abase","above20_flag","mom20_flag","ma20","stock20"]].copy()
        rows.append(x)

    x = pd.concat(rows, ignore_index=True)
    calendar = pd.DatetimeIndex(sorted(x["date"].dropna().unique()))
    cal_pos = {pd.Timestamp(d): i for i,d in enumerate(calendar)}

    q40 = (
        x.dropna(subset=["abase"])
         .groupby("date")["abase"]
         .quantile(.40)
         .reindex(calendar)
    )

    # Denominators naturally include only rows whose indicator is finite/available.
    x20 = x.dropna(subset=["ma20"])
    above20 = x20.groupby("date")["above20_flag"].mean().reindex(calendar)

    xm = x.dropna(subset=["stock20"])
    mom20 = xm.groupby("date")["mom20_flag"].mean().reindex(calendar)

    return MarketContext(calendar, cal_pos, q40, above20, mom20)



def build_market_context_compact(stocks: Dict[str,pd.DataFrame]) -> MarketContext:
    """Memory-compact equivalent of build_market_context()."""
    eligible = [(code, df) for code, df in stocks.items() if eligible_code(code)]
    if not eligible:
        raise ValueError("no eligible stocks for market context")

    date_chunks = [
        df["date"].to_numpy(dtype="datetime64[ns]", copy=False)
        for _, df in eligible
        if len(df)
    ]
    if not date_chunks:
        raise ValueError("no dates for market context")

    all_dates = np.concatenate(date_chunks)
    calendar = pd.DatetimeIndex(np.unique(all_dates))
    del all_dates, date_chunks

    cal_pos = {pd.Timestamp(d): i for i, d in enumerate(calendar)}
    n_dates = len(calendar)
    n_stocks = len(eligible)

    abase_matrix = np.full((n_dates, n_stocks), np.nan, dtype=np.float64)
    above_sum = np.zeros(n_dates, dtype=np.float64)
    above_n = np.zeros(n_dates, dtype=np.int64)
    mom_sum = np.zeros(n_dates, dtype=np.float64)
    mom_n = np.zeros(n_dates, dtype=np.int64)

    for j, (_, df) in enumerate(eligible):
        dates = pd.DatetimeIndex(df["date"])
        pos = calendar.get_indexer(dates)
        ok = pos >= 0
        if not np.any(ok):
            continue
        pp = pos[ok]

        abase = df["abase"].to_numpy(dtype=float, copy=False)[ok]
        abase_matrix[pp, j] = abase

        close = df["close"].to_numpy(dtype=float, copy=False)[ok]
        ma20 = df["ma20"].to_numpy(dtype=float, copy=False)[ok]
        above = close > ma20
        valid = np.isfinite(ma20)
        if np.any(valid):
            np.add.at(above_sum, pp[valid], above[valid].astype(np.float64))
            np.add.at(above_n, pp[valid], 1)

        # Same formula as prepare_stock()["stock20"], derived on demand so the
        # full-market live frame does not retain stock20/mom20_flag.
        full_close = df["close"].to_numpy(dtype=float, copy=False)
        stock20_full = np.full(len(full_close), np.nan, dtype=float)
        if len(full_close) > 20:
            stock20_full[20:] = full_close[20:] / full_close[:-20] - 1
        stock20 = stock20_full[ok]
        mom = stock20 > 0
        valid = np.isfinite(stock20)
        if np.any(valid):
            np.add.at(mom_sum, pp[valid], mom[valid].astype(np.float64))
            np.add.at(mom_n, pp[valid], 1)

    with np.errstate(all="ignore"):
        qvals = np.nanquantile(abase_matrix, 0.40, axis=1, method="linear")
    q40 = pd.Series(qvals, index=calendar)
    del abase_matrix

    above_vals = np.full(n_dates, np.nan, dtype=float)
    mom_vals = np.full(n_dates, np.nan, dtype=float)
    np.divide(above_sum, above_n, out=above_vals, where=above_n > 0)
    np.divide(mom_sum, mom_n, out=mom_vals, where=mom_n > 0)

    above20 = pd.Series(above_vals, index=calendar)
    mom20 = pd.Series(mom_vals, index=calendar)
    return MarketContext(calendar, cal_pos, q40, above20, mom20)




def trim_live_prepared_frame(df: pd.DataFrame) -> pd.DataFrame:
    # Live memory-safe mode only: drop columns redundant after prepare_stock().
    drop = [
        "code", "amount", "ma60",
        "stock20", "above20_flag", "mom20_flag",
        "r5", "er10",
    ]
    return df.drop(columns=[c for c in drop if c in df.columns])


def ensure_exit_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # Restore the exact canonical exit-only indicators lazily for signal stocks.
    if "r5" not in df.columns:
        df["r5"] = df["close"] / df["close"].shift(5) - 1
    if "er10" not in df.columns:
        delta = df["close"].diff()
        den = delta.abs().rolling(10, min_periods=10).sum()
        df["er10"] = (
            (df["close"] - df["close"].shift(10))
            / den.replace(0, np.nan)
        )
    return df


def mainstream_ok(df: pd.DataFrame, t: int, market: MarketContext, p: P=PARAM) -> bool:
    d = pd.Timestamp(df["date"].iloc[t])
    abase = df["abase"].iloc[t]
    tbase = df["tbase"].iloc[t]
    q = market.q40.get(d, np.nan)
    if not np.isfinite(abase) or not np.isfinite(tbase) or not np.isfinite(q):
        return False
    return (abase >= max(p.amount_floor, q)) and (tbase >= p.base_turn_min)


def safe_closepos(o,h,l,c) -> float:
    rng = h-l
    return (c-l)/rng if rng > 0 else .5


# ============================================================
# Engine A
# ============================================================

def scan_a_window(
    df: pd.DataFrame,
    code: str,
    W: int,
    market: MarketContext,
    p: P=PARAM
) -> pd.DataFrame:
    """
    Broad A structure for one W.
    NO global market breadth.
    NO high-quality pre20_atr / 1.8% / peer gate here.
    """
    code = norm_code(code)

    o=df["open"].to_numpy(float); h=df["high"].to_numpy(float)
    l=df["low"].to_numpy(float); c=df["close"].to_numpy(float)
    v=df["volume"].to_numpy(float)
    ma10=df["ma10"].to_numpy(float); atr20=df["atr20"].to_numpy(float)
    wup=df["weekly_up"].to_numpy(bool)

    n=len(df)
    rows=[]
    last_sig=-999

    for t in range(max(140,W+25), n):
        if t-last_sig < 8:
            continue
        if not wup[t]:
            continue
        if not mainstream_ok(df,t,market,p):
            continue
        if not (c[t] > o[t] and c[t] > h[t-1] and c[t] > ma10[t]):
            continue

        found=None

        # t-3 ... t-15 inclusive, nearest first.
        lower=max(W, t-15)
        for bidx in range(t-3, lower-1, -1):
            if bidx-W < 0:
                continue

            pre=np.arange(bidx-W,bidx)
            P0=float(np.max(h[pre]))
            pidx=int(pre[np.argmax(h[pre])])
            age=bidx-pidx

            if age < 8:
                continue
            if np.max(c[pidx+1:bidx]) > P0*1.01:
                continue

            cp=safe_closepos(o[bidx],h[bidx],l[bidx],c[bidx])
            medv=np.median(v[max(0,bidx-20):bidx])
            break_vr=v[bidx]/medv if medv>0 else np.nan

            if not (c[bidx] >= P0*(1+p.break_pct) and cp >= p.break_closepos_min):
                continue

            pr=np.arange(bidx+1,t)
            if len(pr)<2:
                continue

            max_after=float(np.max(h[bidx:t]))
            if max_after < P0*(1+p.leave_pct):
                continue

            qidx=int(pr[np.argmin(l[pr])])
            if not (
                l[qidx] <= P0*1.04
                and np.min(c[pr]) >= P0*.96
                and c[t] >= P0
            ):
                continue

            depth=1-l[qidx]/max_after
            if depth > p.pull_depth_max:
                continue

            denom=np.median(v[max(0,bidx-2):bidx+1])
            pull_vr=np.median(v[pr])/denom if denom>0 else np.nan
            buy_overP=c[t]/P0-1

            if not np.isfinite(pull_vr):
                continue
            if pull_vr > p.pull_vr_max or buy_overP > p.buy_over_p_max:
                continue

            pp=np.arange(max(20,bidx-20),bidx)
            prevol=float(np.nanmedian(atr20[pp]/c[pp])) if len(pp) else np.nan

            found=dict(
                code=code, date=df["date"].iloc[t], idx=t, W=W,
                buy=float(c[t]), break_i=bidx, peak_i=pidx, P=P0,
                res_age=age, break_closepos=cp, break_vr20=break_vr,
                depth=float(depth), pull_vr=float(pull_vr),
                leave=float(max_after/P0-1),
                pull_days=int(t-bidx), buy_overP=float(buy_overP),
                abase=float(df["abase"].iloc[t]),
                turn_base=float(df["tbase"].iloc[t]),
                pre20_atr=prevol
            )
            break

        if found is not None:
            rows.append(found)
            last_sig=t

    z=pd.DataFrame(rows)
    if len(z):
        z=(
            z.sort_values(["code","break_i","idx"])
             .drop_duplicates(["code","break_i"],keep="first")
             .reset_index(drop=True)
        )
    return z


def build_a_peer_pool(
    stocks: Dict[str,pd.DataFrame],
    market: MarketContext,
    p: P=PARAM
) -> pd.DataFrame:
    """
    Canonical reconstruction of the historical `noenv pure-daily` peer pool.

    IMPORTANT:
    This is the only intermediate pool that was not persisted separately.
    If A final event diff remains after everything else matches,
    debug THIS module only against the Golden A78 set.
    """
    parts=[]
    for code, df in stocks.items():
        if not eligible_code(code):
            continue
        z=scan_a_window(df,code,60,market,p)
        if len(z):
            parts.append(z)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts,ignore_index=True)


def _close_asof(df: pd.DataFrame, date: pd.Timestamp) -> float:
    a=df["date"].to_numpy("datetime64[ns]")
    j=np.searchsorted(a, np.datetime64(date), side="right")-1
    if j<0:
        return np.nan
    return float(df["close"].iloc[j])


def build_a_peer_state(
    target_dates: List[pd.Timestamp],
    peer_pool: pd.DataFrame,
    stocks: Dict[str,pd.DataFrame],
    market: MarketContext
) -> Dict[pd.Timestamp,Tuple[int,float]]:
    """
    Peer signal entry dates: T-20 ... T-3 trading-day positions inclusive.
    Mark each at T-1 close/asof close.
    """
    by_pos: Dict[int,List[Tuple[str,pd.Timestamp,float]]] = {}

    for r in peer_pool.itertuples(index=False):
        d=pd.Timestamp(r.date)
        if d not in market.cal_pos:
            continue
        pos=market.cal_pos[d]
        by_pos.setdefault(pos,[]).append((norm_code(r.code),d,float(r.buy)))

    out={}
    for d0 in sorted(set(pd.Timestamp(x) for x in target_dates)):
        pos=market.cal_pos[d0]
        if pos<=0:
            out[d0]=(0,np.nan)
            continue

        prevdate=market.calendar[pos-1]
        peers=[]
        for pp in range(max(0,pos-20), max(0,pos-3)+1):
            peers.extend(by_pos.get(pp,[]))

        rr=[]
        for code,entry_date,buy in peers:
            if code not in stocks:
                continue
            px=_close_asof(stocks[code],prevdate)
            if np.isfinite(px) and buy>0:
                rr.append(px/buy-1)

        a=np.asarray(rr,float)
        out[d0]=(len(a), float(np.mean(a>0)) if len(a) else np.nan)

    return out


def scan_a_final(
    stocks: Dict[str,pd.DataFrame],
    market: MarketContext,
    p: P=PARAM
) -> Tuple[pd.DataFrame,pd.DataFrame]:
    """
    Returns:
      final A78-like signals
      peer_pool used for local-mainline gate
    """
    raw=[]
    for code,df in stocks.items():
        if not eligible_code(code):
            continue
        for W in p.pressure_windows:
            z=scan_a_window(df,code,W,market,p)
            if len(z):
                raw.append(z)

    if not raw:
        return pd.DataFrame(),pd.DataFrame()

    raw=pd.concat(raw,ignore_index=True)

    # High-quality gates BEFORE peer.
    q=raw[
        (raw["pre20_atr"]<=p.a_pre_atr_max) &
        (raw["turn_base"]>=p.a_turn_min)
    ].copy()

    peer_pool=build_a_peer_pool(stocks,market,p)
    pst=build_a_peer_state(q["date"].tolist(),peer_pool,stocks,market)

    q["peer_n"]=[pst[pd.Timestamp(d)][0] for d in q["date"]]
    q["peer_pos"]=[pst[pd.Timestamp(d)][1] for d in q["date"]]

    q=q[
        (q["peer_n"]>=p.a_peer_min_n) &
        (q["peer_pos"]>=p.a_peer_pos_min)
    ].copy()

    # Multi-scale exact same code/date consensus.
    out=[]
    for (code,date),g in q.groupby(["code","date"],sort=False):
        if g["W"].nunique() < p.a_min_window_consensus:
            continue
        gg=g.sort_values("W").reset_index(drop=True)
        # Historical canonical-row rule:
        rep=gg.iloc[len(gg)//2].copy()
        rep["window_count"]=g["W"].nunique()
        rep["windows"]=",".join(map(str,sorted(g["W"].unique())))
        out.append(rep.to_dict())

    return pd.DataFrame(out),peer_pool


# ============================================================
# Engine B
# ============================================================

def scan_b_broad(df: pd.DataFrame, code: str, market: MarketContext, p: P=PARAM) -> pd.DataFrame:
    code=norm_code(code)
    o=df["open"].to_numpy(float);h=df["high"].to_numpy(float)
    l=df["low"].to_numpy(float);c=df["close"].to_numpy(float)
    v=df["volume"].to_numpy(float)
    ma10=df["ma10"].to_numpy(float)
    wup=df["weekly_up"].to_numpy(bool)
    board=df["board"].to_numpy(bool)

    n=len(df); rows=[]; last_entry=-999
    s=20

    while s<n-35:
        if not board[s]:
            s+=1
            continue

        start=s
        end=s
        while end+1<n and board[end+1]:
            end+=1

        if start<20 or c[start] < np.max(h[start-20:start])*.995:
            s=end+1
            continue

        if np.any(board[max(0,start-8):start]):
            s=end+1
            continue

        for t in range(end+2,min(n-35,end+11)):
            if t-last_entry<5:
                continue
            if not wup[t] or not mainstream_ok(df,t,market,p):
                continue

            pr=np.arange(end+1,t)
            if len(pr)<1:
                continue
            if not np.any(c[pr] < o[pr]):
                continue

            act_high=float(np.max(h[start:end+1]))
            dd=1-np.min(l[pr])/act_high
            if dd>.18:
                continue

            if np.min(c[pr]) < c[start]*.90:
                continue

            actv=np.median(v[start:end+1])
            pullvr=np.median(v[pr])/actv if actv>0 else np.nan
            if not np.isfinite(pullvr) or pullvr>1.0:
                continue

            if not (c[t]>o[t] and c[t]>h[t-1] and c[t]>ma10[t]):
                continue

            if c[t] > c[end]*1.25:
                continue

            a20,m20=market.prev_breadth(df["date"].iloc[t])

            rows.append(dict(
                code=code,date=df["date"].iloc[t],idx=t,buy=float(c[t]),
                board_start=start,board_end=end,boards=end-start+1,
                pull_days=t-end,pull_dd=float(dd),pull_vr=float(pullvr),
                turn_base=float(df["tbase"].iloc[t]),
                above20_prev=a20,mom20_prev=m20
            ))
            last_entry=t
            break

        s=end+1

    return pd.DataFrame(rows)


def scan_b_final(stocks: Dict[str,pd.DataFrame], market: MarketContext, p: P=PARAM) -> pd.DataFrame:
    parts=[]
    for code,df in stocks.items():
        if not eligible_code(code):
            continue
        z=scan_b_broad(df,code,market,p)
        if len(z):
            parts.append(z)

    if not parts:
        return pd.DataFrame()

    z=pd.concat(parts,ignore_index=True)
    z=z[
        (z["turn_base"]>=p.b_turn_min) &
        (z["pull_dd"]<=p.b_pull_depth_max) &
        (z["pull_vr"]<=p.b_pull_vr_max) &
        (z["above20_prev"]>=p.b_above20_min) &
        (z["mom20_prev"]>=p.b_mom20_min)
    ].copy()

    return z.reset_index(drop=True)


# ============================================================
# Engine C
# ============================================================

def scan_c_broad(df: pd.DataFrame, code: str, market: MarketContext, p: P=PARAM) -> pd.DataFrame:
    code=norm_code(code)
    o=df["open"].to_numpy(float);h=df["high"].to_numpy(float)
    l=df["low"].to_numpy(float);c=df["close"].to_numpy(float)
    v=df["volume"].to_numpy(float)
    ma10=df["ma10"].to_numpy(float);ma20=df["ma20"].to_numpy(float)
    atr20=df["atr20"].to_numpy(float)
    wup=df["weekly_up"].to_numpy(bool)
    board=df["board"].to_numpy(bool)

    n=len(df);rows=[];last=-999

    for t in range(180,n-35):
        if t-last<7:
            continue
        if not wup[t] or not mainstream_ok(df,t,market,p):
            continue

        if not (c[t]>o[t] and c[t]>h[t-1] and c[t]>ma10[t] and c[t]>ma20[t]):
            continue

        found=None

        for L in [5,6,7,8,9,10]:
            s=t-L
            if s<80:
                continue
            cons=np.arange(s,t)
            if len(cons)<5:
                continue

            prehi=float(np.max(h[max(20,s-10):s+1]))
            if prehi<=0:
                continue

            cons_low=float(np.min(l[cons]))
            dd=1-cons_low/prehi
            if dd>.12:
                continue

            close_range=(np.max(c[cons])-np.min(c[cons]))/prehi
            if close_range>.08:
                continue

            if np.mean(c[cons]>=ma10[cons])<.60:
                continue
            if np.min(c[cons]-ma20[cons]) < -.015*prehi:
                continue

            lr=np.arange(max(30,s-50),max(30,s-8))
            if len(lr)<10:
                continue

            low_i=int(lr[np.argmin(l[lr])])
            low0=float(l[low_i])
            advance=prehi/low0-1
            if advance<.20:
                continue

            if cons_low < low0+.55*(prehi-low0):
                continue

            imp=np.arange(max(low_i,s-10),s)
            if len(imp)<5:
                continue

            impv=float(np.median(v[imp]))
            conv=float(np.median(v[cons]))
            vol_contract=conv/impv if impv>0 else np.nan
            if not np.isfinite(vol_contract) or vol_contract>1.05:
                continue

            cons_atr=float(np.nanmedian(atr20[cons]/c[cons]))
            ps=max(20,s-20)
            prior_atr=float(np.nanmedian(atr20[ps:s]/c[ps:s]))
            atr_contract=cons_atr/prior_atr if prior_atr>0 else np.nan
            if not np.isfinite(atr_contract) or atr_contract>1.10:
                continue

            mini_high=float(np.max(h[cons]))
            if c[t] < mini_high*1.002:
                continue

            ext20=c[t]/ma20[t]-1
            if not np.isfinite(ext20) or ext20>.22:
                continue

            if board[t]:
                continue

            a20,m20=market.prev_breadth(df["date"].iloc[t])
            buy_cp=safe_closepos(o[t],h[t],l[t],c[t])
            med20v=np.median(v[max(0,t-20):t])
            buy_vr20=v[t]/med20v if med20v>0 else np.nan
            stock20=c[t]/c[t-20]-1 if t>=20 else np.nan

            found=dict(
                code=code,date=df["date"].iloc[t],idx=t,buy=float(c[t]),
                flag_days=L,prehi=prehi,advance=float(advance),
                flag_dd=float(dd),flag_close_range=float(close_range),
                vol_contract=float(vol_contract),atr_contract=float(atr_contract),
                ext20=float(ext20),turn_base=float(df["tbase"].iloc[t]),
                above20_prev=a20,mom20_prev=m20,mini_high=mini_high,
                impulse_low_i=low_i,buy_closepos=float(buy_cp),
                buy_vr20=float(buy_vr20),stock20=float(stock20)
            )
            break

        if found is not None:
            rows.append(found)
            last=t

    return pd.DataFrame(rows)


def scan_c_final(stocks: Dict[str,pd.DataFrame], market: MarketContext, p: P=PARAM) -> pd.DataFrame:
    parts=[]
    for code,df in stocks.items():
        if not eligible_code(code):
            continue
        z=scan_c_broad(df,code,market,p)
        if len(z):
            parts.append(z)

    if not parts:
        return pd.DataFrame()

    z=pd.concat(parts,ignore_index=True)
    z=z[
        (z["turn_base"]>=p.c_turn_min) &
        (z["flag_dd"]<=p.c_flag_dd_max) &
        (z["stock20"]>=p.c_stock20_min) &
        (z["above20_prev"]>=p.c_above20_min) &
        (z["mom20_prev"]>=p.c_mom20_min) &
        (z["buy_closepos"]>=p.c_buy_closepos_min) &
        (z["buy_vr20"]>=p.c_buy_vr20_min)
    ].copy()

    return z.reset_index(drop=True)


# ============================================================
# H / Cost50 / exits
# ============================================================

def hswing_daily(df: pd.DataFrame, t: int) -> float:
    h=df["high"].to_numpy(float);l=df["low"].to_numpy(float)
    depths=[]
    st=max(20,t-80)

    for j in range(st,t-5):
        left=max(st,j-3)
        right=min(t,j+4)
        if h[j] >= np.max(h[left:right]):
            low10=np.min(l[j+1:min(t,j+11)])
            dep=1-low10/h[j]
            if .02<=dep<=.30:
                depths.append(dep)

    if len(depths)>=3:
        return float((np.median(depths)+np.max(depths))/2)
    return np.nan


def weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    if len(values)==0 or np.sum(weights)<=0:
        return np.nan
    ix=np.argsort(values)
    v=values[ix];w=weights[ix]
    cs=np.cumsum(w)
    return float(v[np.searchsorted(cs,cs[-1]*.5,side="left")])


def cost50_proxy(df: pd.DataFrame) -> np.ndarray:
    """
    Survival-turnover proxy used in research, not real TDX COST/WINNER.
    """
    h=df["high"].to_numpy(float);l=df["low"].to_numpy(float)
    c=df["close"].to_numpy(float);turn=df["turnover"].fillna(0).to_numpy(float)

    prices=np.empty(0,float)
    weights=np.empty(0,float)
    out=np.full(len(df),np.nan,float)

    for i in range(len(df)):
        tr=float(np.clip(turn[i],0,1))
        if len(weights):
            weights *= (1-tr)
        tp=(h[i]+l[i]+c[i])/3
        if tr>0:
            prices=np.append(prices,tp)
            weights=np.append(weights,tr)

        s=weights.sum()
        if s>0:
            # normalize only for numerical stability
            weights /= s
            out[i]=weighted_median(prices,weights)

        # prune numerically irrelevant ancient chips
        if len(weights)>2000:
            keep=weights>1e-8
            prices=prices[keep]
            weights=weights[keep]
            if weights.sum()>0:
                weights/=weights.sum()

    return out


def ensure_cost50(df: pd.DataFrame) -> pd.DataFrame:
    if "cost50" not in df.columns:
        df=df.copy()
        df["cost50"]=cost50_proxy(df)
    return df


def _h_for_setup(df: pd.DataFrame,t: int,hsetup: float) -> Tuple[float,float,float]:
    hs=hswing_daily(df,t)
    H=float((hs+hsetup)/2) if np.isfinite(hs) else float(hsetup)
    return hs,float(hsetup),H


def attach_a_risk(a: pd.DataFrame, stocks: Dict[str,pd.DataFrame]) -> pd.DataFrame:
    out=[]
    for r in a.itertuples(index=False):
        df=stocks[norm_code(r.code)]
        hs,hu,H=_h_for_setup(df,int(r.idx),float(r.depth))
        atrpct=df["atr20"].iloc[int(r.idx)]/df["close"].iloc[int(r.idx)]
        deepgap=float(np.clip(max(H,3*atrpct),.06,.20))
        fail=float(r.P)*(1-deepgap)
        x=r._asdict()
        x.update(Hswing_daily=hs,Hsetup=hu,Hdaily=H,fail_price=fail)
        out.append(x)
    return pd.DataFrame(out)


def attach_b_risk(b: pd.DataFrame, stocks: Dict[str,pd.DataFrame]) -> pd.DataFrame:
    out=[]
    for r in b.itertuples(index=False):
        df=stocks[norm_code(r.code)]
        t=int(r.idx);be=int(r.board_end)
        pull_low=float(df["low"].iloc[be+1:t].min())
        hs,hu,H=_h_for_setup(df,t,float(r.pull_dd))
        fail=pull_low*.98
        x=r._asdict()
        x.update(pull_low_level=pull_low,Hswing_daily=hs,Hsetup=hu,Hdaily=H,fail_price=fail)
        out.append(x)
    return pd.DataFrame(out)


def attach_c_risk(csig: pd.DataFrame, stocks: Dict[str,pd.DataFrame]) -> pd.DataFrame:
    out=[]
    for r in csig.itertuples(index=False):
        df=stocks[norm_code(r.code)]
        t=int(r.idx);s=t-int(r.flag_days)
        flag_low=float(df["low"].iloc[s:t].min())
        hs,hu,H=_h_for_setup(df,t,float(r.flag_dd))
        fail=flag_low*.98
        x=r._asdict()
        x.update(flag_low_level=flag_low,Hswing_daily=hs,Hsetup=hu,Hdaily=H,fail_price=fail)
        out.append(x)
    return pd.DataFrame(out)


def exit_a(row,df: pd.DataFrame,p: P=PARAM):
    df=ensure_exit_indicators(df)
    df=ensure_cost50(df)
    h=df["high"].to_numpy(float);c=df["close"].to_numpy(float)
    ma10=df["ma10"].to_numpy(float);ma20=df["ma20"].to_numpy(float)
    atr=df["atr20"].to_numpy(float);r5=df["r5"].to_numpy(float)
    er=df["er10"].to_numpy(float);cost=df["cost50"].to_numpy(float)

    t=int(row.idx);buy=float(row.buy);H=float(row.Hdaily);P0=float(row.P)
    maxh=h[t];mature=False;highm=False;deep_count=0

    for i in range(t+1,len(df)):
        maxh=max(maxh,h[i]);mfe=maxh/buy-1
        if mfe>=1.25*H: mature=True
        if mfe>=1.50*H: highm=True

        if not mature:
            atrpct=atr[i]/c[i] if c[i]>0 and np.isfinite(atr[i]) else 0
            dg=float(np.clip(max(H,3*atrpct),.06,.20))
            deep_count=deep_count+1 if c[i] < P0*(1-dg) else 0

            if deep_count>=3:
                return i,c[i]/buy-1,"结构失"

            if (
                i-t>=p.evidence_days and mfe<H
                and c[i]<ma20[i] and ma10[i]<ma20[i] and er[i]<0
            ):
                return i,c[i]/buy-1,"滞败"

        if (
            highm and i>=5 and np.isfinite(cost[i]) and np.isfinite(cost[i-5])
            and cost[i-5]>0 and cost[i]/cost[i-5]-1<=.005
            and r5[i]<0 and c[i]<ma10[i]
        ):
            return i,c[i]/buy-1,"高成熟成本停滞"

        if mature and c[i]<ma20[i] and ma10[i]<ma20[i] and er[i]<0:
            return i,c[i]/buy-1,"趋势破坏"

    return len(df)-1,c[-1]/buy-1,"数据末端"


def exit_b(row,df: pd.DataFrame,p: P=PARAM):
    df=ensure_exit_indicators(df)
    h=df["high"].to_numpy(float);c=df["close"].to_numpy(float)
    ma10=df["ma10"].to_numpy(float);ma20=df["ma20"].to_numpy(float)
    er=df["er10"].to_numpy(float)

    t=int(row.idx);buy=float(row.buy);H=float(row.Hdaily);fail=float(row.fail_price)
    maxh=h[t];proven=False;fail_count=0

    for i in range(t+1,len(df)):
        maxh=max(maxh,h[i]);mfe=maxh/buy-1

        if mfe>=1.5*H:
            proven=True

        if not proven:
            fail_count=fail_count+1 if c[i]<fail else 0

            if fail_count>=2:
                return i,c[i]/buy-1,"首回踩失效"

            if (
                i-t>=p.evidence_days and mfe<H
                and c[i]<ma20[i] and ma10[i]<ma20[i] and er[i]<0
            ):
                return i,c[i]/buy-1,"滞败"

        if proven:
            giveback=(maxh-c[i])/buy
            if giveback>=1.0*H:
                return i,c[i]/buy-1,"证明后结构回吐"

    return len(df)-1,c[-1]/buy-1,"数据末端"


def exit_c(row,df: pd.DataFrame,p: P=PARAM):
    df=ensure_exit_indicators(df)
    df=ensure_cost50(df)
    h=df["high"].to_numpy(float);c=df["close"].to_numpy(float)
    ma10=df["ma10"].to_numpy(float);ma20=df["ma20"].to_numpy(float)
    r5=df["r5"].to_numpy(float);er=df["er10"].to_numpy(float)
    cost=df["cost50"].to_numpy(float)

    t=int(row.idx);buy=float(row.buy);H=float(row.Hdaily);fail=float(row.fail_price)
    maxh=h[t];proven=False;highm=False;fail_count=0

    for i in range(t+1,len(df)):
        maxh=max(maxh,h[i]);mfe=maxh/buy-1

        if mfe>=1.0*H: proven=True
        if mfe>=1.5*H: highm=True

        if not proven:
            fail_count=fail_count+1 if c[i]<fail else 0

            if fail_count>=2:
                return i,c[i]/buy-1,"横盘结构失效"

            if (
                i-t>=p.evidence_days and mfe<H
                and c[i]<ma20[i] and ma10[i]<ma20[i] and er[i]<0
            ):
                return i,c[i]/buy-1,"滞败"

        if (
            highm and i>=5 and np.isfinite(cost[i]) and np.isfinite(cost[i-5])
            and cost[i-5]>0 and cost[i]/cost[i-5]-1<=.005
            and r5[i]<0 and c[i]<ma10[i]
        ):
            return i,c[i]/buy-1,"高成熟成本停滞"

        if proven and c[i]<ma20[i]:
            return i,c[i]/buy-1,"MA20破坏"

    return len(df)-1,c[-1]/buy-1,"数据末端"


# ============================================================
# Combination / sizing
# ============================================================

def target_weight(entry: float, fail: float, engine: str, p: P=PARAM) -> Tuple[float,float]:
    if entry<=0 or not np.isfinite(fail):
        return np.nan,np.nan
    risk=(entry-fail)/entry
    if risk<=0:
        return risk,0.0
    budget=p.c_risk_budget if engine=="C" else p.ab_risk_budget
    return risk,float(min(p.max_weight,budget/risk))


def combine_abc(a: pd.DataFrame,b: pd.DataFrame,c: pd.DataFrame) -> pd.DataFrame:
    aa=a.copy();bb=b.copy();cc=c.copy()
    aa["engine"]="A";bb["engine"]="B";cc["engine"]="C"
    x=pd.concat([aa,bb,cc],ignore_index=True,sort=False)
    x["_priority"]=x["engine"].map({"A":0,"B":1,"C":2})
    x=x.sort_values(["code","date","_priority"]).drop_duplicates(["code","date"],keep="first")
    return x.drop(columns="_priority").reset_index(drop=True)


def enrich_close_entry(signals: pd.DataFrame,p: P=PARAM) -> pd.DataFrame:
    z=signals.copy()
    z["entry_price"]=z["buy"]
    vals=[target_weight(float(r.entry_price),float(r.fail_price),str(r.engine),p)
          for r in z.itertuples(index=False)]
    z["risk_pct"]=[x[0] for x in vals]
    z["target_weight"]=[x[1] for x in vals]
    z["target_pct"]=z["target_weight"]*100
    return z



def run_close_reference(
    raw_stocks: Dict[str,pd.DataFrame],
    p: P=PARAM,
    run_exits: bool=True,
    progress_cb=None,
    memory_safe: bool=False,
) -> Tuple[pd.DataFrame,dict]:
    """
    End-to-end reference entry engine.

    memory_safe=True changes allocation only, not strategy math.
    """
    def emit(stage: str, fraction: float, detail: str = "") -> None:
        if progress_cb is None:
            return
        try:
            progress_cb(stage, float(max(0.0, min(1.0, fraction))), detail)
        except Exception:
            pass

    total=max(1,len(raw_stocks))
    step=max(1,len(raw_stocks)//40)

    if memory_safe:
        import gc
        keys=list(raw_stocks.keys())
        emit("V18数据预处理", 0.00, f"内存安全模式：准备处理 {len(keys)} 只股票")
        for i, code in enumerate(keys, start=1):
            c=norm_code(code)
            if not eligible_code(c):
                raw_stocks.pop(code, None)
                continue

            prepared=prepare_stock(raw_stocks[code], c)
            prepared=trim_live_prepared_frame(prepared)

            if c == code:
                raw_stocks[code]=prepared
            else:
                raw_stocks[c]=prepared
                raw_stocks.pop(code, None)

            if i==len(keys) or i%step==0:
                gc.collect()
                emit("V18数据预处理", 0.18*(i/total), f"已原位预处理 {i}/{len(keys)} 只股票")

        stocks=raw_stocks
        gc.collect()
        emit("构建市场横截面", 0.20, f"紧凑横截面；策略有效股票 {len(stocks)} 只")
        market=build_market_context_compact(stocks)
        gc.collect()
    else:
        stocks={}
        items=list(raw_stocks.items())
        emit("V18数据预处理", 0.00, f"准备处理 {len(items)} 只股票")
        for i,(code,df) in enumerate(items, start=1):
            c=norm_code(code)
            if eligible_code(c):
                stocks[c]=prepare_stock(df,c)
            if i==len(items) or i%step==0:
                emit("V18数据预处理", 0.18*(i/total), f"已预处理 {i}/{len(items)} 只股票")

        emit("构建市场横截面", 0.20, f"策略有效股票 {len(stocks)} 只")
        market=build_market_context(stocks)

    emit("构建市场横截面", 0.23, "q40 / above20 / mom20 已完成")

    emit("Engine A", 0.24, "正在扫描 A 结构")
    a,peer_pool=scan_a_final(stocks,market,p)
    emit("Engine A", 0.48, f"A 完成：{len(a)} 条；peer pool {len(peer_pool)}")

    emit("Engine B", 0.50, "正在扫描 B 结构")
    b=scan_b_final(stocks,market,p)
    emit("Engine B", 0.66, f"B 完成：{len(b)} 条")

    emit("Engine C", 0.68, "正在扫描 C 结构")
    c=scan_c_final(stocks,market,p)
    emit("Engine C", 0.82, f"C 完成：{len(c)} 条")

    emit("风险与组合", 0.84, "计算 H / fail price / target weight")
    a=attach_a_risk(a,stocks)
    b=attach_b_risk(b,stocks)
    c=attach_c_risk(c,stocks)

    sig=combine_abc(a,b,c)
    sig=enrich_close_entry(sig,p)
    emit("风险与组合", 0.88, f"Combined {len(sig)} 条")

    if run_exits and len(sig):
        exit_idx=[];exit_ret=[];exit_reason=[]
        n=len(sig)
        exit_step=max(1,n//20)
        emit("退出生命周期", 0.89, f"准备计算 {n} 条信号退出")
        for i,r in enumerate(sig.itertuples(index=False), start=1):
            df=stocks[norm_code(r.code)]
            if r.engine=="A":
                ex=exit_a(r,df,p)
            elif r.engine=="B":
                ex=exit_b(r,df,p)
            else:
                ex=exit_c(r,df,p)
            exit_idx.append(ex[0]);exit_ret.append(ex[1]);exit_reason.append(ex[2])
            if i==n or i%exit_step==0:
                emit("退出生命周期", 0.89 + 0.09*(i/n), f"已处理 {i}/{n} 条信号")

        sig["exit_idx"]=exit_idx
        sig["exit_ret"]=exit_ret
        sig["exit_reason"]=exit_reason
    else:
        emit("退出生命周期", 0.98, "无退出生命周期需要计算")

    diag=dict(
        A=len(a), B_before_precedence=len(b), C_before_precedence=len(c),
        combined=len(sig), peer_pool=len(peer_pool)
    )
    emit("V18引擎完成", 1.00, f"A={len(a)} B={len(b)} C={len(c)} Combined={len(sig)}")
    return sig,diag


# ============================================================
# Golden comparison
# ============================================================

def _datekey(x) -> str:
    if isinstance(x,(int,np.integer)):
        # If already YYYYMMDD integer.
        s=str(int(x))
        if len(s)==8:
            return s
    return pd.Timestamp(x).strftime("%Y%m%d")


def compare_to_golden(result: pd.DataFrame, golden_csv: str) -> dict:
    g=pd.read_csv(golden_csv,dtype={"code":str,"date_yyyymmdd":str})
    g["code"]=g["code"].map(norm_code)
    g["key"]=list(zip(g["code"],g["date_yyyymmdd"].astype(str),g["engine"].astype(str)))

    r=result.copy()
    r["code"]=r["code"].map(norm_code)
    r["date_yyyymmdd"]=r["date"].map(_datekey)
    r["key"]=list(zip(r["code"],r["date_yyyymmdd"],r["engine"].astype(str)))

    gs=set(g["key"]);rs=set(r["key"])
    missing=sorted(gs-rs)
    extra=sorted(rs-gs)

    out={
        "golden_n":len(gs),
        "result_n":len(rs),
        "matched_n":len(gs&rs),
        "missing_n":len(missing),
        "extra_n":len(extra),
        "missing":missing,
        "extra":extra,
    }

    # Numerical comparison only on matched events.
    if "fail_price" in r.columns:
        mg=g.merge(
            r[["code","date_yyyymmdd","engine","fail_price","target_pct"]],
            on=["code","date_yyyymmdd","engine"],
            how="inner",suffixes=("_gold","_new")
        )
        if len(mg):
            out["fail_abs_error_median"]=float(
                np.nanmedian(np.abs(mg["fail_price_gold"]-mg["fail_price_new"]))
            )
            out["target_pct_abs_error_median"]=float(
                np.nanmedian(np.abs(mg["target_pct_gold"]-mg["target_pct_new"]))
            )

    return out
