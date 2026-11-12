"""Service + HTTP tests over the in-memory provider. Run: python -m unittest tests.test_api -v"""
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fixtures import MemoryProvider
from optbt import DataStore
from lab.runs import RunStore
from lab.server import create_server, serve_in_thread
from lab.service import ApiError, Service, _chart_session_duration, _ensure_trade_contracts

PROVIDER = None


def provider():
    global PROVIDER
    if PROVIDER is None:
        PROVIDER = MemoryProvider("2025-01-01", "2025-02-21", seed=3)
    return PROVIDER


class Api:
    def __init__(self, base):
        self.base = base

    def call(self, method, path, body=None, headers=None, host=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {"X-Lab-Client": "1", "Content-Type": "application/json"}
        h.update(headers or {})
        if host:
            h["Host"] = host
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if r.headers.get("Content-Type", "").startswith("application/json") else raw)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw

    def wait(self, job, timeout=90):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st, j = self.call("GET", f"/api/jobs/{job['id']}")
            assert st == 200, j
            if j["status"] in ("done", "error", "cancelled"):
                return j
            time.sleep(0.05)
        raise AssertionError("job timeout")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service = Service(provider(), RunStore(os.path.join(cls.tmp.name, "runs.db")))
        cls.srv = create_server(cls.service, port=0)
        serve_in_thread(cls.srv)
        port = cls.srv.server_address[1]
        cls.api = Api(f"http://127.0.0.1:{port}")
        st, cfg = cls.api.call("GET", "/api/config")
        assert st == 200
        cls.cfg = cfg

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    def payload(self, **over):
        strat = json.loads(json.dumps(self.cfg["strategy"]))
        strat["legs"][0].update(sl_kind="%", sl_val=25.0)
        strat["legs"][1].update(sl_kind="%", sl_val=25.0)
        s = dict(self.cfg["settings"], start="2025-01-01", end="2025-02-21", entry_time="09:20", exit_time="15:15")
        s.update(over.pop("settings", {}))
        p = {"settings": s, "strategy": strat}
        p.update(over)
        return p

    def run_and_wait(self, payload):
        st, job = self.api.call("POST", "/api/runs", payload)
        self.assertEqual(st, 200, job)
        j = self.api.wait(job)
        self.assertEqual(j["status"], "done", j)
        return j["result"]["run_id"]

    def test_replay_loads_exact_trade_contract_outside_spot_strike_window(self):
        contract = (date(2022, 10, 27), 17150, "CE")
        day = SimpleNamespace(trade_date=date(2022, 10, 26), options={})
        loaded_prices = np.ones((5, 375))
        calls = []

        class Store:
            def load_contract_day(self, trade_date, requested):
                calls.append((trade_date, requested))
                return loaded_prices

        trades = pd.DataFrame([{"expiry": contract[0], "strike": contract[1],
                                "option_type": contract[2]}])
        _ensure_trade_contracts(day, trades, Store())

        self.assertEqual(calls, [(day.trade_date, contract)])
        self.assertIs(day.options[contract], loaded_prices)

    def test_replay_reports_unavailable_trade_contract_as_api_error(self):
        contract = (date(2022, 10, 27), 17150, "CE")
        day = SimpleNamespace(trade_date=date(2022, 10, 26), options={})

        class Store:
            def load_contract_day(self, _trade_date, _requested):
                return None

        trades = pd.DataFrame([{"expiry": contract[0], "strike": contract[1],
                                "option_type": contract[2]}])
        with self.assertRaisesRegex(ApiError, "has no replay data") as error:
            _ensure_trade_contracts(day, trades, Store())
        self.assertEqual(error.exception.status, 409)

    def test_standalone_day_replay_uses_strategy_without_saving_a_run(self):
        payload = self.payload()
        payload["date"] = "2025-01-03"
        before = {run["id"] for run in self.service.list_runs()}
        status, job = self.api.call("POST", "/api/replay", payload)
        self.assertEqual(status, 200, job)
        result = self.api.wait(job)
        self.assertEqual(result["status"], "done", result)
        replay = result["result"]
        self.assertEqual(replay["date"], "2025-01-03")
        self.assertEqual(len(replay["chart"]["spot"]["c"]), 375)
        self.assertIn("totals", replay["chart"])
        self.assertEqual({run["id"] for run in self.service.list_runs()}, before)

    def test_standalone_replay_requires_a_date(self):
        status, error = self.api.call("POST", "/api/replay", self.payload())
        self.assertEqual(status, 400)
        self.assertIn("missing field", error["error"])

    def test_standalone_indicator_replay_includes_indicator_history(self):
        payload = self.payload()
        payload["date"] = "2025-02-03"
        payload["strategy"]["kind"] = "indicator"
        payload["strategy"]["indicator"].update(
            type="EMA", ema_source="Underlying", ema_period=5, ema_timeframe="60m", initial_trend=True)
        payload["strategy"]["ind_leg"].update(sl_kind="%", sl_val=30.0)
        status, job = self.api.call("POST", "/api/replay", payload)
        self.assertEqual(status, 200, job)
        result = self.api.wait(job)
        self.assertEqual(result["status"], "done", result)
        indicator = result["result"]["chart"]["indicator"]
        self.assertTrue(indicator["lines"])
        self.assertEqual(len(indicator["lines"][0]["values"]), 375)
        self.assertEqual(indicator["verification"]["timeframe"], "60m")

    def test_carry_replay_keeps_backtest_boundary_positions_open(self):
        payload = self.payload(settings={"holding_mode": "CARRY_SINGLE"})
        payload["date"] = "2025-01-03"
        status, job = self.api.call("POST", "/api/replay", payload)
        self.assertEqual(status, 200, job)
        result = self.api.wait(job)
        self.assertEqual(result["status"], "done", result)
        legs = result["result"]["chart"]["legs"]
        self.assertTrue(legs)
        self.assertTrue(any(not leg["closed_today"] for leg in legs))

    # ---- tests
    def test_config(self):
        from lab import APP_VERSION
        self.assertEqual(self.cfg["version"], APP_VERSION)
        self.assertGreaterEqual(len(self.cfg["templates"]), 10)
        self.assertIsNotNone(self.cfg["coverage"]["first"])
        self.assertEqual(self.cfg["strategy"]["kind"], "multi")

    def test_run_detail_trades_csv_explorer(self):
        rid = self.run_and_wait(self.payload())
        st, d = self.api.call("GET", f"/api/runs/{rid}")
        self.assertEqual(st, 200)
        self.assertEqual(d["trade_event_dates"], sorted(d["trade_event_dates"]))
        chart_date = d["trade_event_dates"][0]
        st, chart = self.api.call("GET", f"/api/runs/{rid}/nifty-trades?date={chart_date}")
        self.assertEqual(st, 200, chart)
        self.assertEqual(chart["date"], chart_date)
        self.assertEqual(len(chart["bars"]), 375)
        self.assertTrue(chart["events"])
        self.assertIn("ENTRY", {event["kind"] for event in chart["events"]})
        self.assertIn("EXIT", {event["kind"] for event in chart["events"]})
        m = d["metrics"]
        self.assertGreater(m["total_trades"], 20)
        for key in ("equity", "monthly", "weekday", "dte", "hist", "exits", "maemfe", "drawdowns",
                    "iv_ranges", "premium_ranges", "extra"):
            self.assertIn(key, d["dashboard"])
        self.assertEqual(sum(row["days"] for row in d["dashboard"]["premium_ranges"]), m["total_trades"])
        if d["dashboard"]["iv_ranges"]:
            self.assertEqual(sum(row["days"] for row in d["dashboard"]["iv_ranges"]), m["total_trades"])
        self.assertEqual(len(d["dashboard"]["equity"]), m["trading_days_in_scope"])
        st, t = self.api.call("GET", f"/api/runs/{rid}/trades?limit=10&sort=net_pnl&order=desc")
        self.assertEqual(st, 200)
        self.assertEqual(len(t["rows"]), 10)
        self.assertIn("entry_date", t["rows"][0])
        self.assertIn("exit_date", t["rows"][0])
        self.assertIn("expiry", t["rows"][0])
        nets = [r["net_pnl"] for r in t["rows"]]
        self.assertEqual(nets, sorted(nets, reverse=True))
        st, t2 = self.api.call("GET", f"/api/runs/{rid}/trades?result=loss&limit=500")
        self.assertTrue(all(r["net_pnl"] < 0 for r in t2["rows"]))
        selected_day = d["trade_dates"][0]
        st, day_trades = self.api.call("GET", f"/api/runs/{rid}/trades?date={selected_day}&limit=500")
        self.assertEqual(st, 200, day_trades)
        self.assertTrue(day_trades["rows"])
        self.assertTrue(all(row["entry_date"] == selected_day for row in day_trades["rows"]))
        self.assertAlmostEqual(day_trades["sum_net"], sum(row["net_pnl"] for row in day_trades["rows"]), places=2)
        st, csv = self.api.call("GET", f"/api/runs/{rid}/trades.csv")
        self.assertEqual(st, 200)
        self.assertIn(b"net_pnl", csv.split(b"\n")[0])
        self.assertIn(b"entry_date", csv.split(b"\n")[0])
        self.assertIn(b"exit_date", csv.split(b"\n")[0])
        day = d["trade_dates"][3]
        st, ex = self.api.call("GET", f"/api/runs/{rid}/explorer?date={day}")
        self.assertEqual(st, 200, ex)
        self.assertEqual(len(ex["mtm"]), 375)
        self.assertEqual(len(ex["spot"]["c"]), 375)
        self.assertAlmostEqual(ex["mtm"][-1], ex["totals"]["gross"], places=2)
        self.assertEqual(st, 200)

    def test_equity_matches_trade_log(self):
        rid = self.run_and_wait(self.payload())
        st, d = self.api.call("GET", f"/api/runs/{rid}")
        st, t = self.api.call("GET", f"/api/runs/{rid}/trades?limit=500")
        total = d["metrics"]["total_net_pnl"]
        self.assertAlmostEqual(t["sum_net"], total, places=2)
        self.assertAlmostEqual(d["dashboard"]["equity"][-1]["cum"], total, places=2)

    def test_chart_workspace_and_complete_pdf_report(self):
        rid = self.run_and_wait(self.payload())
        status, trades = self.api.call("GET", f"/api/runs/{rid}/trades?limit=1")
        self.assertEqual(status, 200, trades)
        trade = trades["rows"][0]
        chart_date = trade["entry_date"]

        status, contracts = self.api.call(
            "GET", f"/api/runs/{rid}/chart-contracts?date={chart_date}")
        self.assertEqual(status, 200, contracts)
        self.assertIn(chart_date, contracts["dates"])
        self.assertTrue(any(c["expiry"] == trade["expiry"] and c["strike"] == trade["strike"]
                            and c["option_type"] == trade["option_type"] for c in contracts["contracts"]))

        status, spot = self.api.call(
            "GET", f"/api/runs/{rid}/market-chart?date={chart_date}&instrument=spot&timeframe=5m")
        self.assertEqual(status, 200, spot)
        self.assertEqual(len(spot["bars"]), 75)
        self.assertTrue(spot["events"])

        status, option = self.api.call(
            "GET", f"/api/runs/{rid}/market-chart?date={chart_date}&instrument=option"
            f"&expiry={trade['expiry']}&strike={trade['strike']}&option_type={trade['option_type']}&timeframe=5m")
        self.assertEqual(status, 200, option)
        self.assertEqual(len(option["bars"]), 75)
        self.assertTrue(option["events"])

        status, report = self.api.call("GET", f"/api/runs/{rid}/report.pdf")
        self.assertEqual(status, 200)
        self.assertTrue(report.startswith(b"%PDF-"))
        self.assertGreater(len(report), 2000)

    def test_run_chart_accepts_all_listed_timeframes(self):
        rid = self.run_and_wait(self.payload())
        status, trades = self.api.call("GET", f"/api/runs/{rid}/trades?limit=1")
        self.assertEqual(status, 200, trades)
        chart_date = trades["rows"][0]["entry_date"]

        for timeframe in ("3m", "10m", "2h", "4h", "1d", "1w", "1mo"):
            status, chart = self.api.call(
                "GET", f"/api/runs/{rid}/market-chart?date={chart_date}"
                f"&instrument=spot&timeframe={timeframe}")
            self.assertEqual(status, 200, chart)
            self.assertTrue(chart["bars"], timeframe)
            self.assertEqual(chart["timeframe"], timeframe)

    def test_global_chart_history_is_independent_of_runs_and_pages_backwards(self):
        status, contracts = self.api.call("GET", "/api/chart-contracts")
        self.assertEqual(status, 200, contracts)
        self.assertTrue(contracts["expiries"])
        self.assertTrue(contracts["contracts"])
        status, by_year = self.api.call(
            "GET", f"/api/chart-contracts?year={contracts['selected_year']}")
        self.assertEqual(status, 200, by_year)
        self.assertEqual(by_year["selected_year"], contracts["selected_year"])
        self.assertTrue(all(date.fromisoformat(expiry).year == by_year["selected_year"]
                            for expiry in by_year["expiries"]))

        status, recent = self.api.call(
            "GET", "/api/market-chart?instrument=spot&timeframe=5m&limit=100")
        self.assertEqual(status, 200, recent)
        self.assertEqual(len(recent["bars"]), 100)
        self.assertTrue(recent["has_older"])

        cursor = urllib.parse.quote(recent["bars"][0]["time"])
        status, older = self.api.call(
            "GET", f"/api/market-chart?instrument=spot&timeframe=5m&limit=100&before={cursor}")
        self.assertEqual(status, 200, older)
        self.assertTrue(older["bars"])
        self.assertLess(older["bars"][-1]["time"], recent["bars"][0]["time"])

        contract = contracts["contracts"][0]
        query = urllib.parse.urlencode({
            "instrument": "option", "timeframe": "1m",
            "expiry": contract["expiry"], "strike": contract["strike"],
            "option_type": contract["option_type"], "limit": 100,
        })
        status, option = self.api.call("GET", f"/api/market-chart?{query}")
        self.assertEqual(status, 200, option)
        self.assertLessEqual(len(option["bars"]), 100)

        for timeframe in ("2h", "4h", "1d", "1w", "1mo"):
            status, data = self.api.call(
                "GET", f"/api/market-chart?instrument=spot&timeframe={timeframe}&limit=100")
            self.assertEqual(status, 200, data)
            self.assertTrue(data["bars"], timeframe)
            self.assertLessEqual(len(data["bars"]), 100)

    def test_extended_option_chart_session_starts_on_3_august_2026(self):
        self.assertEqual(_chart_session_duration(date(2026, 8, 2), "option"), 375)
        self.assertEqual(_chart_session_duration(date(2026, 8, 3), "option"), 385)
        self.assertEqual(_chart_session_duration(date(2026, 8, 3), "spot"), 375)

    def test_option_chart_includes_bars_until_1539_after_session_extension(self):
        stamps = pd.to_datetime([
            "2026-08-03 09:15", "2026-08-03 15:29",
            "2026-08-03 15:39", "2026-08-03 15:40",
        ])
        spot = pd.DataFrame({
            "timestamp": stamps, "open": 25000.0, "high": 25001.0,
            "low": 24999.0, "close": 25000.0, "volume": 0.0,
        })
        options = pd.DataFrame({
            "timestamp": stamps, "expiry": pd.Timestamp("2026-08-06"),
            "strike": 25000, "option_type": "CE", "open": 100.0,
            "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10.0,
            "open_interest": 5.0,
        })
        store = DataStore(spot, options)

        class ChartProvider:
            def coverage(self):
                return {"first": date(2026, 8, 3), "last": date(2026, 8, 3)}

            def store(self, *_args, **_kwargs):
                return store

        original_provider = self.service.provider
        self.service.provider = ChartProvider()
        try:
            data = self.service.market_chart_history(
                "option", "1m", "2026-08-06", "25000", "CE")
        finally:
            self.service.provider = original_provider
        times = [bar["time"] for bar in data["bars"]]
        self.assertIn("2026-08-03T15:39", times)
        self.assertNotIn("2026-08-03T15:40", times)

    def test_preview_payoff(self):
        st, p = self.api.call("POST", "/api/preview", self.payload())
        self.assertEqual(st, 200, p)
        self.assertTrue(p["supported"] and p["ok"])
        self.assertEqual(len(p["legs"]), 2)
        self.assertTrue(p["unlimited_loss"])                      # short straddle
        self.assertEqual(len(p["breakevens"]), 2)
        self.assertGreater(p["net_premium"], 0)                   # credit
        self.assertLess(p["breakevens"][0], p["spot"] + 1)
        self.assertGreater(p["breakevens"][1], p["spot"] - 1)

    def test_calendar_template_preview_uses_near_and_next_expiries(self):
        template = next(t for t in self.cfg["templates"] if t["name"] == "Call calendar spread")
        pl = self.payload()
        pl["strategy"]["legs"] = template["legs"]

        st, preview = self.api.call("POST", "/api/preview", pl)

        self.assertEqual(st, 200, preview)
        self.assertTrue(preview["ok"])
        self.assertEqual([leg["expiry_offset"] for leg in template["legs"]], [0, 1])
        self.assertEqual(len({leg["expiry"] for leg in preview["legs"]}), 2)

        run_id = self.run_and_wait(pl)
        st, trades = self.api.call("GET", f"/api/runs/{run_id}/trades?limit=500")
        self.assertEqual(st, 200, trades)
        self.assertGreater(len({row["expiry"] for row in trades["rows"]}), 1)

    def test_optimizer_job(self):
        pl = self.payload()
        pl.update(x={"param": "sl", "from": 20, "to": 40, "step": 10}, y={"param": "tp", "from": 0, "to": 40, "step": 20},
                  split="2025-02-03")
        st, job = self.api.call("POST", "/api/optimize", pl)
        self.assertEqual(st, 200, job)
        j = self.api.wait(job)
        self.assertEqual(j["status"], "done", j)
        res = j["result"]
        self.assertEqual(len(res["cells"]), 9)
        self.assertTrue(all("net" in c for c in res["cells"]))
        self.assertTrue(all("oos" in c and "ins" in c for c in res["cells"]))
        self.assertEqual(res["y"]["labels"][0], "off")

    def test_indicator_run_and_history_ops(self):
        pl = self.payload()
        pl["strategy"]["kind"] = "indicator"
        pl["strategy"]["indicator"].update(
            type="EMA", ema_source="Underlying", ema_period=5, ema_timeframe="60m",
            initial_trend=True)
        pl["strategy"]["ind_leg"].update(sl_kind="%", sl_val=30.0)
        rid = self.run_and_wait(pl)
        st, trades = self.api.call("GET", f"/api/runs/{rid}/trades?limit=1")
        self.assertEqual(st, 200)
        self.assertIn(trades["rows"][0]["signal_direction"], ("BULLISH", "BEARISH"))
        self.assertIn("exit_date", trades["rows"][0])
        self.assertIn("expiry", trades["rows"][0])
        self.assertIsNotNone(trades["rows"][0]["signal_value_entry"])
        self.assertIsNotNone(trades["rows"][0]["signal_value_exit"])
        self.assertIsNotNone(trades["rows"][0]["signal_ema_entry"])
        self.assertIsNotNone(trades["rows"][0]["signal_ema_exit"])
        self.assertIsNotNone(trades["rows"][0]["signal_spot_entry"])
        self.assertIsNotNone(trades["rows"][0]["signal_spot_exit"])
        self.assertIsNotNone(trades["rows"][0]["spot_entry"])
        self.assertIsNotNone(trades["rows"][0]["spot_exit"])
        replay_date = trades["rows"][0]["exit_date"]
        st, replay = self.api.call("GET", f"/api/runs/{rid}/explorer?date={replay_date}")
        self.assertEqual(st, 200, replay)
        self.assertTrue(replay["indicator"]["lines"])
        self.assertEqual(len(replay["indicator"]["lines"][0]["values"]), 375)
        self.assertEqual(replay["indicator"]["verification"]["timeframe"], "60m")
        self.assertTrue(replay["indicator"]["verification"]["bars"])
        st, chart = self.api.call("GET", f"/api/runs/{rid}/nifty-backtest-chart")
        self.assertEqual(st, 200, chart)
        self.assertEqual(chart["timeframe"], "60m")
        self.assertTrue(any(line["label"] == "EMA(5)" for line in chart["lines"]))
        session_dates = [bar["date"] for bar in chart["bars"]]
        self.assertEqual(session_dates, sorted(session_dates))
        self.assertGreater(len(set(session_dates)), 1)
        self.assertEqual(sum(bar["time"] == "15:29" for bar in chart["bars"]),
                         len(set(session_dates)))
        ema_values = next(line["values"] for line in chart["lines"] if line["label"] == "EMA(5)")
        self.assertEqual(len(ema_values), len(chart["bars"]))
        self.assertTrue(all(ema_values[i] == ema_values[i - 1]
                            for i, bar in enumerate(chart["bars"])
                            if bar["time"] == "15:29"))
        self.assertTrue(chart["events"])
        self.assertTrue(all(0 <= event["index"] < len(chart["bars"])
                            for event in chart["events"]))
        self.assertEqual({event["kind"] for event in chart["events"]}, {"ENTRY", "EXIT"})
        st, runs = self.api.call("GET", "/api/runs")
        self.assertIn(rid, [r["id"] for r in runs["runs"]])
        rid2 = self.run_and_wait(self.payload())
        st, cmp_ = self.api.call("POST", "/api/compare", {"ids": [rid, rid2]})
        self.assertEqual(len(cmp_["runs"]), 2)
        st, _ = self.api.call("PATCH", f"/api/runs/{rid}", {"name": "My signal run"})
        self.assertEqual(st, 200)
        st, d = self.api.call("GET", f"/api/runs/{rid}")
        self.assertEqual(d["name"], "My signal run")
        st, _ = self.api.call("DELETE", f"/api/runs/{rid}")
        self.assertEqual(st, 200)
        st, e = self.api.call("GET", f"/api/runs/{rid}")
        self.assertEqual(st, 404)

    def test_data_status_and_repair(self):
        st, d = self.api.call("GET", "/api/data?start=2025-01-01&end=2025-02-21")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["session"]["first_bar"], "09:15")
        self.assertGreater(len(d["expiries"]), 3)
        st, job = self.api.call("POST", "/api/repair", {"expiry": "2025-01-14", "type": "CE", "strike": 23500,
                                                       "from": "2025-01-07", "to": "2025-01-14"})
        self.assertEqual(st, 200, job)
        self.assertEqual(self.api.wait(job)["status"], "done")

    def test_validation_and_security(self):
        pl = self.payload()
        pl["strategy"]["legs"][0].update(re_mode="Re-execute", sl_kind="Off")
        st, e = self.api.call("POST", "/api/runs", pl)
        self.assertEqual(st, 400)
        self.assertIn("re-entry needs a stop-loss", e["error"])
        st, e = self.api.call("GET", "/api/runs/abcdef0123")
        self.assertEqual(st, 404)
        st, e = self.api.call("POST", "/api/runs", self.payload(), headers={"X-Lab-Client": "0"})
        self.assertEqual(st, 403)
        st, e = self.api.call("GET", "/api/config", host="evil.example.com")
        self.assertEqual(st, 403)
        st, e = self.api.call("GET", "/api/nothing")
        self.assertEqual(st, 404)
        st, e = self.api.call("GET", "/../lab/server.py")
        self.assertIn(st, (403, 404))

    def test_dte_filter_is_reported_separately(self):
        rid = self.run_and_wait(self.payload(settings={"dte_min": 4, "dte_max": 6}))
        st, d = self.api.call("GET", f"/api/runs/{rid}")
        self.assertGreater(d["metrics"]["filtered_days"], 0)
        self.assertTrue(all(r["dte"] in (4, 5, 6) for r in d["dashboard"]["dte"]))

    def test_cancel(self):
        st, job = self.api.call("POST", "/api/runs", self.payload())
        self.api.call("POST", f"/api/jobs/{job['id']}/cancel", {})
        j = self.api.wait(job)
        self.assertIn(j["status"], ("cancelled", "done"))


if __name__ == "__main__":
    unittest.main()


class ConcurrencyTest(unittest.TestCase):
    def test_preview_during_run_is_consistent(self):
        tmp = tempfile.TemporaryDirectory()
        svc = Service(provider(), RunStore(os.path.join(tmp.name, "r.db")))
        srv = create_server(svc, port=0)
        serve_in_thread(srv)
        api = Api(f"http://127.0.0.1:{srv.server_address[1]}")
        try:
            cfg = api.call("GET", "/api/config")[1]
            pl = {"settings": dict(cfg["settings"], start="2025-01-01", end="2025-02-21"), "strategy": cfg["strategy"]}
            st, job = api.call("POST", "/api/runs", pl)
            other = json.loads(json.dumps(pl))
            other["strategy"]["legs"][0].update(rule="ATM Offset", offset=3)       # different strike window
            st2, pv = api.call("POST", "/api/preview", other)
            self.assertEqual(st2, 200, pv)
            self.assertEqual(pv["legs"][0]["strike"] - round(pv["spot"] / 50) * 50, 150)
            self.assertEqual(api.wait(job)["status"], "done")
        finally:
            srv.shutdown()
            tmp.cleanup()
