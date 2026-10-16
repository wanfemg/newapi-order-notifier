"""轮询 new-api 订单，检测状态变更后推送钉钉。

new-api 的订单表只存状态、不存失败原因，所以本项目不依赖运行日志，只按订单自身
能拿到的信息给出原因：
  1. expired：订单表里真的写成了 expired 时取自订单数据；但 new-api 只在少数渠道
     （如 Stripe 的会话超时回调）会写 expired，易支付这类渠道超时后订单会一直停在
     pending，所以超过 expire_minutes 仍是 pending 的订单由本程序推导为超时；
  2. failed ：先看订单的 provider_payload（渠道回调原文），再看渠道名推导。
每条消息都会标注原因来自「订单数据 / 推导」。
"""

import logging
import re
import threading
import time

import config as config_module
from notify import send_markdown
from source import SourceDB

TERMINAL_STATUSES = ("success", "failed", "expired")
STATUS_TEXT = {"success": "成功", "failed": "失败", "expired": "已过期"}
SOURCE_TEXT = {"order": "订单数据", "derived": "推导"}

PROVIDER_LABELS = {
    "epay": "易支付",
    "stripe": "Stripe",
    "creem": "Creem",
    "waffo": "Waffo",
    "waffo_pancake": "Waffo Pancake",
    "balance": "余额",
}

# 订单的 payment_method 存的才是真实支付类型，payment_provider 只是网关名（如 epay）
PAY_METHOD_LABELS = {
    "wxpay": "微小信",
    "wxpay2": "微小信",
    "wechat": "微小信",
    "weixin": "微小信",
    "wx": "微小信",
    "alipay": "支小宝",
    "alipay2": "支小宝",
    "ali": "支小宝",
}

WALLET_TITLES = {"success": "💰 充值成功", "failed": "❌ 充值失败", "expired": "⏰ 充值超时"}
SUBSCRIPTION_TITLES = {"success": "🎫 订阅开通成功", "failed": "❌ 订阅支付失败", "expired": "⏰ 订阅支付超时"}

# provider_payload 是渠道回调的原文，可能是 JSON（"error":"..."）也可能是 Go 日志式（error="..."）
ERROR_FIELD_RE = re.compile(r'"?error"?\s*[=:]\s*"([^"]*)"')
MESSAGE_FIELD_RE = re.compile(r'"?message"?\s*[=:]\s*"([^"]*)"')
CODE_FIELD_RE = re.compile(r'"?code"?\s*[=:]\s*"?([A-Za-z0-9_.\-]+)')


def payload_reason(payload):
    """从订单的 provider_payload 里抽失败原因，抽不到返回空串。"""
    for regex in (ERROR_FIELD_RE, MESSAGE_FIELD_RE):
        match = regex.search(payload)
        if match and match.group(1).strip():
            reason = " ".join(match.group(1).split()).strip(" ,;|")
            code = CODE_FIELD_RE.search(payload)
            return f"{reason}（code={code.group(1)}）" if code else reason
    return ""


def format_time(timestamp):
    if not timestamp:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp)))


def provider_label(provider):
    if not provider:
        return "未知渠道"
    return PROVIDER_LABELS.get(provider, provider)


def pay_method_label(method, provider=""):
    """支付方式：优先订单的真实支付类型（wxpay/alipay），为空时回退渠道名。"""
    key = (method or "").strip() or (provider or "").strip()
    if not key:
        return "未知方式"
    lower = key.lower()
    return PAY_METHOD_LABELS.get(lower) or PROVIDER_LABELS.get(lower) or key


def resolve_reason(order, config):
    """返回 (原因文本, 来源标记)。原因只来自订单自身，不读运行日志。"""
    status = order["status"]
    if status == "expired":
        created = format_time(order.get("create_time"))
        if order.get("expired_derived"):
            # 订单表里一直是 pending，由本程序按判定时长推导为超时
            minutes = int(config.get("expire_minutes") or 0)
            return f"支付超时：下单后 {minutes} 分钟内未完成支付（下单时间 {created}）", "derived"
        return f"支付超时：下单后未在支付有效期内完成支付（下单时间 {created}）", "order"
    if status == "failed":
        payload = (order.get("provider_payload") or "").strip()
        if payload:
            reason = payload_reason(payload)
            if reason:
                return reason, "order"
        return f"{provider_label(order.get('payment_provider'))} 下单或回调失败", "derived"
    return "", ""


def render_message(order, username, plan_title, reason, reason_source, backfill=False,
                   large_threshold=None):
    """生成钉钉 markdown 消息，返回 (标题, 正文)。backfill 为真时标注「补发推送」。"""
    kind = order["kind"]
    status = order["status"]
    titles = WALLET_TITLES if kind == "wallet" else SUBSCRIPTION_TITLES
    headline = titles[status]
    if (status == "success" and large_threshold is not None
            and float(order.get("money") or 0) >= large_threshold):
        headline += "（大额订单）"
    if backfill:
        headline += "（补发推送）"

    lines = [f"### {headline}"]
    lines.append(f"- **订单号**：{order['trade_no']}")
    lines.append(f"- **用户**：{username}（ID {order['user_id']}）")
    if kind == "wallet":
        lines.append("- **类型**：钱包充值")
    else:
        plan = plan_title or f"方案 #{order.get('plan_id')}"
        lines.append(f"- **类型**：订阅（{plan}）")
    lines.append(f"- **金额**：¥{float(order.get('money') or 0):.2f}")

    if status == "success":
        if kind == "wallet":
            lines.append(f"- **额度**：+{int(order.get('amount') or 0):,}")
    else:
        lines.append(f"- **状态**：{status}（{STATUS_TEXT.get(status, status)}）")
        source = SOURCE_TEXT.get(reason_source, reason_source)
        if reason:
            lines.append(f"- **失败原因**：{reason}（来源：{source}）")
        else:
            lines.append(f"- **失败原因**：未获取到，请到 new-api 后台查看订单 {order['trade_no']}")

    lines.append(f"- **支付方式**：{pay_method_label(order.get('payment_method'), order.get('payment_provider'))}")
    lines.append(f"- **时间**：{format_time(order.get('complete_time') or order.get('create_time'))}")
    return headline, "\n".join(lines)


class Poller:
    def __init__(self, store):
        self.store = store
        self.log = logging.getLogger("poller")
        self._stop = threading.Event()
        # 补发与常规轮询互斥，避免同一订单被同时处理而重复推送
        self._lock = threading.Lock()
        self.status = {
            "runs": 0,
            "last_run": 0,
            "last_orders": 0,
            "pushed": 0,
            "last_error": "",
        }

    def stop(self):
        self._stop.set()

    def run_once(self):
        """执行一轮：拉取订单 → 检测新状态 → 推送。返回本轮成功推送数。"""
        with self._lock:
            return self._run()

    def backfill(self, hours):
        """补发最近 N 小时的订单（含已判定超时的）：忽略去重记录，消息标注「补发推送」。"""
        with self._lock:
            return self._run(backfill=True, window_hours=int(hours))

    def _run(self, backfill=False, window_hours=None):
        config = config_module.effective_config(self.store)
        problems = config_module.validate(config)
        if problems:
            self.status["last_error"] = "配置不完整：" + "；".join(problems)
            return 0

        source = SourceDB(config)
        now = int(time.time())
        window = int(window_hours or config["lookback_hours"])
        # 超过判定时长仍是 pending 的订单按超时处理，取数窗口不能小于判定时长
        expire_seconds = int(config["expire_minutes"]) * 60
        expire_before = now - expire_seconds if expire_seconds > 0 else None
        since = now - max(window * 3600, expire_seconds)
        orders = source.orders(since, limit=500, expired_before=expire_before)
        self.status["last_orders"] = len(orders)

        usernames = source.usernames([order["user_id"] for order in orders])
        plan_titles = source.plan_titles(
            [order["plan_id"] for order in orders if order["kind"] == "subscription"])

        initialized = self.store.get_state("initialized") == "1"
        # 首次运行且开着「回溯历史订单」时，存量订单同样算补发
        mark_backfill = backfill or ((not initialized) and bool(config["backfill"]))
        silent_first_run = (not initialized) and (not config["backfill"]) and not backfill
        mobiles = [m for m in (config["dingtalk_at_mobiles"] or "").split(",") if m.strip()]

        pushed = 0
        for order in orders:
            if not self._wanted(order, config):
                continue
            kind, trade_no, status = order["kind"], order["trade_no"], order["status"]
            if not backfill and self.store.is_pushed(kind, trade_no, status):
                continue

            if silent_first_run:
                # 首次运行默认只登记存量订单，不补推历史消息
                self.store.mark_pushed(kind, trade_no, status)
                continue

            username = usernames.get(int(order["user_id"] or 0)) or f"用户#{order['user_id']}"
            plan_title = plan_titles.get(int(order.get("plan_id") or 0), "")
            reason, reason_source = resolve_reason(order, config)
            title, text = render_message(order, username, plan_title, reason, reason_source,
                                         backfill=mark_backfill,
                                         large_threshold=config["large_order_threshold"])

            ok, detail = send_markdown(
                config["dingtalk_webhook"], title, text,
                secret=config["dingtalk_secret"], keyword=config["dingtalk_keyword"],
                at_mobiles=mobiles,
            )
            self.store.add_history({
                "kind": kind, "trade_no": trade_no, "status": status,
                "username": username, "money": order.get("money") or 0,
                "reason": reason, "reason_source": reason_source, "ok": ok, "detail": detail,
                "backfill": mark_backfill,
            })
            if ok:
                self.store.mark_pushed(kind, trade_no, status)
                pushed += 1
                self.log.info("已推送 %s %s %s", kind, trade_no, status)
            else:
                # 不标记已推送，下一轮自动重试
                self.log.warning("推送失败 %s %s：%s", trade_no, status, detail)

        if not initialized:
            self.store.set_state("initialized", "1")

        if not backfill:
            self.status["runs"] += 1
        self.status["pushed"] += pushed
        self.status["last_run"] = int(time.time())
        self.status["last_error"] = ""
        if backfill:
            self.log.info("补发推送完成：窗口 %s 小时，成功 %s 条", window, pushed)
        return pushed

    @staticmethod
    def _wanted(order, config):
        kind, status = order["kind"], order["status"]
        if status not in TERMINAL_STATUSES:
            return False
        if kind == "wallet" and not config["scope_wallet"]:
            return False
        if kind == "subscription" and not config["scope_subscription"]:
            return False
        if (status == "success"
                and float(order.get("money") or 0) < config.get("min_push_amount", 0)):
            return False
        return {
            "success": config["push_success"],
            "failed": config["push_failed"],
            "expired": config["push_expired"],
        }[status]

    def loop(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.status["last_error"] = str(exc)
                self.log.exception("轮询出错：%s", exc)
            config = config_module.effective_config(self.store)
            self._stop.wait(max(5, int(config["poll_interval"])))
