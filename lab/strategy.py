"""Translation of the web UI's JSON strategy + settings into optbt configs. Pure Python, unit-tested."""
from __future__ import annotations

import dataclasses
from datetime import date, time
from math import ceil
from typing import Any, Dict, List, Optional, Tuple

from optbt.schemas import ATMReference, EntryCondition
from optbt import (NIFTY, EMASignalMode, EMASource, EMAParams, MACDParams, ExpirySelect, InstrumentRegistry, LegConfig,
                   GreekSettings, MovingAverageType, OppositeSignal, OptionExpression, OptionType, PortfolioTrail,
                   HoldingMode, ReEntryMode, ReEntryRule, RiskRule, RolloverStrikeMode, Side, SlippageModel, SignalStrategyConfig,
                   StrategyConfig, StrikeSelection, SupertrendParams, TrailRule, VwapParams, VwapSource)

MAX_LEGS = 8
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
EXPIRY_RULES = {"Nearest weekly": ExpirySelect.CURRENT_WEEKLY, "Second weekly": ExpirySelect.NEXT_WEEKLY,
                "Monthly": ExpirySelect.MONTHLY}

# (side, type, rule, offset) - offsets are signed strike steps from ATM (CE +n = OTM, PE -n = OTM)
TEMPLATES: Dict[str, List[Tuple[Any, ...]]] = {
    "Custom": [],
    "Short straddle": [("Sell", "CE", "ATM", 0), ("Sell", "PE", "ATM", 0)],
    "Long straddle": [("Buy", "CE", "ATM", 0), ("Buy", "PE", "ATM", 0)],
    "Short strangle (±2)": [("Sell", "CE", "ATM Offset", 2), ("Sell", "PE", "ATM Offset", -2)],
    "Long strangle (±2)": [("Buy", "CE", "ATM Offset", 2), ("Buy", "PE", "ATM Offset", -2)],
    "Iron butterfly (wings ±4)": [("Sell", "CE", "ATM", 0), ("Sell", "PE", "ATM", 0),
                                  ("Buy", "CE", "ATM Offset", 4), ("Buy", "PE", "ATM Offset", -4)],
    "Iron condor (±2 / ±4)": [("Sell", "PE", "ATM Offset", -2), ("Buy", "PE", "ATM Offset", -4),
                              ("Sell", "CE", "ATM Offset", 2), ("Buy", "CE", "ATM Offset", 4)],
    "Bull call spread": [("Buy", "CE", "ATM", 0), ("Sell", "CE", "ATM Offset", 2)],
    "Bear put spread": [("Buy", "PE", "ATM", 0), ("Sell", "PE", "ATM Offset", -2)],
    "Bull put spread": [("Sell", "PE", "ATM", 0), ("Buy", "PE", "ATM Offset", -2)],
    "Bear call spread": [("Sell", "CE", "ATM", 0), ("Buy", "CE", "ATM Offset", 2)],
    "Call calendar spread": [("Sell", "CE", "ATM", 0),
                             ("Buy", "CE", "ATM", 0, {"expiry_offset": 1})],
    "Put calendar spread": [("Sell", "PE", "ATM", 0),
                            ("Buy", "PE", "ATM", 0, {"expiry_offset": 1})],
    "20-delta short strangle": [("Sell", "CE", "Greek", 0, {"g_target": 0.20}),
                                ("Sell", "PE", "Greek", 0, {"g_target": 0.20})],
    "16Δ / 5Δ iron condor": [("Sell", "CE", "Greek", 0, {"g_target": 0.16}),
                             ("Buy", "CE", "Greek", 0, {"g_target": 0.05}),
                             ("Sell", "PE", "Greek", 0, {"g_target": 0.16}),
                             ("Buy", "PE", "Greek", 0, {"g_target": 0.05})],
    "30-delta long strangle": [("Buy", "CE", "Greek", 0, {"g_target": 0.30}),
                               ("Buy", "PE", "Greek", 0, {"g_target": 0.30})],
}


def default_leg(i: int = 0) -> Dict[str, Any]:
    return {"side": "Sell", "type": "CE" if i % 2 == 0 else "PE", "rule": "ATM", "offset": 0,
            "expiry_offset": 0, "prem": 100.0,
            "fixed": 24000.0, "lots": 1,
            "sl_kind": "Off", "sl_val": 25.0, "tp_kind": "Off", "tp_val": 50.0,
            "trail_on": False, "trail_trig": 20.0, "trail_step": 10.0,
            "re_mode": "Off", "re_n": 1,
            "g_metric": "delta", "g_target": 0.20, "g_min": 0.0, "g_max": 0.0}


def default_condition() -> Dict[str, Any]:
    return {"kind": "atm_iv", "op": ">=", "value": 15.0, "value2": 20.0,
            "st_period": 10, "st_mult": 3.0, "st_tf": 5}


def default_entry() -> Dict[str, Any]:
    return {"window_end": "", "conditions": []}


def default_portfolio() -> Dict[str, Any]:
    return {"sl_kind": "Off", "sl_val": 5000.0, "tp_kind": "Off", "tp_val": 5000.0,
            "trail_on": False, "trail_trig": 1000.0, "trail_step": 500.0}


def default_indicator() -> Dict[str, Any]:
    return {"type": "Supertrend", "st_period": 10, "st_mult": 3.0, "st_timeframe": "5m", "st_prev": True,
            "vwap_source": "Spot", "vwap_buf": 0.05, "vwap_confirm": 3, "vwap_timeframe": "1m",
            "ema_source": "Underlying", "ema_mode": "Price vs EMA", "ema_period": 20, "ema_fast": 9, "ema_slow": 21,
            "ema_slope_filter": False, "ema_slope_type": "EMA", "ema_slope_period": 20, "ema_timeframe": "1m",
            "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "macd_timeframe": "1m",
            "expression": "Buy option with the trend", "on_opposite": "Reverse",
            "start_time": time(9, 30), "last_entry_time": time(14, 30), "exit_time": time(15, 15),
            "max_trades": 6, "initial_trend": False, "daily_max_loss": 0.0, "daily_target": 0.0}


def default_params() -> Dict[str, Any]:
    return {"kind": "multi", "legs": [default_leg(0), default_leg(1)], "portfolio": default_portfolio(),
            "entry": default_entry(), "ind_leg": {**default_leg(0), "side": "Buy"},
            "indicator_legs": [{**default_leg(0), "side": "Buy"}],
            "indicator": default_indicator()}


def template_legs(name: str) -> List[Dict[str, Any]]:
    out = []
    for i, spec in enumerate(TEMPLATES[name]):
        side, typ, rule, off = spec[:4]
        leg = default_leg(i)
        leg.update(side=side, type=typ, rule=rule, offset=off)
        if len(spec) > 4:
            leg.update(spec[4])
        out.append(leg)
    return out


# --------------------------------------------------------------------------- converters
def _risk(kind: str, val: float) -> Optional[RiskRule]:
    if kind == "Off" or not val or val <= 0:
        return None
    return RiskRule.percent(val) if kind == "%" else RiskRule.points(val)


def _strike(d: Dict[str, Any], atm_reference: ATMReference = ATMReference.SPOT) -> StrikeSelection:
    rule = d["rule"]
    if rule == "ATM":
        return StrikeSelection.atm(atm_reference)
    if rule == "ATM Offset":
        return StrikeSelection.steps(int(d["offset"]), atm_reference)
    if rule == "Premium":
        return StrikeSelection.premium(target=float(d["prem"]))
    if rule == "Fixed":
        return StrikeSelection.fixed(float(d["fixed"]))
    if rule == "Greek":
        return StrikeSelection.greek(d["g_metric"], float(d["g_target"]),
                                     float(d["g_min"]) or None, float(d["g_max"]) or None)
    raise ValueError(f"unknown strike rule {rule!r}")


def _cond(c: Dict[str, Any]) -> EntryCondition:
    kind = c["kind"]
    if kind in ("spot_vs_vwap", "supertrend"):
        return EntryCondition(kind, "is", c["value"], None, int(c["st_period"]),
                              float(c["st_mult"]), int(c["st_tf"]))
    return EntryCondition(kind, c["op"], float(c["value"]),
                          float(c["value2"]) if c["op"] == "between" else None,
                          int(c["st_period"]), float(c["st_mult"]), int(c["st_tf"]))


def _trail(d: Dict[str, Any]) -> Optional[TrailRule]:
    if not d.get("trail_on"):
        return None
    return TrailRule(float(d["trail_trig"]), float(d["trail_step"]))


def _reentry(d: Dict[str, Any]) -> ReEntryRule:
    mode = {"Off": ReEntryMode.NONE, "Re-execute": ReEntryMode.RE_EXECUTE, "Re-cost": ReEntryMode.RE_COST}[d["re_mode"]]
    return ReEntryRule(mode, int(d["re_n"]) if mode is not ReEntryMode.NONE else 0)


def to_leg(d: Dict[str, Any], atm_reference: ATMReference = ATMReference.SPOT) -> LegConfig:
    return LegConfig(
        side=Side.BUY if d["side"] == "Buy" else Side.SELL,
        option_type=OptionType.CE if d["type"] == "CE" else OptionType.PE,
        strike=_strike(d, atm_reference), expiry_offset=int(d.get("expiry_offset", 0)),
        lots=int(d["lots"]), stop_loss=_risk(d["sl_kind"], d["sl_val"]),
        target=_risk(d["tp_kind"], d["tp_val"]), trail_sl=_trail(d), reentry=_reentry(d))


def _indicator_expression_from_legs(legs: List[Dict[str, Any]] | Tuple[Dict[str, Any], ...] | None) -> OptionExpression:
    for leg in legs or []:
        side = str(leg.get("side", "Buy")).lower()
        if side.startswith("buy"):
            return OptionExpression.LONG_PREMIUM
        if side.startswith("sell"):
            return OptionExpression.SHORT_PREMIUM
    return OptionExpression.LONG_PREMIUM


def _indicator_expression_value(ind: Dict[str, Any], legs: List[Dict[str, Any]] | Tuple[Dict[str, Any], ...] | None) -> OptionExpression:
    if legs:
        return _indicator_expression_from_legs(legs)
    expr = ind.get("expression")
    if isinstance(expr, str) and expr:
        return OptionExpression.LONG_PREMIUM if expr.lower().startswith("buy") else OptionExpression.SHORT_PREMIUM
    return OptionExpression.LONG_PREMIUM


def validate_globals(g: Dict[str, Any]) -> List[str]:
    """Bounds for the engine-side settings. Without these a typo (strike interval 0, negative slippage, a
    negative lot size) would only surface as a wall of per-day 'error:' skips, or worse, silently flip P&L."""
    errs: List[str] = []
    if g["strike_interval"] <= 0:
        errs.append("Strike interval must be greater than 0.")
    if g["lot_mode"] not in ("auto", "manual"):
        errs.append("Lot mode must be auto or manual.")
    elif g["lot_mode"] == "manual" and g["lot_size"] < 1:
        errs.append("Manual lot size must be at least 1.")
    if g["capital"] < 0:
        errs.append("Capital cannot be negative.")
    if g["brokerage"] < 0:
        errs.append("Brokerage cannot be negative.")
    if min(g["slip_pct"], g["slip_min_ticks"], g["slip_thin_volume"]) < 0 \
            or g["slip_stop_mult"] < 1 or g["slip_thin_mult"] < 1:
        errs.append("Slippage: percent, ticks and thin-volume must be >= 0; stop and thin multipliers >= 1.")
    if g["entry_field"] not in ("open", "close") or g["exit_field"] not in ("open", "close"):
        errs.append("Entry / exit price field must be open or close.")
    if g["expiry"] not in EXPIRY_RULES:
        errs.append("Choose a valid expiry rule.")
    if g.get("atm_reference", ATMReference.SPOT.value) not in (
            ATMReference.SPOT.value, ATMReference.SYNTHETIC_FUTURE.value):
        errs.append("Choose ATM reference from NIFTY spot or synthetic future.")
    if g.get("rollover_strike_mode", RolloverStrikeMode.ORIGINAL.value) not in (
            RolloverStrikeMode.ORIGINAL.value, RolloverStrikeMode.RESELECT.value):
        errs.append("Choose a valid expiry-rollover strike policy.")
    if not 0 <= g["greek_div_yield"] <= 20:
        errs.append("Dividend yield must be between 0 and 20 %.")
    lo, hi = g.get("dte_min"), g.get("dte_max")
    if (lo is not None and lo < 0) or (hi is not None and hi < 0):
        errs.append("Days-to-expiry filters cannot be negative.")
    if lo is not None and hi is not None and lo > hi:
        errs.append("Minimum days to expiry is larger than the maximum.")
    return errs


def validate(params: Dict[str, Any], g: Dict[str, Any]) -> List[str]:
    errs: List[str] = validate_globals(g)
    if g["end"] < g["start"]:
        errs.append("End date must be on or after the start date.")
    if not g["weekdays"]:
        errs.append("Select at least one weekday.")
    if params["kind"] == "multi":
        if not params["legs"]:
            errs.append("Add at least one leg.")
        if len(params["legs"]) > MAX_LEGS:
            errs.append(f"At most {MAX_LEGS} legs.")
        if g["exit_time"] <= g["entry_time"]:
            errs.append("Exit time must be later than entry time.")
        for i, leg in enumerate(params["legs"], 1):
            if not 0 <= int(leg["expiry_offset"]) <= 2:
                errs.append(f"Leg {i}: expiry offset must be between 0 and 2.")
            if leg["re_mode"] != "Off" and leg["sl_kind"] == "Off":
                errs.append(f"Leg {i}: re-entry needs a stop-loss.")
            if leg["trail_on"] and leg["sl_kind"] == "Off":
                errs.append(f"Leg {i}: trailing SL needs a stop-loss.")
        p = params["portfolio"]
        if p["trail_on"] and p["sl_kind"] == "Off":
            errs.append("Portfolio trailing SL needs a portfolio stop-loss.")
        entry = params.get("entry") or default_entry()
        if entry["window_end"]:
            end = _t(entry["window_end"])
            if not g["entry_time"] <= end < g["exit_time"]:
                errs.append("Entry window end must be between the entry time and the square-off time.")
        for i, condition in enumerate(entry["conditions"], 1):
            try:
                _cond(condition)
            except (ValueError, TypeError, KeyError) as exc:
                errs.append(f"Entry condition {i}: {exc}")
        if not 0 <= g["greek_rate"] <= 25:
            errs.append("Risk-free rate must be between 0 and 25 %.")
    else:
        ind = params["indicator"]
        if any(ind[key] not in ("1m", "5m", "15m", "30m", "60m", "1d", "1w", "1mo")
               for key in ("st_timeframe", "vwap_timeframe", "ema_timeframe", "macd_timeframe")):
            errs.append("Choose a valid indicator timeframe.")
        if not ind["start_time"] <= ind["last_entry_time"] < ind["exit_time"]:
            errs.append("Need start time <= last entry time < exit time.")
        if ind["type"] not in ("Supertrend", "VWAP", "EMA", "MACD"):
            errs.append("Choose Supertrend, VWAP, EMA, or MACD.")
        if ind["type"] == "MACD":
            if int(ind["macd_fast"]) < 1 or int(ind["macd_slow"]) < 1 or int(ind["macd_signal"]) < 1:
                errs.append("MACD periods must be at least 1.")
            elif int(ind["macd_fast"]) >= int(ind["macd_slow"]):
                errs.append("Fast MACD period must be less than slow MACD period.")
        if ind["type"] == "EMA":
            if ind["ema_source"] not in ("Underlying", "Option premium"):
                errs.append("Choose an EMA source: underlying or option premium.")
            if ind["ema_mode"] not in ("Price vs EMA", "EMA crossover"):
                errs.append("Choose a valid EMA signal rule.")
            elif ind["ema_mode"] == "EMA crossover" and int(ind["ema_fast"]) >= int(ind["ema_slow"]):
                errs.append("Fast EMA period must be less than slow EMA period.")
            if ind["ema_slope_filter"] and ind["ema_slope_type"] not in ("EMA", "SMA"):
                errs.append("Trend average must be EMA or SMA.")
        legs = params.get("indicator_legs") or [params["ind_leg"]]
        if not 1 <= len(legs) <= MAX_LEGS:
            errs.append(f"Indicator strategies need between 1 and {MAX_LEGS} option legs.")
        for i, leg in enumerate(legs, 1):
            if int(leg.get("expiry_offset", 0)) != 0:
                errs.append(f"Indicator leg {i}: separate expiries are only supported for multi-leg strategies.")
            if leg["trail_on"] and leg["sl_kind"] == "Off":
                errs.append(f"Leg {i}: trailing SL needs a stop-loss.")
    return errs


def build_registry(g: Dict[str, Any]) -> InstrumentRegistry:
    kw: Dict[str, Any] = {"brokerage_per_order": float(g["brokerage"]), "strike_step": int(g["strike_interval"])}
    if g["lot_mode"] == "manual":
        kw["fixed_lot_size"] = int(g["lot_size"])
    return InstrumentRegistry([dataclasses.replace(NIFTY, **kw)])


def indicator_warmup_days(params: Dict[str, Any]) -> int:
    """Estimate the calendar range needed to seed the selected indicator bars."""
    ind = params["indicator"]
    if ind["type"] == "Supertrend":
        timeframe, bars = ind["st_timeframe"], int(ind["st_period"]) * 3
    elif ind["type"] == "EMA":
        timeframe = ind["ema_timeframe"]
        bars = max(int(ind["ema_period"]), int(ind["ema_slow"]), int(ind["ema_slope_period"]))
    elif ind["type"] == "MACD":
        timeframe = ind["macd_timeframe"]
        bars = int(ind["macd_slow"]) + int(ind["macd_signal"]) - 1
    else:
        timeframe, bars = ind["vwap_timeframe"], int(ind["vwap_confirm"]) + 1
    if timeframe.endswith("m"):
        calendar_days = bars * int(timeframe[:-1]) * 1.5 / 375
    elif timeframe == "1d":
        calendar_days = bars * 1.5
    elif timeframe == "1w":
        calendar_days = bars * 10.5
    else:
        calendar_days = bars * 46.5
    return max(10, ceil(calendar_days) + 10)


def strike_window(params: Dict[str, Any]) -> Tuple[int, Tuple[float, ...]]:
    """(extra strike steps beyond the day's spot range, fixed strikes) so every resolvable strike is loaded."""
    legs = params["legs"] if params["kind"] == "multi" else [params["ind_leg"]]
    extra, fixed = 6, []
    for leg in legs:
        if leg["rule"] == "ATM Offset":
            extra = max(extra, abs(int(leg["offset"])) + 4)
        elif leg["rule"] in ("Premium", "Greek"):
            extra = max(extra, 30)
        elif leg["rule"] == "Fixed":
            fixed.append(float(leg["fixed"]))
    return extra, tuple(fixed)


def needs_multiple_expiries(params: Dict[str, Any]) -> bool:
    legs = params["legs"] if params["kind"] == "multi" else params.get("indicator_legs", [])
    return any(int(leg.get("expiry_offset", 0)) > 0 for leg in legs)


def build_config(params: Dict[str, Any], g: Dict[str, Any]):
    """Returns StrategyConfig (multi-leg) or SignalStrategyConfig (indicator)."""
    sm = SlippageModel(pct=float(g["slip_pct"]), min_ticks=float(g["slip_min_ticks"]),
                       stop_mult=float(g["slip_stop_mult"]), thin_volume=float(g["slip_thin_volume"]),
                       thin_mult=float(g["slip_thin_mult"]))
    gs = GreekSettings(rate=float(g["greek_rate"]) / 100.0, forward=g["greek_forward"],
                       div_yield=float(g["greek_div_yield"]) / 100.0)
    common = dict(slippage_model=sm, dte_min=g.get("dte_min"), dte_max=g.get("dte_max"), greek=gs,
                  capital=g["capital"] or None,
                  start_date=g["start"], end_date=g["end"],
                  weekdays=tuple(WEEKDAY_NAMES.index(w) for w in g["weekdays"]),
                  max_expiry_gap_days=7 if g["gap_guard"] else None, exit_price_field=g["exit_field"],
                  holding_mode=HoldingMode(g["holding_mode"]),
                  rollover_strike_mode=RolloverStrikeMode(g["rollover_strike_mode"]),
                  expiry_day_only=bool(g["expiry_day_only"]))
    expiry = EXPIRY_RULES[g["expiry"]]
    if params["kind"] == "multi":
        p = params["portfolio"]
        psl = psl_pct = psl_points = ptp = ptp_pct = None
        if p["sl_kind"] == "₹":
            psl = float(p["sl_val"])
        elif p["sl_kind"] == "% of premium":
            psl_pct = float(p["sl_val"])
        elif p["sl_kind"] == "pts":
            psl_points = float(p["sl_val"])
        if p["tp_kind"] == "₹":
            ptp = float(p["tp_val"])
        elif p["tp_kind"] == "% of premium":
            ptp_pct = float(p["tp_val"])
        trail = PortfolioTrail(float(p["trail_trig"]), float(p["trail_step"])) if p["trail_on"] else None
        entry = params.get("entry") or default_entry()
        conditions = tuple(_cond(c) for c in entry["conditions"])
        window_end = _t(entry["window_end"]) if entry["window_end"] else None
        return StrategyConfig(
            name="Multi-leg strategy", underlying="NIFTY", entry_time=g["entry_time"], exit_time=g["exit_time"],
            legs=tuple(to_leg(l, ATMReference(g["atm_reference"])) for l in params["legs"]),
            expiry=expiry, portfolio_sl=psl, portfolio_tp=ptp,
            portfolio_sl_pct=psl_pct, portfolio_sl_points=psl_points, portfolio_tp_pct=ptp_pct,
            portfolio_trail=trail,
            entry_conditions=conditions, entry_window_end=window_end,
            entry_price_field=g["entry_field"], **common)
    ind = params["indicator"]
    indicator_legs = tuple(to_leg({**leg, "re_mode": "Off"}) for leg in
                           (params.get("indicator_legs") or [params["ind_leg"]]))
    leg = indicator_legs[0]
    expression = _indicator_expression_value(
        ind,
        params.get("indicator_legs") or [params.get("ind_leg", {})]
    )
    if ind["type"] == "Supertrend":
        timeframe_min = int(ind["st_timeframe"][:-1]) if ind["st_timeframe"].endswith("m") else 5
        indicator = SupertrendParams(int(ind["st_period"]), float(ind["st_mult"]), timeframe_min,
                                     bool(ind["st_prev"]), ind["st_timeframe"])
    elif ind["type"] == "VWAP":
        indicator = VwapParams(VwapSource.SPOT if ind["vwap_source"] == "Spot" else VwapSource.PREMIUM,
                               float(ind["vwap_buf"]), int(ind["vwap_confirm"]), ind["vwap_timeframe"])
    elif ind["type"] == "MACD":
        indicator = MACDParams(int(ind["macd_fast"]), int(ind["macd_slow"]),
                               int(ind["macd_signal"]), ind["macd_timeframe"])
    else:
        indicator = EMAParams(
            mode=(EMASignalMode.PRICE_VS_EMA if ind["ema_mode"] == "Price vs EMA"
                  else EMASignalMode.EMA_CROSSOVER),
            period=int(ind["ema_period"]), fast_period=int(ind["ema_fast"]),
            slow_period=int(ind["ema_slow"]), slope_filter=bool(ind["ema_slope_filter"]),
            slope_type=MovingAverageType(ind["ema_slope_type"]),
            slope_period=int(ind["ema_slope_period"]), timeframe=ind["ema_timeframe"],
            source=(EMASource.UNDERLYING if ind["ema_source"] == "Underlying"
                    else EMASource.OPTION))
    return SignalStrategyConfig(
        name=f"{ind['type']} signals", underlying="NIFTY", indicator=indicator,
        expression=expression,
        start_time=ind["start_time"], last_entry_time=ind["last_entry_time"], exit_time=ind["exit_time"],
        expiry=expiry, strike=leg.strike, lots=leg.lots, stop_loss=leg.stop_loss, target=leg.target,
        trail_sl=leg.trail_sl,
        on_opposite={"Reverse": OppositeSignal.REVERSE, "Exit": OppositeSignal.EXIT,
                     "Ignore": OppositeSignal.IGNORE}[ind["on_opposite"]],
        max_trades_per_day=int(ind["max_trades"]), trade_initial_trend=bool(ind["initial_trend"]),
        signal_legs=indicator_legs,
        daily_max_loss=float(ind["daily_max_loss"]) or None, daily_target=float(ind["daily_target"]) or None,
        **common)


def default_globals(first: Optional[date] = None, last: Optional[date] = None) -> Dict[str, Any]:
    """Engine-side settings. The slippage numbers are a SUGGESTED starting point for liquid NIFTY weeklies
    (about one tick or 0.1% of premium per fill, doubled on stop exits) - calibrate them to your own fills."""
    return {"start": first, "end": last, "weekdays": list(WEEKDAY_NAMES), "entry_time": time(9, 30),
            "exit_time": time(15, 15), "expiry": "Nearest weekly", "gap_guard": True, "lot_mode": "auto",
            "lot_size": 65, "strike_interval": 50, "capital": 100000.0, "brokerage": 20.0,
            "slip_pct": 0.10, "slip_min_ticks": 1.0, "slip_stop_mult": 2.0, "slip_thin_volume": 0.0,
            "slip_thin_mult": 1.0, "dte_min": None, "dte_max": None,
            "greek_rate": 6.5, "greek_forward": "parity", "greek_div_yield": 0.0,
            "entry_field": "open", "exit_field": "close", "holding_mode": HoldingMode.INTRADAY.value,
            "rollover_strike_mode": RolloverStrikeMode.ORIGINAL.value,
            "atm_reference": ATMReference.SPOT.value, "expiry_day_only": False}


# --------------------------------------------------------------------------- JSON <-> python
_TIME_KEYS_SETTINGS = ("entry_time", "exit_time")
_TIME_KEYS_IND = ("start_time", "last_entry_time", "exit_time")


def _t(v) -> time:
    if isinstance(v, time):
        return v
    h, m = str(v).split(":")[:2]
    return time(int(h), int(m))


def _d(v) -> Optional[date]:
    if v in (None, ""):
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def _coerce(defaults: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(defaults)
    for k, v in (values or {}).items():
        if k not in out:
            continue
        d = out[k]
        if isinstance(d, bool):
            out[k] = bool(v)
        elif isinstance(d, float):
            out[k] = float(v)
        elif isinstance(d, int):
            out[k] = int(v)
        else:
            out[k] = v
    return out


def parse_settings(raw: Dict[str, Any]) -> Dict[str, Any]:
    base = default_globals()
    g = dict(base)
    for k, v in (raw or {}).items():
        if k in ("start", "end"):
            g[k] = _d(v)
        elif k in _TIME_KEYS_SETTINGS:
            g[k] = _t(v)
        elif k in ("dte_min", "dte_max"):
            g[k] = None if v in (None, "") else int(v)
        elif k in base:
            d = base[k]
            g[k] = (HoldingMode(v).value if k == "holding_mode" else
                RolloverStrikeMode(v).value if k == "rollover_strike_mode" else
                bool(v) if isinstance(d, bool) else float(v) if isinstance(d, float)
                else int(v) if isinstance(d, int) else v)
    if g["start"] is None or g["end"] is None:
        raise ValueError("start and end dates are required")
    return g


def parse_strategy(raw: Dict[str, Any]) -> Dict[str, Any]:
    base = default_params()
    p = raw or {}
    out = {"kind": p.get("kind", "multi")}
    out["legs"] = [_coerce(default_leg(i), leg) for i, leg in enumerate(p.get("legs", base["legs"]))]
    out["portfolio"] = _coerce(default_portfolio(), p.get("portfolio", {}))
    raw_entry = p.get("entry") or {}
    conditions = []
    for condition in raw_entry.get("conditions", []):
        c = _coerce(default_condition(), {k: v for k, v in condition.items() if k != "value"})
        if c["kind"] in ("spot_vs_vwap", "supertrend"):
            c["value"] = str(condition.get("value", "above" if c["kind"] == "spot_vs_vwap" else "up"))
        else:
            c["value"] = float(condition.get("value", 0.0))
        conditions.append(c)
    out["entry"] = {"window_end": str(raw_entry.get("window_end") or ""), "conditions": conditions}
    out["ind_leg"] = _coerce(base["ind_leg"], p.get("ind_leg", {}))
    raw_legs = p.get("indicator_legs")
    if not isinstance(raw_legs, list) or not raw_legs:
        raw_legs = [p.get("ind_leg", base["ind_leg"])]
    out["indicator_legs"] = [
        _coerce({**default_leg(i), "side": "Buy"}, leg)
        for i, leg in enumerate(raw_legs[:MAX_LEGS])
    ]
    raw_ind = dict(p.get("indicator", {}))
    if "st_timeframe" not in raw_ind and "st_tf" in raw_ind:
        raw_ind["st_timeframe"] = f"{int(raw_ind['st_tf'])}m"
    first_leg = (raw_legs[0] if isinstance(raw_legs, list) and raw_legs else p.get("ind_leg", base["ind_leg"]))
    if first_leg:
        raw_ind["expression"] = (
            "Buy option with the trend" if str(first_leg.get("side", "Buy")).lower().startswith("buy")
            else "Sell option with the trend")
    ind = _coerce({k: v for k, v in default_indicator().items() if k not in _TIME_KEYS_IND}, raw_ind)
    di = default_indicator()
    for k in _TIME_KEYS_IND:
        ind[k] = _t(p.get("indicator", {}).get(k, di[k]))
    out["indicator"] = ind
    return out


def strategy_to_json(params: Dict[str, Any]) -> Dict[str, Any]:
    out = {**params, "indicator": dict(params["indicator"]) }
    out["indicator"].pop("expression", None)
    for k in _TIME_KEYS_IND:
        out["indicator"][k] = params["indicator"][k].strftime("%H:%M")
    return out


def settings_to_json(g: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(g)
    for k in ("start", "end"):
        out[k] = g[k].isoformat() if g[k] else None
    for k in _TIME_KEYS_SETTINGS:
        out[k] = g[k].strftime("%H:%M")
    return out
