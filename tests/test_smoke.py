"""本地联调验证：造 new-api 库 + 假钉钉服务，断言去重与失败原因。

运行： python tests/test_smoke.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "app"))

import config as config_module  # noqa: E402
import poller as poller_module  # noqa: E402
import store as store_module  # noqa: E402
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
        body = json.dumps({"errcode": 0, "errmsg": "ok"}).encode()
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
    add_wallet_order(db_path, 2, "TP-PENDING-STALE", "pending", "epay", method="wxpay",
                     created=now - 40 * 60)
    add_wallet_order(db_path, 2, "TP-PENDING-FRESH", "pending", "epay", method="wxpay",
                     created=now - 5 * 60)
    before = len(RECEIVED)
    poller.run_once()
    check(len(RECEIVED) - before == 1, "只有超过判定时长的那笔 pending 订单被推送")
    text = RECEIVED[-1]["markdown"]["text"]
    check("TP-PENDING-STALE" in text, "推送的是创建了 40 分钟的订单")
    check("⏰ 充值超时" in text, "标题为充值超时")
    check("下单后 30 分钟内未完成支付" in text, "原因写明判定时长")
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
