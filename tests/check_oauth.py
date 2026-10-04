#!/usr/bin/env python3
"""OAuth 预检：在不点「登录」的前提下判断凭据 / 回调地址是否接得通。

用法：python3 tests/check_oauth.py
（可用 EVE_SKILL_PLAN_BASE 指定站点入口，默认 http://127.0.0.1/eveskillplanner）

做三件事：
1. 打印 config.json（或环境变量）实际生效的 client_id 与 callback_url（secret 打码）
2. 用**假的** refresh_token 打 EVE token 端点：
   - 400 invalid_grant      → 凭据正确（EVE 先做 client 认证，再判 token 真假）
   - 401 invalid_client     → client_id / secret 不匹配，门户里核对
   （不会动任何真实 token 文件）
3. GET 一次真实的 authorize 链接（由 /api/oauth/url 生成）：
   - 返回 EVE 的 Log In 页（含 Sign In / title=Log In）→ 回调地址已被门户接受
   - 出现 invalid redirect_uri / invalid_client → 门户登记的地址与 callback_url 不一致
   注意：最终以浏览器里走完「登录 → 同意」为准（同意阶段才会真正回跳）。

退出码：0 = 全部预检通过；1 = 有需要修的地方；2 = 站点不可达。
"""

import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

BASE = (os.environ.get("EVE_SKILL_PLAN_BASE") or "http://127.0.0.1/eveskillplanner").rstrip("/")
TOKEN_URL = config.TOKEN_URL


def main():
    bad = []
    client_id, secret, callback = config.eve_credentials()
    print("生效凭据：client_id =", client_id)
    print("          secret    =", secret[:8] + "…" + secret[-4:])
    print("          callback  =", callback)

    print("\n[1] 凭据体检（假 refresh_token 打 token 端点）")
    try:
        r = requests.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": "not-a-real-token",
            "client_id": client_id, "client_secret": secret}, timeout=30)
        body = r.text[:160].replace("\n", " ")
        print("    HTTP", r.status_code, body)
        if r.status_code == 400 and "invalid_grant" in r.text:
            print("    ✅ 凭据有效（EVE 已通过 client 认证，只是 token 是假的）")
        elif "invalid_client" in r.text:
            print("    ❌ client_id / secret 被拒：请到门户核对")
            bad.append("client 凭据")
        else:
            print("    ⚠️ 非典型响应，EVE 侧可能故障，稍后重试")
    except requests.RequestException as exc:
        print("    ⚠️ 请求失败：", exc)

    print("\n[2] 授权链接体检（GET，不跟随跳转）")
    try:
        d = requests.get(BASE + "/api/oauth/url", timeout=20).json()
    except Exception as exc:
        print("    ❌ 站点不可达：", BASE, exc)
        return 2
    url = d["url"]
    print("    authorize =", url[:120] + "…")
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        html = resp.text
        low = html.lower()
        if "invalid redirect_uri" in low or "invalid_redirect" in low:
            print("    ❌ 门户登记的回调地址与 callback_url 不一致")
            bad.append("回调地址登记")
        elif "invalid_client" in low:
            print("    ❌ 门户不认识这个 client_id")
            bad.append("client_id")
        elif "<title>log in</title>" in low or "sign in" in low:
            print("    ✅ 已到 EVE 的 Log In 页（回调地址看起来已被接受）")
        else:
            print("    ⚠️ 未识别的响应，请人工看一眼：", resp.status_code,
                  resp.headers.get("Content-Type"))
            bad.append("授权页响应")
    except requests.RequestException as exc:
        print("    ⚠️ 请求 EVE 失败：", exc)

    print("\n结论：", "✅ 预检通过，可以点站内「登录」了" if not bad
          else "❌ 需要处理：" + "、".join(bad))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
