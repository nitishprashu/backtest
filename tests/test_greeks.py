import math
import os
import sys
import unittest
from datetime import date

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from optbt import GreekSettings, chain_greeks_at, greeks_at, greeks_series, implied_vol
from optbt.data import C, N_BARS, O, V, DayData
from optbt.greeks import b76_price, estimate_carry, greeks_from_iv, tte_years

N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


class MathTests(unittest.TestCase):
    def test_known_price_and_parity(self):
        F, K, T, s = 100.0, 100.0, 1.0, 0.2
        d1 = 0.5 * s * math.sqrt(T)
        call = F * N(d1) - K * N(d1 - s)
        self.assertAlmostEqual(float(b76_price(F, K, T, s, True, 1.0)), call, places=10)
        for df in (1.0, 0.97):
            for K in (80.0, 100.0, 120.0):
                c, p = b76_price(F, K, 0.5, 0.25, True, df), b76_price(F, K, 0.5, 0.25, False, df)
                self.assertAlmostEqual(float(c - p), df * (F - K), places=9)

    def test_iv_roundtrip_grid(self):
        F, df = 23500.0, math.exp(-0.065 * 0.02)
        for sig in (0.06, 0.12, 0.25, 0.8):
            for T in (0.0007, 0.006, 0.02, 0.2):
                for K in (22000.0, 23000.0, 23500.0, 24000.0, 25000.0):
                    for call in (True, False):
                        px = float(b76_price(F, K, T, sig, call, df))
                        if px < 0.05:
                            continue
                        got = float(implied_vol(px, F, K, T, call, df))
                        if np.isfinite(got):
                            self.assertAlmostEqual(got, sig, delta=2e-5, msg=(sig, T, K, call))

    def test_invalid_prices_are_nan_not_invented(self):
        F, K, T, df = 23500.0, 23000.0, 0.01, 1.0
        self.assertTrue(np.isnan(float(implied_vol(400.0, F, K, T, True, df))))      # below intrinsic 500
        self.assertTrue(np.isnan(float(implied_vol(0.0, F, K, T, True, df))))
        self.assertTrue(np.isnan(float(implied_vol(np.nan, F, K, T, True, df))))
        self.assertTrue(np.isnan(float(implied_vol(24000.0, F, K, T, True, df))))    # above the ceiling

    def test_greeks_vs_finite_differences_and_analytic_theta(self):
        S, K, T, sig, r, c = 23500.0, 23600.0, 0.03, 0.15, 0.065, 0.03
        call = np.array(True)

        def price(S_, sig_, T_, call_=True):
            F = S_ * math.exp(c * T_)
            return float(b76_price(F, K, T_, sig_, call_, math.exp(-r * T_)))
        g = greeks_from_iv(S, K, T, sig, call, r, c)
        dS = 0.5
        self.assertAlmostEqual(float(g["delta"]), (price(S + dS, sig, T) - price(S - dS, sig, T)) / (2 * dS), places=6)
        self.assertAlmostEqual(float(g["gamma"]), (price(S + dS, sig, T) - 2 * price(S, sig, T) + price(S - dS, sig, T)) / dS ** 2, places=7)
        self.assertAlmostEqual(float(g["vega"]), (price(S, sig + 0.0005, T) - price(S, sig - 0.0005, T)) / 0.001 / 100, places=5)
        # analytic BSM theta with q = r - c (per calendar day, constant spot)
        q = r - c
        d1 = (math.log(S / K) + (r - q + 0.5 * sig ** 2) * T) / (sig * math.sqrt(T))
        d2 = d1 - sig * math.sqrt(T)
        npdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
        th = (-S * npdf * sig * math.exp(-q * T) / (2 * math.sqrt(T)) + q * S * N(d1) * math.exp(-q * T) - r * K * math.exp(-r * T) * N(d2)) / 365
        self.assertAlmostEqual(float(g["theta"]), th, delta=abs(th) * 2e-3)
        put = greeks_from_iv(S, K, T, sig, np.array(False), r, c)
        self.assertAlmostEqual(float(g["delta"] - put["delta"]), math.exp((c - r) * T), places=9)   # call - put delta
        self.assertAlmostEqual(float(put["gamma"]), float(g["gamma"]), places=12)
        self.assertLess(float(put["delta"]), 0)
        self.assertLess(float(g["theta"]), 0)

    def test_time_to_expiry(self):
        e = date(2025, 1, 9)
        t0 = float(tte_years(e, date(2025, 1, 9), 0))                      # 09:15 on expiry day: 6h15m left
        self.assertAlmostEqual(t0 * 365 * 24 * 60, 375.0, places=4)
        t1 = float(tte_years(e, date(2025, 1, 8), 374, at_close=True))     # last minute of the previous day
        self.assertAlmostEqual(t1 * 365 * 24 * 60, 1440.0, places=4)       # 15:30 the day before = exactly one day


def make_day(sig=0.15, carry=0.03, rate=0.065, expiry=date(2025, 1, 9), trade=date(2025, 1, 7), spot0=23500.0, drift=0.0):
    bars = np.arange(N_BARS)
    spot = np.full(N_BARS, spot0) + drift * bars
    sp = np.vstack([spot, spot + 1, spot - 1, spot])
    opts = {}
    for K in range(22800, 24250, 50):
        for ot in ("CE", "PE"):
            arr = np.zeros((5, N_BARS))
            for row, at_close in ((O, False), (C, True)):
                T = tte_years(expiry, trade, bars, at_close)
                F = spot * np.exp(carry * T)
                arr[row] = b76_price(F, float(K), T, sig, ot == "CE", np.exp(-rate * T))
            arr[1], arr[2] = np.maximum(arr[0], arr[3]), np.minimum(arr[0], arr[3])
            arr[V] = 100.0
            opts[(expiry, K, ot)] = arr
    return DayData(trade, sp, opts, [expiry], selected_expiry=expiry)


class SessionTests(unittest.TestCase):
    def test_carry_recovered_from_parity(self):
        day = make_day(carry=0.03)
        c, src = estimate_carry(day, date(2025, 1, 9), GreekSettings())
        self.assertEqual(src, "parity")
        self.assertAlmostEqual(c, 0.03, delta=2e-3)

    def test_fallback_and_manual_labels(self):
        day = make_day()
        day.options = {k: v for k, v in day.options.items() if k[2] == "CE"}        # no puts -> no parity
        c, src = estimate_carry(day, date(2025, 1, 9), GreekSettings(rate=0.06, div_yield=0.01))
        self.assertEqual(src, "fallback")
        self.assertAlmostEqual(c, 0.05)
        c2, src2 = estimate_carry(make_day(), date(2025, 1, 9), GreekSettings(forward="manual", rate=0.06, div_yield=0.01))
        self.assertEqual((round(c2, 6), src2), (0.05, "manual"))

    def test_iv_and_greeks_recover_the_generating_vol(self):
        day = make_day(sig=0.15, carry=0.03)
        gs = GreekSettings()
        for key in ((date(2025, 1, 9), 23500, "CE"), (date(2025, 1, 9), 23700, "PE"), (date(2025, 1, 9), 23300, "CE")):
            g = greeks_at(day, key, 60, O, gs)
            self.assertAlmostEqual(g["iv"], 15.0, delta=0.05, msg=key)
            s = greeks_series(day, key, gs)
            self.assertAlmostEqual(float(np.nanmedian(s["iv"])), 15.0, delta=0.05)
        ce = greeks_at(day, (date(2025, 1, 9), 23500, "CE"), 60, O, gs)["delta"]
        pe = greeks_at(day, (date(2025, 1, 9), 23500, "PE"), 60, O, gs)["delta"]
        self.assertAlmostEqual(ce - pe, math.exp((0.03 - 0.065) * float(tte_years(date(2025, 1, 9), date(2025, 1, 7), 60))), delta=1e-3)

    def test_chain_is_ordered_by_moneyness(self):
        day = make_day()
        ch = chain_greeks_at(day, date(2025, 1, 9), "CE", 30, O, GreekSettings())
        order = np.argsort(ch["strike"])
        d = ch["delta"][order]
        self.assertTrue(np.all(np.diff(d) < 0))                       # call delta falls as the strike rises
        self.assertTrue(0 < d.min() and d.max() < 1)
        self.assertTrue(np.all(ch["vega"] > 0) and np.all(ch["theta"] < 0))


if __name__ == "__main__":
    unittest.main()
