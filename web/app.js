/* new-api 订单通知 · 财务后台（液态玻璃，浅/深主题，hash 路由）
 * 写请求都带 X-Requested-With（CSRF）与 JSON Content-Type，与 webui.py 契约一致。
 * 图表全部自绘 SVG（受 CSP 限制不能引第三方库，也不能内联 <style>）。
 */
'use strict';

const CSRF_HEADER = 'X-Requested-With';
const CSRF_VALUE = 'newapi-order-notifier';
const SVGNS = 'http://www.w3.org/2000/svg';

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
    { key: 'weekly_report', label: '每周充值统计推送', hint: '每周一 00:00 统计上一个完整自然周，使用服务端时区' },
    { key: 'large_order_threshold', label: '大额订单阈值（元）', hint: '非负小数，默认 50；达到阈值的成功订单标为大额', default: 50 },
    { key: 'min_push_amount', label: '成功订单最低推送金额（元）', hint: '非负小数，默认 0；仅过滤低于阈值的成功推送，不影响失败和超时通知', default: 0 },
    { key: 'backfill', label: '首次运行补推历史订单', hint: '默认关闭：首次只登记存量；补推的历史订单会标注「补发推送」' },
    { key: 'lookback_hours', label: '回溯窗口（小时）', hint: '每轮扫描最近 N 小时的订单' },
  ],
};

const SECRET_KEYS = new Set(['db_dsn', 'dingtalk_webhook', 'dingtalk_secret']);
const MASK = '内容被猫猫大王吃掉了';
const STATUS_TEXT = { success: '成功', failed: '失败', expired: '已过期' };
const SOURCE_TEXT = { order: '订单数据', derived: '推导', daily: '每日统计', weekly: '每周统计' };
const BUCKET_TEXT = { success: '成功', failed: '失败' };
const BOOL_KEYS = new Set([
  'enabled', 'push_success', 'push_failed',
  'push_expired', 'scope_wallet', 'scope_subscription', 'backfill', 'daily_report', 'weekly_report',
]);
const INT_KEYS = new Set(['poll_interval', 'lookback_hours', 'expire_minutes']);
const DECIMAL_KEYS = new Set(['large_order_threshold', 'min_push_amount']);

const $ = (id) => document.getElementById(id);
const inputs = {};

let currentRange = 7;
let orderFilters = new Set(['success', 'failed']);
let orderRequest = 0;
let settingsBuilt = false;
let refreshTimer = null;
let pageVersion = 0;
let configRequest = 0;
let configRevision = 0;
let configLoaded = false;
let statusRequest = 0;
let historyRequest = 0;
let reportsRequest = 0;
let reportsBuilt = false;
let userSearch = '';

/* ---------- 工具 ---------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function cssVar(name) {
  return `var(${name})`;
}

function fmtYuan(n) {
  const v = Number(n) || 0;
  return '¥' + v.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtCompact(n) {
  const v = Number(n) || 0;
  if (v >= 100000000) return (v / 100000000).toFixed(1) + '亿';
  if (v >= 10000) return (v / 10000).toFixed(1) + '万';
  return v.toFixed(0);
}

function formatTime(seconds) {
  if (!seconds) return '-';
  const date = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function empty(root, text) {
  root.textContent = '';
  root.appendChild(el('div', 'empty', text || '暂无数据'));
}

/* ---------- 请求 ---------- */

async function api(path, { method = 'GET', body } = {}) {
  const options = { method, headers: {}, credentials: 'same-origin' };
  if (method !== 'GET') {
    options.headers['Content-Type'] = 'application/json';
    options.headers[CSRF_HEADER] = CSRF_VALUE;
    if (body !== undefined) options.body = JSON.stringify(body);
  }
  try {
    const response = await fetch(path, options);
    let data;
    try {
      data = await response.json();
    } catch {
      return { ok: false, status: response.status, data: { error: `服务返回无效响应（HTTP ${response.status}）` } };
    }
    if (!data || typeof data !== 'object' || Array.isArray(data)) {
      return { ok: false, status: response.status, data: { error: '服务返回无效数据' } };
    }
    return { ok: response.ok && data.ok !== false, status: response.status, data };
  } catch {
    return { ok: false, status: 0, data: { error: '网络连接失败，请检查连接后重试；发送结果可能未知，请先核对钉钉消息' } };
  }
}

function setMessage(id, text, kind) {
  const node = $(id);
  if (!node) return;
  node.setAttribute('role', 'status');
  node.textContent = text || '';
  node.classList.toggle('ok', kind === 'ok');
  node.classList.toggle('bad', kind === 'bad');
}

/* ---------- 主题 ---------- */

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  try { localStorage.setItem('theme', theme); } catch { /* 存储不可用时仍允许切换主题 */ }
  const toggle = $('theme-toggle');
  if (toggle) {
    toggle.textContent = theme === 'dark' ? '浅色' : '深色';
    toggle.setAttribute('aria-label', `切换为${toggle.textContent}主题`);
  }
}

function initTheme() {
  let saved;
  try { saved = localStorage.getItem('theme'); } catch { /* 使用系统主题 */ }
  const theme = ['light', 'dark'].includes(saved) ? saved : (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  applyTheme(theme);
}

/* ---------- 界面切换 ---------- */

function showLogin() {
  pageVersion++;
  configRequest++;
  statusRequest++;
  historyRequest++;
  reportsRequest++;
  $('login').classList.remove('hidden');
  $('app').classList.add('hidden');
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  $('password').focus();
}

function showApp() {
  $('login').classList.add('hidden');
  $('app').classList.remove('hidden');
  if (!refreshTimer) refreshTimer = setInterval(loadStatus, 15000);
}

/* ---------- 时间范围 ---------- */

const RANGES = [['1', '今天'], ['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天']];

function buildRange() {
  const wrap = $('range');
  wrap.textContent = '';
  for (const [days, label] of RANGES) {
    const btn = el('button', Number(days) === currentRange ? 'on' : '', label);
    btn.addEventListener('click', () => {
      currentRange = Number(days);
      for (const b of wrap.querySelectorAll('button')) b.classList.toggle('on', b.textContent === label);
      if (!['settings', 'reports'].includes(currentRoute())) renderCurrent();
    });
    wrap.appendChild(btn);
  }
}

/* ---------- 路由 ---------- */

function currentRoute() {
  const route = location.hash.replace(/^#\//, '');
  return ['overview', 'orders', 'ranking', 'users', 'reports', 'settings'].includes(route) ? route : 'overview';
}

function renderCurrent() {
  pageVersion++;
  const view = currentRoute();
  $('range').classList.toggle('hidden', ['settings', 'reports'].includes(view));
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  const target = $('view-' + view);
  if (!target) return;
  target.classList.add('active');
  document.querySelectorAll('.nav a').forEach((a) => a.classList.toggle('active', a.dataset.view === view));
  if (view === 'overview') renderOverview();
  else if (view === 'orders') renderOrders();
  else if (view === 'ranking') renderRanking();
  else if (view === 'users') renderUsers();
  else if (view === 'reports') renderReports();
  else if (view === 'settings') renderSettings();
}

/* ---------- SVG 图表 ---------- */

function svgEl(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function niceCeil(v) {
  if (!v) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const norm = v / mag;
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10;
  return step * mag;
}

function areaChart(points) {
  const data = (points || []).map((p) => ({ date: (p.date || '').slice(5), money: Number(p.money) || 0 }));
  if (!data.length) return null;
  const W = 640, H = 240, L = 52, R = 14, T = 18, B = 30;
  const iw = W - L - R, ih = H - T - B;
  const yTop = niceCeil(Math.max(0, ...data.map((d) => d.money)));
  const n = data.length;
  const x = (i) => (n === 1 ? L + iw / 2 : L + (i / (n - 1)) * iw);
  const y = (v) => T + ih - (yTop === 0 ? 0 : (v / yTop) * ih);

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img' });
  const areaColor = cssVar('--primary');
  const textColor = cssVar('--muted');
  const gridColor = cssVar('--line');
  const gradId = 'area-' + Math.random().toString(36).slice(2, 8);

  const defs = svgEl('defs', {});
  const grad = svgEl('linearGradient', { id: gradId, x1: "0", y1: "0", x2: "0", y2: "1" });
  grad.appendChild(svgEl('stop', { offset: "0%", 'stop-color': areaColor, 'stop-opacity': 0.35 }));
  grad.appendChild(svgEl('stop', { offset: "100%", 'stop-color': areaColor, 'stop-opacity': 0.02 }));
  defs.appendChild(grad);
  svg.appendChild(defs);

  for (let i = 0; i <= 4; i++) {
    const gv = (yTop / 4) * i;
    const gy = y(gv);
    svg.appendChild(svgEl('line', { x1: L, y1: gy, x2: W - R, y2: gy, stroke: gridColor, 'stroke-width': 1 }));
    const label = svgEl('text', { x: L - 6, y: gy + 4, 'text-anchor': 'end', 'font-size': 11, fill: textColor });
    label.textContent = fmtCompact(gv);
    svg.appendChild(label);
  }

  const linePath = data.map((d, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(d.money).toFixed(1)}`).join(' ');
  const areaPath = `${linePath} L${x(n - 1).toFixed(1)},${y(0).toFixed(1)} L${x(0).toFixed(1)},${y(0).toFixed(1)} Z`;
  svg.appendChild(svgEl('path', { d: areaPath, fill: `url(#${gradId})` }));
  svg.appendChild(svgEl('path', { d: linePath, fill: 'none', stroke: areaColor, 'stroke-width': 2.5, 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));

  data.forEach((d, i) => {
    svg.appendChild(svgEl('circle', { cx: x(i), cy: y(d.money), r: 3, fill: cssVar('--bg'), stroke: areaColor, 'stroke-width': 2 }));
    const step = Math.max(1, Math.ceil((n - 1) / 6));
    if (i !== n - 1 && (i % step !== 0 || (i > 0 && n - 1 - i < step))) return;
    const label = svgEl('text', { x: x(i), y: H - 8, 'text-anchor': 'middle', 'font-size': 11, fill: textColor });
    label.textContent = d.date;
    svg.appendChild(label);
  });
  return svg;
}

function donut(items) {
  const total = items.reduce((s, i) => s + (Number(i.money) || 0), 0);
  if (total <= 0) return null;
  const size = 176, c = size / 2, r = 60, sw = 22;
  const svg = svgEl('svg', { viewBox: `0 0 ${size} ${size}`, width: size, height: size, role: 'img' });
  const palette = [1, 2, 3, 4, 5, 6].map((i) => cssVar(`--chart${i}`));
  let acc = 0;
  items.forEach((item, i) => {
    const frac = (Number(item.money) || 0) / total;
    svg.appendChild(svgEl('circle', {
      cx: c, cy: c, r, fill: 'none', stroke: palette[i % palette.length], 'stroke-width': sw,
      pathLength: 100,
      'stroke-dasharray': `${(frac * 100).toFixed(2)} ${((1 - frac) * 100).toFixed(2)}`,
      'stroke-dashoffset': (-acc * 100).toFixed(2),
      transform: `rotate(-90 ${c} ${c})`,
    }));
    acc += frac;
  });
  const center = svgEl('text', { x: c, y: c - 2, 'text-anchor': 'middle', 'font-size': 20, 'font-weight': 700, fill: cssVar('--text') });
  center.textContent = fmtCompact(total);
  svg.appendChild(center);
  const sub = svgEl('text', { x: c, y: c + 17, 'text-anchor': 'middle', 'font-size': 11, fill: cssVar('--muted') });
  sub.textContent = '总金额';
  svg.appendChild(sub);
  return svg;
}

function legend(items) {
  const wrap = el('div', 'legend');
  const palette = [1, 2, 3, 4, 5, 6].map((i) => cssVar(`--chart${i}`));
  items.forEach((item, i) => {
    const box = el('div', 'item');
    const dot = el('span', 'dot');
    dot.style.background = palette[i % palette.length];
    box.appendChild(dot);
    box.appendChild(el('span', '', `${item.label} · ${fmtYuan(item.money)}`));
    wrap.appendChild(box);
  });
  return wrap;
}

function kpi(label, value, sub, accent) {
  const node = el('div', 'kpi glass');
  node.appendChild(el('div', 'label', label));
  node.appendChild(el('div', 'value', value));
  node.appendChild(el('div', 'sub', sub));
  if (accent) node.classList.add('accent');
  return node;
}

/* ---------- 概览 ---------- */

async function renderOverview() {
  const version = pageVersion;
  const root = $('view-overview');
  empty(root, '加载中…');
  const [sRes, tRes] = await Promise.all([
    api(`/api/finance/summary?days=${currentRange}`),
    api(`/api/finance/trend?days=${currentRange}`),
  ]);
  if (version !== pageVersion || !root.isConnected) return;
  if (sRes.status === 401 || tRes.status === 401) return showLogin();
  root.textContent = '';
  if (!sRes.ok) return empty(root, sRes.data.error || '加载失败');

  const sum = sRes.data;
  const trend = tRes.ok ? tRes.data : { points: [] };

  const kpis = el('div', 'kpi-grid');
  kpis.appendChild(kpi('区间总收入', fmtYuan(sum.total.money), `近 ${currentRange} 天`, true));
  kpis.appendChild(kpi('订单笔数', String(sum.total.orders), '成功订单'));
  kpis.appendChild(kpi('充值人数', String(sum.total.users), '去重用户'));
  kpis.appendChild(kpi('今日收入', fmtYuan(sum.today.money), `今日 ${sum.today.orders} 笔`));
  root.appendChild(kpis);

  const row = el('div', 'grid-2');
  const trendCard = el('div', 'card glass');
  trendCard.appendChild(el('h3', '', '收入趋势'));
  const lineChart = areaChart(trend.points);
  const chartWrap = el('div', 'chart');
  if (lineChart) chartWrap.appendChild(lineChart);
  else chartWrap.appendChild(el('div', 'empty', '暂无成功订单'));
  trendCard.appendChild(chartWrap);
  row.appendChild(trendCard);

  const methodCard = el('div', 'card glass');
  methodCard.appendChild(el('h3', '', '支付方式'));
  const donutBox = el('div', 'donut-wrap');
  const ring = donut(sum.methods);
  if (ring) { donutBox.appendChild(ring); donutBox.appendChild(legend(sum.methods)); }
  else donutBox.appendChild(el('div', 'empty', '暂无成功订单'));
  methodCard.appendChild(donutBox);
  row.appendChild(methodCard);
  root.appendChild(row);

  const bdCard = el('div', 'card glass');
  bdCard.appendChild(el('h3', '', '订单构成'));
  bdCard.appendChild(breakdownBars(sum.breakdown || []));
  root.appendChild(bdCard);
}

function breakdownBars(items) {
  const max = Math.max(1, ...items.map((i) => Number(i.money) || 0));
  const list = el('div', 'rank-list');
  for (const item of items) {
    const rowEl = el('div', 'rank-item glass');
    rowEl.appendChild(el('div', 'who', item.label));
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = `${(((Number(item.money) || 0) / max) * 100).toFixed(1)}%`;
    bar.appendChild(fill);
    rowEl.appendChild(bar);
    rowEl.appendChild(el('div', 'money', `${fmtYuan(item.money)} · ${item.orders} 笔`));
    list.appendChild(rowEl);
  }
  return (items.length ? list : el('div', 'empty', '暂无数据'));
}

/* ---------- 订单流水 ---------- */

async function renderOrders() {
  const root = $('view-orders');
  root.textContent = '';

  const bar = el('div', 'card glass');
  const top = el('div', 'actions');
  top.style.marginTop = '0';
  const seg = el('div', 'seg', '');
  for (const [key, label] of [['success', '成功'], ['failed', '失败']]) {
    const btn = el('button', orderFilters.has(key) ? 'on' : '', label);
    btn.setAttribute('aria-pressed', String(orderFilters.has(key)));
    btn.addEventListener('click', () => {
      if (orderFilters.has(key) && orderFilters.size === 1) return;
      if (orderFilters.has(key)) orderFilters.delete(key); else orderFilters.add(key);
      btn.classList.toggle('on', orderFilters.has(key));
      btn.setAttribute('aria-pressed', String(orderFilters.has(key)));
      renderOrdersBody();
    });
    seg.appendChild(btn);
  }
  const exportBtn = el('a', 'btn small', '导出 CSV');
  exportBtn.id = 'orders-export';
  exportBtn.setAttribute('download', 'orders.csv');
  top.appendChild(seg);
  top.appendChild(exportBtn);
  top.appendChild(el('span', 'muted', '仅显示成功与失败，超时计入失败'));
  bar.appendChild(top);
  root.appendChild(bar);

  const tableCard = el('div', 'card glass');
  tableCard.appendChild(el('h3', '', '订单流水'));
  const wrap = el('div', 'table-wrap');
  wrap.tabIndex = 0;
  wrap.setAttribute('aria-label', '订单流水，可横向滚动');
  const table = el('table', '');
  const thead = el('thead', '');
  const headRow = el('tr', '');
  for (const text of ['时间', '类型', '状态', '订单号', '用户', '金额', '支付方式']) {
    headRow.appendChild(el('th', '', text));
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  table.appendChild(el('tbody', '', ''));
  wrap.appendChild(table);
  tableCard.appendChild(wrap);
  const notice = el('p', 'muted');
  notice.id = 'orders-notice';
  notice.setAttribute('role', 'status');
  tableCard.appendChild(notice);
  root.appendChild(tableCard);

  await renderOrdersBody();
}

async function renderOrdersBody() {
  const tbody = $('view-orders').querySelector('tbody');
  if (!tbody) return;
  const request = ++orderRequest;
  const version = pageVersion;
  const filters = new Set(orderFilters);
  const query = new URLSearchParams({ days: String(currentRange), status: [...filters].join(',') });
  $('orders-export').href = `/api/orders/export.csv?${query}`;
  setMessage('orders-notice', '加载中…');
  const res = await api(`/api/orders?${query}`);
  if (request !== orderRequest || version !== pageVersion || !tbody.isConnected) return;
  if (res.status === 401) return showLogin();
  setMessage('orders-notice', res.ok && res.data.has_more ? '结果已截断，仅显示部分订单；请缩短时间范围。导出也可能受条数上限限制。' : '', res.data.has_more ? 'bad' : '');
  tbody.textContent = '';
  if (!res.ok) {
    const tr = el('tr', '');
    const td = el('td', 'muted', res.data.error || '加载失败');
    td.colSpan = 7;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  const items = (res.data.items || []).filter((item) => filters.has(item.bucket));
  if (!items.length) {
    const tr = el('tr', '');
    const td = el('td', 'muted', '暂无订单');
    td.colSpan = 7;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  for (const item of items) {
    const tr = el('tr', '');
    tr.appendChild(el('td', '', formatTime(item.create_time)));
    tr.appendChild(el('td', 'muted', item.kind === 'wallet' ? '钱包充值' : '订阅开通'));
    let text = BUCKET_TEXT[item.bucket] || item.bucket;
    if (item.bucket === 'failed' && item.status === 'expired') {
      text = '失败 · 超时' + (item.derived ? '（推导）' : '');
    }
    const badge = el('span', `badge ${item.bucket}`, text);
    const bdCell = el('td', '');
    bdCell.appendChild(badge);
    tr.appendChild(bdCell);
    tr.appendChild(el('td', '', item.trade_no));
    tr.appendChild(el('td', '', item.username || `用户#${item.user_id}`));
    tr.appendChild(el('td', 'num', fmtYuan(item.money)));
    tr.appendChild(el('td', '', item.method));
    tbody.appendChild(tr);
  }
}

/* ---------- 排行榜 ---------- */

async function renderRanking() {
  const version = pageVersion;
  const root = $('view-ranking');
  empty(root, '加载中…');
  const res = await api(`/api/finance/ranking?days=${currentRange}&limit=10`);
  if (version !== pageVersion || !root.isConnected) return;
  if (res.status === 401) return showLogin();
  root.textContent = '';
  if (!res.ok) return empty(root, res.data.error || '加载失败');

  const items = res.data.items || [];
  const card = el('div', 'card glass');
  card.appendChild(el('h3', '', `充值排行榜（近 ${currentRange} 天，前 ${items.length} 名）`));
  if (!items.length) {
    card.appendChild(el('div', 'empty', '暂无成功订单'));
    root.appendChild(card);
    return;
  }
  const max = Math.max(1, ...items.map((i) => Number(i.money) || 0));
  const list = el('div', 'rank-list');
  items.forEach((item, index) => {
    const rowEl = el('div', 'rank-item glass');
    const no = el('div', 'no', String(index + 1));
    if (index < 3) no.classList.add('top');
    rowEl.appendChild(no);
    rowEl.appendChild(el('div', 'who', `${item.name}（ID ${item.user_id}）`));
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = `${(((Number(item.money) || 0) / max) * 100).toFixed(1)}%`;
    bar.appendChild(fill);
    rowEl.appendChild(bar);
    rowEl.appendChild(el('div', 'money', `${fmtYuan(item.money)} · ${item.orders} 笔`));
    list.appendChild(rowEl);
  });
  card.appendChild(list);
  root.appendChild(card);
}

/* ---------- 用户画像 ---------- */

async function renderUsers() {
  const version = pageVersion;
  const days = currentRange;
  const root = $('view-users');
  empty(root, '加载中…');
  const res = await api(`/api/finance/users?days=${days}`);
  if (version !== pageVersion || !root.isConnected) return;
  if (res.status === 401) return showLogin();
  if (!res.ok) return empty(root, res.data.error || '加载用户画像失败');
  root.textContent = '';
  const items = res.data.items || [];
  const total = items.reduce((sum, item) => sum + Number(item.total || 0), 0);
  const orders = items.reduce((sum, item) => sum + Number(item.orders || 0), 0);
  const kpis = el('div', 'kpi-grid');
  kpis.appendChild(kpi('周期充值用户', String(items.length), '仅包含有成功订单的用户'));
  kpis.appendChild(kpi('周期充值总额', fmtYuan(total), `近 ${days} 天`, true));
  kpis.appendChild(kpi('周期成功订单', String(orders), '钱包充值 + 订阅开通'));
  kpis.appendChild(kpi('周期平均客单价', fmtYuan(orders ? total / orders : 0), '成功金额 / 成功订单笔数'));
  root.appendChild(kpis);
  const card = el('div', 'card glass');
  card.appendChild(el('h3', '', '用户画像'));
  card.appendChild(el('p', 'muted', '金额、笔数及首次 / 最近时间均来自所选周期内成功订单的创建时间，不代表用户全生命周期；钱包 / 订阅为充值金额，不是余额。'));
  const field = el('div', 'field user-search');
  const label = el('label', '', '搜索用户名或用户 ID');
  label.htmlFor = 'users-search';
  const search = el('input');
  search.type = 'search';
  search.id = 'users-search';
  search.placeholder = '输入用户名或 ID';
  search.value = userSearch;
  field.appendChild(label);
  field.appendChild(search);
  card.appendChild(field);
  const count = el('p', 'muted');
  count.setAttribute('role', 'status');
  card.appendChild(count);
  const wrap = el('div', 'table-wrap');
  wrap.tabIndex = 0;
  wrap.setAttribute('aria-label', '用户画像统计表，可横向滚动');
  const table = el('table');
  const head = el('thead');
  const row = el('tr');
  for (const text of ['用户 ID', '用户名', '周期总额', '成功笔数', '平均客单价', '钱包充值', '订阅开通', '周期内首次创建', '周期内最近创建']) {
    row.appendChild(el('th', '', text));
  }
  head.appendChild(row);
  table.appendChild(head);
  const body = el('tbody');
  table.appendChild(body);
  wrap.appendChild(table);
  card.appendChild(wrap);
  root.appendChild(card);
  search.addEventListener('input', () => {
    userSearch = search.value;
    renderUserRows(body, count, items, userSearch);
  });
  renderUserRows(body, count, items, userSearch);
}

function renderUserRows(body, count, items, query) {
  const term = query.trim().toLocaleLowerCase('zh-CN');
  const matches = items.filter((item) => String(item.user_id).includes(term)
    || String(item.username || '').toLocaleLowerCase('zh-CN').includes(term));
  count.textContent = `匹配 ${matches.length} / ${items.length} 位周期充值用户；上方指标为整个所选周期，不受搜索影响。`;
  body.textContent = '';
  if (!matches.length) {
    const row = el('tr');
    const td = el('td', 'muted', items.length ? '没有匹配的用户' : '所选周期暂无成功订单用户');
    td.colSpan = 9;
    row.appendChild(td);
    body.appendChild(row);
    return;
  }
  for (const item of matches) {
    const row = el('tr');
    row.appendChild(el('td', '', String(item.user_id)));
    row.appendChild(el('td', '', item.username || `用户#${item.user_id}`));
    row.appendChild(el('td', 'num', fmtYuan(item.total)));
    row.appendChild(el('td', 'num', String(item.orders)));
    for (const key of ['average', 'wallet', 'subscription']) row.appendChild(el('td', 'num', fmtYuan(item[key])));
    row.appendChild(el('td', '', formatTime(item.first_time)));
    row.appendChild(el('td', '', formatTime(item.last_time)));
    body.appendChild(row);
  }
}

/* ---------- 报表与推送 ---------- */

async function renderReports() {
  if (!reportsBuilt) buildReports();
  await Promise.all([loadReports(), loadHistory()]);
}

function buildReports() {
  const root = $('view-reports');
  root.textContent = '';
  root.appendChild(el('p', 'muted', '自动报表使用已保存并生效的开关；手动发送不受开关与去重限制，也不会更新最近定时成功周期。时间以服务端时区为准，与顶部订单统计周期无关。'));
  const link = el('a', 'btn small', '前往设置修改定时开关与钉钉参数');
  link.href = '#/settings';
  root.appendChild(link);
  const grid = el('div', 'grid-2 report-grid');
  for (const [kind, title, schedule, actionText] of [
    ['daily', '每日充值统计', '每天 00:00 推送前一天的成功充值与排行榜', '立即生成今日统计'],
    ['weekly', '每周充值统计', '每周一 00:00 推送上一个完整自然周的成功充值与排行榜', '立即生成本周统计'],
  ]) {
    const card = el('div', 'card glass');
    card.appendChild(el('h3', '', title));
    card.appendChild(el('p', 'muted', schedule));
    const state = el('div', 'report-state');
    state.id = `${kind}-state`;
    card.appendChild(state);
    card.appendChild(el('p', 'muted', '手动发送截至当前的统计；使用设置中尚未保存的钉钉参数（留空沿用已保存值）。'));
    const actions = el('div', 'actions');
    const button = el('button', 'btn', actionText);
    button.id = kind;
    actions.appendChild(button);
    const message = el('span', 'muted');
    message.id = `${kind}-msg`;
    message.setAttribute('role', 'status');
    actions.appendChild(message);
    card.appendChild(actions);
    grid.appendChild(card);
  }
  root.appendChild(grid);
  const notice = el('p', 'muted');
  notice.id = 'reports-msg';
  notice.setAttribute('role', 'status');
  root.appendChild(notice);
  const history = el('div', 'card glass');
  const histHead = el('div', 'actions');
  histHead.appendChild(el('h3', '', '最近推送记录（订单 / 日报 / 周报）'));
  for (const [id, text] of [['refresh', '刷新'], ['clear', '清空记录']]) {
    const button = el('button', 'btn small ghost', text);
    button.id = id;
    histHead.appendChild(button);
  }
  history.appendChild(histHead);
  const tableWrap = el('div', 'table-wrap');
  tableWrap.tabIndex = 0;
  tableWrap.setAttribute('aria-label', '推送历史，可横向滚动');
  const table = el('table');
  const thead = el('thead');
  const headRow = el('tr');
  for (const text of ['时间', '状态', '订单号 / 报表周期', '用户', '金额', '原因 / 统计摘要', '来源', '推送']) {
    headRow.appendChild(el('th', '', text));
  }
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = el('tbody');
  tbody.id = 'history';
  table.appendChild(tbody);
  tableWrap.appendChild(table);
  history.appendChild(tableWrap);
  const historyMessage = el('p', 'muted');
  historyMessage.id = 'history-msg';
  historyMessage.setAttribute('role', 'status');
  history.appendChild(historyMessage);
  root.appendChild(history);
  for (const kind of ['daily', 'weekly']) {
    bindBusyAction(kind, `${kind}-msg`, async () => {
      const version = pageVersion;
      setMessage(`${kind}-msg`, '统计并发送中…');
      const { ok, status, data } = await api(`/api/${kind}-report`, { method: 'POST', body: { config: dingtalkForm() } });
      if (version !== pageVersion) return;
      if (status === 401) return showLogin();
      setMessage(`${kind}-msg`, ok ? (data.detail || '已推送') : (data.error || data.detail || '推送失败'), ok ? 'ok' : 'bad');
      await Promise.all([loadHistory(), loadReports()]);
    });
  }
  bindBusyAction('refresh', 'reports-msg', async () => {
    await Promise.all([loadReports(), loadHistory()]);
  });
  bindBusyAction('clear', 'history-msg', async () => {
    if (!confirm('确定清空所有推送记录？')) return;
    const version = pageVersion;
    const { ok, status, data } = await api('/api/history/clear', { method: 'POST' });
    if (version !== pageVersion) return;
    if (status === 401) return showLogin();
    if (!ok) return setMessage('history-msg', data.error || data.detail || '清空记录失败', 'bad');
    await loadHistory();
  });
  reportsBuilt = true;
}

async function loadReports() {
  const request = ++reportsRequest;
  const version = pageVersion;
  setMessage('reports-msg', '读取报表状态中…');
  for (const kind of ['daily', 'weekly']) empty($(`${kind}-state`), '读取定时状态中…');
  const { ok, status, data } = await api('/api/reports');
  if (request !== reportsRequest || version !== pageVersion) return;
  if (status === 401) return showLogin();
  if (!ok) {
    for (const kind of ['daily', 'weekly']) empty($(`${kind}-state`), '定时状态未知');
    return setMessage('reports-msg', data.error || '读取报表状态失败，请刷新重试', 'bad');
  }
  setMessage('reports-msg', '最近定时成功日期显示报表所覆盖的日期 / 周期，并非手动发送时间。');
  for (const kind of ['daily', 'weekly']) {
    const state = $(`${kind}-state`);
    if (!state) continue;
    state.textContent = '';
    state.appendChild(el('span', `badge ${data[`${kind}_report`] ? 'success' : 'pending'}`, data[`${kind}_report`] ? '定时已开启' : '定时已关闭'));
    state.appendChild(el('span', 'muted', `最近定时成功周期：${data[`${kind}_last`] || '暂无定时成功记录'}`));
  }
}

/* ---------- 设置 ---------- */

function buildField(spec) {
  const wrap = el('div', 'field');
  const label = el('label', '', spec.label);
  label.htmlFor = `f-${spec.key}`;
  wrap.appendChild(label);

  let input;
  if (spec.type === 'select') {
    input = el('select', '');
    for (const [value, text] of spec.options) {
      const option = el('option', '', text);
      option.value = value;
      input.appendChild(option);
    }
  } else if (BOOL_KEYS.has(spec.key)) {
    input = el('input', '');
    input.type = 'checkbox';
  } else {
    input = el('input', '');
    input.type = INT_KEYS.has(spec.key) || DECIMAL_KEYS.has(spec.key) ? 'number' : 'text';
    if (DECIMAL_KEYS.has(spec.key)) {
      input.min = '0';
      input.step = 'any';
      input.value = String(spec.default);
    }
    input.autocomplete = 'off';
  }
  input.id = `f-${spec.key}`;
  inputs[spec.key] = input;
  input.addEventListener('input', () => {
    configRevision++;
    input.dataset.dirty = 'true';
    input.dataset.revision = String(configRevision);
  });

  if (BOOL_KEYS.has(spec.key)) {
    const rowEl = el('div', 'switch');
    rowEl.appendChild(input);
    const caption = el('span', 'muted', spec.hint || '');
    rowEl.appendChild(caption);
    wrap.appendChild(rowEl);
  } else {
    wrap.appendChild(input);
    wrap.appendChild(el('span', 'hint', spec.hint || ''));
  }
  return wrap;
}

function buildSettings() {
  const root = $('view-settings');
  root.textContent = '';

  const grid = el('div', 'grid-2');

  const dingtalk = el('div', 'card glass');
  dingtalk.appendChild(el('h3', '', '钉钉推送'));
  const dgGrid = el('div', 'grid');
  dgGrid.id = 'group-dingtalk';
  for (const spec of FIELDS['group-dingtalk']) dgGrid.appendChild(buildField(spec));
  dingtalk.appendChild(dgGrid);
  const dgActions = el('div', 'actions');
  const testBtn = el('button', 'btn', '测试发送');
  testBtn.id = 'test';
  dgActions.appendChild(testBtn);
  dgActions.appendChild(el('span', 'muted', ''));
  dgActions.lastChild.id = 'test-msg';
  dingtalk.appendChild(dgActions);
  grid.appendChild(dingtalk);

  const source = el('div', 'card glass');
  source.appendChild(el('h3', '', 'new-api 数据源（只读）'));
  const srcGrid = el('div', 'grid');
  srcGrid.id = 'group-source';
  for (const spec of FIELDS['group-source']) srcGrid.appendChild(buildField(spec));
  source.appendChild(srcGrid);
  const srcActions = el('div', 'actions');
  const checkBtn = el('button', 'btn', '测试连接');
  checkBtn.id = 'check';
  srcActions.appendChild(checkBtn);
  srcActions.appendChild(el('span', 'muted', ''));
  srcActions.lastChild.id = 'check-msg';
  source.appendChild(srcActions);
  grid.appendChild(source);
  root.appendChild(grid);

  const push = el('div', 'card glass');
  push.appendChild(el('h3', '', '推送控制'));
  const pushGrid = el('div', 'grid');
  pushGrid.id = 'group-push';
  for (const spec of FIELDS['group-push']) pushGrid.appendChild(buildField(spec));
  push.appendChild(pushGrid);
  root.appendChild(push);

  const backfill = el('div', 'card glass');
  backfill.appendChild(el('h3', '', '推送补发'));
  backfill.appendChild(el('div', 'muted', '把最近 N 小时的订单再推一遍（不受「已推送」去重限制），消息标题会标注「补发推送」。'));
  const bfActions = el('div', 'actions');
  bfActions.appendChild(el('label', 'muted', '回溯小时数'));
  const bfHours = el('input', 'num', '');
  bfHours.type = 'number'; bfHours.value = '24'; bfHours.min = '1'; bfHours.max = '720'; bfHours.id = 'bf-hours';
  bfActions.appendChild(bfHours);
  const bfBtn = el('button', 'btn', '补发推送');
  bfBtn.id = 'backfill';
  bfActions.appendChild(bfBtn);
  bfActions.appendChild(el('span', 'muted', ''));
  bfActions.lastChild.id = 'bf-msg';
  backfill.appendChild(bfActions);
  root.appendChild(backfill);

  const reportsLink = el('a', 'btn', '前往报表与推送：日 / 周报及推送历史');
  reportsLink.href = '#/reports';
  root.appendChild(reportsLink);

  const status = el('div', 'card glass');
  status.appendChild(el('h3', '', '运行状态'));
  const statusbar = el('div', 'statusline');
  statusbar.id = 'statusbar';
  status.appendChild(statusbar);
  root.appendChild(status);

  const saveBar = el('div', 'actions');
  const saveBtn = el('button', 'btn primary', '保存配置');
  saveBtn.id = 'save';
  saveBar.appendChild(saveBtn);
  saveBar.appendChild(el('span', 'muted', ''));
  saveBar.lastChild.id = 'save-msg';
  root.appendChild(saveBar);

  bindSettingsActions();
  settingsBuilt = true;
}

function fill(config, envOverridden, secrets, preserveDirty = false) {
  const overridden = new Set(envOverridden || []);
  const hasSecret = secrets || {};
  for (const specs of Object.values(FIELDS)) {
    for (const spec of specs) {
      const input = inputs[spec.key];
      if (!input || (preserveDirty && input.dataset.dirty === 'true')) continue;
      delete input.dataset.dirty;
      const hint = input.closest('.field').querySelector('.hint, .switch .muted');
      let text = spec.hint || '';
      if (SECRET_KEYS.has(spec.key)) {
        const savedFlag = Boolean(hasSecret[spec.key]);
        input.value = '';
        input.placeholder = savedFlag ? MASK : '';
        if (savedFlag) text += '（已保存，留空表示不修改）';
      } else if (BOOL_KEYS.has(spec.key)) {
        input.checked = Boolean(config[spec.key]);
      } else {
        const value = config[spec.key] ?? spec.default ?? '';
        input.value = String(value);
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
      if (!input) continue;
      if (SECRET_KEYS.has(spec.key)) {
        const raw = input.value.trim();
        if (raw === '' || raw === MASK) continue;
        values[spec.key] = raw;
        continue;
      }
      values[spec.key] = BOOL_KEYS.has(spec.key) ? input.checked : input.value.trim();
    }
  }
  return values;
}

function dingtalkForm() {
  const values = {};
  for (const spec of FIELDS['group-dingtalk']) {
    const input = inputs[spec.key];
    if (!input || !input.isConnected || (!configLoaded && input.dataset.dirty !== 'true')) continue;
    const value = input.value.trim();
    if (SECRET_KEYS.has(spec.key) && (!value || value === MASK)) continue;
    values[spec.key] = value;
  }
  return values;
}

async function renderSettings() {
  if (!settingsBuilt) buildSettings();
  await Promise.all([configLoaded ? Promise.resolve() : loadConfig(), loadStatus()]);
}

function badgeCell(row, text, className) {
  const td = el('td', '');
  const span = el('span', `badge ${className}`, text);
  td.appendChild(span);
  row.appendChild(td);
  return td;
}

function renderStatus(status) {
  const version = $('version');
  if (version) version.textContent = status.version ? `v${status.version}` : '';
  const bar = $('statusbar');
  if (!bar) return;
  bar.textContent = '';
  const parts = [
    ['运行轮次', status.runs],
    ['上次运行', status.last_run ? formatTime(status.last_run) : '-'],
    ['本轮订单', status.last_orders],
    ['累计推送', status.pushed_total],
  ];
  for (const [name, value] of parts) {
    const pill = el('span', 'pill', `${name}: ${value}`);
    bar.appendChild(pill);
  }
  const warnings = status.warnings || [];
  if (warnings.length) setMessage('save-msg', warnings.join('；'), 'bad');
  else if (status.last_error) setMessage('save-msg', status.last_error, 'bad');
}

function renderHistory(items) {
  const body = $('history');
  if (!body) return;
  body.textContent = '';
  if (!items.length) {
    const row = el('tr', '');
    const td = el('td', 'muted', '暂无推送记录');
    td.colSpan = 8;
    row.appendChild(td);
    body.appendChild(row);
    return;
  }
  for (const item of items) {
    const row = el('tr', '');
    if (item.detail) row.title = item.detail;
    row.appendChild(el('td', '', formatTime(item.pushed_at)));
    badgeCell(row, STATUS_TEXT[item.status] || item.status, item.status);
    row.appendChild(el('td', '', item.trade_no));
    row.appendChild(el('td', '', item.username));
    row.appendChild(el('td', 'num', fmtYuan(item.money)));
    row.appendChild(el('td', '', item.reason || '-'));
    badgeCell(row, SOURCE_TEXT[item.reason_source] || item.reason_source || '-', 'source');
    const pushText = (item.ok ? '成功' : '失败') + (item.backfill ? ' · 补发推送' : '');
    row.appendChild(el('td', item.ok ? 'ok' : 'bad', pushText));
    body.appendChild(row);
  }
}

/* ---------- 配置 / 状态 / 记录加载 ---------- */

async function loadConfig(preserveDirty = true) {
  const request = ++configRequest;
  const version = pageVersion;
  const revision = configRevision;
  const { ok, status, data } = await api('/api/config');
  if (request !== configRequest || version !== pageVersion) return;
  if (status === 401) return showLogin();
  if (!ok) return setMessage('save-msg', `读取配置失败：${data.error || status}`, 'bad');
  fill(data.config || {}, data.env_overridden || [], data.secrets || {}, preserveDirty || revision !== configRevision);
  configLoaded = true;
}

async function loadStatus() {
  const request = ++statusRequest;
  const version = pageVersion;
  const { status, data } = await api('/api/status');
  if (request !== statusRequest || version !== pageVersion) return;
  if (status === 401) return showLogin();
  if (status === 200) renderStatus(data);
}

async function loadHistory() {
  const body = $('history');
  if (!body) return;
  const request = ++historyRequest;
  const version = pageVersion;
  setMessage('history-msg', '加载推送记录中…');
  const { ok, status, data } = await api('/api/history');
  if (request !== historyRequest || version !== pageVersion || !body.isConnected) return;
  if (status === 401) return showLogin();
  if (!ok) return setMessage('history-msg', data.error || '读取推送记录失败，请刷新重试', 'bad');
  setMessage('history-msg', '');
  renderHistory(data.items || []);
}

/* ---------- 设置动作 ---------- */

function bindBusyAction(id, messageId, action) {
  const button = $(id);
  button.addEventListener('click', async () => {
    if (button.disabled) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    const version = pageVersion;
    try {
      await action();
    } catch {
      if (version === pageVersion && button.isConnected) setMessage(messageId, '操作失败，请重试；发送结果可能未知，请先核对钉钉消息', 'bad');
    } finally {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  });
}

function bindSettingsActions() {
  bindBusyAction('save', 'save-msg', async () => {
    if (!configLoaded) return setMessage('save-msg', '配置尚未读取成功，请重新进入设置后再保存', 'bad');
    for (const key of DECIMAL_KEYS) {
      const input = inputs[key];
      const value = Number(input.value);
      if (!input.value.trim() || !Number.isFinite(value) || value < 0 || !input.checkValidity()) {
        input.reportValidity();
        return setMessage('save-msg', '大额阈值与最低推送金额必须为非负小数', 'bad');
      }
    }
    const version = pageVersion;
    const revisions = new Map(Object.values(inputs).map((input) => [input, input.dataset.revision]));
    configRequest++;
    const { ok, status, data } = await api('/api/config', { method: 'POST', body: { config: collect() } });
    if (ok) {
      configLoaded = false;
      for (const [key, input] of Object.entries(inputs)) {
        if (input.dataset.revision !== revisions.get(input)) continue;
        delete input.dataset.dirty;
        if (SECRET_KEYS.has(key) && input.value.trim() && input.value.trim() !== MASK) {
          input.value = '';
          input.placeholder = MASK;
        }
      }
    }
    if (version !== pageVersion) return;
    if (status === 401) return showLogin();
    if (!ok) return setMessage('save-msg', `保存失败：${data.error || '未知错误'}`, 'bad');
    const warnings = data.warnings || [];
    setMessage('save-msg',
      warnings.length ? `已保存，但仍有问题：${warnings.join('；')}` : '已保存并生效',
      warnings.length ? 'bad' : 'ok');
    await loadConfig();
  });

  bindBusyAction('test', 'test-msg', async () => {
    const version = pageVersion;
    setMessage('test-msg', '发送中…');
    const { ok, status, data } = await api('/api/test', { method: 'POST', body: { config: dingtalkForm() } });
    if (version !== pageVersion) return;
    if (status === 401) return showLogin();
    setMessage('test-msg', ok ? (data.detail || '发送成功') : (data.error || data.detail || '发送失败'), ok ? 'ok' : 'bad');
  });

  bindBusyAction('check', 'check-msg', async () => {
    const version = pageVersion;
    setMessage('check-msg', '连接中…');
    const { ok, status, data } = await api('/api/check', { method: 'POST' });
    if (version !== pageVersion) return;
    if (status === 401) return showLogin();
    setMessage('check-msg', ok ? (data.detail || '连接正常') : (data.error || data.detail || '连接失败'), ok ? 'ok' : 'bad');
  });

  bindBusyAction('backfill', 'bf-msg', async () => {
    const hours = Number($('bf-hours').value);
    if (!Number.isInteger(hours) || hours < 1 || hours > 720) {
      return setMessage('bf-msg', '回溯小时数需要在 1 ~ 720 之间', 'bad');
    }
    if (!confirm(`确定补发最近 ${hours} 小时的订单？消息会标注「补发推送」。`)) return;
    const version = pageVersion;
    setMessage('bf-msg', '补发中…（订单较多时需要等一会儿）');
    const { ok, status, data } = await api('/api/backfill', { method: 'POST', body: { hours } });
    if (version !== pageVersion) return;
    if (status === 401) return showLogin();
    if (!ok) return setMessage('bf-msg', data.error || data.detail || '补发失败', 'bad');
    setMessage('bf-msg', `已补发 ${data.count} 条（最近 ${data.hours} 小时）`, 'ok');
    await Promise.all([loadHistory(), loadStatus()]);
  });

}

/* ---------- 启动 ---------- */

function bind() {
  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    setMessage('login-error', '');
    const { ok, data } = await api('/api/login', { method: 'POST', body: { password: $('password').value } });
    if (!ok) return setMessage('login-error', data.error || '登录失败');
    $('password').value = '';
    await boot();
  });

  $('logout').addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST' });
    showLogin();
  });

  $('theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    applyTheme(next);
  });

  window.addEventListener('hashchange', renderCurrent);
}

async function boot() {
  const { data } = await api('/api/session');
  if (!data.authed) return showLogin();
  buildRange();
  showApp();
  await loadStatus();
  renderCurrent();
}

initTheme();
bind();
boot();