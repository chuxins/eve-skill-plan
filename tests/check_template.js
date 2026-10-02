/* 前端离线校验（不需要浏览器 / 后端）：
 *  1) 用 vendor 里真实的 Vue（浏览器同款完整版）编译 index.html 的 #app 模板；
 *  2) 审计模板引用的标识符是否都能解析（app.js 的 data/computed/methods 或 util.js 的函数）；
 *  3) 模板函数发布检查：模板用到的 util.js 函数必须挂到实例（app.config.globalProperties）上，
 *     否则 Vue 的渲染代理只会给回 undefined，调用即 TypeError: xxx is not a function；
 *  4) 渲染冒烟：用「同语义的渲染代理」把初始态、选好角色态、四个页签详情真渲染一遍，
 *     并断言渲染过程中没有任何「取不到的名字」（缺失函数 / 缺失字段都会在这里红）；
 *  5) 移动端适配审计：CSS 是布局真相所在 —— 断点顺序、手机上「一次只显示一栏」、
 *     触控目标 ≥44px、输入框 ≥16px（iOS 聚焦不放大）、宽表格有没有 .tw 包裹，
 *     再用渲染出的 vnode 确认 main 的 class 跟着 mpane 走、底部导航确实渲染出来了。
 *     真浏览器的 390×844 视口验证在 tests/check_mobile.js。
 * 用法：node tests/check_template.js
 * 退出码：1 模板编译失败 / 2 标识符缺失 / 3 vendor 缺编译器 / 4 找不到 #app
 *        5 util.js 函数没挂到实例 / 6 渲染冒烟失败 / 7 移动端适配审计失败
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');

/* ---------- 最小 DOM 桩：Vue 的 DOM 编译器用 div 解码属性/文本里的实体 ---------- */
const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: '\u00a0' };
function decodeEntities(raw) {
  return String(raw).replace(/&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z]+);/g, (m, body) => {
    if (body[0] === '#') {
      const code = /^#x/i.test(body) ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return Number.isNaN(code) ? m : String.fromCodePoint(code);
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, body) ? ENTITIES[body] : m;
  });
}
/* 引号感知地找第一个完整标签（属性值里可能出现 >，例如 :class="{a:b>=c}"） */
function firstTag(html) {
  let quote = null;
  for (let i = 0; i < html.length; i++) {
    const ch = html[i];
    if (quote) { if (ch === quote) quote = null; continue; }
    if (ch === '"' || ch === "'") { quote = ch; continue; }
    if (ch === '>') return html.slice(0, i + 1);
  }
  return null;
}
function stripTags(html) {
  let out = '';
  let quote = null;
  let inTag = false;
  for (let i = 0; i < html.length; i++) {
    const ch = html[i];
    if (inTag) {
      if (quote) { if (ch === quote) quote = null; continue; }
      if (ch === '"' || ch === "'") { quote = ch; continue; }
      if (ch === '>') inTag = false;
      continue;
    }
    if (ch === '<') { inTag = true; continue; }
    out += ch;
  }
  return out;
}
function makeEl() {
  let html = '';
  return {
    get innerHTML() { return html; },
    set innerHTML(v) { html = String(v); },
    get textContent() { return decodeEntities(stripTags(html)); },
    get children() {
      const tag = firstTag(html);
      if (!tag) return [];
      const attrs = {};
      for (const a of tag.matchAll(/([\w:.-]+)="([^"]*)"/g)) attrs[a[1]] = decodeEntities(a[2]);
      return [{ getAttribute: n => (Object.prototype.hasOwnProperty.call(attrs, n) ? attrs[n] : null) }];
    },
  };
}

/* vendor 是 IIFE 全局构建（不挂 module.exports），用 vm 造一个浏览器式全局 */
const sandbox = { console, document: { createElement: () => makeEl() } };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'static/vendor/vue.global.prod.js'), 'utf8'), sandbox);
const Vue = sandbox.Vue;
if (!Vue || typeof Vue.compile !== 'function') {
  console.error('vendor/vue.global.prod.js 缺少内建模板编译器（需要完整版，不是 runtime-only）');
  process.exit(3);
}
console.log('Vue', Vue.version, '（含模板编译器）');

/* ---------- 取出 #app 的模板 ---------- */
const html = fs.readFileSync(path.join(ROOT, 'static/index.html'), 'utf8');
const open = '<div id="app">';
const start = html.indexOf(open) + open.length;
const end = html.indexOf('<script src=', start);
if (start <= 0 || end < 0) {
  console.error('index.html 里找不到 <div id="app">…</div> 或脚本标签');
  process.exit(4);
}
let tpl = html.slice(start, end).trim();
if (tpl.endsWith('</div>')) tpl = tpl.slice(0, -'</div>'.length);

let render;
try {
  render = Vue.compile(tpl);       // 完整版返回的是渲染函数本身
} catch (e) {
  console.error('模板编译失败：', e.message);
  process.exit(1);
}
console.log('模板编译通过：', tpl.length, '字符');

/* ---------- 标识符审计（直接分析模板表达式，避开编译产物里的运行时辅助函数） ---------- */

/* util.js / app.js 里定义过的名字 */
function topLevelNames(src) {
  const set = new Set();
  for (const m of src.matchAll(/^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)/gm)) set.add(m[1]);
  return set;
}

/* 从一段 JS 表达式里挑出「自由标识符」：
 * 去掉字符串/模板串、属性名、对象字面量的键、数组下标，剩下的才是需要解析的名字 */
const KEYWORDS = new Set(['true', 'false', 'null', 'undefined', 'this', 'typeof', 'void', 'in',
  'of', 'new', 'instanceof', 'delete', 'return', 'if', 'else', 'await', 'async', 'function',
  'NaN', 'Infinity', 'Math', 'Object', 'String', 'Number', 'Boolean', 'Array', 'JSON', 'Date']);
function identifiersIn(expr) {
  /* 箭头函数的参数是局部变量：本表达式内视为已定义（例如 r.levels.map(l=>lvRoman(l.level))） */
  const bound = new Set();
  for (const m of expr.matchAll(/\(([^()]*)\)\s*=>/g)) {
    for (const p of m[1].split(',')) {
      const n = p.trim();
      if (/^[A-Za-z_$][\w$]*$/.test(n)) bound.add(n);
    }
  }
  for (const m of expr.matchAll(/(?:^|[^\w$.])([A-Za-z_$][\w$]*)\s*=>/g)) bound.add(m[1]);

  const cleaned = expr
    .replace(/'[^']*'/g, '""')
    .replace(/"[^"]*"/g, '""')
    .replace(/`[^`]*`/g, '""')                      // 模板串（模板里极少用）
    .replace(/\?\.\s*([A-Za-z_$][\w$]*)/g, '.p')     // 可选链
    .replace(/\.\s*([A-Za-z_$][\w$]*)/g, '.p')       // 属性访问
    .replace(/([{,]\s*)([A-Za-z_$][\w$]*)\s*:/g, '$1p:') // 对象字面量的键（:class="{a:b}"）
    .replace(/\[\s*\d+\s*\]/g, '[0]');
  const out = new Set();
  for (const m of cleaned.matchAll(/(^|[^\w$.])([A-Za-z_$][\w$]*)/g)) {
    if (!KEYWORDS.has(m[2]) && !bound.has(m[2])) out.add(m[2]);
  }
  return out;
}

const utilSrc = fs.readFileSync(path.join(ROOT, 'static/util.js'), 'utf8');
const utilNames = new Set(topLevelNames(utilSrc));   // util.js 里定义的名字
const known = new Set(utilNames);
const appSrc = fs.readFileSync(path.join(ROOT, 'static/app.js'), 'utf8');
for (const m of appSrc.matchAll(/^\s{0,8}(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)/gm)) known.add(m[1]);
/* data/computed/methods 的名字：
 *  - 方法简写 `name(args) {`（含 async）
 *  - 对象键 `name: ...`（data 的字段、`name: () => {}` 之类） */
for (const m of appSrc.matchAll(/^\s{2,12}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{/gm)) known.add(m[1]);
for (const m of appSrc.matchAll(/^\s*([A-Za-z_$][\w$]*)\s*:/gm)) known.add(m[1]);
for (const m of appSrc.matchAll(/[{,]\s*([A-Za-z_$][\w$]*)\s*:/g)) known.add(m[1]);


/* 模板里 v-for / v-slot 引入的局部变量 */
const locals = new Set(['$event']);
for (const m of tpl.matchAll(/v-for\s*=\s*"([^"]+)"/g)) {
  const left = m[1].split(/\s+(?:in|of)\s+/)[0];
  for (const part of left.replace(/[()[\]]/g, '').split(',')) {
    const name = part.trim();
    if (/^[A-Za-z_$][\w$]*$/.test(name)) locals.add(name);
  }
}
for (const m of tpl.matchAll(/v-slot(?::[\w-]+)?\s*=\s*"([^"]+)"/g)) {
  for (const part of m[1].replace(/[{}]/g, '').split(',')) {
    const name = part.trim();
    if (/^[A-Za-z_$][\w$]*$/.test(name)) locals.add(name);
  }
}

/* 收集模板表达式：{{ }} 与所有指令/属性绑定 */
const missing = new Set();
const fromUtil = new Set();      // 模板里用到的 util.js 函数（必须挂到实例上，见第 3 步）
const exprs = [];
for (const m of tpl.matchAll(/\{\{([\s\S]*?)\}\}/g)) exprs.push(m[1]);
for (const m of tpl.matchAll(/(?:^|\s)(?:v-[\w:.-]*|[:@#][\w:.-]*)\s*=\s*"([^"]*)"/g)) exprs.push(m[1]);
for (const raw of exprs) {
  for (const name of identifiersIn(raw)) {
    if (locals.has(name)) continue;
    if (utilNames.has(name)) fromUtil.add(name);
    if (known.has(name)) continue;
    missing.add(name);
  }
}

if (missing.size) {
  console.error('模板引用了未定义标识符：', [...missing].sort().join(', '));
  console.error('（应定义在 app.js 的 data/computed/methods 或 util.js 里）');
  process.exit(2);
}
console.log('标识符检查通过：模板表达式', exprs.length, '处，引用的标识符全部有定义');

/* ---------- 3) 模板函数发布检查 ----------
 * 运行时编译出来的渲染函数形如 with (_ctx) { … }，而 Vue 3.5 渲染代理的 has()
 * 只放行 Infinity/Math/Date/JSON 等白名单全局；其余自由标识符一律被当成实例属性，
 * 取到 undefined 后一调用就抛 TypeError: xxx is not a function（本站整页渲染曾因此失败）。
 * 所以模板里用到的 util.js 函数必须在 app.js 里挂到实例上。 */
let appOpts = null;
let published = null;
const appSandbox = {
  console,
  Vue: {
    createApp(opts) {
      appOpts = opts;
      published = {};
      return { config: { globalProperties: published }, mount() {} };
    },
  },
};
vm.createContext(appSandbox);
vm.runInContext(utilSrc + '\n' + appSrc, appSandbox, { filename: 'util+app.js' });

if (!appOpts) {
  console.error('app.js 没有调用 Vue.createApp');
  process.exit(5);
}
const notPublished = [...fromUtil].filter(k => typeof published[k] !== 'function');
if (notPublished.length) {
  console.error('模板用到但没挂到实例上的 util.js 函数：', notPublished.sort().join(', '));
  console.error('（在 app.js 末尾的 Object.assign(app.config.globalProperties, {…}) 里补上）');
  process.exit(5);
}
console.log('模板函数发布检查通过：', [...fromUtil].sort().join(', '));

/* ---------- 4) 渲染冒烟：用同语义的渲染代理把主要状态真渲染一遍 ---------- */
const GLOBALS_ALLOWED = new Set(('Infinity,undefined,NaN,isFinite,isNaN,parseFloat,parseInt,' +
  'decodeURI,decodeURIComponent,encodeURI,encodeURIComponent,Math,Number,Date,Array,Object,' +
  'Boolean,String,RegExp,Map,Set,JSON,Intl,BigInt,console,Error,Symbol').split(','));

const inst = appOpts.data.call({});
for (const [k, fn] of Object.entries(appOpts.computed || {})) {
  Object.defineProperty(inst, k, { get: () => fn.call(inst), configurable: true });
}
Object.assign(inst, appOpts.methods, published);

const missed = [];
const ctxProxy = new Proxy(inst, {
  // 与 Vue 3.5 渲染代理 ro 同语义：白名单以外的自由标识符一律「存在」，但取到 undefined
  has: (_t, k) => typeof k === 'string' && k[0] !== '_' && !GLOBALS_ALLOWED.has(k),
  get(t, k) {
    if (k === Symbol.unscopables) return undefined;
    if (k in t) return t[k];
    if (typeof k === 'string') missed.push(k);
    return undefined;
  },
});

function collectText(node, out = []) {
  if (node == null || out.length > 20000) return out;
  if (typeof node === 'string' || typeof node === 'number') { out.push(String(node)); return out; }
  if (Array.isArray(node)) { for (const x of node) collectText(x, out); return out; }
  if (typeof node !== 'object') return out;
  if (node.children !== undefined) collectText(node.children, out);
  if (typeof node.slots === 'function') { try { collectText(node.slots(), out); } catch (e) {} }
  return out;
}

let renders = 0;
function renderState(label, marks) {
  missed.length = 0;
  let vnode;
  try {
    vnode = render(ctxProxy, []);       // render 由 Vue.compile 产出，签名是 (_ctx, _cache)
  } catch (e) {
    console.error(`渲染「${label}」抛错：${e.message}`);
    if (missed.length) console.error('（渲染代理没找到的标识符：' + [...new Set(missed)].join(', ') + '）');
    process.exit(6);
  }
  if (missed.length) {
    console.error(`渲染「${label}」时有取不到的名字：` + [...new Set(missed)].join(', '));
    console.error('（app.js 的 data/computed/methods 或挂到实例上的模板函数里都没有它们）');
    process.exit(6);
  }
  const text = collectText(vnode).join('|');
  const absent = marks.filter(m => (m instanceof RegExp ? !m.test(text) : !text.includes(m)));
  if (absent.length) {
    console.error(`渲染「${label}」缺少预期文本：` + absent.map(String).join(' , '));
    process.exit(6);
  }
  renders++;
  console.log(`  渲染「${label}」通过（${text.length} 字符，命中 ${marks.length} 个标记）`);
}

const ATTRS = { charisma: 17, intelligence: 17, memory: 17, perception: 17, willpower: 17 };
const TIME = /\d{2}-\d{2} \d{2}:\d{2}/;

renderState('初始（未选角色 / 无数据 / 未登录）',
  ['EVE 技能规划', '未选角色', '角色与属性', '快速开始', '还没有目标',
   'SSO：未登录', '未登录：无法保存到服务端', '登录后这里显示你自己的已保存计划',
   'SSO 登录', '保存计划', '登录（EVE SSO）']);

/* 选好角色、boot() 完成后的状态：5 维属性 + 总技能点 + 训练队列 + 已保存计划 */
Object.assign(inst, {
  meta: {
    sde: { source: 'sde', built_at: '2026-09-28 10:00', skills: 520, types: 30000, type_reqs: 9000, career_plans: 40 },
    counts: { skills: 520, groups: 60, types: 30000, careers: 40 },
    levels: [1, 2, 3, 4, 5],
    categories: [{ id: 6, name: '舰船' }, { id: 7, name: '模块' }],
    attribute_presets: [{ key: 'default', name: '默认 17' }, { key: 'balanced', name: '常见 27/21' },
                        { key: 'max', name: '满配全 27' }],
  },
  attrs: Object.assign({}, ATTRS),
  chars: [{ id: 12345, name: '测试角色' }],
  cid: 12345, cname: '测试角色',
  current: { 3300: 3, 3301: 4 },
  totalSp: 12345678,
  queue: [{ queue_position: 0, skill_id: 3300, skill_name: '加达里战列舰', skill_name_en: 'Caldari Battleship', finished_level: 4, finish_date: '2026-09-29T10:00:00Z' }],
  groups: [{ id: 1, name: '舰船指挥', skills: [3300, 3301] }],
  skills: [{ tid: 3300, name: '加达里战列舰', rank: 8 }],
  careers: [{ plan_id: 'caldari_wealth', name: '加达里财富猎手', name_en: 'Caldari Wealth Hunter', skills: 2 }],
  saved: [{ plan_id: 'p1', name: '乌鸦级计划', updated_at: '2026-09-28 12:00', character_id: 12345,
            targets: [{ tid: 638 }] }],
  loginCid: 12345, loginName: '测试角色',
});
renderState('boot 之后（角色 + 属性 + 队列 + 已登录）',
  ['技能 520 · 类型 30000 · 职业路线 40', '测试角色', '总技能点', '12.3M', '已学技能',
   '魅力', '智力', '记忆', '感知', '毅力', '当前训练队列（1）', '加达里战列舰', '到 Ⅳ 级', TIME,
   '最近保存', '乌鸦级计划', 'SSO：已登录', 'SSO 登录']);

/* 技能库页签 + 技能详情 */
inst.tab = 'skills';
inst.skill = {
  skill: { tid: 3300, name: '加达里战列舰', name_en: 'Caldari Battleship', group: '舰船指挥', rank: 8,
           primary_name: '感知', secondary_name: '毅力', description: '测试描述' },
  current: 3,
  level_seconds: { 1: 100, 2: 200, 3: 400, 4: 800, 5: 1600 },
  level_seconds_from_current: { 3: 0, 4: 800, 5: 1600 },
  prereqs: [{ tid: 3301, name: '加达里巡洋舰', level: 3 }],
  closure: [{ tid: 3301, name: '加达里巡洋舰', group: '舰船指挥', level: 3, rank: 5 }],
  consumers: [{ category: '舰船', items: [{ tid: 638, name: '乌鸦级', group: '战列舰', level: 1 }] }],
};
renderState('技能库页签 + 技能详情',
  ['技能库', '训练时间（属性：魅力 17 · 智力 17 · 记忆 17 · 感知 17 · 毅力 17）', '从 0 级', '从 3 级',
   '26分40秒', '前置技能', '需要的完整前置链（1 项', '被需要的场合（1 类）', '舰船指挥 2']);

/* 需求查询页签 + 类型详情 */
inst.tab = 'reqs';
inst.types = [{ tid: 638, name: '乌鸦级', category: '舰船' }];
inst.tinfo = {
  type: { tid: 638, name: '乌鸦级', name_en: 'Raven', category: '舰船', group: '战列舰', description: '测试描述' },
  skills: 2, missing: 1,
  requirements: [{ tid: 3300, name: '加达里战列舰', group: '舰船指挥', level: 5, current: 3, rank: 8, ok: false },
                 { tid: 3301, name: '加达里巡洋舰', group: '舰船指挥', level: 4, current: 4, rank: 5, ok: true }],
};
renderState('需求查询页签 + 类型详情',
  ['需求查询', '缺 1 / 2 项', '所需技能（2 项', '缺 Ⅴ', '已满足', '乌鸦级', '战列舰']);

/* 职业路线页签 + 路线详情 */
inst.tab = 'careers';
inst.career = {
  career: { plan_id: 'caldari_wealth', name: '加达里财富猎手', name_en: 'Caldari Wealth Hunter',
            description: '测试描述', internal_name: 'caldari_career', skills: [3300, 3301],
            milestones: [{ tid: 3300, name: '加达里战列舰', level: 4 }] },
  requirements: [{ tid: 3300, name: '加达里战列舰', group: '舰船指挥', level: 4, rank: 8 }],
};
renderState('职业路线页签 + 路线详情',
  ['职业路线', '1 个里程碑', '里程碑', '技能需求（1 项', '加达里财富猎手', '加达里战列舰', 'Ⅳ']);

/* 计划页签 + 复算结果 */
inst.tab = 'plan';
inst.planName = '测试计划';
inst.targets = [{ kind: 'type', tid: 638, name: '乌鸦级' }];
inst.plan = {
  name: '测试计划', character_id: 12345,
  targets: [{ kind: 'type', tid: 638, name: '乌鸦级', level: null, skills: 2, seconds: 90061 }],
  rows: [
    { tid: 3300, name: '加达里战列舰', group: '舰船指挥', required: 5, current: 3,
      levels: [{ level: 4 }, { level: 5 }], duration: '1天4小时', sp: 25000,
      end_human: '1天4小时', end_seconds: 90061, seconds: 90061, ok: false },
    { tid: 3301, name: '加达里巡洋舰', group: '舰船指挥', required: 4, current: 4,
      levels: [], duration: '—', sp: 0, end_human: '—', end_seconds: 0, seconds: 0, ok: true },
  ],
  steps: [
    { tid: 3300, name: '加达里战列舰', group: '舰船指挥', required: 5, current: 3, level: 4,
      duration: '12小时', sp: 12000, end_human: '12小时', end_seconds: 43200, seconds: 43200, ok: false },
    { tid: 3300, name: '加达里战列舰', group: '舰船指挥', required: 5, current: 3, level: 5,
      duration: '16小时', sp: 13000, end_human: '1天4小时', end_seconds: 90061, seconds: 46861, ok: false },
  ],
  summary: { skills_total: 2, skills_missing: 1, skills_owned: 1, steps: 2, duration: '1天4小时', sp: 1234567,
             rate_note: '训练速率 22.5 SP/分钟', seconds: 90061,
             attributes: { charisma: 20, intelligence: 20, memory: 20, perception: 20, willpower: 20 } },
};
renderState('计划页签 + 复算结果',
  ['训练计划', '共 2 项技能 · 缺 1 项 · 2 步', '总时长', '1天4小时', '总 SP 1.2M', '已满足 1 项',
   '属性 魅力 20', '需求 · 乌鸦级', '待训练（2 步 · 1 项技能', '加达里战列舰',
   'Ⅳ', 'Ⅴ', 'Ⅲ', '12小时', '16小时', '1d1h1m1s', TIME, '已满足（1 项）']);

console.log(`渲染冒烟通过：${renders} 种状态全部渲染成功（模板函数发布 + 渲染代理语义都已覆盖）`);


/* ---------- 5) 移动端适配审计 ----------
 * 不跑浏览器也能拦住几类常见退化：
 *   - 媒体查询写在基础规则之前：同优先级会被基础规则覆盖，手机布局根本不生效；
 *   - 手机上忘了隐藏/显示某一栏 → 三栏挤进 390px；
 *   - 触控目标太小、输入框字号 <16px（iOS 聚焦会把整页放大）；
 *   - 宽表格没包 .tw → 横向溢出，整页跟着左右晃。
 * 这里查 CSS 与模板，真浏览器的 390×844 实测在 tests/check_mobile.js。 */
const styleSrc = html.slice(html.indexOf('<style>') + 7, html.indexOf('</style>'));
if (styleSrc.length < 200) {
  console.error('index.html 里找不到 <style> 段');
  process.exit(7);
}

/* 极简 CSS 扫描：顶层规则 + @media 里的规则（本项目样式是平铺的，这个解析够用） */
function cssRules(src, base) {
  const out = [];
  let i = 0;
  while (i < src.length) {
    while (i < src.length && /\s/.test(src[i])) i++;                 // 跳过空白
    if (src.startsWith('/*', i)) {                                  // 跳过注释
      const e = src.indexOf('*/', i + 2);
      i = e < 0 ? src.length : e + 2;
      continue;
    }
    const brace = src.indexOf('{', i);
    if (brace < 0) break;
    const sel = src.slice(i, brace).trim();
    let depth = 1, j = brace + 1;
    while (j < src.length && depth) { if (src[j] === '{') depth++; else if (src[j] === '}') depth--; j++; }
    const body = src.slice(brace + 1, j - 1);
    if (!sel) { i = j; continue; }
    if (/^@media/.test(sel)) for (const r of cssRules(body, base + i)) out.push(Object.assign(r, { media: sel }));
    else out.push({ sel, body, media: null, order: base + i });
    i = j;
  }
  return out;
}
const cssRuleList = cssRules(styleSrc, 0).sort((a, b) => a.order - b.order);
const MOB = /@media[^{]*max-width:\s*900px/;                 // 手机断点
const sels = r => r.sel.split(',').map(s => s.trim());
/** 取某选择器在（可选）媒体查询里的某条声明；后面的规则生效，所以从后往前找 */
function cssDecl(sel, prop, mediaRe) {
  const list = cssRuleList.filter(r => (mediaRe ? mediaRe.test(r.media || '') : !r.media) && sels(r).includes(sel));
  for (const r of list.slice().reverse()) {
    const m = new RegExp('(?:^|;|\\s)' + prop.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*:\\s*([^;]+)').exec(r.body);
    if (m) return { value: m[1].trim(), sel: r.sel };
  }
  return null;
}
function cssEq(sel, prop, expected, mediaRe) {
  const d = cssDecl(sel, prop, mediaRe);
  return !!d && d.value.replace(/;$/, '') === expected;
}

let mobPass = 0;
const mobFails = [];
function mok(cond, label, extra) {
  if (cond) { mobPass++; console.log('  ✓', label); }
  else { mobFails.push(label + (extra === undefined ? '' : '（' + extra + '）')); console.log('  ✗', label, extra === undefined ? '' : extra); }
}

console.log('\n== 移动端适配');
/* 视口：viewport-fit=cover 才能用 env(safe-area-inset-*) */
mok(/<meta name="viewport"[^>]*width=device-width/.test(html), 'viewport 声明 width=device-width');
mok(/<meta name="viewport"[^>]*viewport-fit=cover/.test(html), 'viewport 带 viewport-fit=cover（刘海屏安全区）');
mok(/<meta name="theme-color"/.test(html), '声明 theme-color（移动端地址栏配色）');

/* 手机断点：一次只显示一栏，靠 mp-l/mp-c/mp-r */
mok(cssRuleList.some(r => MOB.test(r.media || '')), '存在 @media (max-width:900px) 断点块');
mok(cssEq('main', 'display', 'block', MOB), '手机端 main 不再是三栏网格');
for (const p of ['l', 'c', 'r']) {
  mok(cssEq(`main>.${p}`, 'display', 'none', MOB), `手机端默认隐藏 ${p} 栏`);
  mok(cssEq(`main.mp-${p}>.${p}`, 'display', 'block', MOB), `手机端 mpane=${p} 时显示 ${p} 栏`);
}
mok(/<main :class="'mp-'\+mpane">/.test(html), 'main 的 class 绑定 mpane');
mok(!cssDecl('main>.l', 'display'), '基础规则里没写死 l 栏 display（否则会压过媒体查询）');

/* 断点顺序：媒体查询必须写在基础规则之后（同优先级靠源码顺序取胜） */
const gridRule = cssRuleList.find(r => !r.media && sels(r).includes('main') && /grid-template-columns/.test(r.body));
const mobRule = cssRuleList.find(r => MOB.test(r.media || ''));
mok(!!gridRule && !!mobRule && mobRule.order > gridRule.order, '手机媒体查询写在三栏网格之后（顺序对了才生效）');
const gridCols = gridRule ? (/grid-template-columns:\s*([^;]+)/.exec(gridRule.body) || [])[1] : null;
mok(!!gridCols && gridCols.trim().split(/\s+/).length === 3,
  '桌面端 main 仍是三栏网格', gridCols);
mok(cssRuleList.some(r => /max-width:\s*1200px/.test(r.media || '') && sels(r).includes('main')),
  '1200px 断点收窄三栏（窄窗口不挤压详情）');

/* 底部导航 */
const navStart = html.indexOf('<nav class="mnav"');
const navHtml = navStart < 0 ? '' : html.slice(navStart, html.indexOf('</nav>', navStart));
mok(navHtml.length > 30, '模板里有 <nav class="mnav">');
mok(['l', 'c', 'r'].every(p => navHtml.includes(`@click="mpane='${p}'"`)), '底部导航能切三栏',
  navHtml.match(/@click="[^"]+"/g));
mok(cssEq('.mnav', 'display', 'none'), '桌面端隐藏底部导航');
mok(cssEq('.mnav', 'display', 'flex', MOB), '手机端显示底部导航');
mok(cssRuleList.some(r => r.sel === '.mnav' && /safe-area-inset-bottom/.test(r.body)), '底部导航为刘海屏留安全区');
/* --- 5b) 触控目标 / 溢出 / app.js 状态 --- */

/* 触控目标 + iOS 输入框不放大 */
const minH = sel => { const v = cssDecl(sel, 'min-height', MOB); return v ? parseFloat(v.value) || 0 : 0; };
mok(minH('.item-row') >= 44, '列表行高 ≥44px', minH('.item-row'));
/* 底部导航只在手机端出现，尺寸写在基础规则里也算数 */
const navBtnH = Math.max(minH('.mnav button'), parseFloat((cssDecl('.mnav button', 'min-height') || {}).value) || 0);
mok(navBtnH >= 44, '底部导航按钮 ≥44px', navBtnH);
mok(minH('.tabs button') >= 44, '页签按钮 ≥44px', minH('.tabs button'));
const inpFs = parseFloat((cssDecl('input', 'font-size', MOB) || {}).value) || 0;
mok(inpFs >= 16, '手机端输入框字号 ≥16px（iOS 聚焦不放大页面）', inpFs);
mok(/input,select,textarea\{font-size:18px\}/.test(html), '输入框字号写成表单控件统一规则');

/* 宽表格 / 浮层 */
const twCount = (tpl.match(/<div class="tw"><table>/g) || []).length;
const tableCount = (tpl.match(/<table>/g) || []).length;
mok(twCount > 0 && twCount === tableCount, `所有表格都包了 .tw（${twCount}/${tableCount}）`);
mok(cssEq('.tw', 'overflow-x', 'auto'), '.tw 自己横向滚动');
const toastBottom = (cssDecl('.toast', 'bottom', MOB) || {}).value || '';
mok(/calc\(/.test(toastBottom) && /safe-area-inset-bottom/.test(toastBottom), '手机端提示条抬到底部导航之上', toastBottom);
mok(cssEq('.err', 'max-width', 'none', MOB), '手机端错误条不再限制 46vw');
mok(cssEq('header .meta', 'display', 'none', MOB), '手机端隐藏头部统计文字（不挤压标题与按钮）');
mok(/@supports \(height:100dvh\)\{html,body\{height:100dvh\}\}/.test(html),
  '用 100dvh 跟随移动端工具栏（避免底部被顶出屏幕）');
mok(cssRuleList.some(r => MOB.test(r.media || '') && /safe-area-inset-(left|right)/.test(r.body)),
  '横屏刘海屏的左右安全区');

/* app.js 侧的状态 */
mok(/\bmpane:\s*'l'/.test(appSrc) && /\bisMobile:\s*false/.test(appSrc), 'app.js data 里有 mpane / isMobile');
mok(/syncMobile\(\)/.test(appSrc) && /addEventListener\('resize', this\.syncMobile\)/.test(appSrc),
  'app.js 跟随视口宽度更新 isMobile');
const showDetailCalls = (appSrc.match(/this\.showDetail\(\)/g) || []).length;
mok(showDetailCalls === 3, '技能 / 类型 / 职业路线三处都会 showDetail()（手机端自动跳详情）', showDetailCalls);
/* --- 5c) 渲染出的 vnode --- */
function findVNode(node, pred, out = []) {
  if (node == null || typeof node !== 'object') return out;
  if (Array.isArray(node)) { for (const x of node) findVNode(x, pred, out); return out; }
  if (pred(node)) out.push(node);
  if (node.children !== undefined) findVNode(node.children, pred, out);
  if (typeof node.slots === 'function') { try { findVNode(node.slots(), pred, out); } catch (e) {} }
  return out;
}
inst.mpane = 'c';
const vnodeMobile = render(ctxProxy, []);
inst.mpane = 'l';
const mainVNode = findVNode(vnodeMobile, n => n.type === 'main')[0];
mok(!!mainVNode && String((mainVNode.props || {}).class).includes('mp-c'),
  '渲染时 main 的 class 跟随 mpane', mainVNode && JSON.stringify((mainVNode.props || {}).class));
const navVNode = findVNode(vnodeMobile, n => n.type === 'nav')[0];
const navText = navVNode ? collectText(navVNode).join('|') : '';
mok(!!navVNode && /mnav/.test(String((navVNode.props || {}).class)) &&
  ['列表', '详情', '角色'].every(t => navText.includes(t)),
  '渲染出底部导航（列表 / 详情 / 角色）', navText);

if (mobFails.length) {
  console.error(`\n移动端适配审计失败 ${mobFails.length} 项：\n - ` + mobFails.join('\n - '));
  process.exit(7);
}
console.log(`移动端适配审计通过：${mobPass} 项（断点顺序 / 单栏切换 / 触控目标 / 溢出 / mpane 绑定）`);

