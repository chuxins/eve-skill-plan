/* Vue 主应用（模板写在 index.html 的 #app 里，本文件只有状态机与请求逻辑）
 * 页面分四个主标签「技能库 / 需求查询 / 职业路线 / 计划」，右侧常驻「角色与属性」面板：
 * 角色 + 属性 → 决定所有页签里的「当前等级 / 训练时长 / 缺口」。
 * 所有请求都走相对路径，便于在 nginx 的 /eveskillplanner/ 子路径下部署。
 */
const { createApp } = Vue;

const app = createApp({
  data() {
    return {
      tab: 'plan',
      /* 移动端适配：≤900px 时一次只显示一栏（见 index.html 的 @media (max-width:900px)） */
      mpane: 'l',            // 当前栏：l 列表 / c 详情 / r 角色与属性（桌面端三栏同时显示，此值不起作用）
      isMobile: false,       // 由 syncMobile() 跟随视口宽度更新，决定「点列表项是否自动跳详情」
      /* 左右栏宽度（px）：由 .gutter 拖拽手柄调整，存 localStorage，刷新后保留 */
      lw: 330, rw: 372,
      meta: null, busy: '', err: '', toast: '',
      // 角色 / 属性（loginCid/loginName 是服务端会话里的 SSO 登录身份；技能读取也按它隔离，
      // 只能读当前登录角色，不可切换 —— 切换角色需先退出登录再重新登录）
      chars: [], cid: null, cname: '', current: {}, attrs: {}, queue: null, totalSp: null,
      loginCid: null, loginName: '',
      // 技能库
      groups: [], gid: null, gq: '', skills: [], skill: null,
      // 需求查询
      tq: '', tcats: [], types: [], tinfo: null,
      // 职业路线
      careers: [], career: null,
      // 计划
      targets: [], opts: { order: 'prereq' }, plan: null, saved: [], planName: '',
    };
  },
  computed: {
    attrKeys() { return this.attrs ? Object.keys(this.attrs) : []; },
    levels() { return this.meta ? this.meta.levels : [1, 2, 3, 4, 5]; },
    cats() { return this.meta ? this.meta.categories : []; },
    rows() { return this.plan ? this.plan.rows : []; },
    steps() { return this.plan ? (this.plan.steps || []) : []; },
    rowsTodo() { return this.rows.filter(r => !r.ok); },
    rowsDone() { return this.rows.filter(r => r.ok); },
    summary() { return this.plan ? this.plan.summary : null; },
    /* 目标卡片：每个目标的「练完时间」= 该目标名下最晚的那个累计完成时间点（时间轴从「现在」
     * 起算、各步首尾相接）。include_owned=false 时已满足的技能不在 rows 里 —— 若一个目标名下
     * 没有任何待练行，说明它已满足（done=true）。共享的前置技能会被多个目标算进去，
     * 因此越靠后的目标练完时间越晚（与计划表逐级累计一致）。 */
    targetsView() {
      const rows = this.rows || [];
      return (this.plan ? (this.plan.targets || []) : []).map(t => {
        const mine = rows.filter(r => (r.targets || []).includes(t.tid));
        const end_seconds = mine.length ? Math.max.apply(null, mine.map(r => Number(r.end_seconds) || 0)) : 0;
        return Object.assign({}, t, {
          end_seconds, at: fmtBJ(end_seconds),
          done: !mine.length || mine.every(r => r.ok),
        });
      });
    },
    charName() { return this.cname || (this.cid ? String(this.cid) : '未选角色'); },
    attrsText() {
      const eff = this.summary ? this.summary.attributes : this.attrs;
      return Object.keys(eff).map(k => `${attrName(k)} ${Math.round(eff[k])}`).join(' · ');
    },
    // 当前是否处于 SSO 登录状态（服务端会话里有登录角色 → 计划才能保存 / 可见）
    loggedIn() { return !!this.loginCid; },
    // 属性只来自角色：未登录固定按默认 17 点算（后端 normalize_attrs 同口径），
    // 因此未登录时属性框与预设都不可改；登录后可手动微调（改完立即复算）。
    canEditAttrs() { return this.loggedIn; },
    /* 当前训练队列：按「当前属性」本地复算「剩余/整级」用时与完成时间点。
     * 第 1 项从「现在」起算，之后每项接上一项练完的时刻（EVE 同一时间只能训练一项 →
     * 时间点首尾相接）；正在训练的那一级按 ESI 的「剩余」时间算，排队的按整级时长。
     * 属性一改（登录后手改 / applyPreset）这里立刻跟着变；点「重读技能」→
     * pickChar 把属性复位成角色真实属性 → 队列时间随之复位。 */
    queueView() {
      const q = this.queue || [];
      const now = Date.now();
      let acc = 0;
      return q.slice()
        .sort((a, b) => (Number(a.queue_position) || 0) - (Number(b.queue_position) || 0))
        .map(it => {
          const sta = Date.parse(it.start_date || '');
          const seconds = this.queueItemSeconds(it, this.attrs, now);
          acc += seconds;                                  // 同一时间只能训练一项 → 累计
          return Object.assign({}, it, {
            training: !isNaN(sta) && sta <= now,           // ESI 已开始这一级 = 正在训练
            seconds, end_seconds: acc, at: fmtBJ(acc),
            left_seconds: remain(it.finish_date),          // ESI 口径的剩余（悬停对照用）
            esi_at: fmtBJAt(it.finish_date),
          });
        });
    },
  },
  methods: {
    // ---------------------------------------------------------- 基础
    async boot() {
      try { this.meta = await API('api/meta'); } catch (e) { this.err = e.message; }
      this.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 17, willpower: 17 };
      const q = new URLSearchParams(location.search);
      if (q.get('sso_error')) this.err = 'SSO 授权被拒绝或失败：' + q.get('sso_error');
      const want = q.get('cid') || cacheGet('cid', 9e12);
      await this.loadChars(want ? Number(want) : null);
      await Promise.all([this.loadGroups(), this.loadCareers(), this.loadSaved()]);
      if (this.cid) await this.pickChar(this.cid, true);
      if (q.get('cid')) {
        this.tab = 'plan';
        history.replaceState(null, '', './');
        this.flash('登录成功，已载入该角色的技能');
      }
    },
    flash(msg) { this.toast = msg; setTimeout(() => { this.toast = ''; }, 4000); },
    /* ---------------- 移动端适配（布局本身全在 CSS 媒体查询里，这里只管状态） ----------------
     * ≤900px 时一次只显示一栏，靠底部导航切换（index.html 的 .mnav）；
     * isMobile 只在「点列表项要不要自动跳到详情栏」这一个地方用得到。 */
    syncMobile() {
      this.isMobile = !!(window.matchMedia && window.matchMedia('(max-width:900px)').matches);
    },
    showDetail() { if (this.isMobile) this.mpane = 'c'; },
    // ---------------------------------------------------------- 栏宽拖拽（桌面端三栏）
    /* 恢复上次拖拽的栏宽，并写到 <main> 的 CSS 变量上（grid-template-columns 读取它们） */
    loadCols() {
      try {
        const c = JSON.parse(localStorage.getItem('esp_cols') || 'null');
        if (c && typeof c.l === 'number' && c.l >= 140) this.lw = Math.round(c.l);
        if (c && typeof c.r === 'number' && c.r >= 140) this.rw = Math.round(c.r);
      } catch (e) { /* 坏数据直接忽略，用默认宽度 */ }
      this.applyCols();
    },
    applyCols() {
      /* 注意：本组件是多根 fragment（#app 里 header/main/nav 平级），this.$el 指向第一个根
       * <header>，用 $el.querySelector('main') 会拿到 null。页面只有一个 <main>，直接全局查。 */
      const m = document.querySelector('main');
      if (!m) return;
      m.style.setProperty('--lw', this.lw + 'px');
      m.style.setProperty('--rw', this.rw + 'px');
    },
    /* 拖动左右栏边界：mousemove 期间实时改 CSS 变量，松开后持久化 */
    startDrag(side, ev) {
      if (this.isMobile) return;                 // 手机单栏布局不拖
      ev.preventDefault();
      const g = ev.currentTarget;
      if (g) g.classList.add('dragging');        // 拖动期间保持高亮（鼠标移出手柄也不消失）
      const startX = ev.clientX;
      const startW = side === 'l' ? this.lw : this.rw;
      const onMove = (e) => {
        const delta = e.clientX - startX;
        const w = Math.max(140, Math.min(800, Math.round(startW + (side === 'l' ? delta : -delta))));
        if (side === 'l') this.lw = w; else this.rw = w;
        this.applyCols();
      };
      const onUp = () => {
        if (g) g.classList.remove('dragging');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        document.body.style.userSelect = '';
        try { localStorage.setItem('esp_cols', JSON.stringify({ l: this.lw, r: this.rw })); } catch (e) {}
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
      document.body.style.userSelect = 'none';   // 拖拽时防止选中文本
    },
    // ---------------------------------------------------------- 角色
    async loadChars(select) {
      try {
        const d = await API('api/characters');
        this.chars = d.characters || [];
        const lg = d.login;
        this.loginCid = lg && lg.cid ? Number(lg.cid) : null;
        this.loginName = (lg && lg.name) || '';
      } catch (e) { this.chars = []; this.loginCid = null; this.loginName = ''; }
      const ids = this.chars.map(c => c.id);
      const cid = select && ids.includes(select) ? select : (ids.includes(this.cid) ? this.cid : ids[0]);
      this.cid = cid || null;
      if (this.cid) cacheSet('cid', this.cid); else cacheDel('cid');
    },
    async pickChar(id, silent) {
      this.cid = Number(id) || null;
      this.queue = null; this.totalSp = null;
      if (!this.cid) { this.current = {}; this.cname = ''; return; }
      cacheSet('cid', this.cid);
      this.busy = '读取角色数据…';
      try {
        const d = await API(`api/characters/${this.cid}/overview`);
        this.cname = d.name;
        this.current = d.skills || {};
        // 属性一律以角色为准（读不到的项退回默认 17）：这样「重读技能」会把
        // 手动改过的属性复位成角色真实属性，训练队列的时间点也随之复位。
        this.attrs = Object.assign(
          { charisma: 17, intelligence: 17, memory: 17, perception: 17, willpower: 17 },
          d.attributes || {});
        this.queue = d.queue || null;
        this.totalSp = d.total_sp != null ? d.total_sp : null;
        if (d.skills_error) this.err = d.skills_error;
        if (!silent) this.flash(`已载入 ${d.name}：${Object.keys(this.current).length} 项技能`);
      } catch (e) {
        this.err = e.message;
      } finally { this.busy = ''; }
    },
    async logout() {
      // 退出登录：清服务端 SSO 会话 + 本地角色数据（保留 token，重新登录无需再次授权）。
      // 切换角色 = 先退出登录 → 再重新 EVE SSO 登录。
      const cid = this.loginCid;
      if (!cid) return;
      try { await API(`api/characters/${cid}`, { method: 'DELETE' }); } catch (e) { /* 会话已失效也照常退出 */ }
      this.loginCid = null; this.loginName = '';
      this.chars = []; this.cid = null; this.cname = '';
      this.current = {}; this.queue = null; this.totalSp = null;
      this.attrs = { charisma: 17, intelligence: 17, memory: 17, perception: 17, willpower: 17 };
      cacheDel('cid'); clearCharCache(cid);
      this.flash('已退出登录，切换角色请重新通过 EVE SSO 登录');
      await this.loadSaved();
      this.recalc();
    },
    async login() {
      try {
        const d = await API(`api/oauth/url?next=${encodeURIComponent('./?sso_ok=1')}`);
        location.href = d.url;                 // 同窗口跳转，授权后回本站
      } catch (e) { this.err = e.message; }
    },
    applyPreset(p) { this.attrs = Object.assign({}, this.attrs, p.attrs); this.recalc(); },
    /* 队列里「正在训练 / 最先排队」的那一级（技能详情用它把该级标蓝） */
    queueLevel(tid) {
      const q = (this.queue || []).filter(x => String(x.skill_id) === String(tid));
      if (!q.length) return 0;
      q.sort((a, b) => (Number(a.queue_position) || 0) - (Number(b.queue_position) || 0));
      return Number(q[0].finished_level) || 0;
    },
    /* 训练速率 = 主属性 + 副属性 / 2（SP/分钟）；缺项按默认 17（与后端 normalize_attrs 同口径） */
    attrRate(it, attrs) {
      const a = attrs || {};
      return (Number(a[it.primary]) || 17) + (Number(a[it.secondary]) || 17) / 2;
    },
    /* 这条队列项「这一级」涉及多少 SP：优先用 ESI 的 SP 明细，缺了才退回公式
     *   250 × rank × 2^(2.5L − 2.5) 的逐级差（与后端 engine/training.py 同口径）。 */
    queueItemSp(it) {
      const endSp = Number(it.level_end_sp), lvStartSp = Number(it.level_start_sp);
      if (endSp > 0 && lvStartSp >= 0 && endSp > lvStartSp) return endSp - lvStartSp;
      const toLv = Math.max(0, Number(it.finished_level) || 0);
      return spToLevel(it.rank, toLv) - spToLevel(it.rank, Math.max(0, toLv - 1));
    },
    /* 单条队列项的用时（秒）——统一按「剩余」口径：
     * - 正在训练的那一级（ESI 的 start_date 已到）：ESI 的 start_date→finish_date 就是
     *   这一级「剩下这段」的权威用时；若 ESI 同时给了这段的 SP 明细
     *   （training_start_sp → level_end_sp），再按 (ESI 速率 ÷ 当前速率) 折算，
     *   这样改属性只重算「还没练掉的 SP」的用时。注意不能用整级时长 × 时间进度比例：
     *   队列项可能是带着已练进度开训的（training_start_sp > level_start_sp），
     *   乘以整级时长会把开训前就练掉的部分也算进去，剩余时间被严重高估。
     * - 后面排队的整级（start_date 还没到 / 没有）：按当前属性算整级时长。
     * - ESI 日期缺失（旧缓存 / 非 SSO）→ 退回 finish_date 剩余时间，再退回整级时长。 */
    queueItemSeconds(it, attrs, now) {
      const sta = Date.parse(it.start_date || ''), fin = Date.parse(it.finish_date || '');
      const left = isNaN(fin) ? null : Math.max(0, fin - now) / 1000;   // ESI 口径的剩余
      const rate = this.attrRate(it, attrs);                            // 当前属性下的 SP/分钟
      if (!it.primary || !it.secondary || rate <= 0) return left == null ? 0 : left;
      if (!isNaN(sta) && sta <= now) {                                  // 正在训练这一级
        if (left == null) return 0;
        const esiMin = (fin - sta) / 60000;                             // ESI 算这段用的分钟数
        const tSp = Number(it.training_start_sp), eSp = Number(it.level_end_sp);
        if (tSp > 0 && eSp > tSp && esiMin > 0)
          return left * ((eSp - tSp) / esiMin) / rate;                  // 剩余 SP ÷ 当前速率
        return left;
      }
      const sp = this.queueItemSp(it);                                  // 还没轮到 → 整级
      return sp > 0 ? sp / rate * 60 : (left == null ? 0 : left);
    },
    /* 技能详情里某个等级的状态：
     * learned（已学：变黑且不可加入计划）/ training（正在训练队列中：标蓝）/ open（未学：可加入计划） */
    skillLvState(lv) {
      const cur = (this.skill && this.skill.current) || 0;
      if (lv <= cur) return 'learned';
      const tid = (this.skill && this.skill.skill && this.skill.skill.tid) || 0;
      return this.queueLevel(tid) === lv ? 'training' : 'open';
    },
    skillLvTitle(lv) {
      const st = this.skillLvState(lv);
      if (st === 'learned') return '已经学会 ' + lvRoman(lv) + ' 级，不能再加入训练计划';
      if (st === 'training') return '正在训练队列中';
      return '把 ' + lvRoman(lv) + ' 级加入训练计划';
    },
    // ---------------------------------------------------------- 技能库
    async loadGroups() {
      try { this.groups = (await API('api/skillgroups')).groups; } catch (e) { this.err = e.message; }
    },
    async selectGroup(gid) { this.gid = this.gid === gid ? null : gid; await this.searchSkills(); },
    async searchSkills() {
      const p = new URLSearchParams({ limit: '400' });
      if (this.gq) p.set('q', this.gq);
      if (this.gid) p.set('group_id', this.gid);
      try { this.skills = (await API('api/skills?' + p)).skills; } catch (e) { this.err = e.message; }
    },
    async openSkill(tid) {
      this.busy = '读取技能…';
      try {
        const p = this.cid ? `?character_id=${this.cid}` : '';
        this.skill = await API(`api/skill/${tid}${p}`);
        this.showDetail();                 // 手机端自动跳到详情栏
      } catch (e) { this.err = e.message; } finally { this.busy = ''; }
    },
    lvOf(tid) { return this.current[String(tid)] || this.current[tid] || 0; },
    /* 从「需求查询 / 职业路线」跳到技能库查看该技能 */
    jumpSkill(tid) { this.tab = 'skills'; this.openSkill(tid); },
    addSkillTarget(sk, level) {
      this.addTarget({ kind: 'skill', tid: sk.tid, level: level || 5, name: sk.name,
        name_en: sk.name_en || '', group: sk.group || '', category: '技能' });
    },
    // ---------------------------------------------------------- 需求查询
    toggleCat(id) {
      const i = this.tcats.indexOf(id);
      if (i < 0) this.tcats.push(id); else this.tcats.splice(i, 1);
      this.searchTypes();
    },
    async searchTypes() {
      if (!this.tq.trim()) { this.types = []; return; }
      const p = new URLSearchParams({ q: this.tq, limit: '60' });
      if (this.tcats.length) p.set('cats', this.tcats.join(','));
      try { this.types = (await API('api/types?' + p)).types; } catch (e) { this.err = e.message; }
    },
    async openType(tid) {
      this.busy = '展开需求技能…';
      try {
        const p = this.cid ? `?character_id=${this.cid}` : '';
        this.tinfo = await API(`api/type/${tid}${p}`);
        this.showDetail();                 // 手机端自动跳到详情栏
      } catch (e) { this.err = e.message; } finally { this.busy = ''; }
    },
    addTypeTarget() {
      const t = this.tinfo.type;
      this.addTarget({ kind: 'type', tid: t.tid, name: t.name, name_en: t.name_en,
        group: t.group, category: t.category });
    },
    // ---------------------------------------------------------- 职业路线
    async loadCareers() {
      try { this.careers = (await API('api/careers')).careers; } catch (e) { this.err = e.message; }
    },
    async openCareer(planId) {
      this.busy = '读取职业路线…';
      try { this.career = await API(`api/career/${planId}`); this.showDetail(); }   // 手机端自动跳详情栏
      catch (e) { this.err = e.message; } finally { this.busy = ''; }
    },
    addCareerTarget() {
      const c = this.career.career;
      this.addTarget({ kind: 'career', plan_id: c.plan_id, tid: c.plan_id, name: c.name,
        name_en: c.name_en, group: c.internal_name, category: '职业路线' });
    },
    // ---------------------------------------------------------- 计划
    addTarget(t) {
      const dup = this.targets.findIndex(x => x.kind === (t.kind === 'type' ? 'type' : t.kind) &&
        String(x.tid) === String(t.tid) && (x.level || 0) === (t.level || 0));
      if (dup >= 0) { this.flash('该项目已在目标列表里'); return; }
      this.targets.push(t);
      this.tab = 'plan';
      this.recalc();
    },
    removeTarget(i) { this.targets.splice(i, 1); this.recalc(); },
    clearTargets() { this.targets = []; this.plan = null; },
    query() {
      return {
        targets: this.targets.map(t => ({ kind: t.kind, tid: t.tid, plan_id: t.plan_id || t.tid,
          level: t.level || null, name: t.name })),
        current: this.current, attrs: this.attrs, options: this.opts,
      };
    },
    async recalc() {
      if (!this.targets.length) { this.plan = null; return; }
      this.busy = '复算计划…'; this.err = '';
      try {
        this.plan = await API_POST('api/plan', this.query());
        if (this.targets.length && !this.planName)
          this.planName = `${this.charName} · ${this.targets.map(t => t.name).join('+').slice(0, 40)}`;
      } catch (e) { this.err = e.message; } finally { this.busy = ''; }
    },
    async exportTxt() {
      if (!this.plan) return;
      try {
        const r = await fetch('api/plan/txt', { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.query()) });
        if (!r.ok) throw new Error('导出失败：' + r.status);
        const url = URL.createObjectURL(await r.blob());
        const a = document.createElement('a');
        a.href = url;
        a.download = (this.planName || `技能计划-${this.charName}`)
          .replace(/[\\/:*?"<>|]/g, '-') + '.txt';
        a.click(); URL.revokeObjectURL(url);
      } catch (e) { this.err = e.message; }
    },
    async loadSaved() {
      try { this.saved = (await API('api/plans')).plans; } catch (e) { this.saved = []; }
    },
    async savePlan() {
      if (!this.targets.length) { this.err = '还没有目标，先添加目标再保存'; return; }
      // SSO 隔离：未登录不能保存（服务端同样会拒绝并返回 401）
      if (!this.loggedIn) {
        this.err = '未登录无法保存技能训练计划：请先点右上角「登录 EVE 角色」完成 EVE SSO 授权';
        return;
      }
      try {
        await API_POST('api/plans', { name: this.planName, targets: this.targets,
          current: this.current, attrs: this.attrs, options: this.opts, character_id: this.cid });
        await this.loadSaved();
        this.flash('计划已保存');
      } catch (e) { this.err = e.message; }
    },
    async runSaved(pid, refresh) {
      this.busy = '载入计划…';
      try {
        const p = await API_POST(`api/plans/${pid}/run`, { refresh: !!refresh, character_id: this.cid });
        this.plan = p;
        this.planName = p.name;
        this.targets = (p.targets || []).map(t => ({ kind: t.kind, tid: t.tid, plan_id: t.tid,
          level: t.level, name: t.name, name_en: t.name_en, group: t.group, category: t.category }));
        if (refresh) await this.pickChar(this.cid, true);
        this.tab = 'plan';
        this.flash('已载入计划 ' + p.name);
      } catch (e) { this.err = e.message; } finally { this.busy = ''; }
    },
    async delSaved(pid) {
      if (!confirm('删除这个已保存计划？')) return;
      await API(`api/plans/${pid}`, { method: 'DELETE' });
      await this.loadSaved();
    },
  },
  mounted() {
    this.syncMobile();
    this.loadCols();                             // 恢复拖拽后的左右栏宽度
    /* 旋转屏幕 / 拖窗口都会触发；比 matchMedia 的 change 事件更兼容旧浏览器 */
    window.addEventListener('resize', this.syncMobile);
    this.boot();
  },
  unmounted() { window.removeEventListener('resize', this.syncMobile); },
});

/* util.js 里的显示函数在模板里是「自由标识符」，而运行时编译出的渲染函数形如
 * with (_ctx) { … }：Vue 3.5 的渲染代理 has() 只放行 Infinity/Math/Date/JSON 等
 * 白名单全局，其余自由标识符一律当成实例属性 → 取到 undefined，
 * 一调用就抛 TypeError: xxx is not a function，整页渲染直接失败（本站踩过：attrName）。
 * 所以模板里要用到的 util.js 函数必须像下面这样显式挂到实例上：
 * globalProperties 会被渲染代理读到（rs.get 的最后一步就是查它）。
 * 以后新增/删除模板函数时，tests/check_template.js 的「模板函数发布检查」会拦住。 */
Object.assign(app.config.globalProperties, {
  n, fmtDur, fmtShort, remain, fmtWhen, fmtWhen2,   // 数值 / 时长 / 时间
  fmtBJ, fmtBJFull, fmtBJAt,                        // 北京时间（计划时间点 / 训练队列）
  attrName, kindCN, lvRoman, toLevel,               // 文案
  icon, render64, isShip,                           // 图标 / 分类
});

app.mount('#app');
