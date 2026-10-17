"""消耗统计监控：按固定间隔只读 new-api 的 logs，触发阈值 / 变动通知。

与 poller / report 一样，本模块绝不写 new-api，只读它的 logs 表；汇率、成本单价、
运行水位都存本程序自己的库。

两个独立的触发条件（可分别开启）：
  - consume_change_quota：单次间隔内新增消耗额度达到该值，推送「消耗变动」。
  - consume_threshold_quota：累计消耗额度达到该值，推送「达到阈值」并清零累计。

水位用 runtime_state 里的 consume_last_ts 记录，程序重启不丢、不重复统计。
"""

import logging
import threading
import time

import config as config_module
import consumption
from notify import send_markdown
from report import day_window
from source import SourceDB

LAST_TS_KEY = "consume_last_ts"
CUMULATIVE_KEY = "consume_cumulative"


def _as_int(text, default=0):
    try:
        return int(str(text or "").strip() or default)
    except (TypeError, ValueError):
        return default


class ConsumptionMonitor:
    def __init__(self, store):
        self.store = store
        self.log = logging.getLogger("consume")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.status = {
            "runs": 0,
            "last_run": 0,
            "last_quota": 0,
            "pushed": 0,
            "last_error": "",
        }

    def stop(self):
        self._stop.set()

    def run_once(self):
        """执行一轮：统计间隔内消耗 → 按阈值/变动触发推送。返回成功推送条数。"""
        with self._lock:
            return self._run()

    def _run(self):
        config = config_module.effective_config(self.store)
        now = int(time.time())

        if not config["consume_enabled"]:
            # 关闭时把水位推到当前，避免再次开启时把历史消耗一次性补推
            self.store.set_state(LAST_TS_KEY, str(now))
            self.store.set_state(CUMULATIVE_KEY, "0")
            self.status["last_run"] = now
            return 0

        problems = config_module.validate(config)
        if problems:
            self.status["last_error"] = "配置不完整：" + "；".join(problems)
            return 0

        last = _as_int(self.store.get_state(LAST_TS_KEY))
        if last <= 0:
            # 首次运行只落水位，不把历史消耗一次性补推
            self.store.set_state(LAST_TS_KEY, str(now))
            self.store.set_state(CUMULATIVE_KEY, "0")
            self.status["last_run"] = now
            return 0
        if last >= now:
            self.status["last_run"] = now
            return 0

        source = SourceDB(config)
        stats = consumption.compute(
            self.store, source, last, now, config["rate_currency"])
        quota = int(stats.get("quota") or 0)
        self.status["last_quota"] = quota

        pushed = 0
        mobiles = [m for m in (config["dingtalk_at_mobiles"] or "").split(",") if m.strip()]

        # 单次变动通知
        change_quota = _as_int(config["consume_change_quota"])
        if change_quota > 0 and quota >= change_quota:
            if self._push(config, mobiles, "consume_change",
                          "📈 消耗变动通知", stats):
                pushed += 1

        # 累计阈值通知（累计到阈值后清零，重新累计）
        threshold_quota = _as_int(config["consume_threshold_quota"])
        if threshold_quota > 0 and quota > 0:
            cumulative = _as_int(self.store.get_state(CUMULATIVE_KEY)) + quota
            if cumulative >= threshold_quota:
                if self._push(config, mobiles, "consume_threshold",
                              "🚨 消耗达到阈值", stats, cumulative=cumulative):
                    pushed += 1
                cumulative = 0
            self.store.set_state(CUMULATIVE_KEY, str(cumulative))

        self.store.set_state(LAST_TS_KEY, str(now))
        self.status["runs"] += 1
        self.status["pushed"] += pushed
        self.status["last_run"] = now
        self.status["last_error"] = ""
        return pushed

    def _push(self, config, mobiles, kind, title, stats, cumulative=None):
        """发送一条消耗通知并记录历史。返回是否成功。"""
        text = consumption.render_consumption(stats, title=title)[1]
        if cumulative is not None:
            text += (f"\n\n- **累计底层 quota**：{cumulative:,}"
                     f"\n- **累计网站额度**：{consumption.fmt_money(consumption.Decimal(cumulative) / consumption.QUOTA_PER_UNIT)}")
        ok, detail = send_markdown(
            config["dingtalk_webhook"], title, text,
            secret=config["dingtalk_secret"], keyword=config["dingtalk_keyword"],
            at_mobiles=mobiles,
        )
        self.store.add_history({
            "kind": kind,
            "trade_no": f"{stats.get('start_ts')}-{stats.get('end_ts')}",
            "status": "success" if ok else "failed",
            "username": "",
            "money": stats.get("user_money"),
            "money_known": stats.get("user_money") is not None,
            "reason": (f"网站额度 {stats.get('website_units', '未知')}；"
                       f"底层 quota {int(stats.get('quota') or 0):,}"),
            "reason_source": kind,
            "ok": ok,
            "detail": text,
        })
        if ok:
            self.log.info("已推送消耗通知 %s：%s 额度", kind, stats.get("quota"))
        else:
            self.log.warning("消耗通知推送失败 %s：%s", kind, detail)
        return ok

    def run_daily_if_due(self):
        """到点后认领昨日财务日报；发送失败或进程重启也不自动重发。"""
        with self._lock:
            config = config_module.effective_config(self.store)
            if not config["consume_daily_report_enabled"]:
                return 0
            now = time.time()
            local = time.localtime(now)
            hour, minute = map(int, config["consume_daily_report_time"].split(":"))
            if (local.tm_hour, local.tm_min) < (hour, minute):
                return 0
            start, end, day = day_window(now)
            if not self.store.claim_daily_report(day):
                return 0
            try:
                problems = config_module.validate(config)
                if problems:
                    raise ValueError("配置不完整：" + "；".join(problems))
                stats = consumption.compute(
                    self.store, SourceDB(config), start, end, config["rate_currency"])
                title, text = consumption.render_consumption(
                    stats, title=f"每日财务汇总 {day}")
                mobiles = [m.strip() for m in config["dingtalk_at_mobiles"].split(",")
                           if m.strip()]
                ok, detail = send_markdown(
                    config["dingtalk_webhook"], title, text,
                    secret=config["dingtalk_secret"], keyword=config["dingtalk_keyword"],
                    at_mobiles=mobiles)
            except Exception as exc:
                ok, detail = False, str(exc)
                self.log.exception("每日财务汇总失败：%s", exc)
            self.store.finish_daily_report(day, ok, detail)
            if ok:
                self.status["pushed"] += 1
                self.status["last_error"] = ""
            else:
                self.status["last_error"] = str(detail)
                self.log.warning("每日财务汇总推送失败：%s", detail)
            return int(ok)

    def loop(self):
        last_threshold_check = None
        interval = 30
        while not self._stop.is_set():
            try:
                config = config_module.effective_config(self.store)
                interval = max(10, int(config["consume_interval"]))
                if (last_threshold_check is None or
                        time.monotonic() - last_threshold_check >= interval):
                    try:
                        self.run_once()
                    finally:
                        last_threshold_check = time.monotonic()
            except Exception as exc:
                self.status["last_error"] = str(exc)
                self.log.exception("消耗统计出错：%s", exc)
            try:
                self.run_daily_if_due()
            except Exception as exc:
                self.status["last_error"] = str(exc)
                self.log.exception("每日财务调度出错：%s", exc)
            remaining = (interval if last_threshold_check is None else
                         interval - (time.monotonic() - last_threshold_check))
            self._stop.wait(min(30, max(1, remaining)))
