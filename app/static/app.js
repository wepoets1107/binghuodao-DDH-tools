let config = null;
let selectedCurrency = "BTC";
let lastStatus = null;
let credentialState = {};
let lastOpenOrders = [];
const STATUS_REFRESH_MS = 10000;
const ORDERS_REFRESH_MS = 5000;
const FAST_ORDER_REFRESH_MS = 2000;
const FAST_ORDER_REFRESH_ROUNDS = 5;

const $ = (selector) => document.querySelector(selector);
const message = $("#message");

function showMessage(text, isError = false) {
  message.textContent = text || "";
  message.style.color = isError ? "#b83b44" : "#66736f";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { detail: text };
    }
  }
  if (!response.ok) {
    throw new Error(payload?.detail || payload?.message || `请求失败：${response.status}`);
  }
  return payload || {};
}

function assetKeys() {
  return Object.keys(config?.assets || {});
}

function setValue(form, name, value) {
  const node = form.elements[name];
  if (!node) return;
  if (node.type === "checkbox") {
    node.checked = Boolean(value);
  } else {
    node.value = value ?? "";
  }
}

function readNumber(form, name) {
  const value = Number(form.elements[name].value);
  return Number.isFinite(value) ? value : 0;
}

function fillForms(payload) {
  config = payload.config;
  if (!config.assets[selectedCurrency]) {
    selectedCurrency = assetKeys()[0] || "BTC";
  }

  const global = $("#globalForm");
  setValue(global, "mode", config.mode);
  setValue(global, "loop_interval_seconds", config.loop_interval_seconds);
  setValue(global, "order_type", config.order_type);
  setValue(global, "slippage_bps", config.slippage_bps);
  setValue(global, "cooldown_seconds", config.cooldown_seconds);
  setValue(global, "maker_wait_seconds", config.maker_wait_seconds ?? 60);

  renderTabs();
  renderAssetForm();

  credentialState = payload.credentials || {};
  updateModeBadge();
  updateCredentialsDisplay();
}

function renderTabs() {
  const host = $("#assetTabs");
  host.innerHTML = "";
  for (const currency of assetKeys()) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = currency === selectedCurrency ? "tab active" : "tab";
    button.textContent = currency;
    button.addEventListener("click", () => {
      persistCurrentAssetForm();
      selectedCurrency = currency;
      renderTabs();
      renderAssetForm();
      renderSelectedStatus();
    });
    host.appendChild(button);
  }
}

function renderAssetForm() {
  const host = $("#assetFormHost");
  const asset = config.assets[selectedCurrency];
  const currencyText = escapeHtml(selectedCurrency);
  host.innerHTML = `
    <form id="assetForm" class="asset-form" data-currency="${currencyText}">
      <div class="field-grid asset-grid">
        <label class="check"><input type="checkbox" name="enabled"><span>启用阈值对冲</span></label>
        <label><span>目标 Delta</span><input name="target_delta" type="number" step="0.0001"></label>
        <label><span>正向触发阈值</span><input name="positive_trigger_delta" type="number" min="0" step="0.0001"></label>
        <label><span>负向触发阈值</span><input name="negative_trigger_delta" type="number" min="0" step="0.0001"></label>
        <label>
          <span>对冲比例（1 = 100%）</span>
          <input name="hedge_ratio" type="number" min="0" step="0.01">
        </label>
        <label><span>对冲合约</span><input name="hedge_instrument"></label>
        <label>
          <span>最小下单币数量</span>
          <input name="min_order_amount" type="number" min="0" step="0.0001">
        </label>
        <label><span>最大下单币数量</span><input name="max_order_amount" type="number" min="0" step="0.0001"></label>
        <div class="form-subhead">定时对冲</div>
        <div class="schedule-action wide">
          <button id="assetImmediateBtn" type="button" class="primary">立即对冲 ${currencyText}</button>
        </div>
        <label class="check"><input type="checkbox" name="schedule_enabled"><span>启用 ${currencyText} 定时</span></label>
        <label>
          <span>模式</span>
          <select name="schedule_mode">
            <option value="hourly">每小时</option>
            <option value="daily">每天</option>
            <option value="custom">自定义</option>
          </select>
        </label>
        <label><span>时区</span><input name="schedule_timezone"></label>
        <label><span>每小时分钟</span><input name="schedule_minute" type="number" min="0" max="59" step="1"></label>
        <label class="wide"><span>时间点</span><input name="schedule_times" placeholder="08:00,16:00,23:55"></label>
      </div>
    </form>
  `;
  const form = $("#assetForm");
  setValue(form, "enabled", asset.enabled);
  setValue(form, "target_delta", asset.target_delta);
  setValue(form, "positive_trigger_delta", asset.positive_trigger_delta);
  setValue(form, "negative_trigger_delta", asset.negative_trigger_delta);
  setValue(form, "hedge_ratio", asset.hedge_ratio);
  setValue(form, "hedge_instrument", asset.hedge_instrument);
  setValue(form, "min_order_amount", asset.min_order_amount);
  setValue(form, "max_order_amount", asset.max_order_amount);
  const schedule = asset.schedule || defaultSchedule();
  setValue(form, "schedule_enabled", schedule.enabled);
  setValue(form, "schedule_mode", schedule.mode);
  setValue(form, "schedule_timezone", schedule.timezone);
  setValue(form, "schedule_minute", schedule.minute);
  setValue(form, "schedule_times", (schedule.times || []).join(","));
  $("#assetImmediateBtn").addEventListener("click", runImmediateHedge);
}

function persistCurrentAssetForm() {
  const form = $("#assetForm");
  if (!form || !config?.assets?.[form.dataset.currency]) return;
  config.assets[form.dataset.currency] = readAssetForm(form);
}

function readAssetForm(form) {
  const current = config.assets[form.dataset.currency] || {};
  return {
    enabled: form.elements.enabled.checked,
    target_delta: readNumber(form, "target_delta"),
    positive_trigger_delta: readNumber(form, "positive_trigger_delta"),
    negative_trigger_delta: readNumber(form, "negative_trigger_delta"),
    hedge_ratio: readNumber(form, "hedge_ratio"),
    hedge_instrument: form.elements.hedge_instrument.value.trim().toUpperCase(),
    min_order_amount: readNumber(form, "min_order_amount"),
    max_order_amount: readNumber(form, "max_order_amount"),
    delta_to_order: current.delta_to_order || "index_price",
    order_multiplier: current.order_multiplier ?? 1,
    schedule: {
      enabled: form.elements.schedule_enabled.checked,
      mode: form.elements.schedule_mode.value,
      timezone: form.elements.schedule_timezone.value.trim() || "Asia/Shanghai",
      minute: readNumber(form, "schedule_minute"),
      times: form.elements.schedule_times.value
        .split(",")
        .map((item) => item.trim())
        .filter(Boolean),
      force_rebalance: true,
    },
  };
}

function readConfig() {
  persistCurrentAssetForm();
  const global = $("#globalForm");
  return {
    mode: global.elements.mode.value,
    dry_run: false,
    live_trading_armed: true,
    loop_interval_seconds: readNumber(global, "loop_interval_seconds"),
    order_type: global.elements.order_type.value,
    slippage_bps: readNumber(global, "slippage_bps"),
    cooldown_seconds: readNumber(global, "cooldown_seconds"),
    maker_wait_seconds: readNumber(global, "maker_wait_seconds") || 60,
    schedule: config.schedule || defaultSchedule(),
    assets: config.assets,
  };
}

function defaultSchedule() {
  return {
    enabled: true,
    mode: "custom",
    timezone: "Asia/Shanghai",
    minute: 0,
    times: ["08:00", "16:00", "23:55"],
    force_rebalance: true,
  };
}

function updateModeBadge() {
  if (!config) return;
  const mode = selectedMode();
  const badge = $("#modeBadge");
  badge.textContent = mode;
  badge.className = mode === "mainnet" ? "badge warn" : "badge";
}

function selectedMode() {
  return $("#globalForm")?.elements.mode.value || config?.mode || "testnet";
}

function updateCredentialsDisplay() {
  const mode = selectedMode();
  const current = credentialState[mode] || {};
  const configured = Boolean(current.configured);
  $("#credentialBadge").textContent = configured
    ? `${mode} API 已配置 ${current.client_id_masked}`
    : `${mode} API 未配置`;
  $("#credentialBadge").className = configured ? "badge" : "badge warn";
  $("#apiHint").textContent = configured
    ? `${mode} 当前 ID：${current.client_id_masked}。Secret 只保存到本地 .env，不会在页面回显。`
    : `${mode} 当前 ID：空白。`;
  const form = $("#apiForm");
  if (form) {
    form.elements.client_id.placeholder = configured ? "留空则保留当前 ID" : `填写 ${mode} API ID`;
    form.elements.client_secret.placeholder = configured ? "留空则保留当前 Secret" : `填写 ${mode} API Secret`;
  }
}

function renderStatus(status) {
  lastStatus = status;
  const runtimeBadge = $("#runtimeBadge");
  runtimeBadge.textContent = status.running ? "DDH 运行中" : "DDH 已暂停";
  runtimeBadge.className = status.running ? "badge" : "badge muted";
  renderSelectedStatus();
}

function renderSelectedStatus() {
  const status = lastStatus || {};
  const row = (status.last_results || {})[selectedCurrency] || {};
  const decision = row.decision || {};
  const portfolio = row.portfolio || {};
  const market = row.market || {};
  const asset = config?.assets?.[selectedCurrency] || {};
  const thresholdEnabled = asset.enabled !== false;
  const positiveThreshold = thresholdEnabled ? formatNumber(asset.positive_trigger_delta) : "已关闭";
  const negativeThreshold = thresholdEnabled ? formatNumber(asset.negative_trigger_delta) : "已关闭";
  const actionText = hedgeActionText(decision, selectedCurrency);
  const orderAmountText = Number.isFinite(Number(decision.coin_amount))
    ? `${formatNumber(decision.coin_amount)} ${selectedCurrency}`
    : "--";
  const hintText = thresholdEnabled
    ? (row.risk_message || decision.message || portfolio.message || "--")
    : "阈值对冲已暂停；定时对冲和立即对冲仍可按建议动作执行。";

  $("#selectedStatusBadge").textContent = selectedCurrency;
  $("#selectedPositionBadge").textContent = selectedCurrency;

  $("#summary").innerHTML = `
    <div class="summary-card">
      ${metric("Net Delta（PA）", formatNumber(portfolio.net_delta))}
      ${metric("单腿 Delta 合计", formatNumber(portfolio.position_delta_sum))}
      ${metric("目标 Delta", formatNumber(decision.target_delta))}
      ${metric("偏离", formatNumber(decision.delta_gap))}
      ${metric("正向触发阈值", positiveThreshold)}
      ${metric("负向触发阈值", negativeThreshold)}
      ${metric("动作", actionText)}
      ${metric("Deribit 下单数量", orderAmountText)}
      ${metric("交易所最小单位", `${formatNumber(decision.min_trade_amount)} USD ≈ ${formatNumber(decision.min_coin_unit)} ${selectedCurrency}`)}
      ${metric("合约步进单位", `${formatNumber(decision.contract_size)} USD ≈ ${formatNumber(decision.contract_coin_unit)} ${selectedCurrency}`)}
      ${metric("对冲合约", decision.instrument_name || config?.assets?.[selectedCurrency]?.hedge_instrument || "--")}
      ${metric("价格", decision.price || market.mark_price || "--")}
      ${metric("数据源", portfolio.source || "--")}
      ${metric("提示", hintText)}
    </div>
  `;
  renderPositions(portfolio.positions || []);
}

function hedgeActionText(decision, currency) {
  const amount = Number(decision.coin_amount);
  if (!Number.isFinite(amount)) return "--";
  if (Math.abs(amount) < 1e-12) return "无需调整";
  const side = decision.side === "buy" ? "买入" : decision.side === "sell" ? "卖出" : decision.side || "--";
  return `${side} ${formatNumber(amount)} ${currency}`;
}

function renderPositions(positions) {
  const host = $("#positions");
  const oldTable = host.querySelector(".position-table");
  const scrollLeft = oldTable ? oldTable.scrollLeft : 0;
  if (!positions.length) {
    host.innerHTML = `<div class="empty">暂无当前仓位。</div>`;
    return;
  }
  host.innerHTML = `
    <div class="position-table">
      <div class="position-head">
        <span>类型</span><span>合约</span><span>方向</span><span>数量（币本位）</span><span>Delta</span><span>均价</span><span>标记价</span><span>PnL</span>
      </div>
      ${positions.map(positionRow).join("")}
    </div>
  `;
  const newTable = host.querySelector(".position-table");
  if (newTable) {
    newTable.scrollLeft = scrollLeft;
  }
}

function positionRow(row) {
  const pnl = Number(row.floating_profit_loss || row.total_profit_loss || 0);
  return `
    <div class="position-row">
      <span>${escapeHtml(labelType(row.instrument_type || row.kind))}</span>
      <span class="mono">${escapeHtml(row.instrument_name || "--")}</span>
      <span>${escapeHtml(row.direction || "--")}</span>
      <span class="mono">${escapeHtml(positionSize(row))}</span>
      <span class="mono">${escapeHtml(formatNumber(row.delta))}</span>
      <span class="mono">${escapeHtml(formatNumber(row.average_price))}</span>
      <span class="mono">${escapeHtml(formatNumber(row.mark_price))}</span>
      <span class="mono ${pnl < 0 ? "loss" : "gain"}">${escapeHtml(formatNumber(pnl))}</span>
    </div>
  `;
}

function positionSize(row) {
  if (!isPerpetualPosition(row)) {
    return formatNumber(row.size);
  }
  if (Number.isFinite(Number(row.coin_size))) {
    return `${formatNumber(row.coin_size)} ${selectedCurrency}`;
  }
  const size = Number(row.size);
  const price = Number(row.mark_price || row.average_price);
  if (!Number.isFinite(size) || !Number.isFinite(price) || price <= 0) {
    return `${formatNumber(row.size)} USD contracts`;
  }
  const direction = String(row.direction || "").toLowerCase();
  const coinSize = size / price * (direction === "sell" ? -1 : 1);
  return `${formatNumber(coinSize)} ${selectedCurrency}`;
}

function isPerpetualPosition(row) {
  const type = row.instrument_type || row.kind;
  const name = String(row.instrument_name || "");
  return type === "perpetual" || name.endsWith("-PERPETUAL");
}

function labelType(value) {
  if (value === "option") return "期权";
  if (value === "perpetual") return "永续合约";
  if (value === "future") return "交割合约";
  return value || "--";
}

function renderEvents(events) {
  const host = $("#events");
  const fills = events.flatMap(fillRecordsFromEvent).reverse();
  if (!fills.length) {
    host.innerHTML = `<div class="empty">暂无成交记录。</div>`;
    return;
  }
  host.innerHTML = "";
  for (const fill of fills) {
    const line = document.createElement("div");
    line.className = "event-row";
    line.innerHTML = `
      <span>${escapeHtml(formatBeijingTime(fill.ts) || fill.ts || "")}</span>
      <span>${escapeHtml(fill.currency || "--")} / ${escapeHtml(fill.side || "--")}</span>
      <span class="mono">${escapeHtml(fillDetailText(fill))}</span>
    `;
    host.appendChild(line);
  }
}

function renderOpenOrders(orders) {
  const host = $("#openOrders");
  if (!host) return;
  if (!orders.length) {
    host.innerHTML = `<div class="empty">暂无 DDH 挂单。</div>`;
    return;
  }
  host.innerHTML = "";
  for (const order of orders) {
    const line = document.createElement("div");
    line.className = "event-row";
    line.innerHTML = `
      <span>${escapeHtml(formatBeijingTime(order.creation_timestamp) || "--")}</span>
      <span>${escapeHtml(order.currency || "--")} / ${escapeHtml(order.direction || "--")}</span>
      <span class="mono">${escapeHtml(openOrderText(order))}</span>
    `;
    host.appendChild(line);
  }
}

function openOrderText(order) {
  const currency = order.currency || "";
  const coinText = Number.isFinite(Number(order.coin_amount))
    ? `总量 ${formatNumber(order.coin_amount)} ${currency}`
    : "--";
  const filledCoin = Number(order.price) > 0 ? Number(order.filled_amount || 0) / Number(order.price) : 0;
  const filledText = `已成交 ${formatNumber(filledCoin)} ${currency}`;
  const remainingText = Number.isFinite(Number(order.remaining_coin_amount))
    ? `剩余 ${formatNumber(order.remaining_coin_amount)} ${currency}`
    : "剩余 --";
  const contractText = Number.isFinite(Number(order.amount))
    ? `合约 ${formatNumber(order.amount)} USD`
    : "--";
  const priceText = Number.isFinite(Number(order.price)) && Number(order.price) > 0
    ? `挂单价 ${formatNumber(order.price)}`
    : "挂单价 --";
  const state = order.post_only ? "maker" : (order.order_state || "open");
  const label = order.label ? ` · ${order.label}` : "";
  return `${coinText} · ${filledText} · ${remainingText} · ${contractText} · ${priceText} · ${state}${label}`;
}

function orderDetailText(detail, decision) {
  const currency = detail.currency || decision.currency || "";
  const amount = Number.isFinite(Number(decision.coin_amount))
    ? `${formatNumber(decision.coin_amount)} ${currency}`
    : "--";
  const exchangeAmount = Number.isFinite(Number(decision.exchange_amount))
    ? `${formatNumber(decision.exchange_amount)} USD contracts`
    : "--";
  const label = detail.label ? ` · ${detail.label}` : "";
  return `${amount} · ${exchangeAmount}${label}`;
}

function fillRecordsFromEvent(item) {
  if (item.event === "order_filled") {
    const detail = item.detail || {};
    const currency = detail.currency || inferCurrency(detail);
    return [
      {
        ts: item.ts || "",
        currency,
        side: detail.direction || "",
        contracts: Number(detail.fill_amount || 0),
        coinAmount: Number(detail.coin_amount || 0),
        price: Number(detail.price || 0),
        label: detail.label || detail.order_id || "",
      },
    ];
  }
  if (item.event !== "order_submitted") return [];
  const detail = item.detail || {};
  const decision = detail.decision || {};
  const result = detail.result || {};
  const order = result.order || {};
  const trades = Array.isArray(result.trades) ? result.trades : [];
  const currency = detail.currency || decision.currency || "";
  const label = detail.label || order.label || "";

  if (trades.length) {
    return trades.map((trade) => {
      const contracts = Number(trade.amount || 0);
      const price = Number(trade.price || trade.index_price || order.average_price || decision.price || 0);
      return {
        ts: formatTradeTime(trade.timestamp) || item.ts || "",
        currency,
        side: trade.direction || decision.side || order.direction || "",
        contracts,
        coinAmount: price > 0 ? contracts / price : Number(decision.coin_amount || 0),
        price,
        label: trade.trade_id || label,
      };
    });
  }

  const filledAmount = Number(order.filled_amount || 0);
  if (filledAmount <= 0) return [];
  const price = Number(order.average_price || order.price || decision.price || 0);
  return [
    {
      ts: item.ts || "",
      currency,
      side: order.direction || decision.side || "",
      contracts: filledAmount,
      coinAmount: price > 0 ? filledAmount / price : Number(decision.coin_amount || 0),
      price,
      label,
    },
  ];
}

function inferCurrency(detail) {
  const instrument = String(detail.instrument_name || "");
  if (instrument.includes("-")) return instrument.split("-")[0].toUpperCase();
  const label = String(detail.label || "");
  const parts = label.split("-");
  if (parts.length >= 2 && parts[0] === "ddh") return parts[1].toUpperCase();
  return "";
}

function fillDetailText(fill) {
  const coinText = Number.isFinite(Number(fill.coinAmount))
    ? `${formatNumber(fill.coinAmount)} ${fill.currency}`
    : "--";
  const contractText = Number.isFinite(Number(fill.contracts))
    ? `${formatNumber(fill.contracts)} USD contracts`
    : "--";
  const priceText = Number.isFinite(Number(fill.price)) && Number(fill.price) > 0
    ? `均价 ${formatNumber(fill.price)}`
    : "均价 --";
  const label = fill.label ? ` · ${fill.label}` : "";
  return `${coinText} · ${contractText} · ${priceText}${label}`;
}

function formatTradeTime(timestamp) {
  const number = Number(timestamp);
  if (!Number.isFinite(number) || number <= 0) return "";
  return new Date(number).toISOString();
}

function formatBeijingTime(value) {
  const date = parseTimeValue(value);
  if (!date) return "";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).formatToParts(date);
  const part = (type) => parts.find((item) => item.type === type)?.value || "00";
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
}

function parseTimeValue(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  const date = Number.isFinite(number) && number > 0
    ? new Date(number)
    : new Date(String(value));
  return Number.isNaN(date.getTime()) ? null : date;
}

function metric(name, value) {
  return `<div class="metric-row"><span>${escapeHtml(name)}</span><span class="mono">${escapeHtml(value ?? "--")}</span></div>`;
}

function formatNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString("en-US", { maximumFractionDigits: 6 });
}

async function refreshStatus() {
  const status = await api("/api/status");
  renderStatus(status);
}

async function refreshOpenOrders() {
  try {
    const payload = await api("/api/open-orders");
    lastOpenOrders = payload.open_orders || [];
    renderOpenOrders(lastOpenOrders);
  } catch (error) {
    const host = $("#openOrders");
    if (host) {
      host.innerHTML = `<div class="empty">${error.message}</div>`;
    }
  }
}

async function refreshFillEvents() {
  try {
    const payload = await api("/api/fill-events");
    renderEvents(payload.events || []);
  } catch (error) {
    const host = $("#events");
    if (host) {
      host.innerHTML = `<div class="empty">${error.message}</div>`;
    }
  }
}

async function refreshAllData() {
  await refreshStatus();
  await refreshOpenOrders();
  await refreshFillEvents();
}

async function refreshData() {
  showMessage("正在刷新数据...");
  await api("/api/runtime/preview", { method: "POST" });
  await refreshAllData();
  showMessage("数据已刷新。");
}

async function load() {
  const payload = await api("/api/config");
  fillForms(payload);
  await refreshAllData();
}

async function runImmediateHedge() {
  try {
    persistCurrentAssetForm();
    const ok = window.confirm(`确认立即对冲 ${selectedCurrency}？`);
    if (!ok) return;
    await refreshOpenOrders();
    const existing = lastOpenOrders.filter((order) => order.currency === selectedCurrency);
    if (existing.length) {
      const replace = window.confirm(`${selectedCurrency} 当前已有 ${existing.length} 笔 DDH 挂单。继续会取消旧挂单并按最新参数重新挂单，是否继续？`);
      if (!replace) return;
    }
    showMessage(`正在对冲 ${selectedCurrency}...`);
    const payload = await api(`/api/runtime/execute/${selectedCurrency}`, { method: "POST" });
    showMessage(hedgeResultMessage(payload, selectedCurrency));
    await refreshAllData();
    startFastOrderRefresh();
  } catch (error) {
    showMessage(error.message, true);
  }
}

function startFastOrderRefresh() {
  let rounds = 0;
  const timer = setInterval(async () => {
    rounds += 1;
    try {
      await refreshOpenOrders();
      await refreshFillEvents();
    } finally {
      if (rounds >= FAST_ORDER_REFRESH_ROUNDS) {
        clearInterval(timer);
      }
    }
  }, FAST_ORDER_REFRESH_MS);
}

function hedgeResultMessage(payload, currency) {
  const result = payload?.results?.[currency] || {};
  const attempts = Array.isArray(result.attempts) ? result.attempts : [];
  if (!attempts.length) {
    return `${currency} 未提交订单：${result.risk_message || result.decision?.message || "--"}`;
  }
  const filled = attempts.reduce((sum, attempt) => {
    const order = attempt.result?.order || {};
    const trades = Array.isArray(attempt.result?.trades) ? attempt.result.trades : [];
    const orderFilled = Number(order.filled_amount || 0);
    const tradeFilled = trades.reduce((tradeSum, trade) => tradeSum + Number(trade.amount || 0), 0);
    return sum + Math.max(orderFilled, tradeFilled);
  }, 0);
  if (filled > 0) {
    return `${currency} 已成交 ${formatNumber(filled)} USD contracts，Delta 已刷新。`;
  }
  return `${currency} maker 挂单已提交，等待成交；成交前 Delta 不会变化。`;
}

$("#saveBtn").addEventListener("click", async () => {
  try {
    const next = readConfig();
    const payload = await api("/api/config", { method: "POST", body: JSON.stringify(next) });
    config = payload.config;
    renderTabs();
    renderAssetForm();
    updateModeBadge();
    updateCredentialsDisplay();
    showMessage("参数已保存。");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("#saveApiBtn").addEventListener("click", async () => {
  try {
    const form = $("#apiForm");
    const payload = await api("/api/credentials", {
      method: "POST",
      body: JSON.stringify({
        mode: selectedMode(),
        client_id: form.elements.client_id.value.trim(),
        client_secret: form.elements.client_secret.value.trim(),
      }),
    });
    form.reset();
    credentialState = payload.credentials || {};
    updateCredentialsDisplay();
    showMessage("API 已保存。");
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("#startBtn").addEventListener("click", async () => {
  try {
    await api("/api/runtime/start", { method: "POST" });
    showMessage("DDH 已启用。");
    await refreshAllData();
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("#stopBtn").addEventListener("click", async () => {
  try {
    await api("/api/runtime/stop", { method: "POST" });
    showMessage("DDH 已暂停。");
    await refreshAllData();
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("#refreshDataBtn").addEventListener("click", async () => {
  try {
    await refreshData();
  } catch (error) {
    showMessage(error.message, true);
  }
});

$("#globalForm").elements.mode.addEventListener("change", () => {
  updateModeBadge();
  updateCredentialsDisplay();
});

load().catch((error) => showMessage(error.message, true));
setInterval(refreshStatus, STATUS_REFRESH_MS);
setInterval(refreshOpenOrders, ORDERS_REFRESH_MS);
setInterval(refreshFillEvents, ORDERS_REFRESH_MS);
