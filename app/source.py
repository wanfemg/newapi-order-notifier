"""只读访问 new-api 数据库：支付订单、用户名、订阅方案名。

new-api 的两张订单表：
  top_ups(id, user_id, amount, money, trade_no, payment_method, payment_provider,
          create_time, complete_time, status)
  subscription_orders(id, user_id, plan_id, money, trade_no, payment_method,
          payment_provider, status, create_time, complete_time, provider_payload)
状态取值：pending / success / failed / expired（两张表共用）。
"""

import os
import re
import sqlite3
import urllib.parse

TERMINAL_STATUSES = ("success", "failed", "expired")


class SourceError(Exception):
    pass


def parse_dsn(dsn):
    """解析 new-api 的 SQL_DSN，兼容 postgresql:// 与 mysql 的两种常见写法。"""
    text = (dsn or "").strip()
    if text.startswith(("postgres://", "postgresql://")):
        parsed = urllib.parse.urlparse(text)
        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
            "user": urllib.parse.unquote(parsed.username or ""),
            "password": urllib.parse.unquote(parsed.password or ""),
            "dbname": (parsed.path or "/").lstrip("/"),
            "params": dict(urllib.parse.parse_qsl(parsed.query)),
        }
    if text.startswith("mysql://"):
        parsed = urllib.parse.urlparse(text)
        return {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 3306,
            "user": urllib.parse.unquote(parsed.username or ""),
            "password": urllib.parse.unquote(parsed.password or ""),
            "dbname": (parsed.path or "/").lstrip("/"),
            "params": dict(urllib.parse.parse_qsl(parsed.query)),
        }
    # PostgreSQL 关键字写法：host=... port=... user=... password=... dbname=...
    if "://" not in text and re.search(r"(^|\s)host=", text):
        info = {"host": "localhost", "port": 5432, "user": "", "password": "",
                "dbname": "", "params": {}}
        for token in text.split():
            key, _, value = token.partition("=")
            if key in ("host", "user", "password", "dbname"):
                info[key] = value
            elif key == "database":
                info["dbname"] = value
            elif key == "port":
                info["port"] = int(value)
            elif key:
                info["params"][key] = value  # 如 TimeZone / sslmode 原样透传
        if info["dbname"]:
            return info

    match = re.match(
        r"^(?P<user>[^:@/]*)(:(?P<password>[^@]*))?@tcp\((?P<host>[^:)]+)"
        r"(:(?P<port>\d+))?\)/(?P<dbname>[^?]*)(\?(?P<query>.*))?$",
        text,
    )
    if match:
        return {
            "host": match.group("host"),
            "port": int(match.group("port") or 3306),
            "user": match.group("user") or "root",
            "password": match.group("password") or "",
            "dbname": match.group("dbname") or "",
            "params": dict(urllib.parse.parse_qsl(match.group("query") or "")),
        }
    raise SourceError(
        "无法解析 SQL_DSN，请使用 postgresql://user:pass@host:5432/dbname "
        "或 user:pass@tcp(host:3306)/dbname"
    )


class SourceDB:
    """只执行 SELECT，绝不写入 new-api 的库。"""

    def __init__(self, config):
        self.db_type = config["db_type"]
        self.dsn = config["db_dsn"]
        self.sqlite_path = config["sqlite_path"]

    @property
    def placeholder(self):
        return "?" if self.db_type == "sqlite" else "%s"

    def _connect(self):
        if self.db_type == "sqlite":
            if not self.sqlite_path or not os.path.exists(self.sqlite_path):
                raise SourceError(f"new-api 的 SQLite 文件不存在：{self.sqlite_path}")
            conn = sqlite3.connect(f"file:{self.sqlite_path}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn

        info = parse_dsn(self.dsn)
        if self.db_type == "postgres":
            try:
                import psycopg2
            except ImportError:
                raise SourceError("缺少 psycopg2 依赖，请安装 requirements.txt") from None
            conn = psycopg2.connect(
                host=info["host"], port=info["port"], user=info["user"],
                password=info["password"], dbname=info["dbname"],
                connect_timeout=5, **info["params"],
            )
            conn.set_session(readonly=True)
            return conn

        if self.db_type == "mysql":
            try:
                import pymysql
            except ImportError:
                raise SourceError("缺少 PyMySQL 依赖，请安装 requirements.txt") from None
            params = {"charset": "utf8mb4", "connect_timeout": 5}
            params.update(info["params"])
            return pymysql.connect(
                host=info["host"], port=info["port"], user=info["user"],
                password=info["password"], database=info["dbname"], **params,
            )

        raise SourceError(f"不支持的数据源类型：{self.db_type}")

    def _fetch(self, sql, args):
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, args)
            names = [column[0] for column in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        finally:
            conn.close()

    def ping(self):
        """连接自检，返回一句可展示的说明。"""
        rows = self._fetch("SELECT 1 AS ok", ())
        if not rows:
            raise SourceError("连接成功但查询无返回")
        return f"{self.db_type} 连接正常"

    def orders(self, since_ts, limit=500, expired_before=None):
        """按创建时间窗口取订单。

        正常只取已出结果的（success/failed/expired）。传了 expired_before 时，额外把
        创建时间早于它、状态仍停在 pending 的订单一起取出来，并在返回时标成 expired：
        new-api 没有任何后台任务把「超时未支付」的订单写成 expired（只有 Stripe 会话
        超时回调等少数路径会写），易支付等渠道超时后库里会一直是 pending。
        """
        p = self.placeholder
        statuses = ",".join([p] * len(TERMINAL_STATUSES))
        pending_clause = ""
        pending_args = []
        if expired_before is not None:
            pending_clause = f" OR (status = {p} AND create_time <= {p})"
            pending_args = ["pending", int(expired_before)]
        wallet = (
            "SELECT 'wallet' AS kind, id, user_id, money, amount, trade_no, "
            "payment_method, payment_provider, status, create_time, complete_time, "
            "0 AS plan_id, '' AS provider_payload FROM top_ups "
            f"WHERE create_time >= {p} AND (status IN ({statuses}){pending_clause})"
        )
        subscription = (
            "SELECT 'subscription' AS kind, id, user_id, money, 0 AS amount, trade_no, "
            "payment_method, payment_provider, status, create_time, complete_time, "
            "plan_id, provider_payload FROM subscription_orders "
            f"WHERE create_time >= {p} AND (status IN ({statuses}){pending_clause})"
        )
        sql = (
            f"SELECT * FROM ({wallet} UNION ALL {subscription}) AS orders "
            f"ORDER BY create_time DESC LIMIT {p}"
        )
        args = [since_ts, *TERMINAL_STATUSES, *pending_args,
                since_ts, *TERMINAL_STATUSES, *pending_args, int(limit)]
        rows = self._fetch(sql, args)
        for row in rows:
            if row.get("status") == "pending":
                row["status"] = "expired"
                row["expired_derived"] = True
        return rows

    def daily_recharge(self, start_ts, end_ts):
        """某个自然日窗口内的成功充值，按用户汇总（金额倒序）。

        口径：钱包充值（top_ups）+ 订阅开通（subscription_orders）两条线的成功单都算，
        失败 / 过期 / 待支付的订单不计入。
        """
        p = self.placeholder
        wallet = "SELECT user_id, money, status, create_time FROM top_ups"
        subscription = "SELECT user_id, money, status, create_time FROM subscription_orders"
        return self._fetch(
            f"SELECT user_id, SUM(money) AS money, COUNT(*) AS orders FROM "
            f"({wallet} UNION ALL {subscription}) AS paid "
            f"WHERE status = {p} AND create_time >= {p} AND create_time < {p} "
            "GROUP BY user_id ORDER BY money DESC",
            ("success", int(start_ts), int(end_ts)),
        )

    def success_orders(self, start_ts, end_ts):
        """窗口内的成功订单原始行（钱包 + 订阅），供财务看板聚合用。"""
        p = self.placeholder
        wallet = (
            "SELECT 'wallet' AS kind, user_id, money, payment_method, payment_provider, "
            "create_time, trade_no FROM top_ups "
            f"WHERE status = {p} AND create_time >= {p} AND create_time < {p}"
        )
        subscription = (
            "SELECT 'subscription' AS kind, user_id, money, payment_method, "
            "payment_provider, create_time, trade_no FROM subscription_orders "
            f"WHERE status = {p} AND create_time >= {p} AND create_time < {p}"
        )
        return self._fetch(
            f"SELECT * FROM ({wallet} UNION ALL {subscription}) AS paid "
            f"ORDER BY create_time DESC",
            ("success", int(start_ts), int(end_ts),
             "success", int(start_ts), int(end_ts)),
        )

    def window_orders(self, start_ts, end_ts, limit=500, buckets=("success", "failed"),
                      expired_before=None):
        """只读流水：先按成功/失败筛选再截取；超时推导共用机器人阈值。"""
        p = self.placeholder
        conditions = []
        filter_args = []
        if "success" in buckets:
            conditions.append(f"status = {p}")
            filter_args.append("success")
        if "failed" in buckets:
            conditions.append(f"status IN ({p}, {p})")
            filter_args.extend(("failed", "expired"))
            if expired_before is not None:
                conditions.append(f"(status = {p} AND create_time <= {p})")
                filter_args.extend(("pending", int(expired_before)))
        if "pending" in buckets:
            condition = f"status = {p}"
            filter_args.append("pending")
            if expired_before is not None:
                condition += f" AND create_time > {p}"
                filter_args.append(int(expired_before))
            conditions.append(f"({condition})")
        if not conditions:
            return []
        wallet = (
            "SELECT 'wallet' AS kind, id, user_id, money, amount, trade_no, "
            "payment_method, payment_provider, status, create_time, complete_time, "
            "0 AS plan_id, '' AS provider_payload FROM top_ups "
            f"WHERE create_time >= {p} AND create_time < {p}"
        )
        subscription = (
            "SELECT 'subscription' AS kind, id, user_id, money, 0 AS amount, trade_no, "
            "payment_method, payment_provider, status, create_time, complete_time, "
            "plan_id, provider_payload FROM subscription_orders "
            f"WHERE create_time >= {p} AND create_time < {p}"
        )
        sql = (
            f"SELECT * FROM ({wallet} UNION ALL {subscription}) AS orders "
            f"WHERE ({' OR '.join(conditions)}) "
            f"ORDER BY create_time DESC, kind, id DESC LIMIT {p}"
        )
        return self._fetch(
            sql, (int(start_ts), int(end_ts), int(start_ts), int(end_ts),
                  *filter_args, int(limit)))

    def usernames(self, user_ids):
        ids = sorted({int(uid) for uid in user_ids if uid})
        if not ids:
            return {}
        p = self.placeholder
        marks = ",".join([p] * len(ids))
        rows = self._fetch(
            f"SELECT id, username, display_name FROM users WHERE id IN ({marks})", ids)
        result = {}
        for row in rows:
            # 优先用户名：new-api 建 root 用户时 display_name 是写死的 "Root User"
            name = (row.get("username") or "").strip() or (row.get("display_name") or "").strip()
            result[int(row["id"])] = name or f"用户#{row['id']}"
        return result

    def plan_titles(self, plan_ids):
        ids = sorted({int(pid) for pid in plan_ids if pid})
        if not ids:
            return {}
        p = self.placeholder
        marks = ",".join([p] * len(ids))
        try:
            rows = self._fetch(
                f"SELECT id, title FROM subscription_plans WHERE id IN ({marks})", ids)
        except Exception:
            return {}  # 老版本库可能还没有 subscription_plans 表
        return {int(row["id"]): (row.get("title") or "") for row in rows}

    def channels(self):
        """只读渠道列表（id/name/type/models/status），供渠道成本单价配置用。"""
        try:
            return self._fetch(
                "SELECT id, name, type, models, status FROM channels ORDER BY id", ())
        except Exception:
            return []  # 老版本库可能还没有 channels 表

    def consumption(self, start_ts, end_ts):
        """窗口内消耗日志按 (user_id, channel_id, created_at) 汇总 quota 与条数。

        new-api 的 logs 表 type=2 为消耗日志，quota 为消耗额度（整数）。
        只读聚合，不返回原始明细。
        """
        p = self.placeholder
        return self._fetch(
            f"SELECT user_id, channel_id, created_at, SUM(quota) AS quota, COUNT(*) AS cnt "
            f"FROM logs WHERE type = {p} AND created_at >= {p} AND created_at < {p} "
            "GROUP BY user_id, channel_id, created_at ORDER BY created_at, user_id, channel_id",
            (2, int(start_ts), int(end_ts)),
        )
