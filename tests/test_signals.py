"""Indicator + signal-strategy tests. Run:  python -m unittest discover -s tests -v"""
import os
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, time
from unittest.mock import patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optbt import (NIFTY, DataStore, DayData, OppositeSignal, OptionExpression, RiskRule,
                   EMAParams, EMASignalMode, EMASource, HoldingMode, LegConfig, MovingAverageType,
                   MACDParams,
                   OptionType, Side, SignalStrategyConfig, StrikeSelection,
                   SupertrendParams, VwapParams, VwapSource,
                   run_signal_backtest, simulate_signal_day, supertrend, vwap)
from optbt.data import C, N_BARS, O, bar_index
from optbt.signals import (_ema_signals, _macd_curves, _moving_average, _timeframe_groups,
                           compute_signals, compute_signals_for_days,
                           indicator_backtest_chart_data, indicator_signal_values_for_days,
                           indicator_display_for_day, indicator_ema_values_for_days)
from optbt.signal_engine import _build_events_with_prior
from lab.service import Service
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synth import make_synthetic_market

D, EXP = date(2025, 1, 6), date(2025, 1, 9)
STRIKES = range(22000, 25050, 50)


def spot_path(close):
    close = np.asarray(close, float)
    o = np.concatenate(([close[0]], close[:-1]))
    return np.vstack([o, np.maximum(o, close) + 1, np.minimum(o, close) - 1, close])


def make_day(close, ce_fn=None, pe_fn=None, vol=0.0):
    sp = spot_path(close)
    opts = {}
    for K in STRIKES:
        for ot, fn in (("CE", ce_fn), ("PE", pe_fn)):
            if fn:
                p = fn(sp[3], K)
            else:
                p = (np.maximum(sp[3] - K, 0) if ot == "CE" else np.maximum(K - sp[3], 0)) + 50.0
            a = np.vstack([p, p, p, p, np.full(N_BARS, vol)])
            opts[(EXP, K, ot)] = a
    return DayData(D, sp, opts, [EXP])


def _signals_for_days(days, signal_fn):
    return {day.trade_date: signal_fn(day) for day in days}


def v_shape():
    up = np.linspace(23400, 23700, 150)          # rally for 150 minutes
    down = np.linspace(23700, 23300, 150)        # sell-off
    flat = np.full(N_BARS - 300, 23300.0)
    return np.concatenate([up, down, flat])


def st_cfg(**kw):
    base = dict(name="st", underlying="NIFTY", indicator=SupertrendParams(3, 1.0, 5, use_prev_day=False),
                apply_costs=False)
    base.update(kw)
    return SignalStrategyConfig(**base)


class IndicatorTests(unittest.TestCase):
    def test_macd_signals_use_closed_candles_and_next_minute_availability(self):
        close = np.concatenate([
            np.linspace(23000, 23400, 120),
            np.linspace(23400, 22900, 120),
            np.linspace(22900, 23600, N_BARS - 240),
        ])
        day = make_day(close)
        params = MACDParams(3, 8, 4)
        cfg = SignalStrategyConfig(name="MACD", underlying="NIFTY", indicator=params,
                                   apply_costs=False)

        output = compute_signals_for_days([day], cfg, NIFTY, lambda *_args: None)[D]
        signal, valid = output[0], output[1]
        macd, signal_line = _macd_curves(close, params)
        expected = np.where(macd > signal_line, 1,
                            np.where(macd < signal_line, -1, 0)).astype(np.int8)
        comparable = np.isfinite(macd[:-1]) & np.isfinite(signal_line[:-1])

        np.testing.assert_array_equal(valid[1:][comparable], np.ones(comparable.sum(), dtype=bool))
        np.testing.assert_array_equal(signal[1:][comparable], expected[:-1][comparable])
        self.assertFalse(valid[0])

    def test_macd_chart_uses_separate_panel_and_rolls_partial_bar_forward(self):
        close = np.linspace(23000, 23600, N_BARS)
        day = make_day(close)
        cfg = SignalStrategyConfig(name="MACD", underlying="NIFTY",
                                   indicator=MACDParams(2, 5, 3, "60m"), apply_costs=False)
        chart = indicator_backtest_chart_data([day], cfg)

        self.assertEqual(chart["lines"], [])
        self.assertEqual(len(chart["bars"]), 7)
        self.assertEqual(chart["pane"]["label"], "MACD(2, 5, 3) · 60m")
        self.assertEqual(len(chart["pane"]["histogram"]), 7)
        self.assertEqual(chart["pane"]["histogram"][-1], chart["pane"]["histogram"][-2])

    def test_macd_direct_signal_generation_and_replay_values(self):
        day = make_day(np.linspace(23000, 23600, N_BARS))
        params = MACDParams(2, 5, 3)
        cfg = SignalStrategyConfig(name="MACD", underlying="NIFTY", indicator=params,
                                   apply_costs=False)
        signal, valid, _, error = compute_signals(day, cfg, NIFTY, EXP, 0, lambda *_args: None)
        self.assertIsNone(error)
        self.assertTrue(valid.any())
        self.assertTrue(set(np.unique(signal[valid])).issubset({-1, 1}))

        display = indicator_display_for_day([day], cfg, D)
        self.assertEqual([line["label"] for line in display["pane_lines"]], ["MACD", "Signal"])
        self.assertEqual(len(display["pane_histogram"]), N_BARS)
        self.assertEqual(display["lines"], [])

    def test_trade_signal_value_uses_last_closed_indicator_bar(self):
        close = np.linspace(23000, 23600, N_BARS)
        day = make_day(close)
        params = MACDParams(2, 5, 3)
        cfg = SignalStrategyConfig(name="MACD", underlying="NIFTY", indicator=params,
                                   apply_costs=False)

        values = indicator_signal_values_for_days([day], cfg)[D]
        macd, signal = _macd_curves(close, params)

        self.assertTrue(np.isnan(values[0]))
        self.assertAlmostEqual(values[10], macd[9] - signal[9])
        self.assertAlmostEqual(values[-1], macd[-2] - signal[-2])

    def test_ema_trade_values_show_ema_line_and_keep_signed_signal_gap(self):
        close = np.linspace(23000, 23600, N_BARS)
        day = make_day(close)
        cfg = SignalStrategyConfig(name="EMA", underlying="NIFTY",
                                   indicator=EMAParams(period=5), apply_costs=False)

        readings = indicator_signal_values_for_days([day], cfg)[D]
        ema_values = indicator_ema_values_for_days([day], cfg)[D]["ema"]
        expected_ema = _moving_average(close, 5, MovingAverageType.EMA)

        self.assertAlmostEqual(ema_values[20], expected_ema[19])
        self.assertAlmostEqual(readings[20], close[19] - expected_ema[19])
        self.assertGreater(ema_values[20], 0)
        self.assertNotEqual(ema_values[20], readings[20])

    def test_ema_crossover_trade_values_show_both_ema_lines(self):
        close = np.linspace(23000, 23600, N_BARS)
        day = make_day(close)
        cfg = SignalStrategyConfig(name="EMA crossover", underlying="NIFTY",
                                   indicator=EMAParams(mode=EMASignalMode.EMA_CROSSOVER,
                                                       fast_period=3, slow_period=10),
                                   apply_costs=False)

        values = indicator_ema_values_for_days([day], cfg)[D]

        self.assertAlmostEqual(values["fast_ema"][20],
                               _moving_average(close, 3, MovingAverageType.EMA)[19])
        self.assertAlmostEqual(values["slow_ema"][20],
                               _moving_average(close, 10, MovingAverageType.EMA)[19])

    def test_trade_signal_value_carries_last_closed_candle_through_partial_tail(self):
        day = make_day(np.linspace(23000, 23600, N_BARS))
        cfg = SignalStrategyConfig(name="MACD", underlying="NIFTY",
                                   indicator=EMAParams(period=2, timeframe="60m"), apply_costs=False)

        values = indicator_signal_values_for_days([day], cfg)[D]
        ema_lines = indicator_ema_values_for_days([day], cfg)[D]

        self.assertTrue(np.isfinite(values[bar_index(time(15, 15))]))
        self.assertEqual(values[bar_index(time(15, 15))], values[-1])
        self.assertEqual(ema_lines["spot"][bar_index(time(10, 15))], day.spot[C, 59])

    def test_trade_verification_shows_signal_spot_separately_from_indicator_and_fill(self):
        close = np.linspace(23000, 23600, N_BARS)
        day = make_day(close)
        entry_bar, exit_bar = bar_index(time(10, 0)), bar_index(time(10, 5))
        day.spot[O, entry_bar] = close[entry_bar - 1] + 8
        day.spot[O, exit_bar] = close[exit_bar - 1] - 4
        cfg = SignalStrategyConfig(name="EMA", underlying="NIFTY",
                                   indicator=EMAParams(period=2), apply_costs=False)

        class SingleDayStore:
            dates = [D]
            history_dates = [D]

            def build_spot_day(self, trade_date):
                return day

        trades = pd.DataFrame([{
            "entry_time": pd.Timestamp(datetime.combine(D, time(10, 0))),
            "exit_time": pd.Timestamp(datetime.combine(D, time(10, 5))),
            "option_type": "CE",
        }])
        Service._add_trade_verification_values(trades, cfg, SingleDayStore(), True)

        self.assertEqual(trades.at[0, "signal_spot_entry"], day.spot[C, entry_bar - 1])
        self.assertEqual(trades.at[0, "signal_spot_exit"], day.spot[C, exit_bar - 1])
        self.assertEqual(trades.at[0, "spot_entry"], day.spot[O, entry_bar])
        self.assertEqual(trades.at[0, "spot_exit"], day.spot[C, exit_bar])
        self.assertTrue(np.isfinite(trades.at[0, "signal_value_entry"]))
        self.assertNotEqual(trades.at[0, "signal_value_entry"], trades.at[0, "signal_spot_entry"])
        self.assertEqual(trades.at[0, "signal_ema_entry"],
                         _moving_average(close, 2, MovingAverageType.EMA)[entry_bar - 1])
        self.assertEqual(trades.at[0, "signal_ema_exit"],
                         _moving_average(close, 2, MovingAverageType.EMA)[exit_bar - 1])

    def test_existing_spot_ema_trade_rows_backfill_ema_level(self):
        trades = pd.DataFrame([{
            "signal_value_entry": -1.0149,
            "signal_spot_entry": 26151.60,
        }])
        strategy = {"indicator": {"type": "EMA", "ema_source": "Underlying",
                                  "ema_mode": "Price vs EMA"}}

        Service._backfill_spot_ema_values(trades, strategy)

        self.assertAlmostEqual(trades.at[0, "signal_ema_entry"], 26152.6149)

    def test_indicator_signals_execute_each_configured_leg(self):
        class SingleDayStore:
            dates = [D]

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.linspace(23500.0, 23600.0, N_BARS))
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        def signals(_day, *_args):
            sig = np.ones(N_BARS, dtype=np.int8)
            sig[bar_index(time(10, 0)):] = -1
            return sig, np.ones(N_BARS, dtype=bool), None, None

        cfg = SignalStrategyConfig(
            name="two-leg indicator", underlying="NIFTY", indicator=EMAParams(period=5),
            signal_legs=(
                LegConfig(Side.BUY, OptionType.CE, StrikeSelection.atm(), 1),
                LegConfig(Side.SELL, OptionType.PE, StrikeSelection.atm(), 2),
            ),
            trade_initial_trend=True, start_date=D, end_date=D,
            weekdays=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=lambda days, *_args: _signals_for_days(days, signals)):
            result = run_signal_backtest(cfg, SingleDayStore())
            carried = run_signal_backtest(
                replace(cfg, holding_mode=HoldingMode.CARRY_SINGLE), SingleDayStore())

        first_entries = result.trades[result.trades["entry_time"].dt.time == time(9, 30)].set_index("leg_id")
        self.assertEqual(set(first_entries.index), {"L1", "L2"})
        self.assertEqual(first_entries.loc["L1", ["side", "option_type", "lots"]].tolist(), ["BUY", "CE", 1])
        self.assertEqual(first_entries.loc["L2", ["side", "option_type", "lots"]].tolist(), ["SELL", "PE", 2])
        carried_entries = carried.trades[carried.trades["entry_time"].dt.time == time(9, 30)]
        self.assertEqual(set(carried_entries["leg_id"]), {"L1", "L2"})
        self.assertTrue((carried.trades[carried.trades["exit_time"].dt.time == time(10, 0)]
                         ["exit_reason"] == "SIGNAL_EXIT").all())

    def test_intraday_timeframes_drop_incomplete_trailing_candles(self):
        day = make_day(np.linspace(100.0, 200.0, N_BARS))

        groups, bucket_map = _timeframe_groups([day], "60m")

        self.assertEqual(len(groups), 6)
        self.assertEqual(groups[-1][0][2], 360)
        self.assertTrue((bucket_map[0][360:] == -1).all())

    def test_daily_weekly_monthly_signals_only_use_closed_periods(self):
        dates_by_timeframe = {
            "1d": [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)],
            "1w": [date(2025, 1, 6), date(2025, 1, 13), date(2025, 1, 20)],
            "1mo": [date(2025, 1, 15), date(2025, 2, 15), date(2025, 3, 15)],
        }
        for timeframe, dates in dates_by_timeframe.items():
            with self.subTest(timeframe=timeframe):
                days = []
                for d, price in zip(dates, (100.0, 200.0, 300.0)):
                    day = make_day(np.full(N_BARS, price))
                    day.trade_date = d
                    day.options = {}
                    days.append(day)
                cfg = SignalStrategyConfig(
                    name="timeframe", underlying="NIFTY",
                    indicator=EMAParams(period=2, timeframe=timeframe),
                    start_date=dates[-1], end_date=dates[-1], weekdays=None, apply_costs=False)
                first = compute_signals_for_days(days, cfg, NIFTY, lambda *args: None)
                days[-1].spot[:] = spot_path(np.full(N_BARS, 50.0))
                changed_future = compute_signals_for_days(days, cfg, NIFTY, lambda *args: None)

                self.assertEqual(int(first[dates[-1]][0][-1]), 1)
                np.testing.assert_array_equal(first[dates[-1]][0], changed_future[dates[-1]][0])

    def test_daily_spot_vwap_uses_the_last_completed_session(self):
        dates = [date(2025, 1, 6), date(2025, 1, 7)]
        days = [make_day(np.linspace(23500.0, 23800.0, N_BARS)),
                make_day(np.full(N_BARS, 24000.0))]
        for day, d in zip(days, dates):
            day.trade_date = d
        cfg = SignalStrategyConfig(
            name="daily spot VWAP", underlying="NIFTY",
            indicator=VwapParams(source=VwapSource.SPOT, timeframe="1d"),
            start_date=dates[-1], end_date=dates[-1], weekdays=None, apply_costs=False)

        signals = compute_signals_for_days(days, cfg, NIFTY, lambda *args: None)

        self.assertEqual(int(signals[dates[-1]][0][-1]), 1)

    def test_intraday_vwap_accumulates_selected_bars_from_session_open(self):
        close = np.repeat(np.arange(N_BARS // 5, dtype=float) * 10.0 + 100.0, 5)
        day = DayData(D, np.vstack([close, close, close, close]), {}, [])
        cfg = SignalStrategyConfig(
            name="five-minute spot VWAP", underlying="NIFTY",
            indicator=VwapParams(source=VwapSource.SPOT, timeframe="5m"),
            weekdays=None, apply_costs=False)

        signals = compute_signals_for_days([day], cfg, NIFTY, lambda *args: None)[D]

        self.assertEqual(int(signals[0][9]), 0)
        self.assertEqual(int(signals[0][10]), 1)

    def test_ema_price_rule_uses_closed_spot_bars(self):
        close = np.linspace(100.0, 200.0, N_BARS)
        day = make_day(close)

        sig, valid = _ema_signals(day, EMAParams(period=5))

        self.assertFalse(valid[0])
        self.assertTrue(valid[1:].all())
        self.assertTrue((sig[:5] == 0).all())
        self.assertTrue((sig[5:] == 1).all())

    def test_hourly_ema_signal_updates_after_last_complete_bar(self):
        close = np.concatenate([
            np.full(60, 100.0), np.full(240, 110.0),
            np.full(60, 90.0), np.full(15, 90.0),
        ])
        day = make_day(close)
        day.trade_date = date(2026, 3, 18)
        cfg = SignalStrategyConfig(
            name="hourly EMA reversal", underlying="NIFTY",
            indicator=EMAParams(period=2, timeframe="60m"),
            weekdays=None, apply_costs=False,
        )

        sig, valid, _, _ = compute_signals_for_days(
            [day], cfg, NIFTY, lambda *args: None)[day.trade_date]
        events = _build_events_with_prior(sig, valid, 15, N_BARS - 1, N_BARS, False)

        self.assertEqual(sig[359], 1)
        self.assertEqual(sig[360], -1)
        self.assertTrue(valid[360])
        self.assertIn((360, -1), events)

    def test_one_minute_underlying_ema_carries_across_sessions_without_overnight_bars(self):
        dates = [D, date(2025, 1, 9)]
        days = [make_day(np.full(N_BARS, 200.0)), make_day(np.full(N_BARS, 100.0))]
        for day, trade_date in zip(days, dates):
            day.trade_date = trade_date
        cfg = SignalStrategyConfig(
            name="continuous 1m EMA", underlying="NIFTY", indicator=EMAParams(period=3),
            weekdays=None, apply_costs=False,
        )

        signals = compute_signals_for_days(days, cfg, NIFTY, lambda *args: None)

        self.assertTrue(signals[dates[1]][1][1])
        self.assertEqual(signals[dates[1]][0][1], -1)

    def test_one_minute_ema_backtest_uses_prior_session_history(self):
        dates = [D, date(2025, 1, 7)]

        class HistoryStore:
            def __init__(self):
                self.history_dates = dates
                self.dates = dates

            def build_spot_day(self, trade_date):
                price = 23700.0 if trade_date == dates[0] else 23500.0
                day = make_day(np.full(N_BARS, price))
                day.trade_date = trade_date
                return DayData(trade_date, day.spot, {}, [])

            def build_day(self, trade_date, *_args):
                price = 23700.0 if trade_date == dates[0] else 23500.0
                day = make_day(np.full(N_BARS, price))
                day.trade_date = trade_date
                day.selected_expiry = EXP
                return day, None

        cfg = SignalStrategyConfig(
            name="continuous EMA backtest", underlying="NIFTY", indicator=EMAParams(period=3),
            trade_initial_trend=True, start_date=dates[1], end_date=dates[1],
            weekdays=None, max_expiry_gap_days=None, apply_costs=False,
        )

        result = run_signal_backtest(cfg, HistoryStore())

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["signal_direction"], "BEARISH")
        self.assertEqual(result.trades.iloc[0]["option_type"], "PE")

        carried = run_signal_backtest(replace(cfg, holding_mode=HoldingMode.CARRY_SINGLE), HistoryStore())

        self.assertEqual(len(carried.trades), 1)
        self.assertEqual(carried.trades.iloc[0]["signal_direction"], "BEARISH")
        self.assertEqual(carried.trades.iloc[0]["option_type"], "PE")

    def test_ema_strategy_enters_when_average_becomes_ready(self):
        close = np.full(N_BARS, 23500.0)
        close[40:] = np.linspace(23500.0, 24000.0, N_BARS - 40)
        cfg = SignalStrategyConfig(name="ema", underlying="NIFTY",
                                   indicator=EMAParams(period=20), apply_costs=False)

        rows, why = simulate_signal_day(make_day(close), cfg, NIFTY)

        self.assertIsNone(why)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["side"], "BUY")
        self.assertGreaterEqual(bar_index(rows[0]["entry_time"].time()), 40)

    def test_option_ema_tracks_ce_and_pe_as_independent_streams(self):
        class OptionEmaStore:
            dates = [D]
            history_dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0),
                               lambda _spot, _strike: np.linspace(50.0, 100.0, N_BARS),
                               lambda _spot, _strike: np.linspace(100.0, 50.0, N_BARS))
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        cfg = SignalStrategyConfig(
            name="option EMA", underlying="NIFTY",
            indicator=EMAParams(period=5, source=EMASource.OPTION),
            start_date=D, end_date=D, weekdays=None, trade_initial_trend=True,
            max_trades_per_day=2, daily_target=1.0, apply_costs=False)

        result = run_signal_backtest(cfg, OptionEmaStore())

        self.assertEqual(set(result.trades["option_type"]), {"CE", "PE"})
        sides = result.trades.set_index("option_type")["side"].to_dict()
        self.assertEqual(sides, {"CE": "BUY", "PE": "SELL"})
        self.assertEqual(result.trades["entry_time"].nunique(), 1)
        self.assertEqual(set(result.trades["exit_reason"]), {"PORTFOLIO_TP"})

        short = run_signal_backtest(
            replace(cfg, expression=OptionExpression.SHORT_PREMIUM), OptionEmaStore())
        short_sides = short.trades.set_index("option_type")["side"].to_dict()
        self.assertEqual(short_sides, {"CE": "SELL", "PE": "BUY"})

    def test_option_ema_carry_single_allows_one_ce_and_one_pe(self):
        dates = [D, date(2025, 1, 7)]

        class OptionEmaCarryStore:
            dates = [D, date(2025, 1, 7)]
            history_dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0),
                               lambda _spot, _strike: np.linspace(50.0, 100.0, N_BARS),
                               lambda _spot, _strike: np.linspace(100.0, 50.0, N_BARS))
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        cfg = SignalStrategyConfig(
            name="carry option EMA", underlying="NIFTY",
            indicator=EMAParams(period=5, source=EMASource.OPTION),
            start_date=dates[0], end_date=dates[-1], weekdays=None,
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            max_trades_per_day=2, apply_costs=False)

        result = run_signal_backtest(cfg, OptionEmaCarryStore())

        self.assertEqual(set(result.trades["option_type"]), {"CE", "PE"})
        self.assertEqual(result.trades["entry_time"].nunique(), 1)

    def test_option_ema_daily_timeframe_uses_previous_closed_session(self):
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]
        days = []
        for d, premium in zip(dates, (100.0, 200.0, 300.0)):
            day = make_day(np.full(N_BARS, 23500.0),
                           lambda _spot, _strike, p=premium: np.full(N_BARS, p),
                           lambda _spot, _strike, p=premium: np.full(N_BARS, p))
            day.trade_date = d
            day.selected_expiry = EXP
            days.append(day)
        cfg = SignalStrategyConfig(
            name="daily option EMA", underlying="NIFTY",
            indicator=EMAParams(period=2, timeframe="1d", source=EMASource.OPTION),
            start_date=dates[-1], end_date=dates[-1], weekdays=None, apply_costs=False)
        resolve = lambda _strike, opt_type, _day, expiry, _idx, _spec, _field: (
            expiry, 23500, opt_type.value)

        signals = compute_signals_for_days(days, cfg, NIFTY, resolve)

        self.assertEqual(int(signals[dates[-1]]["CE"][0][0]), 1)
        self.assertEqual(int(signals[dates[-1]]["PE"][0][0]), 1)

    def test_ema_crossover_changes_direction_after_cross(self):
        close = np.concatenate([np.linspace(100, 140, 100), np.linspace(140, 80, N_BARS - 100)])
        day = make_day(close)

        sig, valid = _ema_signals(day, EMAParams(
            mode=EMASignalMode.EMA_CROSSOVER, fast_period=3, slow_period=10))

        self.assertTrue(valid[10:].all())
        self.assertEqual(sig[20], 1)
        self.assertEqual(sig[-1], -1)

    def test_indicator_display_returns_closed_candle_ema_curve(self):
        day = make_day(np.linspace(23400.0, 23600.0, N_BARS))
        cfg = SignalStrategyConfig(
            name="ema display", underlying="NIFTY", indicator=EMAParams(period=3),
            weekdays=None, apply_costs=False,
        )

        display = indicator_display_for_day([day], cfg, D)
        values = display["lines"][0]["values"]

        self.assertEqual(display["label"], "EMA · 1m")
        self.assertEqual(display["lines"][0]["label"], "EMA(3)")
        self.assertEqual(len(values), N_BARS)
        self.assertTrue(np.isnan(values[0]))
        self.assertTrue(np.isnan(values[1]))
        self.assertAlmostEqual(values[3], np.mean(day.spot[3, :3]))

    def test_one_minute_ema_display_carries_across_sessions(self):
        first = make_day(np.full(N_BARS, 200.0))
        first.trade_date = D
        second = make_day(np.full(N_BARS, 100.0))
        second.trade_date = date(2025, 1, 7)
        cfg = SignalStrategyConfig(
            name="daily-reset EMA display", underlying="NIFTY", indicator=EMAParams(period=3),
            weekdays=None, apply_costs=False,
        )

        display = indicator_display_for_day([first, second], cfg, second.trade_date)
        values = display["lines"][0]["values"]

        self.assertEqual(values[0], 200.0)
        self.assertEqual(values[1], 150.0)

    def test_ema_verification_chart_marks_the_backtest_signal_reversal(self):
        dates = [D, date(2025, 1, 7)]
        first = make_day(np.linspace(100.0, 200.0, N_BARS))
        second = make_day(np.full(N_BARS, 100.0))
        first.trade_date, second.trade_date = dates
        cfg = SignalStrategyConfig(
            name="EMA verification", underlying="NIFTY", indicator=EMAParams(period=3),
            weekdays=None, apply_costs=False,
        )

        display = indicator_display_for_day([first, second], cfg, dates[1])
        chart = display["verification"]
        reversals = [change for change in chart["changes"] if change["reversal"]]

        self.assertEqual(chart["timeframe"], "1m")
        self.assertEqual(chart["lines"][0]["label"], "EMA(3)")
        self.assertEqual(sum(bar["selected"] for bar in chart["bars"]), N_BARS)
        self.assertTrue(any(change["direction"] == -1
                            and chart["bars"][change["index"]]["date"] == dates[1].isoformat()
                            for change in reversals))

    def test_ema_verification_markers_align_with_signal_availability(self):
        rising = np.linspace(100.0, 200.0, N_BARS)
        falling = np.full(N_BARS, 100.0)
        cases = (
            ("1m", [rising, falling], 3),
            ("5m", [rising, falling], 3),
            ("1d", [np.full(N_BARS, value) for value in (100.0, 200.0, 100.0, 100.0)], 2),
        )

        for timeframe, closes, period in cases:
            with self.subTest(timeframe=timeframe):
                days = []
                for offset, close in enumerate(closes):
                    day = make_day(close)
                    day.trade_date = date.fromordinal(D.toordinal() + offset)
                    days.append(day)
                target = days[-2] if timeframe == "1d" else days[-1]
                cfg = SignalStrategyConfig(
                    name="EMA verification", underlying="NIFTY",
                    indicator=EMAParams(period=period, timeframe=timeframe),
                    weekdays=None, apply_costs=False,
                )

                chart = indicator_display_for_day(days, cfg, target.trade_date)["verification"]
                signals = compute_signals_for_days(days, cfg, NIFTY, lambda *_args: None)
                groups, _ = _timeframe_groups(days, timeframe)
                reversal = next(change for change in chart["changes"]
                                if change["reversal"] and change["date"] == target.trade_date.isoformat())
                source_group = next(i for i, group in enumerate(groups)
                                    if days[group[-1][0]].trade_date.isoformat() == reversal["date"]
                                    and group[-1][2] - 1 == reversal["time"])
                next_day_index, next_bar, _ = groups[source_group + 1][0]
                next_day = days[next_day_index]

                self.assertEqual(int(signals[next_day.trade_date][0][next_bar]), reversal["direction"])
                self.assertTrue(signals[next_day.trade_date][1][next_bar])

    def test_backtest_chart_uses_indicator_timeframe_and_overlays(self):
        days = []
        for offset, base in enumerate((100.0, 110.0, 105.0)):
            day = make_day(np.linspace(base, base + 10.0, N_BARS))
            day.trade_date = date.fromordinal(D.toordinal() + offset)
            days.append(day)

        indicators = (
            EMAParams(period=2, timeframe="60m"),
            SupertrendParams(period=2, timeframe="60m", use_prev_day=True),
            VwapParams(source=VwapSource.SPOT, timeframe="60m"),
        )
        for indicator in indicators:
            with self.subTest(indicator=type(indicator).__name__):
                cfg = SignalStrategyConfig(name="chart", underlying="NIFTY", indicator=indicator)
                chart = indicator_backtest_chart_data(days, cfg)

                self.assertEqual(chart["timeframe"], "60m")
                self.assertEqual(len(chart["bars"]), len(days) * 7)
                self.assertEqual([bar["time"] for bar in chart["bars"][:7]],
                                 ["10:14", "11:14", "12:14", "13:14", "14:14", "15:14", "15:29"])
                self.assertTrue(chart["lines"])
                self.assertEqual(len(chart["lines"][0]["values"]), len(chart["bars"]))
                self.assertEqual(chart["lines"][0]["values"][6],
                                 chart["lines"][0]["values"][5])

    def test_ema_slope_filter_uses_configured_sma(self):
        close = np.concatenate([np.linspace(100, 150, 100), np.linspace(150, 110, N_BARS - 100)])
        day = make_day(close)

        sig, _ = _ema_signals(day, EMAParams(
            period=3, slope_filter=True, slope_type=MovingAverageType.SMA, slope_period=3))

        self.assertEqual(sig[50], 1)
        self.assertEqual(sig[-1], -1)

    def test_supertrend_flips_down_after_crash(self):
        c = v_shape()[:300]
        h, l = c + 1, c - 1
        _, d = supertrend(h[::5], l[::5], c[::5], 3, 1.0)
        self.assertEqual(d[2], 1)                      # seeded in an uptrend
        self.assertTrue((d[3:28] == 1).all())          # stays up during the rally
        self.assertEqual(d[-1], -1)                    # flipped by the end of the sell-off

    def test_supertrend_unseeded_is_zero(self):
        c = np.linspace(100, 110, 20)
        _, d = supertrend(c + 1, c - 1, c, 10, 3.0)
        self.assertTrue((d[:9] == 0).all())
        self.assertNotEqual(d[9], 0)

    def test_vwap_volume_weighted(self):
        h = l = c = np.array([100.0, 110.0, 120.0])
        v = np.array([1.0, 1.0, 8.0])
        out = vwap(h, l, c, v)
        self.assertAlmostEqual(out[2], (100 + 110 + 960) / 10.0)

    def test_vwap_without_volume_is_running_mean(self):
        c = np.array([100.0, 110.0, 120.0])
        out = vwap(c, c, c, np.zeros(3))
        np.testing.assert_allclose(out, [100, 105, 110])


class SignalEngineTests(unittest.TestCase):
    def test_daily_indicator_uses_history_and_enters_after_prior_candle_closes(self):
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]

        class TimeframeStore:
            def __init__(self):
                self.history_dates = dates
                self.dates = dates

            def build_spot_day(self, d):
                price = {dates[0]: 23500.0, dates[1]: 23700.0, dates[2]: 23900.0}[d]
                day = make_day(np.full(N_BARS, price))
                day.trade_date = d
                return DayData(d, day.spot, {}, [])

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                price = {dates[0]: 23500.0, dates[1]: 23700.0, dates[2]: 23900.0}[d]
                day = make_day(np.full(N_BARS, price))
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        cfg = SignalStrategyConfig(
            name="daily EMA", underlying="NIFTY", indicator=EMAParams(period=2, timeframe="1d"),
            start_date=dates[-1], end_date=dates[-1], weekdays=None, apply_costs=False)

        result = run_signal_backtest(cfg, TimeframeStore())

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["date"], dates[-1])
        self.assertEqual(result.trades.iloc[0]["entry_time"].time(), time(9, 30))
        self.assertEqual(result.trades.iloc[0]["signal_direction"], "BULLISH")

        carried = run_signal_backtest(replace(cfg, holding_mode=HoldingMode.CARRY_SINGLE), TimeframeStore())
        self.assertEqual(len(carried.trades), 1)
        self.assertEqual(carried.trades.iloc[0]["entry_time"].time(), time(9, 30))

    def test_daily_premium_vwap_uses_contracts_selected_each_session(self):
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]

        class PremiumStore:
            def __init__(self):
                self.history_dates = dates
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                spot = {dates[0]: 23500.0, dates[1]: 23700.0, dates[2]: 23900.0}[d]
                rising = lambda _spot, _strike: np.linspace(50.0, 100.0, N_BARS)
                flat = lambda _spot, _strike: np.full(N_BARS, 100.0)
                ce_fn, pe_fn = (flat, rising) if d == dates[0] else (rising, flat)
                day = make_day(np.full(N_BARS, spot), ce_fn, pe_fn)
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        cfg = SignalStrategyConfig(
            name="daily premium VWAP", underlying="NIFTY",
            indicator=VwapParams(source=VwapSource.PREMIUM, confirm_bars=1, timeframe="1d"),
            start_date=dates[-1], end_date=dates[-1], weekdays=None, apply_costs=False)

        result = run_signal_backtest(cfg, PremiumStore())

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["signal_direction"], "BULLISH")
        self.assertEqual(result.trades.iloc[0]["entry_time"].time(), time(9, 30))

    def test_carry_single_reverses_within_the_same_session(self):
        class SingleDayStore:
            dates = [D]

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(v_shape())
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        cfg = st_cfg(trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
                     max_trades_per_day=10, start_date=D, end_date=D,
                     weekdays=None, max_expiry_gap_days=None, daily_target=0.01)
        result = run_signal_backtest(cfg, SingleDayStore())

        self.assertEqual(len(result.trades), 2)
        self.assertEqual(result.trades.iloc[0]["signal_direction"], "BULLISH")
        self.assertEqual(result.trades.iloc[0]["exit_reason"], "SIGNAL_EXIT")
        self.assertEqual(result.trades.iloc[1]["signal_direction"], "BEARISH")
        self.assertEqual(result.trades.iloc[1]["entry_time"], result.trades.iloc[0]["exit_time"])

    def test_carry_single_ignores_last_entry_time_for_reversals(self):
        class SingleDayStore:
            dates = [D]

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        def late_flips(_day, *_args):
            sig = np.ones(N_BARS, dtype=np.int8)
            sig[bar_index(time(11, 0)):] = -1
            sig[bar_index(time(12, 0)):] = 1
            return sig, np.ones(N_BARS, dtype=bool), None, None

        cfg = SignalStrategyConfig(
            name="late carry reversals", underlying="NIFTY", indicator=EMAParams(period=5),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            on_opposite=OppositeSignal.REVERSE, start_time=time(9, 30),
            last_entry_time=time(10, 0), start_date=D, end_date=D,
            weekdays=None, max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=lambda days, *_args: _signals_for_days(days, late_flips)):
            result = run_signal_backtest(cfg, SingleDayStore())

        trades = result.trades.sort_values("entry_time").reset_index(drop=True)
        self.assertEqual(len(trades), 3)
        self.assertEqual(list(trades["signal_direction"]), ["BULLISH", "BEARISH", "BULLISH"])
        self.assertEqual(list(trades["entry_time"].dt.time),
                         [time(9, 30), time(11, 0), time(12, 0)])
        self.assertEqual(list(trades["exit_reason"][:2]), ["SIGNAL_EXIT", "SIGNAL_EXIT"])
        self.assertEqual(list(trades["exit_time"].iloc[:2]), list(trades["entry_time"].iloc[1:]))

    def test_carry_single_reverses_on_every_opposite_signal_and_holds_across_sessions(self):
        dates = [D, date(2025, 1, 7)]

        class CarryStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.linspace(23500.0, 23600.0, N_BARS))
                day.trade_date = d
                day.expiries = [EXP]
                day.selected_expiry = EXP
                return day, None

        def fake_signals(day, *_args):
            sig = np.ones(N_BARS, dtype=np.int8)
            if day.trade_date == D:
                sig[bar_index(time(10, 0)):] = -1
                sig[bar_index(time(11, 0)):] = 1
            return sig, np.ones(N_BARS, dtype=bool), None, None

        cfg = SignalStrategyConfig(
            name="carry reversal", underlying="NIFTY", indicator=EMAParams(period=5),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            on_opposite=OppositeSignal.REVERSE, max_trades_per_day=1,
            start_date=dates[0], end_date=dates[-1], weekdays=None,
            max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=lambda days, *_args: _signals_for_days(days, fake_signals)):
            result = run_signal_backtest(cfg, CarryStore())

        trades = result.trades.sort_values("entry_time").reset_index(drop=True)
        self.assertEqual(len(trades), 3)
        self.assertEqual(list(trades["signal_direction"]), ["BULLISH", "BEARISH", "BULLISH"])
        self.assertEqual(list(trades["entry_time"].dt.time),
                         [time(9, 30), time(10, 0), time(11, 0)])
        self.assertEqual(list(trades["exit_reason"][:2]), ["SIGNAL_EXIT", "SIGNAL_EXIT"])
        self.assertEqual(list(trades["exit_time"].iloc[:2]), list(trades["entry_time"].iloc[1:]))

    def test_carry_single_uses_first_signal_change_of_next_session_not_square_off_time(self):
        dates = [D, date(2025, 1, 7)]

        class CarryStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.expiries = [EXP]
                day.selected_expiry = EXP
                return day, None

        def session_flip(day, *_args):
            sig = np.ones(N_BARS, dtype=np.int8)
            if day.trade_date == dates[1]:
                sig[:] = -1
            return sig, np.ones(N_BARS, dtype=bool), None, None

        cfg = SignalStrategyConfig(
            name="carry overnight reversal", underlying="NIFTY", indicator=EMAParams(period=5),
            signal_legs=(LegConfig(Side.BUY, OptionType.PE, StrikeSelection.atm(), 1),),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            start_time=time(9, 30), last_entry_time=time(9, 45), exit_time=time(10, 0),
            start_date=dates[0], end_date=dates[-1], weekdays=None,
            max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=lambda days, *_args: _signals_for_days(days, session_flip)):
            result = run_signal_backtest(cfg, CarryStore())

        trades = result.trades.sort_values("entry_time").reset_index(drop=True)
        self.assertEqual(list(trades["signal_direction"]), ["BULLISH", "BEARISH"])
        self.assertEqual(list(zip(trades["side"], trades["option_type"])),
                         [("BUY", "CE"), ("BUY", "PE")])
        self.assertEqual(list(trades["exit_reason"]), ["SIGNAL_EXIT", "BACKTEST_END"])
        self.assertEqual(trades.iloc[0]["exit_time"], trades.iloc[1]["entry_time"])
        self.assertEqual(trades.iloc[0]["exit_time"].date(), dates[1])
        self.assertEqual(trades.iloc[0]["exit_time"].time(), time(9, 30))
        self.assertNotIn("EOD_SQUARE_OFF", set(trades["exit_reason"]))

    def test_carry_single_skips_entry_at_contract_data_boundary(self):
        class SingleDayStore:
            dates = [D]

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        def final_bar_flip(_day, *_args):
            sig = np.ones(N_BARS, dtype=np.int8)
            sig[-1] = -1
            return sig, np.ones(N_BARS, dtype=bool), None, None

        cfg = SignalStrategyConfig(
            name="carry data boundary", underlying="NIFTY", indicator=EMAParams(period=5),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            on_opposite=OppositeSignal.REVERSE, start_date=D, end_date=D,
            weekdays=None, max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=lambda days, *_args: _signals_for_days(days, final_bar_flip)):
            result = run_signal_backtest(cfg, SingleDayStore())

        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades.iloc[0]["exit_reason"], "SIGNAL_EXIT")

    def test_carry_single_indicator_rolls_to_next_expiry_without_a_signal_exit(self):
        dates = [D, date(2025, 1, 7), date(2025, 1, 8), EXP,
                 date(2025, 1, 10), date(2025, 1, 13)]
        next_expiry = date(2025, 1, 16)

        class CarryStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.expiries = [EXP, next_expiry] if d >= date(2025, 1, 8) else [EXP]
                day.selected_expiry = EXP if d <= EXP else next_expiry
                if d >= date(2025, 1, 8):
                    day.options.update({
                        (next_expiry, strike, option_type): array.copy()
                        for (expiry, strike, option_type), array in list(day.options.items())
                        if expiry == EXP
                    })
                return day, None

        def bullish_signals(_day, *_args):
            return (np.ones(N_BARS, dtype=np.int8),
                    np.ones(N_BARS, dtype=bool), None, None)

        cfg = SignalStrategyConfig(
            name="carry expiry roll", underlying="NIFTY", indicator=EMAParams(period=5),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            start_time=time(9, 30), last_entry_time=time(9, 45), exit_time=time(10, 0),
            start_date=dates[0], end_date=dates[-1], weekdays=None,
            max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=lambda days, *_args: _signals_for_days(days, bullish_signals)):
            result = run_signal_backtest(cfg, CarryStore())

        trades = result.trades.sort_values("entry_time").reset_index(drop=True)
        self.assertEqual(len(trades), 2)
        self.assertEqual(list(trades["expiry"]), [EXP, next_expiry])
        self.assertEqual(list(trades["strike"]), [trades.iloc[0]["strike"]] * 2)
        self.assertEqual(trades.iloc[0]["exit_reason"], "ROLLED")
        self.assertEqual(trades.iloc[1]["exit_reason"], "BACKTEST_END")
        self.assertEqual(trades.iloc[0]["exit_time"].date(), date(2025, 1, 8))
        self.assertEqual(trades.iloc[1]["entry_time"], trades.iloc[0]["exit_time"])
        self.assertEqual(trades.iloc[1]["entry_time"].time(), time(15, 29))

    def test_carry_single_indicator_fails_when_roll_contract_is_unavailable(self):
        dates = [D, date(2025, 1, 7), date(2025, 1, 8), EXP,
                 date(2025, 1, 10), date(2025, 1, 13)]
        next_expiry = date(2025, 1, 16)

        class CarryStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.expiries = [EXP, next_expiry] if d >= date(2025, 1, 8) else [EXP]
                day.selected_expiry = EXP if d <= EXP else next_expiry
                return day, None

        def bullish_signals(_day, *_args):
            return (np.ones(N_BARS, dtype=np.int8),
                    np.ones(N_BARS, dtype=bool), None, None)

        cfg = SignalStrategyConfig(
            name="missing indicator roll", underlying="NIFTY", indicator=EMAParams(period=5),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            start_date=dates[0], end_date=dates[-1], weekdays=None,
            max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=lambda days, *_args: _signals_for_days(days, bullish_signals)):
            with self.assertRaisesRegex(RuntimeError, "Cannot roll carried.*no usable CE contract"):
                run_signal_backtest(cfg, CarryStore())

    def test_carry_single_option_ema_rolls_each_position_to_next_expiry(self):
        dates = [D, date(2025, 1, 7), date(2025, 1, 8), EXP,
                 date(2025, 1, 10), date(2025, 1, 13)]
        next_expiry = date(2025, 1, 16)

        class CarryStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.expiries = [EXP, next_expiry] if d >= date(2025, 1, 8) else [EXP]
                day.selected_expiry = EXP if d <= EXP else next_expiry
                if d >= date(2025, 1, 8):
                    day.options.update({
                        (next_expiry, strike, option_type): array.copy()
                        for (expiry, strike, option_type), array in list(day.options.items())
                        if expiry == EXP
                    })
                return day, None

        def constant_option_signals(days, *_args):
            signals = {}
            for day in days:
                streams = {}
                for typ in ("CE", "PE"):
                    key = (day.selected_expiry, 23500, typ)
                    streams[typ] = (
                        np.ones(N_BARS, dtype=np.int8),
                        np.ones(N_BARS, dtype=bool),
                        {typ: key},
                        None,
                    )
                signals[day.trade_date] = streams
            return signals

        cfg = SignalStrategyConfig(
            name="carry option EMA expiry roll", underlying="NIFTY",
            indicator=EMAParams(period=5, source=EMASource.OPTION),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            start_date=dates[0], end_date=dates[-1], weekdays=None,
            max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=constant_option_signals):
            result = run_signal_backtest(cfg, CarryStore())

        trades = result.trades.sort_values(["option_type", "entry_time"]).reset_index(drop=True)
        self.assertEqual(len(trades), 4)
        for _, stream in trades.groupby("option_type"):
            self.assertEqual(list(stream["expiry"]), [EXP, next_expiry])
            self.assertEqual(list(stream["exit_reason"]), ["ROLLED", "BACKTEST_END"])
            self.assertEqual(stream.iloc[0]["exit_time"].date(), date(2025, 1, 8))
            self.assertEqual(stream.iloc[1]["entry_time"], stream.iloc[0]["exit_time"])

    def test_carry_single_option_ema_fails_when_roll_contract_is_unavailable(self):
        dates = [D, date(2025, 1, 7), date(2025, 1, 8), EXP,
                 date(2025, 1, 10), date(2025, 1, 13)]
        next_expiry = date(2025, 1, 16)

        class CarryStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.expiries = [EXP, next_expiry] if d >= date(2025, 1, 8) else [EXP]
                day.selected_expiry = EXP if d <= EXP else next_expiry
                return day, None

        def constant_option_signals(days, *_args):
            signals = {}
            for day in days:
                streams = {}
                for typ in ("CE", "PE"):
                    key = (day.selected_expiry, 23500, typ)
                    streams[typ] = (
                        np.ones(N_BARS, dtype=np.int8),
                        np.ones(N_BARS, dtype=bool),
                        {typ: key},
                        None,
                    )
                signals[day.trade_date] = streams
            return signals

        cfg = SignalStrategyConfig(
            name="missing option EMA roll", underlying="NIFTY",
            indicator=EMAParams(period=5, source=EMASource.OPTION),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_SINGLE,
            start_date=dates[0], end_date=dates[-1], weekdays=None,
            max_expiry_gap_days=None, apply_costs=False,
        )
        with patch("optbt.signal_engine.compute_signals_for_days",
                   side_effect=constant_option_signals):
            with self.assertRaisesRegex(RuntimeError, "Cannot roll carried.*no usable CE contract"):
                run_signal_backtest(cfg, CarryStore())

    def test_carry_forward_signal_entries_continue_daily(self):
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8)]

        class CarryStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.linspace(23500.0, 23600.0, N_BARS))
                day.trade_date = d
                day.expiries = [EXP]
                day.selected_expiry = EXP
                return day, None

        cfg = SignalStrategyConfig(
            name="ema carry", underlying="NIFTY", indicator=EMAParams(period=5),
            trade_initial_trend=True, holding_mode=HoldingMode.CARRY_FRESH_ENTRIES,
            start_date=dates[0], end_date=EXP, weekdays=None, apply_costs=False,
            max_expiry_gap_days=None,
        )
        result = run_signal_backtest(cfg, CarryStore())

        self.assertEqual(len(result.trades), len(dates))
        self.assertEqual(set(result.trades["exit_reason"]), {"BACKTEST_END"})
        self.assertEqual(set(result.trades["exit_time"].dt.date), {date(2025, 1, 8)})

    def test_supertrend_entries_only_at_bar_boundaries_and_reverse(self):
        day = make_day(v_shape())
        rows, why = simulate_signal_day(day, st_cfg(), NIFTY)
        self.assertIsNone(why)
        self.assertGreaterEqual(len(rows), 1)
        first = rows[0]
        self.assertEqual((first["side"], first["option_type"]), ("BUY", "PE"))   # first event is the down flip
        for r in rows:
            j = bar_index(r["entry_time"].time())
            self.assertEqual(j % 5, 0)                 # decisions land on a closed 5-min bar boundary
        self.assertGreaterEqual(bar_index(first["entry_time"].time()), 150)
        self.assertEqual(rows[-1]["exit_time"].time(), time(15, 15))

    def test_short_premium_expression_flips_sides(self):
        day = make_day(v_shape())
        rows, _ = simulate_signal_day(day, st_cfg(expression=OptionExpression.SHORT_PREMIUM), NIFTY)
        self.assertEqual((rows[0]["side"], rows[0]["option_type"]), ("SELL", "CE"))   # bearish -> sell CE

    def test_signal_leg_action_uses_directional_call_or_put(self):
        class SingleDayStore:
            dates = [D]

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day(np.full(N_BARS, 23500.0))
                day.trade_date = d
                day.selected_expiry = EXP
                return day, None

        def trend_flip(_day, *_args):
            sig = np.ones(N_BARS, dtype=np.int8)
            sig[bar_index(time(10, 0)):] = -1
            return sig, np.ones(N_BARS, dtype=bool), None, None

        expected = {
            Side.BUY: [("BUY", "CE"), ("BUY", "PE")],
            Side.SELL: [("SELL", "PE"), ("SELL", "CE")],
        }
        indicators = {
            "EMA": EMAParams(period=5),
            "MACD": MACDParams(3, 8, 4, "5m"),
        }
        for indicator_name, indicator in indicators.items():
            for action, expected_legs in expected.items():
                with self.subTest(indicator=indicator_name, action=action):
                    cfg = SignalStrategyConfig(
                        name="directional option", underlying="NIFTY", indicator=indicator,
                        signal_legs=(LegConfig(action, OptionType.PE, StrikeSelection.atm(), 1),),
                        trade_initial_trend=True, start_date=D, end_date=D,
                        weekdays=None, apply_costs=False,
                    )
                    with patch("optbt.signal_engine.compute_signals_for_days",
                               side_effect=lambda days, *_args: _signals_for_days(days, trend_flip)):
                        result = run_signal_backtest(cfg, SingleDayStore())

                    trades = result.trades.sort_values("entry_time")
                    self.assertEqual(
                        list(zip(trades["side"], trades["option_type"])),
                        expected_legs,
                    )

    def test_trade_initial_trend_enters_at_start(self):
        day = make_day(v_shape())
        rows, _ = simulate_signal_day(day, st_cfg(trade_initial_trend=True), NIFTY)
        self.assertEqual((rows[0]["side"], rows[0]["option_type"]), ("BUY", "CE"))
        self.assertEqual(rows[0]["exit_reason"], "SIGNAL_EXIT")           # reversed by the down flip
        self.assertEqual(rows[1]["option_type"], "PE")
        self.assertEqual(rows[0]["exit_time"], rows[1]["entry_time"])

    def test_ignore_mode_holds_until_eod(self):
        day = make_day(v_shape())
        cfg = st_cfg(trade_initial_trend=True, on_opposite=OppositeSignal.IGNORE)
        rows, _ = simulate_signal_day(day, cfg, NIFTY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "EOD_SQUARE_OFF")

    def test_exit_mode_closes_without_reversing(self):
        day = make_day(v_shape())
        cfg = st_cfg(trade_initial_trend=True, on_opposite=OppositeSignal.EXIT)
        rows, _ = simulate_signal_day(day, cfg, NIFTY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "SIGNAL_EXIT")

    def test_vwap_spot_cross_buys_ce_then_pe(self):
        close = np.concatenate([np.full(60, 23500.0), np.linspace(23500, 23800, 120),
                                np.linspace(23800, 23100, 195)])
        day = make_day(close)
        cfg = SignalStrategyConfig(name="v", underlying="NIFTY", indicator=VwapParams(VwapSource.SPOT),
                                   start_time=time(9, 30), apply_costs=False)
        rows, why = simulate_signal_day(day, cfg, NIFTY)
        self.assertIsNone(why)
        self.assertEqual([r["option_type"] for r in rows[:2]], ["CE", "PE"])
        self.assertGreaterEqual(bar_index(rows[0]["entry_time"].time()), 60)

    def test_vwap_premium_trades_the_monitored_contract(self):
        n = np.arange(N_BARS)
        ce = lambda s, K: 100 + np.maximum(n - 40, 0) * 0.5            # CE keeps rising above its VWAP
        pe = lambda s, K: 100 - np.minimum(n, 150) * 0.3               # PE keeps falling
        day = make_day(np.full(N_BARS, 23500.0), ce, pe, vol=1000.0)
        cfg = SignalStrategyConfig(name="vp", underlying="NIFTY",
                                   indicator=VwapParams(VwapSource.PREMIUM, confirm_bars=2),
                                   trade_initial_trend=True, apply_costs=False)
        rows, why = simulate_signal_day(day, cfg, NIFTY)
        self.assertIsNone(why)
        self.assertEqual((rows[0]["side"], rows[0]["option_type"]), ("BUY", "CE"))

    def test_stop_loss_applies_to_signal_trade(self):
        n = np.arange(N_BARS)
        ce = lambda s, K: np.where(n < 40, 100.0, 70.0)                # long CE gaps down 30% at minute 40
        day = make_day(v_shape(), ce_fn=ce)
        cfg = st_cfg(trade_initial_trend=True, stop_loss=RiskRule.percent(20))
        rows, _ = simulate_signal_day(day, cfg, NIFTY)
        self.assertEqual((rows[0]["option_type"], rows[0]["exit_reason"]), ("CE", "SL"))
        self.assertAlmostEqual(rows[0]["exit_price"], 70.0)            # gap through the 80 stop -> open

    def test_daily_loss_limit_stops_day(self):
        n = np.arange(N_BARS)
        pe = lambda s, K: 100 + np.maximum(n - 100, 0) * 1.0            # long PE bleeds? (rises -> profit)
        ce = lambda s, K: 100 - np.maximum(n - 20, 0) * 0.2             # CE decays: long CE loses
        day = make_day(v_shape(), ce_fn=ce, pe_fn=pe)
        cfg = st_cfg(trade_initial_trend=True, daily_max_loss=500.0)
        rows, _ = simulate_signal_day(day, cfg, NIFTY)
        self.assertEqual(rows[0]["exit_reason"], "PORTFOLIO_SL")
        self.assertEqual(len(rows), 1)                                  # day stopped, no re-entry

    def test_runner_parallel_matches_serial_and_metrics(self):
        store = DataStore(*make_synthetic_market("2025-01-01", "2025-01-31", seed=11))
        cfg = SignalStrategyConfig(name="st", underlying="NIFTY", indicator=SupertrendParams(10, 3.0, 5),
                                   stop_loss=RiskRule.percent(30), target=RiskRule.percent(60))
        a = run_signal_backtest(cfg, store, workers=1)
        b = run_signal_backtest(cfg, store, workers=2)
        self.assertAlmostEqual(a.metrics["total_net_pnl"], b.metrics["total_net_pnl"], places=2)
        self.assertIn("trade_win_rate_pct", a.metrics)


if __name__ == "__main__":
    unittest.main()
