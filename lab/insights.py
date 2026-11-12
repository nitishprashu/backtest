"""Dashboard tables derived from a run's daily series and trade log (pure pandas/numpy)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def drawdown_episodes(daily: pd.DataFrame, capital: Optional[float], top: int = 6) -> List[Dict[str, Any]]:
    if daily.empty:
        return []
    base = float(capital or 0.0)
    dates = list(daily["date"])
    eq = base + daily["cum_pnl"].values
    peak_v, peak_i = base, -1                 # -1 = the starting point before the first day
    episodes: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for i, (d, v) in enumerate(zip(dates, eq)):
        if v >= peak_v:
            if cur is not None:
                cur["recovery_date"], cur["recovery_idx"] = d, i
                episodes.append(cur)
                cur = None
            peak_v, peak_i = v, i
        else:
            if cur is None:
                cur = {"peak_date": dates[peak_i] if peak_i >= 0 else dates[0], "peak_idx": peak_i,
                       "peak_value": peak_v, "trough_value": v, "trough_date": d, "trough_idx": i,
                       "recovery_date": None, "recovery_idx": None}
            if v < cur["trough_value"]:
                cur["trough_value"], cur["trough_date"], cur["trough_idx"] = v, d, i
    if cur is not None:
        episodes.append(cur)
    out = []
    for e in episodes:
        depth = e["peak_value"] - e["trough_value"]
        out.append({
            "peak_date": e["peak_date"], "trough_date": e["trough_date"], "recovery_date": e["recovery_date"],
            "depth": float(depth),
            "depth_pct": float(depth / e["peak_value"] * 100) if capital and e["peak_value"] > 0 else None,
            "days_to_trough": int(e["trough_idx"] - e["peak_idx"]),
            "days_to_recover": int(e["recovery_idx"] - e["trough_idx"]) if e["recovery_idx"] is not None else None,
            "recovered": e["recovery_idx"] is not None})
    out.sort(key=lambda x: x["depth"], reverse=True)
    return out[:top]


def equity_rows(daily: pd.DataFrame) -> List[Dict[str, Any]]:
    return [{"date": r.date, "net": r.net_pnl, "cum": r.cum_pnl, "equity": r.equity, "dd": r.drawdown,
             "dd_pct": r.drawdown_pct, "traded": bool(r.traded), "legs": int(r.legs), "costs": r.costs}
            for r in daily.itertuples()]


def monthly_rows(daily: pd.DataFrame) -> List[Dict[str, Any]]:
    t = daily[daily["traded"]]
    if t.empty:
        return []
    d = pd.DataFrame({"year": [x.year for x in t["date"]], "month": [x.month for x in t["date"]],
                      "pnl": t["net_pnl"].values})
    g = d.groupby(["year", "month"], as_index=False).agg(pnl=("pnl", "sum"), days=("pnl", "count"),
                                                         wins=("pnl", lambda s: int((s > 0).sum())))
    return g.to_dict("records")


def weekday_rows(daily: pd.DataFrame) -> List[Dict[str, Any]]:
    t = daily[daily["traded"]]
    if t.empty:
        return []
    d = pd.DataFrame({"wd": [x.weekday() for x in t["date"]], "pnl": t["net_pnl"].values})
    g = d.groupby("wd").agg(net=("pnl", "sum"), days=("pnl", "count"), avg=("pnl", "mean"),
                            win=("pnl", lambda s: float((s > 0).mean() * 100))).reset_index()
    g["name"] = g["wd"].map(lambda i: DOW[i])
    return g.to_dict("records")


def dte_rows(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    if trades.empty:
        return []
    day = trades.groupby("date").agg(expiry=("expiry", "first"), net=("net_pnl", "sum")).reset_index()
    day["dte"] = [(e - d).days for d, e in zip(day["date"], day["expiry"])]
    g = day.groupby("dte").agg(days=("net", "count"), net=("net", "sum"), avg=("net", "mean"),
                               win=("net", lambda s: float((s > 0).mean() * 100))).reset_index()
    return g.sort_values("dte").to_dict("records")


def hist_rows(daily: pd.DataFrame, bins: int = 24) -> List[Dict[str, Any]]:
    t = daily[daily["traded"]]["net_pnl"].values
    if t.size == 0:
        return []
    counts, edges = np.histogram(t, bins=min(bins, max(5, t.size // 2)))
    return [{"lo": float(edges[i]), "hi": float(edges[i + 1]), "n": int(counts[i])} for i in range(len(counts))]


def exit_rows(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    if trades.empty:
        return []
    g = trades.groupby("exit_reason").agg(n=("net_pnl", "size"), net=("net_pnl", "sum")).reset_index()
    return g.rename(columns={"exit_reason": "reason"}).sort_values("n", ascending=False).to_dict("records")


def maemfe_rows(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    if trades.empty or trades["day_mae"].isna().all():
        return []
    g = trades.groupby("date").agg(net=("net_pnl", "sum"), mae=("day_mae", "first"), mfe=("day_mfe", "first")).reset_index()
    return g.to_dict("records")


def extra_metrics(metrics: Dict[str, Any], daily: pd.DataFrame, trades: pd.DataFrame) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    n = metrics.get("total_trades", 0)
    if not n:
        return out
    dd = metrics.get("max_drawdown") or 0.0
    out["recovery_factor"] = (metrics["total_net_pnl"] / dd) if dd > 0 else None
    out["profit_per_day"] = metrics["total_net_pnl"] / n
    t = daily[daily["traded"]]["net_pnl"]
    out["median_day"] = float(t.median())
    out["std_day"] = float(t.std(ddof=1)) if len(t) > 1 else None
    out["win_days"] = int((t > 0).sum())
    out["loss_days"] = int((t < 0).sum())
    out["flat_days"] = int((t == 0).sum())
    if not trades.empty:
        out["avg_holding_min"] = float((pd.to_datetime(trades["exit_time"]) - pd.to_datetime(trades["entry_time"])).dt.total_seconds().mean() / 60)
    return out


def greek_rows(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    required = {"entry_iv", "entry_delta", "entry_theta", "entry_vega"}
    if trades.empty or not required.issubset(trades.columns):
        return []
    t = trades[trades["segment"] == 1].copy()
    if t.empty:
        return []
    sign = np.where(t["side"] == "BUY", 1.0, -1.0)
    t["net_delta"] = sign * t["lots"] * t["entry_delta"]
    t["net_theta"] = sign * t["qty"] * t["entry_theta"]
    t["net_vega"] = sign * t["qty"] * t["entry_vega"]
    t["entry"] = pd.to_datetime(t["entry_time"]).dt.strftime("%H:%M")
    return t.groupby("date").agg(
        iv=("entry_iv", "mean"), net_delta=("net_delta", "sum"), net_theta=("net_theta", "sum"),
        net_vega=("net_vega", "sum"), entry=("entry", "min"), pnl=("net_pnl", "sum")
    ).reset_index().to_dict("records")


def iv_range_rows(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    """Group strategy days by the average IV of their initial leg entries."""
    required = {"date", "segment", "entry_iv", "net_pnl"}
    if trades.empty or not required.issubset(trades.columns):
        return []
    initial = trades[trades["segment"] == 1].copy()
    iv = initial.groupby("date", as_index=False).agg(entry_iv=("entry_iv", "mean"))
    values = trades.groupby("date", as_index=False).agg(net_pnl=("net_pnl", "sum"))
    daily = values.merge(iv, on="date", how="left")
    daily = daily[np.isfinite(pd.to_numeric(daily["entry_iv"], errors="coerce"))].copy()
    daily["range"] = pd.cut(daily["entry_iv"], bins=[0, 10, 15, 20, 25, 30, np.inf],
                            labels=["<10%", "10–15%", "15–20%", "20–25%", "25–30%", "30%+"],
                            right=False, include_lowest=True)
    grouped = daily.groupby("range", observed=False).agg(
        days=("net_pnl", "count"), net=("net_pnl", "sum"), avg=("net_pnl", "mean"),
        win=("net_pnl", lambda s: float((s > 0).mean() * 100)),
    )
    return [{"range": str(label), "days": int(row.days), "net": float(row.net), "avg": float(row.avg),
             "win": float(row.win)} for label, row in grouped.iterrows() if row.days]


def premium_range_rows(trades: pd.DataFrame) -> List[Dict[str, Any]]:
    """Group strategy days by short option premium collected across entries, in rupees."""
    required = {"date", "side", "entry_price", "qty", "net_pnl"}
    if trades.empty or not required.issubset(trades.columns):
        return []
    shorts = trades[trades["side"] == "SELL"].copy()
    shorts["collected"] = shorts["entry_price"] * shorts["qty"]
    collected = shorts.groupby("date")["collected"].sum()
    daily = trades.groupby("date", as_index=False).agg(net=("net_pnl", "sum"))
    daily["premium"] = daily["date"].map(collected).fillna(0.0)
    edges = [0, 10_000, 25_000, 50_000, 100_000, np.inf]
    labels = ["<₹10k", "₹10k–25k", "₹25k–50k", "₹50k–1L", "₹1L+"]
    daily["range"] = pd.cut(daily["premium"], bins=edges, labels=labels, right=False, include_lowest=True)
    grouped = daily.groupby("range", observed=False).agg(
        days=("net", "count"), net=("net", "sum"), avg=("net", "mean"),
        win=("net", lambda s: float((s > 0).mean() * 100)),
    )
    return [{"range": str(label), "days": int(row.days), "net": float(row.net), "avg": float(row.avg),
             "win": float(row.win)} for label, row in grouped.iterrows() if row.days]


def build_dashboard(metrics: Dict[str, Any], daily: pd.DataFrame, trades: pd.DataFrame,
                    capital: Optional[float]) -> Dict[str, Any]:
    return {
        "equity": equity_rows(daily), "monthly": monthly_rows(daily), "weekday": weekday_rows(daily),
        "dte": dte_rows(trades), "hist": hist_rows(daily), "exits": exit_rows(trades),
        "maemfe": maemfe_rows(trades), "drawdowns": drawdown_episodes(daily, capital),
        "greeks": greek_rows(trades),
        "iv_ranges": iv_range_rows(trades), "premium_ranges": premium_range_rows(trades),
        "extra": extra_metrics(metrics, daily, trades),
    }
