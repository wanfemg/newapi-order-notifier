"""内嵌 Web 配置界面：口令登录 + REST 接口。

安全基线：口令用 PBKDF2-SHA256 加盐存散列；会话令牌放 HttpOnly + SameSite=Strict
Cookie 且仅在内存中保存；登录失败限速；写操作要求 JSON 与自定义请求头（CSRF）。
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as config_module
from notify import send_markdown
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
        if path == "/api/check":
            return self._check_source()
        if path == "/api/history/clear":
            self.server.store.clear_history()
            return self._json({"ok": True})
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
        body = self._read_json() or {}
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


def build_server(store, poller, reporter, bind, port):
    server = Server((bind, port), Handler)
    server.store = store
    server.poller = poller
    server.reporter = reporter
    server.sessions = Sessions()
    server.limiter = LoginLimiter()
    return server


def serve(store, poller, reporter, bind, port):
    build_server(store, poller, reporter, bind, port).serve_forever()
