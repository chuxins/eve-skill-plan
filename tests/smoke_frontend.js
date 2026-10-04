/* 前端逻辑冒烟测试（Node + vm，无需浏览器 / jsdom）：
 * 在沙箱里加载 util.js + app.js（createApp 用桩捕获配置对象），把 data/computed/methods
 * 和挂到实例上的模板函数（app.config.globalProperties）组装成一个普通实例，
 * 然后对着**真实运行中的后端**跑一遍主要交互流程。
 * 用法：node tests/smoke_frontend.js          （默认 http://127.0.0.1:8092/）
 *       BASE=http://127.0.0.1/eveskillplanner/ node tests/smoke_frontend.js   # 走 nginx
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const BASE = (process.env.BASE || 'http://127.0.0.1:8092/').replace(/\/?$/, '/');

let pass = 0;
const fails = [];
function ok(cond, label, extra) {
  if (cond) { pass++; console.log('  ✓', label); }
  else { fails.push(label); console.log('  ✗', label, extra === undefined ? '' : JSON.stringify(extra).slice(0, 200)); }
}
function eq(actual, expected, label) {
  ok(JSON.stringify(actual) === JSON.stringify(expected), label, { actual, expected });
}
function section(name) { console.log('\n== ' + name); }

/* ---------------- 浏览器环境桩 ---------------- */
let captured = null;
let published = null;      // app.js 挂到 app.config.globalProperties 上的模板函数
const VueStub = {
  createApp: opts => {
    captured = opts;
    published = {};
    return { config: { globalProperties: published }, mount: () => {} };
  },
};
const clicked = [];
/* 视口桩：matchMedia().matches 可切换，用来模拟手机 / 桌面两种宽度 */
const mq = { matches: false, media: '(max-width:900px)', addEventListener() {}, removeEventListener() {} };
const win = { matchMedia: () => mq, addEventListener() {}, removeEventListener() {}, innerWidth: 1280 };
let lastTxt = null;
let lastDownload = '';
const store = {};
const ls = new Proxy(store, {
  get(t, k) {
    if (k === 'getItem') return n => (n in t ? t[n] : null);
    if (k === 'setItem') return (n, v) => { t[n] = String(v); };
    if (k === 'removeItem') return n => { delete t[n]; };
    if (k === 'clear') return () => { for (const n of Object.keys(t)) delete t[n]; };
    if (k === 'key') return i => Object.keys(t)[i];
    if (k === 'length') return Object.keys(t).length;
    return t[k];
  },
  set(t, k, v) { t[k] = v; return true; },
  deleteProperty(t, k) { delete t[k]; return true; },
  has(t, k) { return k in t; },
  ownKeys(t) { return Reflect.ownKeys(t); },
  getOwnPropertyDescriptor(t, k) {
    return { value: t[k], enumerable: true, configurable: true, writable: true };
  },
});
class URL2 extends URL {
  static createObjectURL() { return 'blob:stub'; }
  static revokeObjectURL() {}
}
const fetchStub = async (p, o) => {
  const url = BASE + String(p).replace(/^\.?\//, '');
  const mocked = mockPlans(p, o);
  if (mocked) return mocked;
  const r = await globalThis.fetch(url, o);
  if (String(p).startsWith('api/plan/txt')) { try { lastTxt = await r.clone().text(); } catch (e) {} }
  return r;
};

/* SSO 隔离后，计划保存/载入/删除与角色列表/退出需要服务端会话；Node 里没有浏览器 Cookie
 * 也没有真实 EVE SSO，所以 /api/plans* 与 /api/characters* 这几个接口在测试里用进程内桩
 * 模拟「已登录」的服务端契约（真实服务端的 401 鉴权与角色隔离由 tests/smoke_http.py 用
 * Flask test_client 逐条断言）。其余接口（含 /api/plan、/api/plan/txt）仍全部打真实后端。 */
let mockAuthed = false;              // 模拟「服务端会话里有登录角色」
const planStore = [];                // 桩里的「当前角色的已保存计划」
let nextPlanId = 1;
function jsonResp(obj, status = 200) {
  const body = JSON.stringify(obj);
  return {
    status, ok: status < 400,
    headers: { get: () => 'application/json' },
    json: async () => JSON.parse(body),
    text: async () => body,
    clone() { return jsonResp(obj, status); },
  };
}
function mockPlans(p, o) {
  const method = (o && o.method) || 'GET';
  const url = String(p);
  if (url === 'api/plans' && method === 'GET') {
    return jsonResp({ plans: planStore, authed: mockAuthed,
      login_cid: mockAuthed ? 1234567 : null, login_name: mockAuthed ? '冒烟测试角色' : '' });
  }
  if (url === 'api/plans' && method === 'POST') {
    const body = JSON.parse(o.body);
    if (body.plan_id) {
      const p0 = planStore.find(x => x.plan_id === body.plan_id);
      if (!p0) return jsonResp({ error: '没有这个计划：' + body.plan_id }, 404);
      p0.name = body.name; p0.targets = body.targets || p0.targets;
      return jsonResp({ plan: p0 });
    }
    const plan = { plan_id: nextPlanId++, name: body.name, character_id: body.character_id || null,
      targets: body.targets || [], updated_at: '2026-10-02 12:00:00' };
    planStore.push(plan);
    return jsonResp({ plan });
  }
  const m = url.match(/^api\/plans\/(\d+)(?:\/(run))?$/);
  if (m) {
    const pid = Number(m[1]);
    const plan = planStore.find(x => x.plan_id === pid);
    if (m[2] === 'run') {
      if (!plan) return jsonResp({ error: '没有这个计划：' + pid }, 404);
      return globalThis.fetch(BASE + 'api/plan', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ targets: plan.targets, current: {}, attrs: {}, options: {} }),
      }).then(r => r.json()).then(pd => jsonResp(Object.assign(pd, {
        name: plan.name, targets: plan.targets })));
    }
    if (method === 'DELETE') {
      const i = planStore.findIndex(x => x.plan_id === pid);
      if (i < 0) return jsonResp({ deleted: false });
      planStore.splice(i, 1);
      return jsonResp({ deleted: true });
    }
    if (!plan) return jsonResp({ error: '没有这个计划：' + pid }, 404);
    return jsonResp({ plan });
  }
  if (url === 'api/characters' && method === 'GET') {
    if (mockAuthed) {
      return jsonResp({ characters: [{ id: 1234567, name: '冒烟测试角色', scopes: [], can_read_skills: false }],
        login: { cid: 1234567, name: '冒烟测试角色' } });
    }
    return jsonResp({ characters: [], login: null });
  }
  const cd = url.match(/^api\/characters\/(\d+)$/);
  if (cd && method === 'DELETE') return jsonResp({ logged_out: true, id: Number(cd[1]) });
  const ov = url.match(/^api\/characters\/(\d+)\/overview$/);
  if (ov && method === 'GET') {
    if (mockAuthed && Number(ov[1]) === 1234567) {
      return jsonResp({ id: 1234567, name: '冒烟测试角色', skills: { 3327: 5 }, total_sp: 15000000,
        attributes: { charisma: 17, intelligence: 17, memory: 17, perception: 17, willpower: 17 }, queue: [] });
    }
    return jsonResp({ error: '未登录无法读取该角色的技能' }, 401);
  }
  return null;                       // 未命中 → 走真实后端
}

const sandbox = {
  console,
  window: win,
  fetch: fetchStub,
  Vue: VueStub,
  location: { search: '', href: '' },
  history: { replaceState: () => {} },
  localStorage: ls,
  URL: URL2,
  URLSearchParams,
  setTimeout, clearTimeout,
  confirm: () => true,
  alert: () => {},
  document: { createElement: tag => {
    const el = { tag, href: '', click() { clicked.push(tag); } };
    Object.defineProperty(el, 'download', { get: () => lastDownload, set: v => { lastDownload = v; } });
    return el;
  } },
};
vm.createContext(sandbox);

const src = ['static/util.js', 'static/app.js']
  .map(f => fs.readFileSync(path.join(ROOT, f), 'utf8')).join('\n') +
  '\n;globalThis.__util = { API, API_POST, n, fmtDur, fmtShort, remain, fmtWhen, fmtWhen2,' +
  ' fmtBJ, fmtBJFull, fmtBJAt, spToLevel, trainSeconds,' +
  ' attrName, kindCN, lvRoman, toLevel, cacheGet, cacheSet, cacheDel, clearCharCache, icon };\n';
vm.runInContext(src, sandbox, { filename: 'frontend-bundle.js' });
const U = sandbox.__util;

if (!captured) { console.error('没有捕获到 createApp 的配置对象'); process.exit(2); }
const opts = captured;

/* data / computed / methods + 模板函数 → 普通实例 */
const inst = opts.data.call({});
for (const [k, fn] of Object.entries(opts.computed || {})) {
  Object.defineProperty(inst, k, { get: () => fn.call(inst), configurable: true });
}
Object.assign(inst, opts.methods, published);

/* ---------------- 流程测试 ---------------- */
(async () => {
  console.log('后端：' + BASE);

  section('工具函数');
  eq(U.fmtDur(3 * 86400 + 4 * 3600 + 60), '3天4小时', 'fmtDur 天+小时');
  eq(U.fmtDur(4 * 3600 + 12 * 60), '4小时12分', 'fmtDur 小时+分');
  eq(U.fmtDur(750), '12分30秒', 'fmtDur 分+秒');
  eq(U.fmtShort(90061), '1d1h1m1s', 'fmtShort 紧凑');
  eq(U.lvRoman(5), 'Ⅴ', 'lvRoman');
  eq(U.toLevel(3), '到 Ⅲ 级', 'toLevel');
  eq(U.n(12345), '1.2万', 'n 万');
  eq(U.attrName('perception'), '感知', 'attrName');
  eq(U.kindCN('career'), '职业', 'kindCN');
  ok(U.icon(638).includes('/types/638/icon'), 'icon 地址');

  section('模板函数发布（模板里直接调用，必须挂到实例上）');
  ok(typeof published.attrName === 'function' && typeof published.n === 'function',
    'attrName / n 已挂到 globalProperties', Object.keys(published || {}));
  ok(['fmtDur', 'fmtShort', 'fmtWhen', 'fmtWhen2', 'remain', 'kindCN', 'lvRoman', 'toLevel', 'icon']
    .every(k => typeof published[k] === 'function'), '其余模板函数也都在', Object.keys(published || {}));
  ok(['fmtBJ', 'fmtBJFull', 'fmtBJAt'].every(k => typeof published[k] === 'function'),
    '北京时间格式化函数已挂到 globalProperties', Object.keys(published || {}));
  ok(inst.attrName('perception') === '感知', '实例上取到的 attrName 可用');

  section('启动 boot()');
  await inst.boot();
  ok(inst.meta && inst.meta.counts.skills > 400, 'api/meta 返回技能数', inst.meta && inst.meta.counts);
  ok(Object.keys(inst.attrs).length === 5 && Object.values(inst.attrs).every(v => v > 0),
    '五维为正数（有角色时来自 ESI）', inst.attrs);
  ok(inst.groups.length > 5, '技能组已加载', inst.groups.length);
  eq(inst.careers.length, 40, '职业路线 40 条');
  eq(inst.saved.length, 0, '已保存计划初始为空');
  eq(inst.loginCid, null, '启动时未登录（无会话 Cookie → loginCid 为 null）');
  eq(inst.loggedIn, false, 'loggedIn 初始为 false（未登录不能保存）');
  ok(inst.levels.length >= 5 && inst.cats.length > 5, 'levels / cats 来自 meta', [inst.levels, inst.cats.length]);
  ok(inst.attrsText.includes('感知'), 'attrsText 渲染');
  const hasChar = inst.chars.length > 0;
  if (hasChar) {
    eq(inst.charName, inst.cname, 'charName 显示角色名');
    ok(Object.keys(inst.current).length > 100, '角色技能表已加载', Object.keys(inst.current).length);
    ok(inst.totalSp > 1e6, '总技能点已加载', inst.totalSp);
    ok(inst.queue === null || Array.isArray(inst.queue), '技能队列字段可用', inst.queue && inst.queue.length);
    eq(Number(inst.cid), Number(inst.chars[0].id), 'cid 自动选中首个角色');
  } else {
    eq(inst.chars, [], '暂无授权角色');
    eq(inst.charName, '未选角色', 'charName 兜底');
  }

  section('技能库');
  await inst.searchSkills();
  ok(inst.skills.length >= 400, '默认列出技能', inst.skills.length);
  await inst.openSkill(3300);
  eq(inst.skill.skill.name, '射击学', '打开技能 3300');
  ok(inst.skill.level_seconds['5'] > 0, '含 5 级耗时');
  ok(!inst.busy, 'busy 已复位');

  section('需求查询');
  inst.tq = '乌鸦';
  await inst.searchTypes();
  ok(inst.types.some(t => t.tid === 638), '搜索到乌鸦级', inst.types.slice(0, 3).map(t => t.name));
  await inst.openType(638);
  eq(inst.tinfo.type.name, '乌鸦级', '打开类型 638');
  eq(inst.tinfo.skills, 6, '乌鸦级需求技能数');
  if (inst.cid) ok(typeof inst.tinfo.missing === 'number' && inst.tinfo.missing <= 6, '有角色时给出缺口数', inst.tinfo.missing);
  else eq(inst.tinfo.missing, inst.tinfo.skills, '未选角色时缺口按「从零」算');
  // 页面上「缺 X / Y 项」标签只在选了角色时才渲染（模板 v-if="cid"），与上面口径一致
  const html = fs.readFileSync(path.join(ROOT, 'static/index.html'), 'utf8');
  ok(/v-if="cid">\s*缺 \{\{tinfo\.missing\}\}/.test(html), '模板里缺口标签受 cid 控制');

  section('职业路线');
  await inst.openCareer(21);
  eq(inst.career.career.name, '加达里财富猎手', '打开职业路线 21');
  ok(inst.career.career.milestones.length === 5, '里程碑 5 个', inst.career.career.milestones.length);
  ok(inst.career.career.milestones.every(m => m.name), '里程碑都带技能名', inst.career.career.milestones[0]);
  ok(inst.career.requirements.length >= 29, '含全部需求技能', inst.career.requirements.length);

  section('移动端适配（单栏切换 + 自动跳详情）');
  ok(inst.mpane === 'l' && inst.isMobile === false, '初始：列表栏 + 桌面态', [inst.mpane, inst.isMobile]);
  mq.matches = true;
  inst.syncMobile();
  ok(inst.isMobile === true, 'syncMobile() 认出窄屏（≤900px）');
  inst.mpane = 'l';
  await inst.openSkill(3300);
  eq(inst.mpane, 'c', '手机端点技能自动跳到详情栏');
  inst.mpane = 'l';
  await inst.openType(638);
  eq(inst.mpane, 'c', '手机端点类型自动跳到详情栏');
  inst.mpane = 'l';
  await inst.openCareer(21);
  eq(inst.mpane, 'c', '手机端点职业路线自动跳到详情栏');
  mq.matches = false;
  inst.syncMobile();
  inst.mpane = 'l';
  await inst.openSkill(3300);
  ok(inst.isMobile === false && inst.mpane === 'l', '桌面态不切栏（三栏本来就同时可见）', inst.mpane);
  /* 模板与样式里的移动端零件（布局细节见 check_template.js 的移动端审计） */
  const mnavHtml = html.slice(html.indexOf('<nav class="mnav"'));
  ok(/@media \(max-width:900px\)/.test(html) && mnavHtml.length > 30 &&
    ['l', 'c', 'r'].every(p => mnavHtml.includes(`@click="mpane='${p}'"`)),
    '模板底部导航 + 900px 断点都在', mnavHtml.slice(0, 40));

  section('计划：加目标 → 复算');
  inst.addTypeTarget();
  eq(inst.targets.length, 1, '目标 +1');
  eq(inst.tab, 'plan', '自动切到计划页');
  await inst.recalc();
  eq(inst.rows.length, inst.summary.skills_missing, '默认 rows = 缺口技能数');
  eq(inst.rowsDone.length, 0, '默认不含已掌握技能');
  eq(inst.rowsTodo.length, inst.summary.skills_missing, 'skills_missing = rowsTodo');
  ok(Array.isArray(inst.plan.steps) && inst.plan.steps.length > 0, '计划含逐级 steps');
  ok(inst.plan.steps.length >= inst.plan.rows.length, 'steps ≥ rows（逐级展开）',
    [inst.plan.steps.length, inst.plan.rows.length]);
  ok(inst.plan.steps.every(s => s.level >= 1 && s.seconds > 0), '每步有等级与本步时长');
  ok(inst.plan.steps.every((s, i, a) => i === 0 || s.end_seconds >= a[i - 1].end_seconds),
    'steps 累计完成时间单调');
  eq(inst.plan.steps[0].start_seconds, 0, '计划第 1 步从「现在」开始（start_seconds=0）');
  ok(inst.plan.steps.every((s, i, a) => i === 0 || Math.abs(s.start_seconds - a[i - 1].end_seconds) < 0.05),
    '每步从「上一级练完的时刻」开始（时间点首尾相接）',
    inst.plan.steps.slice(0, 3).map(s => [s.start_seconds, s.end_seconds]));
  ok(inst.plan.rows.every((r, i, a) => i === 0 || Math.abs(r.start_seconds - a[i - 1].end_seconds) < 0.05),
    'rows 同样首尾相接（已满足的行不占用时间）',
    inst.plan.rows.map(r => [r.name, r.start_seconds, r.end_seconds]));
  /* 目标卡片：练完时间点 = 该目标名下最晚的累计完成时间（从「现在」起算，北京时间） */
  const tv = inst.targetsView;
  eq(tv.length, 1, 'targetsView 与目标数一致');
  const mine = inst.plan.rows.filter(r => (r.targets || []).includes(tv[0].tid));
  eq(tv[0].end_seconds, Math.max.apply(null, mine.map(r => r.end_seconds)),
    '目标练完时间 = 名下最晚的累计完成时间');
  eq(tv[0].at, U.fmtBJ(tv[0].end_seconds), '目标练完时间走北京时间');
  eq(tv[0].done, false, '目标还有缺口 → 未满足（显示「预计 … 练完」）');
  eq(inst.plan.steps[inst.plan.steps.length - 1].end_seconds, inst.summary.seconds,
    '最后一步完成时间 = 总时长');
  ok(inst.summary.skills_total === inst.summary.skills_missing + inst.summary.skills_owned,
    'skills_total = 缺 + 已掌握', [inst.summary.skills_total, inst.summary.skills_missing, inst.summary.skills_owned]);
  eq(inst.summary.skills_total, 6, '乌鸦级需求共 6 项');
  ok(inst.summary.skills_missing <= 6, '缺口不超过 6 项', inst.summary.skills_missing);
  ok(/天|小时|分|秒/.test(inst.summary.duration), '总时长为中文时长', inst.summary.duration);
  ok(inst.plan.rows.every((r, i, a) => i === 0 || r.start_seconds >= a[i - 1].start_seconds), '行按开始时间有序');
  ok(inst.planName.startsWith(inst.charName + ' · '), '自动生成计划名', inst.planName);

  // include_owned：把已掌握的技能也列出来
  inst.opts.include_owned = true;
  await inst.recalc();
  eq(inst.rows.length, inst.summary.skills_total, 'include_owned 时 rows = 全部需求');
  eq(inst.rowsDone.length, inst.summary.skills_owned, '含已掌握行');
  delete inst.opts.include_owned;
  await inst.recalc();

  // 把角色清空 → 从零开始算，结果必须确定
  const savedCtx = { cid: inst.cid, cname: inst.cname, current: inst.current };
  inst.cid = null; inst.cname = ''; inst.current = {}; inst.planName = '';
  await inst.recalc();
  eq(inst.summary.skills_missing, 6, '无角色（从零）时 6 项待训');
  eq(inst.rows.length, 6, '无角色时 rows 6 行');
  eq(inst.planName, '未选角色 · 乌鸦级', '无角色时计划名');

  inst.addSkillTarget({ tid: 3300, name: '射击学', name_en: 'Gunnery', group: '射击学' }, 4);
  eq(inst.targets.length, 2, '目标 +1（技能）');
  await inst.recalc();
  eq(inst.summary.skills_total, 7, '两个目标合并后 7 项');
  eq(inst.targets[1].level, 4, '技能目标带等级');
  const dup = inst.targets.length;
  inst.addTypeTarget();
  eq(inst.targets.length, dup, '重复目标被拦截');
  ok(inst.toast.includes('已在目标列表'), '重复目标有提示', inst.toast);

  // 换回启动时的角色上下文：有角色则缺口应小于从零；token 目录为空时只能维持从零口径
  Object.assign(inst, savedCtx);
  await inst.recalc();
  if (hasChar) ok(inst.summary.skills_missing < 7, '有角色时缺口 < 从零', inst.summary.skills_missing);
  else eq(inst.summary.skills_missing, 7, '未授权角色时维持从零 7 项');
  ok(inst.summary.skills_missing === inst.rowsTodo.length, 'rowsTodo 与 skills_missing 一致');

  section('计划：保存 → 载入 → 导出 → 删除（SSO 隔离）');
  const beforeTotal = inst.plan.summary.skills_total;

  // 未登录：前端拦截 + 真实服务端 401（双保险）
  await inst.savePlan();
  ok(inst.err.includes('未登录无法保存'), '未登录点保存被前端拦截并提示登录', inst.err);
  eq(inst.saved.length, 0, '未登录不能保存（saved 为空）');
  const anon = await (await globalThis.fetch(BASE + 'api/plans', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(inst.query()),
  })).json();
  ok(anon.error && anon.error.includes('未登录无法保存'), '未登录直连保存被真实服务端 401 拒绝', anon);

  // 模拟 SSO 登录（真实场景由 OAuth 回调写入会话，前端经 api/characters 的 login 字段读到）
  mockAuthed = true;
  inst.loginCid = 1234567;
  inst.loginName = '冒烟测试角色';
  ok(inst.loggedIn === true, 'loggedIn 计算属性随会话状态翻转');
  await inst.savePlan();
  eq(inst.saved.length, 1, '登录后已保存 1 条');
  ok(inst.saved[0].plan_id && inst.saved[0].name, '列表项含 plan_id 与名称', inst.saved[0]);
  const pid = inst.saved[0].plan_id;
  inst.clearTargets();
  eq(inst.targets.length, 0, '清空目标');
  eq(inst.plan, null, '清空后计划为空');
  await inst.runSaved(pid, false);
  eq(inst.targets.length, 2, '载入计划恢复 2 个目标');
  eq(inst.plan.summary.skills_total, beforeTotal, '载入复算结果与保存时一致');
  eq(inst.planName, inst.saved[0].name, '计划名回填');
  // 导出按「从零」口径（不依赖测试角色已掌握多少技能）：先把 plan 对齐到同口径，再断言逐级行数 = steps
  const savedCurrent = inst.current;
  inst.current = {};
  await inst.recalc();
  await inst.exportTxt();
  inst.current = savedCurrent;
  const txtLines = lastTxt.split('\n').filter(l => l.trim());
  ok(txtLines.every(l => l.startsWith('<localized hint="') && /<\/localized> [1-5]$/.test(l)),
    '每行格式 <localized hint="英文">中文*</localized> 等级', txtLines[0]);
  ok(txtLines.length === inst.plan.steps.length, 'TXT 逐级一行（行数 = steps）',
    [txtLines.length, inst.plan.steps.length]);
  ok(inst.plan.rows.every(r => lastTxt.includes(r.name)), 'TXT 含每个技能名');
  ok(clicked.includes('a'), '触发了下载点击');
  ok(lastDownload.endsWith('.txt') && lastDownload.startsWith(
    inst.planName.replace(/[\\/:*?"<>|]/g, '-')), '导出文件名用已保存计划名', lastDownload);
  const txtBody = await (await globalThis.fetch(BASE + 'api/plan/txt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(inst.query()),
  })).text();
  ok(txtBody.startsWith('<localized hint="'), 'TXT 直接以 <localized 开头（无 BOM / 表头）', txtBody.slice(0, 40));
  await inst.delSaved(pid);
  eq(inst.saved.length, 0, '删除后已保存计划为空');
  mockAuthed = false;                          // 复位「会话状态」桩

  section('角色隔离：登录只显示当前角色，退出登录清会话');
  mockAuthed = true;
  await inst.loadChars(1234567);               // 模拟「登录后重载角色列表」（服务端只返回登录角色）
  eq(inst.loginCid, 1234567, '登录后 loginCid 生效');
  eq(inst.chars.length, 1, '角色列表只含当前登录角色（无切换入口）', inst.chars);
  eq(Number(inst.chars[0].id), 1234567, '列表项即登录角色');
  eq(Number(inst.cid), 1234567, 'cid 自动选中当前登录角色');
  await inst.logout();                         // 退出登录：仅清会话 + 本地角色数据
  eq(inst.loginCid, null, '退出后 loginCid 为 null');
  eq(inst.chars, [], '退出后角色列表为空');
  eq(inst.cid, null, '退出后 cid 为空');
  eq(inst.charName, '未选角色', '退出后回到「未选角色」');
  eq(inst.current, {}, '退出后技能表清空');
  eq(U.cacheGet('cid', 1e9), null, '退出后本地 cid 缓存清除');
  mockAuthed = false;

  section('缓存与登录');
  U.cacheSet('cid', 123);
  eq(U.cacheGet('cid', 1e9), 123, 'cacheGet 命中');
  eq(U.cacheGet('cid', 0), null, 'cacheGet 过期');
  inst.login();
  await new Promise(r => setTimeout(r, 600));
  ok(sandbox.location.href.includes('login.eveonline.com') || !!inst.err, 'login() 拿到 SSO 地址或报错',
    sandbox.location.href || inst.err);
  sandbox.location.href = '';

  section('北京时间 / 训练队列按属性复算 / 技能等级三态');
  /* 北京时间 = UTC+8，与浏览器所在时区无关 */
  const _bj = new Date(Date.now() + 8 * 3600 * 1000);
  const _p2 = x => String(x).padStart(2, '0');
  const _bjStr = ms => `${_p2(ms.getUTCMonth() + 1)}-${_p2(ms.getUTCDate())} ${_p2(ms.getUTCHours())}:${_p2(ms.getUTCMinutes())}`;
  eq(U.fmtBJ(0), _bjStr(_bj), 'fmtBJ(0) = 此刻北京时间');
  eq(U.fmtBJ(3600), _bjStr(new Date(_bj.getTime() + 3600e3)), 'fmtBJ(3600) = 一小时后的北京时间');
  ok(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(U.fmtBJFull(90)), 'fmtBJFull 带年份', U.fmtBJFull(90));
  eq(U.fmtBJAt('2026-10-04T12:00:00Z'), '10-04 20:00', 'fmtBJAt：12:00Z = 北京 20:00');
  eq(U.fmtBJAt('not-a-date'), '—', 'fmtBJAt 非法输入兜底');

  /* 本地训练时长公式必须与后端逐级时间同口径（/api/skill/3300 无角色 → 默认 17 点） */
  const s3300 = await U.API('api/skill/3300');
  const _lp = s3300.skill.primary, _ls = s3300.skill.secondary, _lr = s3300.skill.rank;
  const tl5 = s3300.level_seconds['5'];
  const loc5 = U.trainSeconds(_lr, _lp, _ls, s3300.attributes, 0, 5);
  ok(Math.abs(loc5 - tl5) / tl5 < 0.02, '本地 trainSeconds 5 级 ≈ 后端 level_seconds[5]',
    [Math.round(loc5), tl5]);
  const tl34 = s3300.level_seconds['4'] - s3300.level_seconds['3'];
  ok(Math.abs(U.trainSeconds(_lr, _lp, _ls, s3300.attributes, 3, 4) - tl34) / tl34 < 0.02,
    '本地 trainSeconds 3→4 级 ≈ 后端逐级差');

  /* 未登录：属性固定默认 17，属性框 / 预设不可改 */
  eq(inst.canEditAttrs, false, '未登录不能改属性');
  ok(Object.values(inst.attrs).every(v => v === 17), '未登录属性固定默认 17', inst.attrs);

  /* 登录 + 队列：时间点按当前属性复算，改属性立即生效 */
  inst.loginCid = 1234567; inst.loginName = '冒烟测试角色';
  eq(inst.canEditAttrs, true, '登录后可改属性');
  inst.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 17, willpower: 17 };
  inst.queue = [
    { queue_position: 0, skill_id: 3300, skill_name: '射击学', finished_level: 5, rank: 1,
      primary: 'perception', secondary: 'willpower',
      start_date: new Date(Date.now() - 3600e3).toISOString(),
      finish_date: new Date(Date.now() + 3600e3).toISOString() },
    { queue_position: 1, skill_id: 3301, skill_name: '导弹发射器操作', finished_level: 4, rank: 1,
      primary: 'perception', secondary: 'willpower' },
  ];
  const qv = inst.queueView;
  eq(qv.length, 2, 'queueView 含 2 项');
  ok(qv[0].seconds > 0 && qv[1].end_seconds > qv[0].end_seconds, '队列按顺序累计完成时间',
    qv.map(q => Math.round(q.end_seconds)));
  ok(/^\d{2}-\d{2} \d{2}:\d{2}$/.test(qv[1].at), '队列时间点为北京时间 MM-DD HH:mm', qv[1].at);
  const qBefore = inst.queueView.map(q => q.end_seconds);
  inst.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 34, willpower: 34 };
  const qAfter = inst.queueView.map(q => q.end_seconds);
  ok(qAfter[1] < qBefore[1], '提高主/副属性 → 队列总用时立即变短',
    [Math.round(qBefore[1]), Math.round(qAfter[1])]);
  /* 「重读技能」= pickChar(cid)：属性复位成角色真实属性（桩里全 17），队列随之复位 */
  mockAuthed = true;
  await inst.pickChar(1234567, true);
  eq(inst.attrs, { charisma: 17, intelligence: 17, memory: 17, perception: 17, willpower: 17 },
    '「重读技能」把属性复位为角色真实属性', inst.attrs);
  eq(inst.queueView.length, 0, '「重读技能」后队列按新属性复位（桩返回空队列）');
  mockAuthed = false;

  /* 队列：正在训练的那一级按 ESI「剩余」时间算（不是整级总时长）；
   * 排队的整级按当前属性算整级时长；第 1 项从现在起算，之后接上一项练完的时刻。 */
  const _iso = ms => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');
  const _rate = 31 + 22 / 2;                       // ESI 用的属性 31/22 → 42 SP/分钟
  const _leftS = 3600;                             // ESI 说这一级还剩 1 小时
  const _esiMin = (1280000 - 797697) / _rate;      // 这一段（training_start_sp → level_end_sp）的分钟数
  const _t0 = Date.now(), _fin0 = _t0 + _leftS * 1000;
  const _sta0 = _fin0 - _esiMin * 60000;
  const _fin1 = _fin0 + 47.6 * 60000;              // 排队项：整级 2000 SP @42 → 47.6 分钟
  inst.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 31, willpower: 22 };
  inst.queue = [
    { queue_position: 0, skill_id: 3326, skill_name: '巡航导弹概论', finished_level: 5, rank: 5,
      primary: 'perception', secondary: 'willpower',
      // 带着已练进度开训：training_start_sp(797697) 远大于 level_start_sp(226275)
      level_start_sp: 226275, training_start_sp: 797697, level_end_sp: 1280000,
      start_date: _iso(_sta0), finish_date: _iso(_fin0) },
    { queue_position: 1, skill_id: 20212, skill_name: '导弹专业研究', finished_level: 1, rank: 8,
      primary: 'perception', secondary: 'willpower',
      level_start_sp: 0, training_start_sp: 0, level_end_sp: 2000,
      start_date: _iso(_fin0), finish_date: _iso(_fin1) },
  ];
  const qv1 = inst.queueView;
  const _full0 = U.trainSeconds(5, 'perception', 'willpower', inst.attrs, 4, 5);   // 整级总时长
  eq(qv1[0].training, true, '第 1 项 = 正在训练（training 标记）');
  ok(Math.abs(qv1[0].seconds - _leftS) < 2, '正在训练项按 ESI「剩余」时间（≈1 小时）',
    [Math.round(qv1[0].seconds), Math.round(_leftS)]);
  ok(qv1[0].seconds < _full0 / 10,
    '剩余时间远小于整级总时长 —— 回归：以前按「整级 × 时间进度」会把已练掉的部分也算进去',
    [Math.round(qv1[0].seconds), Math.round(_full0)]);
  eq(qv1[0].end_seconds, qv1[0].seconds, '第 1 项从「现在」起算（累计 = 本项用时）');
  eq(qv1[0].at, U.fmtBJAt(_iso(_fin0)), '正在训练项的完成点 = ESI finish_date 的北京时间');
  const _lvl1 = 2000 / _rate * 60;                 // 排队项整级时长（按当前属性）
  ok(Math.abs(qv1[1].seconds - _lvl1) < 1, '排队项 = 整级时长（按当前属性）',
    [Math.round(qv1[1].seconds), Math.round(_lvl1)]);
  ok(Math.abs(qv1[1].end_seconds - (_leftS + _lvl1)) < 3,
    '队列依次接上一项练完的时刻（第 2 项 = 第 1 项完成 + 整级时长）',
    qv1.map(q => Math.round(q.end_seconds)));
  /* 改属性：正在训练项只重算「还没练掉的 SP」那段（属性翻倍 → 用时减半）；排队项整级重算 */
  inst.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 62, willpower: 44 };
  ok(Math.abs(inst.queueView[0].seconds - _leftS / 2) < 3,
    '属性翻倍 → 训练中项的剩余用时减半（只折算剩余 SP）', Math.round(inst.queueView[0].seconds));
  inst.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 21, willpower: 17 };
  ok(inst.queueView[0].seconds > _leftS, '属性降低 → 训练中项的剩余用时变长（42 ÷ 29.5）',
    Math.round(inst.queueView[0].seconds));
  /* ESI 没给 SP 明细（旧缓存）→ 退回 ESI 的剩余时间 */
  inst.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 31, willpower: 22 };
  inst.queue = [{ queue_position: 0, skill_id: 3300, skill_name: '射击学', finished_level: 5, rank: 1,
    primary: 'perception', secondary: 'willpower',
    start_date: _iso(_t0 - 3600e3), finish_date: _iso(_t0 + 3600e3) }];
  ok(Math.abs(inst.queueView[0].seconds - 3600) < 5, '缺 ESI SP 明细时退回 finish_date 剩余时间',
    Math.round(inst.queueView[0].seconds));

  /* 技能详情等级三态：已学=learned（黑）/ 队列中=training（蓝）/ 未学=open（可加计划） */
  inst.skill = { skill: { tid: 3300, name: '射击学', rank: 1, primary: 'perception', secondary: 'willpower' },
    current: 3, level_seconds: {}, level_seconds_from_current: {}, prereqs: [], closure: [], consumers: [] };
  inst.queue = [{ queue_position: 0, skill_id: 3300, finished_level: 4, rank: 1,
    primary: 'perception', secondary: 'willpower' }];
  eq(inst.skillLvState(3), 'learned', '已学等级 = learned（黑、不可点）');
  eq(inst.skillLvState(4), 'training', '队列中等级 = training（蓝、不可点）');
  eq(inst.skillLvState(5), 'open', '未学等级 = open（可加入计划）');
  eq(inst.skillLvTitle(4), '正在训练队列中', '队列中等级的悬停提示');
  ok(inst.skillLvTitle(3).includes('不能再加入') && inst.skillLvTitle(5).includes('加入训练计划'),
    '已学 / 未学等级的悬停提示');

  console.log('\n通过 ' + pass + ' 项，失败 ' + fails.length + ' 项');
  if (fails.length) { console.log('失败项：\n - ' + fails.join('\n - ')); process.exit(1); }
  console.log('前端冒烟测试全部通过 ✅');
})().catch(e => { console.error('测试异常：', e); process.exit(3); });

