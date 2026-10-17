"""按日志时间匹配渠道充值比例；网站额度与底层 quota 分开，不做隐式汇兑。"""

from decimal import Decimal, InvalidOperation

from rates import fmt_money, normalize_currency

QUOTA_PER_UNIT = Decimal("500000")


def _decimal(text):
    try:
        value = Decimal(str(text))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _bucket():
    return {"quota": 0, "money_by_currency": {}, "cost_by_currency": {},
            "complete": True, "income_complete": True, "cost_complete": True,
            "warnings": set()}


def _add(item, quota, income, fee_currency, cost, cost_currency, warnings, currency):
    item["quota"] += quota
    if income is not None:
        amounts = item["money_by_currency"]
        amounts[fee_currency] = amounts.get(fee_currency, Decimal(0)) + income
    else:
        item["income_complete"] = False
    if cost is not None:
        amounts = item["cost_by_currency"]
        amounts[cost_currency] = amounts.get(cost_currency, Decimal(0)) + cost
    else:
        item["cost_complete"] = False
    item["warnings"].update(warnings)
    item["complete"] = (item["income_complete"] and item["cost_complete"]
                        and not item["warnings"])


def _finish(item, currency):
    money = item["money_by_currency"].get(currency)
    cost = item["cost_by_currency"].get(currency)
    if not item["quota"]:
        money = cost = Decimal(0)
    return {"quota": item["quota"],
            "website_units": fmt_money(Decimal(item["quota"]) / QUOTA_PER_UNIT),
            "income": fmt_money(money) if money is not None else None,
            "money": fmt_money(money) if money is not None else None,
            "cost": fmt_money(cost) if cost is not None and item["cost_complete"] else None,
            "profit": fmt_money((money or Decimal(0)) - (cost or Decimal(0)))
            if item["complete"] else None,
            "complete": item["complete"],
            "income_complete": item["income_complete"],
            "cost_configured": item["cost_complete"],
            "warnings": sorted(item["warnings"]),
            "money_by_currency": {key: fmt_money(value) for key, value in
                                  sorted(item["money_by_currency"].items())},
            "cost_by_currency": {key: fmt_money(value) for key, value in
                                 sorted(item["cost_by_currency"].items())}}


def compute(store, source, start_ts, end_ts, rate_currency):
    """兼容保留 quota 和金额字段；消费统一折算 CNY，不使用全局汇率。"""
    currency = "CNY"
    total = _bucket()
    by_channel, by_user = {}, {}
    channel_names = {int(c["id"]): c.get("name") or f"渠道#{c['id']}"
                     for c in source.channels()}
    legacy_costs = {int(row["channel_id"]): row for row in store.channel_costs()}
    snapshots = {}
    applied_rates = set()
    for row in source.consumption(int(start_ts), int(end_ts)):
        quota = int(row.get("quota") or 0)
        if quota <= 0:
            continue
        cid = int(row.get("channel_id") or 0)
        uid = int(row.get("user_id") or 0)
        ts = row.get("created_at")
        warnings = set()
        snapshot = None
        if ts is not None:
            key = (cid, int(ts))
            if key not in snapshots:
                snapshots[key] = store.find_channel_snapshot(*key)
            snapshot = snapshots[key]
        else:
            warnings.add(f"渠道#{cid}日志缺少时间，无法历史计价")
        cost = None
        cost_currency = currency
        rate = Decimal(0)
        if snapshot:
            cost_currency = normalize_currency(snapshot["cost_currency"])
            # 保留旧外币快照，但不能把它冒充人民币充值比例。
            if normalize_currency(snapshot["fee_currency"]) == "CNY":
                rate = _decimal(snapshot["fee_rate"])
            else:
                warnings.add(f"渠道#{cid}历史收费币种非CNY，折算人民币未知")
            if snapshot["cost_per_quota"] is not None:
                cost_rate = _decimal(snapshot["cost_per_quota"])
                if cost_rate is not None and cost_rate >= 0:
                    cost = Decimal(quota) * cost_rate
        elif ts is not None:
            legacy = legacy_costs.get(cid)
            # 删除后的空档不能复活旧成本；从未有过渠道快照的旧成本才兼容。
            history = store.has_channel_finance_history(cid, ts)
            if legacy and not history and int(legacy["updated_at"]) <= int(ts):
                cost_currency = normalize_currency(legacy["currency"])
                cost_rate = _decimal(legacy["cost_per_unit"])
                if cost_rate is not None and cost_rate >= 0:
                    cost = Decimal(quota) * cost_rate
        income = (Decimal(quota) / QUOTA_PER_UNIT / rate
                  if rate is not None and rate > 0 else None)
        if income is None:
            warnings.add(f"渠道#{cid}充值比例未配置或不可用（日志时间），折算人民币不完整")
        else:
            applied_rates.add(rate)
        if cost is None:
            warnings.add(f"渠道#{cid}成本未配置（日志时间）")
        if cost is not None and cost_currency != currency:
            warnings.add(f"渠道#{cid}成本币种{cost_currency}不计入{currency}总额")
        channel = by_channel.setdefault(cid, _bucket())
        channel["name"] = channel_names.get(cid) or (
            snapshot["channel_name"] if snapshot else f"渠道#{cid}")
        user = by_user.setdefault(uid, _bucket())
        detail = user.setdefault("channels", {}).setdefault(cid, _bucket())
        for item in (total, channel, user, detail):
            _add(item, quota, income, currency, cost, cost_currency, warnings, currency)

    channels = []
    for cid, item in sorted(by_channel.items(), key=lambda pair: (-pair[1]["quota"], pair[0])):
        finished = _finish(item, currency)
        finished.update(channel_id=cid, name=item["name"], currency=currency,
                        income_currency=currency,
                        cost_currency=next(iter(item["cost_by_currency"]), currency)
                        if len(item["cost_by_currency"]) <= 1 else None)
        if item["cost_complete"] and len(item["cost_by_currency"]) == 1:
            finished["cost"] = fmt_money(next(iter(item["cost_by_currency"].values())))
        channels.append(finished)
    names = source.usernames(sorted(by_user))
    users = []
    # 使用未格式化 Decimal 排名；未知金额排在已知金额之后。
    def user_order(pair):
        uid, item = pair
        money = item["money_by_currency"].get(currency)
        return (money is None, -(money or Decimal(0)), uid)

    for uid, item in sorted(by_user.items(), key=user_order):
        finished = _finish(item, currency)
        finished.update(user_id=uid, username=names.get(uid) or f"用户#{uid}", channels=[])
        for cid, detail in sorted(item["channels"].items()):
            finished["channels"].append({**_finish(detail, currency), "channel_id": cid,
                                          "name": by_channel[cid]["name"]})
        users.append(finished)
    result = _finish(total, currency)
    single_rate = next(iter(applied_rates)) if len(applied_rates) == 1 else Decimal(0)
    known_cost = total["cost_by_currency"].get(currency)
    result.update(start_ts=int(start_ts), end_ts=int(end_ts), currency=currency,
                  rate=fmt_money(single_rate), user_money=result["income"],
                  upstream_cost=fmt_money(known_cost) if known_cost is not None else
                  ("0" if not total["quota"] else None),
                  channels=channels, users=users, top_users=users[:5],
                  has_rate=bool(total["quota"]) and total["income_complete"])
    return result


def _amount_text(value, complete=True):
    if value is None:
        return "未知（不完整）"
    return str(value) + ("（已知部分，不完整）" if not complete else "")


def render_consumption(stats, title="财务消耗统计"):
    """日报和阈值消息共用网站额度、人民币折算及完整性口径。"""
    currency = "CNY"
    lines = [f"### {title}", "",
             f"- **统计区间**：{_fmt_ts(stats.get('start_ts'))} ~ {_fmt_ts(stats.get('end_ts'))}",
             f"- **网站额度消耗**：{stats.get('website_units', '未知')}",
             f"- **底层 quota**：{int(stats.get('quota') or 0):,}",
             f"- **折算人民币消费**：{_amount_text(stats.get('user_money'), stats.get('income_complete', False))} {currency}",
             f"- **全站成本**：{_amount_text(stats.get('upstream_cost'), stats.get('complete', False))} {currency}",
             f"- **折算消费利润**：{stats['profit'] if stats.get('profit') is not None else '无法准确计算'} {currency}",
             "", "**渠道网站额度 / 折算人民币 / 成本 / 折算消费利润**"]
    for item in stats.get("channels", []):
        income = _amount_text(item.get("money"), item.get("income_complete", False))
        cost = _currency_text(item.get("cost_by_currency", {}))
        if not item.get("cost_configured", True):
            cost += "（已知部分不完整 / 成本未配置）"
        profit = f"{item['profit']} {currency}" if item.get("profit") is not None else "无法准确计算"
        lines.append(f"- {item['name']}：网站额度 {item['website_units']} / 折算人民币 {income} CNY / 成本 {cost} / 折算消费利润 {profit}")
    lines.extend(["", "**用户消费前五名（按折算CNY金额，缺失部分不参与金额计算）**"])
    for item in stats.get("top_users", [])[:5]:
        money = _amount_text(item.get("money"), item.get("income_complete", False))
        lines.append(f"- {item['username']}（ID {item['user_id']}）：网站额度 {item['website_units']} / 折算人民币 {money} CNY")
    lines.extend(["", "**说明：人民币金额按历史渠道充值比例折算，不代表实收现金；未配置部分或外币不会被汇兑后计入人民币合计。**"])
    if not stats.get("complete", True):
        lines.extend(f"- {warning}" for warning in stats.get("warnings", []))
    return title, "\n".join(lines)


def _currency_text(amounts):
    return " / ".join(f"{value} {key}" for key, value in amounts.items()) or "未知（未配置）"


def _fmt_ts(ts):
    import time
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(int(ts)))
    except (TypeError, ValueError):
        return "-"
