"""本地联调验证：造 new-api 库 + 假钉钉服务，断言去重与失败原因。

运行： python tests/test_smoke.py
"""

import calendar
import csv
from datetime import date, timedelta
from decimal import Decimal
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

import config as config_module  # noqa: E402
import consume_monitor as consume_monitor_module  # noqa: E402
import consumption as consumption_module  # noqa: E402
import poller as poller_module  # noqa: E402
import rates as rates_module  # noqa: E402
import report as report_module  # noqa: E402
import source as source_module  # noqa: E402
import store as store_module  # noqa: E402
import webui as webui_module  # noqa: E402
from poller import pay_method_label, payload_reason  # noqa: E402

RECEIVED = []
CHECKS = []


def check(condition, label):
    CHECKS.append((bool(condition), label))
    print(("  [OK] " if condition else "  [!!] ") + label)


class FakeDingTalk(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        RECEIVED.append(json.loads(self.rfile.read(length).decode("utf-8")))
        responses = {
            "/robot/array": [],
            "/robot/null": None,
            "/robot/string": "ok",
            "/robot/number": 0,
        }
        body = json.dumps(responses.get(self.path.split("?", 1)[0],
                                        {"errcode": 0, "errmsg": "ok"})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def build_source_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT);
        CREATE TABLE top_ups (id INTEGER PRIMARY KEY, user_id INTEGER, amount INTEGER,
            money REAL, trade_no TEXT, payment_method TEXT, payment_provider TEXT,
            create_time INTEGER, complete_time INTEGER, status TEXT);
        CREATE TABLE subscription_orders (id INTEGER PRIMARY KEY, user_id INTEGER,
            plan_id INTEGER, money REAL, trade_no TEXT, payment_method TEXT,
            payment_provider TEXT, status TEXT, create_time INTEGER,
            complete_time INTEGER, provider_payload TEXT);
        CREATE TABLE subscription_plans (id INTEGER PRIMARY KEY, title TEXT);
    """)
    conn.execute("INSERT INTO users (id, username, display_name) VALUES (1, 'alice', '爱丽丝')")
    # display_name 与 username 故意不同，用来验证推送里显示的是用户名
    conn.execute("INSERT INTO users (id, username, display_name) VALUES (2, 'bob', '鲍勃')")
    conn.execute("INSERT INTO subscription_plans (id, title) VALUES (3, '专业版')")
    now = int(time.time())
    conn.execute(
        "INSERT INTO top_ups VALUES (1, 1, 500000, 99.0, 'TP-SUCCESS-1', 'stripe', "
        "'stripe', ?, ?, 'success')", (now, now))
    conn.execute(
        "INSERT INTO subscription_orders VALUES (1, 1, 3, 199.0, 'SUB-SUCCESS-1', 'alipay', "
        "'epay', 'success', ?, ?, '')", (now, now))
    conn.commit()
    conn.close()


def add_wallet_order(db_path, user_id, trade_no, status, provider, money=50.0, amount=100000,
                     method=None, created=None):
    # method（真实支付类型）与 provider（网关名）故意可不同，如 wxpay + epay
    method = provider if method is None else method
    conn = sqlite3.connect(db_path)
    now = int(created or time.time())
    conn.execute(
        "INSERT INTO top_ups (user_id, amount, money, trade_no, payment_method, "
        "payment_provider, create_time, complete_time, status) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, amount, money, trade_no, method, provider, now,
         now if status == "success" else 0, status))
    conn.commit()
    conn.close()


def add_subscription_order(db_path, user_id, plan_id, trade_no, status, provider, payload="",
                           method=None):
    method = provider if method is None else method
    conn = sqlite3.connect(db_path)
    now = int(time.time())
    conn.execute(
        "INSERT INTO subscription_orders (user_id, plan_id, money, trade_no, payment_method, "
        "payment_provider, status, create_time, complete_time, provider_payload) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, plan_id, 199.0, trade_no, method, provider, status, now,
         now if status == "success" else 0, payload))
    conn.commit()
    conn.close()


def build_consume_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, display_name TEXT);
        CREATE TABLE channels (id INTEGER PRIMARY KEY, name TEXT, type TEXT,
            models TEXT, status INTEGER);
        CREATE TABLE logs (id INTEGER PRIMARY KEY, user_id INTEGER, channel_id INTEGER,
            type INTEGER, quota INTEGER, created_at INTEGER);
    """)
    conn.execute("INSERT INTO users (id, username, display_name) VALUES (1, 'alice', '爱丽丝')")
    conn.execute("INSERT INTO users (id, username, display_name) VALUES (2, 'bob', '鲍勃')")
    conn.execute("INSERT INTO channels (id, name, type, models, status) "
                 "VALUES (1, 'OpenAI', 'openai', 'gpt-4o', 1)")
    conn.execute("INSERT INTO channels (id, name, type, models, status) "
                 "VALUES (2, 'Anthropic', 'anthropic', 'claude-3', 1)")
    conn.execute("INSERT INTO channels (id, name, type, models, status) "
                 "VALUES (3, 'Google', 'google', 'gemini', 1)")
    conn.commit()
    conn.close()


def add_consume_log(db_path, log_id, user_id, channel_id, quota, created):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO logs (id, user_id, channel_id, type, quota, created_at) "
        "VALUES (?, ?, ?, 2, ?, ?)",
        (log_id, user_id, channel_id, quota, created))
    conn.commit()
    conn.close()


def main():
    tmp = tempfile.mkdtemp(prefix="notifier-test-")
    db_path = os.path.join(tmp, "one-api.db")
    build_source_db(db_path)

    dingtalk = ThreadingHTTPServer(("127.0.0.1", 0), FakeDingTalk)
    port = dingtalk.server_address[1]
    threading.Thread(target=dingtalk.serve_forever, daemon=True).start()

    for name in ("SQL_DSN", "SQLITE_PATH", "POLL_INTERVAL"):
        os.environ.pop(name, None)

    store = store_module.Store(os.path.join(tmp, "notifier.db"))
    store.set_configs({
        "db_type": "sqlite",
        "sqlite_path": db_path,
        "dingtalk_webhook": f"http://127.0.0.1:{port}/robot/send",
        "enabled": True,
        "backfill": False,
    })
    poller = poller_module.Poller(store)

    print("\n1) 首次运行只登记存量订单")
    poller.run_once()
    check(not RECEIVED, "首次运行没有补推历史订单")
    check(store.pushed_count() == 2, f"存量订单已登记（{store.pushed_count()} 条）")

    print("\n2) 失败订单：原因按渠道推导，支付方式显示真实类型")
    add_wallet_order(db_path, 2, "TP-FAIL-1", "failed", "epay", method="wxpay")
    poller.run_once()
    check(len(RECEIVED) == 1, "失败订单已推送一次")
    text = RECEIVED[-1]["markdown"]["text"] if RECEIVED else ""
    check("❌ 充值失败" in text, "标题为充值失败")
    check("易支付 下单或回调失败" in text, "原因按渠道推导")
    check("来源：推导" in text, "来源标注为推导")
    check("- **支付方式**：微小信" in text, "微信支付显示为微小信（不是 epay）")
    check("bob" in text, "显示的是用户名而不是 display_name")
    check("鲍勃" not in text, "没有显示 display_name")
    check("补发推送" not in text, "普通推送不带补发标注")

    print("\n3) 重复轮询不重复推送")
    poller.run_once()
    check(len(RECEIVED) == 1, "同一订单同一状态只推一次")

    print("\n4) 过期订单：超时原因由订单数据推导")
    add_wallet_order(db_path, 1, "TP-EXPIRE-1", "expired", "epay", method="alipay")
    poller.run_once()
    check(len(RECEIVED) == 2, "过期订单已推送")
    text = RECEIVED[-1]["markdown"]["text"]
    check("⏰ 充值超时" in text, "标题为充值超时")
    check("支付超时" in text, "原因含支付超时")
    check("来源：订单数据" in text, "来源标注为订单数据")
    check("- **支付方式**：支小宝" in text, "支付宝显示为支小宝（不是 epay）")
    check("alice" in text, "显示的是用户名而不是 display_name")
    check("爱丽丝" not in text, "没有显示 display_name")

    print("\n5) 订阅失败订单：原因取自 provider_payload")
    add_subscription_order(db_path, 1, 3, "SUB-FAIL-1", "failed", "epay", method="wxpay",
                           payload='{"status":"failed","code":"CARD_DECLINED",'
                                   '"error":"order creation rejected"}')
    poller.run_once()
    check(len(RECEIVED) == 3, "订阅失败订单已推送")
    text = RECEIVED[-1]["markdown"]["text"]
    check("订阅（专业版）" in text, "订阅消息带方案名")
    check("order creation rejected（code=CARD_DECLINED）" in text, "原因取自 provider_payload")
    check("来源：订单数据" in text, "来源标注为订单数据")
    check("- **支付方式**：微小信" in text, "订阅消息的支付方式同样映射")

    print("\n6) provider_payload 原因抽取")
    check(payload_reason('{"error":"order creation rejected","code":"CARD_DECLINED"}')
          == "order creation rejected（code=CARD_DECLINED）", "能解析 JSON 里的 error/code")
    check(payload_reason('failed trade_no=X error="boom"') == "boom", '兼容 error="..." 写法')
    check(payload_reason('{"status":"pending"}') == "", "没有 error/message 字段时返回空")

    print("\n7) 支付方式映射")
    check(pay_method_label("wxpay", "epay") == "微小信", "wxpay → 微小信")
    check(pay_method_label("ALIPAY", "epay") == "支小宝", "alipay 大小写不敏感 → 支小宝")
    check(pay_method_label("", "epay") == "易支付", "method 为空时回退渠道名")
    check(pay_method_label("stripe", "stripe") == "Stripe", "其他支付方式保持原样")
    check(pay_method_label("", "") == "未知方式", "都没有时显示未知方式")

    print("\n8) 推送历史与配置读取")
    history = store.history(10)
    check(len(history) == 3, f"推送历史记录了 {len(history)} 条")
    check(all(item["ok"] == 1 for item in history), "历史记录都是发送成功")
    check(all(item["backfill"] == 0 for item in history), "普通推送的历史记录不带补发标记")
    check(config_module.effective_config(store)["db_type"] == "sqlite", "配置可正常读取")

    print("\n9) 补发推送：绕过去重并标注补发推送")
    before = len(RECEIVED)
    count = poller.backfill(1)
    check(count == 5, f"补发最近 1 小时的 {count} 条终态订单")
    check(len(RECEIVED) - before == count, "补发不受去重限制，全部重推")
    check(all("（补发推送）" in item["markdown"]["text"] for item in RECEIVED[before:]),
          "补发消息正文都标注了补发推送")
    check(all("（补发推送）" in item["markdown"]["title"] for item in RECEIVED[before:]),
          "钉钉消息标题同样带补发标注")
    check(len([item for item in store.history(50) if item["backfill"] == 1]) == count,
          "推送历史记录了补发标记")
    check("补发推送" not in store.history(50)[0]["reason"], "补发标记不污染失败原因字段")

    print("\n10) 首次运行开启回溯时也标注补发推送")
    store2 = store_module.Store(os.path.join(tmp, "notifier2.db"))
    store2.set_configs({
        "db_type": "sqlite",
        "sqlite_path": db_path,
        "dingtalk_webhook": f"http://127.0.0.1:{port}/robot/send",
        "enabled": True,
        "backfill": True,
    })
    poller2 = poller_module.Poller(store2)
    before = len(RECEIVED)
    poller2.run_once()
    check(len(RECEIVED) - before == 5, "首次运行回溯推送了全部存量订单")
    check(all("（补发推送）" in item["markdown"]["text"] for item in RECEIVED[before:]),
          "回溯的存量订单也标注补发推送")
    check(store2.get_state("initialized") == "1", "回溯后同样标记为已初始化")

    print("\n11) 超时未支付：new-api 不会写 expired，改为按判定时长推导")
    now = int(time.time())
    store.set_configs({"expire_minutes": 5})
    add_wallet_order(db_path, 2, "TP-PENDING-STALE", "pending", "epay", method="wxpay",
                     created=now - 40 * 60)
    add_wallet_order(db_path, 2, "TP-PENDING-FRESH", "pending", "epay", method="wxpay",
                     created=now - 60)
    before = len(RECEIVED)
    poller.run_once()
    check(len(RECEIVED) - before == 1, "只有超过判定时长的那笔 pending 订单被推送")
    text = RECEIVED[-1]["markdown"]["text"]
    check("TP-PENDING-STALE" in text, "推送的是创建了 40 分钟的订单")
    check("⏰ 充值超时" in text, "标题为充值超时")
    check("下单后 5 分钟内未完成支付" in text, "原因写明判定时长")
    check("来源：推导" in text, "来源标注为推导")
    check("TP-PENDING-FRESH" not in text, "还在判定时长内的订单没有被推送")
    latest = store.history(1)[0]
    check(latest["reason_source"] == "derived", "推送历史里的来源记为推导")
    check(latest["backfill"] == 0, "推导出的超时推送不带补发标记")

    print("\n12) 超时判定可以关掉")
    poller.run_once()
    check(len(RECEIVED) - before == 1, "同一笔推导超时订单不重复推送")
    store.set_configs({"expire_minutes": 0})
    check(config_module.effective_config(store)["expire_minutes"] == 0, "配置能保存为 0")
    add_wallet_order(db_path, 2, "TP-PENDING-STALE-2", "pending", "epay", method="wxpay",
                     created=int(time.time()) - 40 * 60)
    before = len(RECEIVED)
    poller.run_once()
    check(len(RECEIVED) == before, "expire_minutes=0 时不再推导超时")

    print("\n13) 每日充值统计：钱包 + 订阅成功单都算，按金额排行")
    add_wallet_order(db_path, 1, "DR-A", "success", "epay", money=100.0, method="wxpay")
    add_wallet_order(db_path, 2, "DR-B", "success", "epay", money=300.0, method="wxpay")
    add_wallet_order(db_path, 2, "DR-C", "success", "epay", money=50.0, method="wxpay")
    add_wallet_order(db_path, 2, "DR-D", "failed", "epay", money=999.0, method="wxpay")
    # 窗口在订单写入之后再取，避免跨秒时新订单被排他上界挡掉
    start, end, label = report_module.day_window(time.time(), 0)
    rows = source_module.SourceDB(config_module.effective_config(store)).daily_recharge(start, end)
    check([row["user_id"] for row in rows] == [1, 2], "按金额倒序：alice(398) 在 bob(350) 前面")
    check(float(rows[0]["money"]) == 398.0 and int(rows[0]["orders"]) == 3,
          "钱包充值与订阅开通记在同一用户名下")
    check(float(rows[1]["money"]) == 350.0, "只统计成功单，失败的 999 元不计入")
    check(len(rows) == 2, "订阅失败单不进入排行")

    reporter = report_module.DailyReporter(store)
    before = len(RECEIVED)
    ok, detail = reporter.run_daily(offset_days=0, force=True)
    check(ok, f"手动触发日报推送（{detail}）")
    check(len(RECEIVED) - before == 1, "日报只推送一条消息")
    message = RECEIVED[-1]["markdown"]
    check("📊 每日充值统计" in message["title"], "标题为每日充值统计")
    check("**💰 总充值**  \n¥748.00" in message["text"], "总充值 748.00，标签与数值各占一行")
    check("**🧾 笔数**  \n5 笔" in message["text"], "笔数 5（含订阅开通）")
    check("**👥 充值人数**  \n2 人" in message["text"], "充值人数 2")
    check("**🏆 充值排行前五名**\n\n**🥇 alice（ID 1）**  \n¥398.00（3 笔）\n\n"
          "**🥈 bob（ID 2）**  \n¥350.00（2 笔）" in message["text"],
          "日报排行标题与每个用户独立成段，金额另起一行")
    weekly_title, weekly_text = report_module.render_report(
        rows, {1: "alice", 2: "bob"}, label, weekly=True)
    check("每周充值统计" in weekly_title and
          "**🏆 充值排行前五名**\n\n**🥇 alice（ID 1）**  \n¥398.00（3 笔）\n\n"
          "**🥈 bob（ID 2）**  \n¥350.00（2 笔）" in weekly_text,
          "周报排行也逐人分段显示")
    check(store.get_state("daily_report_last") == "", "手动触发不写去重标记")
    latest = store.history(1)[0]
    check(latest["kind"] == "daily" and latest["reason_source"] == "daily"
          and latest["trade_no"] == label, "日报也记进推送记录")
    check(float(latest["money"]) == 748.0 and latest["reason"] == "5 笔 / 2 人",
          "推送记录里带上汇总金额与笔数")

    print("\n14) 定时日报：窗口是刚过去的一天，且不重复推送")
    check(report_module.day_window(now, 1)[2] == time.strftime(
        "%Y-%m-%d", time.localtime(report_module.day_window(now, 1)[0])), "日期标签取自窗口起始日")
    yesterday = report_module.day_window(now, 1)[2]
    check(yesterday != report_module.day_window(now, 0)[2], "定时窗口是前一天，手动窗口是今天")
    check(report_module.day_window(now, 1)[1] - report_module.day_window(now, 1)[0] == 86400,
          "定时窗口是完整的一天")
    before = len(RECEIVED)
    ok, detail = reporter.run_daily()
    check(ok, f"定时日报推送成功（{detail}）")
    check("当天没有成功充值" in RECEIVED[-1]["markdown"]["text"], "昨天没有充值也要给出说明")
    check(store.get_state("daily_report_last") == yesterday, "定时推送写下去重标记")
    before = len(RECEIVED)
    ok, detail = reporter.run_daily()
    check(ok and "已推送过" in detail and len(RECEIVED) == before, "同一天不重复推送日报")
    check(report_module.seconds_until_midnight(now) > 0, "距离下一个 0 点为正数")

    print("\n15) 敏感项占位文案与环境变量覆盖")
    masked = config_module.mask_secrets({
        "db_dsn": "postgresql://u:p@h:5432/db", "dingtalk_webhook": "", "enabled": True})
    check(masked["db_dsn"] == config_module.MASK, "SQL_DSN 回显为占位文案")
    check(masked["dingtalk_webhook"] == "", "空值仍回显为空串")
    check(masked["enabled"] is True, "非敏感项不受影响")
    check(config_module.secret_flags({"db_dsn": "x", "dingtalk_webhook": ""})
          == {"db_dsn": True, "dingtalk_webhook": False, "dingtalk_secret": False},
          "secret_flags 标明哪些敏感项已有值")
    check(config_module.MASK == "内容被猫猫大王吃掉了", "占位文案与前端约定一致")
    os.environ["SQL_DSN"] = "postgresql://u:p@h:5432/db"
    try:
        overridden = config_module.env_overrides()
        check("db_dsn" in overridden and "db_type" in overridden, "SQL_DSN 覆盖项被识别")
        check(config_module.effective_config(store)["db_dsn"].startswith("postgresql://"),
              "环境变量优先于界面保存值")
    finally:
        os.environ.pop("SQL_DSN", None)
    check(config_module.env_overrides() == [], "清理后没有残留的环境覆盖")

    store.set_configs({"daily_report": False})
    before = len(RECEIVED)
    ok, detail = reporter.run_daily()
    check(ok and "已关闭" in detail and len(RECEIVED) == before, "关闭开关后定时日报不推送")
    check(config_module.effective_config(store)["daily_report"] is False, "开关能保存为关闭")

    print("\n16) 界面接口：敏感项只回显占位文案，留空提交不改动已保存的值")
    password = "test-password-123"
    webhook = f"http://127.0.0.1:{port}/robot/send"
    store3 = store_module.Store(os.path.join(tmp, "notifier3.db"))
    store3.set_configs({
        "db_type": "sqlite", "sqlite_path": db_path, "dingtalk_webhook": webhook,
        "dingtalk_secret": "SECRET-KEY-123456", "poll_interval": 10,
    })
    store3.set_admin_hash(webui_module.hash_password(password))
    httpd = webui_module.build_server(
        store3, poller, report_module.DailyReporter(store3), None, "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def post(path, body):
        request = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                        method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header(webui_module.CSRF_HEADER, webui_module.CSRF_VALUE)
        if cookie:
            request.add_header("Cookie", cookie)
        return request

    def call(request):
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    cookie = ""
    with urllib.request.urlopen(post("/api/login", {"password": password}), timeout=10) as resp:
        cookie = resp.headers["Set-Cookie"].split(";")[0]

    status_code, payload = call(urllib.request.Request(base + "/api/config",
                                                       headers={"Cookie": cookie}))
    check(status_code == 200, "登录后能读取配置")
    check(payload["config"]["dingtalk_webhook"] == config_module.MASK, "Webhook 回显为占位文案")
    check(payload["config"]["dingtalk_secret"] == config_module.MASK, "加签密钥回显为占位文案")
    check(payload["config"]["poll_interval"] == 10, "非敏感项照样回显真实值")
    check(payload["secrets"] == {"db_dsn": False, "dingtalk_webhook": True,
                                 "dingtalk_secret": True}, "secrets 标明哪些敏感项已保存")
    check(payload["env_overridden"] == [], "没有环境变量覆盖时列表为空")
    check(not any(config_module.MASK == value for key, value in payload["config"].items()
                  if key not in config_module.SECRET_KEYS), "占位文案只出现在敏感项上")

    status_code, payload = call(post("/api/config", {"config": {
        "dingtalk_webhook": config_module.MASK, "dingtalk_secret": "",
        "db_dsn": "", "poll_interval": 15}}))
    check(status_code == 200 and payload.get("ok"), "提交占位文案/空值不报错")
    saved = store3.config_map()
    check(saved["dingtalk_webhook"] == webhook, "提交占位文案保持原 Webhook 不变")
    check(saved["dingtalk_secret"] == "SECRET-KEY-123456", "留空提交保持原密钥不变")
    check(saved["poll_interval"] == 15, "同一次提交里的其他项正常保存")

    before = len(RECEIVED)
    status_code, payload = call(post("/api/daily-report", {}))
    check(status_code == 200 and payload["ok"], "界面能手动生成并推送今日统计")
    check(len(RECEIVED) - before == 1
          and "每日充值统计" in RECEIVED[-1]["markdown"]["title"], "推送的就是每日统计消息")

    status_code, payload = call(post("/api/config",
                                     {"config": {"dingtalk_webhook": "https://example.com/x"}}))
    check(store3.config_map()["dingtalk_webhook"] == "https://example.com/x", "填了新值就覆盖")
    httpd.shutdown()

    print("\n17) 未保存配置也能测试发送与手动统计（表单里的值即时生效）")
    store4 = store_module.Store(os.path.join(tmp, "notifier4.db"))
    store4.set_configs({"db_type": "sqlite", "sqlite_path": db_path})
    store4.set_admin_hash(webui_module.hash_password(password))
    httpd4 = webui_module.build_server(
        store4, poller, report_module.DailyReporter(store4), None, "127.0.0.1", 0)
    threading.Thread(target=httpd4.serve_forever, daemon=True).start()
    base4 = f"http://127.0.0.1:{httpd4.server_address[1]}"
    cookie4 = ""

    def post4(path, body):
        request = urllib.request.Request(base4 + path, data=json.dumps(body).encode(),
                                         method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header(webui_module.CSRF_HEADER, webui_module.CSRF_VALUE)
        if cookie4:
            request.add_header("Cookie", cookie4)
        return request

    with urllib.request.urlopen(post4("/api/login", {"password": password}), timeout=10) as resp:
        cookie4 = resp.headers["Set-Cookie"].split(";")[0]

    before = len(RECEIVED)
    status_code, payload = call(post4("/api/test", {}))
    check(status_code == 400 and "Webhook" in payload.get("error", ""),
          "没填也没保存 Webhook 时测试发送被拒（原问题现场）")

    status_code, payload = call(post4("/api/test", {"config": {"dingtalk_webhook": webhook}}))
    check(status_code == 200 and payload.get("ok"), "表单里未保存的 Webhook 也能测试发送")
    check(len(RECEIVED) - before == 1 and "测试消息" in RECEIVED[-1]["markdown"]["title"],
          "测试消息确实发出去了")
    check(store4.config_map().get("dingtalk_webhook") is None, "即时测试不写入已保存配置")

    before = len(RECEIVED)
    status_code, payload = call(post4("/api/daily-report",
                                      {"config": {"dingtalk_webhook": webhook}}))
    check(status_code == 200 and payload.get("ok"), "表单里未保存的 Webhook 也能手动生成今日统计")
    check(len(RECEIVED) - before == 1, "统计消息确实发出去了")

    status_code, payload = call(post4("/api/test",
                                      {"config": {"dingtalk_webhook": config_module.MASK}}))
    check(status_code == 400, "占位文案不会被当成真实 Webhook 提交")

    print("\n18) 财务 HTTP 接口：成功金额、趋势补零、排行与构成")

    def get4(path):
        return urllib.request.Request(base4 + path, headers={"Cookie": cookie4})

    for path in ("/api/finance/summary", "/api/finance/trend", "/api/finance/ranking",
                 "/api/finance/users", "/api/reports", "/api/orders", "/api/orders/export.csv"):
        status_code, payload = call(urllib.request.Request(base4 + path))
        check(status_code == 401 and payload.get("error") == "未登录",
              f"未登录访问 {path} 返回 401")

    status_code, summary = call(get4("/api/finance/summary?days=7"))
    check(status_code == 200 and summary.get("days") == 7, "财务概览返回 7 天窗口")
    expected_total = {"money": 748.0, "orders": 5, "users": 2}
    check(summary.get("total") == expected_total, "概览总额 748 / 5 订单 / 2 用户")
    check(summary.get("today") == expected_total, "今日成功充值与原有金额断言一致")
    check(summary.get("breakdown") == [
        {"key": "wallet", "label": "钱包充值", "money": 549.0, "orders": 4},
        {"key": "subscription", "label": "订阅开通", "money": 199.0, "orders": 1},
    ], "钱包 549 / 4 笔，订阅 199 / 1 笔")
    check([(item["label"], item["money"], item["orders"])
           for item in summary.get("methods", [])]
          == [("微小信", 450.0, 3), ("支小宝", 199.0, 1), ("Stripe", 99.0, 1)],
          "支付方式构成按真实支付类型汇总")

    status_code, trend = call(get4("/api/finance/trend?days=7"))
    points = trend.get("points", [])
    check(status_code == 200 and trend.get("days") == 7 and len(points) == 7,
          "7 天趋势始终返回 7 个日期")
    today_start = report_module.day_window(time.time(), 0)[0]
    check([point["date"] for point in points] == [
        (date.fromisoformat(time.strftime("%Y-%m-%d", time.localtime(today_start)))
         - timedelta(days=offset)).isoformat()
        for offset in range(6, -1, -1)], "趋势按日期顺序覆盖最近 7 天")
    check(all(point["money"] == 0 and point["orders"] == 0 for point in points[:-1]),
          "无充值的前 6 天补零")
    check(sum(point["money"] for point in points) == 748.0
          and sum(point["orders"] for point in points) == 5,
          "趋势总金额和订单数与概览一致")
    status_code, ranking = call(get4("/api/finance/ranking?days=7"))
    check(status_code == 200 and ranking.get("items") == [
        {"user_id": 1, "name": "alice", "money": 398.0, "orders": 3},
        {"user_id": 2, "name": "bob", "money": 350.0, "orders": 2},
    ], "HTTP 排行为 alice 398 / bob 350，不使用 display_name")

    print("\n19) 流水 HTTP 接口：状态、超时阈值、筛选与分页")
    with sqlite3.connect(db_path) as conn:
        flow_source_before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                              for table in ("top_ups", "subscription_orders")}
    status_code, payload = call(post4("/api/config", {"config": {"expire_minutes": 5}}))
    check(status_code == 200 and payload.get("ok"), "通过 HTTP 设置 5 分钟超时阈值")
    status_code, flow = call(get4("/api/orders?days=7"))
    items = {item["trade_no"]: item for item in flow.get("items", [])}
    successful = {"TP-SUCCESS-1", "SUB-SUCCESS-1", "DR-A", "DR-B", "DR-C"}
    failures = {"TP-FAIL-1", "TP-EXPIRE-1", "SUB-FAIL-1", "DR-D"}
    stale = {"TP-PENDING-STALE", "TP-PENDING-STALE-2"}
    check(status_code == 200 and set(items) == successful | failures | stale,
          "默认流水只展示 success/failed（含超时），不展示新 pending")
    check({item["bucket"] for item in items.values()} == {"success", "failed"},
          "默认流水仅包含成功与失败桶")
    check(items.get("TP-EXPIRE-1", {}).get("status") == "expired"
          and items.get("TP-EXPIRE-1", {}).get("bucket") == "failed"
          and items.get("TP-EXPIRE-1", {}).get("derived") is False,
          "源库 expired 归失败桶，保留原始状态且不是推导")
    check(all(items.get(trade, {}).get("status") == "expired"
              and items.get(trade, {}).get("bucket") == "failed"
              and items.get(trade, {}).get("derived") is True for trade in stale),
          "超时 pending 归失败桶，status=expired 且 derived=True")
    check(flow.get("has_more") is False, "足够大的默认上限返回 has_more=False")
    status_code, pending = call(get4("/api/orders?status=pending"))
    check(status_code == 200
          and [item["trade_no"] for item in pending.get("items", [])] == ["TP-PENDING-FRESH"],
          "阈值 5 时显式 pending 筛选只返回新待支付单")

    status_code, payload = call(post4("/api/config", {"config": {"expire_minutes": 0}}))
    check(status_code == 200 and payload.get("ok"), "通过 HTTP 关闭超时推导")
    status_code, disabled = call(get4("/api/orders"))
    check(status_code == 200
          and {item["trade_no"] for item in disabled.get("items", [])} == successful | failures,
          "阈值 0 时默认流水不包含任何 pending，源库 expired 仍为失败")
    status_code, pending = call(get4("/api/orders?status=pending"))
    check(status_code == 200
          and {item["trade_no"] for item in pending.get("items", [])}
          == stale | {"TP-PENDING-FRESH"}
          and all(item["status"] == "pending" and not item["derived"]
                  for item in pending.get("items", [])),
          "阈值 0 时显式 pending 筛选包含全部待支付，且没有推导标记")
    status_code, payload = call(post4("/api/config", {"config": {"expire_minutes": 5}}))
    status_code, restored = call(get4("/api/orders"))
    check(status_code == 200 and restored == flow, "5→0→5 后恢复完全一致的流水结果")
    with sqlite3.connect(db_path) as conn:
        flow_source_after = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                             for table in ("top_ups", "subscription_orders")}
    check(flow_source_after == flow_source_before, "超时推导和阈值切换不写入源库状态")

    # 新案例的数据只在全部原金额断言完成后插入；较新的待支付单不能挤掉筛选结果。
    for index in range(4):
        add_subscription_order(db_path, 2, 3, f"FLOW-NEW-PENDING-{index}", "pending", "epay")
    status_code, page = call(get4("/api/orders?status=success&limit=2"))
    check(status_code == 200 and len(page.get("items", [])) == 2
          and all(item["trade_no"] in successful and item["bucket"] == "success"
                  for item in page.get("items", [])),
          "先在 SQL 筛选 success 再 limit，较新的 pending 不挤占分页")
    check(page.get("has_more") is True, "5 笔成功单请求 limit=2 返回 has_more=True")
    status_code, full_page = call(get4("/api/orders?status=success&limit=5"))
    check(status_code == 200
          and {item["trade_no"] for item in full_page.get("items", [])} == successful
          and full_page.get("has_more") is False, "刚好 5 笔时 has_more=False")
    status_code, failed_page = call(get4("/api/orders?status=failed&limit=2"))
    check(status_code == 200 and len(failed_page.get("items", [])) == 2
          and all(item["bucket"] == "failed" for item in failed_page.get("items", []))
          and failed_page.get("has_more") is True,
          "失败筛选同样先过滤再截取，并正确返回 has_more")

    print("\n20) CSV：筛选、公式转义、导出上限与源库只读")
    formula_values = ["=1+1", "+SUM(1,2)", "-1+2", "@SUM(1,2)", "  =1+1",
                      "\t=1+1", "\r=1+1", "\n=1+1"]
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO users VALUES (3, ?, '公式用户')", ("=USER()",))
    for value in formula_values:
        add_wallet_order(db_path, 3, value, "failed", "epay", method="=METHOD()")
    with sqlite3.connect(db_path) as conn:
        source_before = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                         for table in ("top_ups", "subscription_orders")}

    for filter_status, expected_trades in (
            ("success", successful), ("failed", failures | stale | set(formula_values)),
            (None, successful | failures | stale | set(formula_values))):
        path = "/api/orders/export.csv" + (f"?status={filter_status}" if filter_status else "")
        with urllib.request.urlopen(get4(path), timeout=10) as response:
            body = response.read()
            csv_rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
            check(response.status == 200 and body.startswith(b"\xef\xbb\xbf")
                  and response.headers.get_content_type() == "text/csv",
                  f"CSV {filter_status or '默认'} 返回 UTF-8 BOM 和 CSV 类型")
        expected_csv_trades = {"'" + trade if trade in formula_values else trade
                               for trade in expected_trades}
        check(csv_rows[0] == ["时间", "状态", "订单号", "用户", "金额", "支付方式", "类型"]
              and {row[2] for row in csv_rows[1:]} == expected_csv_trades,
              f"CSV {filter_status or '默认'} 与流水状态筛选一致")
        if filter_status == "success":
            check(all(row[1] == "成功" for row in csv_rows[1:]), "成功 CSV 不含失败或 pending")
        if filter_status == "failed":
            formula_rows = [row for row in csv_rows[1:] if row[2] in expected_csv_trades
                            and row[2].startswith("'")]
            check({row[2] for row in formula_rows} == {"'" + value for value in formula_values}
                  and all(row[3] == "'=USER()" and row[5] == "'=METHOD()"
                          for row in formula_rows),
                  "CSV 订单号、用户名、支付方式的公式文本均以前导单引号转义")
            check({row[2]: row[1] for row in csv_rows[1:] if row[2] in stale}
                  == {trade: "失败·超时（推导）" for trade in stale},
                  "失败 CSV 保留推导超时状态说明")
            check(any(row[2] == "TP-EXPIRE-1" and row[1] == "失败·超时"
                      for row in csv_rows[1:]), "源库 expired 的 CSV 不冒充推导超时")

    status_code, payload = call(get4("/api/orders/export.csv?status=success&limit=4"))
    check(status_code == 400 and "导出上限" in payload.get("error", ""),
          "匹配订单超过 CSV 上限返回 400 及缩小范围说明")
    with urllib.request.urlopen(get4("/api/orders/export.csv?status=success&limit=5"),
                                timeout=10) as response:
        csv_rows = list(csv.reader(io.StringIO(response.read().decode("utf-8-sig"))))
        check(response.status == 200 and len(csv_rows) == 6,
              "CSV 上限恰好等于匹配笔数时可导出，新 pending/失败单不占上限")
    with sqlite3.connect(db_path) as conn:
        source_after = {table: conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
                        for table in ("top_ups", "subscription_orders")}
    check(source_after == source_before, "流水与 CSV 查询未修改源库订单的任何字段或状态")

    print("\n21) 钉钉异常：malformed Webhook 与非对象 JSON 均给出 400 说明")
    before = len(RECEIVED)
    for malformed in ("not-a-url", "http://[", "ftp://127.0.0.1/robot/send"):
        status_code, payload = call(post4("/api/test", {"config": {"dingtalk_webhook": malformed}}))
        check(status_code == 400 and payload.get("ok") is False
              and bool(payload.get("detail"))
              and ("Webhook" in payload["detail"] or "请求失败" in payload["detail"]),
              f"非法 Webhook {malformed!r} 返回 400 且有说明")
    check(len(RECEIVED) == before, "非法 Webhook 不向钉钉模拟服务发送请求")
    for response_kind in ("array", "null", "string", "number"):
        status_code, payload = call(post4("/api/test", {"config": {
            "dingtalk_webhook": f"http://127.0.0.1:{port}/robot/{response_kind}"}}))
        check(status_code == 400 and payload.get("ok") is False
              and "预期 JSON 对象" in payload.get("detail", ""),
              f"钉钉返回 {response_kind} 而非对象时返回 400 及格式说明")
    check(len(RECEIVED) - before == 4, "四种非对象响应均通过本地模拟服务验证，无外部请求")
    print("\n22) 周报 HTTP、报告状态与未保存钉钉覆盖")
    status_code, payload = call(get4("/api/reports"))
    check(status_code == 200 and payload == {
        "daily_report": True, "weekly_report": True, "daily_last": "", "weekly_last": "",
    }, "报告状态默认值及字段精确符合契约")
    call(post4("/api/config", {"weekly_report": False, "daily_report": False}))
    before = len(RECEIVED)
    status_code, payload = call(post4("/api/weekly-report", {"config": {
        "dingtalk_webhook": webhook, "dingtalk_secret": "UNSAVED-SECRET",
        "dingtalk_keyword": "周报测试", "dingtalk_at_mobiles": "13800000000",
    }}))
    check(status_code == 200 and payload.get("ok") and len(RECEIVED) == before + 1,
          "周报关闭时仍可用未保存表单手动发送")
    check("每周充值统计" in RECEIVED[-1]["markdown"]["title"]
          and "周报测试" in RECEIVED[-1]["markdown"]["text"]
          and RECEIVED[-1]["at"]["atMobiles"] == ["13800000000"],
          "周报复用钉钉关键字和手机号覆盖")
    check(store4.get_state("weekly_report_last") == ""
          and store4.get_state("daily_report_last") == ""
          and "dingtalk_webhook" not in store4.config_map(),
          "手动周报不保存覆盖配置或自动去重状态")
    store4.set_state("daily_report_last", "2026-10-15")
    store4.set_state("weekly_report_last", "2026-10-05 ~ 2026-10-11")
    status_code, payload = call(get4("/api/reports"))
    check(payload == {"daily_report": False, "weekly_report": False,
                      "daily_last": "2026-10-15", "weekly_last": "2026-10-05 ~ 2026-10-11"},
          "报告状态读取独立开关和既有/新增状态键")
    status_code, payload = call(post4("/api/weekly-report", {}))
    check(status_code == 400 and payload.get("ok") is False,
          "周报缺少实际 Webhook 时返回 400")

    print("\n23) 金额阈值、无效配置与成功订单降噪")
    defaults = config_module.DEFAULTS
    check(defaults["large_order_threshold"] == 50 and defaults["min_push_amount"] == 0
          and defaults["weekly_report"] is True, "默认大额 50、降噪 0、周报开启")
    for key in ("large_order_threshold", "min_push_amount"):
        for value in (0, "50.25"):
            status_code, payload = call(post4("/api/config", {key: value}))
            check(status_code == 200 and store4.config_map()[key] == float(value),
                  f"{key} 接受金额 {value!r}")
        for value in (-0.01, "NaN", "Infinity", "-Infinity", None, "abc", "1e999"):
            saved = store4.config_map()
            status_code, payload = call(post4("/api/config", {"poll_interval": 30, key: value}))
            check(status_code == 400 and key in payload.get("error", "")
                  and store4.config_map() == saved, f"{key} 拒绝 {value!r} 且不部分保存")
    sample = {"kind": "wallet", "status": "success", "money": 50.25,
              "trade_no": "BOUNDARY", "user_id": 1}
    for amount, marked in ((50.24, False), (50.25, True), (50.26, True)):
        title, text = poller_module.render_message(
            {**sample, "money": amount}, "alice", "", "", "", large_threshold=50.25)
        check(("大额订单" in title) == marked and ("大额订单" in text) == marked,
              f"大额标记金额边界 {amount}")
    noise_config = {**defaults, "min_push_amount": 50.25}
    for kind in ("wallet", "subscription"):
        for status, amount, wanted in (("success", 50.24, False), ("success", 50.25, True),
                                       ("success", 50.26, True), ("failed", 0, True),
                                       ("expired", 0, True)):
            order = {**sample, "kind": kind, "status": status, "money": amount}
            check(poller_module.Poller._wanted(order, noise_config) == wanted,
                  f"降噪 {kind}/{status}/{amount} 判定正确")
            if status != "success":
                title, _ = poller_module.render_message(
                    order, "alice", "", "", "", large_threshold=0)
                check("大额订单" not in title, f"{kind}/{status} 不添加大额标记")
    check(poller_module.Poller._wanted({**sample, "money": 0}, defaults),
          "默认降噪保留零金额成功订单")

    print("\n24) 自然日、跨年自然周与 DST 窗口")
    fixed = int(time.mktime((2026, 1, 5, 12, 34, 56, 0, 0, -1)))
    midnight = int(time.mktime((2026, 1, 5, 0, 0, 0, 0, 0, -1)))
    start, end, label = report_module.day_window(fixed)
    check(label == "2026-01-04" and end == midnight
          and time.localtime(start)[:3] == (2026, 1, 4), "日报覆盖前一完整自然日")
    check(report_module.day_window(fixed, 0) == (midnight, fixed + 1, "2026-01-05"),
          "手动日报包含当前秒")
    start, end, label = report_module.week_window(fixed)
    check(time.localtime(start)[:3] == (2025, 12, 29) and end == midnight
          and label == "2025-12-29 ~ 2026-01-04", "上周跨年仍按周一至周日计算")
    check(report_module.week_window(fixed, 0) == (
        midnight, fixed + 1, "2026-01-05 ~ 2026-01-05"), "周一手动周报从本周零点起")
    sunday = int(time.mktime((2026, 1, 11, 23, 59, 59, 0, 0, -1)))
    check(report_module.week_window(sunday, 0) == (
        midnight, sunday + 1, "2026-01-05 ~ 2026-01-11"), "周日手动周报不提前跨入下周")
    # 模拟北美春季 DST：3 月 8 日 02:00 跳到 03:00，不依赖系统 tzset。
    transition = calendar.timegm((2026, 3, 8, 7, 0, 0))

    def dst_localtime(timestamp):
        offset = 5 if timestamp < transition else 4
        return time.gmtime(timestamp - offset * 3600)

    def dst_mktime(parts):
        offset = 5 if tuple(parts[:3]) <= (2026, 3, 8) else 4
        return calendar.timegm(tuple(parts[:6])) + offset * 3600

    dst_now = calendar.timegm((2026, 3, 9, 16, 0, 0))
    with patch.object(report_module.time, "localtime", side_effect=dst_localtime), \
            patch.object(report_module.time, "mktime", side_effect=dst_mktime):
        start, end, label = report_module.day_window(dst_now)
        check(label == "2026-03-08" and end - start == 23 * 3600,
              "DST 日报自然日允许 23 小时，不固定减 86400")
        start, end, label = report_module.week_window(dst_now)
        check(label == "2026-03-02 ~ 2026-03-08" and end - start == 167 * 3600,
              "DST 完整自然周允许 167 小时")
        dst_day = calendar.timegm((2026, 3, 8, 5, 0, 0))
        check(report_module.seconds_until_midnight(dst_day) == 23 * 3600,
              "DST 午夜等待按下一个自然日期计算")

    print("\n25) 独立开关、去重与失败重试")
    report_store = store_module.Store(os.path.join(tmp, "weekly.db"))
    report_store.set_configs({"db_type": "sqlite", "sqlite_path": db_path,
                              "dingtalk_webhook": webhook, "daily_report": False})
    weekly_reporter = report_module.DailyReporter(report_store)
    before = len(RECEIVED)
    ok, _ = weekly_reporter.run_weekly()
    weekly_last = report_store.get_state("weekly_report_last")
    check(ok and len(RECEIVED) == before + 1 and bool(weekly_last)
          and report_store.get_state("daily_report_last") == "",
          "日报关闭不阻止周报发送及独立标记")
    ok, detail = weekly_reporter.run_weekly()
    check(ok and "已推送过" in detail and len(RECEIVED) == before + 1, "周报独立去重")
    report_store.set_configs({"daily_report": True, "weekly_report": False})
    ok, _ = weekly_reporter.run_daily()
    check(ok and len(RECEIVED) == before + 2 and bool(report_store.get_state("daily_report_last")),
          "周报关闭不阻止日报发送")
    ok, detail = weekly_reporter.run_weekly()
    check(ok and "已关闭" in detail and len(RECEIVED) == before + 2, "关闭周报不发送")
    report_store.set_configs({"weekly_report": True})
    report_store.set_state("weekly_report_last", "")
    with patch.object(report_module, "send_markdown", return_value=(False, "发送失败")):
        ok, _ = weekly_reporter.run_weekly()
    check(not ok and report_store.get_state("weekly_report_last") == "",
          "失败周报不写去重标记，允许后续重试")
    ok, _ = weekly_reporter.run_weekly()
    check(ok and report_store.get_state("weekly_report_last") == weekly_last,
          "失败后周报可再次发送并记录成功")
    for daily_results, weekly_results, counts in (
            ([(False, "失败")] * 3, [(True, "成功")], (3, 1)),
            ([(True, "关闭")], [(False, "失败"), (True, "成功")], (1, 2)),
            ([RuntimeError("数据源失败"), (True, "成功")], [(True, "成功")], (2, 1))):
        with patch.object(weekly_reporter, "run_daily", side_effect=daily_results) as daily, \
                patch.object(weekly_reporter, "run_weekly", side_effect=weekly_results) as weekly, \
                patch.object(weekly_reporter._stop, "wait", return_value=False):
            weekly_reporter.run_scheduled(now=fixed)
            check((daily.call_count, weekly.call_count) == counts,
                  f"周一日周报分别重试，调用次数为 {counts}")
    with patch.object(weekly_reporter, "run_daily", return_value=(True, "成功")) as daily, \
            patch.object(weekly_reporter, "run_weekly") as weekly:
        weekly_reporter.run_scheduled(now=fixed + 86400)
        check(daily.call_count == 1 and weekly.call_count == 0, "非周一午夜仅执行日报")

    print("\n26) 用户画像：周期边界、类型金额、时间与只读数据源")
    profile_db = os.path.join(tmp, "profiles.db")
    build_source_db(profile_db)
    profile_now = int(time.time())
    start, end = webui_module.finance_module.range_window(7, profile_now)
    with sqlite3.connect(profile_db) as conn:
        conn.execute("UPDATE top_ups SET create_time = ?, complete_time = ?", (start, end + 900))
        conn.execute("UPDATE subscription_orders SET create_time = ?", (profile_now - 5,))
    add_wallet_order(profile_db, 1, "PROFILE-IN", "success", "epay", money=100,
                     created=profile_now)
    add_wallet_order(profile_db, 1, "PROFILE-OLD", "success", "epay", money=999,
                     created=start - 1)
    add_wallet_order(profile_db, 1, "PROFILE-FUTURE", "success", "epay", money=999,
                     created=end)
    add_wallet_order(profile_db, 1, "PROFILE-FAILED", "failed", "epay", money=999,
                     created=profile_now)
    add_wallet_order(profile_db, 2, "PROFILE-BOB", "success", "epay", money=50,
                     created=profile_now - 10)
    store4.set_configs({"sqlite_path": profile_db})
    with sqlite3.connect(profile_db) as conn:
        profile_before = list(conn.iterdump())
    with patch.object(webui_module.finance_module, "range_window", return_value=(start, end)):
        status_code, payload = call(get4("/api/finance/users?days=7"))
        check(status_code == 200 and payload == {"days": 7, "items": [
            {"user_id": 1, "username": "alice", "total": 398.0, "orders": 3,
             "average": 132.67, "first_time": start, "last_time": profile_now,
             "wallet": 199.0, "subscription": 199.0},
            {"user_id": 2, "username": "bob", "total": 50.0, "orders": 1,
             "average": 50.0, "first_time": profile_now - 10, "last_time": profile_now - 10,
             "wallet": 50.0, "subscription": 0.0},
        ]}, "画像字段精确匹配，排除窗外/失败单，时间取窗口内 create_time")
        status_code, default_profile = call(get4("/api/finance/users"))
        check(status_code == 200 and default_profile == payload, "画像默认使用 7 天窗口")
    with patch.object(webui_module.finance_module, "range_window", return_value=(end + 1, end + 2)):
        status_code, payload = call(get4("/api/finance/users?days=1"))
        check(status_code == 200 and payload == {"days": 1, "items": []}, "无成功订单时返回空画像")
    with sqlite3.connect(profile_db) as conn:
        check(list(conn.iterdump()) == profile_before, "画像查询和通知器配置保存不改写源库")
    print("\n27) 真实轮询链路应用降噪和大额阈值")
    noise_db = os.path.join(tmp, "noise-source.db")
    build_source_db(noise_db)
    noise_store = store_module.Store(os.path.join(tmp, "noise.db"))
    noise_store.set_configs({"db_type": "sqlite", "sqlite_path": noise_db,
                             "dingtalk_webhook": webhook, "min_push_amount": 50.25,
                             "large_order_threshold": 50.25})
    noise_poller = poller_module.Poller(noise_store)
    noise_poller.run_once()
    for trade, status, amount in (("NOISE-LOW", "success", 50.24),
                                   ("NOISE-EQUAL", "success", 50.25),
                                   ("NOISE-HIGH", "success", 50.26),
                                   ("NOISE-FAIL", "failed", 0),
                                   ("NOISE-EXPIRED", "expired", 0)):
        add_wallet_order(noise_db, 1, trade, status, "epay", money=amount)
    with sqlite3.connect(noise_db) as conn:
        noise_source_before = list(conn.iterdump())
    before = len(RECEIVED)
    check(noise_poller.run_once() == 4, "轮询推送两笔达标成功单及零金额失败/超时单")
    messages = RECEIVED[before:]
    check(len(messages) == 4 and not any("NOISE-LOW" in item["markdown"]["text"]
                                        for item in messages), "低于阈值成功单实际未发送")
    check(sum("大额订单" in item["markdown"]["title"] for item in messages) == 2,
          "轮询成功消息实际使用已保存的小数大额阈值")
    check(noise_poller.run_once() == 0 and len(RECEIVED) == before + 4,
          "降噪不破坏已发送订单去重")
    noise_store.set_configs({"min_push_amount": 0})
    check(noise_poller.run_once() == 1 and "NOISE-LOW" in RECEIVED[-1]["markdown"]["text"],
          "降低阈值后被过滤成功单仍可正常推送")
    with sqlite3.connect(noise_db) as conn:
        check(list(conn.iterdump()) == noise_source_before, "金额过滤与推送均不改变源库")
    httpd4.shutdown()

    print("\n28) 汇率解析：1:X 输入、货币规范与金额精度")
    check(rates_module.parse_rate("1:7") == Decimal("7"), "1:7 解析为 7")
    check(rates_module.parse_rate("7") == Decimal("7"), "纯数字 7 解析为 7")
    check(rates_module.parse_rate("1:7.5") == Decimal("7.5"), "1:7.5 解析为 7.5")
    check(rates_module.parse_rate("2:14") == Decimal("7"), "2:14 折算为 7")
    for bad in ("", "0", "1:0", "abc", "-1"):
        try:
            rates_module.parse_rate(bad)
            check(False, f"非法汇率 {bad!r} 应抛 ValueError")
        except ValueError:
            check(True, f"非法汇率 {bad!r} 抛 ValueError")
    check(rates_module.parse_cost("0.01") == Decimal("0.01"), "成本单价 0.01 解析正确")
    check(rates_module.normalize_currency(" cny ") == "CNY", "货币种类去空格并大写")
    check(rates_module.fmt_money(Decimal("200") / Decimal("7")) == "28.571429",
          "金额六位小数并去掉末尾零")

    print("\n29) 汇率保存：历史记录与调整通知文案")
    rate_store = store_module.Store(os.path.join(tmp, "rate.db"))
    first = rates_module.save_rate(rate_store, "CNY", "7")
    check(first == ("", "7", "CNY"), "首次保存汇率返回空旧值")
    check(rate_store.rate_history("CNY", 10)[0]["old_rate"] == "0",
          "首次历史记录旧汇率为 0")
    second = rates_module.save_rate(rate_store, "CNY", "7.5")
    check(second == ("7", "7.5", "CNY"), "再次保存返回旧值 7 新值 7.5")
    check([(h["old_rate"], h["new_rate"]) for h in rate_store.rate_history("CNY", 10)]
          == [("7", "7.5"), ("0", "7")], "历史倒序记录两次调整")
    title, text = rates_module.render_rate_change("CNY", "0", "7")
    check("旧汇率**：-" in text and "新汇率**：1:7" in text, "汇率调整文案含旧汇率与新汇率")

    print("\n30) 消耗统计：用户消耗金额、上游渠道成本与排行")
    consume_db = os.path.join(tmp, "consume-source.db")
    build_consume_db(consume_db)
    add_consume_log(consume_db, 1, 1, 1, 70, 1000)   # alice ch1 70
    add_consume_log(consume_db, 2, 1, 2, 20, 1000)   # alice ch2 20
    add_consume_log(consume_db, 3, 2, 1, 10, 1000)   # bob ch1 10
    add_consume_log(consume_db, 4, 2, 3, 100, 1000)  # bob ch3 100
    consume_store = store_module.Store(os.path.join(tmp, "consume-state.db"))
    consume_store.set_configs({"db_type": "sqlite", "sqlite_path": consume_db,
                               "rate_currency": "CNY"})
    consume_store.set_exchange_rate("CNY", "7")
    consume_store.set_channel_cost(1, "0.01", "CNY")
    consume_store.set_channel_cost(2, "0.02", "CNY")
    consume_source = source_module.SourceDB(config_module.effective_config(consume_store))
    cstats = consumption_module.compute(consume_store, consume_source, 0, 10 ** 12, "CNY")
    check(cstats["quota"] == 200, "总消耗额度 200")
    check(cstats["user_money"] == "28.571429", "用户消耗金额 200/7 = 28.571429")
    check(cstats["upstream_cost"] == "1.2", "上游成本 80*0.01 + 20*0.02 = 1.2")
    check([(c["channel_id"], c["quota"], c["cost"], c["cost_currency"])
           for c in cstats["channels"]] ==
          [(3, 100, "0", "CNY"), (1, 80, "0.8", "CNY"), (2, 20, "0.4", "CNY")],
          "渠道按额度降序且成本口径正确")
    check([(u["user_id"], u["quota"], u["money"]) for u in cstats["users"]] ==
          [(2, 110, "15.714286"), (1, 90, "12.857143")],
          "用户按额度降序且金额 = 额度/汇率")

    print("\n31) 消耗监控：变动通知、阈值通知与水位推进")
    monitor_db = os.path.join(tmp, "monitor-source.db")
    build_consume_db(monitor_db)
    monitor_store = store_module.Store(os.path.join(tmp, "monitor-state.db"))
    monitor_store.set_configs({
        "db_type": "sqlite", "sqlite_path": monitor_db,
        "dingtalk_webhook": webhook, "rate_currency": "CNY",
        "consume_enabled": True, "consume_interval": 600,
        "consume_threshold_quota": 100, "consume_change_quota": 50,
    })
    monitor_store.set_exchange_rate("CNY", "7")
    monitor = consume_monitor_module.ConsumptionMonitor(monitor_store)
    before = len(RECEIVED)
    clock = [1000]
    with patch.object(consume_monitor_module.time, "time", side_effect=lambda: clock[0]):
        check(monitor.run_once() == 0, "首次运行只落水位，不推送历史消耗")
        add_consume_log(monitor_db, 1, 1, 1, 60, 1500)
        clock[0] = 2000
        check(monitor.run_once() == 1, "单次新增 60 额度达到变动阈值推送一次")
        check(monitor_store.get_state(consume_monitor_module.CUMULATIVE_KEY) == "60",
              "累计消耗累计到 60")
        add_consume_log(monitor_db, 2, 2, 2, 40, 2500)
        clock[0] = 3000
        check(monitor.run_once() == 1, "累计 100 额度达到阈值推送一次")
        check(monitor_store.get_state(consume_monitor_module.CUMULATIVE_KEY) == "0",
              "达到阈值后累计清零")
        add_consume_log(monitor_db, 3, 1, 1, 60, 3500)
        clock[0] = 4000
        check(monitor.run_once() == 1, "阈值清零后再次新增 60 额度触发变动通知")
        check(monitor_store.get_state(consume_monitor_module.CUMULATIVE_KEY) == "60",
              "清零后重新累计 60")
    messages = RECEIVED[before:]
    check(len(messages) == 3, "三轮消耗推送了 3 条消息")
    check(any("累计消耗额度**：100" in item["markdown"]["text"] for item in messages),
          "阈值消息附带累计消耗额度 100")

    print("\n32) 消耗 HTTP 接口：汇率保存、渠道成本与消耗统计")
    consume_http_db = os.path.join(tmp, "consume-http-source.db")
    build_consume_db(consume_http_db)
    add_consume_log(consume_http_db, 1, 1, 1, 900, int(time.time()))
    store5 = store_module.Store(os.path.join(tmp, "consume-http.db"))
    store5.set_configs({"db_type": "sqlite", "sqlite_path": consume_http_db,
                        "dingtalk_webhook": webhook, "rate_currency": "CNY"})
    store5.set_admin_hash(webui_module.hash_password(password))
    httpd5 = webui_module.build_server(
        store5, poller, report_module.DailyReporter(store5), None, "127.0.0.1", 0)
    threading.Thread(target=httpd5.serve_forever, daemon=True).start()
    base5 = f"http://127.0.0.1:{httpd5.server_address[1]}"
    cookie5 = ""

    def post5(path, body):
        request = urllib.request.Request(base5 + path, data=json.dumps(body).encode(),
                                         method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header(webui_module.CSRF_HEADER, webui_module.CSRF_VALUE)
        if cookie5:
            request.add_header("Cookie", cookie5)
        return request

    def get5(path):
        return urllib.request.Request(base5 + path, headers={"Cookie": cookie5})

    with urllib.request.urlopen(post5("/api/login", {"password": password}), timeout=10) as resp:
        cookie5 = resp.headers["Set-Cookie"].split(";")[0]

    status_code, payload = call(get5("/api/rates"))
    check(status_code == 200 and payload["rates"] == [] and payload["history"] == [],
          "初始汇率列表与历史均为空")
    status_code, payload = call(post5("/api/rates", {"currency": "CNY", "rate": "7"}))
    check(status_code == 200 and payload["ok"] and not payload["changed"]
          and not payload["notified"], "首次保存汇率不触发通知")
    status_code, payload = call(post5("/api/rates", {"currency": "CNY", "rate": "7.5"}))
    check(status_code == 200 and payload["changed"] and payload["notified"],
          "调整汇率触发通知")
    status_code, payload = call(post5("/api/rates", {"currency": "CNY", "rate": "0"}))
    check(status_code == 400, "非法汇率返回 400")

    status_code, payload = call(get5("/api/channels"))
    check(status_code == 200 and len(payload["channels"]) == 3, "渠道列表返回 3 条")
    status_code, payload = call(post5("/api/cost", {"channel_id": 1, "cost_per_unit": "0.01"}))
    check(status_code == 200 and payload.get("ok"), "保存渠道成本单价")
    status_code, payload = call(get5("/api/channels"))
    ch1 = [c for c in payload["channels"] if c["id"] == 1][0]
    check(ch1["cost_per_unit"] == "0.01" and ch1["cost_currency"] == "CNY",
          "渠道成本回显为保存值")
    status_code, payload = call(post5("/api/cost/delete", {"channel_id": 1}))
    check(status_code == 200 and payload.get("ok"), "删除渠道成本单价")
    status_code, payload = call(get5("/api/channels"))
    ch1 = [c for c in payload["channels"] if c["id"] == 1][0]
    check(ch1["cost_per_unit"] == "", "删除后成本单价为空")

    status_code, payload = call(get5("/api/consumption?days=1"))
    check(status_code == 200 and payload["rate"] == "7.5" and payload["quota"] == 900
          and payload["user_money"] == "120" and payload["upstream_cost"] == "0"
          and payload["has_rate"] is True,
          "消耗统计按最新汇率 7.5 计算，900 额度 = 120 元")
    httpd5.shutdown()

    failed = [label for ok, label in CHECKS if not ok]
    print("\n" + "=" * 56)
    if failed:
        print(f"失败 {len(failed)}/{len(CHECKS)}：")
        for label in failed:
            print("  - " + label)
        return 1
    print(f"全部通过：{len(CHECKS)}/{len(CHECKS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
