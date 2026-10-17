"""站点充值汇率：1:X 输入、货币种类、历史记录，全程用 Decimal 保证精度。

汇率语义：1 单位计价货币 = X 站点额度（quota）。例如「1:7」表示 1 元人民币
可兑换 7 个站点额度。汇率与历史都以字符串（Decimal）形式存本程序自己的库，
不写入 new-api。
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import time

MONEY_PLACES = 6


def parse_rate(text):
    """解析汇率输入，接受「1:7」「7」「1:7.5」等写法，返回 Decimal。"""
    raw = str(text or "").strip().replace("：", ":")
    if not raw:
        raise ValueError("汇率不能为空")
    if ":" in raw:
        left, _, right = raw.partition(":")
        try:
            left_num = Decimal(left.strip() or "1")
        except InvalidOperation:
            left_num = Decimal("1")
        try:
            right_num = Decimal(right.strip())
        except InvalidOperation:
            raise ValueError("汇率格式无效，请用 1:X 或直接填数字") from None
        if left_num <= 0:
            raise ValueError("汇率等号左边必须是大于 0 的数字")
        value = right_num / left_num
    else:
        try:
            value = Decimal(raw)
        except InvalidOperation:
            raise ValueError("汇率格式无效，请用 1:X 或直接填数字") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("汇率必须是大于 0 的数字")
    return value


def parse_cost(text):
    """解析渠道成本单价，返回 Decimal（每 1 额度的成本）。"""
    try:
        value = Decimal(str(text or "").strip())
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
    old_str = store.get_exchange_rate(currency)
    store.set_exchange_rate(currency, new_str)
    store.add_rate_history(currency, old_str or "0", new_str, note=note)
    return old_str, new_str, currency


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
