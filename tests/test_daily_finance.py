"""每日财务调度专项测试：python tests/test_daily_finance.py。"""

import calendar
from contextlib import closing
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

import config
import consume_monitor
from store import Store


def local_ts(year, month, day, hour=0, minute=0, second=0):
    return int(time.mktime((year, month, day, hour, minute, second, 0, 0, -1)))


class DailyFinanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in ("SQL_DSN", "SQLITE_PATH", "POLL_INTERVAL"):
            os.environ.pop(key, None)
        self.path = os.path.join(self.tmp.name, "state.db")
        self.source_path = os.path.join(self.tmp.name, "source.db")
        with closing(sqlite3.connect(self.source_path)) as db:
            db.executescript("""
                CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT, type INTEGER,
                    models TEXT, status INTEGER);
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT);
                CREATE TABLE logs (id INTEGER PRIMARY KEY, user_id INTEGER,
                    channel_id INTEGER, type INTEGER, quota INTEGER, created_at INTEGER);
                INSERT INTO channels VALUES (1, '测试渠道', 1, '', 1);
                INSERT INTO users VALUES (1, 'alice', '爱丽丝');
            """)
            start = local_ts(2026, 10, 17)
            end = local_ts(2026, 10, 18)
            db.executemany("INSERT INTO logs VALUES (?, 1, 1, 2, ?, ?)",
                           [(1, 999, start - 1), (2, 20, start),
                            (3, 40, end - 1), (4, 999, end)])
            db.commit()
        self.store = self.open_store()
        self.store.set_configs({
            "db_type": "sqlite", "sqlite_path": self.source_path,
            "dingtalk_webhook": "https://example.com/robot",
            "consume_daily_report_enabled": True,
            "consume_enabled": False, "enabled": False,
        })
        self.store.upsert_channel_finance(1, "测试渠道", "CNY", "2", "CNY", "0.1",
                                         effective_at=1)
        self.monitor = consume_monitor.ConsumptionMonitor(self.store)
        self.now = local_ts(2026, 10, 18, 9)

    def open_store(self):
        store = Store(self.path)
        self.addCleanup(store._conn.close)
        return store

    def run_daily(self, result=(True, "已发送"), now=None):
        with patch.object(consume_monitor.time, "time", return_value=self.now if now is None else now), \
                patch.object(consume_monitor, "send_markdown", return_value=result) as send:
            count = self.monitor.run_daily_if_due()
        return count, send

    def test_defaults_and_disabled_daily(self):
        self.assertIs(config.DEFAULTS["consume_daily_report_enabled"], False)
        self.assertEqual(config.DEFAULTS["consume_daily_report_time"], "09:00")
        self.store.set_configs({"consume_daily_report_enabled": False})
        count, send = self.run_daily()
        self.assertEqual(count, 0)
        send.assert_not_called()
        self.assertEqual(self.store.daily_report_runs(), [])

    def test_before_due_does_not_claim(self):
        count, send = self.run_daily(now=self.now - 1)
        self.assertEqual(count, 0)
        send.assert_not_called()
        self.assertEqual(self.store.daily_report_runs(), [])

    def test_disabled_threshold_still_sends_complete_yesterday(self):
        with patch.object(consume_monitor.consumption, "compute",
                          wraps=consume_monitor.consumption.compute) as compute:
            count, send = self.run_daily()
        self.assertEqual(count, 1)
        self.assertEqual(compute.call_args.args[2:4],
                         (local_ts(2026, 10, 17), local_ts(2026, 10, 18)))
        text = send.call_args.args[2]
        for expected in ("2026-10-17", "网站额度消耗**：0.00012", "底层 quota**：60",
                         "折算人民币消费**：0.00006 CNY", "全站成本**：6 CNY",
                         "折算消费利润**：-5.99994 CNY", "不代表实收现金",
                         "测试渠道", "alice", "用户消费前五名"):
            self.assertIn(expected, text)
        self.assertEqual(self.store.daily_report_runs()[0]["status"], "success")
        self.assertEqual(self.store.get_state(consume_monitor.LAST_TS_KEY), "")

    def test_restart_deduplicates_persisted_claim(self):
        def sending(*args, **kwargs):
            self.assertEqual(self.open_store().daily_report_runs()[0]["status"], "sending")
            return True, "已发送"
        with patch.object(consume_monitor.time, "time", return_value=self.now), \
                patch.object(consume_monitor, "send_markdown", side_effect=sending) as send:
            self.assertEqual(self.monitor.run_daily_if_due(), 1)
            restarted = consume_monitor.ConsumptionMonitor(self.open_store())
            self.assertEqual(restarted.run_daily_if_due(), 0)
            self.assertEqual(send.call_count, 1)

    def test_incomplete_claim_is_not_retried(self):
        self.store.claim_daily_report("2026-10-17")
        count, send = self.run_daily()
        self.assertEqual(count, 0)
        send.assert_not_called()

    def test_send_failure_recorded_without_retry(self):
        count, _ = self.run_daily(result=(False, "钉钉拒绝请求"))
        self.assertEqual(count, 0)
        record = self.store.daily_report_runs()[0]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["detail"], "钉钉拒绝请求")
        self.monitor = consume_monitor.ConsumptionMonitor(self.open_store())
        _, send = self.run_daily()
        send.assert_not_called()

    def test_compute_and_send_exceptions_record_failure(self):
        for target in ("compute", "send_markdown"):
            with self.subTest(target=target):
                day = "2026-10-17" if target == "compute" else "2026-10-18"
                now = self.now if target == "compute" else local_ts(2026, 10, 19, 9)
                owner = consume_monitor.consumption if target == "compute" else consume_monitor
                with patch.object(consume_monitor.time, "time", return_value=now), \
                        patch.object(owner, target, side_effect=RuntimeError("测试异常")):
                    self.assertEqual(self.monitor.run_daily_if_due(), 0)
                record = next(r for r in self.store.daily_report_runs() if r["day"] == day)
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["detail"], "测试异常")

    def test_dst_window_uses_local_midnights(self):
        transition = calendar.timegm((2026, 3, 8, 7, 0, 0))
        def localtime(ts):
            return time.gmtime(ts - (5 if ts < transition else 4) * 3600)
        def mktime(parts):
            offset = 5 if tuple(parts[:3]) <= (2026, 3, 8) else 4
            return calendar.timegm(tuple(parts[:6])) + offset * 3600
        with patch.object(consume_monitor.time, "localtime", side_effect=localtime), \
                patch.object(consume_monitor.time, "mktime", side_effect=mktime), \
                patch.object(consume_monitor.consumption, "compute",
                             wraps=consume_monitor.consumption.compute) as compute:
            self.run_daily(now=calendar.timegm((2026, 3, 9, 13, 0, 0)))
        start, end = compute.call_args.args[2:4]
        self.assertEqual(end - start, 23 * 3600)
        self.assertEqual(self.store.daily_report_runs()[0]["day"], "2026-03-08")

    def test_loop_preserves_threshold_interval(self):
        for interval in (10, 65, 600):
            with self.subTest(interval=interval):
                self.store.set_configs({"consume_interval": interval})
                monitor = consume_monitor.ConsumptionMonitor(self.store)
                clock, calls, waits = [0], [], []
                def run_once():
                    calls.append(clock[0])
                def wait(seconds):
                    waits.append(seconds)
                    clock[0] += seconds
                    if clock[0] > interval * 2:
                        monitor.stop()
                with patch.object(consume_monitor.time, "monotonic", side_effect=lambda: clock[0]), \
                        patch.object(monitor, "run_once", side_effect=run_once), \
                        patch.object(monitor, "run_daily_if_due") as daily, \
                        patch.object(monitor._stop, "wait", side_effect=wait):
                    monitor.loop()
                self.assertEqual(calls, [0, interval, interval * 2])
                self.assertTrue(all(0 < seconds <= 30 for seconds in waits))
                self.assertEqual(daily.call_count, len(waits))

    def test_run_once_remains_independent(self):
        with patch.object(consume_monitor.time, "time", return_value=self.now), \
                patch.object(consume_monitor, "send_markdown") as send:
            self.assertEqual(self.monitor.run_once(), 0)
        send.assert_not_called()
        self.assertEqual(self.store.get_state(consume_monitor.LAST_TS_KEY), str(self.now))
        self.assertEqual(self.store.daily_report_runs(), [])

    def test_time_and_threshold_validation(self):
        key = "consume_daily_report_time"
        for value in ("00:00", "09:00", "23:59"):
            self.assertEqual(config.coerce(key, value), value)
        for value in ("24:00", "09:60", "9:00", "09:0", "-1:00", "12:00:00",
                      " 09:00", "09:00\n", "０９:００", "", None, 900):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.coerce(key, value)
        valid = config.effective_config(self.store)
        self.assertEqual(config.validate(valid), [])
        self.assertTrue(config.validate({**valid, key: "24:00"}))
        for threshold in ("consume_threshold_quota", "consume_change_quota"):
            self.assertEqual(config.coerce(threshold, "0"), 0)
            self.assertEqual(config.coerce(threshold, "100"), 100)
            with self.assertRaises(ValueError):
                config.coerce(threshold, "-1")
            self.assertTrue(config.validate({**valid, threshold: -1}))


if __name__ == "__main__":
    unittest.main()
