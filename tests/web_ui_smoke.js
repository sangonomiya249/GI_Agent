/* 在 node 里用假 DOM 跑一遍 studio/web/app.js —— 验证「远程通道」与「资源冷却」的渲染真的能跑。
   为什么要这样测：界面里 90% 的问题不是"接口错了"，而是"JS 渲染时炸了"（引用了不存在的元素、
   函数名写错、新分支没接上），这类问题 Python 测试看不见，而沙箱/CI 里 Edge 起不来 headless。
   对应的 Python 入口是 tests/test_web_ui_smoke.py（没装 node 会跳过）。 */

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
    _on: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    addEventListener(type, handler) { (this._on[type] = this._on[type] || []).push(handler); },
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

/* 四类资源的口径（特产 48 / 矿物 72 / 魔物 12 小时）—— 与 /api/cooldown 的真实返回结构一致 */
function material(overrides) {
  return Object.assign({
    category: "", category_label: "", hours: 48, cooling: false, known: false,
    partial: false, manual: false, hours_left: 0, hours_left_text: "",
    hours_ago: null, last_at: "", last_ts: null, total_routes: 0, ran_routes: 0,
    describe: "",
  }, overrides);
}

const COOLDOWN = {
  ok: true, hours: 48, min_route_percent: 80, band_enabled: true,
  scanned_days: 4, events: 42, generated_at: "23:40:00",
  category_hours: { specialty: 48, mine: 72, cook: 24, hunt: 12 },
  summary: { total: 4, cooling: 1, ready: 3, partial: 1, manual: 1, with_routes: 4 },
  sections: [
    {
      key: "specialty", label: "地区特产", hours: 48, note: "wiki：采集后 48 小时刷新",
      summary: { total: 2, cooling: 1, ready: 1, partial: 1, manual: 0 },
      materials: [
        material({
          material: "霜仙花", category: "specialty", category_label: "地区特产", hours: 48,
          cooling: true, known: true, hours_left: 20.5, hours_ago: 27.5,
          last_at: "09-13 12:34", last_ts: 1789197240, total_routes: 7, ran_routes: 7,
          describe: "⏳ 霜仙花：还没刷新，还要等约 20 小时 30 分",
        }),
        material({
          material: "慕风蘑菇", category: "specialty", category_label: "地区特产", hours: 48,
          known: true, partial: true, hours_ago: 5.0,
          last_at: "09-14 03:00", last_ts: 1789249200, total_routes: 17, ran_routes: 2,
          describe: "✅ 慕风蘑菇：只跑了 2/17 条路线，不算跑完",
        }),
      ],
    },
    {
      key: "mine", label: "矿物", hours: 72, note: "水晶块 / 紫晶块：上次刷新后的第三日",
      summary: { total: 1, cooling: 0, ready: 1, partial: 0, manual: 0 },
      materials: [
        material({
          material: "水晶块", category: "mine", category_label: "矿物", hours: 72,
          total_routes: 9, describe: "✅ 水晶块：没有记录（按已刷新处理，可以去）",
        }),
      ],
    },
    {
      key: "hunt", label: "敌人与魔物", hours: 12, note: "普通魔物 12 小时刷新",
      summary: { total: 1, cooling: 0, ready: 1, partial: 0, manual: 1 },
      materials: [
        material({
          material: "巡陆艇", category: "hunt", category_label: "敌人与魔物", hours: 12,
          known: true, manual: true, hours_ago: 20.0,
          last_at: "09-13 20:00", last_ts: 1789221600, total_routes: 7, ran_routes: 7,
          describe: "✅ 巡陆艇：已刷新（12 小时冷却已过），可以去",
        }),
      ],
    },
  ],
  materials: [],
};
COOLDOWN.materials = COOLDOWN.sections.flatMap((section) => section.materials);

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
    // 再切到「资源冷却」页，验证这条链路（/api/cooldown → 分类卡片 + 分区表格）也能渲染
    sandbox.switchPage("cooldown");
    await settle();
  } catch (exc) {
    console.error("❌ 运行 app.js 抛异常：", exc && exc.stack ? exc.stack : exc);
    process.exit(2);
  }

  const channelsPage = captured["chan-cards"] || "";
  const dash = captured["dash-channels"] || "";
  const coolTabs = captured["cooldown-tabs"] || "";
  const coolCards = captured["cooldown-cards"] || "";
  const coolTable = captured["cooldown-table"] || "";
  const coolFoot = captured["cooldown-foot"] || "";
  const titleEl = elements.get("cooldown-title");

  // 点一下类别 chip（app.js 用事件委托接的），验证"点哪个类别就显示哪个类别"
  function clickTab(key) {
    const tab = elements.get("cooldown-tabs");
    const handler = (tab._on.click || [])[0];
    if (!handler) return false;
    handler({ target: { closest: () => ({ dataset: { coolTab: key } }) } });
    return true;
  }

  const clicked = clickTab("hunt");
  const huntCards = captured["cooldown-cards"] || "";
  const huntTable = captured["cooldown-table"] || "";
  const huntTabs = captured["cooldown-tabs"] || "";
  const huntTitle = titleEl ? titleEl.textContent : "";
  clickTab("mine");
  const mineTable = captured["cooldown-table"] || "";
  const mineFoot = captured["cooldown-foot"] || "";
  clickTab("specialty");
  const backTable = captured["cooldown-table"] || "";

  const checks = [
    ["通道卡片渲染出来了", channelsPage.includes('data-chan="qq"')],
    ["第二个通道也在", channelsPage.includes('data-chan="feishu"')],
    ["内嵌控制台容器存在", channelsPage.includes('id="chan-console-qq"')],
    ["运行中状态 + PID 显示", channelsPage.includes("运行中") && channelsPage.includes("4242")],
    ["未配置通道写明缺哪一项", channelsPage.includes("FEISHU_APP_ID")],
    ["自动启动勾选框", channelsPage.includes("data-chan-auto=\"qq\"")],
    ["概览摘要卡也有内容", dash.includes("QQ 机器人")],
    ["冷却页：顶部有类别切换（特产/矿物/魔物）",
      coolTabs.includes('data-cool-tab="specialty"') && coolTabs.includes('data-cool-tab="mine"')
      && coolTabs.includes('data-cool-tab="hunt"')],
    ["冷却页：默认选中地区特产", coolTabs.includes('class="chip active" data-cool-tab="specialty"')],
    ["冷却页：类别 chip 带冷却中角标", coolTabs.includes("冷却 1")],
    ["冷却页：汇总卡只统计当前类别",
      coolCards.includes("地区特产 · 冷却中") && coolCards.includes("这一类共 2 项 · 刷新 48 小时")
      && !coolCards.includes("敌人与魔物 · 冷却中")],
    ["冷却页：表格只显示当前类别",
      coolTable.includes("霜仙花") && !coolTable.includes("水晶块") && !coolTable.includes("巡陆艇")],
    ["冷却页：冷却中的目标带剩余时间", coolTable.includes("还要 20 小时 30 分")],
    ["冷却页：部分完成标注出来了", coolTable.includes("慕风蘑菇") && coolTable.includes("部分完成")],
    ["冷却页：每行都有登记/清除按钮",
      coolTable.includes('data-cool-mark="霜仙花"') && coolTable.includes('data-cool-clear="霜仙花"')],
    ["冷却页：页脚写明清单来源（全量目录，不是上次跑的那些）",
      coolFoot.includes("路线仓库全量目录") && coolFoot.includes("这一类共 <b>2</b> 种")],
    ["点「敌人与魔物」→ 表格换成魔物的", clicked && huntTable.includes("巡陆艇") && !huntTable.includes("霜仙花")],
    ["点「敌人与魔物」→ 卡片/标题/chip 高亮都跟着换",
      huntCards.includes("敌人与魔物 · 冷却中") && huntTitle.includes("敌人与魔物")
      && huntTabs.includes('class="chip active" data-cool-tab="hunt"')],
    ["点「矿物」→ 显示矿物（页脚也跟着换）",
      mineTable.includes("水晶块") && !mineTable.includes("巡陆艇") && mineFoot.includes("这一类共 <b>1</b> 种")],
    ["点回「地区特产」→ 恢复特产的表", backTable.includes("霜仙花") && !backTable.includes("水晶块")],
  ];

  let failed = 0;
  for (const [label, ok] of checks) {
    console.log(`${ok ? "✅" : "❌"} ${label}`);
    if (!ok) failed += 1;
  }
  if (!channelsPage) console.log("（chan-cards 是空的 —— 说明渲染没发生）");
  if (!coolTable) console.log("（cooldown-table 是空的 —— 资源冷却页没渲染出来）");
  process.exit(failed ? 1 : 0);
})();
