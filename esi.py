"""ESI 客户端：复用既有角色 token（~/.eve-skill-planner/tokens/<cid>.json），
过期自动刷新；提供技能 / 属性 / 技能队列 / 角色列表接口。

与 /root/eve-skill-planner 共用 token 目录，因此在装配站授权过的角色，
在本站「角色」里直接可选，不必重新走 SSO。
"""

import base64
import json
import os
import threading
import time

import requests

import config

_lock = threading.Lock()
_cache = {}          # cid -> (token_dict, load_ts)
TOKEN_TTL = 1200     # 秒（< access_token 20 分钟有效期）


def _jwt_claims(access_token):
    """解析 access_token（JWT）的 payload claims；解析失败返回 {}。"""
    try:
        payload = str(access_token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)) or {}
    except Exception:
        return {}


def _granted_scopes(access_token):
    """从 access_token（JWT）解析 EVE 实际授予的 scope。"""
    return set(_jwt_claims(access_token).get("scp") or [])


def _jwt_exp(access_token):
    """从 access_token（JWT）解析过期时间（epoch 秒）；解析失败返回 None。

    早期 token 文件只存了 EVE 返回的相对秒数 expires_in、没有绝对时间戳 expires，
    此时用 JWT 自带的 exp claim 判断过期，避免 401 才暴露。
    """
    exp = _jwt_claims(access_token).get("exp")
    try:
        return float(exp) if exp else None
    except (TypeError, ValueError):
        return None


def _token_path(cid):
    return os.path.join(config.TOKEN_DIR, f"{int(cid)}.json")


def load_token(cid):
    """读取 tokens/<cid>.json（Web 格式 {id,name,scopes,access_token,refresh_token}）。"""
    path = _token_path(cid)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_token(cid, token):
    try:
        os.makedirs(config.TOKEN_DIR, exist_ok=True)
        with open(_token_path(cid), "w", encoding="utf-8") as f:
            json.dump(token, f, ensure_ascii=False)
    except OSError:
        pass


def save_token(cid, token):
    """OAuth 回调写入 token（与既有站点同一格式，两端可互相识别）。"""
    _save_token(cid, token)
    with _lock:
        _cache.pop(int(cid), None)


def _verify_name(access_token):
    """用 access_token 拉角色名（登录校验，兼容 2026 仍可用的 /oauth/verify）。"""
    try:
        resp = requests.get("https://login.eveonline.com/oauth/verify",
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        if resp.ok:
            return resp.json().get("CharacterName")
    except requests.RequestException:
        pass
    return None


def _refresh(cid, token):
    """刷新 access_token 并回写 token 文件。"""
    client_id, secret, _ = config.eve_credentials()
    resp = requests.post(config.TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
        "client_id": client_id,
        "client_secret": secret,
    }, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    token["access_token"] = data["access_token"]
    if data.get("refresh_token"):
        token["refresh_token"] = data["refresh_token"]
    token["expires"] = time.time() + float(data.get("expires_in") or 1200)
    token["scopes"] = sorted(_granted_scopes(token["access_token"]))
    _save_token(cid, token)
    return token


def get_token(cid):
    """有效 token（内存缓存 + 过期自动刷新）。"""
    cid = int(cid)
    now = time.time()
    with _lock:
        cached = _cache.get(cid)
    if cached and now - cached[1] < TOKEN_TTL:
        token = dict(cached[0])
    else:
        token = load_token(cid)
        if not token:
            raise RuntimeError(f"角色 {cid} 没有授权记录（请重新走 SSO）")
        _cache[cid] = (dict(token), now)
    # 绝对过期时间：优先 token 文件的 expires，缺失时回退到 JWT 的 exp claim，
    # 两者都没有就视为过期（有 refresh_token 就刷新，无则抛错让用户重新授权）。
    expires = token.get("expires") or _jwt_exp(token.get("access_token", ""))
    if not expires or float(expires) - now < 120:
        token = _refresh(cid, token)
        with _lock:
            _cache[cid] = (dict(token), now)
    return token


def list_characters():
    """已授权角色列表（从 token 目录推断，不含敏感字段）。"""
    out = []
    if not os.path.isdir(config.TOKEN_DIR):
        return out
    for fn in sorted(os.listdir(config.TOKEN_DIR)):
        cid = fn[:-5] if fn.endswith(".json") else ""
        if not cid.isdigit():
            continue
        token = load_token(cid)
        if not token:
            continue
        name = token.get("name") or token.get("CharacterName")
        scopes = token.get("scopes") or sorted(_granted_scopes(token.get("access_token", "")))
        if not name:
            try:
                name = _verify_name(get_token(int(cid))["access_token"]) or cid
            except Exception:
                name = cid
        out.append({"id": int(cid), "name": name, "scopes": sorted(scopes),
                    "can_read_skills": "esi-skills.read_skills.v1" in set(scopes)})
    return out


def forget(cid):
    """退出该角色：删除 token 文件并清缓存（本站只删本地记录，不动共享的旧 CLI token）。"""
    cid = int(cid)
    token = load_token(cid) or {}
    path = _token_path(cid)
    removed = False
    if os.path.exists(path):
        try:
            os.remove(path)
            removed = True
        except OSError:
            pass
    with _lock:
        _cache.pop(cid, None)
    return {"id": cid, "name": token.get("name") or token.get("CharacterName") or str(cid),
            "removed": removed}


def _headers(cid):
    return {"Authorization": f"Bearer {get_token(cid)['access_token']}",
            "User-Agent": "eve-skill-plan/1.0 (your-contact@example.com)"}


def _force_refresh(cid):
    """无视本地过期判断，强制用 refresh_token 刷新（ESI 401 的兜底）。"""
    cid = int(cid)
    token = load_token(cid) or {}
    if not token.get("refresh_token"):
        raise RuntimeError(f"角色 {cid} 缺少 refresh_token，请重新走 SSO 授权")
    refreshed = _refresh(cid, token)
    with _lock:
        _cache[cid] = (dict(refreshed), time.time())
    return refreshed


def esi_get(cid, path, params=None, retry=1):
    """带角色授权的 GET。

    - 401：token 失效（本地过期判断未覆盖，如服务端吊销/换应用轮换），强制刷新后重试一次
    - 502：ESI 偶发故障，稍等重试一次
    """
    url = f"{config.ESI_BASE}{path}"
    headers = _headers(cid)
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 401 and retry > 0:
        _force_refresh(cid)
        headers = _headers(cid)
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        retry -= 1
    if resp.status_code == 502 and retry > 0:
        time.sleep(1)
        resp = requests.get(url, headers=headers, params=params, timeout=30)
    if resp.status_code == 403:
        raise RuntimeError("该角色未授予所需 scope（请重新授权并勾选技能读权限）")
    resp.raise_for_status()
    return resp.json()


def get_skills(cid):
    """角色技能等级 {skill_type_id: 活跃等级}（需 esi-skills.read_skills.v1）。"""
    data = esi_get(cid, f"/latest/characters/{int(cid)}/skills/?datasource=tranquility")
    return {int(s["skill_id"]): int(s.get("active_skill_level") or 0)
            for s in data.get("skills", [])}


def get_skill_detail(cid):
    """技能接口原始返回（含总 SP），用于展示角色总技能点。"""
    return esi_get(cid, f"/latest/characters/{int(cid)}/skills/?datasource=tranquility")


def get_attributes(cid):
    """角色有效属性（含植入体）：{perception: n, ...}。

    实测 ESI 该接口返回扁平整数（与 swagger 的 base/implant 对象不同），两种形状都兼容。
    """
    data = esi_get(cid, f"/latest/characters/{int(cid)}/attributes/?datasource=tranquility")
    out = {}
    for key in ("charisma", "intelligence", "memory", "perception", "willpower"):
        val = data.get(key)
        if isinstance(val, dict):
            out[key] = float(val.get("base") or 0) + float(val.get("implant") or 0)
        elif isinstance(val, (int, float)):
            out[key] = float(val)
    return out


def get_skillqueue(cid):
    """角色训练队列（需 esi-skills.read_skillqueue.v1）。"""
    return esi_get(cid, f"/latest/characters/{int(cid)}/skillqueue/?datasource=tranquility")

