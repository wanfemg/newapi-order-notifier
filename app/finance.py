"""财务看板聚合：从成功订单行与流水订单行算出总额、趋势、构成与排行。

跨库的按天分桶放在 Python 里做，避免各数据库 strftime / FROM_UNIXTIME / to_timestamp
的方言差异；金额用浮点累加，仅用于界面展示，不做记账与结算。
"""

from datetime import date, timedelta
import time

from poller import pay_method_label


def _money(row):
    try:
        return float(row.get("money") or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(ts):
    return time.strftime("%Y-%m-%d", time.localtime(int(ts)))


def range_window(days, now=None):
    """返回 (start_ts, end_ts)，覆盖最近 days 个自然日（含今天，结束为排他上界）。"""
    now = int(time.time() if now is None else now)
    local = time.localtime(now)
    start = date(local.tm_year, local.tm_mon, local.tm_mday) - timedelta(days=days - 1)
    return int(time.mktime((start.year, start.month, start.day, 0, 0, 0, 0, 0, -1))), now + 1


def totals(rows):
    money = round(sum(_money(row) for row in rows), 2)
    users = len({row.get("user_id") for row in rows if row.get("user_id") is not None})
    return {"money": money, "orders": len(rows), "users": users}


def trend(rows, days):
    """按天汇总金额与笔数，缺失的天补 0。返回 {days, points:[{date,money,orders}]}。"""
    buckets = {}
    start, _ = range_window(days)
    for offset in range(days):
        day = date.fromisoformat(_day(start)) + timedelta(days=offset)
        buckets[day.isoformat()] = {"money": 0.0, "orders": 0}
    for row in rows:
        key = _day(row.get("create_time") or 0)
        if key not in buckets:
            continue
        buckets[key]["money"] += _money(row)
        buckets[key]["orders"] += 1
    points = [
        {"date": day, "money": round(buckets[day]["money"], 2),
         "orders": buckets[day]["orders"]}
        for day in sorted(buckets)
    ]
    return {"days": days, "points": points}


def breakdown(rows):
    """按订单类型拆分（钱包充值 / 订阅开通）。"""
    wallet = {"money": 0.0, "orders": 0}
    subscription = {"money": 0.0, "orders": 0}
    for row in rows:
        target = wallet if row.get("kind") == "wallet" else subscription
        target["money"] += _money(row)
        target["orders"] += 1
    for item in (wallet, subscription):
        item["money"] = round(item["money"], 2)
    return [
        {"key": "wallet", "label": "钱包充值", **wallet},
        {"key": "subscription", "label": "订阅开通", **subscription},
    ]


def methods(rows):
    """按支付方式聚合（复用 poller.pay_method_label：微信→微小信 / 支付宝→支小宝）。"""
    buckets = {}
    for row in rows:
        label = pay_method_label(row.get("payment_method"), row.get("payment_provider"))
        item = buckets.setdefault(label, {"money": 0.0, "orders": 0})
        item["money"] += _money(row)
        item["orders"] += 1
    return [
        {"key": label, "label": label, "money": round(item["money"], 2),
         "orders": item["orders"]}
        for label, item in sorted(buckets.items(), key=lambda kv: -kv[1]["money"])
    ]


def ranking(rows, names, limit=10):
    """按用户汇总金额，返回前 limit 名。"""
    buckets = {}
    for row in rows:
        uid = row.get("user_id")
        if uid is None:
            continue
        item = buckets.setdefault(uid, {"user_id": uid, "money": 0.0, "orders": 0})
        item["money"] += _money(row)
        item["orders"] += 1
    ranked = sorted(buckets.values(), key=lambda item: -item["money"])[:limit]
    for item in ranked:
        item["money"] = round(item["money"], 2)
        item["name"] = names.get(int(item["user_id"])) or f"用户#{item['user_id']}"
    return ranked


def users(rows, names):
    """所选周期内的成功订单画像；钱包和订阅字段为各自累计金额。"""
    buckets = {}
    for row in rows:
        uid = row.get("user_id")
        if uid is None:
            continue
        uid = int(uid)
        created = int(row.get("create_time") or 0)
        item = buckets.setdefault(uid, {
            "user_id": uid, "username": names.get(uid) or f"用户#{uid}",
            "total": 0.0, "orders": 0, "average": 0.0,
            "first_time": created, "last_time": created,
            "wallet": 0.0, "subscription": 0.0,
        })
        money = _money(row)
        item["total"] += money
        item["orders"] += 1
        item["first_time"] = min(item["first_time"], created)
        item["last_time"] = max(item["last_time"], created)
        item[row["kind"]] += money
    result = sorted(buckets.values(), key=lambda item: (-item["total"], item["user_id"]))
    for item in result:
        item["average"] = round(item["total"] / item["orders"], 2)
        for key in ("total", "wallet", "subscription"):
            item[key] = round(item[key], 2)
    return result