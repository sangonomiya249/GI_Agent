/* ============================================================
   GI Agent Studio · 前端逻辑（原生 JS，无构建步骤、无外网依赖）
   ============================================================ */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const GROWTH_HIDE_COMPLETE_TASKS_KEY = "gi-agent.growth.hideCompleteTasks";

const state = {
  page: "run",
  seq: 0,
  agent: { state: "stopped", pid: null },
  env: {},
  config: null,
  configGroup: 0,
  pending: {},
  plan: null,
  autoscroll: true,
  logFiles: [],
  channels: [],          // 远程通道摘要（来自 /api/state）
  chanSeq: {},           // 各通道日志游标 { qq: 12, feishu: 0 }
  chanLines: {},         // 已渲染的行数（只用于控制 DOM 体积）
  cooldown: null,        // 资源冷却（/api/cooldown 的最近一次结果）
  cooldownCategory: "specialty",   //   当前看的是哪一类（点上面的类别 chip 切换）
  cooldownSearch: "",    //   页面上的筛选条件
  cooldownOnlyCooling: false,
  growth: null,          // 角色养成（/api/growth 的最近一次结果）
  growthDragging: null,  //   拖拽排序时被拖的那个角色 id
  growthHideCompleteTasks: false, //   当前养成计划：隐藏所需为 0 的已达成材料
  update: null,          // 版本更新（/api/update 的最近一次结果）
};

try {
  state.growthHideCompleteTasks =
    localStorage.getItem(GROWTH_HIDE_COMPLETE_TASKS_KEY) === "1";
} catch {}

/** 冷却页每个类别的图标（"地区特产"用叶子、"矿物"用地图…）。 */
const COOLDOWN_ICONS = { specialty: "leaf", mine: "map", cook: "book", hunt: "spark" };

const ICONS = {
  layout: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7V5Z"/></svg>',
  stop: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  map: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3V6Z"/><path d="M9 3v15M15 6v15"/></svg>',
  sliders: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="11" cy="18" r="2"/></svg>',
  book: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H6.5A2.5 2.5 0 0 0 4 21.5v-16Z"/><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/></svg>',
  pulse: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h4l2-6 4 12 2-6h6"/></svg>',
  terminal: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 7 5 5-5 5M13 17h6"/></svg>',
  info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/></svg>',
  power: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v9M7 5.8a8 8 0 1 0 10 0"/></svg>',
  moon: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 15.5A8.5 8.5 0 0 1 8.5 4 8.5 8.5 0 1 0 20 15.5Z"/></svg>',
  refresh: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.9-4L3 10M3 5v5h5M4 13a8 8 0 0 0 14.9 4L21 14m0 5v-5h-5"/></svg>',
  folder: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6.5A2.5 2.5 0 0 1 5.5 4H10l2 2h6.5A2.5 2.5 0 0 1 21 8.5v9A2.5 2.5 0 0 1 18.5 20h-13A2.5 2.5 0 0 1 3 17.5v-11Z"/></svg>',
  undo: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 7 4 12l5 5"/><path d="M5 12h8a7 7 0 0 1 7 7"/></svg>',
  check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6"/></svg>',
  file: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3h8l4 4v14H6z"/><path d="M14 3v5h5M9 13h6M9 17h6"/></svg>',
  logout: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 4H5v16h5M14 8l4 4-4 4M9 12h9"/></svg>',
  wrench: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 6a5 5 0 0 0-6.4 6.4L4 16l4 4 3.6-3.6A5 5 0 0 0 18 10l-3 3-4-4 3-3Z"/></svg>',
  close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>',
  min: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 12h12"/></svg>',
  max: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  restore: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="8" width="11" height="11" rx="2"/><path d="M9 8V6a2 2 0 0 1 2-2h7a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-2"/></svg>',
  spark: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3Z"/></svg>',
  plug: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3v6M15 3v6M6 9h12v3a6 6 0 0 1-12 0V9ZM12 18v3"/></svg>',
  leaf: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 4c0 8-5 12-11 12H5c0-6 4-10 10-10 2 0 3-.7 5-2Z"/><path d="M5 20c1-4 4-7 8-9"/></svg>',
  clock: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 8v4.5l3 2"/></svg>',
};

function renderIcons() {
  $$("[data-icon]").forEach((el) => {
    const icon = ICONS[el.dataset.icon];
    if (icon) el.innerHTML = icon;
  });
}

const PAGE_META = {
  dashboard: ["概览", "一眼看清 Agent、BetterGI 与今天的路线状态"],
  run: ["运行", "启动 Agent、审批方案、看实时日志"],
  cooldown: ["资源冷却", "特产 48h / 矿物 72h / 食材 24h / 魔物 12h：谁还在冷却、还要等多久"],
  growth: ["角色养成", "米游社库存 + 养成计算器算缺口，BetterGI 只负责跑"],
  routes: ["任务与路线", "调度器脚本组、战斗策略与各类目路线"],
  channels: ["远程通道", "QQ 机器人 / 飞书服务端：内嵌启停与日志，可随 Agent 自动启动"],
  config: ["配置", "图形化编辑 .env（保留注释，自动备份）"],
  guide: ["使用说明", "一条龙怎么配、要加哪些调度器、本软件与 PowerShell 怎么配"],
  update: ["版本更新", "本地版本 vs GitHub 最新 release，要不要更新由你决定"],
  doctor: ["环境体检", "LLM / BetterGI / 脚本组 / 路径 / 事务，一次查完"],
  logs: ["BetterGI 日志", "直接读 BetterGI 自己的 log，已知问题自动标出来"],
  about: ["关于", "GI Agent Studio"],
};

/* ---------------- 基础工具 ---------------- */
const PROVIDER_LABELS = {
  deepseek: "DeepSeek",
  openai: "OpenAI",
  github: "GitHub Models",
  nvidia: "NVIDIA NIM",
  custom: "自定义兼容服务",
  local: "本地模型",
};

const PROVIDER_PRESETS = {
  deepseek: { url: "https://api.deepseek.com/v1", model: "deepseek-chat" },
  openai: { url: "https://api.openai.com/v1", model: "gpt-4o-mini" },
  nvidia: { url: "https://integrate.api.nvidia.com/v1", model: "" },
  local: { url: "http://127.0.0.1:11434/v1", model: "" },
};

async function api(path, options = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...options };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const response = await fetch(path, init);
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = { ok: false, error: text }; }
  return data;
}

function toast(message, kind = "ok", ttl = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 200); }, ttl);
}

function text(value) { return value === undefined || value === null || value === "" ? "—" : String(value); }

function statCard({ label, value, sub, tone = "", icon = "spark" }) {
  return `<div class="stat ${tone}">
    <div class="label"><span class="stat-icon">${ICONS[icon] || ICONS.spark}</span>${label}</div>
    <div class="value">${escapeHtml(text(value))}</div>
    ${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ""}
  </div>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function bytes(size) {
  if (!size) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let index = 0, value = size;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(value < 10 && index > 0 ? 1 : 0)} ${units[index]}`;
}

function when(seconds) {
  if (!seconds) return "—";
  const date = new Date(seconds * 1000);
  return date.toLocaleString("zh-CN", { hour12: false });
}

/* ---------------- 版本更新（系统 → 版本更新） ---------------- */
const UPDATE_TAGS = {
  newer: ["有新版本", "warn", "refresh"],
  same: ["已是最新", "ok", "check"],
  older: ["本地比 release 新", "", "spark"],
  unknown: ["版本号认不出", "", "info"],
  none: ["还没有 release", "", "info"],
  error: ["检查失败", "dim", "info"],
  disabled: ["检查已关闭", "dim", "info"],
};

function renderUpdate(data) {
  const body = $("#update-body");
  const cards = $("#update-cards");
  const notes = $("#update-notes");
  if (!body) return;

  if (!data || !data.ok) {
    body.innerHTML = `<div class="empty">${escapeHtml((data && data.error) || "读取失败")}</div>`;
    if (cards) cards.innerHTML = "";
    $("#update-hint").textContent = "读取失败";
    setUpdateBadge(false);
    return;
  }

  const status = data.status || "error";
  const [label, tone, icon] = UPDATE_TAGS[status] || [status, "", "info"];
  setUpdateBadge(Boolean(data.update_available));
  $("#update-hint").textContent = data.checked_at ? `${data.checked_at} 读取` : "";

  if (cards) {
    cards.innerHTML = [
      statCard({
        label: "本地版本", value: data.local_version || "未知",
        sub: `来源：${data.local_source || "未知"}`, icon: "folder",
      }),
      statCard({
        label: "最新 release", value: data.latest_version || "—",
        sub: data.latest_version ? `发布于 ${String(data.published_at || "").slice(0, 10) || "未知"}` : "仓库里还没有 release",
        icon: "refresh", tone: data.update_available ? "warn" : "",
      }),
      statCard({
        label: "状态", value: label,
        sub: data.update_available ? "可以更新了" : (status === "error" ? "这一项没结论，其他功能照常" : "不需要做任何事"),
        icon, tone,
      }),
      statCard({
        label: "检查方式", value: data.via || "—",
        sub: data.from_cache
          ? `来自缓存（${typeof data.cache_age_hours === "number" ? data.cache_age_hours.toFixed(1) + " 小时前" : "之前"}）`
          : "刚刚联网查的",
        icon: "plug",
      }),
    ].join("");
  }

  const rows = [];
  rows.push(`<div class="update-line"><span class="status-icon">${ICONS[icon] || ICONS.info}</span>${escapeHtml(data.headline || label)}</div>`);
  if (data.detail && ["newer", "older", "unknown", "none"].includes(status)) {
    rows.push(`<div class="muted">${escapeHtml(data.detail)}</div>`);
  }
  if (data.error && status === "error") {
    rows.push(`<div class="muted update-error">${escapeHtml(data.error)}</div>`);
    rows.push(`<div class="hint">只有这一页受影响：Agent / Studio / 跑图都不需要网络，照常用。`
      + `如果本机开着代理（Clash 之类），把地址填进 <code>UPDATE_PROXY</code>；`
      + `如果是证书报错（SSLError），把 Windows 根证书导出成 pem 填进 <code>UPDATE_CA_BUNDLE</code>。</div>`);
  }
  if (data.latest_version) {
    rows.push(`<div class="muted">最新 release：<b>${escapeHtml(data.latest_version)}</b>`
      + `${data.latest_name && data.latest_name !== data.latest_version ? `「${escapeHtml(data.latest_name)}」` : ""}`
      + `${data.prerelease ? "（预发布）" : ""}</div>`);
  }
  if (status === "newer") {
    rows.push(`<div class="hint">更新方式：${escapeHtml(data.update_hint || "")}</div>`);
  }
  body.innerHTML = rows.join("");

  const open = $("#btn-update-open");
  if (open) open.disabled = !data.latest_url;
  if (notes) {
    notes.textContent = data.notes || "（这个 release 没写说明）";
    $("#update-notes-hint").textContent = data.latest_version
      ? `${data.latest_version} · 来自 GitHub release`
      : "来自最新 release";
  }
  state.update = data;
}

/** 侧边栏「版本更新」上的小红点：有新版本才显示。 */
function setUpdateBadge(show) {
  const badge = $("#nav-badge-update");
  if (badge && badge.classList) badge.classList.toggle("hidden", !show);
}

async function loadUpdate(force = false) {
  const body = $("#update-body");
  if (body && force) body.innerHTML = '<div class="empty">正在问 GitHub…</div>';
  const data = await api(`/api/update${force ? "?force=1" : ""}`);
  renderUpdate(data);
}

function setupUpdatePage() {
  const button = $("#btn-update-check");
  if (button && button.addEventListener) button.addEventListener("click", () => loadUpdate(true));
  const open = $("#btn-update-open");
  if (open && open.addEventListener) {
    open.addEventListener("click", async () => {
      const url = (state.update && state.update.latest_url) || "";
      if (!url) return;
      const result = await api("/api/open", { method: "POST", body: { url } });
      if (!result.ok) toast(result.error || "打不开链接", "error");
    });
  }
}

/* ---------------- 导航 ---------------- */
function switchPage(page) {
  state.page = page;
  $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  $$(".page").forEach((el) => el.classList.toggle("hidden", el.dataset.page !== page));
  const [title, sub] = PAGE_META[page] || ["", ""];
  $("#page-title").textContent = title;
  $("#page-sub").textContent = sub;
  if (page === "config") { loadConfig(); loadMysStatus(); }
  if (page === "doctor") loadDoctor();
  if (page === "guide") loadGuide();
  if (page === "logs") loadBgiLog();
  if (page === "routes") loadRoutes();
  if (page === "cooldown") loadCooldown();
  if (page === "growth") loadGrowth();
  if (page === "update") loadUpdate();
  if (page === "dashboard") loadBgiLog(true);
  if (page === "channels") pollChannels();
}

/* ---------------- 状态轮询 ---------------- */
function applyState(data) {
  state.agent = { state: data.state || "stopped", pid: data.pid };
  state.plan = data.plan || state.plan;
  state.env = data;
  if (data.channels) {
    state.channels = data.channels;
    renderChannels();
  }

  const pill = $("#status-pill");
  const labels = { stopped: "未运行", running: "运行中", waiting: "等你确认", exited: "已退出" };
  pill.dataset.state = state.agent.state;
  $("#status-text").textContent = labels[state.agent.state] || state.agent.state;
  $("#side-dot").style.background = {
    running: "var(--ok)", waiting: "var(--warn)", exited: "var(--danger)", stopped: "var(--text-faint)",
  }[state.agent.state];
  $("#side-state").textContent = labels[state.agent.state] + (state.agent.pid ? ` · PID ${state.agent.pid}` : "");

  $("#btn-start").disabled = state.agent.state === "running" || state.agent.state === "waiting";
  $("#btn-stop").disabled = state.agent.state !== "running" && state.agent.state !== "waiting";
  $("#nav-badge-run").classList.toggle("hidden", state.agent.state !== "waiting");

  renderDashboardCards(data);
  renderPlan(data.plan);
}

function renderDashboardCards(data) {
  const routes = data.routes || {};
  const routeText = Object.values(routes).map((row) => `${row.label} ${row.count}`).join(" · ") || "—";
  $("#dash-cards").innerHTML = [
    statCard({ label: "Agent", value: { stopped: "未运行", running: "运行中", waiting: "等你确认", exited: "已退出" }[data.state] || data.state, sub: data.pid ? `PID ${data.pid}` : "点右上角「启动」", icon: "spark", tone: data.state === "waiting" ? "warn" : data.state === "running" ? "ok" : "" }),
    statCard({ label: "LLM", value: `${data.provider || "未配置"} / ${data.model || "—"}`, sub: data.uid ? `UID ${data.uid}` : "还没填 UID", icon: "pulse" }),
    statCard({ label: "BetterGI", value: data.bgi_running_label || (data.bgi_running ? "正在运行" : "未运行"), sub: data.bgi_dir, icon: "play", tone: data.bgi_running ? "ok" : "" }),
    statCard({ label: "当前一条龙", value: data.one_dragon || "—", sub: "Agent 会改这一份配置", icon: "map" }),
    statCard({ label: "展柜数据", value: data.showcase_age || "未知", sub: "可在概览页一键刷新", icon: "book" }),
    statCard({ label: "可用路线", value: routeText, sub: "User\\AutoPathing 下各类目条数", icon: "layout" }),
  ].join("");
}

function planLines(plan) {
  const lines = (plan && plan.lines) || [];
  if (!lines.length) return `<div class="empty">还没有规划结果：去「运行」页启动 Agent 并说出你的需求。</div>`;
  return lines.map((line) => {
    const icon = line.trim().slice(0, 2);
    const rest = line.trim().slice(2).trim();
    return `<div class="plan-line"><span class="ico">${escapeHtml(icon)}</span><span>${escapeHtml(rest)}</span></div>`;
  }).join("");
}

function renderPlan(plan) {
  const html = planLines(plan);
  $("#dash-plan").innerHTML = html;
  $("#run-plan").innerHTML = html;
  const hasPlan = Boolean(plan && plan.lines && plan.lines.length);
  $("#plan-hint").textContent = hasPlan ? "来自最近一次审批" : "等待 Agent 规划…";
  $("#dash-plan-hint").textContent = hasPlan ? "来自最近一次审批" : "等待 Agent 规划…";
  const actions = hasPlan
    ? `<button class="btn primary" data-send="y"><span class="btn-icon">${ICONS.check}</span>批准执行</button>
       <button class="btn" data-send="t"><span class="btn-icon">${ICONS.file}</span>仅写配置</button>
       <button class="btn danger" data-send="exit"><span class="btn-icon">${ICONS.logout}</span>退出</button>`
    : "";
  $("#dash-plan-actions").innerHTML = actions;
}

async function pollLog() {
  const data = await api(`/api/log?since=${state.seq}`);
  if (!data.ok) return;
  state.seq = data.seq;
  if (data.lines && data.lines.length) appendLog(data.lines);
  renderPartial(data.partial);
}

function appendLog(lines) {
  const box = $("#console");
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  const fragment = document.createDocumentFragment();
  lines.forEach((line) => {
    const span = document.createElement("span");
    span.className = `l-${line.level || "info"}`;
    span.textContent = line.text;
    fragment.appendChild(span);
  });
  box.appendChild(fragment);
  while (box.childNodes.length > 3000) box.removeChild(box.firstChild);
  if (state.autoscroll && atBottom) box.scrollTop = box.scrollHeight;
}

let partialEl = null;
function renderPartial(value) {
  const box = $("#console");
  if (partialEl) { partialEl.remove(); partialEl = null; }
  if (!value) return;
  partialEl = document.createElement("span");
  partialEl.className = "l-partial";
  partialEl.textContent = value;
  box.appendChild(partialEl);
  if (state.autoscroll) box.scrollTop = box.scrollHeight;
}

/* ---------------- 远程通道（QQ 机器人 / 飞书） ---------------- */
const CHAN_STATE_LABEL = { running: "运行中", stopped: "未启动", exited: "已退出" };

function channelCard(row) {
  const running = row.state === "running";
  const configured = row.configured !== false;
  const statusText = !configured ? "未配置" : (CHAN_STATE_LABEL[row.state] || row.state);
  const tone = !configured ? "warn" : running ? "ok" : row.state === "exited" ? "bad" : "";
  const missing = (row.missing || []).join(" / ");
  return `<div class="chan" data-chan="${escapeHtml(row.name)}">
    <div class="chan-head">
      <div class="chan-title">
        <b>${escapeHtml(row.label)}</b>
        <span class="chan-state ${tone}">${escapeHtml(statusText)}${row.pid ? ` · PID ${row.pid}` : ""}</span>
      </div>
      <div class="chan-actions">
        <label class="switch" title="点「启动 Agent」时是否一起唤醒这个通道">
          <input type="checkbox" data-chan-auto="${escapeHtml(row.name)}" ${row.auto_start ? "checked" : ""} ${configured ? "" : "disabled"} />
          随 Agent 自动启动
        </label>
        <button class="btn primary sm" data-chan-start="${escapeHtml(row.name)}" ${running || !configured ? "disabled" : ""}>
          <span class="btn-icon">${ICONS.play}</span>启动</button>
        <button class="btn danger sm" data-chan-stop="${escapeHtml(row.name)}" ${running ? "" : "disabled"}>
          <span class="btn-icon">${ICONS.stop}</span>停止</button>
        <button class="btn ghost sm" data-chan-clear="${escapeHtml(row.name)}">清空日志</button>
      </div>
    </div>
    ${configured
      ? `<div class="chan-note">${escapeHtml(row.notes || "")}</div>`
      : `<div class="chan-note warn">还没配置：缺 <code>${escapeHtml(missing)}</code> → ${escapeHtml(row.hint || "")}</div>`}
    <div class="console chan-console" id="chan-console-${escapeHtml(row.name)}"></div>
  </div>`;
}

function renderChannels() {
  const rows = state.channels || [];
  const box = $("#chan-cards");
  if (box) box.innerHTML = rows.length ? rows.map(channelCard).join("") : `<div class="empty">没有可用的通道。</div>`;
  renderIcons();
  // 切页/重绘后把已收到的日志补回来（缓存里存的是完整行，避免空白）
  rows.forEach((row) => {
    const lines = state.chanLines[row.name] || [];
    if (lines.length) appendChannelLines(row.name, lines);
  });

  const mini = $("#dash-channels");
  if (mini) {
    mini.innerHTML = rows.length
      ? rows.map((row) => {
        const configured = row.configured !== false;
        const status = !configured ? "未配置" : (CHAN_STATE_LABEL[row.state] || row.state);
        const tone = !configured ? "warn" : row.state === "running" ? "ok" : "";
        return `<div class="chan-mini-row">
          <span class="chan-state ${tone}">${escapeHtml(status)}</span>
          <b>${escapeHtml(row.label)}</b>
          <span class="muted">${escapeHtml(configured ? (row.auto_start ? "随 Agent 启动" : "手动启动") : `缺 ${(row.missing || []).join("/")}`)}</span>
          <button class="btn ghost sm" data-goto="channels">管理</button>
        </div>`;
      }).join("")
      : `<div class="empty">没有可用的通道。</div>`;
    const hint = $("#dash-chan-hint");
    if (hint) {
      const running = rows.filter((row) => row.state === "running").length;
      hint.textContent = running ? `${running} 个运行中` : "都未启动";
    }
  }
}

function appendChannelLines(name, lines) {
  const box = $(`#chan-console-${name}`);
  if (!box || !lines.length) return;
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  const fragment = document.createDocumentFragment();
  lines.forEach((line) => {
    const span = document.createElement("span");
    span.className = `l-${line.level || "info"}`;
    span.textContent = line.text;
    fragment.appendChild(span);
  });
  box.appendChild(fragment);
  while (box.childNodes.length > 2000) box.removeChild(box.firstChild);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

async function pollChannels() {
  const params = Object.keys(state.chanSeq).map((name) => `since_${name}=${state.chanSeq[name]}`).join("&");
  const data = await api(`/api/channels${params ? `?${params}` : ""}`);
  if (!data.ok) return;
  state.channels = data.channels || [];
  (data.channels || []).forEach((row) => {
    state.chanSeq[row.name] = row.seq;
    if (row.lines && row.lines.length) {
      state.chanLines[row.name] = [...(state.chanLines[row.name] || []), ...row.lines].slice(-2000);
      appendChannelLines(row.name, row.lines);
    }
  });
  renderChannels();
}

async function startChannel(name) {
  const data = await api(`/api/channels/${name}/start`, { method: "POST" });
  if (data.ok) toast(`已启动 ${name === "qq" ? "QQ 机器人" : "飞书服务端"}（PID ${data.pid}）`);
  else toast(data.error || "启动失败", "error");
  await pollChannels();
}

async function stopChannel(name) {
  const data = await api(`/api/channels/${name}/stop`, { method: "POST" });
  if (data.ok) toast("已请求停止");
  else toast(data.error || "停止失败", "error");
  await pollChannels();
}

async function clearChannelLog(name) {
  await api(`/api/channels/${name}/clear`, { method: "POST" });
  state.chanSeq[name] = 0;
  state.chanLines[name] = [];
  const box = $(`#chan-console-${name}`);
  if (box) box.innerHTML = "";
}

async function toggleChannelAuto(name, enabled) {
  const row = (state.channels || []).find((item) => item.name === name);
  if (!row) return;
  const values = { [row.auto_field]: enabled ? "1" : "0" };
  const data = await api("/api/config", { method: "POST", body: { values } });
  if (data.ok) toast(`${row.label}：随 Agent 自动启动已${enabled ? "开启" : "关闭"}`);
  else toast(data.error || "保存失败", "error");
  await pollChannels();
}

/* ---------------- 动作 ---------------- */
async function startAgent() {
  const data = await api("/api/agent/start", { method: "POST" });
  if (data.ok) {
    toast(`已启动 Agent（PID ${data.pid}）`);
    // 通道是"已配置 + 开着自动启动"才会被唤醒，把结果如实说出来（包括为什么没启动）
    (data.channels || []).forEach((note, index) => setTimeout(() => toast(note, note.includes("失败") ? "error" : "ok", 5200), 300 + index * 150));
    switchPage("run");
  } else toast(data.error || "启动失败", "error");
  pollChannels();
}

async function stopAgent() {
  if (!confirm("确定停止吗？\n\n· 会停掉 Agent 子进程；\n· 也会停掉「远程通道」里正在跑的 QQ 机器人 / 飞书服务端；\n· 不会关掉正在跑的 BetterGI。\n· 已提交的配置改动不会回滚。")) return;
  const data = await api("/api/agent/stop", { method: "POST" });
  toast(data.ok ? "已请求停止" : (data.error || "停止失败"), data.ok ? "warn" : "error");
  pollChannels();
}

async function sendInput(value) {
  const fromEntry = value === undefined || value === null;
  const content = String(fromEntry ? $("#input-text").value : value).trim();
  if (!content) return;
  const data = await api("/api/agent/input", { method: "POST", body: { text: content } });
  if (data.ok) {
    if (fromEntry) $("#input-text").value = "";
  } else {
    toast(data.error || "发送失败", "error");
  }
}

async function refreshEnv() {
  toast("正在刷新展柜…");
  const data = await api("/api/refresh-env", { method: "POST" });
  toast(data.ok ? data.notice : (data.error || "刷新失败"), data.ok ? "ok" : "error");
  if (data.ok) await tick();
}

async function openTarget(target) {
  const data = await api("/api/open", { method: "POST", body: { target } });
  if (!data.ok) toast(data.error || "打不开", "error");
}

/* ---------------- 使用说明 ---------------- */
const GUIDE_STATUS = {
  ok: { icon: "check", label: "已完成" },
  warn: { icon: "pulse", label: "建议处理" },
  todo: { icon: "close", label: "还没做" },
  info: { icon: "info", label: "可选项" },
  unknown: { icon: "info", label: "无法确认" },
};

const TASK_STATE = {
  registered: { text: "已登记一条龙", cls: "ok" },
  group_only: { text: "有组未登记（执行时自动登记）", cls: "warn" },
  missing: { text: "缺脚本组", cls: "warn" },
};

async function loadGuide() {
  const data = await api("/api/guide");
  if (!data.ok) {
    $("#guide-steps").innerHTML = `<div class="doctor-row error"><span class="status-icon">${ICONS.close}</span>${escapeHtml(data.error || "读取失败")}</div>`;
    return;
  }

  $("#guide-steps").innerHTML = (data.steps || []).map((step) => {
    const meta = GUIDE_STATUS[step.status] || GUIDE_STATUS.unknown;
    const fix = step.fix ? `<div class="field-help">↳ ${escapeHtml(step.fix)}</div>` : "";
    const rowClass = step.status === "todo" ? "error"
      : step.status === "ok" ? "ok"
      : step.status === "info" ? "" : "warn";
    return `<div class="doctor-row ${rowClass}">
      <span class="status-icon">${ICONS[meta.icon]}</span><span><b>${escapeHtml(step.title)}</b>　${escapeHtml(step.detail || "")}${fix}</span>
    </div>`;
  }).join("");

  const s = data.summary || {};
  const optional = s.optional ? ` · ${s.optional} 项可选` : "";
  $("#guide-summary").textContent = `${s.done || 0}/${s.total || 0} 项就绪 · ${s.todo || 0} 项待办 · ${s.warn || 0} 项建议${optional}`;

  const paths = data.paths || {};
  $("#guide-paths").innerHTML = `当前读取的路径：BetterGI <code>${escapeHtml(paths.bgi_dir || "-")}</code> ｜
    脚本组 <code>${escapeHtml(paths.script_group_dir || "-")}</code> ｜
    一条龙 <code>${escapeHtml(paths.one_dragon_dir || "-")}</code> ｜
    路径追踪 <code>${escapeHtml(paths.auto_pathing_dir || "-")}</code>`;

  const rows = (data.expected_tasks || []).map((task) => {
    const state = TASK_STATE[task.state] || TASK_STATE.missing;
    const flags = [
      task.has_group ? "" : (task.auto_group ? "（Agent 会自动建组）" : "（需要你建组）"),
      task.registered ? "" : (task.auto_register ? "（Agent 会自动登记）" : ""),
    ].filter(Boolean).join("");
    return `<tr>
      <td><b>${escapeHtml(task.name)}</b></td>
      <td>${escapeHtml(task.purpose)}</td>
      <td><span class="tag ${state.cls}">${state.text}</span> ${escapeHtml(flags)}</td>
    </tr>`;
  }).join("");
  $("#guide-tasks").innerHTML = `<thead><tr><th>调度器（脚本组）名</th><th>用途</th><th>当前状态</th></tr></thead>
    <tbody>${rows}</tbody>`;

  // 计划任务逐条状态（StartBetterGI / StopBetterGI / StopGenshin）
  const SCHEDULED_STATE = {
    registered: { text: "已注册", cls: "ok" },
    missing: { text: "缺失", cls: "warn" },
    unknown: { text: "查不出来", cls: "warn" },
  };
  const scheduledRows = (data.scheduled_tasks || []).map((task) => {
    const state = SCHEDULED_STATE[task.state] || SCHEDULED_STATE.unknown;
    return `<tr>
      <td><b>${escapeHtml(task.name)}</b></td>
      <td>${escapeHtml(task.purpose)}</td>
      <td><span class="tag ${state.cls}">${state.text}</span> ${task.required ? "" : "（可选）"}</td>
    </tr>`;
  }).join("");
  $("#guide-scheduled").innerHTML = scheduledRows
    ? `<thead><tr><th>计划任务</th><th>用途</th><th>当前状态</th></tr></thead><tbody>${scheduledRows}</tbody>`
    : "";

  $("#guide-ps").innerHTML = (data.ps_scripts || []).map((script) => `
    <div class="ps-script">
      <h3>${escapeHtml(script.path)}</h3>
      <div class="purpose">${escapeHtml(script.purpose)}</div>
      <pre>${escapeHtml(script.run)}</pre>
      <div class="notes">${escapeHtml(script.notes)}</div>
    </div>`).join("");
}

/* ---------------- 米游社个人战绩 ---------------- */
async function loadMysStatus() {
  const box = $("#mys-status");
  const result = $("#mys-result");
  if (!box) return;
  result.innerHTML = "";
  const data = await api("/api/mys");
  if (!data.ok) {
    box.textContent = `读取失败：${data.error || "未知错误"}`;
    return;
  }
  // 顺带问一下"登录状态完不完整"：只有 v2 的 cookie 能列出角色却算不了材料，
  // 必须在配置页就说清楚，否则玩家会以为一切就绪。
  const status = await api("/api/mys/login/status");
  const login = (status && status.ok && status.status) || null;
  const line = data.configured
    ? `已配置（${data.masked}）｜缓存 ${data.count} 个角色，更新于 ${data.age}｜名单缓存 TTL ${data.ttl_hours} 小时`
    : "未配置：展柜只有 8 个角色，点上面的「扫码登录」就能配好（步骤见 docs/MYS_COOKIE.md）";
  const warning = login && login.configured && !login.complete
    ? `<span class="bad">⚠️ cookie 不完整：缺 ${escapeHtml((login.missing || []).join("、"))}
       —— 能列出角色，但养成计算器与战绩接口会一直报未登录，请点「扫码登录」重新取一份。</span>`
    : "";
  // ★ 两份能力分开报（玩家问过"养成计算器要单独登录"）：
  //   · 算材料 / 已有还差 → 网页那套 v2（「网页版扫码」）；
  //   · 体力 / 战绩       → 扫码那套 v1（「扫码登录」）。
  //   缺哪份就说缺哪份、点哪个按钮，不要只丢一句"cookie 不完整"。
  const caps = login && login.configured
    ? `<span class="muted">｜计算器 ${login.calculator_ready ? "✅" : "❌"}`
      + `　体力 ${login.resin_ready ? "✅" : "❌"}`
      + (login.advice && (!login.calculator_ready || !login.resin_ready)
        ? `<br />${escapeHtml(login.advice)}` : "")
      + "</span>"
    : "";
  box.innerHTML = `${escapeHtml(line)}${caps}${warning ? "<br />" + warning : ""}`;
}

async function checkMysCookie() {
  const result = $("#mys-result");
  const input = $("#f-MYS_COOKIE");
  const cookie = input ? input.value.trim() : "";
  result.innerHTML = `<div class="muted">验证中…（会向米游社发 1 次请求）</div>`;
  const data = await api("/api/mys/check", { method: "POST", body: { cookie } });
  if (!data.ok) {
    result.innerHTML = `<div class="doctor-row error"><span class="status-icon">${ICONS.close}</span>${escapeHtml(data.error || "验证失败")}</div>`;
    return;
  }
  const rows = (data.roles || []).map((role) => (
    `<div class="doctor-row ok"><span class="status-icon">${ICONS.check}</span>✅ ${escapeHtml(role.nickname || "?")}｜UID ${escapeHtml(String(role.uid || ""))}｜${escapeHtml(role.server || "")}｜冒险等阶 ${escapeHtml(String(role.level ?? "?"))}</div>`
  )).join("");
  // ⚠️ 能列出角色 ≠ cookie 完整：只有 v2 键的 cookie 这一关能过，
  // 但养成计算器/战绩接口会全部失败 —— 必须在这里就说出来，别让人以为万事俱备。
  const warning = data.complete === false
    ? `<div class="doctor-row error"><span class="status-icon">${ICONS.close}</span>
        <b>cookie 不完整</b>：缺 ${escapeHtml((data.missing_keys || []).join("、"))}。
        这一关能过，但<b>养成计算器与战绩接口会一直报「未登录」</b>。
        v2 的 ltoken_v2 不能代替 ltoken。${escapeHtml(String(data.warning || "").slice(0, 400))}</div>`
    : "";
  result.innerHTML = `${rows}${warning}<div class="hint">Cookie 可用${data.complete === false ? "（但见上面的告警）" : ""}：记得点上面的<b>保存</b>写进 .env，然后重启 Agent。</div>`;
}

async function refreshMysSnapshot() {
  const result = $("#mys-result");
  result.innerHTML = `<div class="muted">正在拉取全角色名单…</div>`;
  const data = await api("/api/mys/refresh", { method: "POST" });
  if (!data.ok) {
    result.innerHTML = `<div class="doctor-row error"><span class="status-icon">${ICONS.close}</span>${escapeHtml(data.error || "刷新失败")}</div>`;
    return;
  }
  result.innerHTML = `<div class="doctor-row ok"><span class="status-icon">${ICONS.check}</span>✅ 已刷新：${escapeHtml(data.nickname || "")}（UID ${escapeHtml(String(data.uid || ""))}）共 ${data.count} 个角色</div>`;
  loadMysStatus();
}

/* ---------------- 米游社扫码登录 ---------------- */
//
// 为什么要有这个：米游社的 `ltoken` 只在**真正的登录流程**里发放。从浏览器 cookie 库里
// 复制出来的往往只有 `*_v2` 那一套 —— 它能列出角色（看起来配好了），但养成计算器/战绩
// 接口全部失败。
//
// 现在这条路是**服务端直接跟米游社换**：后端调 createQRLogin 拿到一张二维码（SVG 直接
// 画在这里），玩家用米游社 App 扫码确认，后端拿 stoken 换 **v1 ltoken**。
// 全程不碰浏览器、不碰 cookie 库、不涉及任何解密 —— 之前那套"开窗口读 cookie"的坑
// （App-Bound 加密、库被占用、调试端口）统统绕过去了。
//
// ⚠️ 二维码**只活两分钟左右**，所以这里带倒计时，到期自动换一张（并提示玩家重新扫）。
let mysScanTimer = null;
let mysScanCountdown = null;
let mysScanDeadline = 0;
// 当前扫的是哪一种码：`app` = 米游社 App（换 v1 ltoken，战绩/体力认它）；
// `web` = 网页版（换 ltoken_v2，**养成计算器**认它）。两条路的结果会合并进同一份 cookie。
let mysScanKind = "app";

function renderScanProgress(html) {
  const box = $("#mys-scan-progress");
  if (box) box.innerHTML = html;
}

function renderScanQr(svg) {
  const box = $("#mys-scan-qr");
  if (box) box.innerHTML = svg || "";
}

function stopScanPolling() {
  if (mysScanTimer) {
    clearInterval(mysScanTimer);
    mysScanTimer = null;
  }
  if (mysScanCountdown) {
    clearInterval(mysScanCountdown);
    mysScanCountdown = null;
  }
}

/** 倒计时：二维码两分钟就过期，让玩家一眼看到还剩多久。 */
function startScanCountdown(seconds) {
  mysScanDeadline = Date.now() + Math.max(0, Number(seconds) || 0) * 1000;
  if (mysScanCountdown) clearInterval(mysScanCountdown);
  const paint = () => {
    const left = Math.max(0, Math.round((mysScanDeadline - Date.now()) / 1000));
    const hint = $("#mys-scan-hint");
    if (hint) hint.textContent = left > 0
      ? `用手机「米游社」App 扫码 · 这张码还剩 ${left} 秒`
      : "这张二维码已过期，正在换一张…";
    return left;
  };
  paint();
  mysScanCountdown = setInterval(paint, 1000);
}

/** 取一张新二维码并开始轮询（首次点「扫码登录」和「换一张」都走这里）。
 *
 *  `kind`：
 *    · `app`（默认）→ 米游社 App 扫码，换 **v1 `ltoken`**（战绩 / 体力接口认它）；
 *    · `web`        → **网页版**扫码，换 **`ltoken_v2`** 那套（**养成计算器**要的会话）。
 *  两条路换到的键名不冲突，服务端会把它们**合并**进同一份 MYS_COOKIE，
 *  所以两个都点一次，就能同时算材料 + 读体力。
 */
async function startMysScan(kind = "app") {
  mysScanKind = kind === "web" ? "web" : "app";
  const panel = $("#mys-scan-panel");
  if (panel) panel.classList.remove("hidden");
  stopScanPolling();
  renderScanQr("");
  renderScanProgress(`<div class="muted">正在向米游社要一张二维码…</div>`);

  const data = await api(`/api/mys/login${mysScanKind === "web" ? "/web" : ""}/start`,
                         { method: "POST" });
  if (!data.ok) {
    renderScanProgress(`<div class="doctor-row error"><span class="status-icon">${ICONS.close}</span>${escapeHtml(data.error || "二维码没拿到")}</div>`);
    return;
  }
  renderScanQr(data.svg || "");
  const hint = $("#mys-scan-hint");
  if (hint) hint.textContent = data.note || "用手机「米游社」App 扫码";
  startScanCountdown(data.expires_in);
  renderScanProgress(`<div class="muted">等手机扫码…</div>`);
  mysScanTimer = setInterval(pollMysScan, 2000);
}

async function pollMysScan() {
  let data;
  try {
    // ⚠️ app 版的接口就是 `/api/mys/login/poll`（**没有** `app/` 这一层）；
    //    网页版才是 `/api/mys/login/web/poll`。以前这里按 "app" 去拼路径，
    //    那个地址并不存在，POST 落到 SPA 的兜底路由 → 玩家点「扫码登录」只看到一坨
    //    `405 Method Not Allowed` 的 HTML。
    data = await api(`/api/mys/login${mysScanKind === "web" ? "/web" : ""}/poll`);
  } catch (error) {
    return;                       // 网络抖动：下一轮继续
  }
  if (data.state === "pending") {
    renderScanProgress(`<div class="muted">还没扫到，请用手机「米游社」App 扫上面的二维码…</div>`);
    return;
  }
  if (data.state === "scanned") {
    renderScanProgress(`<div class="doctor-row"><span class="status-icon">${ICONS.check}</span>已扫码，请在手机上点「确认登录」。</div>`);
    return;
  }
  if (data.state === "expired") {
    // 过期是"正常"的（两分钟而已），自动换一张，别让玩家自己点
    renderScanProgress(`<div class="muted">${escapeHtml(data.note || "二维码过期了，正在换一张…")}</div>`);
    stopScanPolling();
    await startMysScan();
    return;
  }
  if (data.state === "done") {
    stopScanPolling();
    renderScanQr("");
    if (mysScanKind === "web") {
      // 网页版扫码：拿到的是养成计算器那套 v2 cookie（和 app 那套合并写入）
      renderScanProgress(
        `<div class="doctor-row ok"><span class="status-icon">${ICONS.check}</span>
          ✅ ${escapeHtml(data.note || "网页登录成功")}</div>
         <div class="muted">拿到的键：${escapeHtml((data.keys || []).join("、"))}</div>`
      );
      toast("养成计算器 cookie 已合并写入 .env（v1 的体力键保留）");
    } else {
      const user = data.user || {};
      const stock = data.inventory || {};
      const stockLine = stock.ok
        ? `<div class="muted">已顺手取回 ${escapeHtml(String(stock.avatars))} 个角色 —— 养成页现在就能算材料了。</div>`
        : (stock.error
          ? `<div class="muted">材料这次没取回（${escapeHtml(stock.error)}）：到「角色养成」页点「刷新材料数据」重试。</div>`
          : "");
      renderScanProgress(
        `<div class="doctor-row ok"><span class="status-icon">${ICONS.check}</span>
          ✅ 登录成功，cookie（含 v1 <code>ltoken</code>）已写入 .env。${escapeHtml(data.note || "")}</div>
         <div class="muted">账号：${escapeHtml(user.aid || user.mid || "?")}
          ${user.email ? "｜" + escapeHtml(user.email) : ""}
          ${user.mobile ? "｜" + escapeHtml(user.mobile) : ""}</div>
         <div class="muted">掩码：${escapeHtml(data.masked || "")}</div>
         ${stockLine}`
      );
      toast("米游社登录成功，cookie 已保存（重启 Agent 后生效）");
    }
    loadMysStatus();
    loadConfig();
    loadGrowth();                     // 库存刚同步过，养成页的数据顺手刷新
    return;
  }
  if (data.state === "cancelled") {
    stopScanPolling();
    renderScanQr("");
    renderScanProgress(`<div class="muted">已取消扫码登录（没有改动任何配置）。</div>`);
    return;
  }
  // failed / idle
  stopScanPolling();
  renderScanProgress(`<div class="doctor-row error"><span class="status-icon">${ICONS.close}</span>${escapeHtml(data.error || "登录失败")}</div>`);
}

async function cancelMysScan() {
  stopScanPolling();
  await api("/api/mys/login/cancel", { method: "POST" });
  renderScanQr("");
  renderScanProgress(`<div class="muted">已取消扫码登录。</div>`);
}

/** 兜底：扫码读不出 cookie 时，让玩家直接粘一份过来（同样会先验证再保存）。 */
function promptMysManual() {
  // ★ 默认引导去**养成计算器**那页拿 cookie：它是单独登录的网页，会话里有
  //   DEVICEFP/_MHYUUID 之类的东西，手抄必然漏 —— 所以直接教「Copy as cURL」。
  modal("粘贴米游社 / 养成计算器 Cookie", `
    <div class="growth-form">
      <div class="hint">
        <b>最省事</b>：浏览器打开
        <code>act.mihoyo.com/ys/event/calculator/index.html</code>
        并登录（养成计算器是<b>单独</b>登录的）→ F12 → <b>Network</b> → 随便点一条发往
        <code>api-takumi.mihoyo.com</code> 的请求 → 右键 <b>Copy as cURL</b> →
        把<b>整段</b>粘进下面（我们自动把 <code>Cookie:</code> 抠出来，不用自己挑）。<br />
        也可以只粘 cookie 本体（<code>ltuid=...; ltoken_v2=...</code> 那一行）。
        <b>验证通过才会保存。</b>
      </div>
      <label>Cookie 或整段 cURL
        <textarea id="f-mys-manual-cookie" class="input" rows="5"
          placeholder="curl 'https://api-takumi.mihoyo.com/...' -H 'cookie: ltuid=...; ltoken_v2=...'"></textarea>
      </label>
    </div>`, [
    { label: "验证并保存", kind: "primary", action: submitMysManual },
    { label: "取消", kind: "ghost", action: closeModal },
  ]);
}

async function submitMysManual() {
  const box = $("#f-mys-manual-cookie");
  const cookie = box ? String(box.value || "").trim() : "";
  if (!cookie) {
    toast("先粘贴 cookie", "error");
    return;
  }
  const data = await api("/api/mys/login/manual", { method: "POST", body: { cookie } });
  if (!data.ok) {
    toast(data.error || "验证失败", "error");
    return;
  }
  closeModal();
  toast("cookie 已验证并写入 .env（重启 Agent 后生效）");
  renderScanProgress(`<div class="doctor-row ok"><span class="status-icon">${ICONS.check}</span>✅ 手动粘贴的 cookie 已保存。</div>`);
  loadMysStatus();
  loadConfig();
}

/* ---------------- 体检 ---------------- */
async function loadDoctor(force = true) {
  const box = $("#doctor-rows");
  box.innerHTML = `<div class="empty">检查中…</div>`;
  const data = await api("/api/doctor");
  if (!data.ok) { box.innerHTML = `<div class="doctor-row error"><span class="status-icon">${ICONS.close}</span>${escapeHtml(data.error || "体检失败")}</div>`; return; }
  const icons = { ok: "check", warn: "pulse", error: "close", info: "info" };
  box.innerHTML = data.rows.map((row) => `<div class="doctor-row ${row.level}"><span class="status-icon">${ICONS[icons[row.level]] || ICONS.info}</span><span>${escapeHtml(row.text)}</span></div>`).join("");
  $("#doctor-summary").textContent = `${data.summary.errors} 个错误 · ${data.summary.warns} 个告警`;
}

/* ---------------- 采集冷却 ---------------- */
/** 把小时数说成人话（1 天 23 小时 / 40 分钟）。后端也会给一份，这里兜住手动改数据的情况。 */
function hoursText(hours) {
  if (hours === null || hours === undefined) return "";
  if (hours < 1) return `${Math.round(hours * 60)} 分钟`;
  const whole = Math.floor(hours);
  const minutes = Math.round((hours - whole) * 60);
  if (whole >= 24) {
    const days = Math.floor(whole / 24), rest = whole % 24;
    return rest ? `${days} 天 ${rest} 小时` : `${days} 天`;
  }
  return minutes ? `${whole} 小时 ${minutes} 分` : `${whole} 小时`;
}

function cooldownStatusTag(row) {
  if (row.cooling) return `<span class="tag warn"><span class="status-icon">${ICONS.clock}</span>冷却中 · 还要 ${escapeHtml(hoursText(row.hours_left))}</span>`;
  if (row.partial) return `<span class="tag ok">可以去</span><span class="tag warn">部分完成</span>`;
  if (!row.known) return `<span class="tag ok">可以去</span><span class="tag dim">没有记录</span>`;
  return `<span class="tag ok">可以去</span>`;
}

/** 一个类别里的行；没有匹配的返回空串（好让调用方整块跳过）。 */
function cooldownRows(rows) {
  const keyword = (state.cooldownSearch || "").trim();
  const onlyCooling = Boolean(state.cooldownOnlyCooling);
  const filtered = rows.filter((row) => {
    if (onlyCooling && !row.cooling) return false;
    return !keyword || row.material.includes(keyword);
  });
  if (!filtered.length) return "";
  return filtered.map((row) => {
    const routes = row.total_routes
      ? `${row.ran_routes}/${row.total_routes} 条`
      : `<span class="muted">路线仓库里没有</span>`;
    const source = row.manual ? `<span class="tag dim">手动登记</span>` : "";
    const last = row.last_at
      ? `${escapeHtml(row.last_at)}${row.hours_ago !== null ? `<div class="muted">${escapeHtml(hoursText(row.hours_ago))}前</div>` : ""}`
      : `<span class="muted">—</span>`;
    return `<tr>
      <td><b>${escapeHtml(row.material)}</b> ${source}</td>
      <td>${cooldownStatusTag(row)}</td>
      <td>${last}</td>
      <td>${routes}</td>
      <td><div class="row-actions tight">
        <button class="btn ghost sm" data-cool-mark="${escapeHtml(row.material)}">记为刚刷过</button>
        <button class="btn ghost sm" data-cool-clear="${escapeHtml(row.material)}"${row.known ? "" : " disabled"}>清除</button>
      </div></td>
    </tr>`;
  }).join("");
}

/** 当前选中的类别（点了别的类别就换一个；默认地区特产）。 */
function activeCooldownSection() {
  const sections = (state.cooldown && state.cooldown.sections) || [];
  return sections.find((section) => section.key === state.cooldownCategory) || sections[0] || null;
}

/** 顶部的类别切换（每个类别一个 chip，带"冷却中"角标）。 */
function cooldownTabs(sections) {
  return sections.map((section) => {
    const stats = section.summary || {};
    const active = section.key === state.cooldownCategory;
    const badge = stats.cooling
      ? ` <span class="tag warn">冷却 ${stats.cooling}</span>`
      : "";
    return `<button class="chip${active ? " active" : ""}" data-cool-tab="${escapeHtml(section.key)}">
      ${escapeHtml(section.label)} <span class="muted">${stats.total || 0}</span>${badge}
    </button>`;
  }).join("");
}

/** 汇总卡：只统计**当前类别**（加上一张判定依据）。 */
function cooldownCards(section, data) {
  if (!section) return "";
  const stats = section.summary || {};
  const icon = COOLDOWN_ICONS[section.key] || "leaf";
  return [
    statCard({
      label: `${section.label} · 冷却中`,
      value: `${stats.cooling || 0} 项`,
      tone: stats.cooling ? "warn" : "ok",
      sub: `这一类共 ${stats.total || 0} 项 · 刷新 ${section.hours} 小时`,
      icon,
    }),
    statCard({
      label: "可以去",
      value: `${stats.ready || 0} 项`,
      sub: "已刷新 / 没有记录",
      icon: "pulse",
    }),
    statCard({
      label: "部分完成",
      value: `${stats.partial || 0} 项`,
      tone: stats.partial ? "warn" : "",
      sub: `只跑了零星几条 → 不算跑完（门槛 ${data.min_route_percent}%）`,
      icon: "clock",
    }),
    statCard({
      label: "手动登记",
      value: `${stats.manual || 0} 项`,
      sub: "游戏里自己采过、日志里没有的",
      icon: "check",
    }),
    statCard({
      label: "判定依据",
      value: data.band_enabled ? "按路线比例" : "正常冷却",
      sub: `${section.note || ""}${data.band_enabled ? " · 隔离带开" : " · 隔离带关"}`,
      icon: "book",
    }),
  ].join("");
}

/** 当前类别的表格体；类别由上面的 chip 决定，所以这里不再分块。 */
function cooldownTableBody(section) {
  if (!section) return `<tr><td colspan="5"><div class="empty">还没有可显示的目标</div></td></tr>`;
  const rows = cooldownRows(section.materials || []);
  if (rows) return rows;

  const keyword = (state.cooldownSearch || "").trim();
  const what = state.cooldownOnlyCooling
    ? "这一类没有冷却中的目标（取消「只看冷却中」看看全部）"
    : (keyword ? `这一类没有匹配「${escapeHtml(keyword)}」的目标` : "这一类还没有可显示的目标");
  return `<tr><td colspan="5"><div class="empty">${what}</div></td></tr>`;
}

/** 页脚：这一类的口径说明（数据从哪来、怎么登记自己采的）。 */
function cooldownFoot(section, data) {
  if (!section) return "";
  const parts = [];
  parts.push(`这一类共 <b>${section.summary ? section.summary.total || 0 : 0}</b> 种，` +
    `刷新 <b>${section.hours} 小时</b>（${escapeHtml(section.note || "")}）。` +
    `清单来自 BetterGI 的<b>路线仓库全量目录</b>（Agent 精简过的脚本组只是一次run 的临时样子），` +
    `所以"没跑过的"也在表里，显示为「没有记录（按已刷新处理）」。`);
  parts.push(`数据来自 <b>BetterGI 自己的日志</b>（每条路线跑完都会记一笔），所以你在 BGI 里手动跑的也算。`);
  parts.push(`自己<b>在游戏里采过</b>（日志里没有的）：点那行的「记为刚刷过」即可，等价于
    <code>python -m skills.gather_cooldown --manual 霜仙花</code>；点「清除」把记录删掉。`);
  return parts.join("<br />");
}

function renderCooldown(data) {
  if (!data || !data.ok) {
    $("#cooldown-hint").textContent = (data && data.error) || "读取失败";
    $("#cooldown-table").innerHTML = "";
    $("#cooldown-cards").innerHTML = "";
    $("#cooldown-tabs").innerHTML = "";
    $("#cooldown-title").textContent = "资源冷却";
    $("#cooldown-foot").innerHTML = "";
    return;
  }
  state.cooldown = data;
  const summary = data.summary || {};
  const sections = data.sections || [];
  if (!sections.some((section) => section.key === state.cooldownCategory)) {
    state.cooldownCategory = sections.length ? sections[0].key : "specialty";
  }
  const section = activeCooldownSection();

  // 五个板块：每个类别一个 chip（点它切类别）+ 当前类别的四张统计卡 + 判定依据
  $("#cooldown-tabs").innerHTML = cooldownTabs(sections);
  $("#cooldown-cards").innerHTML = cooldownCards(section, data);
  $("#cooldown-title").textContent = section
    ? `${section.label} · 刷新 ${section.hours} 小时`
    : "资源冷却";

  $("#cooldown-table").innerHTML = `<thead><tr>
      <th>目标</th><th>状态</th><th>上次</th><th>路线</th><th>操作</th>
    </tr></thead><tbody>${cooldownTableBody(section)}</tbody>`;
  $("#cooldown-hint").textContent =
    `${data.generated_at} 读取 · 日志事件 ${data.events} 条 · 四类合计 冷却中 ${summary.cooling || 0}／${summary.total || 0} · 点「刷新」可以随时重算`;
  $("#cooldown-foot").innerHTML = cooldownFoot(section, data);
}

async function loadCooldown() {
  $("#cooldown-hint").textContent = "读取中…";
  const data = await api("/api/cooldown");
  renderCooldown(data);
}

async function cooldownManual(material, action) {
  const data = await api("/api/cooldown/manual", { method: "POST", body: { material, action } });
  if (!data.ok) { toast(data.error || "操作失败", "error"); return; }
  toast(data.message || "已更新");
  await loadCooldown();
}

function setupCooldownPage() {
  const search = $("#cooldown-search");
  const only = $("#cooldown-only-cooling");
  const tabs = $("#cooldown-tabs");
  if (tabs && tabs.addEventListener) {
    // 点类别 chip 只换视图，不重新请求（数据一次就全拿到了）
    tabs.addEventListener("click", (event) => {
      const target = event.target && event.target.closest ? event.target.closest("[data-cool-tab]") : null;
      if (!target) return;
      state.cooldownCategory = target.dataset.coolTab;
      if (state.cooldown) renderCooldown(state.cooldown);
    });
  }
  if (search && search.addEventListener) {
    search.addEventListener("input", () => {
      state.cooldownSearch = search.value || "";
      if (state.cooldown) renderCooldown(state.cooldown);
    });
  }
  if (only && only.addEventListener) {
    only.addEventListener("change", () => {
      state.cooldownOnlyCooling = Boolean(only.checked);
      if (state.cooldown) renderCooldown(state.cooldown);
    });
  }
  const reload = $("#btn-cooldown-reload");
  if (reload && reload.addEventListener) reload.addEventListener("click", () => loadCooldown());
  const table = $("#cooldown-table");
  if (table && table.addEventListener) {
    table.addEventListener("click", (event) => {
      const target = event.target && event.target.closest ? event.target.closest("[data-cool-mark], [data-cool-clear]") : null;
      if (!target) return;
      if (target.dataset.coolMark) cooldownManual(target.dataset.coolMark, "mark");
      else if (target.dataset.coolClear) cooldownManual(target.dataset.coolClear, "clear");
    });
  }
}

/* ============================================================
   角色养成：米游社库存 + 养成计算器算缺口 + BetterGI 只负责跑
   ------------------------------------------------------------
   这一页的三条规矩（和规格书一致，改动前先想清楚）：
     ① 库存永远来自米游社同步，**BetterGI 跑完不在这里加减任何数字**；
     ② 材料需求永远来自养成计算器，前端只显示、不估算；
     ③ 库存不是实时数据时必须显眼地标出来（"旧快照 ≠ 实时库存"）。
   ============================================================ */

/** 阶段 / 状态的中文名（后端会一起给，这里只做兜底）。 */
const GROWTH_PHASE_FALLBACK = { character_level: "角色等级", weapon_level: "武器等级", talent: "天赋" };
const GROWTH_STATUS_TONE = {
  runnable: "ok", cooldown: "warn", closed_today: "warn", waiting_route: "bad", complete: "ok",
};

function growthPhaseLabel(key) {
  const labels = (state.growth && state.growth.phase_labels) || GROWTH_PHASE_FALLBACK;
  return labels[key] || GROWTH_PHASE_FALLBACK[key] || key || "—";
}

function growthNumber(value) {
  const number = Number(value || 0);
  return Number.isFinite(number) ? number.toLocaleString("zh-CN") : String(value);
}

/** 顶部四张卡：计划状态 / 材料数据 / 当前角色 / 今日体力。 */
function growthCards(data) {
  const plan = data.plan || {};
  const summary = plan.summary || {};
  const known = plan.known_inventory || {};
  const resin = plan.current_resin || {};
  // 本轮**实际会下发**的那些任务要花多少体力（和"当前角色的任务"不是一回事：
  // 当前角色可能没得跑、改跑了后面角色的 → 那个数字才是真正要花掉的）
  const plannedResin = ((plan.execution || {}).tasks || [])
    .reduce((sum, task) => sum + Number(task.resin || 0), 0);
  const stateTone = plan.status === "COMPLETE" ? "ok"
    : plan.status === "BLOCKED" ? "warn"
      : plan.status === "WAIT_CONFIRM" ? "ok" : "";
  return [
    statCard({
      label: "养成计划",
      value: plan.status_label || "—",
      tone: stateTone,
      sub: `可执行 ${summary.runnable || 0} 个 · 受阻 ${summary.blocked || 0} 个`,
      icon: "spark",
    }),
    // ⚠️ 这张卡以前显示"米游社库存 未同步"，依据是那条**已经下线**的背包接口的结果。
    //    现在的口径：材料数据由养成计算器提供（每次算材料都会刷新）——
    //    所以这里报"已知已有数量的材料数"，而不是拿一份永远不会有的背包快照说事。
    statCard({
      label: "材料数据",
      value: known.count ? `${known.count} 种已知` : "待计算",
      tone: known.count ? "ok" : "",
      sub: known.count ? "来自养成计算器 · 每次算材料刷新" : "点「刷新材料数据」",
      icon: "book",
    }),
    statCard({
      label: "当前角色",
      value: plan.current_character_name || (plan.status === "COMPLETE" ? "全部完成" : "—"),
      sub: plan.current_phase_label ? `正在做：${plan.current_phase_label}` : "还没有角色目标",
      icon: "layout",
    }),
    statCard({
      label: "当前体力",
      // ⚠️ 以前这张卡显示的是 **summary.resin（本轮计划要花多少体力）**，
      //    但标题写"今日预计体力"，玩家自然读成"我有多少体力" ——
      //    实测被反馈过："实际只有 27 点，卡片却显示 160"。
      //    现在：主数字 = **你实际有多少体力**（接口读不到就用手动记的），
      //    副标题才写"本轮计划要花多少"，两者分开，不再混为一谈。
      value: resin.available
        ? growthNumber(resin.current)
        : (resin.reason ? "未知" : "—"),
      tone: resin.available ? "ok" : "warn",
      sub: resin.available
        ? `本轮会花 ${growthNumber(plannedResin)}${resin.max ? ` · 上限 ${growthNumber(resin.max)}` : ""}`
        : `本轮会花 ${growthNumber(plannedResin)} · 在下面填一下当前体力`,
      icon: "clock",
    }),
  ].join("");
}

/** 库存卡：把"这是不是实时数据"说清楚，并把本次同步的增减列出来。 */
/** 米游社库存卡片。
 *
 * ⚠️ 这里的口径已经变了：**「已有」不再来自那个下线的背包接口**，而是养成计算器
 * 每次算材料时一起给的 `available_material`（`plan.known_inventory`）。
 * 所以：
 *   · 有已知数据 → 直接列出来，别再显示"未同步/失败"那种吓人的东西；
 *   · 一条都没有 → 说明"算一次材料就有了"，而不是让玩家去点一个必然失败的按钮。
 */
function renderGrowthInventory(data) {
  const plan = data.plan || {};
  const known = plan.known_inventory || {};
  const inventory = data.inventory || {};
  const rows = [];

  if (!inventory.configured) {
    rows.push(`<div class="growth-line bad">未配置米游社 Cookie —— 材料算不出来。<br />
      去「配置 → 米游社个人战绩」点「扫码登录」，或从养成计算器网页复制 cookie 粘进去。</div>`);
  } else if (known.count) {
    // ⚠️ 这里**不再把材料一个个列出来**（玩家反馈："没必要汇总，没用"）。
    //    具体某个材料有没有、还差多少，看下面任务表的「消耗 / 所需」就够了。
    rows.push(`<div class="growth-line ok">
      材料数据已就绪：<b>${known.count}</b> 种材料知道你已有多少
      <span class="muted">（来自养成计算器，每次算材料自动刷新；点「刷新材料数据」可立即更新）</span>
    </div>`);
  } else {
    rows.push(`<div class="growth-line muted">
      还没有算过材料 —— 点「刷新材料数据」，米游社会在算材料时一起给出「已有」数量。
    </div>`);
  }

  const delta = state.growthDelta;
  if (delta && (delta.deposited || delta.consumed || []).length) {
    const parts = [];
    for (const row of (delta.deposited || []).slice(0, 10)) {
      parts.push(`<span class="growth-delta up">${escapeHtml(row.name)} ${growthNumber(row.before)} → ${growthNumber(row.after)}（+${growthNumber(row.delta)}）</span>`);
    }
    for (const row of (delta.consumed || []).slice(0, 10)) {
      parts.push(`<span class="growth-delta down">${escapeHtml(row.name)} ${growthNumber(row.before)} → ${growthNumber(row.after)}（${growthNumber(row.delta)}）</span>`);
    }
    rows.push(`<div class="growth-line"><b>本次变化：</b>${parts.join("")}</div>`);
  }

  // ★ 当前体力：米游社的体力接口对这个账号被风控挡着（5003 账号数据异常），
  //   所以让玩家**填一次**，程序按 8 分钟 1 点往后推算 —— 比"读不到就瞎排趟数"强得多。
  const resin = plan.current_resin || {};
  const resinNow = resin.available
    ? `<b>${growthNumber(resin.current)}</b>${resin.max ? `/${growthNumber(resin.max)}` : ""}`
      + `<span class="muted">（${escapeHtml(resin.source || "已记录")}`
      + (resin.recovered ? `，已回涨 ${resin.recovered} 点` : "")
      + (resin.stale ? " ⚠️ 记下来超过 12 小时了，建议重填" : "") + "）</span>"
    : `<span class="muted">未知（${escapeHtml(resin.reason || "还没记过")}）</span>`;
  rows.push(`<div class="growth-line">
    💧 当前体力：${resinNow}
    <button class="btn ghost sm" data-growth-resin-refresh
            title="真的打一次米游社读现在的实时体力；读不到会说明原因">拉取实时体力</button>
    <input id="f-growth-resin" class="input sm" type="number" min="0" max="200"
           placeholder="当前体力" style="width:8em;margin-left:6px" />
    <button class="btn ghost sm" data-growth-resin-save>记下体力</button>
    <span class="muted">—— 接口读不到时才需要手填：填一次按 8 分钟 1 点回涨推算（上限 200）</span>
  </div>`);
  $("#growth-inventory").innerHTML = rows.join("");
}

/** 手动拉取实时体力（真打一次米游社，不是拿推算糊弄）。 */
async function refreshGrowthResin(button) {
  if (button) { button.disabled = true; button.textContent = "拉取中…"; }
  try {
    const data = await api("/api/growth/resin/refresh", { method: "POST" });
    if (!data.ok) { toast(data.error || "拉取失败", "error"); return; }
    const state = data.resin || {};
    if (data.from_api) {
      toast(`✅ 已读到实时体力：${state.current}${state.max ? "/" + state.max : ""}`);
    } else {
      toast(data.note || "读不到实时体力", "warn", 7000);
    }
    await loadGrowth();
  } finally {
    if (button) { button.disabled = false; button.textContent = "拉取实时体力"; }
  }
}

/** 保存当前体力（填了值就按它算趟数）。 */
async function saveGrowthResin() {
  const input = $("#f-growth-resin");
  const value = input ? String(input.value || "").trim() : "";
  if (!value) { toast("先填当前体力（数字）", "warn"); return; }
  const data = await api("/api/growth/resin", { method: "POST", body: { value } });
  if (!data.ok) { toast(data.error || "保存失败", "error"); return; }
  toast(data.note || "已记下体力");
  await loadGrowth();
}

/** 受阻原因。
 *
 * ⚠️ 玩家明确要求删掉这块：任务表里每行已经有**状态列**（可执行 / 测试未开放 / 缺路线），
 * 这里再把这些原因抄一遍只是重复。函数保留（容器还在 HTML 里），但不再渲染任何内容 ——
 * 别的页面/未来要恢复的话改这一处就行。
 */
function renderGrowthBlockers(plan) {
  const box = $("#growth-blockers");
  if (box) box.innerHTML = "";
}

/** 任务表：一条任务一行，状态用颜色区分（可执行 / 冷却 / 未开放 / 缺路线 / 已齐）。
 *
 * 数字列对齐米游社计算器：**消耗** = 总需求，**所需** = 还需补的数量。
 * ⚠️ 别再用 `a / b` 那种写法 —— 会被读成"拥有/拥有"或者"进度占比"。
 * 拿不到背包数据时，所需会退化为消耗（保守：宁可多刷），末尾带一个 `*` 并在 title 里说明。
 */
function renderGrowthTasks(plan) {
  const sourceTasks = plan.tasks || [];
  const tasks = state.growthHideCompleteTasks
    ? sourceTasks.filter((task) => Number(task.missing || 0) > 0)
    : sourceTasks;
  const head = `<thead><tr>
    <th>状态</th><th>角色 / 阶段</th><th>材料</th>
    <th class="num">消耗</th><th class="num">所需</th>
    <th>来源</th><th>说明</th>
  </tr></thead>`;
  const legend = `<caption class="muted growth-table-note">消耗 = 本次养成一共会用多少；所需 = 按你账号背包算出的<strong>还差</strong>。</caption>`;
  if (!tasks.length) {
    const emptyText = sourceTasks.length && state.growthHideCompleteTasks
      ? "已隐藏所需为 0 的材料"
      : "当前没有待补的材料";
    $("#growth-tasks").innerHTML = legend + head +
      `<tbody><tr><td colspan="7"><div class="empty">${emptyText}</div></td></tr></tbody>`;
    return;
  }
  const body = tasks.map((task) => {
    const phases = (task.phases || [{ phase: task.phase, phase_label: task.phase_label }])
      .map((row) => escapeHtml(row.phase_label || growthPhaseLabel(row.phase))).join("、");
    const route = task.route && task.route !== task.material
      ? `${escapeHtml(task.task_label)}：${escapeHtml(task.route)}`
      : escapeHtml(task.task_label || "");
    // 材料族（架构文档 §6）：同族材料是**同一条路线**掉的（异海凝珠/异海之块/异色结晶石
    // 都由「原海异种」掉），悬停时把这件事说清楚 —— 玩家才知道刷一趟能同时补几档。
    const family = task.family && (task.family_materials || []).length > 1
      ? ` title="${escapeHtml(`「${task.material}」由「${task.family}」掉落，同族还有 ${task.family_materials.filter((x) => x !== task.material).join("、")}；一条路线全掉`)}"`
      : "";
    const note = [task.status_note, task.count_note].filter(Boolean).join("；");
    const star = task.owned_known ? ""
      : `<span class="muted" title="拿不到背包数据（米游社没给这个材料的已有数量），所需按消耗计">*</span>`;
    return `<tr>
      <td><span class="tag ${GROWTH_STATUS_TONE[task.status] || ""}">${escapeHtml(task.status_label || task.status)}</span></td>
      <td>${escapeHtml(task.character_name || "—")}<span class="muted"> · ${phases}</span></td>
      <td>${escapeHtml(task.material || "—")}</td>
      <td class="num"><b>${growthNumber(task.required)}</b></td>
      <td class="num muted">${growthNumber(task.missing)}${star}</td>
      <td${family}>${route}</td>
      <td class="muted">${escapeHtml(note)}</td>
    </tr>`;
  }).join("");
  $("#growth-tasks").innerHTML = legend + head + `<tbody>${body}</tbody>`;
}

/** 角色目标表：启用、拖拽手柄、等级 / 天赋 / 武器目标、阶段优先级、编辑与删除。 */
function renderGrowthTargets(data) {
  const targets = data.targets || [];
  const head = `<thead><tr>
    <th class="drag-col">顺序</th><th>启用</th><th>角色</th><th>当前</th><th>目标</th>
    <th>养成顺序</th><th>素材缺口</th><th class="ops-col">操作</th>
  </tr></thead>`;
  if (!targets.length) {
    $("#growth-targets").innerHTML = head +
      `<tbody><tr><td colspan="8"><div class="empty">还没有角色目标 —— 点右上角「添加角色」</div></td></tr></tbody>`;
    return;
  }
  const byId = {};
  for (const row of (data.plan && data.plan.characters) || []) byId[row.character_id] = row;

  const body = targets.map((target, targetIndex) => {
    const view = byId[target.character_id] || {};
    const stars = "★".repeat(Math.max(0, Math.min(5, Number(target.rarity || 0))));
    const order = [
      [target.level_priority, "角色等级", target.level_target],
      [target.weapon_priority, target.weapon_enabled ? "武器等级" : "武器（未纳入）", target.weapon_enabled ? target.weapon_level_target : "—"],
      [target.talent_priority, "天赋", `${target.normal_target}/${target.skill_target}/${target.burst_target}`],
    ].sort((a, b) => Number(a[0] || 9) - Number(b[0] || 9))
      .map((row, index) => `${index + 1}. ${escapeHtml(String(row[1]))} ${escapeHtml(String(row[2]))}`)
      .join("<br />");
    const missing = view.missing_total;
    const missingLabel = view.compute_pending
      ? `<span class="tag warn" title="${escapeHtml(view.compute_error || "本轮尚未算到材料需求")}">待计算</span>`
      : view.complete
        ? `<span class="tag ok">目标已达成</span>`
      : (missing === undefined || missing === null
        ? "—"
        : (missing > 0 ? `${growthNumber(missing)} 个` : "✅ 已齐"));
    return `<tr data-growth-id="${target.character_id}" draggable="true">
      <td class="drag-col">
        <span class="drag-handle" title="拖动排序">⠿</span>
        <input class="input sm growth-order-input" type="number" min="1" max="${targets.length}"
               value="${targetIndex + 1}" data-growth-order="${target.character_id}"
               title="输入序号后回车，快速移动到指定位置" />
      </td>
      <td><input type="checkbox" data-growth-toggle="${target.character_id}" ${target.enabled ? "checked" : ""} /></td>
      <td>${escapeHtml(target.character_name || target.character_id)}<span class="muted"> ${stars}</span>
        <button class="btn ghost sm growth-owned" data-growth-owned="${target.character_id}"
                data-growth-owned-now="${target.owned ? 1 : 0}"
                title="点一下切换「已拥有 / 未拥有」（米游社关掉了「我的角色」接口，这个得你来标）">
          ${target.owned ? "已拥有" : "未拥有"}
        </button></td>
      <td>${escapeHtml(view.current_summary || "—")}</td>
      <td>${escapeHtml(view.target_summary || `${target.level_target}/${target.normal_target}/${target.skill_target}/${target.burst_target}`)}</td>
      <td class="muted">${order}</td>
      <td>${missingLabel}</td>
      <td class="ops-col">
        <button class="btn ghost sm" data-growth-refresh-one="${target.character_id}">拉取缺口</button>
        <button class="btn ghost sm" data-growth-edit="${target.character_id}">编辑</button>
        <button class="btn ghost sm" data-growth-delete="${target.character_id}">删除</button>
      </td>
    </tr>`;
  }).join("");
  $("#growth-targets").innerHTML = head + `<tbody>${body}</tbody>`;
}

/** 历史：只显示**有意义**的同步与执行记录。
 *
 * ⚠️ 背包接口已经下线，那条路上的失败每次都会往历史里塞一行 —— 玩家看到的是
 * "成功 0 次 · 失败 50 次"加一屏 `retcode=-100`，全是噪音（而且材料明明已经拿到了）。
 * 所以：背包接口相关的失败**不显示**，也不参与成功/失败计数。
 */
const GROWTH_NOISE_SYNC = /背包|avatar\/list|-100/;

function renderGrowthHistory(data) {
  const syncs = (data.sync_history || [])
    .filter((row) => row.success || !GROWTH_NOISE_SYNC.test(String(row.error || "")))
    .slice(0, 8);
  const executions = (data.executions || []).slice(0, 8);
  const parts = [];

  const ok = syncs.filter((row) => row.success).length;
  const bad = syncs.length - ok;
  if (syncs.length) {
    parts.push(`<div class="growth-line">材料数据：成功 <b>${ok}</b> 次${bad ? ` · 失败 <b>${bad}</b> 次` : ""}</div>`);
    parts.push("<div class=\"growth-line\">" + syncs.map((row) => {
      const tone = row.success ? "up" : "down";
      const tail = row.success ? `快照 ${row.snapshot_id}` : escapeHtml(row.error || "失败");
      return `<span class="growth-delta ${tone}">${escapeHtml(row.finished_at || row.started_at || "—")} ${tail}</span>`;
    }).join("") + "</div>");
  } else {
    parts.push(`<div class="growth-line muted">还没有失败记录 —— 材料数据由养成计算器提供，不再需要"同步背包"这一步。</div>`);
  }
  if (executions.length) {
    parts.push("<div class=\"growth-line\"><b>最近执行：</b>" + executions.map((row) =>
      `<span class="growth-delta">${escapeHtml(row.started_at || "—")} 角色 ${row.character_id || "—"}
        · ${escapeHtml(row.status || "")} · ${escapeHtml((row.result || "").slice(0, 40))}</span>`).join("") + "</div>");
  }
  if ((data.snapshots || []).length) {
    parts.push(`<div class="growth-line muted">本地快照：${data.snapshots.slice(0, 5).map((name) => escapeHtml(name)).join("、")}</div>`);
  }
  $("#growth-history").innerHTML = parts.join("");
  $("#growth-history-hint").textContent = "";
}

function renderGrowth(data) {
  if (!data || !data.ok) {
    $("#growth-plan-hint").textContent = (data && data.error) || "读取失败";
    $("#growth-plan-hint").className = "muted bad";
    $("#growth-cards").innerHTML = "";
    return;
  }
  state.growth = data;
  const plan = data.plan || {};
  $("#growth-cards").innerHTML = growthCards(data);
  renderGrowthInventory(data);
  renderGrowthTargets(data);
  renderGrowthHistory(data);

  // ⚠️ 表格下面那几行提示**按玩家要求删掉了**：
  //   · 时间戳 + "计算 · 受阻 · 材料 3 项有缺口 · 可执行 0 个"（顶部第一张卡已经说了状态）；
  //   · "「已有 / 还差」由养成计算器提供…"（库存卡已经说了来源）；
  //   · "XX 缺少 BetterGI 可执行路线"（任务表里每一行的状态列就是"缺少路线"）。
  //   它们只是把同一件事说三遍。真要报错（计划生成失败）才显示一行。
  const summary = plan.summary || {};
  $("#growth-plan-hint").textContent = plan.error ? `计划生成失败：${plan.error}` : "";
  $("#growth-plan-hint").className = plan.error ? "muted bad" : "muted";
  $("#growth-blockers").innerHTML = "";
  renderGrowthTasks(plan);

  // 表尾也只留"每个角色缺多少"这种别处看不到的信息
  const foot = [];
  // 执行方式（一次性 / 分批次）与队列进度 —— 配置页里才能改，这里要能一眼看到
  if (plan.execution_mode) {
    const queue = plan.execution_queue || {};
    let line = `执行方式：${escapeHtml(plan.execution_mode_label || plan.execution_mode)}`;
    if (plan.execution_mode === "stepwise" && queue.total) {
      line += `（共 ${queue.total} 条，已跑 ${queue.done || 0} 条`;
      line += queue.current
        ? `，下一条：${escapeHtml(queue.current.material)}）`
        : "，已跑完）";
    }
    foot.push(line);
  }
  const estimate = plan.estimate || {};
  if (estimate.text) foot.push(escapeHtml(estimate.text));
  const missing = (plan.characters || []).filter((row) => row.missing_total > 0);
  if (missing.length) {
    foot.push("各角色还差：" + missing.map((row) =>
      `${escapeHtml(row.character_name || row.character_id)} ${growthNumber(row.missing_total)} 个`).join("；"));
  }
  if (plan.compute_errors && plan.compute_errors.length) {
    foot.push("算不出需求的角色：" + plan.compute_errors.map((row) =>
      `${escapeHtml(row.character_name || row.character_id)}（${escapeHtml(row.error)}）`).join("；"));
  }
  const computeBudget = plan.compute_budget || {};
  if (computeBudget.deferred) {
    foot.push(`本轮只新算了 ${computeBudget.used || 0}/${computeBudget.limit || 0} 个角色，`
      + `${computeBudget.deferred} 个角色待下一轮重新计算，不代表材料已齐。`);
  }
  $("#growth-plan-foot").innerHTML = foot.join("<br />");

  const select = $("#growth-sort-mode");
  if (select && data.sort_modes) {
    select.innerHTML = data.sort_modes.map((row) =>
      `<option value="${escapeHtml(row.value)}"${row.value === data.sort_mode ? " selected" : ""}>${escapeHtml(row.label)}</option>`).join("");
  }
}

async function loadGrowth() {
  $("#growth-plan-hint").textContent = "读取中…";
  $("#growth-plan-hint").className = "muted";
  const data = await api("/api/growth");
  renderGrowth(data);
}

/** 「刷新材料数据」：重算一次缺口（米游社会在算材料时一起把「已有」数量给出来）。
 *
 * ⚠️ 不再调 `/api/growth/sync` —— 那条走的是**已经下线**的背包接口，
 * 每次必然 `-100`，只会在界面上刷一堆失败记录。材料的「已有 / 还差」现在
 * 由养成计算器的 `available_material` 提供，重算就有。
 */
async function syncGrowthInventory() {
  const button = $("#btn-growth-sync");
  if (button) { button.disabled = true; button.textContent = "计算中…"; }
  toast("正在向米游社算材料（顺带取回「已有」数量）…");
  try {
    const data = await api("/api/growth/plan", { method: "POST", body: { force_compute: true } });
    if (!data.ok) {
      toast(data.error || "重算失败", "error");
      await loadGrowth();
      return;
    }
    const known = ((data.plan || {}).known_inventory || {}).count || 0;
    toast(known ? `已更新：${known} 种材料的「已有」数量` : "已重算（米游社这次没给已有数量）");
    await loadGrowth();
  } finally {
    if (button) { button.disabled = false; button.textContent = "刷新材料数据"; }
  }
}

async function refreshGrowthPlan(sync) {
  const data = await api("/api/growth/plan", { method: "POST", body: { sync: Boolean(sync), force_compute: true } });
  if (!data.ok) { toast(data.error || "重算失败", "error"); return; }
  if (data.sync && !data.sync.ok) {
    toast(`同步失败：${data.sync.error}`, "error");
    if (data.sync.advice) toast(data.sync.advice, "warn");
  }
  if (data.lines && data.lines.length) toast(data.lines[0]);
  await loadGrowth();
}

async function executeGrowthPlan() {
  const onlyConfig = Boolean($("#growth-execute-test") && $("#growth-execute-test").checked);
  const decision = onlyConfig ? "t" : "y";
  const plan = (state.growth && state.growth.plan) || {};
  const stepwise = plan.execution_mode === "stepwise";
  const queue = plan.execution_queue || {};
  // 分批次：每次只跑**当前这一条**，点一次往下走一条（队列跑完了才重新起一轮）
  const stepLine = stepwise && queue.current
    ? `\n\n分批次：第 ${queue.current.index}/${queue.total} 条 —— ${queue.current.material}`
      + `（${queue.current.task_label}${queue.current.route ? " → " + queue.current.route : ""}）`
    : "";
  const label = onlyConfig ? "只写配置（不启动 BetterGI）" : "真的启动 BetterGI 执行";
  if (!window.confirm(`确认${label}？${stepLine}\n\n下发后 BetterGI 跑完会自动重新同步米游社并重算缺口。`)) return;

  const button = $("#btn-growth-execute");
  if (button) button.disabled = true;
  try {
    const data = await api("/api/growth/plan/execute", { method: "POST", body: { decision } });
    if (!data.ok) {
      toast(data.error || "下发失败", "error");
      for (const blocker of (data.blockers || []).slice(0, 3)) toast(blocker.message, "warn");
      if (data.steps && data.steps.length) {
        for (const line of data.steps.slice(0, 6)) toast(line);
      }
      if (data.plan) renderGrowth({ ok: true, ...state.growth, plan: data.plan });
      return;
    }
    toast(data.note || "任务已下发");
    if (data.stepwise && data.queue && data.queue.current) {
      const current = data.queue.current;
      toast(`分批次：正在跑第 ${current.index}/${data.queue.total} 条 —— `
        + `${current.material}（${current.task_label}）`);
    }
    if (data.fallback) toast("当前角色的材料今天拿不到，本轮改做后面角色的可执行材料", "warn");
    renderGrowth({ ok: true, ...state.growth, plan: data.plan });
  } finally {
    if (button) button.disabled = false;
  }
}

async function toggleGrowthTarget(characterId, enabled) {
  const data = await api("/api/growth/targets", {
    method: "POST", body: { character_id: characterId, target: { enabled } },
  });
  if (!data.ok) { toast(data.error || "更新失败", "error"); return; }
  await loadGrowth();
}

async function deleteGrowthTarget(characterId) {
  if (!window.confirm("确认删除这个角色的养成目标？")) return;
  const data = await api(`/api/growth/targets/${characterId}`, { method: "DELETE" });
  if (!data.ok) { toast(data.error || "删除失败", "error"); return; }
  toast("已删除");
  await loadGrowth();
}

async function refreshGrowthTargetRequirements(characterId, button) {
  const oldText = button ? button.textContent : "";
  if (button) { button.disabled = true; button.textContent = "拉取中…"; }
  try {
    const data = await api(`/api/growth/targets/${characterId}/refresh-requirements`, {
      method: "POST",
    });
    if (!data.ok) { toast(data.error || "拉取失败", "error"); return; }
    renderGrowth(data);
    if (data.refresh_ok) {
      toast("已拉取这个角色的材料缺口");
    } else {
      toast(data.refresh_error || "这个角色本次没有算出材料缺口", "warn");
    }
  } finally {
    if (button) { button.disabled = false; button.textContent = oldText || "拉取缺口"; }
  }
}

async function clearGrowthTargets() {
  const count = ((state.growth && state.growth.targets) || []).length;
  if (!count) { toast("当前没有已添加的养成目标"); return; }
  if (!window.confirm(`确认清空 ${count} 个角色养成目标？\n\n只会删除目标列表和旧材料缺口缓存，不会删除拥有状态、库存或材料字典。`)) return;
  const data = await api("/api/growth/targets", { method: "DELETE" });
  if (!data.ok) { toast(data.error || "清空失败", "error"); return; }
  toast(`已清空 ${data.deleted || count} 个养成目标`);
  await loadGrowth();
}

/** 编辑表单：等级 1~90 任意值（81 合法），三个天赋各自 1~10。 */
function growthLevelOptions(selected) {
  let html = "";
  for (let level = 1; level <= 90; level += 1) {
    html += `<option value="${level}"${Number(selected) === level ? " selected" : ""}>${level}</option>`;
  }
  return html;
}

function growthTalentOptions(selected) {
  let html = "";
  for (let level = 1; level <= 10; level += 1) {
    html += `<option value="${level}"${Number(selected) === level ? " selected" : ""}>${level}</option>`;
  }
  return html;
}

function growthTargetForm(target, current) {
  target = target || {};
  current = current || {};
  const talents = current.talents || {};
  return `
    <div class="growth-form">
      <div class="muted">当前状态：Lv ${escapeHtml(current.level === undefined ? "—" : String(current.level))}
        · 天赋 ${escapeHtml(`${talents.normal || 0}/${talents.skill || 0}/${talents.burst || 0}`)}
        ${current.weapon_level ? `· 武器 ${current.weapon_level}` : ""}
        （${current.source === "mys_sync" ? "来自养成计算器的账号状态"
            : current.source === "mys" ? "来自米游社档案" : "尚未读到账号状态"}）</div>
      <label>角色等级目标
        <select id="f-g-level">${growthLevelOptions(target.level_target || 88)}</select>
      </label>
      <div class="growth-form-row">
        <label>普通攻击<select id="f-g-normal">${growthTalentOptions(target.normal_target || 1)}</select></label>
        <label>元素战技<select id="f-g-skill">${growthTalentOptions(target.skill_target || 9)}</select></label>
        <label>元素爆发<select id="f-g-burst">${growthTalentOptions(target.burst_target || 9)}</select></label>
      </div>
      <label class="switch"><input type="checkbox" id="f-g-weapon" ${target.weapon_enabled ? "checked" : ""} /><span>纳入武器养成（第一版只规划等级，不含精炼）</span></label>
      <label>武器等级目标
        <select id="f-g-weapon-level">${growthLevelOptions(target.weapon_level_target || 90)}</select>
      </label>
      <div class="growth-form-row">
        <label>角色等级优先级<select id="f-g-p-level">${[1, 2, 3].map((n) => `<option value="${n}"${Number(target.level_priority || 1) === n ? " selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>武器优先级<select id="f-g-p-weapon">${[1, 2, 3].map((n) => `<option value="${n}"${Number(target.weapon_priority || 2) === n ? " selected" : ""}>${n}</option>`).join("")}</select></label>
        <label>天赋优先级<select id="f-g-p-talent">${[1, 2, 3].map((n) => `<option value="${n}"${Number(target.talent_priority || 3) === n ? " selected" : ""}>${n}</option>`).join("")}</select></label>
      </div>
      <div class="hint">优先级数字越小越先做（默认 角色等级 1 &gt; 武器等级 2 &gt; 天赋 3）。</div>
    </div>`;
}

async function editGrowthTarget(characterId) {
  const data = state.growth || {};
  const target = (data.targets || []).find((row) => Number(row.character_id) === Number(characterId)) || {};
  const planRow = ((data.plan || {}).characters || []).find((row) => Number(row.character_id) === Number(characterId)) || {};
  modal(`养成目标 · ${target.character_name || characterId}`, growthTargetForm(target, planRow.current), [
    { label: "保存", kind: "primary", action: () => saveGrowthTarget(characterId) },
    { label: "取消", kind: "ghost", action: closeModal },
  ]);
}

async function saveGrowthTarget(characterId) {
  const values = {
    level_target: Number(($("#f-g-level") || {}).value || 88),
    normal_target: Number(($("#f-g-normal") || {}).value || 1),
    skill_target: Number(($("#f-g-skill") || {}).value || 9),
    burst_target: Number(($("#f-g-burst") || {}).value || 9),
    weapon_enabled: Boolean($("#f-g-weapon") && $("#f-g-weapon").checked),
    weapon_level_target: Number(($("#f-g-weapon-level") || {}).value || 90),
    level_priority: Number(($("#f-g-p-level") || {}).value || 1),
    weapon_priority: Number(($("#f-g-p-weapon") || {}).value || 2),
    talent_priority: Number(($("#f-g-p-talent") || {}).value || 3),
  };
  const data = await api(`/api/growth/targets/${characterId}`, { method: "PUT", body: { target: values } });
  if (!data.ok) { toast(data.error || "保存失败", "error"); return; }
  closeModal();
  toast("已保存（下一轮规划会按新目标重算缺口）");
  await loadGrowth();
}

function growthBulkTargetForm() {
  return `
    <div class="growth-form">
      <div class="hint">这会覆盖当前所有角色的培养目标，但不会改变角色顺序、启用状态、拥有状态或武器选择。</div>
      <label>角色等级目标
        <select id="f-g-bulk-level">${growthLevelOptions(88)}</select>
      </label>
      <div class="growth-form-row">
        <label>普通攻击<select id="f-g-bulk-normal">${growthTalentOptions(1)}</select></label>
        <label>元素战技<select id="f-g-bulk-skill">${growthTalentOptions(9)}</select></label>
        <label>元素爆发<select id="f-g-bulk-burst">${growthTalentOptions(9)}</select></label>
      </div>
      <label class="switch"><input type="checkbox" id="f-g-bulk-weapon" checked />
        <span>纳入武器养成</span>
      </label>
      <label>武器等级目标
        <select id="f-g-bulk-weapon-level">${growthLevelOptions(90)}</select>
      </label>
      <div class="hint">目标低于当前等级、天赋或武器等级的角色会显示“目标已达成”。</div>
    </div>`;
}

function editAllGrowthTargets() {
  const count = ((state.growth && state.growth.targets) || []).length;
  if (!count) {
    toast("还没有角色培养目标", "warn");
    return;
  }
  modal("批量修改全部角色目标", growthBulkTargetForm(), [
    { label: "保存全部", kind: "primary", action: saveAllGrowthTargets },
    { label: "取消", kind: "ghost", action: closeModal },
  ]);
}

async function saveAllGrowthTargets() {
  const count = ((state.growth && state.growth.targets) || []).length;
  const values = {
    level_target: Number(($("#f-g-bulk-level") || {}).value || 88),
    normal_target: Number(($("#f-g-bulk-normal") || {}).value || 1),
    skill_target: Number(($("#f-g-bulk-skill") || {}).value || 9),
    burst_target: Number(($("#f-g-bulk-burst") || {}).value || 9),
    weapon_enabled: Boolean($("#f-g-bulk-weapon") && $("#f-g-bulk-weapon").checked),
    weapon_level_target: Number(($("#f-g-bulk-weapon-level") || {}).value || 90),
  };
  if (!window.confirm(`确认把 ${count} 个角色的培养目标全部改为：\n\n`
      + `角色 ${values.level_target} · 天赋 ${values.normal_target}/${values.skill_target}/${values.burst_target}`
      + ` · 武器 ${values.weapon_enabled ? values.weapon_level_target : "不纳入"}？`)) return;
  const data = await api("/api/growth/targets/bulk-update", {
    method: "POST", body: { target: values },
  });
  if (!data.ok) {
    toast(data.error || "批量修改失败", "error");
    return;
  }
  closeModal();
  toast(`已修改 ${data.updated || count} 个角色，正在重新计算材料缺口`);
  await loadGrowth();
}

/** 添加角色：确认账号拥有状态后，点一个就建默认目标（88 / 1-9-9 / 武器 90）。 */
/** 「添加角色」：**两个视图** + 明确的「添加为」语义。
 *
 * 批量的「我的角色」接口已经下线，后端会用单角色状态接口扫描并缓存拥有关系。
 * 扫描暂时不完整时，仍然允许玩家按当前栏位语义添加；目标列表里也能手动修正。
 */
async function addGrowthTarget(mode, refresh) {
  state.growthPickMode = mode || state.growthPickMode || "owned";
  const endpoint = refresh ? "/api/growth/characters?refresh=1" : "/api/growth/characters";
  const button = $("#btn-growth-add");
  if (button && !refresh) { button.disabled = true; button.textContent = "读取中…"; }
  try {
    const data = await api(endpoint);
    if (!data.ok) { toast(data.error || "读不到角色列表", "error"); return; }
    // 缓存下来：切栏 / 搜索都在这份数据上做，不再重复请求
    data.owned_count = (data.characters || []).filter((row) => row.owned_known && row.owned).length;
    data.missing_count = (data.characters || []).filter((row) => row.owned_known && !row.owned).length;
    state.growthCharacters = data;
    renderGrowthPicker(data, "");
  } finally {
    if (button && !refresh) { button.disabled = false; button.textContent = "添加角色"; }
  }
}

function renderGrowthPicker(data, keyword) {
  const mode = state.growthPickMode || "owned";
  const existing = new Set(((state.growth && state.growth.targets) || []).map((row) => Number(row.character_id)));
  const text = String(keyword || "").trim();
  const wantedOwned = mode === "owned";
  const all = (data.characters || []).filter((row) =>
    (!text || String(row.name || "").includes(text) || String(row.character_id).includes(text))
    && row.owned_known && Boolean(row.owned) === wantedOwned);
  const rows = all.slice().sort((a, b) => {
    return Number(b.rarity || 0) - Number(a.rarity || 0)
      || Number(a.character_id) - Number(b.character_id);
  });
  const tabs = [
    ["owned", `已拥有${data.owned_count ? `（${data.owned_count}）` : ""}`],
    ["missing", `未拥有${data.missing_count ? `（${data.missing_count}）` : ""}`],
  ].map(([value, label]) =>
    `<button class="btn sm ${value === mode ? "primary" : "ghost"}" data-growth-pick-mode="${value}">${label}</button>`)
    .join(" ");
  const pendingRows = rows.filter((row) => !existing.has(Number(row.character_id)));
  const list = rows.slice(0, 200).map((row) => {
    const added = existing.has(Number(row.character_id));
    const talents = row.talents || {};
    const talentText = `${talents.normal || 0}/${talents.skill || 0}/${talents.burst || 0}`;
    const status = Number(row.level || 0) > 0
      ? `Lv ${row.level} · 天赋 ${talentText}${row.weapon && row.weapon.level ? ` · 武器 ${row.weapon.level}` : ""}`
      : "状态未读到";
    const known = row.owned_known
      ? `<span class="muted">· 已确认${row.owned ? "拥有" : "未拥有"}</span>`
      : `<span class="muted">· 未知</span>`;
    return `<button class="btn ghost sm growth-pick" data-growth-add="${row.character_id}"
      data-growth-name="${escapeHtml(row.name)}" data-growth-rarity="${row.rarity}"
      data-growth-owned="${mode === "owned" ? 1 : 0}" ${added ? "disabled" : ""}>
      <span class="growth-pick-main">${"★".repeat(Math.max(0, Math.min(5, Number(row.rarity || 0))))} ${escapeHtml(row.name)}
        ${known}${added ? "<span class=\"muted\"> · 已添加</span>" : ""}</span>
      <span class="growth-pick-sub">${escapeHtml(status)}</span>
    </button>`;
  }).join("");
  modal(`添加角色 · ${mode === "owned" ? "已拥有" : "未拥有"}`, `
    <div class="growth-form">
      <div class="row-actions tight" style="margin-bottom:8px">${tabs}
        <button class="btn primary sm" data-growth-bulk-add
          ${pendingRows.length ? "" : "disabled"}>
          一键添加本栏${pendingRows.length ? `（${pendingRows.length}）` : ""}
        </button>
        <button class="btn ghost sm" data-growth-pick-refresh>重新拉取拥有状态</button>
      </div>
      <div class="hint">在「${mode === "owned" ? "已拥有" : "未拥有"}」这一栏里点角色，就会按
        <b>${mode === "owned" ? "已拥有" : "未拥有"}</b> 添加。加错了不要紧 ——
        目标列表里点一下「已拥有 / 未拥有」就能改。</div>
      <label>搜索
        <input id="f-growth-pick-search" class="input" value="${escapeHtml(text)}"
               placeholder="输入角色名或 id" />
      </label>
      <div class="growth-picks">${list || `<div class="empty">没找到匹配的角色</div>`}</div>
      <div class="muted">${escapeHtml(data.note || "")}</div>
    </div>`,
    [{ label: "关闭", kind: "ghost", action: closeModal }]);
}

/** 切换"这个号有没有这个角色"（米游社问不到，只能玩家自己标）。
 *
 * 标了之后会记进数据库（`settings` 里的 `owned_characters_v1`），
 * 「添加角色」的两个列表以后就按它分类。
 */
async function flipGrowthOwned(characterId, owned) {
  const data = await api(`/api/growth/targets/${characterId}`, {
    method: "PUT", body: { owned: owned ? 1 : 0 },
  });
  if (!data.ok) { toast(data.error || "改不了", "error"); return; }
  toast(owned ? "已标为「已拥有」" : "已标为「未拥有」");
  await loadGrowth();
}

async function createGrowthTarget(characterId, name, rarity, owned) {
  const data = await api("/api/growth/targets", {
    method: "POST",
    body: { character_id: Number(characterId), character_name: name, rarity: Number(rarity || 0),
            owned: owned ? 1 : 0,
            target: { level_target: 88, normal_target: 1, skill_target: 9, burst_target: 9,
                      weapon_enabled: 1, weapon_level_target: 90 } },
  });
  if (!data.ok) { toast(data.error || "添加失败", "error"); return; }
  closeModal();
  toast(`已添加 ${name}（${owned ? "已拥有" : "未拥有"}；默认目标 88 / 1-9-9 / 武器 90）`);
  await loadGrowth();
}

async function createGrowthTargetsBulk(rows, owned, button) {
  if (!rows.length) {
    toast("本栏没有待添加的角色", "warn");
    return;
  }
  const label = owned ? "已拥有" : "未拥有";
  if (!confirm(`确认一次添加「${label}」栏里的 ${rows.length} 个角色？\n\n已添加的角色不会重复计算。`)) return;
  if (button) {
    button.disabled = true;
    button.textContent = "添加中…";
  }
  const data = await api("/api/growth/targets/bulk", {
    method: "POST",
    body: {
      characters: rows.map((row) => ({
        character_id: Number(row.character_id),
        name: row.name,
        rarity: Number(row.rarity || 0),
        element: row.element || "",
        level: Number(row.level || 0),
        owned: owned ? 1 : 0,
      })),
    },
  });
  if (!data.ok) {
    if (button) {
      button.disabled = false;
      button.textContent = `一键添加本栏（${rows.length}）`;
    }
    toast(data.error || "批量添加失败", "error");
    return;
  }
  closeModal();
  toast(`已批量添加 ${data.added || rows.length} 个角色（${label}）`);
  await loadGrowth();
}

/** 拖拽排序：把被拖的行放到目标行前面，然后整表提交顺序。 */
function growthDragOver(event) {
  const row = event.target && event.target.closest ? event.target.closest("[data-growth-id]") : null;
  if (!row || !state.growthDragging) return;
  event.preventDefault();
  if (String(row.dataset.growthId) === String(state.growthDragging)) return;

  const tbody = row.parentNode;
  const dragging = tbody.querySelector(`[data-growth-id="${state.growthDragging}"]`);
  if (!dragging) return;
  const rect = row.getBoundingClientRect ? row.getBoundingClientRect() : null;
  const after = rect ? (event.clientY || 0) > rect.top + rect.height / 2 : false;
  if (after) row.after(dragging); else row.before(dragging);
}

async function growthDrop() {
  if (!state.growthDragging) return;
  state.growthDragging = null;
  const table = $("#growth-targets");
  if (!table || !table.querySelectorAll) return;
  const order = Array.from(table.querySelectorAll("[data-growth-id]"))
    .map((row) => Number(row.dataset.growthId));
  if (!order.length) return;
  const data = await api("/api/growth/targets/reorder", { method: "POST", body: { order } });
  if (!data.ok) { toast(data.error || "排序保存失败", "error"); return; }
  toast("顺序已保存（已切到「自定义」排序）");
  await loadGrowth();
}

async function setGrowthTargetOrder(characterId, position) {
  const table = $("#growth-targets");
  if (!table || !table.querySelectorAll) return;
  const rows = Array.from(table.querySelectorAll("[data-growth-id]"));
  const order = rows.map((row) => Number(row.dataset.growthId)).filter(Boolean);
  const id = Number(characterId);
  if (!id || !order.includes(id)) return;
  const targetIndex = Math.max(0, Math.min(order.length - 1, Number(position || 1) - 1));
  const currentIndex = order.indexOf(id);
  if (currentIndex === targetIndex) return;
  order.splice(currentIndex, 1);
  order.splice(targetIndex, 0, id);
  const data = await api("/api/growth/targets/reorder", { method: "POST", body: { order } });
  if (!data.ok) { toast(data.error || "排序保存失败", "error"); return; }
  toast(`已移动到第 ${targetIndex + 1} 位`);
  await loadGrowth();
}

async function changeGrowthSortMode() {
  const select = $("#growth-sort-mode");
  if (!select) return;
  const data = await api("/api/growth/sort-mode", { method: "POST", body: { mode: select.value } });
  if (!data.ok) { toast(data.error || "设置失败", "error"); return; }
  toast(`排序模式：${select.options[select.selectedIndex] ? select.options[select.selectedIndex].textContent : select.value}`);
  await loadGrowth();
}

function setupGrowthPage() {
  const hideComplete = $("#growth-hide-complete-tasks");
  if (hideComplete) {
    hideComplete.checked = Boolean(state.growthHideCompleteTasks);
    hideComplete.addEventListener("change", () => {
      state.growthHideCompleteTasks = Boolean(hideComplete.checked);
      try {
        localStorage.setItem(GROWTH_HIDE_COMPLETE_TASKS_KEY,
          state.growthHideCompleteTasks ? "1" : "0");
      } catch {}
      if (state.growth && state.growth.plan) renderGrowthTasks(state.growth.plan);
    });
  }
  const sync = $("#btn-growth-sync");
  if (sync && sync.addEventListener) sync.addEventListener("click", syncGrowthInventory);
  const replan = $("#btn-growth-plan");
  if (replan && replan.addEventListener) replan.addEventListener("click", () => refreshGrowthPlan(true));
  const refresh = $("#btn-growth-refresh");
  if (refresh && refresh.addEventListener) refresh.addEventListener("click", () => loadGrowth());
  const execute = $("#btn-growth-execute");
  if (execute && execute.addEventListener) execute.addEventListener("click", executeGrowthPlan);
  const bulkEdit = $("#btn-growth-bulk-edit");
  if (bulkEdit && bulkEdit.addEventListener) bulkEdit.addEventListener("click", editAllGrowthTargets);
  const clearTargets = $("#btn-growth-clear");
  if (clearTargets && clearTargets.addEventListener) clearTargets.addEventListener("click", clearGrowthTargets);
  const add = $("#btn-growth-add");
  if (add && add.addEventListener) add.addEventListener("click", addGrowthTarget);
  const sortMode = $("#growth-sort-mode");
  if (sortMode && sortMode.addEventListener) sortMode.addEventListener("change", changeGrowthSortMode);
  // 当前体力那一行是**动态渲染**的（在库存卡里），所以用委托绑定
  const inventory = $("#growth-inventory");
  if (inventory && inventory.addEventListener) {
    inventory.addEventListener("click", (event) => {
      const target = event.target && event.target.closest ? event.target : null;
      const refreshResin = target ? target.closest("[data-growth-resin-refresh]") : null;
      if (refreshResin) { refreshGrowthResin(refreshResin); return; }
      const save = target ? target.closest("[data-growth-resin-save]") : null;
      if (save) saveGrowthResin();
    });
    inventory.addEventListener("keydown", (event) => {
      if (event.target && event.target.id === "f-growth-resin" && event.key === "Enter") {
        event.preventDefault();
        saveGrowthResin();
      }
    });
  }

  const table = $("#growth-targets");
  if (table && table.addEventListener) {
    table.addEventListener("click", (event) => {
      const owned = event.target && event.target.closest ? event.target.closest("[data-growth-owned]") : null;
      if (owned) {
        flipGrowthOwned(owned.dataset.growthOwned,
          owned.dataset.growthOwnedNow !== "1");
        return;
      }
      const target = event.target && event.target.closest ? event.target.closest(
        "[data-growth-refresh-one], [data-growth-edit], [data-growth-delete], [data-growth-toggle]") : null;
      if (!target) return;
      if (target.dataset.growthRefreshOne) {
        refreshGrowthTargetRequirements(target.dataset.growthRefreshOne, target);
      } else if (target.dataset.growthEdit) editGrowthTarget(target.dataset.growthEdit);
      else if (target.dataset.growthDelete) deleteGrowthTarget(target.dataset.growthDelete);
    });
    table.addEventListener("change", (event) => {
      const box = event.target;
      if (box && box.dataset && box.dataset.growthToggle) {
        toggleGrowthTarget(box.dataset.growthToggle, box.checked);
      } else if (box && box.dataset && box.dataset.growthOrder) {
        setGrowthTargetOrder(box.dataset.growthOrder, box.value);
      }
    });
    table.addEventListener("keydown", (event) => {
      const box = event.target;
      if (box && box.dataset && box.dataset.growthOrder && event.key === "Enter") {
        event.preventDefault();
        setGrowthTargetOrder(box.dataset.growthOrder, box.value);
      }
    });
    table.addEventListener("dragstart", (event) => {
      const row = event.target && event.target.closest ? event.target.closest("[data-growth-id]") : null;
      if (!row) return;
      state.growthDragging = row.dataset.growthId;
      if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
    });
    table.addEventListener("dragover", growthDragOver);
    table.addEventListener("drop", (event) => { event.preventDefault(); growthDrop(); });
    table.addEventListener("dragend", () => { state.growthDragging = null; });
  }

  const modalBody = $("#modal-body");
  if (modalBody && modalBody.addEventListener) {
    modalBody.addEventListener("click", (event) => {
      const refresh = event.target && event.target.closest
        ? event.target.closest("[data-growth-pick-refresh]") : null;
      if (refresh) {
        refresh.disabled = true;
        refresh.textContent = "拉取中…";
        addGrowthTarget(state.growthPickMode, true);
        return;
      }
      const bulk = event.target && event.target.closest
        ? event.target.closest("[data-growth-bulk-add]") : null;
      if (bulk && !bulk.disabled) {
        const mode = state.growthPickMode || "owned";
        const existing = new Set(((state.growth && state.growth.targets) || [])
          .map((row) => Number(row.character_id)));
        const search = $("#f-growth-pick-search");
        const text = String(search ? search.value : "").trim();
        const rows = (state.growthCharacters && state.growthCharacters.characters || [])
          .filter((row) =>
            (!text || String(row.name || "").includes(text)
              || String(row.character_id).includes(text))
            && row.owned_known && Boolean(row.owned) === (mode === "owned")
            && !existing.has(Number(row.character_id)));
        createGrowthTargetsBulk(rows, mode === "owned", bulk);
        return;
      }
      const tab = event.target && event.target.closest ? event.target.closest("[data-growth-pick-mode]") : null;
      if (tab) {
        state.growthPickMode = tab.dataset.growthPickMode;
        const search = $("#f-growth-pick-search");
        renderGrowthPicker(state.growthCharacters || { ok: true, characters: [] },
          search ? search.value : "");
        return;
      }
      const pick = event.target && event.target.closest ? event.target.closest("[data-growth-add]") : null;
      if (!pick || pick.disabled) return;
      createGrowthTarget(pick.dataset.growthAdd, pick.dataset.growthName,
        pick.dataset.growthRarity, pick.dataset.growthOwned === "1");
    });
    modalBody.addEventListener("input", (event) => {
      const box = event.target;
      if (box && box.id === "f-growth-pick-search") {
        renderGrowthPicker(state.growthCharacters || { ok: true, characters: [] }, box.value);
      }
    });
  }
}

/* ---------------- 任务与路线 ---------------- */
async function loadRoutes() {
  const data = await api("/api/routes");
  if (!data.ok) return;
  $("#route-cards").innerHTML = data.categories.map((row) => statCard({
    label: row.label,
    value: `${row.count} 条`,
    sub: `${row.prefix || "路线库"}目录${row.note ? ` · ${row.note}` : ""} · ${row.group_exists ? `组「${row.group}」已存在` : `还没有组（执行时会自动生成 ${row.group}）`}`,
    icon: "map",
    tone: row.group_exists ? "ok" : "",
  })).join("");

  const rows = data.groups.map((group) => {
    const strategyTag = group.strategy
      ? (group.strategy_ok
        ? `<span class="tag ok">${escapeHtml(group.strategy)}</span>`
        : `<span class="tag warn"><span class="status-icon">${ICONS.pulse}</span>${escapeHtml(group.strategy)} 找不到 txt</span>`)
      : `<span class="tag dim">未配置</span>`;
    const registered = group.registered ? `<span class="tag ok">已登记</span>` : `<span class="tag warn">未登记</span>`;
    const enabled = group.enabled ? `<span class="tag ok">本轮勾选</span>` : `<span class="tag dim">未勾选</span>`;
    return `<tr>
      <td>${escapeHtml(group.name)}</td>
      <td>${group.pathing}</td>
      <td>${group.projects}</td>
      <td>${strategyTag}</td>
      <td>${registered} ${enabled}</td>
    </tr>`;
  }).join("");

  $("#routes-table").innerHTML = `<thead><tr>
      <th>脚本组</th><th>路线数</th><th>项目数</th><th>战斗策略</th><th>一条龙状态</th>
    </tr></thead><tbody>${rows}</tbody>`;
  $("#routes-hint").textContent = `共 ${data.groups.length} 个组；未登记的组写进配置也不会被执行`;
}

/* ---------------- 配置 ---------------- */
async function loadConfig() {
  const data = await api("/api/config");
  if (!data.ok) return;
  state.config = data;
  state.pending = {};   // 重新载入 = 丢掉未保存的改动
  $("#config-path").textContent = `${data.path}${data.exists ? "" : "（还不存在，保存时会创建）"}`;
  $("#config-tabs").innerHTML = data.groups.map((group, index) => (
    `<button class="tab ${index === state.configGroup ? "active" : ""}" data-group="${index}">${escapeHtml(group.title)}</button>`
  )).join("");
  renderConfigForm();
}

/** 把当前页面上的输入收进待保存集合（切页前调用，避免改动丢失）。 */
function collectPending() {
  state.pending = state.pending || {};
  $$("#config-form [data-key]").forEach((el) => { state.pending[el.dataset.key] = el.value; });
  markDirty();
}

function markDirty() {
  const count = Object.keys(state.pending || {}).length;
  $("#btn-config-save").textContent = count ? `保存（${count} 项改动）` : "保存";
  $("#btn-config-save").classList.toggle("primary", Boolean(count));
}

function renderConfigForm() {
  const group = state.config?.groups?.[state.configGroup];
  if (!group) return;
  const showSecret = $("#show-secret").checked;
  const pending = state.pending || {};
  $("#config-form").innerHTML = group.fields.map((field) => {
    const id = `f-${field.key}`;
    const value = Object.prototype.hasOwnProperty.call(pending, field.key) ? pending[field.key] : field.value;
    let control;
    if (field.kind === "bool") {
      control = `<select id="${id}" data-key="${field.key}" class="select">
        <option value="1"${value === "1" ? " selected" : ""}>1（开）</option>
        <option value="0"${value !== "1" ? " selected" : ""}>0（关）</option>
      </select>`;
    } else if (field.kind === "choice") {
      const options = field.choices.map((choice) => (
        `<option value="${escapeHtml(choice)}"${value === choice ? " selected" : ""}>${escapeHtml(PROVIDER_LABELS[choice] || choice)}</option>`
      )).join("");
      const extra = field.choices.includes(value) ? "" : `<option value="${escapeHtml(value)}" selected>${escapeHtml(value)}（自定义）</option>`;
      control = `<select id="${id}" data-key="${field.key}" class="select">${options}${extra}</select>`;
    } else {
      const type = field.kind === "secret" && !showSecret ? "password" : "text";
      control = `<input id="${id}" data-key="${field.key}" type="${type}" class="input" value="${escapeHtml(value)}" spellcheck="false" />`;
    }
    const extraHint = field.kind === "path"
      ? `<div class="field-help">把完整路径粘进来即可（例如 C:\\Program Files\\BetterGI）</div>`
      : "";
    return `<div class="field">
      <div class="field-label"><b>${escapeHtml(field.label)}</b><span>${escapeHtml(field.key)}</span></div>
      <div class="field-input">
        <div class="row">${control}</div>
        ${field.help ? `<div class="field-help">${escapeHtml(field.help)}</div>` : extraHint}
      </div>
    </div>`;
  }).join("");
  const provider = $("#f-LLM_PROVIDER");
  if (provider) provider.addEventListener("change", applyProviderPreset);
}

function applyProviderPreset(event) {
  const preset = PROVIDER_PRESETS[event.target.value];
  if (!preset) return;
  const url = $("#f-OPENAI_BASE_URL");
  const model = $("#f-MODEL_NAME");
  if (url) url.value = preset.url;
  if (model && (!model.value || Object.values(PROVIDER_PRESETS).some((item) => item.model === model.value))) {
    model.value = preset.model;
  }
  collectPending();
}

async function saveConfig() {
  collectPending();
  const values = state.pending || {};
  if (!Object.keys(values).length) { toast("没有改动需要保存", "warn"); return; }
  const data = await api("/api/config", { method: "POST", body: { values } });
  if (!data.ok) { toast(data.error || "保存失败", "error"); return; }
  toast(`已保存 ${data.saved ? data.saved.length : 0} 项` + (data.backup ? `，备份 ${data.backup.split("\\").pop()}` : ""));

  const warnings = data.warnings || [];
  if (warnings.length) {
    modal("保存成功，但有告警", `<div class="doctor">${warnings.map((w) => `<div class="doctor-row warn"><span class="status-icon">${ICONS.pulse}</span><span>${escapeHtml(w)}</span></div>`).join("")}</div>`, [
      { label: "知道了", kind: "primary", action: closeModal },
    ]);
  }
  await loadConfig();
  toast("改完需要「重新启动」Agent 才生效", "warn", 6500);
}

/* ---------------- BetterGI 日志 ---------------- */
async function loadBgiLog(dashboardOnly = false) {
  const file = dashboardOnly ? "" : ($("#log-file").value || "");
  const keyword = dashboardOnly ? "" : ($("#log-keyword").value || "");
  const data = await api(`/api/bettergi-log?file=${encodeURIComponent(file)}&keyword=${encodeURIComponent(keyword)}&lines=300`);
  if (!data.ok) return;
  state.logFiles = data.files || [];

  if (!dashboardOnly) {
    $("#log-file").innerHTML = state.logFiles.map((row) => (
      `<option value="${escapeHtml(row.name)}"${row.name === data.selected ? " selected" : ""}>${escapeHtml(row.name)} · ${bytes(row.size)}</option>`
    )).join("");
  }

  const highlight = data.highlight;
  const hlHtml = highlight
    ? `<div class="hl ${highlight.level || ""}"><span class="status-icon">${highlight.level === "error" ? ICONS.close : ICONS.pulse}</span><span>第 ${highlight.n} 行：${escapeHtml(highlight.note || highlight.text)}</span></div>`
    : `<div class="hl ok"><span class="status-icon">${ICONS.check}</span><span>最近的日志里没发现已知问题</span></div>`;
  $("#bgi-highlight").innerHTML = hlHtml;
  if (dashboardOnly) $("#dash-highlight").innerHTML = hlHtml;

  const rowsHtml = (data.rows || []).map((row) => {
    const cls = row.level ? `l-${row.level}` : "l-dim";
    return `<span class="${cls}"><span class="l-n">${row.n}</span>${escapeHtml(row.text)}</span>\n`;
  }).join("");
  const target = dashboardOnly ? $("#dash-bgi-log") : $("#bgi-console");
  target.innerHTML = rowsHtml || `<span class="l-dim">（这个日志文件是空的，或关键字没匹配到）</span>`;
  if (!dashboardOnly) target.scrollTop = target.scrollHeight;
}

/* ---------------- 回滚 ---------------- */
async function rollbackDialog() {
  const data = await api("/api/transactions");
  if (!data.ok) { toast(data.error || "读不到事务", "error"); return; }
  if (!data.items?.length) { toast("还没有可回滚的事务", "warn"); return; }

  const body = `<div class="muted" style="margin-bottom:10px">备份目录：${escapeHtml(data.backup_dir)}</div>` +
    data.items.map((item) => `<div class="tx" data-id="${escapeHtml(item.id)}">
      ${escapeHtml(item.id)} · ${escapeHtml(item.status)} · ${item.updates} 个文件 · ${escapeHtml(item.created_at)}
    </div>`).join("");
  modal("回滚 BetterGI 配置", body, [
    { label: "取消", kind: "ghost", action: closeModal },
    { label: "恢复选中事务", kind: "danger", action: async () => {
        const selected = $(".modal-body .tx.selected");
        if (!selected) { toast("先选一条事务", "warn"); return; }
        if (!confirm("确认把该事务执行前的 BetterGI 配置恢复回来？\n\n建议先关掉 BetterGI。")) return;
        const result = await api("/api/rollback", { method: "POST", body: { id: selected.dataset.id } });
        closeModal();
        toast(result.ok ? `已恢复：${result.restored.join("、")}` : (result.error || "回滚失败"), result.ok ? "ok" : "error", 8000);
      } },
  ]);
  $$(".modal-body .tx").forEach((el) => el.addEventListener("click", () => {
    $$(".modal-body .tx").forEach((other) => other.classList.remove("selected"));
    el.classList.add("selected");
  }));
}

/* ---------------- 弹窗 ---------------- */
function modal(title, bodyHtml, buttons) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = bodyHtml;
  $("#modal-foot").innerHTML = "";
  (buttons || []).forEach((button) => {
    const el = document.createElement("button");
    el.className = `btn ${button.kind || ""}`;
    el.textContent = button.label;
    el.addEventListener("click", button.action);
    $("#modal-foot").appendChild(el);
  });
  $("#modal-mask").classList.remove("hidden");
}

function closeModal() { $("#modal-mask").classList.add("hidden"); }

/* ---------------- 主题 ---------------- */
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("#btn-theme").innerHTML = theme === "dark" ? ICONS.moon : ICONS.spark;
  try { localStorage.setItem("gi-agent-theme", theme); } catch {}
}

/* ---------------- 事件绑定 ---------------- */
function bindEvents() {
  $("#nav").addEventListener("click", (event) => {
    const item = event.target.closest(".nav-item");
    if (item) switchPage(item.dataset.page);
  });

  setupCooldownPage();
  setupGrowthPage();

  $("#btn-start").addEventListener("click", startAgent);
  $("#btn-stop").addEventListener("click", stopAgent);
  $("#btn-send").addEventListener("click", () => sendInput());
  $("#input-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); sendInput(); }
  });

  $("#btn-theme").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });

  $("#btn-clear-log").addEventListener("click", async () => {
    await api("/api/log/clear", { method: "POST" });
    $("#console").innerHTML = ""; state.seq = 0; partialEl = null;
  });
  $("#autoscroll").addEventListener("change", (event) => { state.autoscroll = event.target.checked; });

  document.body.addEventListener("click", (event) => {
    const sendEl = event.target.closest("[data-send]");
    if (sendEl) { sendInput(sendEl.dataset.send); return; }
    const chanStart = event.target.closest("[data-chan-start]");
    if (chanStart) { startChannel(chanStart.dataset.chanStart); return; }
    const chanStop = event.target.closest("[data-chan-stop]");
    if (chanStop) { stopChannel(chanStop.dataset.chanStop); return; }
    const chanClear = event.target.closest("[data-chan-clear]");
    if (chanClear) { clearChannelLog(chanClear.dataset.chanClear); return; }
    const goto = event.target.closest("[data-goto]");
    if (goto) { switchPage(goto.dataset.goto); return; }
    const actEl = event.target.closest("[data-act]");
    if (actEl) {
      const act = actEl.dataset.act;
      if (act === "refresh-env") refreshEnv();
      if (act === "doctor") { switchPage("doctor"); loadDoctor(); }
      if (act === "open") openTarget(actEl.dataset.target);
      if (act === "rollback") rollbackDialog();
      return;
    }
    const tabEl = event.target.closest("#config-tabs .tab");
    if (tabEl) {
      collectPending();                       // 切页前先收好当前页的修改
      state.configGroup = Number(tabEl.dataset.group);
      $$("#config-tabs .tab").forEach((tab) => tab.classList.toggle("active", tab === tabEl));
      renderConfigForm();
    }
  });

  $("#btn-config-save").addEventListener("click", saveConfig);
  $("#btn-config-reload").addEventListener("click", loadConfig);
  $("#show-secret").addEventListener("change", renderConfigForm);
  const mysCheck = $("#btn-mys-check");
  if (mysCheck) mysCheck.addEventListener("click", checkMysCookie);
  const mysScan = $("#btn-mys-scan");
  if (mysScan) mysScan.addEventListener("click", () => startMysScan("app"));
  const mysScanWeb = $("#btn-mys-scan-web");
  if (mysScanWeb) mysScanWeb.addEventListener("click", () => startMysScan("web"));
  const mysScanRefresh = $("#btn-mys-scan-refresh");
  if (mysScanRefresh) mysScanRefresh.addEventListener("click", () => startMysScan(mysScanKind));
  const mysScanCancel = $("#btn-mys-scan-cancel");
  if (mysScanCancel) mysScanCancel.addEventListener("click", cancelMysScan);
  const mysScanManual = $("#btn-mys-scan-manual");
  if (mysScanManual) mysScanManual.addEventListener("click", promptMysManual);
  const mysRefresh = $("#btn-mys-refresh");
  if (mysRefresh) mysRefresh.addEventListener("click", refreshMysSnapshot);

  document.body.addEventListener("change", (event) => {
    const autoEl = event.target.closest("[data-chan-auto]");
    if (autoEl) { toggleChannelAuto(autoEl.dataset.chanAuto, autoEl.checked); }
  });

  $("#btn-doctor").addEventListener("click", () => loadDoctor());
  $("#btn-guide-reload").addEventListener("click", () => loadGuide());
  $("#btn-guide-repair").addEventListener("click", async () => {
    const data = await api("/api/repair", { method: "POST" });
    const lines = (data.lines || []).map((line) => `<div class="doctor-row ${data.ok ? "ok" : "warn"}"><span class="status-icon">${ICONS.spark}</span><span>${escapeHtml(line)}</span></div>`).join("");
    modal("一键修复结果", `<div class="doctor">${lines || `<div class="doctor-row warn"><span class="status-icon">${ICONS.pulse}</span><span>${escapeHtml(data.error || "没有任何输出")}</span></div>`}</div>`, [
      { label: "知道了", kind: "primary", action: closeModal },
    ]);
    if (data.ok) toast("修复完成");
    await loadGuide();
  });
  $("#btn-bgi-log").addEventListener("click", () => loadBgiLog());
  $("#log-file").addEventListener("change", () => loadBgiLog());
  $("#log-keyword").addEventListener("keydown", (event) => { if (event.key === "Enter") loadBgiLog(); });
  $("#dash-log-refresh").addEventListener("click", () => loadBgiLog(true));
  setupUpdatePage();

  $("#modal-close").addEventListener("click", closeModal);
  $("#modal-mask").addEventListener("click", (event) => { if (event.target.id === "modal-mask") closeModal(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!$("#modal-mask").classList.contains("hidden")) closeModal();
      else if (state.page === "run" && state.agent.state === "waiting") sendInput("exit");
      return;
    }
    if (event.target.matches("input, textarea, select, button")) return;
    if (state.page === "run" && state.agent.state === "waiting" && event.key.toLowerCase() === "y") sendInput("y");
    if (state.page === "run" && state.agent.state === "waiting" && event.key.toLowerCase() === "t") sendInput("t");
  });

  $("#btn-quit").addEventListener("click", async () => {
    if (!confirm("退出 Studio？\n\n· 会先停掉 Agent 子进程；\n· 不会关闭已经在跑的 BetterGI。")) return;
    await api("/api/shutdown", { method: "POST" });
    document.body.innerHTML = `<div style="display:grid;place-items:center;height:100vh;font-family:system-ui;color:#888">
      Studio 已退出，可以直接关掉这个窗口了。</div>`;
    setTimeout(() => window.close(), 400);
  });
}

/* ---------------- 原生窗口按钮（无系统标题栏时的最小化/最大化/关闭） ---------------- */
function hostBridge() {
  return window.chrome && window.chrome.webview ? window.chrome.webview : null;
}

function bindWindowControls() {
  const bridge = hostBridge();
  if (!bridge) return;   // 浏览器模式：隐藏这组按钮，用浏览器自己的标题栏

  document.body.classList.add("native-host");
  $("#win-controls").classList.remove("hidden");

  const post = (action, extra) => bridge.postMessage(JSON.stringify(
    Object.assign({ type: "window", action }, extra || {})
  ));
  $("#win-min").addEventListener("click", () => post("minimize"));
  $("#win-max").addEventListener("click", () => post("maximize"));
  $("#win-close").addEventListener("click", () => post("close"));

  // 双击顶栏空白处 = 最大化/还原（Windows 习惯）
  const topbar = $(".topbar");
  topbar.addEventListener("dblclick", (event) => {
    if (isInteractive(event.target)) return;
    post("maximize");
  });

  // 拖动顶栏移动窗口：WebView2 的 app-region 在自绘窗口里实测拖不动，
  // 所以由界面上报屏幕坐标 + 缩放比，宿主按增量搬窗口。
  let dragging = false;
  const scale = () => window.devicePixelRatio || 1;
  topbar.addEventListener("mousedown", (event) => {
    if (event.button !== 0 || isInteractive(event.target)) return;
    dragging = true;
    post("drag", { phase: "start", x: event.screenX, y: event.screenY, scale: scale() });
    event.preventDefault();
  });
  window.addEventListener("mousemove", (event) => {
    if (!dragging) return;
    post("drag", { phase: "move", x: event.screenX, y: event.screenY, scale: scale() });
  });
  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    post("drag", { phase: "end" });
  });

  // 自检钩子：宿主带 --selftest 启动时会调用它，用来验证"界面 → 宿主拖动窗口"这条链路
  // （真实鼠标注入在受限环境里会被 UIPI 拦掉，所以留一个可编程的探针）。
  window.__giDragProbe = (dx, dy) => {
    post("drag", { phase: "start", x: 0, y: 0, scale: 1 });
    post("drag", { phase: "move", x: dx, y: dy, scale: 1 });
    post("drag", { phase: "end" });
    return "drag-probe-sent";
  };
  window.__giWindowProbe = (action) => {
    post(action);
    return "window-probe-sent:" + action;
  };
}

function isInteractive(target) {
  return Boolean(target.closest("button, input, select, textarea, a, .win-controls, .pill"));
}

/* ---------------- 启动 ---------------- */
async function tick() {
  const data = await api("/api/state");
  if (data.ok) applyState(data);
}

async function boot() {
  let theme = "dark";
  try { theme = localStorage.getItem("gi-agent-theme") || "dark"; } catch {}
  applyTheme(theme);
  renderIcons();
  bindEvents();
  bindWindowControls();
  switchPage("run");
  $("#about-kv").innerHTML = `
    <div><b>项目目录</b><span id="about-root">—</span></div>
    <div><b>界面</b><span>${hostBridge() ? "原生窗口（WinForms + WebView2 控件）" : "浏览器窗口（WebView2 / Edge）"}</span></div>
    <div><b>服务端</b><span>Flask（项目自带依赖）</span></div>`;
  await tick();
  $("#about-root").textContent = state.env.project_root || "—";
  await pollLog();
  await pollChannels();
  // 版本检查走缓存（不 force）：只为早点把侧边栏那个"有新版本"的红点点亮
  loadUpdate();
  setTimeout(() => {
    if (state.page === "dashboard") loadBgiLog(true);
  }, 0);

  setInterval(tick, 1200);
  setInterval(pollLog, 900);
  // 通道日志只在看得见的时候拉（省得后台一直拉）；状态摘要走 /api/state 的 1.2 秒轮询
  setInterval(() => { if (state.page === "channels") pollChannels(); }, 1200);
  setInterval(() => { if (state.page === "dashboard") loadBgiLog(true); }, 20000);
}

boot();
