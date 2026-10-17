"""站点充值汇率：1:X 输入、货币种类、历史记录，全程用 Decimal 保证精度。

全局汇率仅保留兼容 CRUD 与历史记录，不参与渠道消费统计。渠道充值比例 1:N 表示 1 元人民币
兑换 N 个网站额度；网站额度由 raw quota ÷ 500000 得到。汇率与历史都以字符串（Decimal）形式存本程序自己的库，
不写入 new-api。
"""

from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP

import time

MONEY_PLACES = 6


def parse_rate(text):
    """解析汇率输入，接受「1:7」「7」「1:7.5」等写法，返回 Decimal。"""
    raw = str(text or "").strip().replace("：", ":")
    if not raw:
        raise ValueError("汇率不能为空")
    try:
        if ":" in raw:
            left, _, right = raw.partition(":")
            left_num = Decimal(left.strip())
            right_num = Decimal(right.strip())
            if not left_num.is_finite() or left_num <= 0 or not right_num.is_finite():
                raise ValueError("汇率必须是有限的正数")
            value = right_num / left_num
        else:
            value = Decimal(raw)
    except DecimalException:
        raise ValueError("汇率格式无效，请用 1:X 或直接填数字") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("汇率必须是大于 0 的数字")
    return value


def parse_cost(text):
    """解析渠道成本单价，返回 Decimal（每 1 raw quota 的成本）。"""
    try:
        value = Decimal(str(text if text is not None else "").strip())
    except InvalidOperation:
        raise ValueError("成本单价格式无效") from None
    if not value.is_finite() or value < 0:
        raise ValueError("成本单价必须是非负数字")
    return value


def fmt_money(value, places=MONEY_PLACES):
    """把 Decimal 金额格式化成去掉末尾零的字符串。"""
    if value is None:
        return "0"
    quantized = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def fmt_rate(value):
    """汇率展示成 1:X 的形式。"""
    if value is None:
        return "-"
    return f"1:{fmt_money(value)}"


def normalize_currency(text):
    """货币种类：去空格并大写，空值报错。"""
    currency = str(text or "").strip().upper()
    if not currency:
        raise ValueError("货币种类不能为空")
    if len(currency) > 16:
        raise ValueError("货币种类长度不能超过 16 个字符")
    return currency


def save_rate(store, currency, rate_text, note=""):
    """保存汇率：校验 → 记录历史 → 写入当前值。返回 (old, new, currency)。"""
    currency = normalize_currency(currency)
    new_rate = parse_rate(rate_text)
    new_str = str(new_rate)
    old_str = store.save_exchange_rate(currency, new_str, note=note)
    return old_str, new_str, currency


def save_channel_finance(store, body):
    """API 共用的渠道财务配置验证入口，不接受客户端回填生效时间。"""
    if not isinstance(body, dict):
        raise ValueError("渠道配置必须是对象")
    cid = body.get("channel_id")
    if isinstance(cid, bool) or not str(cid).isdigit() or int(cid) <= 0:
        raise ValueError("channel_id 必须是正整数")
    fee_currency = normalize_currency(body.get("fee_currency", "CNY"))
    if fee_currency != "CNY":
        raise ValueError("渠道收费货币必须为 CNY")
    cost_currency = normalize_currency(body.get("cost_currency", fee_currency))
    fee_rate = parse_rate(body.get("fee_rate"))
    cost = body.get("cost_per_quota")
    cost = None if cost is None or cost == "" else parse_cost(cost)
    return store.upsert_channel_finance(
        int(cid), str(body.get("channel_name") or "").strip(), fee_currency,
        fee_rate, cost_currency, cost)


def render_rate_change(currency, old_rate, new_rate, note=""):
    """生成汇率调整的钉钉 markdown，返回 (标题, 正文)。"""
    title = "💱 汇率已调整"
    lines = ["### 💱 汇率已调整", ""]
    lines.append(f"- **货币**：{currency}")
    old_text = "-"
    try:
        if Decimal(old_rate or "") > 0:
            old_text = fmt_rate(Decimal(old_rate))
    except InvalidOperation:
        pass
    lines.append(f"- **旧汇率**：{old_text}")
    lines.append(f"- **新汇率**：{fmt_rate(Decimal(new_rate))}")
    if note:
        lines.append(f"- **备注**：{note}")
    lines.append(f"- **时间**：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    return title, "\n".join(lines)
