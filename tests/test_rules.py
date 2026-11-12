"""Lot sizes, charges, expiry guard, analytics calendar, and the SQLite store (hand-built fixtures)."""
import os
import sys
import unittest
from datetime import date, time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optbt import (ATMReference, NIFTY, ExpirySelect, HoldingMode, LegConfig, OptionExpression, OptionType,
                   RiskRule, RolloverStrikeMode, Side, StrategyConfig, StrikeSelection, choose_expiry,
                   run_backtest)
from optbt.analytics import build_daily, compute_metrics
from optbt.costs import leg_costs
from lab import strategy as lab_strategy
from optbt import EMAParams, EMASignalMode, EMASource, MACDParams, MovingAverageType


class RuleTests(unittest.TestCase):
    def test_nifty_lot_size_by_contract_expiry(self):
        cases = {date(2024, 4, 25): 50, date(2024, 5, 2): 25, date(2024, 12, 19): 25,
                 date(2024, 12, 26): 25, date(2025, 1, 2): 75, date(2025, 1, 30): 25,
                 date(2025, 2, 27): 75, date(2025, 12, 30): 75, date(2026, 1, 6): 65}
        for d, lot in cases.items():
            self.assertEqual(NIFTY.lot_size_for(d), lot, d)
        self.assertEqual(NIFTY.lot_size_for(date(2021, 7, 29)), 65)

    def test_charges_match_hand_calculation(self):
        # SELL 65 x Rs100 on 2025-01-15: turnover 6500, brokerage 0 (not assumed)
        c = leg_costs(NIFTY, date(2025, 1, 15), 100.0, 65, is_buy=False)
        t = 6500.0
        expect = t * 0.001 + t * 0.0003503 + t * 50 / 1e7 + t * 10 / 1e7
        expect += 0.18 * (t * 0.0003503 + t * 10 / 1e7 + t * 50 / 1e7)
        self.assertAlmostEqual(c, expect, places=9)
        b = leg_costs(NIFTY, date(2025, 1, 15), 100.0, 65, is_buy=True)         # no STT, stamp duty instead
        self.assertAlmostEqual(b, expect - t * 0.001 + t * 0.00003, places=9)

    def test_dated_rates(self):
        self.assertEqual(NIFTY.stt_rate_on(date(2023, 11, 23)), 0.000625)
        self.assertEqual(NIFTY.stt_rate_on(date(2025, 3, 3)), 0.001)
        self.assertEqual(NIFTY.stt_rate_on(date(2026, 4, 1)), 0.0015)
        self.assertEqual(NIFTY.txn_rate_on(date(2024, 5, 1)), 0.000495)
        self.assertEqual(NIFTY.stt_rate_on(date(2023, 3, 31)), 0.0015)
        self.assertEqual(NIFTY.txn_rate_on(date(2023, 3, 31)), 0.0003503)

    def test_strategy_combined_points_sl_config(self):
        params = lab_strategy.default_params()
        params["portfolio"].update(sl_kind="pts", sl_val=30.0)
        settings = lab_strategy.default_globals(date(2025, 1, 1), date(2025, 1, 31))

        cfg = lab_strategy.build_config(params, settings)

        self.assertEqual(cfg.portfolio_sl_points, 30.0)
        self.assertIsNone(cfg.portfolio_sl)
        self.assertIsNone(cfg.portfolio_sl_pct)

    def test_ema_strategy_config(self):
        params = lab_strategy.default_params()
        params["kind"] = "indicator"
        params["indicator"].update(type="EMA", ema_mode="EMA crossover",
                                   ema_fast=8, ema_slow=21, ema_slope_filter=True,
                                   ema_slope_type="SMA", ema_slope_period=30, ema_timeframe="1d",
                                   ema_source="Option premium")
        settings = lab_strategy.default_globals(date(2025, 1, 1), date(2025, 1, 31))
        settings["expiry_day_only"] = True

        cfg = lab_strategy.build_config(params, settings)

        self.assertTrue(cfg.expiry_day_only)
        self.assertIsInstance(cfg.indicator, EMAParams)
        self.assertEqual(cfg.indicator.mode, EMASignalMode.EMA_CROSSOVER)
        self.assertEqual(cfg.indicator.fast_period, 8)
        self.assertEqual(cfg.indicator.slow_period, 21)
        self.assertEqual(cfg.indicator.slope_type, MovingAverageType.SMA)
        self.assertEqual(cfg.indicator.slope_period, 30)
        self.assertEqual(cfg.indicator.timeframe, "1d")
        self.assertEqual(cfg.indicator.source, EMASource.OPTION)

    def test_macd_strategy_config_and_defaults(self):
        params = lab_strategy.default_params()
        params["kind"] = "indicator"
        params["indicator"].update(type="MACD", macd_fast=8, macd_slow=21,
                                   macd_signal=5, macd_timeframe="60m")
        settings = lab_strategy.default_globals(date(2025, 1, 1), date(2025, 1, 31))

        cfg = lab_strategy.build_config(params, settings)
        self.assertEqual(cfg.indicator, MACDParams(8, 21, 5, "60m"))
        self.assertEqual(lab_strategy.parse_strategy(lab_strategy.strategy_to_json(params))["indicator"]["macd_timeframe"], "60m")
        self.assertEqual(lab_strategy.indicator_warmup_days(params), 16)

    def test_macd_rejects_invalid_period_order(self):
        with self.assertRaisesRegex(ValueError, "fast MACD period"):
            MACDParams(26, 26, 9)

    def test_time_based_strategy_configures_atm_reference(self):
        params = lab_strategy.default_params()
        params["legs"] = [lab_strategy.default_leg(0)]
        settings = lab_strategy.default_globals(date(2025, 1, 1), date(2025, 1, 31))
        settings["atm_reference"] = ATMReference.SYNTHETIC_FUTURE.value
        serialized = lab_strategy.settings_to_json(settings)
        parsed = lab_strategy.parse_settings(serialized)

        cfg = lab_strategy.build_config(params, parsed)
        self.assertEqual(cfg.legs[0].strike.atm_reference, ATMReference.SYNTHETIC_FUTURE)

        parsed["atm_reference"] = "invalid"
        self.assertTrue(any(error.startswith("Choose ATM reference")
                            for error in lab_strategy.validate(params, parsed)))

    def test_legacy_ema_strategy_defaults_to_underlying_source(self):
        params = lab_strategy.parse_strategy({
            "kind": "indicator",
            "indicator": {"type": "EMA", "ema_mode": "Price vs EMA"},
        })

        self.assertEqual(params["indicator"]["ema_source"], "Underlying")

    def test_legacy_supertrend_timeframe_is_migrated(self):
        params = lab_strategy.parse_strategy({
            "kind": "indicator",
            "indicator": {"type": "Supertrend", "st_tf": 15},
        })

        self.assertEqual(params["indicator"]["st_timeframe"], "15m")

    def test_indicator_strategy_omits_legacy_expression_and_uses_leg_side(self):
        params = lab_strategy.default_params()
        params["kind"] = "indicator"
        params["indicator_legs"] = [{**lab_strategy.default_leg(0), "side": "Sell", "type": "PE"}]
        params["indicator"]["expression"] = "Buy option with the trend"

        payload = lab_strategy.strategy_to_json(params)
        self.assertNotIn("expression", payload["indicator"])

        cfg = lab_strategy.build_config(params, lab_strategy.default_globals(date(2025, 1, 1), date(2025, 1, 31)))
        self.assertEqual(cfg.expression, OptionExpression.SHORT_PREMIUM)

    def test_holding_mode_defaults_and_config_round_trip(self):
        settings = lab_strategy.default_globals(date(2025, 1, 1), date(2025, 1, 31))
        self.assertEqual(settings["holding_mode"], HoldingMode.INTRADAY.value)
        self.assertEqual(settings["rollover_strike_mode"], RolloverStrikeMode.ORIGINAL.value)
        settings["holding_mode"] = HoldingMode.CARRY_FRESH_ENTRIES.value
        settings["rollover_strike_mode"] = RolloverStrikeMode.RESELECT.value
        settings["expiry_day_only"] = True
        parsed = lab_strategy.parse_settings(lab_strategy.settings_to_json(settings))
        cfg = lab_strategy.build_config(lab_strategy.default_params(), parsed)
        self.assertEqual(cfg.holding_mode, HoldingMode.CARRY_FRESH_ENTRIES)
        self.assertEqual(cfg.rollover_strike_mode, RolloverStrikeMode.RESELECT)
        self.assertTrue(cfg.expiry_day_only)
        legacy = lab_strategy.parse_settings({"start": "2025-01-01", "end": "2025-01-31"})
        self.assertEqual(legacy["holding_mode"], HoldingMode.INTRADAY.value)
        self.assertEqual(legacy["rollover_strike_mode"], RolloverStrikeMode.ORIGINAL.value)
        self.assertFalse(legacy["expiry_day_only"])

    def test_expiry_guard_blocks_missing_weekly(self):
        e = [date(2025, 1, 7), date(2025, 1, 21)]       # Jan 14 missing
        got, why = choose_expiry(e, date(2025, 1, 8), ExpirySelect.CURRENT_WEEKLY, 7)
        self.assertIsNone(got)
        self.assertIn("missing", why)
        got, _ = choose_expiry(e, date(2025, 1, 8), ExpirySelect.CURRENT_WEEKLY, None)
        self.assertEqual(got, date(2025, 1, 21))        # guard off: silently the next one
        got, why = choose_expiry([date(2025, 1, 7), date(2025, 1, 21)], date(2025, 1, 1),
                                 ExpirySelect.NEXT_WEEKLY, 7)
        self.assertIsNone(got)                           # Jan 14 missing: second expiry would be wrong
        got, why = choose_expiry([date(2025, 1, 7), date(2025, 1, 14)], date(2025, 1, 1),
                                 ExpirySelect.NEXT_WEEKLY, 7)
        self.assertEqual(got, date(2025, 1, 14))

    def test_monthly_picks_last_expiry_of_month(self):
        e = [date(2025, 1, 7), date(2025, 1, 14), date(2025, 1, 21), date(2025, 1, 28), date(2025, 2, 4)]
        self.assertEqual(choose_expiry(e, date(2025, 1, 2), ExpirySelect.MONTHLY, 7)[0], date(2025, 1, 28))


class AnalyticsTests(unittest.TestCase):
    def _trades(self):
        return pd.DataFrame({"date": [date(2025, 1, 6), date(2025, 1, 8)], "leg_id": ["L1", "L1"],
                             "gross_pnl": [100.0, -50.0], "costs": [0.0, 0.0], "net_pnl": [100.0, -50.0],
                             "slippage_cost": [0.0, 0.0], "exit_reason": ["EOD_SQUARE_OFF"] * 2})

    def test_flat_days_enter_the_series(self):
        uni = [date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 8), date(2025, 1, 9)]
        d = build_daily(self._trades(), 100000.0, uni)
        self.assertEqual(len(d), 4)
        self.assertEqual(list(d["traded"]), [True, False, True, False])
        m = compute_metrics(self._trades(), d, 100000.0)
        self.assertEqual(m["total_trades"], 2)
        self.assertEqual(m["trading_days_in_scope"], 4)
        self.assertAlmostEqual(m["periods_per_year"], 4 / (4 / 365.25))   # derived from the data, not 252

    def test_no_capital_means_no_ratios(self):
        d = build_daily(self._trades(), None, [date(2025, 1, 6), date(2025, 1, 8)])
        m = compute_metrics(self._trades(), d, None)
        self.assertTrue(np.isnan(m["sharpe"]) and np.isnan(m["max_drawdown_pct"]))
        self.assertAlmostEqual(m["max_drawdown"], 50.0)                    # Rs drawdown still reported




if __name__ == "__main__":
    unittest.main()
