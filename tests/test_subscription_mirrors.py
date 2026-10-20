"""订阅记账镜像回归：python tests/test_subscription_mirrors.py。"""

from contextlib import closing
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_smoke as smoke


class SubscriptionMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), smoke.FakeDingTalk)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.webhook = f"http://127.0.0.1:{cls.server.server_address[1]}/robot/send"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="subscription-mirror-test-")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "source.db")
        smoke.build_source_db(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM top_ups")
            conn.execute("DELETE FROM subscription_orders")
        self.now = int(time.time())
        self.start = self.now - 600
        self.end = self.now + 1
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        smoke.RECEIVED.clear()
        self.store = smoke.store_module.Store(os.path.join(self.tmp.name, "notifier.db"))
        self.addCleanup(self.store._conn.close)
        self.store.set_configs({
            "db_type": "sqlite", "sqlite_path": self.db_path,
            "dingtalk_webhook": self.webhook, "enabled": True,
            "backfill": False, "expire_minutes": 5,
        })
        self.source = smoke.source_module.SourceDB(
            smoke.config_module.effective_config(self.store))
        self.poller = smoke.poller_module.Poller(self.store)

    def subscription(self, trade, status="success", created=None):
        smoke.add_subscription_order(self.db_path, 1, 3, trade, status, "epay")
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE subscription_orders SET create_time = ? WHERE id = "
                         "(SELECT MAX(id) FROM subscription_orders)",
                         (self.now - 20 if created is None else created,))

    def wallet(self, trade, status="success", created=None, amount=0, money=199):
        smoke.add_wallet_order(self.db_path, 1, trade, status, "epay", money=money,
                               amount=amount,
                               created=self.now - 10 if created is None else created)

    def snapshot(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            return list(conn.iterdump())

    def query_rows(self):
        return {
            "orders": self.source.orders(self.start),
            "success_orders": self.source.success_orders(self.start, self.end),
            "window_orders": self.source.window_orders(self.start, self.end,
                                                        buckets=("success",)),
        }

    def test_poll_backfill_and_wallet_keep_subscription_authoritative(self):
        self.assertEqual(self.poller.run_once(), 0)
        self.subscription("SUB-MIRROR")
        self.wallet("SUB-MIRROR")
        before = self.snapshot()
        self.assertEqual(self.poller.run_once(), 1)
        self.assertEqual(len(smoke.RECEIVED), 1)
        message = smoke.RECEIVED[0]["markdown"]["text"]
        self.assertIn("订阅（专业版）", message)
        self.assertNotIn("钱包充值", message)
        self.assertEqual(self.poller.run_once(), 0)
        self.assertEqual(len(smoke.RECEIVED), 1)
        self.assertEqual([(item["kind"], item["trade_no"])
                          for item in self.store.history(20)],
                         [("subscription", "SUB-MIRROR")])
        self.assertEqual(self.poller.backfill(1), 1)
        self.assertEqual(len(smoke.RECEIVED), 2)
        self.assertIn("补发推送", smoke.RECEIVED[-1]["markdown"]["text"])
        self.assertIn("订阅（专业版）", smoke.RECEIVED[-1]["markdown"]["text"])
        self.assertEqual(self.poller.run_once(), 0)
        self.assertEqual(self.snapshot(), before)

        self.wallet("WALLET-SAME-MONEY", amount=0)
        self.wallet("WALLET-NORMAL", amount=500000, money=99)
        before = self.snapshot()
        self.assertEqual(self.poller.run_once(), 2)
        self.assertEqual(len(smoke.RECEIVED), 4)
        self.assertTrue(all("钱包充值" in item["markdown"]["text"]
                            for item in smoke.RECEIVED[-2:]))
        self.assertEqual(self.poller.run_once(), 0)
        self.assertEqual(len(smoke.RECEIVED), 4)
        self.assertEqual(self.snapshot(), before)

    def test_all_queries_agree_on_types_totals_and_limit(self):
        self.subscription("SUB-MIRROR")
        # 镜像最新，必须在 LIMIT 之前排除，且不能凭 amount 判断。
        self.wallet("SUB-MIRROR", created=self.now, amount=123)
        self.wallet("WALLET-SAME-MONEY", created=self.now - 30)
        self.wallet("WALLET-NORMAL", created=self.now - 40, amount=500000, money=99)
        before = self.snapshot()
        expected = [("subscription", "SUB-MIRROR"),
                    ("wallet", "WALLET-SAME-MONEY"), ("wallet", "WALLET-NORMAL")]
        for name, rows in self.query_rows().items():
            with self.subTest(query=name):
                self.assertEqual([(r["kind"], r["trade_no"]) for r in rows], expected)
                self.assertEqual(sum(r["money"] for r in rows), 497)
        self.assertEqual(self.source.daily_recharge(self.start, self.end),
                         [{"user_id": 1, "money": 497.0, "orders": 3}])
        for limit in (1, 2, 3, 4):
            for name, rows in (
                    ("orders", self.source.orders(self.start, limit=limit)),
                    ("window_orders", self.source.window_orders(
                        self.start, self.end, limit=limit, buckets=("success",)))):
                with self.subTest(query=name, limit=limit):
                    self.assertEqual([(r["kind"], r["trade_no"]) for r in rows],
                                     expected[:limit])
        self.assertEqual(self.snapshot(), before)

    def test_subscription_outside_window_or_status_still_excludes_mirror(self):
        self.subscription("OLD-SUB", created=self.start - 1)
        self.subscription("FUTURE-SUB", created=self.end)
        self.subscription("PENDING-SUB", status="pending")
        self.subscription("FAILED-SUB", status="failed")
        for trade in ("OLD-SUB", "FUTURE-SUB", "PENDING-SUB", "FAILED-SUB"):
            self.wallet(trade)
        self.wallet("REAL-WALLET", money=50)
        before = self.snapshot()
        for name, rows in self.query_rows().items():
            with self.subTest(query=name):
                wallets = [r["trade_no"] for r in rows if r["kind"] == "wallet"]
                self.assertEqual(wallets, ["REAL-WALLET"])
        self.assertEqual(self.source.daily_recharge(self.start, self.end),
                         [{"user_id": 1, "money": 50.0, "orders": 1}])
        self.assertEqual(self.snapshot(), before)

    def test_mirror_exclusion_applies_to_failed_and_derived_pending(self):
        for status in ("failed", "expired", "pending"):
            trade = "MIRROR-" + status
            self.subscription(trade, status=status, created=self.now - 400)
            self.wallet(trade, status=status, created=self.now - 400)
            self.wallet("REAL-" + status, status=status, created=self.now - 400)
        self.subscription("FRESH", status="pending")
        self.wallet("FRESH", status="pending")
        before = self.snapshot()
        rows = self.source.orders(self.start, expired_before=self.now - 300)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(not r["trade_no"].startswith("MIRROR-")
                            for r in rows if r["kind"] == "wallet"))
        derived = [r for r in rows if r["trade_no"].endswith("pending")]
        self.assertEqual(len(derived), 2)
        self.assertTrue(all(r["status"] == "expired" and r["expired_derived"]
                            for r in derived))
        rows = self.source.window_orders(self.start, self.end, buckets=("failed",),
                                         expired_before=self.now - 300)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["trade_no"].startswith("REAL-")
                            for r in rows if r["kind"] == "wallet"))
        rows = self.source.window_orders(self.start, self.end, buckets=("pending",),
                                         expired_before=self.now - 300)
        self.assertEqual([(r["kind"], r["trade_no"]) for r in rows],
                         [("subscription", "FRESH")])
        self.assertEqual(self.snapshot(), before)

    def test_empty_and_null_trade_numbers_are_not_mirrors(self):
        for trade in ("", None):
            self.subscription(trade)
            self.wallet(trade)
        before = self.snapshot()
        for name, rows in self.query_rows().items():
            with self.subTest(query=name):
                self.assertEqual(len(rows), 4)
                self.assertEqual(sum(r["kind"] == "wallet" for r in rows), 2)
                self.assertEqual(sum(r["money"] for r in rows), 796)
        self.assertEqual(self.source.daily_recharge(self.start, self.end),
                         [{"user_id": 1, "money": 796.0, "orders": 4}])
        self.assertEqual(self.snapshot(), before)

    def test_first_run_registers_or_backfills_only_subscription(self):
        self.subscription("INITIAL-SUB")
        self.wallet("INITIAL-SUB")
        before = self.snapshot()
        self.assertEqual(self.poller.run_once(), 0)
        self.assertEqual(self.store.pushed_count(), 1)
        self.assertEqual(smoke.RECEIVED, [])
        second_store = smoke.store_module.Store(os.path.join(self.tmp.name, "backfill.db"))
        self.addCleanup(second_store._conn.close)
        second_store.set_configs({**self.store.config_map(), "backfill": True})
        second_poller = smoke.poller_module.Poller(second_store)
        self.assertEqual(second_poller.run_once(), 1)
        self.assertEqual(len(smoke.RECEIVED), 1)
        self.assertIn("订阅（专业版）", smoke.RECEIVED[0]["markdown"]["text"])
        self.assertIn("补发推送", smoke.RECEIVED[0]["markdown"]["text"])
        self.assertEqual(second_poller.run_once(), 0)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
