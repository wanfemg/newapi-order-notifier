"""每日充值统计：每天 00:00 汇总前一天的充值，推送总金额与充值前五名。

口径：钱包充值（top_ups）与订阅开通（subscription_orders）的成功单都计入，按用户汇总
金额倒序；统计窗口是本地时区的自然日（容器 TZ 见 docker-compose.yml）。
"""

from datetime import date, timedelta
import logging
import threading
import time

import config as config_module
from notify import send_markdown
from source import SourceDB

STATE_KEY = "daily_report_last"
WEEKLY_STATE_KEY = "weekly_report_last"
MEDALS = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")


def _local_date(now):
    local = time.localtime(now)
    return date(local.tm_year, local.tm_mon, local.tm_mday)


def _date_midnight(day):
    return int(time.mktime((day.year, day.month, day.day, 0, 0, 0, 0, 0, -1)))


def _midnight(now):
    """当天 00:00 的时间戳。"""
    return _date_midnight(_local_date(now))


def day_window(now, offset_days=1):
    """返回 (起始时间戳, 结束时间戳, 日期标签)。

    offset_days=1：刚过去的那一天（定时任务用）；0：今天截至此刻（手动触发用）。
    结束时间是排他上界，手动窗口加 1 秒，好让「刚刚这一秒」产生的订单也算进来。
    """
    if offset_days <= 0:
        local = time.localtime(now)
        return _midnight(now), int(now) + 1, time.strftime("%Y-%m-%d", local)
    day = _local_date(now) - timedelta(days=offset_days)
    return _date_midnight(day), _date_midnight(day + timedelta(days=1)), day.isoformat()


def week_window(now, offset_weeks=1):
    """上个完整自然周；offset_weeks=0 表示本周一至当前秒（含）。"""
    today = _local_date(now)
    monday = today - timedelta(days=today.weekday())
    start_day = monday - timedelta(weeks=max(0, offset_weeks))
    last_day = start_day + timedelta(days=6) if offset_weeks > 0 else today
    end = _date_midnight(start_day + timedelta(days=7)) if offset_weeks > 0 else int(now) + 1
    return _date_midnight(start_day), end, f"{start_day.isoformat()} ~ {last_day.isoformat()}"


def seconds_until_midnight(now):
    """距离下一个 00:00 的秒数（至少 1 秒，避免忙等）。"""
    tomorrow = _local_date(now) + timedelta(days=1)
    return max(1, _date_midnight(tomorrow) - int(now))


def summarize(rows):
    """汇总一天的成功充值，返回 (总金额, 笔数, 人数)。"""
    return (sum(float(row["money"] or 0) for row in rows),
            sum(int(row["orders"] or 0) for row in rows),
            len(rows))


def _two_line(label, value):
    """标签一行、数值一行；行末两个空格是 markdown 硬换行，避免钉钉把两行并成一行。"""
    return f"**{label}**  \n{value}"


def render_report(rows, names, label, weekly=False):
    """生成钉钉 markdown，返回 (标题, 正文)。rows 是 daily_recharge 的按用户汇总结果。"""
    period = "每周" if weekly else "每日"
    title = f"📊 {period}充值统计（{label}）"
    if not rows:
        empty = "本周没有成功充值" if weekly else "当天没有成功充值"
        return title, f"### {title}\n\n{empty}"

    total, orders, users = summarize(rows)
    lines = [f"### {title}", ""]
    for name, value in (("总充值", f"¥{total:,.2f}"), ("笔数", f"{orders} 笔"),
                        ("充值人数", f"{users} 人")):
        lines.append(_two_line(name, value))
        lines.append("")
    lines.append("**充值排行前五名**")
    for index, row in enumerate(rows[:5]):
        uid = int(row["user_id"])
        name = names.get(uid)
        who = f"{name}（ID {uid}）" if name else f"用户#{uid}"
        lines.append(f"{MEDALS[index]} {who} —— ¥{float(row['money'] or 0):,.2f}"
                     f"（{int(row['orders'] or 0)} 笔）")
    return title, "\n".join(lines)


class DailyReporter:
    def __init__(self, store):
        self.store = store
        self.log = logging.getLogger("daily")
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run_daily(self, offset_days=1, force=False, overrides=None):
        """生成并推送一份日报。返回 (ok, detail)。

        ok 为真表示不必重试（已推送 / 之前推过 / 功能关闭）。force 供界面手动触发：
        不受开关与去重限制，也不写去重标记。overrides 是界面上还没保存的钉钉推送项。
        """
        return self._run_report("daily", offset_days, force, overrides)

    def run_weekly(self, offset_weeks=1, force=False, overrides=None):
        """生成周报，手动推送与日报采用相同的覆盖和去重约定。"""
        return self._run_report("weekly", offset_weeks, force, overrides)

    def _run_report(self, kind, offset, force, overrides):
        config = config_module.apply_overrides(
            config_module.effective_config(self.store), overrides)
        state_key = WEEKLY_STATE_KEY if kind == "weekly" else STATE_KEY
        if not config[f"{kind}_report"] and not force:
            self.log.info("%s 充值统计推送已关闭，跳过", kind)
            return True, "已关闭"
        if not config["dingtalk_webhook"]:
            return False, "请先填写并保存钉钉 Webhook"

        window = week_window if kind == "weekly" else day_window
        start, end, label = window(time.time(), offset)
        if not force and self.store.get_state(state_key) == label:
            self.log.info("%s %s 已推送过，跳过", kind, label)
            return True, "已推送过"

        source = SourceDB(config)
        rows = source.daily_recharge(start, end)
        names = source.usernames([row["user_id"] for row in rows])
        title, text = render_report(rows, names, label, weekly=kind == "weekly")
        total, orders, users = summarize(rows)
        ok, detail = send_markdown(
            config["dingtalk_webhook"], title, text,
            secret=config["dingtalk_secret"], keyword=config["dingtalk_keyword"],
            at_mobiles=[m for m in (config["dingtalk_at_mobiles"] or "").split(",") if m.strip()],
        )
        # 日报和周报分别记录，成功后才写各自的去重标记。
        self.store.add_history({
            "kind": kind, "trade_no": label, "status": "success" if ok else "failed",
            "money": total, "reason": f"{orders} 笔 / {users} 人",
            "reason_source": kind, "ok": ok, "detail": text,
        })
        if ok:
            if not force:
                self.store.set_state(state_key, label)
            self.log.info("已推送 %s 的充值统计：%s 人", label, len(rows))
            return True, f"已推送 {label} 的充值统计（{len(rows)} 人）"
        self.log.warning("充值统计推送失败：%s", detail)
        return False, detail

    def run_scheduled(self, now=None):
        """午夜执行日报，周一另执行周报；每种报告独立重试至多三次。"""
        pending = {"daily": self.run_daily}
        if _local_date(time.time() if now is None else now).weekday() == 0:
            pending["weekly"] = self.run_weekly
        for attempt in range(3):
            for kind, run in list(pending.items()):
                if self._stop.is_set():
                    return
                try:
                    ok, detail = run()
                except Exception as exc:
                    ok, detail = False, str(exc)
                    self.log.exception("%s 充值统计推送出错：%s", kind, exc)
                if ok:
                    del pending[kind]
                else:
                    self.log.warning("%s 充值统计待重试：%s", kind, detail)
            if not pending or attempt == 2 or self._stop.wait(60):
                return

    def loop(self):
        while not self._stop.is_set():
            if self._stop.wait(seconds_until_midnight(time.time())):
                return
            self.run_scheduled()
