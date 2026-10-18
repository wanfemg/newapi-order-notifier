"""双比例、历史快照和显式全量重算测试。"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
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
        self.store = self.open_store()
        self.source_path = os.path.join(self.tmp.name, "source.db")
        with source_connection(self.source_path) as conn:
            conn.executescript("""
                CREATE TABLE logs (user_id INTEGER, channel_id INTEGER, quota INTEGER,
                                   created_at INTEGER, type INTEGER);
                CREATE TABLE channels (id INTEGER, name TEXT, type TEXT, models TEXT, status INTEGER);
                CREATE TABLE users (id INTEGER, username TEXT, display_name TEXT);
                INSERT INTO channels VALUES (1, '源渠道', '', '', 1);
                INSERT INTO users VALUES (1, 'alice', 'Alice'), (2, 'bob', 'Bob');
            """)
        self.source = SourceDB({"db_type": "sqlite", "db_dsn": "", "sqlite_path": self.source_path})

    def open_store(self):
        db = Store(self.path)
        self.addCleanup(db._conn.close)
        return db

    def log(self, uid, cid, quota, ts, kind=2):
        with source_connection(self.source_path) as conn:
            conn.execute("INSERT INTO logs VALUES (?, ?, ?, ?, ?)", (uid, cid, quota, ts, kind))

    def config(self, cid=1, rate="14", ts=100, fee="CNY", cost="999"):
        return self.store.upsert_channel_finance(cid, "配置渠道", fee, rate, "USD", cost, ts)

    def stats(self, start=0, end=1000):
        return compute(self.store, self.source, start, end, "USD")

    def preview(self, end=500):
        channel = self.store.current_channel_snapshot(1)
        site = self.store.find_site_rate()
        return self.store.save_finance_preview({
            "channel_id": 1, "channel_snapshot_id": channel["id"],
            "site_snapshot_id": site["id"], "site_rate": site["rate"],
            "channel_rate": channel["fee_rate"], "start": 0, "end": end})

    def test_dual_ratio_all_levels_and_no_double_cost(self):
        self.store.save_site_rate("7", effective_at=100)
        self.config()
        self.log(1, 1, 3500000, 100)
        result = self.stats()
        for item in (result, result["channels"][0], result["users"][0], result["users"][0]["channels"][0]):
            self.assertEqual((item["quota"], item["website_units"], item["money"], item["cost"], item["profit"]),
                             (3500000, "7", "1", "0.5", "0.5"))
            self.assertTrue(item["complete"])
        self.assertEqual(result["currency"], "CNY")
        text = render_consumption(result)[1]
        for label in ("站点折算收入", "渠道折算成本", "折算利润", "不代表实收现金"):
            self.assertIn(label, text)

    def test_missing_each_ratio_is_independently_unknown(self):
        self.log(1, 1, 3500000, 100)
        self.config()
        self.assertIsNone(self.stats()["money"])
        self.assertEqual(self.stats()["cost"], "0.5")
        self.store.save_site_rate("7", effective_at=100)
        self.log(2, 2, 3500000, 100)
        result = self.stats()
        self.assertEqual(result["money"], "2")
        self.assertIsNone(result["cost"])
        self.assertIsNone(result["profit"])
        self.assertEqual(result["website_units"], "14")
        self.assertIsNone(result["users"][1]["channels"][0]["cost"])

    def test_partial_income_is_unknown_and_ranks_last(self):
        self.store.save_site_rate("7", effective_at=100)
        self.config()
        self.log(1, 1, 3500000, 99)
        self.log(1, 1, 3500000, 100)
        self.log(2, 1, 1, 100)
        result = self.stats()
        self.assertIsNone(result["money"])
        self.assertFalse(result["income_complete"])
        self.assertEqual([u["user_id"] for u in result["users"]], [2, 1])
        self.assertIn("未知", render_consumption(result)[1])

    def test_ranking_uses_site_income_not_channel_ratio_or_rounded_value(self):
        self.store.save_site_rate("100000000", effective_at=100)
        self.config(1, "1")
        self.config(2, "1000")
        self.log(1, 1, 1, 100)
        self.log(2, 2, 2, 100)
        self.log(3, 1, 2, 100)
        result = self.stats()
        self.assertEqual([u["user_id"] for u in result["top_users"]], [2, 3, 1])
        self.assertEqual([u["money"] for u in result["users"]], ["0", "0", "0"])

    def test_site_and_channel_history_boundaries_delete_recreate(self):
        self.store.save_site_rate("7", effective_at=100)
        self.store.save_site_rate("14", effective_at=200)
        self.config(rate="7")
        self.config(rate="14", ts=200)
        self.store.delete_channel_finance(1, 300)
        self.config(rate="28", ts=400)
        self.assertIsNone(self.store.find_site_rate(99))
        self.assertEqual(self.store.find_site_rate(200)["rate"], "14")
        self.assertEqual(self.store.find_channel_snapshot(1, 199)["fee_rate"], "7")
        self.assertIsNone(self.store.find_channel_snapshot(1, 350))
        self.assertEqual(self.store.find_channel_snapshot(1, 400)["fee_rate"], "28")
        with self.assertRaises(ValueError):
            self.config(ts=399)
        self.log(1, 1, 3500000, 199)
        self.log(1, 1, 3500000, 200)
        self.assertEqual((self.stats(100, 201)["money"], self.stats(100, 201)["cost"]), ("1.5", "1.5"))

    def test_full_override_old_snapshots_gaps_freezes_both_ratios_and_persists(self):
        self.store.save_site_rate("2", effective_at=100)
        self.config(rate="4")
        self.store.delete_channel_finance(1, 200)
        self.config(rate="14", ts=400)
        self.store.save_site_rate("7", effective_at=400)
        for ts in (50, 100, 250, 450, 500):
            self.log(1, 1, 3500000, ts)
        history = self.store.list_channel_finance_history()
        with source_connection(self.source_path) as conn:
            before = list(conn.iterdump())
        token = self.preview()
        self.store.confirm_finance_preview(token, 1)
        self.assertEqual((self.stats(0, 500)["money"], self.stats(0, 500)["cost"]), ("4", "2"))
        self.assertEqual(self.store.list_channel_finance_history(), history)
        self.store.save_site_rate("14", effective_at=600)
        self.config(rate="28", ts=600)
        self.assertEqual(self.stats(0, 500)["profit"], "2")
        self.assertIsNone(self.store.finance_override(1, 500))
        self.assertEqual(self.open_store().finance_override(1, 50)["site_rate"], "7")
        with self.assertRaises(ValueError):
            self.store.confirm_finance_preview(token, 1)
        self.store.confirm_finance_preview(self.preview(), 1)
        self.assertEqual(self.stats(0, 500)["money"], "2")
        self.assertEqual(len(self.store.finance_overrides()), 2)
        with source_connection(self.source_path) as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_preview_rejects_changed_site_channel_expiry_and_wrong_channel(self):
        self.config()
        self.store.save_site_rate("7", effective_at=100)
        token = self.preview()
        with self.assertRaises(ValueError):
            self.store.confirm_finance_preview(token, 2)
        self.store.save_site_rate("8", effective_at=200)
        with self.assertRaises(ValueError):
            self.store.confirm_finance_preview(token, 1)
        token = self.preview()
        self.config(rate="15", ts=200)
        with self.assertRaises(ValueError):
            self.store.confirm_finance_preview(token, 1)
        with patch("store.time.time", return_value=1000):
            token = self.preview()
        with patch("store.time.time", return_value=1601), self.assertRaises(ValueError):
            self.store.confirm_finance_preview(token, 1)
        self.assertEqual(self.store.finance_overrides(), [])

    def test_preview_concurrent_confirmation_once_and_transaction_rollback(self):
        self.config()
        self.store.save_site_rate("7", effective_at=100)
        token = self.preview()
        other = self.open_store()
        def confirm(db):
            try:
                db.confirm_finance_preview(token, 1)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(confirm, (self.store, other))), [False, True])
        token = self.preview()
        self.store._conn.execute("CREATE TRIGGER reject_override BEFORE INSERT ON finance_overrides "
                                 "BEGIN SELECT RAISE(ABORT, 'reject'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.confirm_finance_preview(token, 1)
        self.assertEqual(len(self.store.finance_overrides()), 1)

    def test_legacy_data_retained_but_never_used_as_new_rates_or_costs(self):
        with patch("store.time.time", return_value=50):
            save_rate(self.store, "CNY", "7")
            self.store.set_channel_cost(1, "99")
        self.log(1, 1, 3500000, 100)
        self.assertIsNone(self.stats()["money"])
        self.assertIsNone(self.stats()["cost"])
        self.assertIsNone(self.store.find_site_rate())
        self.store.save_site_rate("7", effective_at=100)
        self.config()
        with self.store._conn:
            self.store._conn.execute("UPDATE channel_finance_history SET cost_per_quota = 'NaN'")
        self.assertEqual(self.stats()["cost"], "0.5")
        self.assertEqual(self.store.find_exchange_rate("CNY", 100), "7")
        self.assertTrue(self.store.channel_costs())

    def test_source_aggregation_window_channels_and_read_only(self):
        self.log(1, 1, 30, 100)
        self.log(1, 1, 70, 100)
        self.log(1, 1, 999, 101)
        self.log(1, 1, 999, 100, kind=1)
        rows = self.source.consumption(100, 101)
        self.assertEqual([(r["quota"], r["cnt"]) for r in rows], [(100, 2)])
        with source_connection(self.source_path) as conn:
            conn.execute("INSERT INTO channels VALUES (2, '禁用', '', '', 2), (3, '自动禁用', '', '', 3)")
        self.assertEqual([c["status"] for c in self.source.channels()], [1, 2, 3])
        conn = self.source._connect()
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM logs")
        finally:
            conn.close()

    def test_validation_normalization_and_ignored_legacy_cost(self):
        for bad in ("abc:7", "NaN", "1:Infinity", "0:7", "1:0", "-1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_rate(bad)
        self.assertEqual(parse_rate("2：14"), Decimal("7"))
        body = {"channel_id": 1, "fee_currency": " cny ", "fee_rate": "1:7", "cost_per_quota": "NaN"}
        with patch("store.time.time", return_value=200):
            saved = save_channel_finance(self.store, body)
        self.assertEqual((saved["fee_currency"], saved["cost_currency"], saved["cost_per_quota"]), ("CNY", "CNY", None))
        for cid in (True, 0, -1, 1.5, "1.0", None):
            with self.subTest(cid=cid), self.assertRaises(ValueError):
                save_channel_finance(self.store, {**body, "channel_id": cid})
        with self.assertRaises(ValueError):
            save_channel_finance(self.store, {**body, "fee_currency": "USD"})

    def test_daily_claim_persistence_and_no_retry_after_failure(self):
        self.assertTrue(self.store.claim_daily_report("2026-10-18"))
        other = self.open_store()
        self.assertFalse(other.claim_daily_report("2026-10-18"))
        self.store.finish_daily_report("2026-10-18", False, "uncertain")
        self.assertFalse(other.claim_daily_report("2026-10-18"))
        self.assertEqual(other.daily_report_runs()[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
