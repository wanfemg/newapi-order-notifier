/* new-api 订单通知 · 内嵌管理界面
 * 所有写请求都带 X-Requested-With（CSRF）与 JSON Content-Type，与 webui.py 契约一致。
 */
'use strict';

const CSRF_HEADER = 'X-Requested-With';
const CSRF_VALUE = 'newapi-order-notifier';

const FIELDS = {
  'group-dingtalk': [
    { key: 'dingtalk_webhook', label: 'Webhook 地址', hint: '钉钉自定义机器人的 Webhook，必须 https://' },
    { key: 'dingtalk_secret', label: '加签密钥', hint: '机器人安全设置选“加签”时填写，否则留空' },
    { key: 'dingtalk_keyword', label: '自定义关键词', hint: '安全设置选“自定义关键词”时填写，否则留空' },
    { key: 'dingtalk_at_mobiles', label: '@手机号', hint: '多个手机号用英文逗号分隔，可留空' },
  ],
  'group-source': [
    { key: 'db_type', label: '数据库类型', hint: '与 new-api 使用的数据库一致', type: 'select',
      options: [['postgres', 'PostgreSQL'], ['mysql', 'MySQL'], ['sqlite', 'SQLite']] },
    { key: 'db_dsn', label: 'SQL_DSN', hint: 'PostgreSQL / MySQL 连接串，与 new-api 的 SQL_DSN 一致' },
    { key: 'sqlite_path', label: 'SQLite 文件路径', hint: 'SQLite 模式下 new-api 的 .db 文件在容器内的路径' },
  ],
  'group-push': [
    { key: 'enabled', label: '启用推送', hint: '关闭后仅记录状态变更，不发送钉钉' },
    { key: 'poll_interval', label: '轮询间隔（秒）', hint: '默认 10 秒，最小 5 秒' },
    { key: 'push_success', label: '推送成功订单', hint: '钱包充值成功 / 订阅开通成功' },
    { key: 'push_failed', label: '推送失败订单', hint: '支付失败，原因取自订单数据或渠道推导' },
    { key: 'push_expired', label: '推送超时订单', hint: '支付超时，原因由订单数据推导' },
    { key: 'expire_minutes', label: '超时未支付判定（分钟）', hint: 'new-api 不会把超时订单写成 expired，这里按「创建后超过 N 分钟仍待支付」推导为超时；0 表示关闭，N 建议与支付渠道的订单有效期一致' },
    { key: 'scope_wallet', label: '包含钱包充值', hint: 'top_ups 表订单' },
    { key: 'scope_subscription', label: '包含订阅订单', hint: 'subscription_orders 表订单' },
    { key: 'daily_report', label: '每日充值统计推送', hint: '每天 00:00 统计前一天的充值并推送排行榜' },
    { key: 'backfill', label: '首次运行补推历史订单', hint: '默认关闭：首次只登记存量；补推的历史订单会标注「补发推送」' },
    { key: 'lookback_hours', label: '回溯窗口（小时）', hint: '每轮扫描最近 N 小时的订单' },
  ],
};

// 敏感项：界面只显示掩码，留空提交表示保持已保存的值（掩码需与 config.py 的 MASK 一致）
const SECRET_KEYS = new Set(['db_dsn', 'dingtalk_webhook', 'dingtalk_secret']);
const MASK = '••••••••';

const STATUS_TEXT = { success: '成功', failed: '失败', expired: '已过期' };
const SOURCE_TEXT = { order: '订单数据', derived: '推导' };
const BOOL_KEYS = new Set([
  'enabled', 'push_success', 'push_failed',
  'push_expired', 'scope_wallet', 'scope_subscription', 'backfill', 'daily_report',
]);
const INT_KEYS = new Set(['poll_interval', 'lookback_hours', 'expire_minutes']);

const $ = (id) => document.getElementById(id);
const inputs = {};

let refreshTimer = null;

/* ---------- 请求 ---------- */

async function api(path, { method = 'GET', body } = {}) {
  const options = { method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json';
    options.headers[CSRF_HEADER] = CSRF_VALUE;
    if (body !== undefined) options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = null;
  try {
    data = await response.json();
  } catch (err) {
    data = {};
  }
  return { ok: response.ok, status: response.status, data: data || {} };
}

function setMessage(id, text, kind) {
  const node = $(id);
  node.textContent = text || '';
  node.classList.toggle('ok', kind === 'ok');
  node.classList.toggle('bad', kind === 'bad');
}

/* ---------- 界面切换 ---------- */

function showLogin() {
  $('login').classList.remove('hidden');
  $('app').classList.add('hidden');
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
  $('password').focus();
}

function showApp() {
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  if (!refreshTimer) refreshTimer = setInterval(loadStatus, 15000);
}

/* ---------- 字段渲染 ---------- */

function buildField(spec) {
  const wrap = document.createElement('div');
  wrap.className = 'field';

  const label = document.createElement('label');
  label.textContent = spec.label;
  label.htmlFor = `f-${spec.key}`;
  wrap.appendChild(label);

  let input;
  if (spec.type === 'select') {
    input = document.createElement('select');
    for (const [value, text] of spec.options) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = text;
      input.appendChild(option);
    }
  } else if (BOOL_KEYS.has(spec.key)) {
    input = document.createElement('input');
    input.type = 'checkbox';
  } else {
    input = document.createElement('input');
    input.type = INT_KEYS.has(spec.key) ? 'number' : 'text';
    input.autocomplete = 'off';
  }
  input.id = `f-${spec.key}`;
  inputs[spec.key] = input;

  if (BOOL_KEYS.has(spec.key)) {
    const row = document.createElement('div');
    row.className = 'switch';
    row.appendChild(input);
    const caption = document.createElement('span');
    caption.className = 'muted';
    caption.textContent = spec.hint || '';
    row.appendChild(caption);
    wrap.appendChild(row);
  } else {
    wrap.appendChild(input);
    const hint = document.createElement('span');
    hint.className = 'hint';
    hint.textContent = spec.hint || '';
    wrap.appendChild(hint);
  }
  return wrap;
}

function renderFields() {
  for (const [containerId, specs] of Object.entries(FIELDS)) {
    const container = $(containerId);
    container.textContent = '';
    for (const spec of specs) container.appendChild(buildField(spec));
  }
}

function fill(config, envOverridden, secrets) {
  const overridden = new Set(envOverridden || []);
  const hasSecret = secrets || {};
  for (const specs of Object.values(FIELDS)) {
    for (const spec of specs) {
      const input = inputs[spec.key];
      const hint = input.closest('.field').querySelector('.hint, .switch .muted');
      let text = spec.hint || '';

      if (SECRET_KEYS.has(spec.key)) {
        // 已保存的值不返回前端，只显示掩码；留空提交表示不修改
        const savedFlag = Boolean(hasSecret[spec.key]);
        input.value = savedFlag ? MASK : '';
        input.placeholder = savedFlag ? '已保存，留空表示不修改' : '';
      } else if (BOOL_KEYS.has(spec.key)) {
        input.checked = Boolean(config[spec.key]);
      } else {
        const value = config[spec.key];
        input.value = value === undefined || value === null ? '' : String(value);
      }

      if (overridden.has(spec.key)) text += '（当前被环境变量覆盖）';
      if (hint) hint.textContent = text;
    }
  }
}

function collect() {
  const values = {};
  for (const specs of Object.values(FIELDS)) {
    for (const spec of specs) {
      const input = inputs[spec.key];
      if (SECRET_KEYS.has(spec.key)) {
        const raw = input.value.trim();
        // 掩码或空值 = 用户没打算改，交给服务端保留原值
        if (raw === '' || raw === MASK) continue;
        values[spec.key] = raw;
        continue;
      }
      values[spec.key] = BOOL_KEYS.has(spec.key) ? input.checked : input.value.trim();
    }
  }
  return values;
}

/* ---------- 状态与记录 ---------- */

function formatTime(seconds) {
  if (!seconds) return '-';
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function renderStatus(status) {
  const bar = $('statusbar');
  bar.textContent = '';
  const parts = [
    ['运行轮次', status.runs],
    ['上次运行', status.last_run ? formatTime(status.last_run) : '-'],
    ['本轮订单', status.last_orders],
    ['累计推送', status.pushed_total],
    ['版本', status.version || '-'],
  ];
  for (const [name, value] of parts) {
    const span = document.createElement('span');
    span.textContent = `${name}: ${value}`;
    bar.appendChild(span);
  }
  const warnings = status.warnings || [];
  if (warnings.length) {
    setMessage('save-msg', `⚠️ ${warnings.join('；')}`, 'bad');
  } else if (status.last_error) {
    setMessage('save-msg', `⚠️ ${status.last_error}`, 'bad');
  }
}

function cell(row, text, className) {
  const td = document.createElement('td');
  if (className) td.className = className;
  td.textContent = text === undefined || text === null ? '' : String(text);
  row.appendChild(td);
  return td;
}

function badgeCell(row, text, className) {
  const td = document.createElement('td');
  const span = document.createElement('span');
  span.className = `badge ${className}`;
  span.textContent = text;
  td.appendChild(span);
  row.appendChild(td);
  return td;
}

function renderHistory(items) {
  const body = $('history');
  body.textContent = '';
  if (!items.length) {
    const row = document.createElement('tr');
    const td = cell(row, '暂无推送记录');
    td.colSpan = 8;
    td.className = 'muted';
    body.appendChild(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement('tr');
    if (item.detail) row.title = item.detail;
    cell(row, formatTime(item.pushed_at));
    badgeCell(row, STATUS_TEXT[item.status] || item.status, item.status);
    cell(row, item.trade_no);
    cell(row, item.username);
    cell(row, `¥${Number(item.money || 0).toFixed(2)}`);
    cell(row, item.reason || '-', 'reason');
    badgeCell(row, SOURCE_TEXT[item.reason_source] || item.reason_source || '-', 'source');
    const pushText = (item.ok ? '✅ 成功' : '❌ 失败') + (item.backfill ? ' · 📮 补发推送' : '');
    cell(row, pushText, item.ok ? 'ok' : 'bad');
    body.appendChild(row);
  }
}

/* ---------- 动作 ---------- */

async function loadConfig() {
  const { ok, status, data } = await api('/api/config');
  if (status === 401) return showLogin();
  if (!ok) return setMessage('save-msg', `读取配置失败：${data.error || status}`, 'bad');
  fill(data.config || {}, data.env_overridden || [], data.secrets || {});
}

async function loadStatus() {
  const { status, data } = await api('/api/status');
  if (status === 401) return showLogin();
  if (status === 200) renderStatus(data);
}

async function loadHistory() {
  const { ok, status, data } = await api('/api/history');
  if (status === 401) return showLogin();
  if (ok) renderHistory(data.items || []);
}

async function boot() {
  const { data } = await api('/api/session');
  if (!data.authed) return showLogin();
  showApp();
  await loadConfig();
  await Promise.all([loadStatus(), loadHistory()]);
}

function bind() {
  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    setMessage('login-error', '');
    const { ok, data } = await api('/api/login', {
      method: 'POST', body: { password: $('password').value },
    });
    if (!ok) return setMessage('login-error', data.error || '登录失败');
    $('password').value = '';
    await boot();
  });

  $('logout').addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST' });
    showLogin();
  });

  $('save').addEventListener('click', async () => {
    const { ok, data } = await api('/api/config', {
      method: 'POST', body: { config: collect() },
    });
    if (!ok) return setMessage('save-msg', `保存失败：${data.error || '未知错误'}`, 'bad');
    const warnings = data.warnings || [];
    setMessage('save-msg',
      warnings.length ? `已保存，但仍有问题：${warnings.join('；')}` : '✅ 已保存并生效',
      warnings.length ? 'bad' : 'ok');
    await loadConfig();
  });

  $('test').addEventListener('click', async () => {
    setMessage('test-msg', '发送中…');
    const { ok, data } = await api('/api/test', { method: 'POST' });
    setMessage('test-msg', ok ? `✅ ${data.detail || '发送成功'}` : `❌ ${data.error || data.detail || '发送失败'}`,
      ok ? 'ok' : 'bad');
  });

  $('check').addEventListener('click', async () => {
    setMessage('check-msg', '连接中…');
    const { ok, data } = await api('/api/check', { method: 'POST' });
    setMessage('check-msg', ok ? `✅ ${data.detail || '连接正常'}` : `❌ ${data.detail || '连接失败'}`,
      ok ? 'ok' : 'bad');
  });

  $('backfill').addEventListener('click', async () => {
    const hours = Number($('bf-hours').value);
    if (!Number.isInteger(hours) || hours < 1 || hours > 720) {
      return setMessage('bf-msg', '回溯小时数需要在 1 ~ 720 之间', 'bad');
    }
    if (!confirm(`确定补发最近 ${hours} 小时的订单？消息会标注「补发推送」。`)) return;
    setMessage('bf-msg', '补发中…（订单较多时需要等一会儿）');
    const { ok, data } = await api('/api/backfill', { method: 'POST', body: { hours } });
    if (!ok) return setMessage('bf-msg', `❌ ${data.error || '补发失败'}`, 'bad');
    setMessage('bf-msg', `✅ 已补发 ${data.count} 条（最近 ${data.hours} 小时）`, 'ok');
    await loadHistory();
    await loadStatus();
  });

  $('refresh').addEventListener('click', () => {
    loadStatus();
    loadHistory();
  });

  $('daily').addEventListener('click', async () => {
    setMessage('daily-msg', '统计中…');
    const { ok, data } = await api('/api/daily-report', { method: 'POST' });
    setMessage('daily-msg', ok ? `✅ ${data.detail || '已推送'}` : `❌ ${data.detail || '推送失败'}`,
      ok ? 'ok' : 'bad');
  });

  $('clear').addEventListener('click', async () => {
    if (!confirm('确定清空所有推送记录？')) return;
    const { ok } = await api('/api/history/clear', { method: 'POST' });
    if (ok) await loadHistory();
  });
}

renderFields();
bind();
boot();
