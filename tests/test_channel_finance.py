"""直接运行：python tests/test_channel_finance.py。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from consumption import compute, render_consumption
from rates import parse_rate, save_channel_finance, save_rate
from source import SourceDB
from store import Store


@contextmanager
def source_connection(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class ChannelFinanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.db")
        self.store = Store(self.path)
        self.addCleanup(self.store._conn.close)
        self.source_path = os.path.join(self.tmp.name, "source.db")
        with source_connection(self.source_path) as conn:
            conn.executescript("""
                CREATE TABLE logs (user_id INTEGER, channel_id INTEGER, quota INTEGER,
                                   created_at INTEGER, type INTEGER);
                CREATE TABLE channels (id INTEGER, name TEXT, type TEXT, models TEXT, status INTEGER);
                CREATE TABLE users (id INTEGER, username TEXT, display_name TEXT);
                INSERT INTO channels VALUES (1, '源渠道', '', '', 1);
                INSERT INTO users VALUES (1, 'alice', 'Alice');
                INSERT INTO users VALUES (2, 'bob', 'Bob');
            """)
        self.source = SourceDB({"db_type": "sqlite", "db_dsn": "", "sqlite_path": self.source_path})

    def log(self, uid, cid, quota, ts, kind=2):
        with source_connection(self.source_path) as conn:
            conn.execute("INSERT INTO logs VALUES (?, ?, ?, ?, ?)", (uid, cid, quota, ts, kind))

    def config(self, cid=1, rate="10", cost="0.01", ts=100, fee="CNY", currency="CNY"):
        return self.store.upsert_channel_finance(cid, "配置渠道", fee, rate, currency, cost, ts)

    def stats(self, start=0, end=1000):
        return compute(self.store, self.source, start, end, "CNY")

    def test_backfill_first_snapshot_persistence_and_delete_gap(self):
        self.log(1, 1, 3500000, 50)
        self.config(rate="7", cost="0", ts=100)
        first = self.store.first_channel_snapshot(1)
        self.config(rate="14", ts=200)
        self.store.delete_channel_finance(1, 300)
        self.config(rate="28", ts=400)
        history = self.store.list_channel_finance_history()
        with source_connection(self.source_path) as conn:
            before = list(conn.iterdump())
        self.assertEqual(self.source.first_channel_consumption(1, 100), 50)
        self.assertTrue(self.store.save_channel_backfill(1, first["id"], 50, 100))
        self.assertFalse(self.store.save_channel_backfill(1, first["id"], 50, 100))
        self.assertEqual(Decimal(self.stats(50, 100)["money"]), Decimal("1"))
        self.assertIsNone(self.store.find_channel_snapshot(1, 49))
        self.assertEqual(self.store.find_channel_snapshot(1, 100)["fee_rate"], "7")
        self.assertEqual(self.store.find_channel_snapshot(1, 200)["fee_rate"], "14")
        self.assertIsNone(self.store.find_channel_snapshot(1, 350))
        self.assertEqual(self.store.list_channel_finance_history(), history)
        other = Store(self.path)
        try:
            self.assertFalse(other.save_channel_backfill(1, first["id"], 50, 100))
            self.assertEqual(other.find_channel_snapshot(1, 50)["fee_rate"], "7")
        finally:
            other._conn.close()
        with source_connection(self.source_path) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_backfill_concurrent_connections_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.config()
        first = self.store.first_channel_snapshot(1)
        other = Store(self.path)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(
                    lambda db: db.save_channel_backfill(1, first["id"], 50, 100),
                    (self.store, other)))
            self.assertEqual(sorted(results), [False, True])
        finally:
            other._conn.close()

    def test_backfill_transaction_rollback(self):
        self.config()
        first = self.store.first_channel_snapshot(1)
        self.store._conn.execute(
            "CREATE TRIGGER reject_backfill BEFORE INSERT ON channel_finance_backfill "
            "BEGIN SELECT RAISE(ABORT, 'reject'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_channel_backfill(1, first["id"], 50, 100)
        self.assertIsNone(self.store.channel_backfill(1))
        self.assertIsNone(self.store.find_channel_snapshot(1, 50))

    def test_channels_all_statuses_compatible_fields_and_errors(self):
        with source_connection(self.source_path) as conn:
            conn.execute("INSERT INTO channels VALUES (2, '禁用', '', '', 2)")
            conn.execute("INSERT INTO channels VALUES (3, '自动禁用', '', '', 3)")
        self.assertEqual([r["status"] for r in self.source.channels()], [1, 2, 3])
        with source_connection(self.source_path) as conn:
            conn.execute("DROP TABLE channels")
            conn.execute("CREATE TABLE channels (id INTEGER, name TEXT, key TEXT)")
            conn.execute("INSERT INTO channels VALUES (1, '旧渠道', 'secret')")
        self.assertEqual(self.source.channels(), [
            {"id": 1, "name": "旧渠道", "type": 0, "models": "", "status": 0}])
        with source_connection(self.source_path) as conn:
            conn.execute("DROP TABLE channels")
        with self.assertRaises(sqlite3.OperationalError):
            self.source.channels()

    def test_snapshot_boundaries_delete_recreate(self):
        self.config(ts=100)
        first = self.store.find_channel_snapshot(1, 100)
        self.config(rate="20", ts=200)
        self.store.delete_channel_finance(1, 300)
        self.config(rate="40", ts=400)
        self.assertIsNone(self.store.find_channel_snapshot(1, 99))
        self.assertEqual(self.store.find_channel_snapshot(1, 199), first)
        self.assertEqual(self.store.find_channel_snapshot(1, 200)["fee_rate"], "20")
        self.assertIsNone(self.store.find_channel_snapshot(1, 300))
        self.assertIsNone(self.store.find_channel_snapshot(1, 399))
        self.assertEqual(self.store.find_channel_snapshot(1, 400)["fee_rate"], "40")
        self.assertEqual(len(self.store.list_channel_finance()), 1)
        history = self.store.list_channel_finance_history(1)
        self.assertEqual([row["deleted"] for row in history], [0, 1, 0, 0])
        with self.assertRaises(ValueError):
            self.config(ts=399)
        self.assertEqual(len(self.store.list_channel_finance_history()), 4)

    def test_transactions_rollback_history_and_current(self):
        self.config()
        self.store._conn.execute("CREATE TRIGGER reject_current BEFORE UPDATE ON channel_finance "
                                 "BEGIN SELECT RAISE(ABORT, 'reject'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.config(rate="20", ts=200)
        self.assertEqual(len(self.store.list_channel_finance_history()), 1)
        self.assertEqual(self.store.list_channel_finance()[0]["fee_rate"], "10")

    def test_default_now_does_not_backfill(self):
        self.log(1, 1, 100, 99)
        with patch("store.time.time", return_value=100):
            self.store.upsert_channel_finance(1, "channel", "CNY", "10", "CNY", "0.01")
        self.assertIsNone(self.store.find_channel_snapshot(1, 99))
        result = self.stats()
        self.assertFalse(result["has_rate"])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["profit"])
        self.assertIsNone(result["channels"][0]["cost"])

    def test_global_rates_and_legacy_cost_only_effective_from_update(self):
        with patch("store.time.time", return_value=100):
            save_rate(self.store, "CNY", "10")
            self.store.set_channel_cost(1, "0.01")
        with patch("store.time.time", return_value=200):
            save_rate(self.store, "CNY", "20")
        self.assertEqual(self.store.find_exchange_rate("CNY", 99), "")
        self.assertEqual(self.store.find_exchange_rate("CNY", 199), "10")
        self.assertEqual(self.store.find_exchange_rate("CNY", 200), "20")
        for ts in (99, 100, 200):
            self.log(1, 1, 100, ts)
        known = self.stats(100, 201)
        self.assertEqual((known["website_units"], known["user_money"], known["upstream_cost"], known["profit"]),
                         ("0.0004", None, "2", None))
        self.assertFalse(known["income_complete"])
        self.assertFalse(known["complete"])
        self.assertIsNone(self.stats()["profit"])
        self.config(ts=300)
        self.assertEqual(self.stats(100, 201), known)

    def test_time_aggregation_read_only_and_window(self):
        self.config(ts=100)
        self.config(rate="20", ts=200)
        self.log(1, 1, 30, 100)
        self.log(1, 1, 70, 100)
        self.log(1, 1, 100, 200)
        self.log(1, 1, 999, 201)
        self.log(1, 1, 999, 150, kind=1)
        with source_connection(self.source_path) as conn:
            before = list(conn.iterdump())
        rows = self.source.consumption(100, 201)
        self.assertEqual([(r["created_at"], r["quota"], r["cnt"]) for r in rows],
                         [(100, 100, 2), (200, 100, 1)])
        result = self.stats(100, 201)
        self.assertEqual((result["quota"], result["website_units"], result["user_money"], result["profit"]),
                         (200, "0.0004", "0.00003", "-1.99997"))
        with source_connection(self.source_path) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_ranking_by_income_not_quota_and_uid_tie(self):
        self.config(1, rate="100", cost="0")
        self.config(2, rate="1", cost="0")
        self.log(1, 1, 100, 100)
        self.log(2, 2, 10, 100)
        self.log(3, 2, 10, 100)
        for uid in range(4, 9):
            self.log(uid, 2, 1, 100)
        result = self.stats()
        self.assertEqual([u["user_id"] for u in result["top_users"]], [2, 3, 1, 4, 5])
        self.assertEqual(result["top_users"][0]["channels"][0]["money"], "0.00002")
        self.assertTrue(result["complete"])
        _, text = render_consumption(result)
        self.assertIn("折算消费利润", text)
        self.assertIn("用户消费前五名", text)
        self.assertNotIn("用户#6", text)

    def test_ranking_uses_unrounded_money(self):
        self.config(1, rate="100000000", cost="0")
        self.log(1, 1, 1, 100)
        self.log(2, 1, 2, 100)
        result = self.stats()
        self.assertEqual([u["money"] for u in result["top_users"]], ["0", "0"])
        self.assertEqual([u["user_id"] for u in result["top_users"]], [2, 1])

    def test_currencies_and_missing_cost_are_not_real_zero(self):
        self.config(1, rate="10", cost="0.02", currency="USD")
        self.config(2, rate="10", cost=None)
        self.config(3, rate="10", cost="0.01", fee="USD", currency="USD")
        for cid in (1, 2, 3):
            self.log(cid, cid, 100, 100)
        result = self.stats()
        self.assertEqual(result["user_money"], "0.00004")
        self.assertFalse(result["income_complete"])
        self.assertIsNone(result["upstream_cost"])
        self.assertEqual(result["money_by_currency"], {"CNY": "0.00004"})
        self.assertEqual(result["cost_by_currency"], {"USD": "3"})
        self.assertFalse(result["complete"])
        self.assertIsNone(result["profit"])
        channels = {c["channel_id"]: c for c in result["channels"]}
        self.assertEqual(channels[1]["cost"], "2")
        self.assertIsNone(channels[2]["cost"])
        self.assertFalse(channels[2]["cost_configured"])
        _, message = render_consumption(result)
        self.assertIn("成本未配置", message)
        self.assertIn("不会被汇兑", message)

    def test_delete_gap_does_not_revive_legacy_cost(self):
        with patch("store.time.time", return_value=50):
            self.store.set_exchange_rate("CNY", "10")
            self.store.set_channel_cost(1, "0.01")
        self.config(ts=100)
        self.store.delete_channel_finance(1, 200)
        self.log(1, 1, 100, 200)
        self.assertIsNone(self.stats()["channels"][0]["cost"])
        self.assertIsNone(self.stats()["user_money"])
        self.assertEqual(self.stats()["website_units"], "0.0002")

    def test_website_units_and_channel_ratios(self):
        self.config(1, rate="7", cost="0.0000001")
        self.log(1, 1, 3500000, 100)
        result = self.stats()
        for item in (result, result["channels"][0], result["users"][0],
                     result["users"][0]["channels"][0]):
            self.assertEqual(item["quota"], 3500000)
            self.assertEqual(item["website_units"], "7")
            self.assertEqual(item["money"], "1")
            self.assertEqual(item["cost"], "0.35")
        self.config(2, rate="14", cost="0")
        self.log(2, 2, 3500000, 100)
        result = self.stats()
        self.assertEqual([(u["user_id"], u["money"]) for u in result["users"]],
                         [(1, "1"), (2, "0.5")])
        self.assertEqual(result["website_units"], "14")
        self.assertEqual(result["money"], "1.5")

    def test_missing_ratio_keeps_units_and_partial_amount(self):
        self.config(rate="7", cost="0")
        self.log(1, 1, 3500000, 100)
        self.log(1, 2, 3500000, 100)
        result = self.stats()
        self.assertEqual(result["website_units"], "14")
        self.assertEqual(result["money"], "1")
        self.assertFalse(result["income_complete"])
        self.assertIsNone(result["profit"])
        missing = result["users"][0]["channels"][1]
        self.assertEqual(missing["website_units"], "7")
        self.assertIsNone(missing["money"])
        self.assertIn("已知部分，不完整", render_consumption(result)[1])

    def test_invalid_historical_cost_is_unknown(self):
        self.config()
        snapshot = self.store.find_channel_snapshot(1, 100)
        snapshot["cost_per_quota"] = "NaN"
        patcher = patch.object(self.store, "find_channel_snapshot", return_value=snapshot)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log(1, 1, 3500000, 100)
        result = self.stats()
        self.assertIsNone(result["cost"])
        self.assertIsNone(result["profit"])

    def test_foreign_cost_warning_survives_later_cny_row(self):
        self.config(1, currency="USD")
        self.config(2)
        self.log(1, 1, 100, 100)
        self.log(1, 2, 100, 101)
        result = self.stats()
        self.assertFalse(result["complete"])
        self.assertIsNone(result["profit"])
        self.assertIsNone(result["users"][0]["profit"])

    def test_validation_and_normalization(self):
        for bad in ("abc:7", ":7", "NaN:7", "Infinity:7", "1:NaN", "sNaN", "1:Infinity", "1:2:3", "0:7"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_rate(bad)
        self.assertEqual(parse_rate("2：14"), Decimal("7"))
        body = {"channel_id": 1, "fee_rate": "1:7", "fee_currency": " cny ",
                "cost_currency": " usd ", "cost_per_quota": None, "effective_at": 1}
        with patch("store.time.time", return_value=200):
            saved = save_channel_finance(self.store, body)
        self.assertEqual((saved["fee_currency"], saved["cost_currency"], saved["updated_at"]), ("CNY", "USD", 200))
        for cid in (True, 0, -1, 1.5, "1.0", None):
            with self.subTest(cid=cid), self.assertRaises(ValueError):
                save_channel_finance(self.store, {**body, "channel_id": cid})
        for cost in ("NaN", "Infinity", "-1"):
            with self.subTest(cost=cost), self.assertRaises(ValueError):
                save_channel_finance(self.store, {**body, "cost_per_quota": cost})

    def test_legacy_rate_migration_is_not_retroactive(self):
        path = os.path.join(self.tmp.name, "legacy.db")
        with source_connection(path) as conn:
            conn.execute("CREATE TABLE exchange_rates (currency TEXT PRIMARY KEY, "
                         "rate TEXT NOT NULL, updated_at INTEGER NOT NULL)")
            conn.execute("INSERT INTO exchange_rates VALUES ('CNY', '7', 100)")
        legacy = Store(path)
        self.addCleanup(legacy._conn.close)
        self.assertEqual(legacy.find_exchange_rate("CNY", 99), "")
        self.assertEqual(legacy.find_exchange_rate("CNY", 100), "7")
        with patch("store.time.time", return_value=200):
            save_rate(legacy, "CNY", "14")
        self.assertEqual(legacy.find_exchange_rate("CNY", 199), "7")
        self.assertEqual(legacy.find_exchange_rate("CNY", 200), "14")

    def test_numeric_zero_cost_is_configured(self):
        save_channel_finance(self.store, {"channel_id": 1, "fee_rate": "10", "cost_per_quota": 0})
        self.assertEqual(self.store.list_channel_finance()[0]["cost_per_quota"], "0")

    def test_daily_claim_persistence_and_no_retry_after_failure(self):
        self.config()
        self.assertTrue(self.store.claim_daily_report("2026-10-18"))
        other = Store(self.path)
        self.addCleanup(other._conn.close)
        self.assertFalse(other.claim_daily_report("2026-10-18"))
        self.assertEqual(other.daily_report_runs()[0]["status"], "sending")
        self.store.finish_daily_report("2026-10-18", False, "uncertain")
        self.assertFalse(other.claim_daily_report("2026-10-18"))
        self.assertEqual(other.daily_report_runs()[0]["status"], "failed")
        self.assertEqual(other.daily_report_runs()[0]["detail"], "uncertain")
        self.assertTrue(other.claim_daily_report("2026-10-19"))
        other.finish_daily_report("2026-10-19", True, "sent")
        self.assertEqual(other.daily_report_runs(1)[0]["status"], "success")
        self.assertEqual(other.find_channel_snapshot(1, 100)["fee_rate"], "10")


if __name__ == "__main__":
    unittest.main(verbosity=2)
