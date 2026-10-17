"""财务 API 的真实 HTTP 集成测试：python tests/test_finance_api.py。"""

from contextlib import closing
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

import config
import consumption
import source
import store
import webui


class FinanceAPITest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.source_path = os.path.join(self.tmp.name, "source.db")
        self.now = int(time.time())
        with closing(sqlite3.connect(self.source_path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT);
                INSERT INTO users VALUES (1, 'recharge', '充值用户'), (2, 'consume', '消费用户');
                CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT, type TEXT,
                    models TEXT, status INTEGER);
                INSERT INTO channels VALUES (1, '测试渠道', 'openai', 'gpt', 1);
                CREATE TABLE logs (id INTEGER PRIMARY KEY, user_id INTEGER,
                    channel_id INTEGER, type INTEGER, quota INTEGER, created_at INTEGER);
                CREATE TABLE top_ups (user_id INTEGER, money REAL, payment_method TEXT,
                    payment_provider TEXT, create_time INTEGER, trade_no TEXT, status TEXT);
                CREATE TABLE subscription_orders (user_id INTEGER, money REAL,
                    payment_method TEXT, payment_provider TEXT, create_time INTEGER,
                    trade_no TEXT, status TEXT);
            """)
            conn.execute("INSERT INTO top_ups VALUES (1, 20, 'alipay', 'epay', ?, 'T1', 'success')",
                         (self.now,))
            conn.execute("INSERT INTO logs VALUES (1, 2, 1, 2, 70, ?)", (self.now,))
        self.store = store.Store(os.path.join(self.tmp.name, "state.db"))
        self.store.set_configs({"db_type": "sqlite", "sqlite_path": self.source_path})
        self.store.set_admin_hash(webui.hash_password("finance-test-password"))
        self.server = webui.build_server(self.store, None, None, None, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookie = ""
        status, payload, headers = self.call(
            "/api/login", {"password": "finance-test-password"}, auth=False)
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store._conn.close()
        self.tmp.cleanup()
        self.env.stop()

    def call(self, path, body=None, auth=True, csrf=True, raw=None, content_type="application/json"):
        headers = {}
        if auth:
            headers["Cookie"] = self.cookie
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            headers["Content-Type"] = content_type
            if csrf:
                headers[webui.CSRF_HEADER] = webui.CSRF_VALUE
        request = urllib.request.Request(self.base + path, data=data, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read().decode()), response.headers

    def save(self, **overrides):
        body = {"channel_id": 1, "channel_name": "测试渠道", "fee_currency": "CNY",
                "fee_rate": "1:7", "cost_currency": "CNY", "cost_per_quota": "0.01"}
        body.update(overrides)
        return self.call("/api/channel-finance", body)

    def test_explicit_ranges_and_validation(self):
        paths = ("/api/finance/summary", "/api/finance/trend", "/api/finance/ranking",
                 "/api/finance/users", "/api/orders", "/api/orders/export.csv", "/api/consumption")
        for path in paths:
            for query in ("start=1", "end=2", "start=&end=2", "start=0&end=2",
                          "start=2&end=2", "start=3&end=2", "start=1.5&end=2",
                          "start=1&start=2&end=3", "start=-1&end=2"):
                with self.subTest(path=path, query=query):
                    self.assertEqual(self.call(path + "?" + query)[0], 400)
        query = f"?days=1&start={self.now}&end={self.now + 1}"
        self.assertEqual(self.call(paths[0] + query)[1]["total"]["money"], 20)
        excluded = f"?start={self.now - 1}&end={self.now}"
        self.assertEqual(self.call(paths[0] + excluded)[1]["total"]["orders"], 0)
        self.assertEqual(len(self.call(paths[1] + query)[1]["points"]), 1)
        self.assertEqual(len(self.call(paths[2] + query)[1]["items"]), 1)
        self.assertEqual(len(self.call(paths[3] + query)[1]["consumption_users"]), 1)
        self.assertEqual(self.call(paths[-1] + query)[1]["start_ts"], self.now)
        for path in ("/api/orders", "/api/orders/export.csv"):
            with patch.object(source.SourceDB, "window_orders", return_value=[]) as fetch:
                if path.endswith("csv"):
                    request = urllib.request.Request(self.base + path + query,
                                                     headers={"Cookie": self.cookie})
                    with urllib.request.urlopen(request) as response:
                        self.assertTrue(response.read().startswith(b"\xef\xbb\xbf"))
                else:
                    self.assertEqual(self.call(path + query)[0], 200)
                self.assertEqual(fetch.call_args.args[:2], (self.now, self.now + 1))

    def test_historical_backfill_contract_and_security(self):
        path = "/api/channel-finance/backfill"
        self.assertEqual(self.call(path + "?channel_id=1", auth=False)[0], 401)
        self.assertEqual(self.call(path, {}, csrf=False)[0], 403)
        for value in (1, "true", False, None):
            self.assertEqual(self.call(path, {"channel_id": 1, "confirmed": value})[0], 400)
        self.assertEqual(self.call(path + "?channel_id=1&channel_id=2")[0], 400)
        self.assertFalse(self.call(path + "?channel_id=1")[1]["eligible"])
        self.store.upsert_channel_finance(1, "channel", "CNY", "7", "CNY", "0", self.now + 10)
        preview = self.call(path + "?channel_id=1")[1]
        self.assertIs(preview["eligible"], True)
        self.assertEqual((preview["start"], preview["end"]), (self.now, self.now + 10))
        before = self.store.list_channel_finance_history()
        for _ in range(2):
            status, result, _ = self.call(path, {"channel_id": 1, "confirmed": True})
            self.assertEqual(status, 200)
            self.assertEqual(set(result), {"message"})
        self.assertFalse(self.call(path + "?channel_id=1")[1]["eligible"])
        self.assertEqual(self.store.list_channel_finance_history(), before)
        self.assertTrue(self.call(f"/api/consumption?start={self.now}&end={self.now + 1}")[1]["has_rate"])

    def test_authentication_and_csrf(self):
        for path in ("/api/channel-finance", "/api/channel-finance/history",
                     "/api/finance/users", "/api/reports", "/api/consumption"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, auth=False)[0], 401)
        for path in ("/api/channel-finance", "/api/channel-finance/delete", "/api/config"):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, {}, auth=False)[0], 401)
                self.assertEqual(self.call(path, {}, csrf=False)[0], 403)
                self.assertEqual(self.call(path, {}, content_type="text/plain")[0], 403)
        self.assertEqual(self.store.list_channel_finance_history(), [])

    def test_crud_history_and_source_read_only(self):
        with closing(sqlite3.connect(self.source_path)) as conn:
            before = list(conn.iterdump())
        status, payload, _ = self.call("/api/channel-finance")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["history"], [])
        self.assertEqual(payload["channels"][0]["id"], 1)
        status, saved, _ = self.save(effective_at=1)
        self.assertEqual(status, 200)
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["item"]["fee_rate"], "7")
        self.assertGreaterEqual(saved["item"]["updated_at"], self.now)
        self.assertEqual(self.save(fee_rate="10", cost_per_quota=None)[0], 200)
        self.assertEqual(self.save(channel_id=2)[0], 200)
        payload = self.call("/api/channel-finance")[1]
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(len(payload["history"]), 3)
        history = self.call("/api/channel-finance/history?channel_id=1")[1]["items"]
        self.assertEqual([row["fee_rate"] for row in history], ["10", "7"])
        self.assertTrue(all(row["channel_id"] == 1 for row in history))
        self.assertEqual(self.call("/api/channel-finance/delete", {"channel_id": "1"})[0], 200)
        payload = self.call("/api/channel-finance")[1]
        self.assertEqual([row["channel_id"] for row in payload["items"]], [2])
        history = self.call("/api/channel-finance/history?channel_id=1")[1]["items"]
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0]["deleted"], 1)
        self.assertEqual(history[-1]["fee_rate"], "7")
        with closing(sqlite3.connect(self.source_path)) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_invalid_channel_inputs_are_atomic(self):
        invalid = [None, True, False, 0, -1, 1.2, 1.0, "1.0", "1e2", "+1", " 1", "", [], {}]
        for value in invalid:
            with self.subTest(value=value):
                self.assertEqual(self.save(channel_id=value)[0], 400)
                self.assertEqual(self.call("/api/channel-finance/delete", {"channel_id": value})[0], 400)
        for fields in ({"fee_rate": "0"}, {"fee_rate": "NaN"}, {"fee_rate": "1:Infinity"},
                       {"cost_per_quota": "-1"}, {"cost_per_quota": "Infinity"},
                       {"fee_currency": ""}, {"fee_currency": "USD"},
                       {"fee_currency": "eur"}, {"cost_currency": "x" * 17}):
            with self.subTest(fields=fields):
                self.assertEqual(self.save(**fields)[0], 400)
        for path in ("/api/channel-finance", "/api/channel-finance/delete"):
            for raw in (b"[]", b"null", b"1", b'"text"', b"{", b"{}"):
                with self.subTest(path=path, raw=raw[:20]):
                    self.assertEqual(self.call(path, raw=raw)[0], 400)
        with patch.object(webui, "MAX_BODY", 8):
            self.assertEqual(self.call("/api/channel-finance", {"channel_id": 1})[0], 400)
        for query in ("", "0", "-1", "1.2", "NaN", "true", "%20", "%2B1", "1&channel_id=2"):
            with self.subTest(query=query):
                self.assertEqual(self.call("/api/channel-finance/history?channel_id=" + query)[0], 400)
        self.assertEqual(self.store.list_channel_finance(), [])
        self.assertEqual(self.store.list_channel_finance_history(), [])

    def test_non_cny_rejection_preserves_existing_snapshot(self):
        self.assertEqual(self.save()[0], 200)
        current = self.store.list_channel_finance()
        history = self.store.list_channel_finance_history()
        self.assertEqual(self.save(fee_currency="USD", fee_rate="99")[0], 400)
        self.assertEqual(self.store.list_channel_finance(), current)
        self.assertEqual(self.store.list_channel_finance_history(), history)

    def test_report_config_and_invalid_time(self):
        status, payload, _ = self.call("/api/reports")
        self.assertEqual(status, 200)
        self.assertFalse(payload["consume_daily_report_enabled"])
        self.assertEqual(payload["consume_daily_report_time"], "09:00")
        self.assertEqual(payload["consume_daily_runs"], [])
        for valid in ("00:00", "23:59", "09:05"):
            self.assertEqual(self.call("/api/config", {"config": {
                "consume_daily_report_time": valid, "consume_daily_report_enabled": True}})[0], 200)
            self.assertEqual(self.call("/api/config")[1]["config"]["consume_daily_report_time"], valid)
        before = self.store.config_map()
        for value in ("9:00", "24:00", "12:60", "09:00 ", " 09:00", "09:00:00", "", None, 900, True):
            with self.subTest(value=value):
                status, payload, _ = self.call("/api/config", {
                    "consume_daily_report_time": value, "consume_daily_report_enabled": False})
                self.assertEqual(status, 400)
                self.assertIn("consume_daily_report_time", payload["error"])
                self.assertEqual(self.store.config_map(), before)
        with patch.object(store.time, "time", return_value=1234):
            self.store.claim_daily_report("2026-10-17")
        run = self.call("/api/reports")[1]["consume_daily_runs"][0]
        self.assertEqual(run["status"], "sending")
        self.assertEqual(run["started_at"], 1234)
        self.assertIsNone(run["finished_at"])
        with open(os.path.join(BASE, "web", "app.js"), encoding="utf-8") as script:
            self.assertIn("formatTime(run.finished_at || run.started_at)", script.read())
        self.store.finish_daily_report("2026-10-17", True, "发送成功")
        payload = self.call("/api/reports")[1]
        self.assertTrue(payload["consume_daily_report_enabled"])
        self.assertEqual(payload["consume_daily_report_time"], "09:05")
        self.assertEqual(payload["consume_daily_runs"][0]["status"], "success")
        self.assertIn("daily_last", payload)
        self.assertIn("weekly_last", payload)

    def test_users_preserve_recharge_and_include_consumption_only_user(self):
        self.store.upsert_channel_finance(1, "测试渠道", "CNY", "7", "CNY", "0.01",
                                          effective_at=self.now - 1)
        status, payload, _ = self.call("/api/finance/users")
        self.assertEqual(status, 200)
        self.assertEqual([row["user_id"] for row in payload["items"]], [1])
        self.assertEqual(payload["items"][0]["total"], 20)
        self.assertEqual(payload["currency"], "CNY")
        self.assertEqual(payload["consumption_error"], "")
        user = payload["consumption_users"][0]
        self.assertEqual(user["user_id"], 2)
        self.assertEqual(user["username"], "consume")
        self.assertEqual(user["money"], "0.00002")
        self.assertEqual(user["website_units"], "0.00014")
        self.assertEqual(user["channels"][0]["website_units"], "0.00014")
        self.assertEqual(user["channels"][0]["channel_id"], 1)
        self.assertEqual(user["profit"], "-0.69998")
        self.assertEqual(self.call("/api/finance/users?days=1")[1]["days"], 1)

    def test_missing_logs_does_not_break_recharge(self):
        with closing(sqlite3.connect(self.source_path)) as conn, conn:
            conn.execute("DROP TABLE logs")
        status, payload, _ = self.call("/api/finance/users")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"][0]["total"], 20)
        self.assertEqual(payload["consumption_users"], [])
        self.assertIn("logs", payload["consumption_error"])
        self.assertEqual(self.call("/api/consumption")[0], 500)

    def test_consumption_preserves_all_compute_fields_and_legacy_cost_errors(self):
        self.store.upsert_channel_finance(1, "测试渠道", "CNY", "7", "CNY", "0.01",
                                          effective_at=self.now - 1)
        window = (self.now - 60, self.now + 60)
        expected = consumption.compute(self.store, source.SourceDB(config.effective_config(self.store)),
                                       *window, "CNY")
        with patch.object(webui.finance_module, "range_window", return_value=window):
            status, payload, _ = self.call("/api/consumption")
        self.assertEqual(status, 200)
        self.assertEqual(payload, expected)
        for bad in ("NaN", "Infinity", "-1", "abc", None):
            with self.subTest(bad=bad):
                self.assertEqual(self.call("/api/cost", {"channel_id": 1, "cost_per_unit": bad})[0], 400)
        self.assertEqual(self.call("/api/cost", {
            "channel_id": 1, "cost_per_unit": "1", "cost_currency": "x" * 17})[0], 400)
        self.assertEqual(self.store.channel_costs(), [])
        self.assertEqual(self.call("/api/cost", {"channel_id": 1, "cost_per_unit": "0"})[0], 200)


if __name__ == "__main__":
    unittest.main()
