"""配置项定义、环境变量注入与校验。

配置优先级：环境变量 > 界面保存的配置 > 默认值。
环境变量只注入少数部署相关项（SQL_DSN / SQLITE_PATH / POLL_INTERVAL），
其余项在 Web 界面配置，保存在本程序自己的 SQLite 里。
"""

import os
import re

DATA_DIR = os.environ.get("DATA_DIR", "./data")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))
UI_BIND = os.environ.get("UI_BIND", "0.0.0.0")
# 由 deploy.sh 写入 .env，便于在界面确认当前跑的是哪个版本
VERSION = os.environ.get("APP_VERSION", "").strip() or "unknown"

DEFAULTS = {
    # 钉钉自定义机器人
    "dingtalk_webhook": "",
    "dingtalk_secret": "",
    "dingtalk_keyword": "",
    "dingtalk_at_mobiles": "",
    # new-api 数据源（只读）
    "db_type": "postgres",  # postgres | mysql | sqlite
    "db_dsn": "",
    "sqlite_path": "",
    # 推送控制
    "enabled": True,
    "poll_interval": 10,
    "push_success": True,
    "push_failed": True,
    "push_expired": True,
    # new-api 不会把超时未支付的订单写成 expired，这里按「创建后超过 N 分钟仍待支付」
    # 自行推导为超时；0 表示关闭该推导
    "expire_minutes": 30,
    "scope_wallet": True,
    "scope_subscription": True,
    "backfill": False,
    "lookback_hours": 48,
    # 每天 00:00 推送前一天的充值统计与排行榜
    "daily_report": True,
}

# 敏感项：界面只回显掩码，提交空值或掩码时保持已保存的值
SECRET_KEYS = ("db_dsn", "dingtalk_webhook", "dingtalk_secret")
MASK = "••••••••"
# 由环境变量注入、界面改不动的项（deploy.sh 写入 .env）
ENV_KEYS = ("db_type", "db_dsn", "sqlite_path", "poll_interval")

BOOL_KEYS = {
    "enabled", "push_success", "push_failed",
    "push_expired", "scope_wallet", "scope_subscription", "backfill", "daily_report",
}
INT_KEYS = {"poll_interval", "lookback_hours", "expire_minutes"}
STR_KEYS = {"dingtalk_webhook", "dingtalk_secret", "dingtalk_keyword",
            "dingtalk_at_mobiles", "db_type", "db_dsn", "sqlite_path"}

DB_TYPES = ("postgres", "mysql", "sqlite")

_DURATION_RE = re.compile(r"^(\d+)\s*(s|ms|m|h)?$")


def parse_duration(value, default_seconds):
    """解析 10 / 10s / 500ms / 2m 这类时长写法。"""
    if isinstance(value, (int, float)):
        return int(value)
    match = _DURATION_RE.match(str(value or "").strip())
    if not match:
        return default_seconds
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    if unit == "ms":
        return max(1, amount // 1000)
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    return amount


def infer_db_type(dsn):
    """从 SQL_DSN 推断数据库类型，兼容 new-api 常见写法。"""
    text = (dsn or "").strip()
    if text.startswith("postgres://") or text.startswith("postgresql://"):
        return "postgres"
    if text.startswith("mysql://") or "@tcp(" in text:
        return "mysql"
    return ""


def effective_config(store):
    """合并默认值、已保存配置与环境变量，返回生效配置。"""
    config = dict(DEFAULTS)
    for key, value in store.config_map().items():
        if key in DEFAULTS:
            config[key] = coerce(key, value)

    sql_dsn = os.environ.get("SQL_DSN", "").strip()
    if sql_dsn:
        config["db_dsn"] = sql_dsn
        db_type = infer_db_type(sql_dsn)
        if db_type:
            config["db_type"] = db_type
    sqlite_path = os.environ.get("SQLITE_PATH", "").strip()
    if sqlite_path:
        config["sqlite_path"] = sqlite_path
        config["db_type"] = "sqlite"
    if os.environ.get("POLL_INTERVAL"):
        config["poll_interval"] = parse_duration(
            os.environ["POLL_INTERVAL"], config["poll_interval"])
    return config


def mask_secrets(values):
    """把敏感项的值换成掩码（原本就是空值的仍为空串），用于界面回显。"""
    masked = dict(values)
    for key in SECRET_KEYS:
        if key in masked:
            masked[key] = MASK if str(masked[key] or "").strip() else ""
    return masked


def secret_flags(values):
    """敏感项是否已有真实值，界面据此提示「已保存，留空表示不修改」。"""
    return {key: bool(str(values.get(key) or "").strip()) for key in SECRET_KEYS}


def env_overrides():
    """返回被环境变量覆盖的键，界面据此给出提示。

    与 effective_config 的注入逻辑保持一致：只注入 SQL_DSN / SQLITE_PATH / POLL_INTERVAL。
    """
    keys = []
    sql_dsn = os.environ.get("SQL_DSN", "").strip()
    if sql_dsn:
        keys.append("db_dsn")
        if infer_db_type(sql_dsn):
            keys.append("db_type")
    if os.environ.get("SQLITE_PATH", "").strip():
        keys.extend(("sqlite_path", "db_type"))
    if os.environ.get("POLL_INTERVAL"):
        keys.append("poll_interval")
    return keys


def coerce(key, value):
    """把界面/环境传入的值转成正确的类型，非法值抛 ValueError。"""
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"{key} 需要是布尔值")
    if key in INT_KEYS:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key} 需要是整数") from None
        return number
    if key in STR_KEYS:
        return str(value).strip()
    return value


def validate(config):
    """整体校验，返回错误信息列表（空列表表示通过）。"""
    errors = []
    if config["db_type"] not in DB_TYPES:
        errors.append("数据源类型必须是 postgres / mysql / sqlite 之一")
    if config["db_type"] == "sqlite":
        if not config["sqlite_path"]:
            errors.append("SQLite 模式需要填写 new-api 的 .db 文件路径")
    elif not config["db_dsn"]:
        errors.append("PostgreSQL / MySQL 模式需要填写 SQL_DSN")
    if config["poll_interval"] < 5:
        errors.append("轮询间隔不能小于 5 秒")
    if config["poll_interval"] > 86400:
        errors.append("轮询间隔不能大于 86400 秒")
    if config["lookback_hours"] < 1:
        errors.append("回溯窗口不能小于 1 小时")
    if not 0 <= config["expire_minutes"] <= 10080:
        errors.append("超时判定分钟数需要在 0 ~ 10080 之间（0 表示关闭推导）")
    webhook = config["dingtalk_webhook"]
    if webhook and not webhook.startswith("https://"):
        # 仅本机回环地址放行 http，便于本地联调；其余必须是 https
        if not webhook.startswith(("http://127.0.0.1", "http://localhost")):
            errors.append("钉钉 Webhook 必须是 https:// 开头的地址")
    return errors
