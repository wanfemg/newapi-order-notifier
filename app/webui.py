"""内嵌 Web 配置界面：口令登录 + REST 接口。

安全基线：口令用 PBKDF2-SHA256 加盐存散列；会话令牌放 HttpOnly + SameSite=Strict
Cookie 且仅在内存中保存；登录失败限速；写操作要求 JSON 与自定义请求头（CSRF）。
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as config_module
import consumption as consumption_module
import finance as finance_module
import rates as rates_module
from notify import send_markdown
from poller import pay_method_label
from source import SourceDB

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web")
STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}

SESSION_COOKIE = "notifier_session"
SESSION_TTL = 12 * 3600
LOGIN_WINDOW = 300
LOGIN_MAX_ATTEMPTS = 8
PBKDF2_ROUNDS = 200_000
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "newapi-order-notifier"
MAX_BODY = 64 * 1024

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
HTML_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ROUNDS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password, stored):
    try:
        algorithm, rounds, salt_b64, digest_b64 = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
    return hmac.compare_digest(digest, expected)


def ensure_admin(store):
    """首次启动初始化管理口令。返回 (口令, 是否自动生成) 或 None。"""
    if store.admin_hash():
        return None
    password = os.environ.get("UI_PASSWORD", "").strip()
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
    if len(password) < 8:
        raise SystemExit("UI_PASSWORD 至少需要 8 位")
    store.set_admin_hash(hash_password(password))
    return password, generated


FLOW_STATUS_TEXT = {"success": "成功", "failed": "失败", "pending": "待支付"}
FLOW_BUCKETS = ("success", "failed", "pending")


def flow_bucket(status):
    """超时归入失败，未到阈值的待支付订单不视为失败。"""
    return "failed" if status == "expired" else (status or "pending")


def flow_status_text(status, derived):
    if status == "expired":
        return "失败·超时（推导）" if derived else "失败·超时"
    return FLOW_STATUS_TEXT.get(status, status)


def csv_cell(value):
    """阻止外部文本被电子表格解释为公式。"""
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


class Sessions:
    def __init__(self):
        self._lock = threading.Lock()
        self._tokens = {}

    def create(self):
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._expire()
            self._tokens[token] = time.time() + SESSION_TTL
        return token

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            self._expire()
            return token in self._tokens

    def drop(self, token):
        with self._lock:
            self._tokens.pop(token, None)

    def _expire(self):
        now = time.time()
        for token in [t for t, expiry in self._tokens.items() if expiry < now]:
            del self._tokens[token]


class LoginLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._hits = {}

    def allow(self, ip):
        now = time.time()
        with self._lock:
            recent = [t for t in self._hits.get(ip, []) if now - t < LOGIN_WINDOW]
            self._hits[ip] = recent
            return len(recent) < LOGIN_MAX_ATTEMPTS

    def record_failure(self, ip):
        with self._lock:
            self._hits.setdefault(ip, []).append(time.time())

    def clear(self, ip):
        with self._lock:
            self._hits.pop(ip, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "newapi-order-notifier"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        logging.getLogger("web").info("%s %s", self.address_string(), fmt % args)

    # ---------- 路由 ----------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self._send_bytes(b"ok", "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            return self._serve_static("index.html", html=True)
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/session":
            return self._json({"authed": self._cookie_authed()})
        if not self._require_auth():
            return
        if path in ("/api/finance/summary", "/api/finance/trend", "/api/finance/ranking",
                    "/api/finance/users", "/api/orders", "/api/orders/export.csv",
                    "/api/consumption"):
            try:
                self._period = self._range_window()
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        if path == "/api/channel-finance/backfill":
            return self._channel_backfill_preview()
        if path == "/api/config":
            current = config_module.effective_config(self.server.store)
            return self._json({
                # 敏感项只回显占位文案，真实值留在服务端
                "config": config_module.mask_secrets(current),
                "secrets": config_module.secret_flags(current),
                "env_overridden": config_module.env_overrides(),
                "warnings": config_module.validate(current),
            })
        if path == "/api/status":
            return self._json(self._status())
        if path == "/api/history":
            return self._json({"items": self.server.store.history(200)})
        if path == "/api/finance/summary":
            return self._finance_summary()
        if path == "/api/finance/trend":
            return self._finance_trend()
        if path == "/api/finance/ranking":
            return self._finance_ranking()
        if path == "/api/finance/users":
            return self._finance_users()
        if path == "/api/reports":
            config = config_module.effective_config(self.server.store)
            return self._json({
                "daily_report": config["daily_report"],
                "weekly_report": config["weekly_report"],
                "daily_last": self.server.store.get_state("daily_report_last"),
                "weekly_last": self.server.store.get_state("weekly_report_last"),
                "consume_daily_report_enabled": config["consume_daily_report_enabled"],
                "consume_daily_report_time": config["consume_daily_report_time"],
                "consume_daily_runs": self.server.store.daily_report_runs(),
            })
        if path == "/api/orders":
            return self._orders()
        if path == "/api/orders/export.csv":
            return self._orders_export()
        if path == "/api/site-rate":
            return self._json({"current": self.server.store.find_site_rate(),
                               "history": self.server.store.site_rate_history()})
        if path == "/api/channel-finance":
            return self._channel_finance()
        if path == "/api/channel-finance/history":
            return self._channel_finance_history()
        if path == "/api/channels":
            return self._channels()
        if path == "/api/consumption":
            return self._consumption()
        return self._json({"error": "接口不存在"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/login":
            return self._login()
        if not self._require_auth():
            return
        if not self._csrf_ok():
            return self._json({"error": "缺少合法的请求头"}, 403)
        if path == "/api/logout":
            self.server.sessions.drop(self._session_token())
            return self._json({"ok": True}, cookie="")
        if path == "/api/config":
            return self._save_config()
        if path == "/api/test":
            return self._test_dingtalk()
        if path == "/api/backfill":
            return self._backfill()
        if path == "/api/daily-report":
            return self._daily_report()
        if path == "/api/weekly-report":
            return self._weekly_report()
        if path == "/api/check":
            return self._check_source()
        if path == "/api/history/clear":
            self.server.store.clear_history()
            return self._json({"ok": True})
        if path == "/api/site-rate":
            body = self._read_json()
            try:
                if not isinstance(body, dict):
                    raise ValueError("站点比例必须是对象")
                self.server.store.save_site_rate(body.get("rate"), body.get("note", ""))
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            return self._json({"ok": True})
        if path == "/api/channel-finance":
            return self._save_channel_finance()
        if path == "/api/channel-finance/backfill":
            return self._save_channel_backfill()
        if path == "/api/channel-finance/delete":
            return self._delete_channel_finance()
        return self._json({"error": "接口不存在"}, 404)

    # ---------- 业务 ----------

    def _login(self):
        ip = self.client_address[0]
        if not self.server.limiter.allow(ip):
            logging.warning("登录请求过多被拒 ip=%s", ip)
            return self._json({"error": "尝试过于频繁，请稍后再试"}, 429)
        body = self._read_json()
        password = str((body or {}).get("password") or "")
        if not verify_password(password, self.server.store.admin_hash()):
            self.server.limiter.record_failure(ip)
            logging.warning("管理口令校验失败 ip=%s", ip)
            return self._json({"error": "口令错误"}, 401)
        self.server.limiter.clear(ip)
        token = self.server.sessions.create()
        logging.info("管理界面登录成功 ip=%s", ip)
        return self._json({"ok": True}, cookie=token)

    def _save_config(self):
        body = self._read_json()
        if not isinstance(body, dict):
            return self._json({"error": "配置必须是 JSON 对象"}, 400)
        incoming = body.get("config") if isinstance(body.get("config"), dict) else body
        values = {}
        for key, value in incoming.items():
            if key not in config_module.DEFAULTS:
                continue
            try:
                coerced = config_module.coerce(key, value)
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
            # 敏感项提交的是占位文案或空值时说明用户没打算改，保持已保存的值
            if key in config_module.SECRET_KEYS and coerced in ("", config_module.MASK):
                continue
            values[key] = coerced
        if not values:
            return self._json({"error": "没有可保存的配置项"}, 400)
        self.server.store.set_configs(values)
        current = config_module.effective_config(self.server.store)
        return self._json({"ok": True, "warnings": config_module.validate(current)})

    def _test_dingtalk(self):
        config = config_module.apply_overrides(
            config_module.effective_config(self.server.store), self._form_overrides())
        if not config["dingtalk_webhook"]:
            return self._json({"error": "请先填写并保存钉钉 Webhook"}, 400)
        text = (
            "### 🔔 测试消息\n"
            "- **来源**：new-api 订单通知服务\n"
            f"- **时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            "- **说明**：收到这条消息说明钉钉推送配置正常"
        )
        ok, detail = send_markdown(
            config["dingtalk_webhook"], "🔔 测试消息", text,
            secret=config["dingtalk_secret"], keyword=config["dingtalk_keyword"],
            at_mobiles=[m for m in (config["dingtalk_at_mobiles"] or "").split(",") if m.strip()],
        )
        return self._json({"ok": ok, "detail": detail}, 200 if ok else 400)

    def _check_source(self):
        try:
            detail = SourceDB(config_module.effective_config(self.server.store)).ping()
        except Exception as exc:
            return self._json({"ok": False, "detail": str(exc)}, 400)
        return self._json({"ok": True, "detail": detail})

    # ---------- 计价比例 / 渠道成本 / 消耗统计 ----------

    def _channel_finance(self):
        config = config_module.effective_config(self.server.store)
        try:
            channels = SourceDB(config).channels()
        except Exception as exc:
            return self._json({"error": f"读取渠道失败：{exc}"}, 500)
        return self._json({
            "items": self.server.store.list_channel_finance(),
            "history": self.server.store.list_channel_finance_history(),
            "overrides": self.server.store.finance_overrides(),
            "channels": channels,
        })

    @staticmethod
    def _channel_id(value):
        text = str(value)
        if isinstance(value, bool) or not text.isascii() or not text.isdigit() or int(text) <= 0:
            raise ValueError("channel_id 必须是正整数")
        return int(text)

    def _backfill_preview(self, channel_id):
        store = self.server.store
        result = {"eligible": False, "start": 0, "end": 0,
                  "message": "请先配置当前站点比例和该渠道 CNY 比例"}
        snapshot = store.current_channel_snapshot(channel_id)
        site = store.find_site_rate()
        if not snapshot or snapshot["fee_currency"] != "CNY" or not site:
            return result, None
        channel_rate = str(rates_module.parse_rate(snapshot["fee_rate"]))
        site_rate = str(rates_module.parse_rate(site["rate"]))
        end = int(time.time())
        source = SourceDB(config_module.effective_config(store))
        summary = source.channel_consumption_summary(channel_id, end)
        if summary["first_at"] is None:
            result["message"] = "该渠道没有可重算的过去消耗日志"
            return result, snapshot
        quota = int(summary["quota"])
        result.update(eligible=True, channel_id=channel_id, start=0, end=end,
                      earliest_log=int(summary["first_at"]),
                      quota=quota, website_units=rates_module.fmt_money(
                          rates_module.Decimal(quota) / consumption_module.QUOTA_PER_UNIT),
                      channel_snapshot_id=snapshot["id"], site_snapshot_id=site["id"],
                      channel_rate=channel_rate, site_rate=site_rate,
                      message="冻结当前两个比例覆盖该渠道全部过去日志，包含旧快照和删除空档；原快照及源日志不修改，未来仍按历史计价。")
        result["preview_token"] = store.save_finance_preview(result)
        return result, snapshot

    def _channel_backfill_preview(self):
        try:
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(self.path).query, keep_blank_values=True)
            ids = query.get("channel_id", [])
            if len(ids) != 1:
                raise ValueError("channel_id 必须是单个正整数")
            channel_id = self._channel_id(ids[0])
            result, _ = self._backfill_preview(channel_id)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": f"预览历史补算失败：{exc}"}, 500)
        return self._json(result)

    def _save_channel_backfill(self):
        try:
            body = self._read_json()
            if not isinstance(body, dict) or body.get("confirmed") is not True:
                raise ValueError("历史补算必须明确 confirmed: true")
            channel_id = self._channel_id(body.get("channel_id"))
            self.server.store.confirm_finance_preview(body.get("preview_token"), channel_id)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": f"历史补算失败：{exc}"}, 500)
        return self._json({"message": "全量历史重算规则已保存，查询和推送立即使用；原快照及源日志保留"})

    def _channel_finance_history(self):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True)
        channel_id = None
        if "channel_id" in query:
            try:
                if len(query["channel_id"]) != 1:
                    raise ValueError("channel_id 必须是单个正整数")
                channel_id = self._channel_id(query["channel_id"][0])
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)
        return self._json({
            "items": self.server.store.list_channel_finance_history(channel_id),
            "overrides": self.server.store.finance_overrides(channel_id),
        })

    def _save_channel_finance(self):
        body = self._read_json()
        try:
            item = rates_module.save_channel_finance(self.server.store, body)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "item": item})

    def _delete_channel_finance(self):
        body = self._read_json()
        try:
            if not isinstance(body, dict):
                raise ValueError("渠道配置必须是对象")
            channel_id = self._channel_id(body.get("channel_id"))
            self.server.store.delete_channel_finance(channel_id)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        return self._json({"ok": True, "channel_id": channel_id})

    def _channels(self):
        config = config_module.effective_config(self.server.store)
        try:
            channels = SourceDB(config).channels()
        except Exception as exc:
            return self._json({"error": f"读取渠道失败：{exc}"}, 500)
        costs = {int(row["channel_id"]): row for row in self.server.store.channel_costs()}
        for channel in channels:
            cost = costs.get(int(channel["id"]))
            channel["cost_per_unit"] = cost["cost_per_unit"] if cost else ""
            channel["cost_currency"] = cost["currency"] if cost else "CNY"
        return self._json({"channels": channels})

    def _consumption(self):
        days = self._qint("days", 7, 1, 365)
        config = config_module.effective_config(self.server.store)
        start, end = self._period
        try:
            stats = consumption_module.compute(
                self.server.store, SourceDB(config), int(start), int(end),
                config["rate_currency"])
        except Exception as exc:
            return self._json({"error": f"读取消耗数据失败：{exc}"}, 500)
        return self._json(stats)

    def _status(self):
        status = dict(self.server.poller.status)
        status["pushed_total"] = self.server.store.pushed_count()
        status["version"] = config_module.VERSION
        status["warnings"] = config_module.validate(
            config_module.effective_config(self.server.store))
        return status

    def _backfill(self):
        """补发最近 N 小时的订单：忽略去重记录，消息标注「补发推送」。"""
        body = self._read_json() or {}
        try:
            hours = int(body.get("hours", 24))
        except (TypeError, ValueError):
            return self._json({"error": "回溯小时数需要是整数"}, 400)
        if not 1 <= hours <= 720:
            return self._json({"error": "回溯小时数需要在 1 ~ 720 之间"}, 400)
        config = config_module.effective_config(self.server.store)
        problems = config_module.validate(config)
        if problems:
            return self._json({"error": "配置不完整：" + "；".join(problems)}, 400)
        if not config["dingtalk_webhook"]:
            return self._json({"error": "请先填写并保存钉钉 Webhook"}, 400)
        count = self.server.poller.backfill(hours)
        return self._json({"ok": True, "count": count, "hours": hours})

    def _daily_report(self):
        """手动生成一份今天的充值统计（不受开关与去重限制），便于验证。"""
        try:
            ok, detail = self.server.reporter.run_daily(
                offset_days=0, force=True, overrides=self._form_overrides())
        except Exception as exc:
            return self._json({"ok": False, "detail": str(exc)}, 400)
        return self._json({"ok": ok, "detail": detail}, 200 if ok else 400)

    def _weekly_report(self):
        """手动推送本周截至当前的统计，不改变自动报告状态。"""
        try:
            ok, detail = self.server.reporter.run_weekly(
                offset_weeks=0, force=True, overrides=self._form_overrides())
        except Exception as exc:
            return self._json({"ok": False, "detail": str(exc)}, 400)
        return self._json({"ok": ok, "detail": detail}, 200 if ok else 400)

    # ---------- 财务看板 ----------

    def _query(self):
        return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))

    def _qint(self, name, default, minimum, maximum):
        raw = self._query().get(name, "")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    def _range_window(self):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query, keep_blank_values=True)
        if "start" not in query and "end" not in query:
            return finance_module.range_window(self._qint("days", 7, 1, 365))
        values = []
        for key in ("start", "end"):
            parts = query.get(key, [])
            if (len(parts) != 1 or not parts[0].isascii() or not parts[0].isdigit()
                    or not 0 < int(parts[0]) <= 253402300799):
                raise ValueError("start/end 必须成对提供单个正整数 Unix 秒")
            values.append(int(parts[0]))
        if values[1] <= values[0]:
            raise ValueError("end 必须大于 start")
        return tuple(values)

    def _finance_success(self, days):
        """窗口内的成功订单行，供概览/趋势/排行复用。"""
        start, end = self._period
        source = SourceDB(config_module.effective_config(self.server.store))
        return source.success_orders(int(start), int(end))

    def _finance_summary(self):
        days = self._qint("days", 7, 1, 365)
        try:
            rows = self._finance_success(days)
        except Exception as exc:
            return self._json({"error": f"读取财务数据失败：{exc}"}, 500)
        today_start, _ = finance_module.range_window(1)
        today_rows = [row for row in rows if int(row.get("create_time") or 0) >= today_start]
        return self._json({
            "days": days,
            "total": finance_module.totals(rows),
            "today": finance_module.totals(today_rows),
            "breakdown": finance_module.breakdown(rows),
            "methods": finance_module.methods(rows),
        })

    def _finance_trend(self):
        days = self._qint("days", 7, 1, 365)
        try:
            rows = self._finance_success(days)
            return self._json(finance_module.trend(rows, days, *self._period))
        except Exception as exc:
            return self._json({"error": f"读取财务数据失败：{exc}"}, 500)

    def _finance_ranking(self):
        days = self._qint("days", 7, 1, 365)
        limit = self._qint("limit", 10, 1, 50)
        try:
            rows = self._finance_success(days)
            names = SourceDB(config_module.effective_config(self.server.store)).usernames(
                [row["user_id"] for row in rows])
        except Exception as exc:
            return self._json({"error": f"读取财务数据失败：{exc}"}, 500)
        return self._json({"days": days, "items": finance_module.ranking(rows, names, limit)})

    def _finance_users(self):
        days = self._qint("days", 7, 1, 365)
        try:
            rows = self._finance_success(days)
            names = SourceDB(config_module.effective_config(self.server.store)).usernames(
                [row["user_id"] for row in rows])
        except Exception as exc:
            return self._json({"error": f"读取财务数据失败：{exc}"}, 500)
        config = config_module.effective_config(self.server.store)
        payload = {
            "days": days, "items": finance_module.users(rows, names),
            "consumption_users": [], "currency": config["rate_currency"],
            "consumption_error": "",
        }
        start, end = self._period
        try:
            stats = consumption_module.compute(
                self.server.store, SourceDB(config), int(start), int(end),
                config["rate_currency"])
            payload["consumption_users"] = stats["users"]
            payload["currency"] = stats["currency"]
        except Exception as exc:
            payload["consumption_error"] = f"读取消耗数据失败：{exc}"
        return self._json(payload)

    def _flow_rows(self, days, limit, buckets):
        """窗口内的流水订单，超时未支付的 pending 按 expire_minutes 推导为超时。"""
        start, end = self._period
        config = config_module.effective_config(self.server.store)
        expire_minutes = int(config["expire_minutes"] or 0)
        expire_before = int(time.time()) - expire_minutes * 60 if expire_minutes > 0 else None
        source = SourceDB(config)
        rows = source.window_orders(int(start), int(end), limit, buckets, expire_before)
        for row in rows:
            if (row.get("status") == "pending" and expire_before is not None
                    and int(row.get("create_time") or 0) <= expire_before):
                row["status"] = "expired"
                row["expired_derived"] = True
        return rows

    def _usernames(self, rows, config):
        try:
            return SourceDB(config).usernames([row["user_id"] for row in rows])
        except Exception as exc:
            logging.warning("读取用户名失败：%s", exc)
            return {}

    def _order_item(self, row, bucket, names):
        uid = row.get("user_id")
        return {
            "kind": row.get("kind"),
            "trade_no": row.get("trade_no") or "-",
            "user_id": uid,
            "username": names.get(int(uid)) if uid is not None else None,
            "money": round(float(row.get("money") or 0), 2),
            "method": pay_method_label(row.get("payment_method"), row.get("payment_provider")),
            "bucket": bucket,
            "status": row.get("status"),
            "derived": bool(row.get("expired_derived")),
            "create_time": row.get("create_time"),
            "complete_time": row.get("complete_time"),
            "plan_id": row.get("plan_id"),
        }

    def _order_filters(self):
        raw_status = self._query().get("status", "success,failed")
        return {token for token in raw_status.split(",") if token in FLOW_BUCKETS}

    def _orders(self):
        days = self._qint("days", 7, 1, 365)
        limit = self._qint("limit", 500, 1, 2000)
        wanted = self._order_filters()
        try:
            rows = self._flow_rows(days, limit + 1, wanted)
            has_more = len(rows) > limit
            rows = rows[:limit]
            config = config_module.effective_config(self.server.store)
            names = self._usernames(rows, config)
            items = [
                self._order_item(row, flow_bucket(row.get("status")), names)
                for row in rows
                if flow_bucket(row.get("status")) in wanted
            ]
        except Exception as exc:
            return self._json({"error": f"读取订单失败：{exc}"}, 500)
        return self._json({"days": days, "items": items, "has_more": has_more})

    def _orders_export(self):
        days = self._qint("days", 7, 1, 365)
        limit = self._qint("limit", 2000, 1, 10000)
        try:
            rows = self._flow_rows(days, limit + 1, self._order_filters())
            if len(rows) > limit:
                return self._json({"error": "订单超出导出上限，请缩小日期范围后重试"}, 400)
            config = config_module.effective_config(self.server.store)
            names = self._usernames(rows, config)
        except Exception as exc:
            return self._json({"error": f"导出订单失败：{exc}"}, 500)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["时间", "状态", "订单号", "用户", "金额", "支付方式", "类型"])
        for row in rows:
            bucket = flow_bucket(row.get("status"))
            uid = row.get("user_id")
            username = names.get(int(uid)) or f"用户#{uid}" if uid is not None else "-"
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(int(row.get("create_time") or 0))),
                flow_status_text(row.get("status"), bool(row.get("expired_derived"))),
                csv_cell(row.get("trade_no") or "-"),
                csv_cell(username),
                f"{float(row.get('money') or 0):.2f}",
                csv_cell(pay_method_label(row.get("payment_method"), row.get("payment_provider"))),
                "钱包充值" if row.get("kind") == "wallet" else "订阅开通",
            ])
        body = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        extra = {"Content-Disposition": "attachment; filename=orders.csv"}
        return self._send_bytes(body, "text/csv; charset=utf-8", extra)

    def _form_overrides(self):
        """读请求体里的钉钉推送项，供测试发送 / 生成统计即时使用（未保存也生效）。"""
        body = self._read_json() or {}
        incoming = body.get("config") if isinstance(body.get("config"), dict) else body
        return {key: incoming[key] for key in config_module.DINGTALK_KEYS if key in incoming}

    # ---------- 基础设施 ----------

    def _session_token(self):
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def _cookie_authed(self):
        return self.server.sessions.valid(self._session_token())

    def _require_auth(self):
        if self._cookie_authed():
            return True
        self._json({"error": "未登录"}, 401)
        return False

    def _csrf_ok(self):
        if self.headers.get(CSRF_HEADER, "") != CSRF_VALUE:
            return False
        return "application/json" in (self.headers.get("Content-Type", "").lower())

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0 or length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _json(self, payload, status=200, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if cookie is not None:
            if cookie:
                headers["Set-Cookie"] = (
                    f"{SESSION_COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age={SESSION_TTL}")
            else:
                headers["Set-Cookie"] = (
                    f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        self._send_bytes(body, headers.pop("Content-Type"), headers, status=status)

    def _send_bytes(self, body, content_type, extra=None, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, name, html=False):
        content_type = STATIC_TYPES.get(name)
        path = os.path.join(WEB_DIR, name)
        if not content_type or not os.path.isfile(path):
            return self._send_bytes(b"not found", "text/plain; charset=utf-8", status=404)
        with open(path, "rb") as handle:
            body = handle.read()
        extra = {"Content-Security-Policy": HTML_CSP} if html else None
        self._send_bytes(body, content_type, extra)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def build_server(store, poller, reporter, monitor, bind, port):
    server = Server((bind, port), Handler)
    server.store = store
    server.poller = poller
    server.reporter = reporter
    server.monitor = monitor
    server.sessions = Sessions()
    server.limiter = LoginLimiter()
    return server


def serve(store, poller, reporter, monitor, bind, port):
    build_server(store, poller, reporter, monitor, bind, port).serve_forever()
