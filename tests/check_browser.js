/* 真浏览器冒烟（无头 Chrome，无 npm 依赖；本机没装浏览器就跳过）：
 *   1) 控制台里不能有 JS 报错（TypeError / ReferenceError / SyntaxError …）——
 *      Vue 渲染失败的信号就是 "TypeError: xxx is not a function"，只看 stderr 就能抓到；
 *   2) 首页必须真的渲染出来：DOM 里要有中文界面与 5 维属性名（属性名来自模板里的
 *      attrName(k)，正是「模板函数没挂到实例上」这类 bug 会炸掉的地方）；
 *   3) 桌面宽度下 main 是「当前栏位」class 的宿主、底部导航存在但被 CSS 隐藏。
 * 手机布局（390×844 真视口、单栏切换、触控目标）由 tests/check_mobile.js 负责。
 * 用法：node tests/check_browser.js
 *       BASE=http://127.0.0.1/skills/ node tests/check_browser.js      # 走 nginx
 *       EVE_SKILL_PLAN_BASE=http://127.0.0.1:8091/ node tests/check_browser.js
 *       CHROME_BIN=/usr/bin/chromium node tests/check_browser.js
 *       WINDOW_SIZE=390,844 node tests/check_browser.js                # 换个视口再看一遍
 * 退出码：0 通过（含「跳过」）/ 1 有断言失败
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const BASE = (process.env.BASE || process.env.EVE_SKILL_PLAN_BASE || 'http://127.0.0.1:8091/');
/* 视口：默认桌面宽度（三栏布局）；WINDOW_SIZE=390,844 可以按手机宽度再看一遍 */
const WINDOW_SIZE = process.env.WINDOW_SIZE || '1280,900';

let pass = 0;
const fails = [];
function ok(cond, label, extra) {
  if (cond) { pass++; console.log('  ✓', label); }
  else { fails.push(label); console.log('  ✗', label, extra === undefined ? '' : String(extra).slice(0, 300)); }
}

/* ---------------- 找浏览器 ---------------- */
function candidates() {
  const out = [];
  if (process.env.CHROME_BIN) out.push(process.env.CHROME_BIN);
  const roots = [path.join(os.homedir(), '.cache/puppeteer'), '/usr/bin', '/usr/local/bin', '/opt'];
  const names = new Set(['chrome-headless-shell', 'chrome', 'chromium', 'chromium-browser', 'google-chrome', 'google-chrome-stable']);
  const walk = (dir, depth) => {
    if (depth > 4) return;
    let ents;
    try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (e) { return; }
    for (const e of ents) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p, depth + 1);
      else if (names.has(e.name)) out.push(p);
    }
  };
  roots.forEach(r => walk(r, 0));
  return out.filter(p => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch (e) { return false; } });
}

const found = candidates();
/* headless shell 的 --dump-dom 最稳定，优先用它 */
found.sort((a, b) => (b.includes('headless') ? 1 : 0) - (a.includes('headless') ? 1 : 0));
const chrome = found[0];

if (!chrome) {
  console.log('跳过真浏览器冒烟：没找到 Chrome / chromium（可用 CHROME_BIN=/path/to/chrome 指定）');
  process.exit(0);
}

console.log('站点：' + BASE);
console.log('浏览器：' + chrome);

const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'spl-chrome-'));
let dom = '', log = '', status = null;
try {
  const r = spawnSync(chrome, ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--window-size=' + WINDOW_SIZE,
    '--virtual-time-budget=10000', '--enable-logging=stderr', '--v=0',
    '--user-data-dir=' + profile, '--dump-dom', BASE],
    { encoding: 'utf8', timeout: 120000, maxBuffer: 64 * 1024 * 1024 });
  dom = r.stdout || '';
  log = r.stderr || '';
  status = r.status;
} finally {
  fs.rmSync(profile, { recursive: true, force: true });
}

console.log('\n== 浏览器加载');
ok(status === 0, '无头 Chrome 正常退出', status);
ok(dom.length > 2000, '拿到渲染后的 DOM', dom.length + ' 字节');

console.log('\n== 控制台');
const consoleLines = log.split('\n').filter(l => l.includes('CONSOLE'));
const badLines = consoleLines.filter(l => /\b(TypeError|ReferenceError|SyntaxError|RangeError|Uncaught)\b/.test(l));
ok(badLines.length === 0, '控制台没有 JS 报错',
  badLines.length ? badLines.slice(0, 3).join(' | ') : consoleLines.length + ' 条普通日志');
ok(!/is not a function/.test(log), '没有 "xxx is not a function"（模板函数缺失的典型症状）');

console.log('\n== 渲染结果');
for (const mark of ['EVE 技能规划', '技能库', '需求查询', '职业路线', '角色与属性', '快速开始'])
  ok(dom.includes(mark), 'DOM 含「' + mark + '」');
/* 5 维属性名是模板里 attrName(k) 渲染出来的：这一组能直接证明模板函数可用 */
const attrNames = ['魅力', '智力', '记忆', '感知', '毅力'];
ok(attrNames.every(k => dom.includes(k)), 'DOM 含 5 维属性名（attrName(k) 渲染成功）',
  attrNames.filter(k => !dom.includes(k)).join(', '));
ok(/\?v=[0-9a-f]{6,}/.test(dom), '静态资源带版本号（缓存不会用到旧 JS）');

console.log('\n== 移动端零件（布局细节见 check_mobile.js）');
ok(/class="mp-l"/.test(dom), 'main 带「当前栏位」class（mpane 绑定到了 DOM）');
ok(/<nav class="mnav"/.test(dom), '底部导航在 DOM 里（桌面宽度下被 CSS 隐藏）');
ok(['列表', '详情', '角色'].every(t => dom.includes(t)), '底部导航三个按钮标签都在');

console.log('');
if (fails.length) {
  console.log(`通过 ${pass} 项，失败 ${fails.length} 项`);
  console.log('真浏览器冒烟失败 ❌');
  process.exit(1);
}
console.log(`通过 ${pass} 项，失败 0 项`);
console.log('真浏览器冒烟通过 ✅');
