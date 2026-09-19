/**
 * 把 genshin-db（Node 包）导出成 GI_Agent 能读的 JSON。
 *
 * 为什么要有这一步：genshin-db 只有 Node 版，而 GI_Agent 是纯 Python、且要能离线跑。
 * 按架构文档 §9 的要求（"不要让业务逻辑直接依赖 genshin-db 原始 JSON"），这里做一次性
 * 导出 —— 之后 Python 侧只读 `memory/game_knowledge/genshin_db_export.json`，
 * **运行时不需要 Node，也不需要这个包**。
 *
 * 导出的三类数据（都是 genshin-db 独有、`mapping.json` 里没有的）：
 *   · materials.sources   —— 权威来源文本，用来**交叉校验**材料族推导
 *   · enemies.rewardPreview —— 怪物 → 掉落（含掉率），用来建「族群 → 成员怪物」层（§8）
 *   · domains.daysOfWeek  —— 秘境**开放星期**（含哪些天赋书），补天赋书日程
 *
 * 用法：
 *   npm install                       # 装 devDependency（package.json 里已声明 genshin-db）
 *   npm run export:genshin-db         # 导出 → memory/game_knowledge/genshin_db_export.json
 *
 * 包在哪儿找（按顺序，第一个能用的就用）：
 *   ① `--pkg <目录>`   —— 手动指定；
 *   ② `.env` 里的 `GENSHIN_DB_PATH` —— 你把包装在别处（比如全局 node_modules）时用这个；
 *   ③ 项目自己的 `node_modules/genshin-db`（`npm install` 之后就在这儿）；
 *   ④ 用户主目录的 `node_modules/genshin-db`（`npm i` 在 C:\\Users\\<你> 下执行过）；
 *   ⑤ 当前目录的 `node_modules`。
 *
 * ⚠️ 实测 v5.2.7（游戏数据 v6.2）**比 mapping.json 旧**：至冬/挪德卡莱的新材料
 * （幻造晶鳞石、嵌合种、扭曲的枯枝、奥黛塔…）它一个都没有。所以它只做**补充与校验**，
 * 覆盖优先级低于 mapping.json。
 */
const fs = require('fs');
const path = require('path');
const os = require('os');

const ROOT = path.join(__dirname, '..');

/** 极简 .env 读取（只认 `KEY=VALUE`，够用了；不引入 dotenv 这个依赖）。 */
function readEnvFile() {
  const values = {};
  const file = path.join(ROOT, '.env');
  if (!fs.existsSync(file)) return values;
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const text = line.trim();
    if (!text || text.startsWith('#')) continue;
    const at = text.indexOf('=');
    if (at <= 0) continue;
    values[text.slice(0, at).trim()] = text.slice(at + 1).trim().replace(/^["']|["']$/g, '');
  }
  return values;
}

function findPackage(explicit) {
  const env = readEnvFile();
  const fromEnv = process.env.GENSHIN_DB_PATH || env.GENSHIN_DB_PATH || '';
  const candidates = [
    explicit,
    fromEnv,
    path.join(ROOT, 'node_modules', 'genshin-db'),
    path.join(os.homedir(), 'node_modules', 'genshin-db'),
    path.join(process.cwd(), 'node_modules', 'genshin-db'),
  ].filter(Boolean);
  for (const dir of candidates) {
    if (dir && fs.existsSync(path.join(dir, 'package.json'))) return dir;
  }
  return null;
}

const args = process.argv.slice(2);
const pkgIndex = args.indexOf('--pkg');
const pkgDir = findPackage(pkgIndex >= 0 ? args[pkgIndex + 1] : null);
if (!pkgDir) {
  console.error('❌ 找不到 genshin-db。三种办法任选一种：');
  console.error('   ① 在项目根目录跑 `npm install`（package.json 已经声明了这个 devDependency）；');
  console.error('   ② 在 .env 里写 `GENSHIN_DB_PATH=D:\\某个\\node_modules\\genshin-db`；');
  console.error('   ③ 直接指定：node scripts/export_genshin_db.js --pkg <目录>');
  process.exit(1);
}
console.log(`📦 用的是 ${pkgDir}`);

const pkg = JSON.parse(fs.readFileSync(path.join(pkgDir, 'package.json'), 'utf8'));
const dataPath = path.join(pkgDir, 'src', 'min', 'data.min.json');
if (!fs.existsSync(dataPath)) {
  console.error(`❌ 找不到数据文件 ${dataPath}`);
  process.exit(1);
}
const raw = JSON.parse(fs.readFileSync(dataPath, 'utf8'));
const zh = (raw.data || {})[ 'ChineseSimplified' ] || {};
const outPath = path.join(__dirname, '..', 'memory', 'game_knowledge', 'genshin_db_export.json');

// —— 材料：来源文本（校验用）——
const materials = {};
for (const v of Object.values(zh.materials || {})) {
  if (!v || !v.name) continue;
  materials[v.name] = {
    id: Number(v.id) || 0,
    rarity: Number(v.rarity) || 0,
    category: v.category || '',
    type_text: v.typeText || '',
    sources: Array.isArray(v.sources) ? v.sources : [],
  };
}

// —— 怪物：掉落（建族群 → 成员怪物用）——
const enemies = {};
for (const v of Object.values(zh.enemies || {})) {
  if (!v || !v.name) continue;
  const drops = (v.rewardPreview || [])
    .filter((r) => r && r.name && r.name !== '摩拉')
    .map((r) => ({ name: r.name, rate: typeof r.count === 'number' ? r.count : null }));
  enemies[v.name] = {
    id: Number(v.id) || 0,
    enemy_type: v.enemyType || '',
    category_text: v.categoryText || '',
    drops,
  };
}

// —— 秘境：开放星期 + 奖励（天赋书日程用）——
// daysOfWeek 是 ["周二","周五","周日"]，项目里用的是单个汉字 ["二","五","日"]。
const domains = [];
for (const v of Object.values(zh.domains || {})) {
  if (!v || !v.name) continue;
  const days = (v.daysOfWeek || []).map((d) => String(d).replace(/^周/, '')).filter(Boolean);
  domains.push({
    stage: v.name,
    entrance: v.entranceName || '',
    region: v.regionName || '',
    domain_type: v.domainType || '',
    domain_text: v.domainText || '',
    days,
    rewards: (v.rewardPreview || []).filter((r) => r && r.name).map((r) => r.name),
  });
}

// —— 合成配方：档位链（3 低换 1 高）——
// 用途：把「指引 / 裂齿」这类**非最低档**材料换算成"等效绿色素材"，
// 才能套用体力产出比例（那个比例是按绿材料给的）。见 brain/resin_math.py。
const crafts = {};
for (const v of Object.values(zh.crafts || {})) {
  if (!v || !v.name) continue;
  crafts[v.name] = {
    filter_text: v.filterText || '',
    result_count: Number(v.resultCount) || 1,
    recipe: (v.recipe || []).filter((r) => r && r.name)
      .map((r) => ({ name: r.name, count: Number(r.count) || 1 })),
  };
}

const payload = {
  source: 'genshin-db',
  package_version: pkg.version || '',
  data_version: (pkg.description || '').match(/v[\d.]+/)?.[0] || '',
  // ⚠️ **不要**写 `package_path`（包所在的本机绝对路径）：
  //    这个产物是**要提交进仓库**的，而绝对路径会暴露开发机的目录结构
  //    （例如 `C:\Users\<用户名>\node_modules\genshin-db`）。排查"用的哪个包"
  //    看脚本运行时打印的那行 `📦 用的是 …` 就够了。
  language: 'ChineseSimplified',
  exported_at: new Date().toISOString().replace('T', ' ').slice(0, 19),
  counts: {
    materials: Object.keys(materials).length,
    enemies: Object.keys(enemies).length,
    domains: domains.length,
    crafts: Object.keys(crafts).length,
    enemies_with_drops: Object.values(enemies).filter((e) => e.drops.length).length,
    domains_with_days: domains.filter((d) => d.days.length).length,
  },
  materials,
  enemies,
  domains,
  crafts,
};

fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, JSON.stringify(payload, null, 1), 'utf8');
console.log(`✅ 已导出 ${path.relative(process.cwd(), outPath)}`);
console.log(`   genshin-db ${payload.package_version}（游戏数据 ${payload.data_version}）`);
console.log(`   ` + JSON.stringify(payload.counts));
