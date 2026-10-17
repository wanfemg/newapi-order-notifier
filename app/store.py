"""本程序自己的 SQLite：配置、推送去重、推送历史。"""

import json
import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pushed_orders (
    kind      TEXT NOT NULL,
    trade_no  TEXT NOT NULL,
    status    TEXT NOT NULL,
    pushed_at INTEGER NOT NULL,
    PRIMARY KEY (kind, trade_no, status)
);
CREATE TABLE IF NOT EXISTS push_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pushed_at     INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    trade_no      TEXT NOT NULL,
    status        TEXT NOT NULL,
    username      TEXT NOT NULL,
    money         REAL NOT NULL,
    reason        TEXT NOT NULL,
    reason_source TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    backfill      INTEGER NOT NULL DEFAULT 0,
    detail        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_credential (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_rates (
    currency   TEXT PRIMARY KEY,
    rate       TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_rate_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    currency   TEXT NOT NULL,
    old_rate   TEXT NOT NULL,
    new_rate   TEXT NOT NULL,
    changed_at INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS channel_costs (
    channel_id    INTEGER PRIMARY KEY,
    cost_per_unit TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    updated_at    INTEGER NOT NULL
);
"""

HISTORY_KEEP = 500


class Store:
    def __init__(self, path):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript("PRAGMA journal_mode=WAL;")
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self):
        """老库补列：已存在的 push_history 表不会因为 _SCHEMA 变化而自动加列。"""
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(push_history)")}
        if "backfill" not in columns:
            self._conn.execute(
                "ALTER TABLE push_history ADD COLUMN backfill INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

    def _write(self, sql, args=()):
        with self._lock:
            cursor = self._conn.execute(sql, args)
            self._conn.commit()
            return cursor

    def _read(self, sql, args=()):
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    # ---------- 配置 ----------

    def config_map(self):
        result = {}
        for row in self._read("SELECT key, value FROM config"):
            try:
                result[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                result[row["key"]] = row["value"]
        return result

    def set_configs(self, values):
        with self._lock:
            for key, value in values.items():
                self._conn.execute(
                    "INSERT INTO config (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
            self._conn.commit()

    # ---------- 推送去重 ----------

    def is_pushed(self, kind, trade_no, status):
        rows = self._read(
            "SELECT 1 FROM pushed_orders WHERE kind = ? AND trade_no = ? AND status = ?",
            (kind, trade_no, status),
        )
        return bool(rows)

    def mark_pushed(self, kind, trade_no, status):
        self._write(
            "INSERT OR IGNORE INTO pushed_orders (kind, trade_no, status, pushed_at) "
            "VALUES (?, ?, ?, ?)",
            (kind, trade_no, status, int(time.time())),
        )

    def pushed_count(self):
        return self._read("SELECT COUNT(*) AS n FROM pushed_orders")[0]["n"]

    def add_history(self, record):
        self._write(
            "INSERT INTO push_history "
            "(pushed_at, kind, trade_no, status, username, money, reason, reason_source, ok, "
            "backfill, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(record.get("pushed_at") or time.time()),
                record["kind"], record["trade_no"], record["status"],
                record.get("username", ""), float(record.get("money") or 0),
                record.get("reason", ""), record.get("reason_source", ""),
                1 if record.get("ok") else 0, 1 if record.get("backfill") else 0,
                record.get("detail", ""),
            ),
        )
        self._write(
            "DELETE FROM push_history WHERE id NOT IN "
            "(SELECT id FROM push_history ORDER BY id DESC LIMIT ?)",
            (HISTORY_KEEP,),
        )

    def clear_history(self):
        self._write("DELETE FROM push_history")

    def history(self, limit=100):
        rows = self._read(
            "SELECT * FROM push_history ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(row) for row in rows]

    # ---------- 管理口令 ----------

    def admin_hash(self):
        rows = self._read("SELECT password_hash FROM admin_credential WHERE id = 1")
        return rows[0]["password_hash"] if rows else ""

    def set_admin_hash(self, password_hash):
        self._write(
            "INSERT INTO admin_credential (id, password_hash) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET password_hash = excluded.password_hash",
            (password_hash,),
        )

    # ---------- 运行状态 ----------

    def get_state(self, key, default=""):
        rows = self._read("SELECT value FROM runtime_state WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_state(self, key, value):
        self._write(
            "INSERT INTO runtime_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    # ---------- 汇率 ----------

    def exchange_rates(self):
        return [dict(row) for row in self._read(
            "SELECT currency, rate, updated_at FROM exchange_rates ORDER BY currency")]

    def get_exchange_rate(self, currency):
        rows = self._read(
            "SELECT rate FROM exchange_rates WHERE currency = ?", (currency,))
        return rows[0]["rate"] if rows else ""

    def set_exchange_rate(self, currency, rate):
        """写入当前汇率，返回旧值（无旧值返回空串）。rate 为 decimal 字符串。"""
        old = self.get_exchange_rate(currency)
        self._write(
            "INSERT INTO exchange_rates (currency, rate, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(currency) DO UPDATE SET rate = excluded.rate, "
            "updated_at = excluded.updated_at",
            (currency, str(rate), int(time.time())),
        )
        return old

    def add_rate_history(self, currency, old_rate, new_rate, note=""):
        self._write(
            "INSERT INTO exchange_rate_history "
            "(currency, old_rate, new_rate, changed_at, note) VALUES (?, ?, ?, ?, ?)",
            (currency, str(old_rate), str(new_rate), int(time.time()), note or ""),
        )

    def rate_history(self, currency=None, limit=200):
        if currency:
            rows = self._read(
                "SELECT * FROM exchange_rate_history WHERE currency = ? "
                "ORDER BY id DESC LIMIT ?",
                (currency, int(limit)),
            )
        else:
            rows = self._read(
                "SELECT * FROM exchange_rate_history ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
        return [dict(row) for row in rows]

    # ---------- 渠道成本单价 ----------

    def channel_costs(self):
        return [dict(row) for row in self._read(
            "SELECT channel_id, cost_per_unit, currency, updated_at "
            "FROM channel_costs ORDER BY channel_id")]

    def get_channel_cost(self, channel_id):
        rows = self._read(
            "SELECT * FROM channel_costs WHERE channel_id = ?", (int(channel_id),))
        return dict(rows[0]) if rows else None

    def set_channel_cost(self, channel_id, cost_per_unit, currency="CNY"):
        self._write(
            "INSERT INTO channel_costs (channel_id, cost_per_unit, currency, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET cost_per_unit = excluded.cost_per_unit, "
            "currency = excluded.currency, updated_at = excluded.updated_at",
            (int(channel_id), str(cost_per_unit), currency or "CNY", int(time.time())),
        )

    def delete_channel_cost(self, channel_id):
        self._write("DELETE FROM channel_costs WHERE channel_id = ?", (int(channel_id),))
