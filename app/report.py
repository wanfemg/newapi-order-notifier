"""每日充值统计：每天 00:00 汇总前一天的充值，推送总金额与充值前五名。

口径：只统计钱包充值（top_ups）里状态为 success 的订单，按用户汇总金额倒序，
订阅订单不计入；统计窗口是本地时区的自然日（容器 TZ 见 docker-compose.yml）。
"""

import logging
import threading
import time

import config as config_module
from notify import send_markdown
from source import SourceDB

STATE_KEY = "daily_report_last"
MEDALS = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")


def _midnight(now):
    """当天 00:00 的时间戳。"""
    local = time.localtime(now)
    return int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1)))


def day_window(now, offset_days=1):
    """返回 (起始时间戳, 结束时间戳, 日期标签)。

    offset_days=1：刚过去的那一天（定时任务用）；0：今天截至此刻（手动触发用）。
    结束时间是排他上界，手动窗口加 1 秒，好让「刚刚这一秒」产生的订单也算进来。
    """
    if offset_days <= 0:
        local = time.localtime(now)
        return _midnight(now), int(now) + 1, time.strftime("%Y-%m-%d", local)
    start = _midnight(now) - 86400 * offset_days
    end = start + 86400
    return start, end, time.strftime("%Y-%m-%d", time.localtime(start))


def seconds_until_midnight(now):
    """距离下一个 00:00 的秒数（至少 1 秒，避免忙等）。"""
    return max(1, _midnight(now) + 86400 - int(now))


def render_report(rows, names, label):
    """生成钉钉 markdown，返回 (标题, 正文)。rows 是 daily_recharge 的按用户汇总结果。"""
    title = f"📊 每日充值统计（{label}）"
    lines = [f"### {title}"]
    if not rows:
        lines.append("- 当天没有成功充值")
        return title, "\n".join(lines)

    total = sum(float(row["money"] or 0) for row in rows)
    orders = sum(int(row["orders"] or 0) for row in rows)
    lines.append(f"- **总充值**：¥{total:,.2f}")
    lines.append(f"- **笔数**：{orders} 笔")
    lines.append(f"- **充值人数**：{len(rows)} 人")
    lines.append("")
    lines.append("**充值排行前五名**")
    for index, row in enumerate(rows[:5]):
        name = names.get(int(row["user_id"])) or f"用户#{row['user_id']}"
        lines.append(f"{MEDALS[index]} {name} —— ¥{float(row['money'] or 0):,.2f}"
                     f"（{int(row['orders'] or 0)} 笔）")
    return title, "\n".join(lines)


class DailyReporter:
    def __init__(self, store):
        self.store = store
        self.log = logging.getLogger("daily")
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run_daily(self, offset_days=1, force=False):
        """生成并推送一份日报。返回 (ok, detail)。

        ok 为真表示不必重试（已推送 / 之前推过 / 功能关闭）。force 供界面手动触发：
        不受开关与去重限制，也不写去重标记。
        """
        config = config_module.effective_config(self.store)
        if not config["daily_report"] and not force:
            self.log.info("每日充值统计推送已关闭，跳过")
            return True, "已关闭"
        if not config["dingtalk_webhook"]:
            return False, "请先填写并保存钉钉 Webhook"

        start, end, label = day_window(time.time(), offset_days)
        if not force and self.store.get_state(STATE_KEY) == label:
            self.log.info("日报 %s 已推送过，跳过", label)
            return True, "已推送过"

        source = SourceDB(config)
        rows = source.daily_recharge(start, end)
        names = source.usernames([row["user_id"] for row in rows])
        title, text = render_report(rows, names, label)
        ok, detail = send_markdown(
            config["dingtalk_webhook"], title, text,
            secret=config["dingtalk_secret"], keyword=config["dingtalk_keyword"],
            at_mobiles=[m for m in (config["dingtalk_at_mobiles"] or "").split(",") if m.strip()],
        )
        if ok:
            if not force:
                self.store.set_state(STATE_KEY, label)
            self.log.info("已推送 %s 的充值统计：%s 人", label, len(rows))
            return True, f"已推送 {label} 的充值统计（{len(rows)} 人）"
        self.log.warning("充值统计推送失败：%s", detail)
        return False, detail

    def loop(self):
        while not self._stop.is_set():
            if self._stop.wait(seconds_until_midnight(time.time())):
                return
            # 到点后重试几次，避免一次网络抖动丢掉一整天的统计
            for _ in range(3):
                try:
                    ok, detail = self.run_daily()
                except Exception as exc:
                    ok, detail = False, str(exc)
                    self.log.exception("每日充值统计推送出错：%s", exc)
                if ok:
                    break
                self.log.warning("每日充值统计待重试：%s", detail)
                if self._stop.wait(60):
                    return
