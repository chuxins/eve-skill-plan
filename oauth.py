"""EVE SSO（PKCE）：发起授权 → 回调换 token → 写入共享 token 目录。

与 /root/eve-skill-planner 的 oauth.py 同一套流程与 token 文件格式，
差别只在 scope（本站只读技能/队列）与回调地址（/skills/oauth/callback）。
"""

import base64
import hashlib
import json
import secrets
from urllib.parse import urlencode

import requests

import config


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _basic_auth(client_id, client_secret):
    raw = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.urlsafe_b64encode(raw).decode("ascii")


def new_state():
    """生成 state + PKCE verifier（调用方需把 verifier 随 state 暂存，回调时复用）。"""
    verifier, challenge = _pkce_pair()
    return secrets.token_urlsafe(24), verifier, challenge


def authorize_url(state, verifier=None):
    """构造 EVE SSO 授权 URL（redirect_uri = config.json 的 callback_url）。"""
    client_id, _, callback = config.eve_credentials()
    if verifier is None:
        verifier, _ = _pkce_pair()
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return config.SSO_AUTHORIZE + "?" + urlencode({
        "response_type": "code",
        "redirect_uri": callback,
        "client_id": client_id,
        "scope": " ".join(config.SCOPES),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def scp(access_token):
    """从 access_token（JWT）解析 EVE 实际授予的 scope 列表。"""
    try:
        payload = str(access_token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return [str(x) for x in (claims.get("scp") or [])]
    except Exception:
        return []


def _character_id(access_token):
    """从 JWT 的 sub 取角色 ID（形如 CHARACTER:EVE:123456）。"""
    try:
        payload = str(access_token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        sub = str(claims.get("sub") or "")
        return int(sub.rsplit(":", 1)[-1])
    except Exception:
        return None


def exchange_code(code, verifier):
    """授权码换 token → {id, name, scopes, token}（token 直接可写入 token 目录）。"""
    client_id, client_secret, callback = config.eve_credentials()
    resp = requests.post(config.TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": callback,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
    }, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"换取 token 失败: {resp.status_code} {resp.text[:200]}")
    token = resp.json()
    export = requests.get("https://login.eveonline.com/oauth/verify",
                          headers={"Authorization": f"Bearer {token['access_token']}"},
                          timeout=30)
    cid, name = None, None
    if export.ok:                        # 兼容旧 verify 接口
        char = export.json()
        cid = int(char.get("CharacterID") or 0) or None
        name = char.get("CharacterName")
    if not cid:                          # verify 不可用时退回 JWT claims
        cid = _character_id(token["access_token"])
    if not cid:
        raise RuntimeError("无法从 token 解析角色 ID（SSO 返回异常）")
    scopes = sorted(scp(token["access_token"]))
    token.update({"id": cid, "name": name or str(cid), "scopes": scopes})
    return {"id": cid, "name": name or str(cid), "scopes": scopes, "token": token}
