"""HTTP 冒烟测试：经 nginx（/skills/ 前缀）→ Flask(:8091) 逐条断言接口行为。

用法：
    cd /root/eve-skill-plan && python3 tests/smoke_http.py
    EVE_SKILL_PLAN_BASE=http://127.0.0.1:8091 python3 tests/smoke_http.py   # 直连后端
依赖：后端已启动（systemd eve-skill-plan.service）、nginx 已挂 /skills/。
token 目录里没有角色也能跑：角色相关断言会自动跳过（没走过 SSO 时属正常）。
（没有角色时「角色 / SSO」一节会自动降级为只测空列表）。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("EVE_SKILL_PLAN_BASE", "http://127.0.0.1/skills").rstrip("/")
PORT = int(os.environ.get("EVE_SKILL_PLAN_PORT", "8091"))
NGINX_PORT = urllib.parse.urlsplit(BASE).port or 80   # raw_call 默认打 nginx 端口
# BASE 带路径前缀（如 /skills）说明测的是 nginx 反代入口；直连后端时跳过 nginx 专有断言
PREFIX = urllib.parse.urlsplit(BASE).path.rstrip("/")
IS_NGINX = bool(PREFIX)
CHAR_ID = int(os.environ.get("EVE_SKILL_PLAN_CHAR", "2124544250"))   # token 目录里的测试角色
passed, failed = 0, []


def ok(cond, label, extra=""):
    global passed
    if cond:
        passed += 1
        print("  \u2713", label)
    else:
        failed.append(label)
        print("  \u2717", label, extra)


def call(method, path, body=None, raw=False, base=BASE):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body_bytes = r.read()
            return r.status, body_bytes if raw else json.loads(body_bytes.decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def raw_call(path_bytes, port=None):
    """直接发原始字节请求（模拟 curl/browser 不转义 UTF-8 的场景）。"""
    import socket
    port = port or NGINX_PORT
    sk = socket.create_connection(("127.0.0.1", port), timeout=30)
    sk.sendall(b"GET " + path_bytes + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               b"Accept: application/json\r\nConnection: close\r\n\r\n")
    chunks = []
    while True:
        buf = sk.recv(65536)
        if not buf:
            break
        chunks.append(buf)
    sk.close()
    data = b"".join(chunks)
    head, _, body = data.partition(b"\r\n\r\n")
    if b"chunked" in head.lower():
        out, rest = b"", body
        while True:
            size_line, _, rest = rest.partition(b"\r\n")
            size = int(size_line.split(b";")[0] or b"0", 16)
            if size == 0:
                break
            out += rest[:size]
            rest = rest[size + 2:]
        body = out
    return head.split(b"\r\n")[0].decode("utf-8", "replace"), body


print("== 静态入口")
st, _ = call("GET", "/healthz")
ok(st == 200, f"GET {PREFIX or ''}/healthz → 200", st)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


if IS_NGINX:
    _nored = urllib.request.build_opener(_NoRedirect)
    try:
        _r = _nored.open(BASE.rsplit("/", 1)[0] + PREFIX, timeout=30)
        _code, _loc = _r.status, _r.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        _code, _loc = exc.code, exc.headers.get("Location", "")
    ok(_code == 301 and _loc.rstrip("/").endswith(PREFIX),
       f"GET {PREFIX}（无尾斜杠）→ 301 到 {PREFIX}/", [_code, _loc])
else:
    print("  (直连后端模式，跳过 nginx 的 301 与非法请求断言)")

html = urllib.request.urlopen(BASE + "/", timeout=30).read().decode()
ok('<div id="app"' in html and "app.js" in html, "首页返回 Vue 挂载点 + app.js")
ok(re.search(r"\{\{\s*[A-Z_]+\s*\}\}", html) is None, "后端占位符（{{ASSET_VERSION}} 等）已全部注入")
ok(html.count("{{") > 20, "Vue 插值原样保留", html.count("{{"))
ok("?v=" in html, "静态资源带版本号")
st, body = call("GET", "/static/index.html", raw=True)
ok(st == 200 and b'id="app"' in body, "GET /skills/static/index.html → 200", st)

print("== 元数据 / 技能库")
st, meta = call("GET", "/api/meta")
ok(st == 200 and meta["counts"]["skills"] == 511 and meta["counts"]["careers"] == 40, "api/meta 计数",
   meta.get("counts"))
st, groups = call("GET", "/api/skillgroups")
ok(st == 200 and len(groups["groups"]) == 24, "api/skillgroups 24 组", len(groups.get("groups", [])))
st, d = call("GET", "/api/skills?q=%E6%97%A0%E4%BA%BA%E6%9C%BA&limit=3")
ok(d["query"] == "无人机" and d["skills"][0]["tid"] == 12305, "api/skills 百分号编码查询", d["skills"][:1])
# 裸 UTF-8 请求行（curl 不转义时）：nginx 在代理 location 里把原始字节原样转发给 Flask，
# 由视图层 _q() 还原（无 0xA0 时 200）；而需要 nginx 自己规范化 URI 的路径（如未挂代理的
# /api/）会被 nginx 直接判为非法请求 → 400 Invalid HTTP request。
_st, _body = raw_call(PREFIX.encode() + b"/api/skills?q=" + "乌鸦".encode("utf-8") + b"&limit=2")
_d = json.loads(_body.decode())
ok(_st.startswith("HTTP/1.1 200") and _d["query"] == "乌鸦",
   f"{PREFIX or ''}/api/skills 裸 UTF-8（无 0xA0 字节）→ _q() 还原查询词", _d.get("query"))
_st, _body = raw_call(PREFIX.encode() + b"/api/skills?q=" + "无人机".encode("utf-8") + b"&limit=2")
ok(_st.startswith("HTTP/1.1 400") and b"Bad request syntax" in _body,
   "裸 UTF-8 含 0xA0 字节 → backend 请求行被拆成 4 段 → 400（前端必须 encodeURIComponent）", _st)
if IS_NGINX:
    _st, _body = raw_call(b"/api/skills?q=" + "乌鸦".encode("utf-8"))
    ok(_st.startswith("HTTP/1.1 400") and b"Invalid HTTP request" in _body,
       "未挂代理的 /api/ 裸 UTF-8 → nginx 直接 400 Invalid HTTP request", _st)

# _q() 容错：nginx/Werkzeug 按 iso-8859-1 解码请求行后中文会变 latin-1 乱码，
# 视图里用 _q() 把乱码还原回 UTF-8（走真实视图函数，不经过网络层）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import webapp                                   # noqa: E402  （导入不会启动服务）
client = webapp.app.test_client()
_r = client.get("/api/skills?q=" + "无人机".encode("utf-8").decode("latin-1") + "&limit=3")
ok(_r.status_code == 200 and _r.get_json()["query"] == "无人机" and _r.get_json()["skills"],
   "_q() 把 latin-1 乱码查询还原成 UTF-8", _r.get_json().get("query"))
_r = client.get("/api/types?q=" + "乌鸦".encode("utf-8").decode("latin-1"))
ok(_r.status_code == 200 and _r.get_json()["types"][0]["tid"] == 638, "_q() 对 /api/types 同样生效")
_r = client.get("/healthz")
ok(_r.status_code == 200, "Flask test client /healthz → 200")
st, d = call("GET", "/api/skills?limit=9999")
ok(st == 200 and len(d["skills"]) == 500, "api/skills limit 上限 500", len(d.get("skills", [])))
st, d = call("GET", "/api/skills?group_id=273&limit=500")
ok(st == 200 and bool(d["skills"]) and all(s["group_id"] == 273 for s in d["skills"]),
   "api/skills 按技能组过滤", len(d.get("skills", [])))
st, d = call("GET", "/api/skill/3300")
ok(d["skill"]["name"] == "射击学" and d["level_seconds"]["5"] > 0 and len(d["consumers"]) >= 1,
   "api/skill/3300 详情 + 逐级时间 + 被需求")
st, d = call("GET", f"/api/skill/3300?character_id={CHAR_ID}")
ok(st == 401 and "登录" in d.get("error", ""),
   "匿名带 character_id 的技能详情 → 401（只能读当前登录角色）", st)
st, d = call("GET", "/api/skill/999999")
ok(st == 404 and bool(d.get("error")), "api/skill 不存在 → 404", st)

print("== 需求查询")
st, d = call("GET", "/api/types?q=%E4%B9%8C%E9%B8%A6&limit=5")
ok(d["types"][0]["tid"] == 638, "api/types 搜索乌鸦级", d["types"][:1])
st, d = call("GET", "/api/type/638")
ok(d["skills"] == 6 and d["type"]["group"] == "战列舰" and len(d["direct"]) == 1
   and d["direct"][0]["name"] == "加达里战列舰操作",
   "api/type/638 递归需求 6 项 / 直接需求 1 项", [d.get("skills"), len(d.get("direct", []))])
ok(all(r["name"] for r in d["requirements"]), "需求项带技能名")

# 裸 UTF-8 请求行（curl 不转义时）在 http.server 里的真实行为：
# 请求行按 iso-8859-1 解码后 split()，若字节流里含 0xA0（latin-1 的 NBSP 被当作空白）
# 会拆出 4 个词 → 400；不含 0xA0 时能进视图，由 _q() 还原 UTF-8。
_st, _body = raw_call(b"/api/skills?q=" + "乌鸦".encode("utf-8") + b"&limit=2", port=PORT)
_qd = json.loads(_body.decode())
ok(_st.startswith("HTTP/1.1 200") and _qd["query"] == "乌鸦",
   "直连 8091 裸 UTF-8（字节里无 0xA0）→ _q() 还原查询词", _qd.get("query"))
_st, _body = raw_call(b"/api/skills?q=" + "无人机".encode("utf-8") + b"&limit=2", port=PORT)
ok(_st.startswith("HTTP/1.1 400") and b"Bad request syntax" in _body,
   "直连 8091 裸 UTF-8 含 0xA0 → dev server 判为坏请求行（故前端必须 encodeURIComponent）", _st)

print("== 职业路线")
st, d = call("GET", "/api/careers")
ok(len(d["careers"]) == 40 and d["careers"][0]["plan_id"], "api/careers 40 条")
st, d = call("GET", "/api/career/21")
ok(d["career"]["name"] == "加达里财富猎手" and all(m.get("name") for m in d["career"]["milestones"]),
   "api/career/21 里程碑带技能名", d["career"]["milestones"][0])
ok(len(d["requirements"]) >= 29, "api/career/21 需求技能", len(d["requirements"]))
st, d = call("GET", "/api/career/9999")
ok(st == 404, "api/career 不存在 → 404", st)

print("== 计划复算")
targets = [{"kind": "type", "tid": 638}]
attrs = dict(charisma=17, intelligence=17, memory=17, perception=17, willpower=17)
st, p0 = call("POST", "/api/plan", {"targets": targets, "current": {}, "attrs": attrs})
ok(st == 200 and p0["summary"]["skills_missing"] == 6 and len(p0["rows"]) == 6, "从零起算 6 项缺口",
   p0.get("summary"))
ok(p0["rows"][0]["name"] == "飞船操控学" and p0["rows"][0]["seconds"] > 0, "行按前置排序",
   p0["rows"][0].get("name"))
cur = {r["tid"]: r["required"] for r in p0["rows"][:3]}   # 用真实前置技能的 tid 造「已有」快照
st, p1 = call("POST", "/api/plan", {"targets": targets, "current": cur, "attrs": attrs})
ok(p1["summary"]["skills_missing"] == 3 and p1["summary"]["skills_total"] == 6, "带当前技能 → 3 项缺口",
   p1["summary"])
st, p2 = call("POST", "/api/plan", {"targets": targets, "current": {}, "attrs": attrs,
                                    "options": {"include_owned": True}})
ok(len(p2["rows"]) == p2["summary"]["skills_total"] == 6, "include_owned 列出全部需求")
st, p3 = call("POST", "/api/plan", {"targets": targets, "current": {}, "attrs": attrs,
                                    "options": {"order": "shortest"}})
secs = [r["seconds"] for r in p3["rows"]]
ok(secs == sorted(secs), "order=shortest 生效", secs)
st, p4 = call("POST", "/api/plan", {"targets": targets, "character_id": CHAR_ID})
ok(st == 401 and "登录" in p4.get("error", ""),
   "匿名带 character_id 的计划复算 → 401（SSO 隔离）", st)
st, p5 = call("POST", "/api/plan", {"targets": [{"kind": "career", "tid": 21, "plan_id": 21}]})
ok(st == 200 and p5["summary"]["skills_total"] > 20, "职业路线目标", p5["summary"]["skills_total"])
st, p6 = call("POST", "/api/plan", {"targets": [{"kind": "skill", "tid": 3300, "level": 4}]})
ok(st == 200 and any(r["name"] == "射击学" for r in p6["rows"]), "单个技能目标")
st, d = call("POST", "/api/plan", {"targets": []})
ok(st == 400 and bool(d.get("error")), "空目标 → 400", st)
st, d = call("POST", "/api/plan", {"targets": [{"kind": "type", "tid": "abc"}]})
ok(st == 400, "非法 tid → 400", st)

print("== 导出 TXT")
st, body = call("POST", "/api/plan/txt", {"targets": targets, "current": {}, "attrs": attrs}, raw=True)
text = body.decode("utf-8")
lines = [ln for ln in text.split("\n") if ln.strip()]
ok(st == 200 and not body.startswith(b"\xef\xbb\xbf"), "TXT 无 BOM（纯文本）", st)
ok(len(lines) == 6, "TXT 6 行（乌鸦级 6 项需求，无表头/汇总）", len(lines))
ok(all(ln.startswith('<localized hint="') for ln in lines), "每行以 <localized hint= 开头", lines[0])
ok(all(ln.split("</localized> ", 1)[1] in "12345" for ln in lines), "每行结尾为 1-5 目标等级", lines[-1])
ok(sorted(int(ln.rsplit(" ", 1)[1]) for ln in lines) == [1, 3, 3, 3, 3, 4],
   "目标等级与乌鸦级需求一致（飞船操控学 4 / 战列舰 1 / 其余 3）", lines)
ok("飞船操控学" in text and "Caldari Battleship" in text, "TXT 含中英文技能名")

print("== 计划保存 / SSO 隔离（Flask test_client + 临时库，不碰线上数据）")
import tempfile
import config as _cfg
_cfg.APP_DB = os.path.join(tempfile.mkdtemp(prefix="esp_test_"), "test.db")
client = webapp.app.test_client()

# 未登录：保存被拒（401），也看不到 / 删不掉任何计划
st, d = call("POST", "/api/plans", {"name": "冒烟-乌鸦", "targets": targets, "current": cur,
                                    "attrs": attrs, "character_id": CHAR_ID})
ok(st == 401 and "未登录无法保存" in d.get("error", ""), "未登录保存 → 401（SSO 隔离）", st)
r = client.get("/api/plans")
jd = r.get_json()
ok(r.status_code == 200 and jd["plans"] == [] and jd["authed"] is False,
   "未登录 GET /api/plans → 空列表 + authed=false", jd)
r = client.get("/api/plans/1")
ok(r.status_code == 404, "未登录 GET 计划详情 → 404（不泄露他人数据）", r.status_code)
r = client.delete("/api/plans/1")
ok(r.status_code == 200 and r.get_json()["deleted"] is False, "未登录 DELETE → deleted=false", r.get_json())

# 角色甲登录：能创建 / 读取 / 复算 / 更新 / 删除自己的计划
with client.session_transaction() as s:
    s["cid"], s["name"] = 1111, "冒烟角色甲"
r = client.post("/api/plans", json={"name": "冒烟-乌鸦", "targets": targets, "current": cur,
                                    "attrs": attrs, "character_id": CHAR_ID})
pid = r.get_json()["plan"]["plan_id"]
ok(r.status_code == 200 and bool(pid), "登录后 POST /api/plans 创建", pid)
r = client.get("/api/plans")
jd = r.get_json()
ok(jd["authed"] is True and jd["login_cid"] == 1111 and jd["login_name"] == "冒烟角色甲"
   and any(p["plan_id"] == pid for p in jd["plans"]), "登录后列表含本人计划 + 会话信息", jd["login_name"])
r = client.get(f"/api/plans/{pid}")
ok(r.get_json()["plan"]["name"] == "冒烟-乌鸦" and bool(r.get_json()["plan"]["targets"]),
   "登录后 GET 详情")
r = client.post(f"/api/plans/{pid}/run", json={"refresh": False})
ok(r.status_code == 200 and r.get_json()["name"] == "冒烟-乌鸦"
   and r.get_json()["summary"]["skills_missing"] == 3, "run 用保存快照复算", r.get_json().get("summary"))
r = client.post("/api/plans", json={"plan_id": pid, "name": "冒烟-改名", "targets": targets,
                                    "current": cur, "attrs": attrs, "character_id": CHAR_ID})
ok(r.status_code == 200 and r.get_json()["plan"]["name"] == "冒烟-改名", "本人可更新计划", r.get_json())

# 角色乙登录：完全看不到 / 改不动甲的计划（按 SSO 角色隔离）
with client.session_transaction() as s:
    s["cid"], s["name"] = 2222, "冒烟角色乙"
r = client.get("/api/plans")
ok(r.status_code == 200 and r.get_json()["plans"] == [], "角色乙列表为空（看不到甲的）", r.get_json()["plans"])
r = client.get(f"/api/plans/{pid}")
ok(r.status_code == 404, "角色乙 GET 甲的计划 → 404", r.status_code)
r = client.post(f"/api/plans/{pid}/run", json={})
ok(r.status_code == 404, "角色乙 run 甲的计划 → 404", r.status_code)
r = client.post("/api/plans", json={"plan_id": pid, "name": "劫持", "targets": targets})
ok(r.status_code == 404, "角色乙不能更新甲的计划", r.status_code)
r = client.delete(f"/api/plans/{pid}")
ok(r.status_code == 200 and r.get_json()["deleted"] is False, "角色乙删不掉甲的计划", r.get_json())

# 角色甲回来：计划还在（含更新），可删除；删除后 404
with client.session_transaction() as s:
    s["cid"], s["name"] = 1111, "冒烟角色甲"
r = client.get(f"/api/plans/{pid}")
ok(r.status_code == 200 and r.get_json()["plan"]["name"] == "冒烟-改名", "甲的更新已生效（计划仍在）")
r = client.delete(f"/api/plans/{pid}")
ok(r.status_code == 200 and r.get_json()["deleted"] is True, "本人删除计划", r.get_json())
r = client.get(f"/api/plans/{pid}")
ok(r.status_code == 404, "删除后再查 → 404", r.status_code)
with client.session_transaction() as s:
    s.clear()

# 技能读取隔离：登录后只能读当前角色的技能 / 属性 / 队列，不能切换角色
with client.session_transaction() as s:
    s["cid"], s["name"] = 1111, "冒烟角色甲"
r = client.get("/api/characters")
jd = r.get_json()
ok(r.status_code == 200 and [c["id"] for c in jd["characters"]] == [1111]
   and jd["login"]["cid"] == 1111 and jd["login"]["name"] == "冒烟角色甲",
   "登录后 /api/characters 只列出当前登录角色（无切换入口）", jd)
r = client.get("/api/characters/1111/overview")
ok(r.status_code == 200 and ("skills" in r.get_json() or "skills_error" in r.get_json()),
   "登录后可读自己的 overview（无 token 时带 skills_error）", r.status_code)
r = client.get("/api/characters/2222/overview")
ok(r.status_code == 403 and "当前登录角色" in r.get_json().get("error", ""),
   "读其他角色 overview → 403（不可切换）", r.status_code)
r = client.get("/api/skill/3300?character_id=2222")
ok(r.status_code == 403, "带他人 character_id 的技能详情 → 403", r.status_code)
r = client.get("/api/type/638?character_id=2222")
ok(r.status_code == 403, "带他人 character_id 的类型需求 → 403", r.status_code)
r = client.post("/api/plan", json={"targets": targets, "character_id": 2222})
ok(r.status_code == 403, "带他人 character_id 的计划复算 → 403", r.status_code)
r = client.delete("/api/characters/2222")
ok(r.status_code == 403, "不能退出非当前登录角色 → 403", r.status_code)
r = client.delete("/api/characters/1111")
jd = r.get_json()
ok(r.status_code == 200 and jd.get("logged_out") is True, "退出登录（仅清会话，保留 token）", jd)
r = client.get("/api/characters")
jd = r.get_json()
ok(r.status_code == 200 and jd["characters"] == [] and jd["login"] is None,
   "退出后 /api/characters 恢复空列表", jd)
r = client.get("/api/characters/1111/overview")
ok(r.status_code == 401, "退出后再读 overview → 401", r.status_code)
with client.session_transaction() as s:
    s.clear()

# 登录态读真实角色技能（token 目录有测试角色时才断言）
if os.path.exists(os.path.join(_cfg.TOKEN_DIR, f"{CHAR_ID}.json")):
    with client.session_transaction() as s:
        s["cid"], s["name"] = CHAR_ID, "真实角色"
    r = client.get(f"/api/characters/{CHAR_ID}/overview")
    jd = r.get_json()
    ok(r.status_code == 200 and jd.get("name") and len(jd.get("skills", {})) > 100
       and jd.get("total_sp", 0) > 1e6,
       "登录后可读真实角色的技能 / SP / 属性 / 队列",
       [jd.get("name"), len(jd.get("skills", {})), jd.get("total_sp")])
    r = client.post("/api/plan", json={"targets": targets, "character_id": CHAR_ID})
    jd = r.get_json()
    ok(r.status_code == 200 and jd.get("character_id") == CHAR_ID,
       "登录后带自己 character_id 的计划复算走 ESI", jd.get("summary"))
    with client.session_transaction() as s:
        s.clear()
else:
    print("  (token 目录无测试角色，跳过真实角色 overview / ESI 断言 —— 未走 SSO 时属正常)")

# 真实服务端（无 Cookie 的匿名视角）：不存在的计划 404；角色接口按 SSO 隔离拒绝
st, d = call("GET", "/api/plans/999999")
ok(st == 404, "GET 不存在计划 → 404", st)
st, d = call("POST", "/api/plans/999999/run", {})
ok(st == 404, "run 不存在计划 → 404", st)

print("== 角色 / SSO（只能读当前登录角色，不可切换）")
st, d = call("GET", "/api/characters")
ok(st == 200 and d.get("characters") == [] and d.get("login") is None,
   "匿名 /api/characters → 空列表（不泄露 token 目录角色）", d.get("characters"))
st, d = call("GET", f"/api/characters/{CHAR_ID}/overview")
ok(st == 401 and "登录" in d.get("error", ""), "匿名 overview → 401（未登录不能读任何角色技能）", st)
st, d = call("GET", "/api/characters/1/overview")
ok(st == 401, "匿名 overview(其他 cid) → 401", st)
st, d = call("GET", "/api/type/638?character_id=1")
ok(st == 401, "匿名带 character_id 的类型需求 → 401", st)
st, d = call("POST", "/api/plan", {"targets": targets, "character_id": 1})
ok(st == 401, "匿名带 character_id 的计划复算 → 401", st)
st, d = call("GET", "/api/oauth/url")
ok(st == 200 and d["url"].startswith("https://login.eveonline.com/v2/oauth/authorize")
   and "code_challenge_method=S256" in d["url"] and "redirect_uri=" in d["url"] and d.get("state"),
   "api/oauth/url 生成 PKCE 授权链接", d.get("url", "")[:70])
ok("esi-skills.read_skills.v1" in urllib.parse.unquote_plus(d["url"]), "授权链接含技能 scope")
st, d = call("GET", "/oauth/callback")
ok(st == 400 and bool(d.get("error")), "oauth/callback 缺 code/state → 400", st)

print("\n通过 %d 项，失败 %d 项" % (passed, len(failed)))
if failed:
    print("失败项：\n - " + "\n - ".join(failed))
    raise SystemExit(1)
print("HTTP 冒烟测试全部通过 ✅")
