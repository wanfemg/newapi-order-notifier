# 渠道财务配置与消耗分析设计

## 目标与边界
渠道级充值比例、成本、不可变历史账务及每日消费排行。new-api 源码与数据库只读；配置、快照和通知记录写入财务后台自己的 SQLite。保留原液态玻璃 UI，不提交、不推送。

## 单位与计算模型
- 渠道唯一键为 `channel_id`，名称仅展示。
- NewAPI `QuotaPerUnit = 500000`，日志 `quota` 是原始整数。
- `website_units = Decimal(quota) / 500000`。
- 渠道充值比例 `1:N` 表示 1 元人民币兑换 N 网站额度。
- `CNY consumption = website_units / historical_channel_fee_rate`。
- `cost = Decimal(quota) × cost_per_quota`，成本仍以 raw quota 计价。
- 例如 3,500,000 quota、1:7，得到 7 网站额度、1 CNY 折算消费。
- 使用 Decimal，展示六位小数、ROUND_HALF_UP；排行按未格式化 CNY 金额降序，同额按用户 ID 升序。

## 数据与历史
当前配置保存 `channel_id`、`channel_name`、`fee_currency`、`fee_rate`、`cost_currency`、`cost_per_quota`、`updated_at`。
每次新增、修改、删除原子写入不可变快照。按日志时间选取 `effective_at <= created_at` 的最近快照，同秒按历史 ID 确定顺序。Web 不接受回填生效时间。
删除前的日志仍按当时配置计算；删除后的空档不沿用删除配置，也不复活旧成本。
全局汇率 CRUD 与历史独立保留，但完全不参与消费统计，不作为缺配置渠道的回退。

## 完整性与 API
新渠道收费货币固定 CNY，API 拒绝其他 `fee_currency`，拒绝不得写入当前表或历史表。旧外币收费快照保留，但不解释为 CNY。
缺渠道比例仍输出网站额度，CNY 为 null 或已知部分；缺成本不是零成本。成本币种独立，不自动汇兑。
`income_complete` 表示折算收入是否完整；内部 `cost_complete`（对外 `cost_configured`）表示成本配置是否完整；`complete` 同时要求收入、成本完整且不存在跨币种等警告。
只有数据完整且均可按 CNY 比较时才输出“折算消费利润”，不声称实收现金利润。未知值为 null，已知部分须明确标记不完整。
总计、`channels`、`users`、`users[].channels` 均保留整数 `quota`，增加十进制字符串 `website_units`；分币种成本单独保留。
所有 API 保留认证、CSRF 和只读源库约束。

## 页面与推送
消费页、用户详情、日报和阈值消息同时展示网站额度与折算人民币，raw quota 为辅助信息。阈值仍按整数 raw quota 触发。未知推送历史金额通过 `money_known` 区分，不冒充零。
每日按服务器本地自然日统计昨日，以日期唯一键认领；sending 或 failed 不自动重试。每日任务与间隔告警独立。

## 验证
覆盖 3,500,000 quota / 1:7、多渠道比例、相同 quota 不同金额排行、未舍入排行、历史变化、删除空档、缺配置、raw quota 成本、外币与无效成本保护、日报双单位、API 非 CNY 原子拒绝、原充值功能回归。
