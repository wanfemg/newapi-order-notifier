"""入口：加载配置 → 启动轮询线程 → 启动配置界面。"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as config_module
import store as store_module
import webui
from poller import Poller
from report import DailyReporter


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("main")

    data_dir = config_module.DATA_DIR
    os.makedirs(data_dir, exist_ok=True)
    store = store_module.Store(os.path.join(data_dir, "notifier.db"))

    credential = webui.ensure_admin(store)
    if credential:
        password, generated = credential
        if generated:
            log.warning("已生成管理界面口令：%s （登录后请及时更换：设置 UI_PASSWORD 环境变量）", password)
        else:
            log.info("管理界面口令来自 UI_PASSWORD 环境变量")

    poller = Poller(store)
    try:
        poller.run_once()
    except Exception as exc:
        log.warning("首次轮询未完成：%s", exc)

    reporter = DailyReporter(store)

    import threading
    threading.Thread(target=poller.loop, name="poller", daemon=True).start()
    threading.Thread(target=reporter.loop, name="daily-reporter", daemon=True).start()

    log.info("订单通知服务已启动，配置界面 http://%s:%s",
             config_module.UI_BIND, config_module.UI_PORT)
    try:
        webui.serve(store, poller, reporter, config_module.UI_BIND, config_module.UI_PORT)
    except KeyboardInterrupt:
        log.info("收到退出信号，正在停止")
        poller.stop()
        reporter.stop()


if __name__ == "__main__":
    main()
