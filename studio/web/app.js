/* ============================================================
   GI Agent Studio · 前端逻辑（原生 JS，无构建步骤、无外网依赖）
   ============================================================ */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

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
};

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
  routes: ["任务与路线", "调度器脚本组、战斗策略与各类目路线"],
  channels: ["远程通道", "QQ 机器人 / 飞书服务端：内嵌启停与日志，可随 Agent 自动启动"],
  config: ["配置", "图形化编辑 .env（保留注释，自动备份）"],
  guide: ["使用说明", "一条龙怎么配、要加哪些调度器、本软件与 PowerShell 怎么配"],
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
  box.textContent = data.configured
    ? `已配置（${data.masked}）｜缓存 ${data.count} 个角色，更新于 ${data.age}｜名单缓存 TTL ${data.ttl_hours} 小时`
    : "未配置：展柜只有 8 个角色，填了 MYS_COOKIE 才能查展柜外的角色（步骤见 docs/MYS_COOKIE.md）";
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
  result.innerHTML = `${rows}<div class="hint">Cookie 可用：记得点上面的<b>保存</b>写进 .env，然后重启 Agent。</div>`;
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
