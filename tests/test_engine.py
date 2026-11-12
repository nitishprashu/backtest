"""Deterministic engine tests on hand-built days. Run:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
import unittest
from datetime import date, time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optbt import (ATMReference, DataStore, DayData, ExpirySelect, HoldingMode, LegConfig, OptionType, PortfolioTrail,
                   ReEntryMode, ReEntryRule, RiskRule, RolloverStrikeMode, Side, StrategyConfig, StrikeSelection,
                   TrailRule, NIFTY, run_backtest, simulate_day)
from optbt.data import C, N_BARS, O, bar_index
from optbt.carry import Timeline, _horizon, _next_roll_key, _timeline
from optbt.engine import resolve_strike
from optbt.runner import select_dates
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from synth import make_synthetic_market

D, EXP, K = date(2025, 1, 6), date(2025, 1, 9), 23500
EXP_FAR = date(2025, 1, 16)
QTY = NIFTY.lot_size_for(EXP)
E, X = bar_index(time(9, 20)), bar_index(time(15, 15))


def flat(price):
    return np.full((5, N_BARS), float(price))


def make_day(ce=None, pe=None):
    spot = np.full((4, N_BARS), 23500.0)
    return DayData(D, spot, {(EXP, K, "CE"): ce if ce is not None else flat(100),
                             (EXP, K, "PE"): pe if pe is not None else flat(100)}, [EXP])


def cfg(legs, **kw):
    return StrategyConfig(name="t", underlying="NIFTY", entry_time=time(9, 20), exit_time=time(15, 15),
                          legs=tuple(legs), apply_costs=False, slippage_pct=0.0, **kw)


def short_ce(**kw):
    return LegConfig(Side.SELL, OptionType.CE, StrikeSelection.atm(), **kw)

def short_pe(**kw):
    return LegConfig(Side.SELL, OptionType.PE, StrikeSelection.atm(), **kw)


def bar(arr, i, o, h, l, c):
    arr[0, i], arr[1, i], arr[2, i], arr[3, i] = o, h, l, c


class EngineTests(unittest.TestCase):
    def run_day(self, day, c):
        rows, why = simulate_day(day, c, NIFTY)
        self.assertIsNone(why)
        return rows

    def test_atm_reference_can_use_spot_or_synthetic_future(self):
        day = make_day()
        day.options[(EXP, K, "CE")] = flat(180)
        day.options[(EXP, K, "PE")] = flat(100)
        day.options[(EXP, K + 100, "CE")] = flat(80)

        spot_key = resolve_strike(StrikeSelection.atm(ATMReference.SPOT), OptionType.CE,
                                  day, EXP, E, NIFTY, O)
        future_key = resolve_strike(StrikeSelection.atm(ATMReference.SYNTHETIC_FUTURE), OptionType.CE,
                                    day, EXP, E, NIFTY, O)

        self.assertEqual(spot_key, (EXP, K, "CE"))
        self.assertEqual(future_key, (EXP, K + 100, "CE"))

    def test_synthetic_future_atm_requires_both_atm_option_prices(self):
        day = make_day()
        day.options.pop((EXP, K, "PE"))
        selected = resolve_strike(StrikeSelection.atm(ATMReference.SYNTHETIC_FUTURE),
                                  OptionType.CE, day, EXP, E, NIFTY, O)
        self.assertIsNone(selected)

    def test_expiry_day_filter_selects_listed_expiry_sessions_only(self):
        store = self._carry_store()
        config = cfg([short_ce()], start_date=D, end_date=EXP, weekdays=None,
                     expiry_day_only=True)

        self.assertEqual(select_dates(store, config), [EXP])

    def test_expiry_day_filter_intersects_selected_weekdays(self):
        store = self._carry_store()
        config = cfg([short_ce()], start_date=D, end_date=EXP, weekdays=(0, 3),
                     expiry_day_only=True)

        self.assertEqual(select_dates(store, config), [EXP])

    def test_eod_square_off(self):
        r = self.run_day(make_day(), cfg([short_ce()]))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["exit_reason"], "EOD_SQUARE_OFF")
        self.assertEqual(r[0]["exit_time"].time(), time(15, 15))
        self.assertAlmostEqual(r[0]["net_pnl"], 0.0)

    def test_calendar_legs_resolve_to_distinct_expiries(self):
        day = make_day()
        day.expiries.append(EXP_FAR)
        day.options[(EXP_FAR, K, "CE")] = flat(120)
        legs = [short_ce(), LegConfig(Side.BUY, OptionType.CE, expiry_offset=1)]

        rows = self.run_day(day, cfg(legs))

        self.assertEqual({row["expiry"] for row in rows}, {EXP, EXP_FAR})
        self.assertEqual([row["side"] for row in rows], ["SELL", "BUY"])

    def test_carried_calendar_leg_keeps_its_own_expiry(self):
        store = self._carry_store()
        far_calls = store.options[store.options["option_type"] == "CE"].copy()
        far_calls["expiry"] = pd.Timestamp(EXP_FAR)
        store = DataStore(store.spot, pd.concat([store.options, far_calls], ignore_index=True))
        config = cfg([short_ce(), LegConfig(Side.BUY, OptionType.CE, expiry_offset=1)],
                     holding_mode=HoldingMode.CARRY_SINGLE, start_date=D, end_date=EXP, weekdays=None)

        result = run_backtest(config, store)

        self.assertEqual(set(result.trades["expiry"]), {EXP, EXP_FAR})

    def _carry_store(self, trigger=False):
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), EXP]
        stamps, spot_rows, option_rows = [], [], []
        for d in dates:
            for minute in range(N_BARS):
                ts = pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15 + minute)
                stamps.append(ts)
                spot_rows.append((ts, 23500.0, 23500.0, 23500.0, 23500.0))
                hi = 130.0 if trigger and d == date(2025, 1, 7) and minute == 40 else 100.0
                option_rows.append((ts, pd.Timestamp(EXP), K, "CE", 100.0, hi, 100.0, 100.0, 10.0))
                option_rows.append((ts, pd.Timestamp(EXP), K, "PE", 100.0, 100.0, 100.0, 100.0, 10.0))
        spot = pd.DataFrame(spot_rows, columns=["timestamp", "open", "high", "low", "close"])
        options = pd.DataFrame(option_rows, columns=[
            "timestamp", "expiry", "strike", "option_type", "open", "high", "low", "close", "volume"])
        return DataStore(spot, options)

    def test_carry_single_holds_contract_and_skips_entries_until_stop(self):
        store = self._carry_store(trigger=True)
        c = cfg([short_ce(stop_loss=RiskRule.percent(25),
                          reentry=ReEntryRule(ReEntryMode.RE_EXECUTE, 2))],
                holding_mode=HoldingMode.CARRY_SINGLE,
                start_date=date(2025, 1, 6), end_date=EXP,
                max_expiry_gap_days=None, weekdays=None)
        result = run_backtest(c, store)
        trades = result.trades.sort_values("entry_time")
        self.assertEqual(len(trades), 2, trades.to_string())
        self.assertEqual(trades.iloc[0]["exit_reason"], "SL")
        self.assertEqual(trades.iloc[0]["exit_time"].date(), date(2025, 1, 7))
        self.assertEqual(trades.iloc[1]["entry_time"].date(), date(2025, 1, 8))
        self.assertEqual(trades.iloc[1]["exit_reason"], "EXPIRY")

    def test_carry_fresh_entries_keeps_each_entry_until_expiry(self):
        store = self._carry_store()
        c = cfg([short_ce()], holding_mode=HoldingMode.CARRY_FRESH_ENTRIES,
                start_date=date(2025, 1, 6), end_date=EXP, max_expiry_gap_days=None, weekdays=None)
        result = run_backtest(c, store)
        self.assertEqual(len(result.trades), 3)
        self.assertEqual(set(result.trades["exit_reason"]), {"EXPIRY"})
        self.assertEqual(set(result.trades["exit_time"].dt.date), {date(2025, 1, 8)})

    def test_carry_single_rolls_same_strike_to_next_weekly_expiry(self):
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8),
                 EXP, date(2025, 1, 10), date(2025, 1, 13)]
        next_expiry = date(2025, 1, 16)
        stamps, spot_rows, option_rows = [], [], []
        for d in dates:
            for minute in range(N_BARS):
                ts = pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15 + minute)
                stamps.append(ts)
                spot_rows.append((ts, 23500.0, 23500.0, 23500.0, 23500.0))
                expiry_prices = [(EXP, 100.0)]
                if d >= dates[2]:
                    expiry_prices.append((next_expiry, 140.0))
                for expiry, price in expiry_prices:
                    option_rows.append((ts, pd.Timestamp(expiry), K, "CE",
                                        price, price, price, price, 10.0))
        spot = pd.DataFrame(spot_rows, columns=["timestamp", "open", "high", "low", "close"])
        options = pd.DataFrame(option_rows, columns=[
            "timestamp", "expiry", "strike", "option_type", "open", "high", "low", "close", "volume"])
        c = cfg([short_ce()], holding_mode=HoldingMode.CARRY_SINGLE,
                expiry=ExpirySelect.CURRENT_WEEKLY, start_date=dates[0], end_date=dates[-1],
                max_expiry_gap_days=None, weekdays=None)

        result = run_backtest(c, DataStore(spot, options))
        trades = result.trades.sort_values("entry_time").reset_index(drop=True)

        self.assertEqual(len(trades), 2, trades.to_string())
        self.assertEqual(list(trades["expiry"]), [EXP, next_expiry])
        self.assertEqual(list(trades["strike"]), [K, K])
        self.assertEqual(trades.iloc[0]["exit_reason"], "ROLLED")
        self.assertEqual(trades.iloc[1]["exit_reason"], "BACKTEST_END")
        self.assertEqual(trades.iloc[0]["exit_time"].date(), dates[2])
        self.assertEqual(trades.iloc[1]["entry_time"], trades.iloc[0]["exit_time"])

    def test_carry_rollover_can_reapply_the_configured_strike_rule(self):
        dates = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8),
                 EXP, date(2025, 1, 10), date(2025, 1, 13)]
        next_expiry = date(2025, 1, 16)
        stamps, spot_rows, option_rows = [], [], []
        for d in dates:
            for minute in range(N_BARS):
                ts = pd.Timestamp(d) + pd.Timedelta(hours=9, minutes=15 + minute)
                stamps.append(ts)
                spot = 23600.0 if d >= dates[2] else 23500.0
                spot_rows.append((ts, spot, spot, spot, spot))
                option_rows.append((ts, pd.Timestamp(EXP), K, "CE",
                                    100.0, 100.0, 100.0, 100.0, 10.0))
                if d >= dates[2]:
                    for strike, price in ((K, 140.0), (K + 100, 120.0)):
                        option_rows.append((ts, pd.Timestamp(next_expiry), strike, "CE",
                                            price, price, price, price, 10.0))
        spot = pd.DataFrame(spot_rows, columns=["timestamp", "open", "high", "low", "close"])
        options = pd.DataFrame(option_rows, columns=[
            "timestamp", "expiry", "strike", "option_type", "open", "high", "low", "close", "volume"])
        store = DataStore(spot, options)
        config = cfg(
            [short_ce()], holding_mode=HoldingMode.CARRY_SINGLE,
            expiry=ExpirySelect.CURRENT_WEEKLY, start_date=dates[0], end_date=dates[-1],
            max_expiry_gap_days=None, weekdays=None,
            rollover_strike_mode=RolloverStrikeMode.RESELECT,
        )

        result = run_backtest(config, store)
        trades = result.trades.sort_values("entry_time").reset_index(drop=True)

        self.assertEqual(list(trades["expiry"]), [EXP, next_expiry])
        self.assertEqual(list(trades["strike"]), [K, K + 100])
        self.assertEqual(trades.iloc[0]["exit_reason"], "ROLLED")
        self.assertEqual(trades.iloc[1]["entry_time"], trades.iloc[0]["exit_time"])

    def test_carry_contract_gap_has_distinct_reason_and_download_range(self):
        dates = [D, date(2025, 1, 7), date(2025, 1, 8)]

        class MissingHeldContractStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day()
                day.trade_date = d
                day.options.pop((EXP, K, "CE"), None)
                if d != dates[1]:
                    day.options[(EXP, K, "CE")] = flat(100)
                day.expiries = [EXP]
                day.selected_expiry = EXP
                return day, None

        store = MissingHeldContractStore()
        c = cfg([short_ce()], holding_mode=HoldingMode.CARRY_SINGLE,
                start_date=dates[0], end_date=dates[-1], max_expiry_gap_days=None, weekdays=None)
        timeline = _timeline(store, c, need_prev=False)
        held = (EXP, K, "CE")
        boundary, reason = _horizon(timeline, 0, EXP, held)

        self.assertEqual(boundary, N_BARS - 1)
        self.assertEqual(reason.value, "DATA_GAP")
        timeline.ensure_contract(held, 0)
        self.assertEqual(timeline.data_gap_report(), [{
            "expiry": EXP.isoformat(), "strike": K, "option_type": "CE",
            "from": dates[1].isoformat(), "to": dates[1].isoformat(),
            "missing_dates": [dates[1].isoformat()],
        }])
        result = run_backtest(c, store)
        first_trade = result.trades.sort_values("entry_time").iloc[0]
        self.assertEqual(first_trade["exit_reason"], "DATA_GAP")
        self.assertEqual(result.metrics["data_gaps"][0]["missing_dates"], [dates[1].isoformat()])

    def test_timeline_fills_an_out_of_window_contract_before_scanning_carry_horizon(self):
        dates = [D, date(2025, 1, 7), date(2025, 1, 8)]

        class RecoverableContractStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day()
                day.trade_date = d
                if d == dates[1]:
                    day.options.pop((EXP, K, "CE"))
                day.expiries = [EXP]
                day.selected_expiry = EXP
                return day, None

            def load_contract_day(self, d, contract):
                return flat(100) if d == dates[1] and contract == (EXP, K, "CE") else None

        c = cfg([short_ce()], holding_mode=HoldingMode.CARRY_SINGLE,
                start_date=dates[0], end_date=dates[-1], max_expiry_gap_days=None, weekdays=None)
        timeline = _timeline(RecoverableContractStore(), c, need_prev=False)
        held = (EXP, K, "CE")
        timeline.ensure_contract(held, 0)
        boundary, reason = _horizon(timeline, 0, EXP, held)

        self.assertIn(1, timeline.key_presence[held])
        self.assertEqual(boundary, 3 * N_BARS - 1)
        self.assertEqual(reason.value, "BACKTEST_END")
        self.assertEqual(timeline.data_gap_report(), [])

    def test_carry_timeline_keeps_contract_arrays_sparse(self):
        dates = [D, date(2025, 1, 7), date(2025, 1, 8)]

        class SparseStore:
            def __init__(self):
                self.dates = dates

            def build_day(self, d, expiry_mode, max_gap_days, need_prev):
                day = make_day()
                day.trade_date = d
                if d == dates[1]:
                    day.options.pop((EXP, K, "CE"))
                day.expiries = [EXP]
                day.selected_expiry = EXP
                return day, None

        timeline = _timeline(
            SparseStore(),
            cfg([short_ce()], holding_mode=HoldingMode.CARRY_SINGLE,
                start_date=dates[0], end_date=dates[-1], max_expiry_gap_days=None),
            need_prev=False,
        )
        contract = timeline.market.options[(EXP, K, "CE")]
        self.assertEqual(contract.shape, (5, len(dates) * N_BARS))
        self.assertEqual(contract[C, 0], 100)
        self.assertTrue(np.isnan(contract[C, N_BARS]))
        self.assertEqual(contract[C, 2 * N_BARS], 100)
        boundary = contract[C, N_BARS - 1:N_BARS + 1]
        self.assertEqual(boundary[0], 100)
        self.assertTrue(np.isnan(boundary[1]))

    def test_holiday_week_expiry_rolls_on_last_trading_day_before_expiry(self):
        expiry = date(2022, 1, 27)
        next_expiry = date(2022, 2, 3)
        last_trading_day_before_expiry = date(2022, 1, 25)
        dates = [
            date(2022, 1, 20), date(2022, 1, 21), date(2022, 1, 24),
            last_trading_day_before_expiry, expiry, date(2022, 1, 28),
        ]
        stamps, spot_rows, option_rows = [], [], []
        for d in dates:
            for minute in range(N_BARS):
                ts = pd.Timestamp(d) + pd.Timedelta(minutes=555 + minute)
                stamps.append(ts)
                spot_rows.append((ts, 17800.0, 17800.0, 17800.0, 17800.0))
                expiries = [expiry] if d <= expiry else []
                if d >= last_trading_day_before_expiry:
                    expiries.append(next_expiry)
                for contract_expiry in expiries:
                    option_rows.append((ts, pd.Timestamp(contract_expiry), 17850, "CE",
                                        100.0, 100.0, 100.0, 100.0, 10.0))
        spot = pd.DataFrame(spot_rows, columns=["timestamp", "open", "high", "low", "close"])
        options = pd.DataFrame(option_rows, columns=[
            "timestamp", "expiry", "strike", "option_type", "open", "high", "low", "close", "volume"])
        strategy = cfg(
            [LegConfig(Side.SELL, OptionType.CE, StrikeSelection.fixed(17850))],
            expiry=ExpirySelect.CURRENT_WEEKLY,
            holding_mode=HoldingMode.CARRY_SINGLE, start_date=dates[0], end_date=dates[-1],
            max_expiry_gap_days=None, weekdays=None,
        )

        result = run_backtest(strategy, DataStore(spot, options))
        trades = result.trades.sort_values("entry_time").reset_index(drop=True)

        self.assertEqual(len(trades), 2)
        self.assertEqual(list(trades["expiry"]), [expiry, next_expiry])
        self.assertEqual(trades.iloc[0]["exit_time"].date(), last_trading_day_before_expiry)
        self.assertEqual(trades.iloc[0]["exit_reason"], "ROLLED")
        self.assertEqual(trades.iloc[1]["entry_time"], trades.iloc[0]["exit_time"])
        self.assertEqual(trades.iloc[1]["side"], trades.iloc[0]["side"])
        self.assertEqual(trades.iloc[1]["strike"], trades.iloc[0]["strike"])

    def test_monthly_roll_selects_the_next_monthly_not_intervening_weekly(self):
        expiry_day = date(2025, 1, 30)
        weekly_after = date(2025, 2, 6)
        monthly_after = date(2025, 2, 27)
        options = {
            (expiry, K, "CE"): flat(100.0)
            for expiry in (expiry_day, weekly_after, monthly_after)
        }
        day = DayData(expiry_day, np.full((4, N_BARS), 23500.0), options,
                      [expiry_day, weekly_after, monthly_after])
        tl = Timeline([day], [expiry_day], day, {}, {}, [])

        next_key = _next_roll_key(
            tl, 0, expiry_day, (expiry_day, K, "CE"), ExpirySelect.MONTHLY)

        self.assertEqual(next_key, (monthly_after, K, "CE"))

    def test_short_sl_fills_at_level(self):
        ce = flat(100); bar(ce, 45, 100, 130, 100, 128)
        r = self.run_day(make_day(ce=ce), cfg([short_ce(stop_loss=RiskRule.percent(25))]))[0]
        self.assertEqual(r["exit_reason"], "SL")
        self.assertAlmostEqual(r["exit_price"], 125.0)
        self.assertAlmostEqual(r["gross_pnl"], -25 * QTY)

    def test_gap_through_sl_fills_at_open(self):
        ce = flat(100); bar(ce, 45, 140, 142, 139, 141)
        r = self.run_day(make_day(ce=ce), cfg([short_ce(stop_loss=RiskRule.percent(25))]))[0]
        self.assertAlmostEqual(r["exit_price"], 140.0)

    def test_sl_and_tp_same_bar_sl_wins(self):
        ce = flat(100); bar(ce, 45, 100, 130, 70, 100)
        r = self.run_day(make_day(ce=ce), cfg([short_ce(stop_loss=RiskRule.percent(25),
                                                        target=RiskRule.percent(20))]))[0]
        self.assertEqual(r["exit_reason"], "SL")

    def test_target_hit(self):
        ce = flat(100); bar(ce, 60, 100, 100, 75, 80)
        r = self.run_day(make_day(ce=ce), cfg([short_ce(target=RiskRule.points(20))]))[0]
        self.assertEqual(r["exit_reason"], "TP")
        self.assertAlmostEqual(r["exit_price"], 80.0)

    def test_trailing_sl(self):
        ce = flat(100)
        bar(ce, 40, 100, 100, 80, 82)        # favourable 20 -> two 10% steps -> SL 125 - 20 = 105
        bar(ce, 41, 90, 106, 90, 100)        # high 106 >= 105
        r = self.run_day(make_day(ce=ce), cfg([short_ce(stop_loss=RiskRule.percent(25),
                                                        trail_sl=TrailRule(10, 10))]))[0]
        self.assertEqual(r["exit_reason"], "TRAILING_SL")
        self.assertAlmostEqual(r["exit_price"], 105.0)

    def test_slippage_is_adverse(self):
        c = StrategyConfig(name="t", underlying="NIFTY", entry_time=time(9, 20), exit_time=time(15, 15),
                           legs=(short_ce(),), apply_costs=False, slippage_pct=1.0)
        r = self.run_day(make_day(), c)[0]
        self.assertAlmostEqual(r["entry_price"], 99.0)    # sold lower
        self.assertAlmostEqual(r["exit_price"], 101.0)    # bought back higher

    def test_portfolio_sl_closes_all_next_open(self):
        ce = flat(100)
        ce[:, 50:] = 100
        bar(ce, 50, 100, 121, 100, 120)       # MTM = -20*65 = -1300 <= -1000 at bar 50
        bar(ce, 51, 121, 121, 121, 121)
        pe_leg = LegConfig(Side.SELL, OptionType.PE, StrikeSelection.atm())
        r = self.run_day(make_day(ce=ce), cfg([short_ce(), pe_leg], portfolio_sl=1000))
        self.assertEqual({x["exit_reason"] for x in r}, {"PORTFOLIO_SL"})
        ce_row = next(x for x in r if x["option_type"] == "CE")
        self.assertAlmostEqual(ce_row["exit_price"], 121.0)
        self.assertEqual(ce_row["exit_time"].time(), time(10, 6))   # bar 51 = 10:06

    def test_reexecute_after_sl(self):
        ce = flat(100); bar(ce, 45, 100, 130, 100, 128); bar(ce, 46, 100, 100, 100, 100)
        leg = short_ce(stop_loss=RiskRule.percent(25), reentry=ReEntryRule(ReEntryMode.RE_EXECUTE, 1))
        r = self.run_day(make_day(ce=ce), cfg([leg]))
        self.assertEqual([x["segment"] for x in r], [1, 2])
        self.assertEqual(r[0]["exit_reason"], "SL")
        self.assertEqual(r[1]["exit_reason"], "EOD_SQUARE_OFF")

    def test_unavailable_strike_skips_day(self):
        leg = LegConfig(Side.SELL, OptionType.CE, StrikeSelection.steps(3))
        rows, why = simulate_day(make_day(), cfg([leg]), NIFTY)
        self.assertEqual(rows, [])
        self.assertIn("unavailable", why)

    def test_leg_limit(self):
        with self.assertRaises(ValueError):
            cfg([short_ce()] * 9)


class PipelineTests(unittest.TestCase):
    def test_parallel_matches_serial(self):
        store = DataStore(*make_synthetic_market("2025-01-01", "2025-01-31", seed=3))
        c = cfg([short_ce(stop_loss=RiskRule.percent(25))])
        a = run_backtest(c, store, workers=1)
        b = run_backtest(c, store, workers=2)
        self.assertAlmostEqual(a.metrics["total_net_pnl"], b.metrics["total_net_pnl"], places=2)

    def test_breeze_folder_loader(self):
        with tempfile.TemporaryDirectory() as t:
            ts = pd.date_range("2025-01-06 09:15", periods=N_BARS, freq="min")
            pd.DataFrame({"datetime": ts, "open": 23500.0, "high": 23510.0, "low": 23490.0,
                          "close": 23500.0}).to_csv(os.path.join(t, "NIFTY_spot_1min.csv"), index=False)
            os.makedirs(os.path.join(t, "2025-01-09"))
            for right in ("call", "put"):
                pd.DataFrame({"datetime": ts, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                              "volume": 10}).to_csv(
                    os.path.join(t, "2025-01-09", f"NIFTY_25_01_09_23500_{right}.csv"), index=False)
            store = DataStore.from_breeze_output(t, "NIFTY")
            res = run_backtest(cfg([short_ce()]), store)
            self.assertEqual(len(res.trades), 1)
            self.assertEqual(res.trades.iloc[0]["option_type"], "CE")


if __name__ == "__main__":
    unittest.main()


class ExtraEngineTests(unittest.TestCase):
    def test_fixed_strike_leg(self):
        day = make_day()
        day.options[(EXP, K + 100, "CE")] = flat(60)
        leg = LegConfig(Side.SELL, OptionType.CE, StrikeSelection.fixed(K + 100))
        r = simulate_day(day, cfg([leg]), NIFTY)[0][0]
        self.assertEqual(r["strike"], K + 100)
        self.assertAlmostEqual(r["entry_price"], 60.0)
        rows, why = simulate_day(day, cfg([LegConfig(Side.SELL, OptionType.CE, StrikeSelection.fixed(K + 300))]), NIFTY)
        self.assertEqual(rows, [])
        self.assertIn("unavailable", why)

    def test_portfolio_sl_as_percent_of_entry_premium(self):
        ce = flat(100)
        bar(ce, 50, 100, 111, 100, 110)                # short CE loses 10 pts; notional = 100 * qty
        r = simulate_day(make_day(ce=ce), cfg([short_ce()], portfolio_sl_pct=10), NIFTY)[0][0]
        self.assertEqual(r["exit_reason"], "PORTFOLIO_SL") 

    def test_strategy_sl_in_combined_premium_points(self):
        ce, pe = flat(100), flat(100)
        bar(ce, 50, 100, 107, 100, 106)
        bar(pe, 50, 100, 107, 100, 106)
        bar(ce, 51, 106, 106, 106, 106)
        bar(pe, 51, 106, 106, 106, 106)

        rows, why = simulate_day(make_day(ce=ce, pe=pe),
                                 cfg([short_ce(), short_pe()], portfolio_sl_points=10), NIFTY)

        self.assertIsNone(why)
        self.assertEqual({r["exit_reason"] for r in rows}, {"PORTFOLIO_SL"})
        self.assertEqual({r["exit_time"].time() for r in rows}, {time(10, 6)})

    def test_day_mae_mfe(self):
        ce = flat(100)
        bar(ce, 30, 100, 100, 90, 90)                  # +10 pts = +650 best
        bar(ce, 60, 90, 120, 90, 120)                  # -20 pts = -1300 worst (close basis)
        for i in range(31, 60):
            bar(ce, i, 90, 90, 90, 90)
        for i in range(61, N_BARS):
            bar(ce, i, 120, 120, 120, 120)
        r = simulate_day(make_day(ce=ce), cfg([short_ce()]), NIFTY)[0][0]
        self.assertAlmostEqual(r["day_mfe"], 10 * QTY)
        self.assertAlmostEqual(r["day_mae"], -20 * QTY)

    def test_intraday_path_matches_ledger(self):
        import pandas as pd
        from optbt import intraday_path
        ce = flat(100); bar(ce, 45, 100, 130, 100, 128)
        day = make_day(ce=ce)
        rows, _ = simulate_day(day, cfg([short_ce(stop_loss=RiskRule.percent(25))]), NIFTY)
        path = intraday_path(day, pd.DataFrame(rows))
        self.assertAlmostEqual(path["mtm"].iloc[-1], rows[0]["gross_pnl"])
        self.assertAlmostEqual(path["mtm"].iloc[44], 0.0)

    def test_intraday_path_marks_a_position_carried_across_sessions(self):
        import pandas as pd
        from datetime import timedelta
        from optbt import intraday_path
        ce = flat(100)
        day = make_day(ce=ce)
        rows, _ = simulate_day(day, cfg([short_ce()]), NIFTY)
        trade = dict(rows[0])
        trade["entry_time"] = pd.Timestamp.combine(day.trade_date - timedelta(days=1), time(14, 0))
        trade["exit_time"] = pd.Timestamp.combine(day.trade_date + timedelta(days=1), time(10, 0))
        trade["gross_pnl"] = 999999.0
        path = intraday_path(day, pd.DataFrame([trade]))
        contract = (trade["expiry"], trade["strike"], trade["option_type"])
        expected = -(day.options[contract][C, -1] - trade["entry_price"]) * trade["qty"]
        self.assertAlmostEqual(path["mtm"].iloc[-1], expected)



class SlippageAndFilterTests(unittest.TestCase):
    def _cfg(self, model, legs):
        return StrategyConfig(name="t", underlying="NIFTY", entry_time=time(9, 20), exit_time=time(15, 15),
                              legs=tuple(legs), apply_costs=False, slippage_model=model)

    def test_tick_floor_and_stop_multiplier(self):
        from optbt import SlippageModel
        m = SlippageModel(pct=0.1, min_ticks=2, stop_mult=3)
        self.assertAlmostEqual(m.fill(100.0, False, 1e6), 99.9)                  # floor 0.10 > 0.1% of 100
        self.assertAlmostEqual(m.fill(125.0, True, 1e6, stop=True), 125 + 0.125 * 3)
        ce = flat(100); bar(ce, 45, 100, 130, 100, 128)
        r = simulate_day(make_day(ce=ce), self._cfg(m, [short_ce(stop_loss=RiskRule.percent(25))]), NIFTY)[0][0]
        self.assertAlmostEqual(r["entry_price"], 99.9)
        self.assertAlmostEqual(r["exit_price"], 125.375)                         # SL exit paid with the stop multiplier

    def test_thin_bar_multiplier(self):
        from optbt import SlippageModel
        m = SlippageModel(min_ticks=2, thin_volume=1000, thin_mult=4)             # flat() volume row is 100 -> thin
        r = simulate_day(make_day(), self._cfg(m, [short_ce()]), NIFTY)[0][0]
        self.assertAlmostEqual(r["entry_price"], 100 - 0.10 * 4)

    def test_model_validation(self):
        from optbt import SlippageModel
        with self.assertRaises(ValueError):
            SlippageModel(stop_mult=0.5)

    def test_dte_filter_skips_with_filter_reason(self):
        c = StrategyConfig(name="t", underlying="NIFTY", entry_time=time(9, 20), exit_time=time(15, 15),
                           legs=(short_ce(),), apply_costs=False, dte_min=5)
        rows, why = simulate_day(make_day(), c, NIFTY)                            # trade date is 3 days before expiry
        self.assertEqual(rows, [])
        self.assertTrue(why.startswith("filter:"))
        c2 = StrategyConfig(name="t", underlying="NIFTY", entry_time=time(9, 20), exit_time=time(15, 15),
                            legs=(short_ce(),), apply_costs=False, dte_min=2, dte_max=3)
        self.assertEqual(len(simulate_day(make_day(), c2, NIFTY)[0]), 1)
