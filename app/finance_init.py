"""逐渠道历史财务初始化，仅写入后台的覆盖规则。"""

import copy
import threading
import time

import config
from rates import parse_rate
from source import SourceDB


class FinanceInitializer:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._state = self._empty_state()

    @staticmethod
    def _empty_state():
        return {
            "status": "idle", "total": 0, "completed": 0, "success": 0,
            "skipped": 0, "failed": 0, "current_channel": None, "results": [],
            "error": "任务仅保存在内存；进程重启后不恢复未完成任务，已成功保存的覆盖规则仍然保留。",
        }

    def status(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def start(self):
        with self._lock:
            if self._state["status"] == "running":
                return copy.deepcopy(self._state)
            end = int(time.time())
            site, channels = self.store.finance_initialization_snapshot(end)
            if not site:
                raise ValueError("请先保存当前站点比例")
            source_config = config.effective_config(self.store)
            self._state = self._empty_state()
            self._state.update(status="running", total=len(channels), error="")
            try:
                worker = threading.Thread(
                    target=self._run, args=(site, channels, end, source_config), daemon=True)
                worker.start()
            except Exception as exc:
                self._state.update(status="failed", error=f"启动初始化失败：{exc}")
                raise RuntimeError(self._state["error"]) from exc
            return copy.deepcopy(self._state)

    def _run(self, site, channels, end, source_config):
        try:
            source = SourceDB(source_config)
            for item, snapshot in channels:
                channel = {"channel_id": item["channel_id"],
                           "channel_name": item["channel_name"]}
                with self._lock:
                    self._state["current_channel"] = channel
                try:
                    status, message = self._initialize_channel(source, site, snapshot, end)
                except Exception as exc:
                    status, message = "failed", f"渠道初始化失败：{exc}"
                with self._lock:
                    self._state["results"].append(dict(channel, status=status, message=message))
                    self._state[status] += 1
                    self._state["completed"] += 1
                    self._state["current_channel"] = None
            with self._lock:
                self._state["status"] = "completed"
        except Exception as exc:
            with self._lock:
                self._state.update(status="failed", current_channel=None,
                                   error=f"初始化任务失败：{exc}")

    def _initialize_channel(self, source, site, snapshot, end):
        if not snapshot or snapshot["fee_currency"] != "CNY":
            return "skipped", "渠道未配置有效的 CNY 比例"
        try:
            channel_rate = str(parse_rate(snapshot["fee_rate"]))
            site_rate = str(parse_rate(site["rate"]))
        except ValueError as exc:
            return "skipped", f"比例无效：{exc}"
        cid = snapshot["channel_id"]
        summary = source.channel_consumption_summary(cid, end)
        if summary["first_at"] is None:
            return "skipped", "该渠道没有可重算的过去消耗日志"
        payload = {
            "channel_id": cid, "start": 0, "end": end,
            "quota": int(summary["quota"]), "earliest_log": int(summary["first_at"]),
            "channel_snapshot_id": snapshot["id"], "site_snapshot_id": site["id"],
            "channel_rate": channel_rate, "site_rate": site_rate,
        }
        token = self.store.save_finance_preview(payload)
        self.store.confirm_finance_preview(token, cid)
        return "success", f"历史覆盖规则已保存，实际消耗额度 {payload['quota']}"
