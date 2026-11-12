import os
import sys
import unittest
from datetime import date, time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optbt import (NIFTY, GreekSettings, LegConfig, OptionType, RiskRule, Side, StrategyConfig, StrikeSelection,
                   chain_greeks_at, simulate_day)
from optbt.data import O
from optbt.schemas import EntryCondition
from test_greeks import make_day

EXP = date(2025, 1, 9)


def strangle(delta=0.20, **kw):
    legs = (LegConfig(Side.SELL, OptionType.CE, StrikeSelection.greek("delta", delta)),
            LegConfig(Side.SELL, OptionType.PE, StrikeSelection.greek("delta", delta)))
    base = dict(name="g", underlying="NIFTY", entry_time=time(9, 30), exit_time=time(15, 15), legs=legs, apply_costs=False)
    base.update(kw)
    return StrategyConfig(**base)


class GreekSelectionTests(unittest.TestCase):
    def test_picks_the_strike_closest_to_target_delta(self):
        day = make_day()
        rows, why = simulate_day(day, strangle(0.20), NIFTY)
        self.assertIsNone(why)
        ce = next(r for r in rows if r["option_type"] == "CE")
        pe = next(r for r in rows if r["option_type"] == "PE")
        gs = GreekSettings()
        for ot, row in (("CE", ce), ("PE", pe)):
            ch = chain_greeks_at(day, EXP, ot, 15, O, gs)           # entry bar for 09:30
            best = ch["strike"][np.argmin(np.abs(np.abs(ch["delta"]) - 0.20))]
            self.assertEqual(row["strike"], int(best))
            self.assertAlmostEqual(abs(row["entry_delta"]), 0.20, delta=0.04)
        self.assertGreater(ce["strike"], 23500)
        self.assertLess(pe["strike"], 23500)
        for r in rows:
            self.assertTrue(np.isfinite([r["entry_iv"], r["entry_gamma"], r["entry_theta"], r["entry_vega"]]).all())
            self.assertLess(r["entry_theta"], 0)
            self.assertGreater(r["entry_vega"], 0)

    def test_higher_delta_is_closer_to_the_money(self):
        day = make_day()
        a = {r["option_type"]: r["strike"] for r in simulate_day(day, strangle(0.10), NIFTY)[0]}
        b = {r["option_type"]: r["strike"] for r in simulate_day(day, strangle(0.35), NIFTY)[0]}
        self.assertGreater(a["CE"], b["CE"])
        self.assertLess(a["PE"], b["PE"])

    def test_min_max_window_and_no_candidate(self):
        day = make_day()
        leg = LegConfig(Side.SELL, OptionType.CE, StrikeSelection.greek("delta", 0.20, lo=0.45, hi=0.55))
        rows, _ = simulate_day(day, strangle(0.2, legs=(leg,)), NIFTY)
        self.assertAlmostEqual(abs(rows[0]["entry_delta"]), 0.5, delta=0.06)       # forced into the 45-55 band
        none = LegConfig(Side.SELL, OptionType.CE, StrikeSelection.greek("delta", 1.2, lo=1.1, hi=1.5))
        rows, why = simulate_day(day, strangle(0.2, legs=(none,)), NIFTY)
        self.assertEqual(rows, [])
        self.assertIn("unavailable", why)

    def test_other_metrics_select_by_iv_theta_vega(self):
        day = make_day()
        for metric, target in (("iv", 15.0), ("theta", 8.0), ("vega", 10.0)):
            leg = LegConfig(Side.BUY, OptionType.CE, StrikeSelection.greek(metric, target))
            rows, why = simulate_day(day, strangle(legs=(leg,)), NIFTY)
            self.assertIsNone(why, metric)
            self.assertEqual(len(rows), 1)


class EntryConditionTests(unittest.TestCase):
    def test_spot_move_condition_delays_entry_to_first_true_minute(self):
        day = make_day(drift=0.5)                 # +0.5 pt per minute
        c = EntryCondition("spot_move", ">=", 0.10)   # needs +23.5 points -> about minute 47
        cfg = strangle(0.2, entry_conditions=(c,), entry_window_end=time(12, 0))
        rows, why = simulate_day(day, cfg, NIFTY)
        self.assertIsNone(why)
        mins = rows[0]["entry_time"].hour * 60 + rows[0]["entry_time"].minute - 555
        self.assertAlmostEqual(mins, 47, delta=1)

    def test_conditions_never_met_skips_with_filter_reason(self):
        day = make_day(drift=0.0)
        cfg = strangle(0.2, entry_conditions=(EntryCondition("spot_move", ">=", 5.0),), entry_window_end=time(11, 0))
        rows, why = simulate_day(day, cfg, NIFTY)
        self.assertEqual(rows, [])
        self.assertTrue(why.startswith("filter:"))

    def test_all_conditions_must_hold(self):
        day = make_day(drift=0.5)
        ok = EntryCondition("spot_vs_vwap", "is", "above")
        bad = EntryCondition("spot_vs_vwap", "is", "below")
        rows, _ = simulate_day(day, strangle(0.2, entry_conditions=(ok,), entry_window_end=time(10, 0)), NIFTY)
        self.assertEqual(len(rows), 2)
        rows, why = simulate_day(day, strangle(0.2, entry_conditions=(ok, bad), entry_window_end=time(10, 0)), NIFTY)
        self.assertEqual(rows, [])

    def test_atm_iv_condition(self):
        day = make_day(sig=0.15)
        inside = EntryCondition("atm_iv", "between", 14.0, 16.0)
        outside = EntryCondition("atm_iv", ">=", 25.0)
        self.assertEqual(len(simulate_day(day, strangle(0.2, entry_conditions=(inside,)), NIFTY)[0]), 2)
        rows, why = simulate_day(day, strangle(0.2, entry_conditions=(outside,)), NIFTY)
        self.assertEqual(rows, [])
        self.assertTrue(why.startswith("filter:"))

    def test_position_greek_conditions(self):
        day = make_day()
        # a symmetric short strangle is nearly delta neutral, and short theta/vega P&L sign: net_theta > 0 (collects decay)
        neutral = EntryCondition("net_delta", "abs<=", 0.15)
        self.assertEqual(len(simulate_day(day, strangle(0.2, entry_conditions=(neutral,)), NIFTY)[0]), 2)
        tight = EntryCondition("net_delta", "abs<=", 0.0001)
        self.assertEqual(simulate_day(day, strangle(0.2, entry_conditions=(tight,)), NIFTY)[0], [])
        earns_theta = EntryCondition("net_theta", ">=", 1.0)
        self.assertEqual(len(simulate_day(day, strangle(0.2, entry_conditions=(earns_theta,)), NIFTY)[0]), 2)
        short_vega = EntryCondition("net_vega", "<=", -1.0)
        self.assertEqual(len(simulate_day(day, strangle(0.2, entry_conditions=(short_vega,)), NIFTY)[0]), 2)

    def test_validation(self):
        with self.assertRaises(ValueError):
            EntryCondition("bogus")
        with self.assertRaises(ValueError):
            EntryCondition("atm_iv", "between", 20.0, 10.0)
        with self.assertRaises(ValueError):
            EntryCondition("spot_vs_vwap", "is", "up")
        with self.assertRaises(ValueError):
            strangle(entry_window_end=time(9, 0))
        with self.assertRaises(ValueError):
            StrikeSelection.greek("gamma", 0.1)


if __name__ == "__main__":
    unittest.main()
