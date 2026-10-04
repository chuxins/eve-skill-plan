/* 移动端适配的真浏览器验证（无头 Chrome + 本地同源反代，无 npm 依赖）：
 *   1) 起一个本地 http 服务：/probe.html 由 tests/probe_mobile.html 现场提供，
 *      其余请求按原样转发到站点（BASE）——这样探针页与被测页面同源，才能读 iframe 里的
 *      computedStyle、量真实尺寸、模拟点击；
 *   2) 探针在一个 390×844 的 iframe 里加载真站点：断言手机端单栏布局、底部导航可切换、
 *      触控目标尺寸、输入框 ≥16px、没有横向溢出、点列表项自动跳详情；
 *   3) 再把 iframe 拉到 1280×800：三栏回来、底部导航隐藏；缩回手机宽度后单栏回来且
 *      仍能自动跳详情（说明 resize 监听把 isMobile 更新对了）。
 * 用法：node tests/check_mobile.js
 *       BASE=http://127.0.0.1/eveskillplanner/ node tests/check_mobile.js   # 走 nginx
 *       CHROME_BIN=/usr/bin/chromium node tests/check_mobile.js
 * 退出码：0 通过（含「跳过」）/ 1 有断言失败
 */
const fs = require('fs');
const http = require('http');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const BASE = (process.env.BASE || process.env.EVE_SKILL_PLAN_BASE || 'http://127.0.0.1:8092/');
const APP_PATH = new URL(BASE).pathname || '/';      // 站点部署前缀，例如 / 或 /eveskillplanner/
const CHROME_WINDOW = process.env.WINDOW_SIZE || '1200,1000';
const VTIME = process.env.VTIME || '60000';          // 无头 Chrome 的虚拟时间预算（毫秒）

/* 必须用异步 spawn：反代就在本进程里，spawnSync 会阻塞事件循环 → Chrome 请求永远等不到响应 */
function runChrome(args, timeoutMs) {
  return new Promise(resolve => {
    const p = spawn(chrome, args);
    let stdout = '', stderr = '';
    const timer = setTimeout(() => { try { p.kill('SIGKILL'); } catch (e) {} }, timeoutMs);
    p.stdout.on('data', d => { stdout += d; });
    p.stderr.on('data', d => { stderr += d; });
    p.on('error', e => { stderr += '\nspawn error: ' + e.message; });
    p.on('close', code => { clearTimeout(timer); resolve({ status: code, stdout, stderr }); });
  });
}

let pass = 0;
const fails = [];
function ok(cond, label, extra) {
  if (cond) { pass++; console.log('  ✓', label); }
  else { fails.push(label); console.log('  ✗', label, extra === undefined ? '' : String(extra).slice(0, 400)); }
}

/* ---------------- 找浏览器（与 check_browser.js 同一套候选顺序） ---------------- */
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
found.sort((a, b) => (b.includes('headless') ? 1 : 0) - (a.includes('headless') ? 1 : 0));
const chrome = found[0];
if (!chrome) {
  console.log('跳过移动端真浏览器验证：没找到 Chrome / chromium（可用 CHROME_BIN=/path/to/chrome 指定）');
  process.exit(0);
}

const PROBE = fs.readFileSync(path.join(__dirname, 'probe_mobile.html'), 'utf8');

/* ---------------- 同源反代：/probe.html 是本测试提供的探针，其余转发给站点 ---------------- */
const upstream = new URL(BASE);
const proxy = http.createServer((req, res) => {
  if (req.url.split('?')[0] === '/probe.html') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
    res.end(PROBE);
    return;
  }
  const pReq = http.request({
    hostname: upstream.hostname, port: upstream.port || 80,
    path: req.url, method: req.method,
    headers: Object.assign({}, req.headers, { host: upstream.host }),
  }, pRes => { res.writeHead(pRes.statusCode, pRes.headers); pRes.pipe(res); });
  pReq.on('error', e => { res.writeHead(502, { 'Content-Type': 'text/plain' }); res.end('proxy error: ' + e.message); });
  req.pipe(pReq);
});

function decode(s) {
  return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

(async () => {
  await new Promise(r => proxy.listen(0, '127.0.0.1', r));
  const port = proxy.address().port;
  const url = `http://127.0.0.1:${port}/probe.html?app=${encodeURIComponent(APP_PATH)}`;
  console.log('站点：' + BASE + '（部署前缀 ' + APP_PATH + '）');
  console.log('浏览器：' + chrome);
  console.log('探针：' + url);

  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'spl-mob-'));
  let dom = '', log = '', status = null;
  try {
    const r = await runChrome(['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
      '--window-size=' + CHROME_WINDOW, '--virtual-time-budget=' + VTIME,
      '--enable-logging=stderr', '--v=0', '--user-data-dir=' + profile, '--dump-dom', url], 240000);
    dom = r.stdout; log = r.stderr; status = r.status;
  } finally {
    fs.rmSync(profile, { recursive: true, force: true });
    proxy.close();
  }

  console.log('\n== 浏览器加载');
  ok(status === 0, '无头 Chrome 正常退出', status);
  const consoleLines = log.split('\n').filter(l => l.includes('CONSOLE'));
  const badLines = consoleLines.filter(l => /\b(TypeError|ReferenceError|SyntaxError|RangeError|Uncaught)\b/.test(l));
  ok(badLines.length === 0, '探针与被测页面的控制台都没有 JS 报错',
    badLines.length ? badLines.slice(0, 3).join(' | ') : consoleLines.length + ' 条普通日志');

  const m = /<pre id="result">([\s\S]*?)<\/pre>/.exec(dom);
  ok(!!m, '取到探针结果 #result');
  const lines = m ? decode(m[1]).split('\n').map(s => s.trim()).filter(Boolean) : [];
  const done = lines.includes('DONE');
  ok(done, '探针跑完（有 DONE 结尾）', lines.slice(-3).join(' | '));

  console.log('\n== 探针结果（390×844 → 1280×800 → 390×844）');
  const probePass = lines.filter(l => l.startsWith('PASS '));
  const probeFail = lines.filter(l => l.startsWith('FAIL '));
  for (const l of probePass) console.log('  ✓', l.slice(5));
  for (const l of probeFail) console.log('  ✗', l.slice(5));
  ok(probePass.length >= 22, '探针跑完的检查项数量正常', probePass.length);
  ok(probeFail.length === 0, '探针没有失败项', probeFail.length ? probeFail.slice(0, 3).join(' | ') : '');

  console.log('');
  if (fails.length) {
    console.log(`通过 ${pass} 项，失败 ${fails.length} 项（其中探针内部失败 ${probeFail.length} 项）`);
    console.log('移动端真浏览器验证失败 ❌');
    process.exit(1);
  }
  console.log(`通过 ${pass} 项，失败 0 项（探针 ${probePass.length} 项全绿）`);
  console.log('移动端真浏览器验证通过 ✅');
})();
