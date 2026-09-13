/* 临时：在 node 里用假 DOM 跑一遍 studio/web/app.js —— 验证新加的「远程通道」渲染逻辑真的能跑。
   跑完删除（同类检查会被固化成 tests/test_web_ui_smoke.py）。 */

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const WEB = path.join(__dirname, "..", "studio", "web");
const source = fs.readFileSync(path.join(WEB, "app.js"), "utf8");

const captured = {};

function makeEl(id = "") {
  const el = {
    id,
    innerHTML: "",
    textContent: "",
    className: "",
    value: "",
    checked: false,
    disabled: false,
    scrollTop: 0,
    scrollHeight: 100,
    clientHeight: 50,
    dataset: {},
    style: {},
    childNodes: [],
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener() {},
    appendChild(child) { this.childNodes.push(child); return child; },
    removeChild(child) { this.childNodes = this.childNodes.filter((c) => c !== child); },
    remove() {},
    closest() { return null; },
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
    setAttribute() {},
    focus() {},
    matches() { return false; },
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return captured[el.id] ?? ""; },
    set(value) { captured[el.id] = value; },
  });
  return el;
}

const elements = new Map();
const document = {
  querySelector(sel) {
    const key = sel.replace(/^#/, "");
    if (!elements.has(key)) elements.set(key, makeEl(key));
    return elements.get(key);
  },
  querySelectorAll() { return []; },
  createElement() { return makeEl(); },
  createDocumentFragment() {
    const frag = makeEl();
    frag.appendChild = function (child) { return child; };
    return frag;
  },
  addEventListener() {},
  body: makeEl("body"),
  documentElement: makeEl("html"),
};

const CHANNELS = {
  ok: true,
  channels: [
    {
      ok: true, name: "qq", label: "QQ 机器人", state: "running", pid: 4242,
      seq: 2, lines: [{ i: 1, text: "🤖 QQ 机器人已启动\n", level: "agent" }],
      configured: true, missing: [], auto_start: true, auto_field: "AUTO_START_QQ_BOT",
      hint: "去配置页填", notes: "群里 @机器人 即可",
    },
    {
      ok: true, name: "feishu", label: "飞书服务端", state: "stopped", pid: null,
      seq: 0, lines: [],
      configured: false, missing: ["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
      auto_start: false, auto_field: "AUTO_START_FEISHU_BOT",
      hint: "去配置页填", notes: "",
    },
  ],
};

const COOLDOWN = {
  ok: true, hours: 48, min_route_percent: 80, band_enabled: true,
  scanned_days: 3, events: 42, generated_at: "23:40:00",
  summary: { total: 3, cooling: 1, ready: 2, partial: 1, manual: 1, with_routes: 3 },
  materials: [
    {
      material: "霜仙花", cooling: true, known: true, partial: false, manual: false,
      hours_left: 20.5, hours_left_text: "20 小时 30 分", hours_ago: 27.5,
      last_at: "09-13 12:34", last_ts: 1789197240, total_routes: 7, ran_routes: 7,
      describe: "⏳ 霜仙花：还没刷新，还要等约 20 小时 30 分",
    },
    {
      material: "慕风蘑菇", cooling: false, known: true, partial: true, manual: false,
      hours_left: 0, hours_left_text: "", hours_ago: 5.0,
      last_at: "09-14 03:00", last_ts: 1789249200, total_routes: 17, ran_routes: 2,
      describe: "✅ 慕风蘑菇：只采了 2/17 条路线，不算采完",
    },
    {
      material: "沙脂蛹", cooling: false, known: false, partial: false, manual: false,
      hours_left: 0, hours_left_text: "", hours_ago: null,
      last_at: "", last_ts: null, total_routes: 10, ran_routes: 0,
      describe: "✅ 沙脂蛹：没有采集记录（按已刷新处理，可以采）",
    },
  ],
};

const STATE = {
  ok: true, state: "stopped", pid: null, seq: 0, lines: [], partial: "",
  plan: { lines: ["⚔️ 体力目标：无"] }, project_root: "C:\\proj",
  provider: "deepseek", model: "deepseek-chat", uid: "1", bgi_running: false,
  routes: {}, channels: CHANNELS.channels,
};

async function fakeFetch(url) {
  let payload = STATE;
  if (url.startsWith("/api/channels")) payload = CHANNELS;
  if (url.startsWith("/api/log")) payload = { ok: true, seq: 0, lines: [], partial: "" };
  if (url.startsWith("/api/config")) payload = { ok: true, groups: [] };
  if (url.startsWith("/api/cooldown")) payload = COOLDOWN;
  return { ok: true, status: 200, text: async () => JSON.stringify(payload) };
}

const sandbox = {
  console,
  document,
  fetch: fakeFetch,
  localStorage: { getItem: () => "dark", setItem() {} },
  setTimeout: () => 0,
  setInterval: () => 0,
  clearTimeout() {},
  requestAnimationFrame: () => 0,
  confirm: () => true,
  alert() {},
  window: {},
  location: { href: "http://127.0.0.1/" },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

vm.createContext(sandbox);

(async () => {
  const settle = async (rounds = 4) => {
    for (let index = 0; index < rounds; index += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
  };
  try {
    vm.runInContext(source, sandbox, { filename: "app.js" });
    // boot() 是异步的：让 API 的 microtask 跑完
    await settle();
    // 再切到「采集冷却」页，验证这条链路（/api/cooldown → 汇总卡 + 表格）也能渲染
    sandbox.switchPage("cooldown");
    await settle();
  } catch (exc) {
    console.error("❌ 运行 app.js 抛异常：", exc && exc.stack ? exc.stack : exc);
    process.exit(2);
  }

  const channelsPage = captured["chan-cards"] || "";
  const dash = captured["dash-channels"] || "";
  const coolCards = captured["cooldown-cards"] || "";
  const coolTable = captured["cooldown-table"] || "";

  const checks = [
    ["通道卡片渲染出来了", channelsPage.includes('data-chan="qq"')],
    ["第二个通道也在", channelsPage.includes('data-chan="feishu"')],
    ["内嵌控制台容器存在", channelsPage.includes('id="chan-console-qq"')],
    ["运行中状态 + PID 显示", channelsPage.includes("运行中") && channelsPage.includes("4242")],
    ["未配置通道写明缺哪一项", channelsPage.includes("FEISHU_APP_ID")],
    ["自动启动勾选框", channelsPage.includes("data-chan-auto=\"qq\"")],
    ["概览摘要卡也有内容", dash.includes("QQ 机器人")],
    ["采集冷却：汇总卡渲染出来了", coolCards.includes("冷却中") && coolCards.includes("48 小时")],
    ["采集冷却：冷却中的材料带剩余时间", coolTable.includes("霜仙花") && coolTable.includes("还要 20 小时 30 分")],
    ["采集冷却：部分采集标注出来了", coolTable.includes("慕风蘑菇") && coolTable.includes("部分采集")],
    ["采集冷却：没有记录的按可以采显示", coolTable.includes("沙脂蛹") && coolTable.includes("可以采")],
    ["采集冷却：每行都有登记/清除按钮",
      coolTable.includes('data-cool-mark="霜仙花"') && coolTable.includes('data-cool-clear="霜仙花"')],
  ];

  let failed = 0;
  for (const [label, ok] of checks) {
    console.log(`${ok ? "✅" : "❌"} ${label}`);
    if (!ok) failed += 1;
  }
  if (!channelsPage) console.log("（chan-cards 是空的 —— 说明渲染没发生）");
  if (!coolTable) console.log("（cooldown-table 是空的 —— 采集冷却页没渲染出来）");
  process.exit(failed ? 1 : 0);
})();
