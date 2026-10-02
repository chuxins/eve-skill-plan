/* 纯工具函数与 API 封装（无 Vue 依赖；须在 app.js 之前加载）
 * 与 /root/eve-skill-planner 保持同一套约定：
 * - API() 统一 fetch，非 2xx 抛出后端 error 字段，UI 直接 alert
 * - 静态资源版本号由 webapp.py 注入 ?v={{ASSET_VERSION}}
 * - localStorage 前缀 spl_（与装配站同源，因此「当前角色」等偏好互通）
 * - 模板（index.html 的 #app）里要直接调用的函数，还必须在 app.js 里挂到实例上
 *   （见 app.js 末尾的 globalProperties 一段）：Vue 的模板渲染代理 has() 只放行
 *   Infinity/Math/Date/JSON 等白名单全局，其余自由标识符会被当成实例属性取成
 *   undefined → 调用即 TypeError: xxx is not a function
 * 路径一律用相对路径（api/…、static/…）：本站挂在 nginx 的 /skills/ 子路径下，
 * nginx 会把前缀剥掉再转给 Flask，浏览器侧则靠相对路径自然带上 /skills/。
 */

const API = (p, o) => fetch(p, o).then(async r => {
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || r.status);
  return d;
});
const API_POST = (p, body) => API(p, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {})
});

/* 类型图标（EVE 图片服务） */
const icon = tid => `https://images.evetech.net/types/${tid}/icon?size=32`;
const render64 = tid => `https://images.evetech.net/types/${tid}/render?size=64`;
const isShip = catId => catId === 6;

/* localStorage 缓存（带 TTL，前缀 spl_） */
function cacheGet(k, ttl) { try { const v = JSON.parse(localStorage.getItem('spl_' + k)); return v && Date.now() - v.ts < ttl ? v.data : null; } catch (e) { return null; } }
function cacheSet(k, d) { try { localStorage.setItem('spl_' + k, JSON.stringify({ ts: Date.now(), data: d })); } catch (e) {} }
function cacheDel(k) { try { localStorage.removeItem('spl_' + k); } catch (e) {} }
/* 退出登录后清缓存：传 cid 只清该角色的 sk_/fits_/isk_；不传则清全部角色相关键 */
function clearCharCache(cid) {
  const keep = cid ? String(cid) : null;
  try {
    Object.keys(localStorage).filter(k => k.startsWith('spl_')).forEach(k => {
      const s = k.slice(4);
      if (/^(sk|fits|isk)_/.test(s)) { if (!keep || s.endsWith('_' + keep)) localStorage.removeItem(k); }
      else if (!keep && (s === 'cid' || s === 'cname' || s === 'chars')) localStorage.removeItem(k);
    });
  } catch (e) {}
}

/* 数值格式化 */
function n(v) { if (v == null || isNaN(v)) return '—'; let s = Math.ceil(v * 10) / 10; if (Math.abs(s) >= 1e9) return (s / 1e9).toFixed(2) + 'B'; if (Math.abs(s) >= 1e6) return (s / 1e6).toFixed(1) + 'M'; if (Math.abs(s) >= 1e4) return (s / 1e4).toFixed(1) + '万'; return String(Math.ceil(v * 10) / 10); }
/* 秒 → 「3天4小时」/「4小时12分」/「12分30秒」；与后端 engine.training.format_duration 同口径 */
function fmtDur(s) {
  if (s == null || isNaN(s)) return '—';
  s = Math.max(0, Math.round(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  if (d) return `${d}天${h}小时`;
  if (h) return `${h}小时${m}分`;
  if (m) return `${m}分${s % 60}秒`;
  return `${s}秒`;
}
/* 秒 → 紧凑编码 1d4h / 5h30m / 45m / 30s（窄列） */
function fmtShort(s) {
  if (!s) return '—';
  let r = Math.max(0, Math.round(s)), out = '';
  [[86400, 'd'], [3600, 'h'], [60, 'm'], [1, 's']].forEach(([size, unit]) => {
    const q = Math.floor(r / size);
    if (q) { out += q + unit; r -= q * size; }
  });
  return out;
}
/* ISO 时间 → 距现在秒数（负数=已过） */
function remain(iso) { if (!iso) return null; const t = Date.parse(iso); return isNaN(t) ? null : (t - Date.now()) / 1000; }
/* 完成后时间点 → 本地「MM-DD HH:mm」 */
function fmtWhen(seconds) {
  if (seconds == null) return '—';
  const d = new Date(Date.now() + seconds * 1000);
  const p = x => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ESI attributes 键 → 中文（技能规划面板） */
const ATTR_CN = { charisma: '魅力', intelligence: '智力', memory: '记忆', perception: '感知', willpower: '毅力' };
function attrName(k) { return ATTR_CN[k] || k; }
/* 目标类型 → 显示名（计划列表用） */
function kindCN(kind) { return { type: '需求', skill: '技能', career: '职业' }[kind] || kind; }
/* 技能等级 → 「Ⅴ」罗马数字（表格紧凑展示） */
const ROMAN = ['', 'Ⅰ', 'Ⅱ', 'Ⅲ', 'Ⅳ', 'Ⅴ'];
function lvRoman(lv) { return ROMAN[lv] || lv || '—'; }
function toLevel(lv) { return '到 ' + lvRoman(lv) + ' 级'; }
/* ISO 时间 → 本地「MM-DD HH:mm」（练习队列完成时间展示） */
function fmtWhen2(iso) {
  const t = Date.parse(iso);
  if (isNaN(t)) return '—';
  const d = new Date(t), p = x => String(x).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
