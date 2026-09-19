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
  const classes = new Set();
  const el = {
    id,
    innerHTML: "",
    textContent: "",
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
    // classList 用真 Set 实现：页面切换、侧边栏红点都靠它，假的会让断言看不出问题
    classList: {
      add: (name) => classes.add(name),
      remove: (name) => classes.delete(name),
      toggle: (name, force) => {
        const want = force === undefined ? !classes.has(name) : Boolean(force);
        if (want) classes.add(name); else classes.delete(name);
        return want;
      },
      contains: (name) => classes.has(name),
    },
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
  Object.defineProperty(el, "className", {
    get() { return [...classes].join(" "); },
    set(value) {
      classes.clear();
      String(value || "").split(/\s+/).filter(Boolean).forEach((name) => classes.add(name));
    },
  });
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

const UPDATE = {
  ok: true, check_ok: true, status: "newer", update_available: true,
  local_version: "1.0.0", local_source: "VERSION",
  latest_version: "v1.5.0", latest_name: "修了一堆坑",
  latest_url: "https://github.com/sangonomiya249/GI_Agent/releases/tag/v1.5.0",
  published_at: "2026-09-15T10:00:00Z", notes: "· 修了 A\n· 修了 B",
  prerelease: false, headline: "有新版本 v1.5.0（本地 1.0.0）",
  detail: "GitHub 上是 v1.5.0，本地是 1.0.0", error: "",
  checked_at: "2026-09-15 20:00:00", from_cache: false, cache_age_hours: null,
  repo: "sangonomiya249/GI_Agent", update_hint: "在项目目录里执行 git pull",
  via: "系统 / 环境变量代理 + 证书 win-ca-bundle.pem",
};

/* 角色养成页的假响应：形状与 POST /api/growth 一致（plan + targets + 库存状态 + 历史）。
   重点是那几条"界面必须表达清楚"的事实：库存非实时、任务为什么跑不了、缺口多大。 */
const GROWTH = {
  ok: true,
  plan: {
    generated_at: "2026-09-18T14:03:00", status: "WAIT_CONFIRM", status_label: "等待确认",
    sort_mode: "default",
    current_character_id: 10000089, current_character_name: "胡桃", current_phase: "character_level",
    current_phase_label: "角色等级",
    characters: [
      {
        character_id: 10000089, character_name: "胡桃", rarity: 5, enabled: true, priority: 1,
        complete: false, current_summary: "80/8/8/8", target_summary: "90/10/10/10",
        missing_total: 180, compute_error: "",
      },
      {
        character_id: 10000032, character_name: "班尼特", rarity: 4, enabled: true, priority: 2,
        complete: false, current_summary: "70/6/6/6", target_summary: "80/8/8/8",
        missing_total: 24, compute_error: "",
      },
    ],
    tasks: [
      {
        task_type: "leyline", task_label: "地脉花", phase: "character_level", phase_label: "角色等级",
        phases: [{ phase: "character_level", phase_label: "角色等级", missing: 625000 }],
        character_id: 10000089, character_name: "胡桃", material: "摩拉", item_id: 104001,
        category: "currency", required: 1250000, missing: 625000, route: "藏金之花",
        domain: "", domain_index: "", status: "runnable", status_label: "可执行",
        status_note: "", reason: "摩拉走地脉花「藏金之花」", count: 6, resin: 120,
        count_note: "一次地脉花约 480,000 摩拉，按 6 趟跑",
      },
      {
        task_type: "domain", task_label: "秘境", phase: "talent", phase_label: "天赋",
        phases: [{ phase: "talent", phase_label: "天赋", missing: 38 }],
        character_id: 10000089, character_name: "胡桃", material: "「诗文」的哲学", item_id: 104301,
        category: "talent_book", required: 38, missing: 38, route: "忘却之峡", domain: "忘却之峡",
        domain_index: "3", status: "closed_today", status_label: "今日未开放",
        status_note: "该秘境今天（周五）不开放，开放日是 周三/六/日", reason: "秘境产出",
        count: 2, resin: 40,
      },
    ],
    bettergi_cmd: { energy_task: { action: "run_leyline", target: "藏金之花", count: 6 } },
    execution: {
      tasks: [{ task_type: "leyline", task_label: "地脉花", material: "摩拉", route: "藏金之花",
                count: 6, resin: 120, status: "runnable", priority_label: "⑥ 摩拉地脉「藏金之花」" }],
      fallback: false, note: "",
    },
    // 当前体力（玩家填的 / 接口读到的）—— 卡片主数字显示它，**不是**计划消耗
    current_resin: {
      available: true, current: 27, max: 200, base: 27, recovered: 0,
      source: "手动录入（按 8 分钟 1 点推算已回涨）", reason: "", stale: false,
    },
    execution_mode: "all", execution_mode_label: "一次性全部执行",
    execution_queue: { mode: "all", active: false, total: 0, cursor: 0, done: 0, current: null },
    estimate: { days: 12.3, total_resin: 2214, resin_per_day: 180,
                text: "预计还要约 12.3 天（要 2,214 体力，按每天 180 体力折算）" },
    summary: { materials: 5, missing_kinds: 3, tasks: 2, runnable: 1, blocked: 1, resin: 120 },
    inventory: {
      configured: true, available: true, stale: true, fetched_at: "2026-09-17T14:00:00",
      age_text: "1.0 天前", item_count: 412, uid: "100000001", server: "cn_gf01",
      hint: "「已有 / 还差」由养成计算器提供（每次算材料都会刷新），不依赖已下线的背包接口。",
    },
    // 养成计算器给的「已有」材料（部分数据）—— 库存卡现在显示这个，而不是"未同步"
    known_inventory: {
      count: 2,
      source: "养成计算器（available_material）",
      note: "「已有 / 还差」由养成计算器提供",
      items: [
        { item_id: 112146, item_name: "幻造萤屑", owned: 196, required: 6, lack: 0, characters: ["胡桃"] },
        { item_id: 104366, item_name: "「慈爱」的指引", owned: 16, required: 21, lack: 5, characters: ["胡桃"] },
      ],
    },
    compute_errors: [],
    missing_overview: [{ item_id: 104001, item_name: "摩拉", category: "currency", missing: 625000 }],
    resin: { character_level: 120, weapon_level: 0, talent: 40, total: 160 },
    blockers: [
      { kind: "inventory_stale", message: "这份不是实时数据（1.0 天前同步的），材料缺口可能已经不准，建议先同步。" },
      { kind: "closed_today", material: "「诗文」的哲学", message: "该秘境今天（周五）不开放，开放日是 周三/六/日。" },
    ],
  },
  targets: [
    {
      character_id: 10000089, character_name: "胡桃", rarity: 5, enabled: true, priority: 1,
      level_target: 90, normal_target: 10, skill_target: 10, burst_target: 10,
      weapon_enabled: true, weapon_level_target: 90, weapon_id: 12512,
      level_priority: 1, weapon_priority: 2, talent_priority: 3, completion_mode: "sequential",
    },
    {
      character_id: 10000032, character_name: "班尼特", rarity: 4, enabled: true, priority: 2,
      level_target: 80, normal_target: 8, skill_target: 8, burst_target: 8,
      weapon_enabled: false, weapon_level_target: 90, weapon_id: 0,
      level_priority: 1, weapon_priority: 2, talent_priority: 3, completion_mode: "sequential",
    },
  ],
  sort_mode: "default",
  sort_modes: [
    { value: "default", label: "默认（5 星优先）" },
    { value: "custom", label: "自定义（按我拖的顺序）" },
    { value: "auto", label: "自动规划（第二阶段）" },
  ],
  inventory: {
    configured: true, available: true, stale: true, fetched_at: "2026-09-17T14:00:00",
    age_text: "1.0 天前", item_count: 412, uid: "100000001", server: "cn_gf01",
    hint: "这份不是实时数据（1.0 天前同步的），材料缺口可能已经不准，建议先同步。",
  },
  phase_labels: { character_level: "角色等级", weapon_level: "武器等级", talent: "天赋" },
  state_labels: { WAIT_CONFIRM: "等待确认", BLOCKED: "受阻（等材料 / 等路线）" },
  task_type_labels: { domain: "秘境", leyline: "地脉花" },
  status_labels: { runnable: "可执行", closed_today: "今日未开放" },
  snapshots: ["inventory_20260917_140000.json"],
  sync_history: [
    { id: 2, success: 1, started_at: "2026-09-17T14:00:00", finished_at: "2026-09-17T14:00:03", snapshot_id: 7, error: "" },
    { id: 1, success: 0, started_at: "2026-09-17T09:00:00", finished_at: "2026-09-17T09:00:20", snapshot_id: 0, error: "Cookie 无效或已过期" },
  ],
  sync_summary: { total: 2, success: 1, failed: 1, items: [] },
  executions: [
    { id: 1, plan_id: 3, character_id: 10000089, phase: "character_level", started_at: "2026-09-17T15:00:00", status: "FINISHED", result: "BetterGI 任务结束" },
  ],
  mys: { configured: true, masked: "ltuid=1847…4563; ltoken=abcd…wxyz" },
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
  if (url.startsWith("/api/update")) payload = UPDATE;
  if (url.startsWith("/api/growth")) payload = GROWTH;
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
    // 「系统 → 版本更新」页（boot 里 switchPage("run")，所以这里手动切过去）
    sandbox.switchPage("update");
    await settle();
    // 再切到「资源冷却」页，验证这条链路（/api/cooldown → 分类卡片 + 分区表格）也能渲染
    sandbox.switchPage("cooldown");
    await settle();
    // 再切到「角色养成」页（/api/growth → 四张卡 + 库存状态 + 任务表 + 目标表 + 历史）
    sandbox.switchPage("growth");
    await settle();
  } catch (exc) {
    console.error("❌ 运行 app.js 抛异常：", exc && exc.stack ? exc.stack : exc);
    process.exit(2);
  }

  const channelsPage = captured["chan-cards"] || "";
  const dash = captured["dash-channels"] || "";
  const updateCards = captured["update-cards"] || "";
  const updateBody = captured["update-body"] || "";
  const updateNotes = elements.get("update-notes");
  const updateNotesText = updateNotes ? updateNotes.textContent : "";
  const updateBadge = elements.get("nav-badge-update");
  const openButton = elements.get("btn-update-open");
  const coolTabs = captured["cooldown-tabs"] || "";
  const coolCards = captured["cooldown-cards"] || "";
  const coolTable = captured["cooldown-table"] || "";
  const coolFoot = captured["cooldown-foot"] || "";
  const titleEl = elements.get("cooldown-title");
  const growCards = captured["growth-cards"] || "";
  const growInventory = captured["growth-inventory"] || "";
  const growTasks = captured["growth-tasks"] || "";
  const growTargets = captured["growth-targets"] || "";
  const growBlockers = captured["growth-blockers"] || "";
  const growFoot = captured["growth-plan-foot"] || "";
  const growHistory = captured["growth-history"] || "";
  const growHint = elements.get("growth-plan-hint");

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

  // 「版本更新」页：点「检查更新」要真的再拉一次（force=1）并重新渲染
  const updateButton = elements.get("btn-update-check");
  const updateHandler = (updateButton._on.click || [])[0];
  let forceUsed = false;
  const originalFetch = sandbox.fetch;
  sandbox.fetch = async (url, init) => {
    if (String(url).includes("force=1")) forceUsed = true;
    return originalFetch(url, init);
  };
  if (updateHandler) updateHandler({ target: updateButton });
  await settle();
  const updateAfterClick = captured["update-body"] || "";

  // 「打开发布页」：应该 POST /api/open（带上 release 链接），而不是让 WebView 自己跳转
  const openHandler = (openButton && openButton._on.click || [])[0];
  let openedUrl = "";
  sandbox.fetch = async (url, init) => {
    if (String(url).includes("/api/open")) {
      openedUrl = JSON.parse((init && init.body) || "{}").url || "";
    }
    return originalFetch(url, init);
  };
  if (openHandler) openHandler({ target: openButton });
  await settle();
  sandbox.fetch = originalFetch;

  const checks = [
    ["通道卡片渲染出来了", channelsPage.includes('data-chan="qq"')],
    ["第二个通道也在", channelsPage.includes('data-chan="feishu"')],
    ["内嵌控制台容器存在", channelsPage.includes('id="chan-console-qq"')],
    ["运行中状态 + PID 显示", channelsPage.includes("运行中") && channelsPage.includes("4242")],
    ["未配置通道写明缺哪一项", channelsPage.includes("FEISHU_APP_ID")],
    ["自动启动勾选框", channelsPage.includes("data-chan-auto=\"qq\"")],
    ["概览摘要卡也有内容", dash.includes("QQ 机器人")],
    ["版本页：四张卡写明本地版本 / 最新 release / 状态 / 检查方式",
      updateCards.includes("本地版本") && updateCards.includes("1.0.0")
      && updateCards.includes("最新 release") && updateCards.includes("v1.5.0")
      && updateCards.includes("有新版本") && updateCards.includes("检查方式")],
    ["版本页：正文写清结论与更新方式",
      updateBody.includes("有新版本") && updateBody.includes("git pull")],
    ["版本页：更新说明贴在下面那张卡",
      updateNotesText.includes("修了 A") && updateNotesText.includes("修了 B")],
    ["版本页：有新版本时侧边栏亮红点",
      updateBadge && !updateBadge.classList.contains("hidden")],
    ["版本页：「打开发布页」被启用且会走 /api/open",
      openButton && openButton.disabled === false && openedUrl.includes("releases/tag/v1.5.0")],
    ["版本页：点「检查更新」会忽略缓存重查（force=1）",
      forceUsed && updateAfterClick.includes("v1.5.0")],
    ["版本页：文案里不放 emoji（界面用自己的图标）",
      !/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}]/u.test(updateCards + updateBody)],
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
    // ---- 角色养成页 ----
    // ⚠️ 这张卡以前标题是「今日预计体力」、数字是**计划要花多少体力** ——
    //    玩家会读成"我有多少体力"（实测被反馈："实际只有 27 点，卡片却显示 160"）。
    //    现在：标题「当前体力」= 你**实际**有多少（接口 / 手动记的），计划消耗放副标题。
    ["养成页：四张卡（计划状态 / 材料数据 / 当前角色 / 体力）",
      growCards.includes("养成计划") && growCards.includes("材料数据")
      && growCards.includes("当前角色") && growCards.includes("当前体力")
      && !growCards.includes("今日预计体力")],
    ["养成页：体力卡主数字是**实际体力**、计划消耗只作副标题",
      growCards.includes("27") && growCards.includes("本轮会花")],
    ["养成页：当前角色与阶段显示出来",
      growCards.includes("胡桃") && growCards.includes("角色等级")],
    ["养成页：材料数据卡说清来源（养成计算器）",
      // 口径变了：不再有"米游社库存 未同步 / 点同步米游社库存"那种说法。
      growCards.includes("养成计算器") && !growCards.includes("未同步")],
    ["养成页：库存卡不再把材料一条条列出来（只报数量）",
      growInventory.includes("2") && growInventory.includes("养成计算器")
      && !growInventory.includes("幻造萤屑")],
    ["养成页：表格下面不再重复「受阻原因」（任务表的状态列已经说了）",
      // 玩家要求删掉：以前这里把"缺路线 / 未开放"再抄一遍，纯重复。
      !growBlockers.includes("缺少 BetterGI 可执行路线")
      && !growBlockers.includes("材料缺口可能已经不准")],
    ["养成页：任务表里可执行与不可执行都看得到",
      growTasks.includes("可执行") && growTasks.includes("今日未开放")
      && growTasks.includes("藏金之花") && growTasks.includes("忘却之峡")],
    ["养成页：任务表用「消耗 / 所需」两栏，并说明两个数的区别",
      growTasks.includes("消耗") && growTasks.includes("所需") && growTasks.includes("还差")],
    ["养成页：任务表标出缺口数量", growTasks.includes("625,000") && growTasks.includes("38")],
    ["养成页：角色目标表列出当前状态与目标",
      growTargets.includes("胡桃") && growTargets.includes("80/8/8/8") && growTargets.includes("90/10/10/10")],
    ["养成页：角色目标表有拖拽手柄与编辑/删除按钮",
      growTargets.includes("drag-handle") && growTargets.includes("data-growth-edit=\"10000089\"")
      && growTargets.includes("data-growth-delete=\"10000089\"")],
    ["养成页：武器未纳入的角色在顺序里写明",
      growTargets.includes("武器（未纳入）")],
    ["养成页：排序模式下拉框带三个选项",
      (captured["growth-sort-mode"] || "").includes("默认（5 星优先）")
      && (captured["growth-sort-mode"] || "").includes("自定义")],
    ["养成页：表尾只写「各角色还差多少」（别的说明都删了）",
      // 以前这里堆着"材料需求由…给出 / 库存由…给出 / BetterGI 从不写回库存"那段，
      // 玩家要求删掉（口径都写在卡片和表头说明了）。
      growFoot.includes("各角色还差") && !growFoot.includes("从不")],
    ["养成页：历史区不再把背包接口的失败当失败统计",
      // 背包接口下线的那些失败要过滤掉（mock 里有一条 Cookie 失效 + 一条成功）
      growHistory.includes("材料数据") || growHistory.includes("养成计算器")],
    ["养成页：状态提示行不再重复计划状态（顶部卡片已经说了）",
      !growHint.textContent || !growHint.textContent.includes("可执行任务")],
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
