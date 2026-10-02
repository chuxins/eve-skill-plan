"""eve-skill-plan Web 应用（Flask，监听 :8091）。

- 前端：/（static/index.html，注入静态资源版本号）
- 技能库：/api/skillgroups、/api/skills、/api/skill/<tid>
- 需求查询：/api/types、/api/type/<tid>
- 职业路线：/api/careers、/api/career/<plan_id>
- 计划：/api/plan（POST 复算）、/api/plan/txt（导出）、/api/plans（保存/读取/删除）
- 角色：/api/characters（列表/退出）、/api/characters/<cid>/overview（技能+属性+队列）
- OAuth：/api/oauth/url → 浏览器跳转 EVE SSO → nginx 反代 /skills/oauth/callback

SSO 隔离：OAuth 成功后签发会话 Cookie（esp_session），保存的技能训练计划
按「创建它的登录角色」隔离 —— 未登录无法保存（POST /api/plans → 401），
也只能读取/修改/删除自己创建的计划。

nginx 里 /skills/ 前缀会被剥掉（proxy_pass 末尾带 /），因此本站路径都是裸的，
前端一律用相对路径（static/…、api/…），换部署前缀不用改代码。
"""

import argparse
import hashlib
import json
import logging
import os
import secrets
from datetime import timedelta
from urllib.parse import urlencode

from flask import Flask, jsonify, redirect, request, send_from_directory, session

import config
import esi
import oauth
from engine import planner, store, training
from engine.skills import CAT_NAMES, get_index

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webapp")

app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False
try:                                        # 单进程部署：OAuth pending state 放内存即可
    app.json.sort_keys = False
except Exception:
    pass

# SSO 登录会话：签名 Cookie 标识「当前登录角色」，保存/读取计划按它隔离。
# 密钥持久化在 data/.session_secret（见 config.SESSION_SECRET），重启不失效。
app.secret_key = config.SESSION_SECRET
app.config.update(
    SESSION_COOKIE_NAME=config.SESSION_COOKIE_NAME,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(days=config.SESSION_DAYS),
)

_states = {}                                # state -> {verifier, next}


# ---------------------------------------------------------------- 登录身份
def _login():
    """当前 SSO 登录身份 (cid, name)；未登录返回 (None, None)。"""
    cid = session.get("cid")
    if not cid:
        return None, ""
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return None, ""
    return cid, session.get("name") or str(cid)


def _login_or_401():
    """要求已登录（保存计划的前置条件）；未登录返回 (None, 401 响应)。"""
    cid, _ = _login()
    if not cid:
        return None, _fail("未登录无法保存技能训练计划（请先通过 EVE SSO 登录）", 401)
    return cid, None


# ---------------------------------------------------------------- 通用
def _index():
    return get_index()


def _int_arg(name, default=None, low=None, high=None):
    try:
        val = int(float(request.args.get(name, default if default is not None else "")))
    except (TypeError, ValueError):
        return default
    if low is not None and val < low:
        val = low
    if high is not None and val > high:
        val = high
    return val


def _payload():
    """POST body（JSON 或表单）→ dict。"""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    return {k: v for k, v in request.form.items()}


def _q(name, default=""):
    """查询参数（容错解码）。

    浏览器会把 URL 里的 UTF-8 做百分号编码，但 curl / 旧客户端常直接塞原始字节，
    此时 WSGI 层按 latin-1 解码会得到乱码 —— 这里再做一次 latin-1 → utf-8 还原。
    """
    raw = request.args.get(name, default)
    if not raw:
        return default
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


def _fail(message, code=400):
    return jsonify({"error": message}), code


def _normalize_targets(raw):
    """前端目标列表 → planner 需要的结构（兼容字符串 tid）。"""
    out = []
    for t in raw or []:
        if not isinstance(t, dict):
            continue
        kind = (t.get("kind") or "type").lower()
        tid = t.get("tid") or t.get("plan_id") or t.get("type_id")
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            continue
        item = {"kind": kind, "tid": tid}
        if t.get("plan_id"):
            item["plan_id"] = int(t["plan_id"])
        if t.get("level"):
            try:
                item["level"] = max(1, min(5, int(t["level"])))
            except (TypeError, ValueError):
                pass
        out.append(item)
    return out


def _current_skills(data):
    """当前技能：显式传入优先，否则取 character_id 的 ESI 技能表。"""
    current = data.get("current") if isinstance(data, dict) else None
    if isinstance(current, dict) and current:
        return {int(k): int(v or 0) for k, v in current.items()}, None
    cid = data.get("character_id") if isinstance(data, dict) else None
    if not cid:
        return {}, None
    try:
        return esi.get_skills(int(cid)), int(cid)
    except Exception as exc:                # ESI/授权异常不应让计划接口整体失败
        log.warning("读取角色 %s 技能失败：%s", cid, exc)
        return {}, int(cid)


def _attrs(data):
    """属性：显式传入优先，否则取 character_id 的 ESI 有效属性。"""
    attrs = data.get("attrs") if isinstance(data, dict) else None
    if isinstance(attrs, dict) and attrs:
        return training.normalize_attrs(attrs)
    cid = data.get("character_id") if isinstance(data, dict) else None
    if cid:
        try:
            return training.normalize_attrs(esi.get_attributes(int(cid)))
        except Exception as exc:
            log.warning("读取角色 %s 属性失败：%s", cid, exc)
    return dict(training.DEFAULT_ATTRS)


def _asset_version():
    """静态资源版本号：static/ 下所有文件 (相对路径, mtime, 大小) 的摘要。"""
    h = hashlib.md5()
    for root, _dirs, files in os.walk(config.STATIC_DIR):
        for fn in sorted(files):
            path = os.path.join(root, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            rel = os.path.relpath(path, config.STATIC_DIR)
            h.update(f"{rel}:{st.st_mtime_ns}:{st.st_size}".encode("utf-8"))
    return h.hexdigest()[:10]


# ---------------------------------------------------------------- 静态
@app.route("/")
def page_index():
    # 兼容「EVE 门户里把回调登记成站点根」的写法（形如 http://host/skills/）：
    # 此时 SSO 会把 ?code=…&state=… 送到根路径，这里原样转交给 /oauth/callback。
    # 用相对路径（"./oauth/callback"）而非绝对路径，因为 nginx 会剥掉 /skills 前缀：
    # 浏览器看到的是 /skills/…，Flask 看到的是 /…（与 /oauth/callback 自身的回跳一致）。
    if request.args.get("code") and request.args.get("state"):
        return redirect("./oauth/callback?" + urlencode(request.args))
    with open(os.path.join(config.STATIC_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()
    resp = app.response_class(html.replace("{{ASSET_VERSION}}", _asset_version()),
                              mimetype="text/html")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/favicon.ico")
def favicon_ico():
    return "", 204


@app.route("/static/<path:name>")
def static_files(name):
    resp = send_from_directory(config.STATIC_DIR, name)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/healthz")
def healthz():
    idx = _index()
    return jsonify({"ok": True, "skills": len(idx.skills), "types": len(idx.types),
                    "built_at": idx.meta.get("built_at", ""), "port": config.PORT})


# ---------------------------------------------------------------- 技能库
@app.route("/api/meta")
def api_meta():
    idx = _index()
    return jsonify({
        "sde": {k: idx.meta.get(k, "") for k in
                ("source", "built_at", "skills", "types", "type_reqs", "career_plans")},
        "counts": {"skills": len(idx.skills), "groups": len(idx.groups),
                   "types": len(idx.types), "careers": len(idx.career_plans)},
        "categories": [{"id": c, "name": n} for c, n in sorted(CAT_NAMES.items())],
        "req_sources": list(config.CAT_REQ_SOURCES),
        "attribute_presets": planner.attributes_grid(),
        "attribute_names": training.ATTR_NAMES,
        "levels": list(training.LEVELS),
        "scopes": config.SCOPES,
    })


@app.route("/api/skillgroups")
def api_skillgroups():
    return jsonify({"groups": _index().groups_tree()})


@app.route("/api/skills")
def api_skills():
    idx = _index()
    query = _q("q")
    group_id = _int_arg("group_id")
    limit = _int_arg("limit", 60, 1, 500) or 60
    return jsonify({"query": query, "skills": idx.search_skills(query, limit, group_id)})


def _skill_refs(idx, need):
    """{技能 tid: 等级} → 按前置优先排序的引用列表（供前端展示）。"""
    out = []
    for sk in idx.topo_order(need):
        info = idx.skills.get(sk, {})
        out.append({"tid": sk, "name": idx.name(sk), "group": info.get("group", ""),
                    "rank": info.get("rank", 1), "level": need[sk]})
    return out


@app.route("/api/skill/<int:tid>")
def api_skill(tid):
    """技能详情：描述 / 属性 / 逐级时间 / 前置 / 被谁需要（可选带角色当前等级）。"""
    idx = _index()
    skill = idx.skills.get(tid)
    if not skill:
        return _fail(f"没有这个技能：{tid}", 404)
    cid = _int_arg("character_id")
    current, attrs = 0, dict(training.DEFAULT_ATTRS)
    if cid:
        try:
            current = int(esi.get_skills(cid).get(tid, 0))
        except Exception as exc:
            log.warning("读取角色 %s 技能失败：%s", cid, exc)
        try:
            attrs = training.normalize_attrs(esi.get_attributes(cid))
        except Exception:
            pass
    p_key, s_key = training.skill_attrs(skill)
    closure = idx.closure([tid])
    return jsonify({
        "skill": {
            "tid": tid, "name": skill["name"], "name_en": skill["name_en"],
            "group": skill["group"], "group_id": skill["group_id"],
            "rank": round(skill["rank"], 2), "description": skill["description"],
            "primary": p_key, "secondary": s_key,
            "primary_name": training.ATTR_NAMES.get(p_key, p_key),
            "secondary_name": training.ATTR_NAMES.get(s_key, s_key),
        },
        "current": current, "attributes": attrs,
        "level_sp": {lv: round(training.sp_to_level(skill["rank"], lv), 0)
                     for lv in training.LEVELS},
        "level_seconds": training.level_table(skill, attrs, 0),
        "level_seconds_from_current": training.level_table(skill, attrs, current),
        "prereqs": [{"tid": p, "name": idx.name(p), "level": lv,
                     "rank": idx.skills.get(p, {}).get("rank", 1)}
                    for p, lv in idx.direct_reqs(tid)],
        "closure": _skill_refs(idx, closure),
        "consumers": idx.consumers(tid),
    })


# ---------------------------------------------------------------- 需求查询
@app.route("/api/types")
def api_types():
    idx = _index()
    query = _q("q")
    cats = request.args.get("cats")
    categories = [int(c) for c in cats.split(",") if c.strip().lstrip("-").isdigit()] \
        if cats else None
    limit = _int_arg("limit", 40, 1, 200) or 40
    return jsonify({"query": query, "types": idx.search_types(query, categories, limit)})


@app.route("/api/type/<int:tid>")
def api_type(tid):
    """舰船/装备/弹药/无人机的需求技能（可选对照角色当前等级）。"""
    idx = _index()
    if tid not in idx.types:
        return _fail(f"没有这个类型：{tid}", 404)
    cid = _int_arg("character_id")
    current, attrs = {}, dict(training.DEFAULT_ATTRS)
    if cid:
        try:
            current = esi.get_skills(cid)
        except Exception as exc:
            log.warning("读取角色 %s 技能失败：%s", cid, exc)
        try:
            attrs = training.normalize_attrs(esi.get_attributes(cid))
        except Exception:
            pass
    refs = _skill_refs(idx, idx.closure([tid]))
    missing = 0
    for ref in refs:
        ref["current"] = int(current.get(ref["tid"], 0))
        ref["ok"] = ref["current"] >= ref["level"]
        missing += 0 if ref["ok"] else 1
    t = idx.types[tid]
    return jsonify({
        "type": {"tid": tid, "name": t["name"], "name_en": t["name_en"],
                 "group": t["group"], "category": t["category"],
                 "category_id": t["category_id"], "is_skill": t["is_skill"],
                 "description": t.get("description", "")},
        "direct": [{"tid": p, "name": idx.name(p), "level": lv}
                   for p, lv in idx.direct_reqs(tid)],
        "requirements": refs,
        "skills": len(refs), "missing": missing,
        "character_id": cid, "attributes": attrs,
    })


# ---------------------------------------------------------------- 职业路线
@app.route("/api/careers")
def api_careers():
    idx = _index()
    return jsonify({"careers": [
        {"plan_id": p["plan_id"], "name": p["name"], "name_en": p["name_en"],
         "faction_id": p["faction_id"], "career_path_id": p["career_path_id"],
         "internal_name": p["internal_name"], "skills": len(p["skills"]),
         "milestones": len(p["milestones"]), "description": p["description"]}
        for p in idx.career_plans]})


@app.route("/api/career/<int:plan_id>")
def api_career(plan_id):
    idx = _index()
    plan = next((p for p in idx.career_plans if p["plan_id"] == plan_id), None)
    if not plan:
        return _fail(f"没有这条职业路线：{plan_id}", 404)
    need = idx.closure([(s["tid"], s["level"]) for s in plan["skills"]])
    # 里程碑在 SDE 里只存 {type_id, level}，这里补上技能名/组，前端直接用
    milestones = [{"tid": m.get("type_id"), "level": m.get("level"),
                   "name": idx.name(m.get("type_id")),
                   "group": idx.skills.get(m.get("type_id"), {}).get("group", ""),
                   "rank": idx.skills.get(m.get("type_id"), {}).get("rank", 1)}
                  for m in plan["milestones"]]
    return jsonify({
        "career": dict(plan, milestones=milestones),
        "requirements": _skill_refs(idx, need),
    })


# ---------------------------------------------------------------- 计划
@app.route("/api/plan", methods=["POST"])
def api_plan():
    """依据目标 + 当前技能 + 属性复算技能计划。"""
    data = _payload()
    targets = _normalize_targets(data.get("targets"))
    options = data.get("options") if isinstance(data.get("options"), dict) else {}
    current, cid = _current_skills(data)
    attrs = _attrs(data)
    if not targets:
        return _fail("请先添加至少一个目标（舰船 / 装备 / 技能 / 职业路线）")
    try:
        plan = planner.build_plan(_index(), targets, current=current, attrs=attrs,
                                  options=options)
    except Exception as exc:
        log.exception("构建计划失败")
        return _fail(f"构建计划失败：{exc}", 500)
    plan["character_id"] = cid
    plan["current"] = current
    return jsonify(plan)


@app.route("/api/plan/txt", methods=["POST"])
def api_plan_txt():
    """计划导出为 EVE 技能计划文本（eve-skill.com 兼容格式，可粘贴导入）。"""
    data = _payload()
    targets = _normalize_targets(data.get("targets"))
    if not targets:
        return _fail("请先添加至少一个目标")
    current, _cid = _current_skills(data)
    plan = planner.build_plan(_index(), targets, current=current, attrs=_attrs(data),
                              options=data.get("options") or {})
    body = planner.plan_txt(_index(), plan)
    resp = app.response_class(body, mimetype="text/plain; charset=utf-8")
    resp.headers["Content-Disposition"] = "attachment; filename=skill-plan.txt"
    return resp


@app.route("/api/plans", methods=["GET", "POST"])
def api_plans():
    cid, name = _login()
    if request.method == "GET":
        # 只返回当前 SSO 登录角色的计划；未登录 → 空列表（authed=false），前端据此显示登录引导
        return jsonify({"plans": store.list_plans(cid) if cid else [],
                        "authed": cid is not None,
                        "login_cid": cid, "login_name": name})
    # 保存必须登录（SSO 隔离）：未登录直接 401
    if not cid:
        return _fail("未登录无法保存技能训练计划（请先通过 EVE SSO 登录）", 401)
    data = _payload()
    targets = _normalize_targets(data.get("targets"))
    if not targets:
        return _fail("计划至少要有一个目标")
    plan_id = data.get("plan_id")
    if plan_id:                              # 更新已有计划：必须是本人创建（隔离）
        existing = store.get_plan(plan_id, owner_cid=cid)
        if not existing:
            return _fail(f"没有这个计划：{plan_id}", 404)
        plan_id = int(plan_id)
    plan = store.save_plan(data.get("name"), targets, skills=data.get("current"),
                           attrs=data.get("attrs"), options=data.get("options"),
                           character_id=data.get("character_id"),
                           owner_cid=cid, plan_id=plan_id)
    return jsonify({"plan": plan})


@app.route("/api/plans/<int:plan_id>", methods=["GET", "DELETE"])
def api_plan_item(plan_id):
    cid, _ = _login()
    if not cid:                              # 未登录视为「不存在」，不泄露任何计划
        if request.method == "DELETE":
            return jsonify({"deleted": False})
        return _fail(f"没有这个计划：{plan_id}", 404)
    if request.method == "DELETE":
        ok = store.delete_plan(plan_id, owner_cid=cid)
        return jsonify({"deleted": ok})
    plan = store.get_plan(plan_id, owner_cid=cid)
    if not plan:
        return _fail(f"没有这个计划：{plan_id}", 404)
    return jsonify({"plan": plan})


@app.route("/api/plans/<int:plan_id>/run", methods=["POST"])
def api_plan_run(plan_id):
    """载入已保存计划并用其快照（或指定角色）重新计算（仅本人创建的计划）。"""
    cid, _ = _login()
    saved = store.get_plan(plan_id, owner_cid=cid) if cid else None
    if not saved:
        return _fail(f"没有这个计划：{plan_id}", 404)
    data = _payload()
    refresh = bool(data.get("refresh"))          # refresh=True → 重新拉 ESI 技能
    current = {} if refresh else dict(saved["skills"] or {})
    if refresh or not current:
        current, _cid = _current_skills({"character_id": data.get("character_id")
                                        or saved.get("character_id")})
    attrs = dict(saved["attrs"] or {})
    if refresh or not attrs:
        attrs = _attrs({"character_id": data.get("character_id") or saved.get("character_id")})
    plan = planner.build_plan(_index(), saved["targets"], current=current, attrs=attrs,
                              options=saved["options"] or {})
    plan["plan_id"] = plan_id
    plan["name"] = saved["name"]
    plan["current"] = current
    return jsonify(plan)




# ---------------------------------------------------------------- 角色 / SSO
@app.route("/api/characters")
def api_characters():
    """已授权角色（含共享 token 目录里其他站点授权过的角色）+ 当前 SSO 登录身份。"""
    try:
        chars = esi.list_characters()
    except Exception as exc:
        log.warning("读取角色列表失败：%s", exc)
        chars = []
    login_cid, login_name = _login()
    return jsonify({
        "characters": chars,
        "token_dir": config.TOKEN_DIR,
        "login": {"cid": login_cid, "name": login_name} if login_cid else None,
    })


@app.route("/api/characters/<int:cid>", methods=["DELETE"])
def api_character_forget(cid):
    cur, _ = _login()
    if cur == cid:                           # 退出的是当前登录角色 → 一并清掉会话
        session.clear()
    return jsonify(esi.forget(cid))


@app.route("/api/characters/<int:cid>/overview")
def api_character_overview(cid):
    """角色概览：技能等级 / 有效属性 / 训练队列 / 技能点。"""
    out = {"id": cid, "name": str(cid)}
    for ch in esi.list_characters():
        if ch["id"] == cid:
            out["name"] = ch["name"]
            out["scopes"] = ch["scopes"]
            break
    try:
        detail = esi.get_skill_detail(cid)
        out["skills"] = {int(s["skill_id"]): int(s.get("active_skill_level") or 0)
                         for s in detail.get("skills", [])}
        out["total_sp"] = detail.get("total_sp")
        out["unallocated_sp"] = detail.get("unallocated_sp")
    except Exception as exc:
        out["skills_error"] = str(exc)
    try:
        out["attributes"] = training.normalize_attrs(esi.get_attributes(cid))
    except Exception as exc:
        out["attributes_error"] = str(exc)
    try:
        idx = get_index().skills
        out["queue"] = []
        for q in esi.get_skillqueue(cid) or []:
            item = dict(q)
            sk = idx.get(int(q.get("skill_id") or 0))
            item["skill_name"] = sk["name"] if sk else f"#{q.get('skill_id')}"
            item["skill_name_en"] = sk["name_en"] if sk else ""
            out["queue"].append(item)
    except Exception as exc:
        out["queue"] = None
        out["queue_error"] = str(exc)
    return jsonify(out)


@app.route("/api/oauth/url")
def api_oauth_url():
    """返回授权链接（前端新标签打开，授权后回跳站内）。"""
    try:
        state, verifier, _challenge = oauth.new_state()
        url = oauth.authorize_url(state, verifier)
    except RuntimeError as exc:
        return _fail(str(exc), 500)
    nxt = request.args.get("next") or ""
    _states[state] = {"verifier": verifier, "next": nxt}
    while len(_states) > 200:               # 防止内存无限增长
        _states.pop(next(iter(_states)))
    return jsonify({"url": url, "state": state})


@app.route("/oauth/start")
def oauth_start():
    resp = api_oauth_url()
    if isinstance(resp, tuple):
        return resp
    return redirect(resp.get_json()["url"])


@app.route("/oauth/callback")
@app.route("/callback")
def oauth_callback():
    """EVE SSO 回调：换 token → 写入共享 token 目录 → 回前端。

    /oauth/callback 与 /callback 均可（后者供本地测试登记 http://localhost:8001/callback）。
    """
    code = request.args.get("code")
    state = request.args.get("state") or ""
    pending = _states.pop(state, None)
    if request.args.get("error"):
        return redirect(f"./?sso_error={request.args.get('error')}")
    if not code or not pending:
        return _fail("授权回调缺少 code/state（请从站内「登录」按钮重新发起）")
    try:
        result = oauth.exchange_code(code, pending["verifier"])
    except Exception as exc:
        log.exception("OAuth 换取 token 失败")
        return _fail(f"授权失败：{exc}", 500)
    esi.save_token(result["id"], result["token"])
    # SSO 登录成功：签发会话 Cookie，后续保存/读取计划以此角色隔离
    session.permanent = True
    session["cid"] = int(result["id"])
    session["name"] = result["name"]
    log.info("角色 %s(%s) 授权成功并登录，scopes=%s", result["name"], result["id"], result["scopes"])
    # 回调路径被 nginx 剥掉 /skills 前缀，故用相对路径回站点根（index.html 读 ?cid=）
    return redirect(pending.get("next") or f"./?cid={result['id']}")


# ---------------------------------------------------------------- 启动
def main():
    ap = argparse.ArgumentParser(description="EVE 技能规划站（Flask）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()
    idx = get_index()
    log.info("技能索引：%s 个技能 / %s 个类型 / %s 条需求（构建于 %s）",
             len(idx.skills), len(idx.types), idx.meta.get("type_reqs", "?"),
             idx.meta.get("built_at", "?"))
    store.connect().close()                 # 确保 data/app.db 就绪
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()

