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
  try {
    vm.runInContext(source, sandbox, { filename: "app.js" });
    // boot() 是异步的：让 API 的 microtask 跑完
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
  } catch (exc) {
    console.error("❌ 运行 app.js 抛异常：", exc && exc.stack ? exc.stack : exc);
    process.exit(2);
  }

  const channelsPage = captured["chan-cards"] || "";
  const dash = captured["dash-channels"] || "";

  const checks = [
    ["通道卡片渲染出来了", channelsPage.includes('data-chan="qq"')],
    ["第二个通道也在", channelsPage.includes('data-chan="feishu"')],
    ["内嵌控制台容器存在", channelsPage.includes('id="chan-console-qq"')],
    ["运行中状态 + PID 显示", channelsPage.includes("运行中") && channelsPage.includes("4242")],
    ["未配置通道写明缺哪一项", channelsPage.includes("FEISHU_APP_ID")],
    ["自动启动勾选框", channelsPage.includes("data-chan-auto=\"qq\"")],
    ["概览摘要卡也有内容", dash.includes("QQ 机器人")],
  ];

  let failed = 0;
  for (const [label, ok] of checks) {
    console.log(`${ok ? "✅" : "❌"} ${label}`);
    if (!ok) failed += 1;
  }
  if (!channelsPage) console.log("（chan-cards 是空的 —— 说明渲染没发生）");
  process.exit(failed ? 1 : 0);
})();
