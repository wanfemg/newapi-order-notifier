# newapi-order-notifier

监听 new-api 的**全部支付订单**（钱包充值 + 订阅），把**成功 / 失败 / 过期**结果推送到**钉钉群**，失败与过期订单带**失败原因**。自带 Web 配置界面，一键 Docker 部署。

**不修改 new-api 任何文件** —— 只读它的数据库，所以官方 `git pull` 更新不会覆盖本目录。

## 一键部署（Linux 服务器，new-api 已在运行）

一条命令即可（自动拉取本项目 → 探测 new-api 环境 → 生成配置 → 构建启动）：

```bash
bash <(curl -sSL https://raw.githubusercontent.com/wanfemg/newapi-order-notifier/main/install.sh)
```

已经在本项目目录里的话，等价于：

```bash
cd newapi-order-notifier
bash deploy.sh
```

脚本会自动：检查 docker/compose → 定位 new-api 容器 → 读取它的 `SQL_DSN`/`SQLITE_PATH`/docker 网络（含 host 模式）→ 生成管理界面口令 → 写出 `.env` → 构建镜像并启动。

完成后按部署横幅提示访问界面，填写钉钉 Webhook → 点「测试发送」→ 保存配置。

> 检测不到容器时可用 `NEWAPI_CONTAINER=<容器名> bash deploy.sh` 指定。

### 界面访问安全

管理界面能改配置、能看到推送记录，所以**默认只绑 `127.0.0.1`**：只有服务器本机能访问，不监听公网地址，防火墙/安全组放不放行都无关。从自己电脑打开有两种方式：

```bash
# 方式一：SSH 隧道（推荐，不用改任何配置）
ssh -L 8765:127.0.0.1:8765 <你的用户名>@<服务器IP>
# 然后浏览器打开 http://127.0.0.1:8765
```

方式二：只对内网开放 —— 把 `.env` 里的 `UI_BIND_ADDR` 改成服务器内网地址（如 `192.168.1.10`），再重跑 `bash deploy.sh`。

**用反向代理时不需要改它**：Nginx / 宝塔 / 1Panel 装在宿主机上时，直接反代 `http://127.0.0.1:8765` 就能访问（建议用子域名，不要用子路径，并在反代上加 HTTPS 与访问限制）。只有当反代程序不在宿主机上（例如反代是另一个 docker 容器且不在 `new-api-network` 里）时，才需要把 `UI_BIND_ADDR` 改成那个容器可达的地址，例如宿主机内网 IP。

### 升级

重新执行同一条命令即可，脚本会用 `git reset --hard` 把部署副本对齐远端最新提交，并把当前版本号打印在部署横幅和界面顶栏上（方便确认到底升没升上去）。

如果更新失败或仍显示旧版本，手动执行：

```bash
cd ~/newapi-order-notifier
git fetch --depth 1 origin main && git reset --hard FETCH_HEAD
bash deploy.sh
```

### 手动部署

```bash
cp -n .env.example .env   # 或自行编辑 .env，至少填 UI_PASSWORD 与 SQL_DSN
export COMPOSE_FILE="docker-compose.yml:docker-compose.bridge.yml"
docker compose up -d --build
```

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `SQL_DSN` | new-api 数据库连接串（PostgreSQL / MySQL），与 new-api 的值一致即可 |
| `SQLITE_PATH` | new-api 使用 SQLite 时，容器内看到的 `.db` 路径 |
| `UI_PASSWORD` | 管理界面口令（首次由 `deploy.sh` 生成，也可自行指定） |
| `UI_BIND_PORT` | 界面监听端口（bridge 模式 8080、host 模式 8765，由脚本自动决定） |
| `UI_PUBLISH_PORT` | 对外发布端口，默认 8765 |
| `UI_BIND_ADDR` | 宿主机侧绑定地址，默认 `127.0.0.1`（仅本机）；改内网 IP 可对内网开放，`0.0.0.0` 为对外全开 |
| `UI_BIND` | 容器内监听地址，默认 `0.0.0.0`；host 模式下由 `deploy.sh` 跟随 `UI_BIND_ADDR` |
| `POLL_INTERVAL` | 轮询间隔，默认 `10s` |
| `DATA_DIR` | 本程序自己的 SQLite 存放目录，默认 `/data` |

## 失败原因从哪里来

new-api 的订单表**只存状态，不存失败原因**（`UpdatePendingTopUpStatus` 不接收 error，`ExpireSubscriptionOrder` 也没有原因字段），所以本项目**不依赖 new-api 的运行日志**，只按订单自身能拿到的信息给出原因，并在消息里标注来源：

1. **过期订单** → 由订单数据推导：`支付超时：下单后未在支付有效期内完成支付（下单时间 …）`，来源标注 `订单数据`；
2. **失败订单** → 先看订单的 `provider_payload`（渠道回调原文）里的 `error`/`message`/`code`，来源标注 `订单数据`；
3. `provider_payload` 里没有 → 按渠道推导，如 `易支付 下单或回调失败`，来源标注 `推导`。

## 推送格式（钉钉 markdown）

**钱包充值成功**

```
### 💰 充值成功
- **订单号**：TP1750000001-ab12cd
- **用户**：alice（ID 12）
- **类型**：钱包充值
- **金额**：¥99.00
- **额度**：+500,000
- **支付方式**：微小信
- **时间**：2026-10-16 15:30:22
```

**钱包充值失败**

```
### ❌ 充值失败
- **订单号**：TP1750000002-ef34gh
- **用户**：bob（ID 20）
- **类型**：钱包充值
- **金额**：¥50.00
- **状态**：failed（失败）
- **失败原因**：易支付 下单或回调失败（来源：推导）
- **支付方式**：微小信
- **时间**：2026-10-16 15:31:05
```

**充值超时**

```
### ⏰ 充值超时
- **订单号**：TP1750000003-op78qr
- **用户**：carol（ID 33）
- **类型**：钱包充值
- **金额**：¥100.00
- **状态**：expired（已过期）
- **失败原因**：支付超时：下单后未在支付有效期内完成支付（下单时间 2026-10-16 15:00:03）（来源：订单数据）
- **支付方式**：支小宝
```

订阅订单同理，标题为 `🎫 订阅开通成功` / `❌ 订阅支付失败` / `⏰ 订阅支付超时`，类型显示 `订阅（专业版）`。

补发推送的消息标题带 `（补发推送）` 后缀，例如 `### 💰 充值成功（补发推送）`。

### 用户与支付方式显示规则

- **用户**：显示 new-api 的**用户名**（`users.username`），而不是显示名 `display_name` —— root 账号的显示名是 new-api 写死的 `Root User`，不具参考价值。
- **支付方式**：取订单的 `payment_method`（真实支付类型），不是网关名 `payment_provider`。epay 网关下 `payment_method` 是 `wxpay` / `alipay`；映射为 **微信支付 → 微小信、支付宝 → 支小宝**，其他方式（`stripe`、`waffo` 等）原样显示；`payment_method` 为空时回退显示网关名。

## 行为说明

- 推送事件、订单范围、轮询间隔、回溯开关都在界面里配置，改完立即生效（下一轮生效）。
- 去重键为 `订单类型 + 订单号 + 状态`，同一状态只推一次；发送失败不记账，下一轮自动重试。
- **推送补发**：界面「推送补发」里填回溯小时数，即可把最近 N 小时的订单重推一遍（不受去重限制），这些消息的标题会标注 **（补发推送）**。打开「首次运行补推历史订单」后，首次运行回溯的存量订单同样标注。
- **首次运行只登记存量订单、不补推历史消息**（避免刷屏）；需要补推就打开「首次运行回溯历史订单」或直接用「推送补发」。
- 补发与自动轮询互斥执行，不会把同一条订单同时推两次。
- 界面需口令登录，口令以 PBKDF2-SHA256 加盐保存，会话令牌仅存内存、HttpOnly + SameSite=Strict，登录失败限速。
- 本程序自己的数据（配置、去重、推送历史）在 `./data/notifier.db`。

## 本地自测

```bash
python tests/test_smoke.py
```

会造一份带订单的 SQLite 库 + 一个假钉钉服务，断言：首次只登记、失败原因按渠道推导、订阅失败原因取自 provider_payload、过期原因取自订单数据、重复轮询不重推、补发绕过去重并标注「补发推送」。
