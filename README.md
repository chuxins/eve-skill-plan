# eve-skill-plan（EVE 技能规划站）

Flask + Vue 单页技能规划站：**技能库 / 需求查询 / 职业路线 / 计划** 四个主标签 + 常驻「角色与属性」面板，
按角色已有技能与有效属性算「还缺哪些技能、各缺几级、预计训练多久」，可保存计划、导出 EVE 技能计划 TXT。
页面挂在 nginx 的 `/eveskillplanner/` 子路径下：**http://8.156.88.102/eveskillplanner/**

## 运行

```bash
cd /root/eve-skill-plan
python3 build_index.py                 # 前置数据：从 SDE 构建 data/skills.db（见下）
python3 webapp.py --port 8092 --host 127.0.0.1   # 生产由 systemd 托管（eve-skill-plan.service）
```

```bash
systemctl status eve-skill-plan        # 服务状态；日志 append 到 data/webapp.log
systemctl restart eve-skill-plan       # 改完代码/静态资源后重启
curl -s http://127.0.0.1:8092/healthz  # 健康检查（返回技能/类型/需求计数）
```

前置数据（`python3 build_index.py`，全离线，产物 `data/skills.db`）：

- 主来源：既有 SDE 索引 `/root/eve-skill-planner/data/staticdata.db`（读 types/groups/type_attrs，秒级）
- 补充来源：官方 SDE 压缩包 `/root/eve_esi/sde.zip`（技能/舰船描述 + 官方职业路线 `skillPlans.jsonl`）
- 产出表：`skills`（技能元数据）、`type_reqs`（任意类型 → 直接需求技能+等级）、
  `types`、`career_plans`（职业路线 + 里程碑）、`meta`（构建来源/时间/计数）
- 参数：`--no-zip` 只从 SDE 索引构建（无描述/职业路线）；`--stats` 只看统计
- 当前数据量：**512 个技能 / 40 条职业路线 / 24 个技能组**

已保存计划存在 `data/app.db`（SQLite，表 `plans`），与技能索引库分开，删 `skills.db` 不影响存档。
每条计划带 `owner_cid`（创建它的 SSO 登录角色）：**未登录不能保存**（服务端返回 401），
登录后也只能看到 / 修改 / 删除自己创建的计划，角色之间互相隔离；旧库首次启动自动迁移。
技能读取同样按登录角色隔离：只能读取**当前登录角色**的技能列表（未登录 401、非当前角色 403），
`/api/characters` 只返回当前登录角色 —— 前端没有「切换角色」入口，切换需先退出登录再重新登录。

## 功能

- **技能库**：按技能组（24 组）浏览 + 中英文模糊搜索；技能详情含描述、主/副属性、训练时间倍增系数、
  逐级训练时长、前置技能、**需要的完整前置链**（递归展开、前置优先排序）、「被需要的场合」（哪些舰船/装备/技能要它）
- **需求查询**：搜索舰船 / 装备 / 弹药 / 无人机 / 建筑，得到该类型**递归展开后的全部所需技能**
  （含直接需求与完整前置链），可直接「加为计划目标」
- **职业路线**：官方的 40 条职业路线（如「加达里财富猎手」），含里程碑与其技能需求
- **计划**：把「舰船类型 / 单个技能到 N 级 / 职业路线」加为目标，实时复算
  - 自动带出目标所需的**完整前置技能**（含前置的前置）并**每一级单列一行**：如 飞船操控学 Ⅳ 会展开成
    飞船操控学 Ⅰ / Ⅱ / Ⅲ / Ⅳ 四行，每行显示本步训练时长、SP、开始时刻（上一步练完的时刻，第 1 步＝现在）
    与累计完成时间
  - 角色已有等级（0–5）与**有效属性**（base + 植入体）决定训练时长与缺口
  - 排序：`前置优先`（拓扑序，可自上而下依次训练）/ `耗时最短`
  - `include_owned` 可把已满足的技能一并列出；已满足/待训练分区显示
  - **目标卡片给出每个目标的「练完时间点」**（该目标名下最晚的累计完成时间，北京时间；已满足的目标显示「已满足」），
    多个目标共享前置时越靠后的目标练完越晚，与计划表逐级累计一致
  - **导出 TXT**：EVE 技能计划文本（`<localized hint="英文名">中文名*</localized> 等级`，与计划页一致**每一级一行**，可粘贴导入游戏 / 第三方规划工具）
  - **保存计划**：需先「登录 EVE 角色」（EVE SSO）；命名保存到 `data/app.db` 并按登录角色隔离，列表里可载入（用保存时的目标与快照复算）或 `refresh` 用当前角色重算，可删除
- **角色与属性 / SSO 登录**：页头「登录」走 EVE SSO（PKCE）；授权成功即**登录**并签发会话 Cookie
  （`esp_session`，HttpOnly，30 天，密钥持久化在 `data/.session_secret`，可用 `EVE_SKILL_PLAN_SECRET` 覆盖）；
  **只能读取当前登录角色的技能**（未登录 401 / 非当前角色 403）：角色面板只显示当前登录角色及其角色名、
  技能点、有效属性（可手改/用预设）、**当前训练队列**（剩余时间/完成时间），**没有「切换角色」入口**；
  切换角色需先「退出登录」（仅清会话，保留 token）再重新登录
- **属性/时长口径**：`SP(L) = 250 × rank × 2^(2.5L − 2.5)`；速率 = `主属性 + 副属性 / 2` SP/分钟；
  只估算「技能等级从 0 升到目标等级」的时间，不含角色当前等级内的技能点进度（与 pyfa 口径一致）

## 引擎要点（engine/）

- `engine/skills.py`：技能索引（`data/skills.db` 只读）；`search_skills` 中英文搜索、
  `requirements(tid)` 递归前置展开（同技能取所需最高等级）、`topo_order` Kahn 拓扑排序（保证前置优先）、
  `closure` 完整前置链、`consumers` 反向索引（谁需要这个技能）
- `engine/training.py`：SP/时长公式、`normalize_attrs`（属性缺失/非法时回退默认 17 点）
- `engine/planner.py`：`plan(targets, current, attrs, options)` → `{rows, steps, summary}`；
  `rows` 每技能一行、按前置优先排序并带 `ok`（已满足）标记与逐级明细 `levels`；`steps` 把每个缺口技能
  的等级逐级展开（每一级一行，含本步时长 / SP 与累计完成时间）；`summary` 含
  `skills_total / skills_missing / skills_owned / steps / sp / seconds / duration / attributes / rate_note`
- `engine/store.py`：`plans` 表的存取（`data/app.db`）；`owner_cid` 按 SSO 登录角色隔离，旧库自动迁移（存量无主计划转为不可见）
- `esi.py` / `oauth.py`：PKCE(S256) 授权、token 刷新、角色技能/属性/队列/技能点读取；
  token 目录 `~/.eve-skill-plan/tokens/<cid>.json`（`EVE_SKILL_PLAN_TOKEN_DIR` 可覆盖）
- SDE attributeID 常量集中在 `config.py`（`requiredSkill1..5` / 对应等级 / 主副属性 / rank），换 SDE 只需改这里

## API

```
GET    /healthz                          健康检查 + 索引计数
GET    /api/meta                         元信息（技能数/职业数/技能组、等级、类别）
GET    /api/skillgroups                  技能组树
GET    /api/skills?q=&group_id=&limit=    技能搜索（limit 上限 500）
GET    /api/skill/<tid>?character_id=     技能详情（描述/属性/逐级时间/前置/被需求；character_id 仅限当前登录角色）
GET    /api/types?q=&limit=               类型搜索（舰船/装备/弹药/无人机/建筑/技能）
GET    /api/type/<tid>?character_id=      该类型的递归需求技能（含直接需求与前置链；character_id 仅限当前登录角色）
GET    /api/careers                       职业路线列表
GET    /api/career/<plan_id>              职业路线详情（里程碑 + 技能需求）
POST   /api/plan                          复算计划 {targets,current,attrs,character_id,options}（character_id 仅限当前登录角色）
POST   /api/plan/txt                      导出 TXT（同一入参，返回 text/plain 技能计划文本）
GET    /api/plans                         已保存计划列表
POST   /api/plans                         保存计划 {name,targets,current,attrs,character_id}
GET    /api/plans/<id>                    计划详情
DELETE /api/plans/<id>                    删除计划
POST   /api/plans/<id>/run                载入计划并复算 {refresh,character_id,attrs}
GET    /api/characters                    当前登录角色（未登录 → 空列表；只列登录角色，无切换入口）
DELETE /api/characters/<cid>              退出登录（仅清会话，保留 token；只能退出当前登录角色）
GET    /api/characters/<cid>/overview     角色概览（技能等级/有效属性/训练队列/技能点；仅当前登录角色可读，
                                          未登录 401 / 非当前角色 403）
GET    /api/oauth/url                     生成 SSO 授权地址（PKCE，带 state）
GET    /oauth/start                       直接 302 到 EVE SSO
GET    /oauth/callback                    授权回调：换 token → 写 token 目录 → 302 回 ./?cid=<id>
```

`targets` 元素：`{kind:"type"|"skill"|"career", tid, level?, plan_id?}`；`current` 为 `{技能tid: 等级}`；
`attrs` 为 `{charisma,intelligence,memory,perception,willpower}`；`options` 支持 `order`（`prereq`/`shortest`）
与 `include_owned`。参数非法（空目标、非数字 tid、未知 tid）返回 400，未知技能/类型/计划返回 404。

### 查询参数的中文容错（`_q()`）

浏览器会把 URL 里的 UTF-8 百分号编码，但 curl / 旧客户端常直接塞原始字节；此时 WSGI 层按 **iso-8859-1**
解码会得到 latin-1 乱码，`webapp.py` 的 `_q()` 再做一次 `latin-1 → utf-8` 还原。实测（`tests/smoke_http.py` 覆盖）：

- 经 nginx `/eveskillplanner/` 的代理 location，原始 UTF-8 字节会被**原样转发**给后端：`?q=乌鸦`（字节里无 `0xA0`）
  → 200 且查询词被正确还原；`?q=无人机`（UTF-8 字节含 `0xA0`）→ 400 `Bad request syntax`
  （Python `http.server` 把请求行按 iso-8859-1 解码后 `split()`，字节 `0xA0` = NBSP 被当成空白 → 拆成 4 段）
- 走 nginx 但**没有挂到本站代理前缀**的路径（如裸 `/api/...`）由 nginx 自己应答（当前是 308 跳到别的应用；
  需要 nginx 规范化 URI 的路径直接 400），原始 UTF-8 根本进不到 Flask

结论：**前端一律用 `encodeURIComponent`**（`static/util.js` 的 `API()` 已保证），`_q()` 只是对老客户端的兜底。

## 前端

无构建步骤：`static/index.html`（模板 + 样式）+ `static/util.js`（纯函数与 API 封装）+ `static/app.js`（Vue 3 setup 逻辑），
Vue 用本地文件 `static/vendor/vue.global.prod.js`（3.5.13，**含模板编译器**，故无需构建步骤）。

- **子路径部署**：所有请求都是相对路径（`API('api/meta')` 等），`/eveskillplanner/` 下与后端根路径下都能跑；
  nginx 会把前缀剥掉再转给 Flask（当前对外入口 `https://eve-tools.xyz/eveskillplanner/`，443）；
  OAuth 回调也用相对跳转 `./?cid=<id>`（nginx 已剥掉 `/eveskillplanner` 前缀）
- **静态资源自动版本号**：`index.html` 里写 `{{ASSET_VERSION}}`，`webapp.py` 渲染 `/` 时按 `static/` 下文件的
  (路径, mtime, 大小) 摘要替换 —— 改动任意静态资源后版本号自动变化，无需手工改 `?v=NN`
- **本地缓存**：`localStorage` 前缀 `spl_`（`cacheGet/cacheSet`，带 TTL），换角色时 `clearCharCache(cid)` 清掉该角色缓存
- **模板里只能用白名单全局**（最容易踩的坑）：`index.html` 的模板表达式由运行时编译器包进 `with (_ctx) { … }`，
  而 Vue 3.5 渲染代理的 `has()` 只放行 `Infinity/undefined/NaN/isFinite/parseInt/decodeURI/Math/Number/Date/Array/Object/String/JSON/Intl/…`
  这些白名单全局；**其它自由标识符一律当成实例属性**，取到 `undefined` 后一调用就抛
  `TypeError: xxx is not a function`，整页渲染失败（本站就在 `attrName` 上栽过：控制台三连 `TypeError`、页面只剩空壳）。
  所以 `util.js` 里的显示函数必须在 `app.js` 末尾 `Object.assign(app.config.globalProperties, {...})` 里显式挂上；
  新增/删除模板函数后 `node tests/check_template.js`、`node tests/smoke_frontend.js` 会立刻报出来
- 页头「登录」→ `/api/oauth/url` → EVE SSO → `/eveskillplanner/oauth/callback` → 302 回 `./?cid=<id>`，
  前端读到 `?cid=` 后载入该角色并 `history.replaceState` 清掉查询串；SSO 失败带 `?sso_error=` 显示错误；
  登录后**只能读取当前登录角色**（`/api/characters` 只返回它），切换角色需先「退出登录」再重新登录
- **时间口径（计划表 / 训练队列一致）**：
  - **时间点首尾相接**：第 1 步（实现上 `start_seconds = 0`）从「现在」开始算，之后每一步
    **从上一级练完的时刻**开始（`step[i].start_seconds == step[i-1].end_seconds`），
    因为 EVE 同一时间只能训练一项技能；计划表因此给出「开始（北京时间）」与「预计完成（北京时间）」两列，
    「完成于（累计）」是本步练完时的累计时长（后端 `engine/planner.py` 的 `_steps()` 就按行序累计，
    前端只加「现在」这个锚点）。目标卡片里的「预计 MM-DD HH:mm 练完」= 该目标名下最晚的那个累计完成时间点。
  - **当前训练队列按「剩余」算，不是整级总时长**：正在训练的那一级用 ESI 的
    `start_date → finish_date`（这一级「剩下这段」的权威用时），若 ESI 同时给了这段的 SP 明细
    （`training_start_sp → level_end_sp`），再按 `ESI 速率 ÷ 当前速率` 折算 —— 这样改属性只重算
    「还没练掉的 SP」。**注意**：队列项可能是带着已练进度开训的（`training_start_sp > level_start_sp`），
    早年实现用「整级时长 × 时间进度比例」会把开训前就练掉的部分也算进去，剩余时间被严重高估
    （实测某角色 巡航导弹概论 Ⅴ：ESI 剩余 ≈ 8 天，旧算法显示 ≈ 17 天）；后面排队的整级
    （`level_end_sp − level_start_sp`）按当前属性算整级时长。队列卡片每项标「训练中 / 排队」。
- **时间一律按北京时间展示**（`util.js` 的 `fmtBJ` / `fmtBJFull` / `fmtBJAt`：固定 UTC+8，与浏览器时区无关）：
  计划表「开始 / 预计完成（北京时间）」列、计划摘要的完成点、训练队列每一项的完成点都走它；悬停给带年份的完整
  时间，队列项还附 ESI 原始时间（UTC）便于对照。单级时长由后端算（`engine/training.py`），前端
  `trainSeconds` 用同一公式（速率 = 主属性 + 副属性 / 2 SP/分钟）在本地复算队列，两者口径一致（测试有对齐断言）。
- **属性决定时长**：登录后用角色真实属性（含植入体/增效剂），未登录固定默认 17（属性框与预设按钮禁用）；
  登录后手改属性 → 训练计划与「当前训练队列」的时间点立即复算；点「重读技能」＝`pickChar()` 重读角色，
  属性复位成真实值，队列时间随之复位。
- **技能详情的等级按钮三态**：已学等级黑底（`.lvbtn.learned`，不可点）、队列中那一级蓝底
  （`.lvbtn.training`，悬停提示「正在训练队列中」，不可点）、只有未学等级可点（加入训练计划）。
- **移动端适配（纯 CSS 媒体查询，JS 只管状态）**：桌面是三栏 grid（330px / 1fr / 372px），
  - `≤1200px`：三栏收窄到 290px / 1fr / 330px，不换布局；
  - `≤900px`：**单栏 + 底部导航**（列表 / 详情 / 角色），`main` 的 class 跟着 `mpane` 走
    （`main.mp-l>.l{display:block}` 这类规则在 `@media` 段里），点列表项由 `app.js` 的 `showDetail()`
    自动跳到「详情」栏（`isMobile` 由 `resize` 监听更新）；
  - 触控目标 ≥44px（列表行 / 页签 / 底部导航）、`input` 字号 ≥16px（iOS 聚焦不放大整页）、
    宽表格各自包在 `.tw` 里横向滚、底部导航与浮层留 `env(safe-area-inset-bottom)` 安全区；
  - **媒体查询必须写在基础规则之后**（同优先级靠源码顺序取胜，写在前面等于没写）——
    这条和上面的触控/断点约束由 `tests/check_template.js` 的「移动端适配审计」把守，
    真浏览器 390×844 实测见 `tests/check_mobile.js`

## 测试

```bash
python3 tests/smoke_http.py     # HTTP 冒烟（经 nginx https://eve-tools.xyz/eveskillplanner/ → Flask）：93 项断言
                                #   token 目录里没有授权角色时 91 项（真实角色断言自动跳过）
                                #   直连后端（EVE_SKILL_PLAN_BASE=http://127.0.0.1:8092）再少 2 项 nginx 专有断言
node tests/smoke_frontend.js    # 前端冒烟（Node + vm，无浏览器/jsdom）：125 项断言（有授权角色时 128 项）
node tests/check_template.js    # 前端离线校验：语法 + 模板编译 + 标识符 + 模板函数发布 + 7 种状态渲染冒烟
                                #   + 等级三态/北京时间审计 + 移动端适配审计（断点 / 单栏切换 / 触控目标 / 溢出）
node tests/check_browser.js     # 真浏览器冒烟（无头 Chrome，没装浏览器自动跳过）：控制台零报错 + DOM 真渲染
node tests/check_mobile.js      # 移动端真浏览器验证（390×844 单栏 + 切到 1280×800 再切回）
python3 tests/check_oauth.py    # OAuth 预检：凭据是否被 EVE 接受 + 回调地址是否已登记（不点登录）
node --check static/app.js      # 单文件语法检查
```

- `tests/check_oauth.py`：用**假的** refresh_token 打 EVE token 端点（`invalid_grant` = 凭据有效，
  `invalid_client` = 门户里的密钥不对），再 GET 一次真实 authorize 链接看是否直接进 EVE 登录页
  （出现 `invalid redirect_uri` 就说明门户登记的回调与 `callback_url` 不一致）。它不读写 token 文件，
  换凭据 / 改门户登记后先跑它，比在浏览器里试要快。`EVE_SKILL_PLAN_BASE` 可指定站点入口。

- `tests/check_template.js` 不依赖后端也不依赖浏览器：用 `static/vendor/vue.global.prod.js`（浏览器同款完整版）
  真编译 `#app` 模板，审计模板表达式里的标识符，检查模板用到的 `util.js` 函数有没有挂到实例上
  （`app.config.globalProperties`），最后用「与 Vue 3.5 渲染代理同语义的 Proxy」把初始态、选好角色态、四个页签
  详情、未登录态共 7 种状态各渲染一遍 —— 模板函数缺失、模板里写错标识符都会在这里红。接着是「等级三态 /
  北京时间审计」（技能详情已学=黑·队列中=蓝+「正在训练队列中」提示·未学可点、计划表与队列都走北京时间、
  属性框按登录态禁用）与「移动端适配审计」：
  解析 `index.html` 的 `<style>`（含 `@media` 段），断言断点写在基础规则**之后**、手机上三栏互斥显示、
  触控目标 ≥44px、`input` ≥16px、所有 `<table>` 都包了 `.tw`，并用渲染出的 vnode 确认 `main` 的 class
  跟着 `mpane` 走、底部导航真渲染出来了。退出码：1 编译 / 2 标识符 / 3 缺编译器 / 4 无 `#app` /
  5 模板函数没发布 / 6 渲染冒烟 / 7 移动端审计 / 8 等级三态·北京时间审计。
- `tests/check_browser.js`：用无头 Chrome 打开站点，断言控制台里没有
  `TypeError/ReferenceError/SyntaxError/Uncaught`、DOM 里出现中文界面与 5 维属性名（属性名由模板 `attrName(k)`
  渲染出来，正是「模板函数没挂到实例上」会炸的地方），以及 `main` 带上了移动端栏位 class、底部导航在 DOM 里。
  找不到浏览器会打印「跳过」并以 0 退出；`CHROME_BIN=/path/to/chrome` 指定浏览器，
  `BASE=http://127.0.0.1:8092/` 可测直连后端、`BASE=https://eve-tools.xyz/eveskillplanner/` 测 nginx 入口，
  `WINDOW_SIZE=390,844` 换个视口再看一遍。
- `tests/check_mobile.js` + `tests/probe_mobile.html`：移动端适配的真浏览器验证。测试自己起一个本地 HTTP
  服务：`/probe.html` 现场提供探针页，其余请求原样转发到站点 —— 这样探针与被测页面**同源**，
  才能读 `iframe` 里的 `computedStyle`、量真实尺寸、模拟点击；探针在一个真 **390×844** 的 iframe 视口里
  加载站点，断言：底部导航可见且能切三栏、一次只显示一栏、`main` 的 class 跟随 `mpane`、头部统计隐藏、
  输入框字号、列表行高、点列表项自动跳详情、详情栏与整页无横向溢出；再把 iframe 拉到 **1280×800**
  断言三栏回来、底部导航隐藏，然后缩回手机宽度断言单栏回来且 `isMobile` 已更新（覆盖 `resize` 监听）。
  探针结果（`PASS/FAIL` 行）由 `--dump-dom` 取回判定；找不到浏览器同样「跳过」并 0 退出。

- `tests/smoke_http.py` 覆盖：静态入口（`/eveskillplanner` → 301、首页占位符注入、资源版本号）、元信息/技能搜索（含
  裸 UTF-8 与 latin-1 乱码容错）、技能与类型/职业详情、计划复算（从零 / 带 `current` / `include_owned` /
  排序 / 带 `character_id` / 职业 / 单技能 / **技能目标自动带前置** / **逐级 `steps`** / 空目标 400 / 非法 tid 400）、
  TXT（localized 格式 / **逐级展开**（每一级一行）/ 中英文名）、
  计划 CRUD + `run`、角色 overview 与 OAuth（授权地址含 PKCE + scope、回调缺参 400）。
  可用环境变量调整：`EVE_SKILL_PLAN_BASE`（默认 `https://eve-tools.xyz/eveskillplanner`，即 nginx 入口；
  设成 `http://127.0.0.1:8092` 则直连后端，跳过 2 条 nginx 专有断言）、`EVE_SKILL_PLAN_PORT`（8092）、
  `EVE_SKILL_PLAN_CHAR`（测试角色，默认 2124544250；token 目录为空时相关断言自动跳过）。
  断言总数随「token 目录里有没有角色」浮动：nginx 入口 93 / 91 项，直连后端 91 / 89 项。
- `tests/smoke_frontend.js` 用 `node:vm` 加载 `util.js` + `app.js`（stub 掉 `Vue.createApp` / `localStorage` /
  `fetch` / DOM），直接驱动真实的 `data/computed/methods`，断言格式化函数、搜索、目标增删、复算、
  保存/载入/导出、角色隔离（登录只显示当前角色 / 退出登录清会话）、缓存与 `login()`；`BASE` 环境变量可指向任意后端。
- 数据依赖：断言里的计数（512 技能 / 40 职业 / 24 技能组 / 乌鸦级 6 项缺口 / 4天 13小时）绑定当前
  `data/skills.db` 与测试角色的技能表，换 SDE 或换角色后需同步更新。

## 部署（nginx `/eveskillplanner/` + systemd）

`/etc/nginx/sites-enabled/snowluma` 的 **443** server（80 端口只留 ACME 校验，其余 301 到 https）：

```nginx
location = /eveskillplanner { return 301 /eveskillplanner/$is_args$args; }   # 无尾斜杠 → 301
location ~ ^/(?i)eveskillplanner(/.*)?$ { return 301 /eveskillplanner$1$is_args$args; }  # 大小写纠正
location ^~ /eveskillplanner/ {
    rewrite ^/eveskillplanner/(.*)$ /$1 break;       # 剥掉前缀后再转给 Flask
    proxy_pass http://127.0.0.1:8092;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;                         # 首次加载技能索引 + ESI 汇总
    proxy_redirect ./ /eveskillplanner/;             # OAuth 回跳的 ./?cid=… 收敛到站点根
    client_max_body_size 4m;
}
```

`/etc/systemd/system/eve-skill-plan.service`：`WorkingDirectory=/root/eve-skill-plan`，
`ExecStart=/usr/bin/python3 -u webapp.py --port 8092 --host 127.0.0.1`，
`Environment=EVE_SKILL_PLAN_PORT=8092` / `EVE_SKILL_PLAN_URL=https://eve-tools.xyz/eveskillplanner`，
`Restart=always`，`OOMScoreAdjust=-500`（内存紧张时优先牺牲 VS Code 远端进程），日志 append 到 `data/webapp.log`。

改完配置：`nginx -t && systemctl reload nginx`；改完代码/静态资源：`systemctl restart eve-skill-plan`。

### 自动部署（Windows 开发机 → git push → 服务器自动拉取）

仓库已配好「push 即部署」：本地 `git push origin main` 后，GitHub Actions 经 SSH 登录服务器执行
`deploy/auto-update.sh`（`git fetch` → 有变化才 `git reset --hard origin/main` → `requirements.txt`
变化才装依赖 → `systemctl restart eve-skill-plan`）；服务器上的 `eve-skill-plan-update.timer`
每 2 分钟轮询一次作兜底。数据与凭据安全：`data/`（skills.db / app.db / 日志）与 `config.json`
被 `.gitignore` 忽略，`reset --hard` 不触碰，已保存计划、技能索引与 OAuth 凭据不受影响。

服务器一次性安装（只做一次）：

```bash
# 1) 生成只读 Deploy Key（属主与服务用户一致，默认 root），把 .pub 内容加到
#    GitHub 仓库 Settings → Deploy keys（勾选 read-only，只允许拉取）
mkdir -p /root/eve-skill-plan/.ssh
ssh-keygen -t ed25519 -f /root/eve-skill-plan/.deploy_key -N '' -C 'eve-skill-plan auto-update (read-only)'
cat /root/eve-skill-plan/.deploy_key.pub

# 2) 远端改为 SSH（只拉取，不在服务器上 push；push 一律在 Windows 开发机进行）
git -C /root/eve-skill-plan remote set-url origin git@github.com:chuxins/eve-skill-plan.git

# 3) 安装定时器（兜底轮询）
cp /root/eve-skill-plan/deploy/eve-skill-plan-update.service \
   /root/eve-skill-plan/deploy/eve-skill-plan-update.timer /etc/systemd/system/
chmod +x /root/eve-skill-plan/deploy/auto-update.sh
systemctl daemon-reload
systemctl enable --now eve-skill-plan-update.timer

# 4) 手动验证一次（无更新应显示“已是最新”且不重启）
/root/eve-skill-plan/deploy/auto-update.sh
```

GitHub 仓库 Secrets（Settings → Secrets and variables → Actions，与 eve-isk 同款）：

| Secret    | 值 |
|-----------|----|
| `SSH_HOST` | 服务器公网 IP `8.156.88.102` |
| `SSH_USER` | `root`（需能执行 sudo） |
| `SSH_KEY`  | 用于 SSH 登录服务器的私钥（公钥已加入服务器 `authorized_keys`） |

常用命令：手动触发 `/root/eve-skill-plan/deploy/auto-update.sh`；查看定时器
`systemctl list-timers eve-skill-plan-update.timer`；更新日志 `journalctl -u eve-skill-plan-update -n 50`；
暂停止自动更新 `systemctl disable --now eve-skill-plan-update.timer`。

## OAuth 接线

本站使用**自己独立的 EVE 应用**（凭据写在项目根目录 `config.json`，含 secret，别提交到版本库；
也可用环境变量逐字段覆盖）：

```json
{
  "client_id": "1ea3b22467d64f37ae6b5ea7a800a8de",
  "client_secret": "eat_…",
  "callback_url": "https://eve-tools.xyz/eveskillplanner/oauth/callback"
}
```

- EVE 开发者后台登记的「回调地址」须**逐字符等于** `callback_url`（不一致会报 `invalid redirect_uri`）。
  实测本站可用的两种写法（任选其一登记即可，程序两种都兼容）：
  - `https://eve-tools.xyz/eveskillplanner/oauth/callback` ← 首选，`/api/oauth/url` 实际发送的就是它
  - `https://eve-tools.xyz/eveskillplanner/`（把回调登记成站点根）——SSO 会把 `?code=…&state=…` 送到根路径，
    `webapp.py` 的 `page_index()` 检测到 `code`+`state` 后 302 转交 `./oauth/callback`，流程照常完成
- 该公网地址由 nginx 反代到本站（`location /eveskillplanner/ → 127.0.0.1:8092`，Flask 侧看到的是 `/oauth/callback`）；
  `callback_url` 里必须写浏览器看到的**公网地址**，不能写 `127.0.0.1`
- 授权范围：`esi-skills.read_skills.v1` + `esi-skills.read_skillqueue.v1`（属性接口也要求 skills 读权限）
- 环境变量逐字段覆盖：`EVE_CLIENT_ID` / `EVE_CLIENT_SECRET` / `EVE_CALLBACK_URL`；
  公网基址 `EVE_SKILL_PLAN_URL`（默认 `https://eve-tools.xyz/eveskillplanner`）、`EVE_SKILL_PLAN_PORT`（8092）、
  `EVE_SKILL_PLAN_DB`（技能索引路径）、`EVE_SKILL_PLAN_TOKEN_DIR`（角色 token 目录）
- **换 EVE 应用后必须重新授权一次**：EVE 的 `refresh_token` 与签发它的应用绑定，用新应用去刷老 token
  实测得到 `400 {"error":"invalid_grant","error_description":"Invalid refresh token. Character grant missing/expired."}`，
  旧的授权记录不会再自动续期（走一次「登录」即可写入新 token）
- **token 目录不要和「用另一个 EVE 应用」的站点共用**：`refresh_token` 每次刷新都会轮换，
  两个应用共用一个目录会各自动手把对方的 refresh_token 轮换失效（症状：交替出现 `invalid_grant`）。
  本站默认 `~/.eve-skill-plan/tokens/<cid>.json`；若想和装配站共用一条授权，就让两站用**同一个** EVE 应用
  （门户里给同一应用登记两个回调地址），再把 `EVE_SKILL_PLAN_TOKEN_DIR` 指回同一个目录
- 「退出登录」只清当前 SSO 会话（保留 token，重新登录无需再次授权）；切换角色 = 退出登录 → 重新 EVE SSO 登录


## 已验证行为 / 已知口径

- 角色 token 失效、ESI 报错、技能索引缺失都**不会让接口整体 500**：技能读取失败按「从零开始」算，
  属性读取失败回退默认 17 点（见 `webapp.py` 的 `_chars_skills` / `_attrs`），overview 会带 `skills_error` 说明
- ESI 自身故障（实测遇到过 `502 {"status":502,"error":"Bad Gateway"}`、`{"error":"unroutable"}`、
  `{"error":"Timeout contacting tranquility"}`）会原样落进 `skills_error` / `queue_error` / `attributes_error`，
  此时页面仍可用（技能按未训练算），ESI 恢复即自动正常，不需要动本站配置
- 需求技能递归展开取「同技能所需最高等级」；计划顺序为 Kahn 拓扑序，保证自上而下可依次训练
- 时长是「从 0 级练到目标等级」的理论值，不含角色当前等级内的进度、属性重映射与加速剂
- 需求技能查询覆盖的类别见 `config.py` 的 `CAT_REQ_SOURCES`（舰船/装备/弹药/无人机/建筑/技能），
  其余类别（如材料、行星交互物）不支持「需求技能」查询
