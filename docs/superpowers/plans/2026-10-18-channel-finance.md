# 渠道财务配置与消耗分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将财务后台升级为渠道级收费、成本、历史账务、利润分析和每日推送系统。

**Architecture:** 在财务后台自己的 SQLite 中保存当前渠道配置和不可变生效快照；从 new-api 只读读取日志、用户和渠道；计算层按日志时间选择快照并输出渠道、用户和全站账务；Web API 与原生前端复用现有认证和路由。

**Tech Stack:** Python 标准库、SQLite、原生 HTML/CSS/JavaScript、现有 smoke test。

---

### Task 1: 扩展独立存储

**Files:**
- Modify: `app/store.py`
- Test: `tests/test_smoke.py`

- [ ] 增加渠道当前配置表，主键为 `channel_id`，保存收费汇率、成本单价、货币、渠道名称和更新时间。
- [ ] 增加不可变渠道配置历史表，保存 `effective_at`，并为 `(channel_id, effective_at)` 建索引。
- [ ] 增加每日推送运行记录，使用日期唯一键实现幂等。
- [ ] 提供 `upsert_channel_finance`、`delete_channel_finance`、`list_channel_finance`、`list_channel_finance_history`、`find_channel_snapshot` 和每日推送记录方法。
- [ ] 运行 `py tests/test_smoke.py`，确认旧存储测试全部通过。

### Task 2: 重写渠道配置与历史服务

**Files:**
- Modify: `app/rates.py`
- Modify: `app/config.py`
- Test: `tests/test_smoke.py`

- [ ] 将收费汇率解析、成本单价解析和 `HH:MM` 推送时间校验集中到服务边界。
- [ ] 新增按渠道 CRUD 服务，修改时先写历史快照再更新当前配置。
- [ ] 删除只删除当前配置并写入删除事件，不删除历史快照。
- [ ] 收费币种在 API 边界固定 CNY，非 CNY 请求原子拒绝；成本币种独立保留。
- [ ] 未配置渠道仍展示网站额度，人民币未知或不完整；全局汇率 CRUD/history 保留但不参与消费计价；缺成本不等于零。
- [ ] 增加历史匹配和非法金额、非法时间测试。

### Task 3: 重写消耗账务计算

**Files:**
- Modify: `app/consumption.py`
- Modify: `app/source.py`
- Test: `tests/test_smoke.py`

- [ ] 确认日志读取返回 `channel_id`、渠道名称、用户标识、quota 和时间。
- [ ] 按日志时间调用快照匹配；网站额度 = raw quota / 500000，折算 CNY = 网站额度 / 历史渠道比例，成本 = raw quota × 成本单价。
- [ ] 总计、渠道、用户及用户渠道新增 website_units 十进制字符串；保留 quota 整数；按未格式化 CNY 排行。
- [ ] 完整性警告累计保留，缺失金额或外币成本时不输出精确利润；名称统一为“折算消费利润”，不代表实收现金。
- [ ] 输出渠道汇总、用户总汇总、用户渠道明细、全站合计和收入前五名。
- [ ] 使用 Decimal、六位精度和 ROUND_HALF_UP，避免浮点金额误差。
- [ ] 增加跨渠道、跨历史配置、未配置成本和并列排行测试。

### Task 4: 接入每日财务推送

**Files:**
- Modify: `app/consume_monitor.py`
- Modify: `app/main.py`
- Modify: `app/notify.py`
- Test: `tests/test_smoke.py`

- [ ] 增加每日 `HH:MM` 调度配置和本地日期判断。
- [ ] 每日首次到点生成财务汇总，调用现有通知适配器发送。
- [ ] 日报和阈值消息同时展示网站额度、raw quota、折算人民币消费、成本、折算消费利润和用户前五名；阈值仍按 raw quota 触发。
- [ ] 使用独立运行记录避免重启重复推送。
- [ ] 保留现有间隔阈值告警并测试两种推送互不干扰。

### Task 5: 扩展 Web API

**Files:**
- Modify: `app/webui.py`
- Test: `tests/test_smoke.py`

- [ ] 增加渠道配置列表、新增修改、删除和历史接口。
- [ ] 增加渠道财务消耗、用户渠道明细、全站利润和排行接口字段。
- [ ] 增加每日推送时间读取和保存接口。
- [ ] 所有写接口继续使用认证、CSRF、请求体大小和 Decimal 校验。
- [ ] 增加 HTTP 层 CRUD、鉴权、非法输入和只读源库测试。

### Task 6: 重做前端财务页面

**Files:**
- Modify: `web/app.js`
- Modify: `web/style.css`
- Test: `tests/test_smoke.py`

- [ ] 汇率页面改为渠道配置表，支持新增、修改、删除和历史弹窗区域。
- [ ] 消耗页面增加渠道收入、上游成本、利润和配置状态。
- [ ] 用户页面增加用户总消费和按渠道展开明细。
- [ ] 报表页面增加每日推送时间、最近推送状态、全站利润和前五名排行。
- [ ] 沿用现有液态玻璃主题、响应式表格和无障碍状态文本。
- [ ] 运行完整测试并检查静态页面关键节点。

### Task 7: 回归与交付检查

**Files:**
- Modify: `汇率与消耗统计使用说明.md`
- Modify: `.env.example`

- [ ] 更新渠道配置、历史账务、每日推送和利润口径说明。
- [ ] 覆盖 3500000 quota / 1:7 = 7 网站额度 / 1 CNY、多渠道比例、同 quota 不同排行、历史比例、缺配置及 raw quota 成本。
- [ ] 按顺序运行 smoke、channel_finance、daily_finance、finance_api 四组测试。
- [ ] 运行 `node --check web/app.js` 和 `py -m compileall -q app`，按实际结果报告。
- [ ] 运行 `git diff --check`，确认无空白错误。
- [ ] 检查 `newapi源项目` 无任何修改。
