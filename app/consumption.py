"""消耗统计：用户实际消耗金额 + 上游渠道实际成本，全部用 Decimal 保证精度。

数据来源：
  - new-api 只读：logs 表 type=2 的消耗日志，按 (user_id, channel_id) 汇总 quota。
  - 本程序自己的库：汇率（1 单位计价货币 = X 额度）与渠道成本单价。

口径：
  - 用户消耗金额 = quota / 汇率（按 rate_currency 计价）。
  - 上游成本   = quota × 渠道成本单价（未配置成本单价的渠道不计入）。
"""

from decimal import Decimal

from rates import fmt_money


def _decimal(text):
    try:
        return Decimal(str(text or "").strip() or "0")
    except Exception:
        return Decimal("0")


def compute(store, source, start_ts, end_ts, rate_currency):
    """计算窗口内消耗统计。返回结构化 dict，金额均为 decimal 字符串。"""
    rate_map = {row["currency"]: _decimal(row["rate"]) for row in store.exchange_rates()}
    cost_map = {}
    for row in store.channel_costs():
        cost_map[int(row["channel_id"])] = (_decimal(row["cost_per_unit"]),
                                            row.get("currency") or "CNY")

    rows = source.consumption(int(start_ts), int(end_ts))

    rate = rate_map.get(rate_currency, Decimal("0"))
    total_quota = 0
    user_money = Decimal("0")
    upstream_cost = Decimal("0")

    by_channel = {}
    by_user = {}
    for row in rows:
        quota = int(row.get("quota") or 0)
        if quota <= 0:
            continue
        uid = row.get("user_id")
        cid = row.get("channel_id")
        total_quota += quota

        if rate > 0:
            user_money += Decimal(quota) / rate

        cost = Decimal("0")
        cost_currency = None
        if cid is not None:
            info = cost_map.get(int(cid))
            if info:
                unit, cost_currency = info
                cost = Decimal(quota) * unit
                upstream_cost += cost
            item = by_channel.setdefault(int(cid), {
                "channel_id": int(cid), "quota": 0,
                "cost": Decimal("0"), "cost_currency": cost_currency,
            })
            item["quota"] += quota
            item["cost"] += cost

        if uid is not None:
            item = by_user.setdefault(int(uid), {
                "user_id": int(uid), "quota": 0, "money": Decimal("0"),
            })
            item["quota"] += quota
            if rate > 0:
                item["money"] += Decimal(quota) / rate

    channel_names = {int(c["id"]): (c.get("name") or f"渠道#{c['id']}") for c in source.channels()}
    user_ids = sorted(by_user)
    user_names = source.usernames(user_ids)

    channels = []
    for cid, item in sorted(by_channel.items(), key=lambda kv: -kv[1]["quota"]):
        channels.append({
            "channel_id": item["channel_id"],
            "name": channel_names.get(item["channel_id"], f"渠道#{item['channel_id']}"),
            "quota": item["quota"],
            "cost": fmt_money(item["cost"]),
            "cost_currency": item["cost_currency"] or "CNY",
        })
    users = []
    for uid, item in sorted(by_user.items(), key=lambda kv: -kv[1]["quota"]):
        users.append({
            "user_id": item["user_id"],
            "username": user_names.get(uid) or f"用户#{uid}",
            "quota": item["quota"],
            "money": fmt_money(item["money"]),
        })

    return {
        "start_ts": int(start_ts),
        "end_ts": int(end_ts),
        "currency": rate_currency,
        "rate": fmt_money(rate) if rate > 0 else "0",
        "quota": total_quota,
        "user_money": fmt_money(user_money),
        "upstream_cost": fmt_money(upstream_cost),
        "channels": channels,
        "users": users,
        "has_rate": rate > 0,
    }


def render_consumption(stats, title="📊 消耗统计"):
    """生成消耗统计的钉钉 markdown，返回 (标题, 正文)。"""
    lines = [f"### {title}", ""]
    currency = stats.get("currency") or "CNY"
    lines.append(f"- **统计区间**：{_fmt_ts(stats.get('start_ts'))} ~ {_fmt_ts(stats.get('end_ts'))}")
    lines.append(f"- **汇率**：1 {currency} = {stats.get('rate')} 额度")
    lines.append(f"- **消耗额度**：{int(stats.get('quota') or 0):,}")
    lines.append(f"- **用户消耗金额**：{stats.get('user_money')} {currency}")
    lines.append(f"- **上游渠道成本**：{stats.get('upstream_cost')}")

    channels = stats.get("channels") or []
    if channels:
        lines.append("")
        lines.append("**🔌 上游渠道成本明细**")
        for item in channels[:10]:
            lines.append(f"- {item['name']}：{int(item['quota']):,} 额度 "
                         f"/ 成本 {item['cost']} {item['cost_currency']}")

    users = stats.get("users") or []
    if users:
        lines.append("")
        lines.append("**👥 用户消耗排行**")
        for item in users[:10]:
            lines.append(f"- {item['username']}：{int(item['quota']):,} 额度 "
                         f"/ {item['money']} {currency}")
    return title, "\n".join(lines)


def _fmt_ts(ts):
    import time
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError):
        return "-"
