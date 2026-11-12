"""Performance analytics.

* The daily series covers EVERY trading day in scope (days with no trade carry 0 P&L), so
  volatility-based ratios are not inflated by dropping flat days.
* Annualisation is data driven: periods_per_year = days in the series / years spanned.
  (No hard-coded 252.)
* Win/loss rate, profit factor, expectancy, streaks and the monthly / weekday tables use TRADED days
  (a "trade" in the headline numbers is one strategy-day, all legs and re-entries summed).
* `capital=None` means no capital base was given: Rs figures are produced, but % drawdown, Sharpe,
  Sortino, CAGR and Calmar are NaN rather than computed from an invented capital.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from .schemas import BacktestResult

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAILY_COLUMNS = ["date", "traded", "legs", "gross_pnl", "costs", "net_pnl", "cum_pnl", "equity",
                 "peak_equity", "drawdown", "drawdown_pct"]
NAN = float("nan")
MIN_CAGR_DAYS = 30


def build_daily(trades: pd.DataFrame, capital: Optional[float],
                universe: Iterable[date] = ()) -> pd.DataFrame:
    dates = set(universe)
    if not trades.empty:
        dates |= set(trades["date"])
    if not dates:
        return pd.DataFrame(columns=DAILY_COLUMNS)
    idx = pd.Index(sorted(dates), name="date")
    if trades.empty:
        per = pd.DataFrame(index=idx, columns=["legs", "gross_pnl", "costs", "net_pnl"], dtype=float)
    else:
        per = (trades.groupby("date", sort=True)
               .agg(legs=("leg_id", "count"), gross_pnl=("gross_pnl", "sum"),
                    costs=("costs", "sum"), net_pnl=("net_pnl", "sum")))
    d = per.reindex(idx).fillna(0.0).reset_index()
    d["legs"] = d["legs"].astype(int)
    d["traded"] = d["legs"] > 0
    base = 0.0 if capital is None else float(capital)
    d["cum_pnl"] = d["net_pnl"].cumsum()
    d["equity"] = base + d["cum_pnl"]
    d["peak_equity"] = np.maximum.accumulate(np.maximum(d["equity"].values, base))
    d["drawdown"] = d["equity"] - d["peak_equity"]                      # <= 0, Rs
    if capital is None:
        d["drawdown_pct"] = NAN
    else:
        d["drawdown_pct"] = d["drawdown"] / d["peak_equity"] * 100.0    # <= 0, %
    return d[DAILY_COLUMNS]


def _streaks(pnl: np.ndarray) -> Tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for x in pnl:
        if x > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif x < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return best_w, best_l


def _drawdown_stats(daily: pd.DataFrame, capital: Optional[float]) -> Dict[str, Any]:
    base = 0.0 if capital is None else float(capital)
    dates: List[date] = [daily["date"].iloc[0]] + list(daily["date"])
    eq = np.concatenate(([base], daily["equity"].values))
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    tr = int(dd.argmax())
    pk = int(eq[:tr + 1].argmax())
    rec: Optional[int] = None
    if dd[tr] > 0:
        after = np.where(eq[tr:] >= eq[pk])[0]
        if after.size:
            rec = tr + int(after[0])
    if capital is None:
        pct = NAN
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            pct = float(np.where(peak > 0, dd / peak * 100.0, 0.0).max())
    has = dd[tr] > 0
    return {
        "max_drawdown": float(dd[tr]),
        "max_drawdown_pct": pct,
        "max_dd_peak_date": dates[pk] if has else None,
        "max_dd_trough_date": dates[tr] if has else None,
        "max_dd_recovery_date": dates[rec] if rec is not None else None,
        "peak_to_trough_days": (dates[tr] - dates[pk]).days if has else 0,
        "trough_to_recovery_days": (dates[rec] - dates[tr]).days if rec is not None else None,
        "peak_to_trough_trading_days": tr - pk if has else 0,
        "trough_to_recovery_trading_days": (rec - tr) if rec is not None else None,
        "recovered": (rec is not None) if has else True,
    }


def compute_metrics(trades: pd.DataFrame, daily: pd.DataFrame, capital: Optional[float],
                    risk_free_annual: float = 0.0) -> Dict[str, Any]:
    n_scope = len(daily)
    traded = daily[daily["traded"]] if n_scope else daily
    if traded.empty:
        return {"total_net_pnl": 0.0, "total_trades": 0, "total_leg_trades": 0,
                "trading_days_in_scope": n_scope, "capital": capital}
    inf = float("inf")
    pt = traded["net_pnl"].values
    wins, losses = pt[pt > 0], pt[pt < 0]
    gross_win, gross_loss = wins.sum(), abs(losses.sum())
    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = float(losses.mean()) if losses.size else 0.0
    max_w, max_l = _streaks(pt)

    pnl_all = daily["net_pnl"].values
    span_days = (daily["date"].iloc[-1] - daily["date"].iloc[0]).days + 1
    years = span_days / 365.25
    ppy = n_scope / years
    sharpe = sortino = cagr = calmar = NAN
    if capital is not None:
        eq = np.concatenate(([float(capital)], daily["equity"].values))
        rets = pnl_all / eq[:-1]
        ex = rets - risk_free_annual / ppy
        std = ex.std(ddof=1) if n_scope > 1 else 0.0
        if std > 0:
            sharpe = float(ex.mean() / std * math.sqrt(ppy))
        down = math.sqrt(float(np.mean(np.minimum(ex, 0.0) ** 2)))
        if down > 0:
            sortino = float(ex.mean() / down * math.sqrt(ppy))
        elif ex.mean() > 0:
            sortino = inf
        if eq[-1] > 0 and span_days >= MIN_CAGR_DAYS:
            # Annualising a window shorter than a month turns noise into absurd percentages (and can overflow).
            try:
                cagr = ((eq[-1] / eq[0]) ** (1 / years) - 1) * 100.0
            except OverflowError:
                cagr = NAN
    dd = _drawdown_stats(daily, capital)
    if capital is not None and dd["max_drawdown_pct"] > 0 and not math.isnan(cagr):
        calmar = cagr / dd["max_drawdown_pct"]

    return {
        "total_net_pnl": float(pnl_all.sum()),
        "total_gross_pnl": float(daily["gross_pnl"].sum()),
        "total_costs": float(daily["costs"].sum()),
        "total_slippage_cost": float(trades["slippage_cost"].sum()),
        "total_trades": int(traded.shape[0]),            # traded strategy-days
        "total_leg_trades": int(len(trades)),             # leg executions incl. re-entries
        "trading_days_in_scope": n_scope,
        "win_rate_pct": float(wins.size / pt.size * 100),
        "loss_rate_pct": float(losses.size / pt.size * 100),
        "profit_factor": float(gross_win / gross_loss) if gross_loss > 0 else inf,
        "win_loss_ratio": float(avg_win / abs(avg_loss)) if avg_loss != 0 else inf,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": float(pt.mean()),
        "best_day": float(pt.max()),
        "worst_day": float(pt.min()),
        **dd,
        "periods_per_year": float(ppy),
        "cagr_pct": cagr,
        "sharpe": sharpe,
        "sortino": sortino,
        "calmar": float(calmar),
        "max_consecutive_wins": max_w,
        "max_consecutive_losses": max_l,
        "exit_reasons": trades["exit_reason"].value_counts().to_dict(),
        "capital": capital,
        "risk_free_annual": risk_free_annual,
    }


def monthly_matrix(daily: pd.DataFrame) -> pd.DataFrame:
    t = daily[daily["traded"]] if len(daily) else daily
    if t.empty:
        return pd.DataFrame()
    d = t.assign(year=[x.year for x in t["date"]], month=[x.month for x in t["date"]])
    pv = d.pivot_table(index="year", columns="month", values="net_pnl", aggfunc="sum", fill_value=0.0)
    pv = pv.reindex(columns=range(1, 13), fill_value=0.0)
    pv.columns = _MONTHS
    pv["Total"] = pv.sum(axis=1)
    return pv.round(2)


def weekday_table(daily: pd.DataFrame) -> pd.DataFrame:
    t = daily[daily["traded"]] if len(daily) else daily
    if t.empty:
        return pd.DataFrame()
    d = t.assign(dow=[x.weekday() for x in t["date"]])
    g = d.groupby("dow")["net_pnl"]
    out = pd.DataFrame({
        "trades": g.count(), "net_pnl": g.sum(), "avg_pnl": g.mean(),
        "win_rate_pct": g.apply(lambda s: (s > 0).mean() * 100),
        "worst": g.min(), "best": g.max(),
    })
    out.index = [_DOW[i] for i in out.index]
    out.index.name = "weekday"
    return out.round(2)


def trade_level_metrics(trades: pd.DataFrame) -> Dict[str, Any]:
    """Per-trade statistics (each row of a signal strategy's log is one round trip)."""
    if trades.empty:
        return {"signal_trades": 0}
    p = trades["net_pnl"].values
    w, lo = p[p > 0], p[p < 0]
    return {
        "signal_trades": int(p.size),
        "trades_per_day": float(p.size / trades["date"].nunique()),
        "trade_win_rate_pct": float(w.size / p.size * 100),
        "trade_profit_factor": float(w.sum() / abs(lo.sum())) if lo.size else float("inf"),
        "trade_avg_win": float(w.mean()) if w.size else 0.0,
        "trade_avg_loss": float(lo.mean()) if lo.size else 0.0,
        "trade_expectancy": float(p.mean()),
    }


def build_result(cfg, trades: pd.DataFrame, skipped: List[Tuple[date, str]],
                 universe: Iterable[date] = ()) -> BacktestResult:
    daily = build_daily(trades, cfg.capital, universe)
    metrics = compute_metrics(trades, daily, cfg.capital)
    metrics["skipped_days"] = sum(1 for _, why in skipped if not str(why).startswith("filter:"))
    metrics["filtered_days"] = sum(1 for _, why in skipped if str(why).startswith("filter:"))
    return BacktestResult(config=cfg, trades=trades, daily=daily, metrics=metrics,
                          monthly=monthly_matrix(daily), weekday=weekday_table(daily), skipped=skipped)
