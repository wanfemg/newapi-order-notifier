"""配置项定义、环境变量注入与校验。

配置优先级：环境变量 > 界面保存的配置 > 默认值。
环境变量只注入少数部署相关项（SQL_DSN / SQLITE_PATH / POLL_INTERVAL），
其余项在 Web 界面配置，保存在本程序自己的 SQLite 里。
"""

import math
import os
import re

DATA_DIR = os.environ.get("DATA_DIR", "./data")
UI_PORT = int(os.environ.get("UI_PORT", "8080"))
UI_BIND = os.environ.get("UI_BIND", "0.0.0.0")
# 由 deploy.sh 写入 .env，便于在界面确认当前跑的是哪个版本
VERSION = os.environ.get("APP_VERSION", "").strip() or "1.1.2-max"

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
    "large_order_threshold": 50.0,
    "min_push_amount": 0.0,
    # new-api 不会把超时未支付的订单写成 expired，这里按「创建后超过 N 分钟仍待支付」
    # 自行推导为超时；0 表示关闭该推导。订单判定与推送文案共用这一项，改一处两处同步生效
    "expire_minutes": 5,
    "scope_wallet": True,
    "scope_subscription": True,
    "backfill": False,
    "lookback_hours": 48,
    # 每天 00:00 推送前一天的充值统计与排行榜
    "daily_report": True,
    "weekly_report": True,
    # 站点充值汇率与消耗统计（只读 new-api，成本单价与汇率存本程序自己的库）
    "rate_currency": "CNY",   # 用户消耗金额的计价货币（1 rate_currency = X 额度）
    "consume_daily_report_enabled": False,
    "consume_daily_report_time": "09:00",
    "consume_enabled": False,  # 是否开启消耗统计自动通知
    "consume_interval": 600,   # 消耗统计检查间隔（秒）
    "consume_threshold_quota": 0,  # 累计额度达到该值触发阈值通知，0 表示关闭
    "consume_change_quota": 0,     # 单次新增额度达到该值触发变动通知，0 表示关闭
}

# 敏感项：界面只回显占位文案，提交该文案或空值时保持已保存的值
SECRET_KEYS = ("db_dsn", "dingtalk_webhook", "dingtalk_secret")
MASK = "内容被猫猫大王吃掉了"
# 由环境变量注入、界面改不动的项（deploy.sh 写入 .env）
ENV_KEYS = ("db_type", "db_dsn", "sqlite_path", "poll_interval")

BOOL_KEYS = {
    "enabled", "push_success", "push_failed",
    "push_expired", "scope_wallet", "scope_subscription", "backfill", "daily_report",
    "weekly_report", "consume_enabled", "consume_daily_report_enabled",
}
INT_KEYS = {"poll_interval", "lookback_hours", "expire_minutes", "consume_interval",
            "consume_threshold_quota", "consume_change_quota"}
FLOAT_KEYS = {"large_order_threshold", "min_push_amount"}
STR_KEYS = {"dingtalk_webhook", "dingtalk_secret", "dingtalk_keyword",
            "dingtalk_at_mobiles", "db_type", "db_dsn", "sqlite_path", "rate_currency",
            "consume_daily_report_time"}

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
    """把敏感项的值换成占位文案（原本就是空值的仍为空串），用于界面回显。"""
    masked = dict(values)
    for key in SECRET_KEYS:
        if key in masked:
            masked[key] = MASK if str(masked[key] or "").strip() else ""
    return masked


def secret_flags(values):
    """敏感项是否已有真实值，界面据此提示「已保存，留空表示不修改」。"""
    return {key: bool(str(values.get(key) or "").strip()) for key in SECRET_KEYS}


# 界面「测试发送」「立即生成统计」会用当前表单里的钉钉推送项做即时验证
DINGTALK_KEYS = ("dingtalk_webhook", "dingtalk_secret", "dingtalk_keyword", "dingtalk_at_mobiles")


def apply_overrides(config, overrides):
    """把界面上还没保存的钉钉推送项合并进生效配置。

    敏感项为空值或占位文案时继续用已保存的值；其余项直接采用表单里的值。
    这样用户填了 Webhook 还没点保存，也能先测试发送 / 生成统计。
    """
    if not overrides:
        return config
    merged = dict(config)
    for key in DINGTALK_KEYS:
        if key not in overrides:
            continue
        text = str(overrides.get(key) or "").strip()
        if key in SECRET_KEYS and text in ("", MASK):
            continue
        merged[key] = text
    return merged


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
        if key in ("consume_threshold_quota", "consume_change_quota") and number < 0:
            raise ValueError(f"{key} 需要是非负整数")
        return number
    if key in FLOAT_KEYS:
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{key} 需要是非负有限金额") from None
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{key} 需要是非负有限金额")
        return number
    if key == "consume_daily_report_time":
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError(f"{key} 需要是 HH:MM 格式的有效时间（00:00 ~ 23:59）")
        return value
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
    if not config["rate_currency"]:
        errors.append("汇率基准货币不能为空")
    if not 10 <= config["consume_interval"] <= 86400:
        errors.append("消耗统计检查间隔需要在 10 ~ 86400 秒之间")
    for key in (*FLOAT_KEYS, "consume_daily_report_time",
                "consume_threshold_quota", "consume_change_quota"):
        try:
            coerce(key, config[key])
        except ValueError as exc:
            errors.append(str(exc))
    webhook = config["dingtalk_webhook"]
    if webhook and not webhook.startswith("https://"):
        # 仅本机回环地址放行 http，便于本地联调；其余必须是 https
        if not webhook.startswith(("http://127.0.0.1", "http://localhost")):
            errors.append("钉钉 Webhook 必须是 https:// 开头的地址")
    return errors
