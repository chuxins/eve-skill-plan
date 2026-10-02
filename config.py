"""应用配置：路径 / EVE 应用凭据 / 授权范围 / 公网基址。

与 /root/eve-skill-planner（模拟装配）共用同一套约定：
- 凭据优先级：环境变量（EVE_CLIENT_ID / EVE_CLIENT_SECRET / EVE_CALLBACK_URL）→ 项目根目录 config.json
- 角色 token 放在本站自己的目录 ~/.eve-skill-plan/tokens/<cid>.json。
  EVE 的 refresh_token 与签发它的 EVE 应用绑定（换应用刷新会得到
  `400 invalid_grant: Invalid refresh token`），所以**不能**和用另一个 EVE 应用的站点
  共用一个目录：两边各自轮换会把对方的 refresh_token 弄失效。若两个站想共用一份授权，
  就让它们用同一个 EVE 应用（门户里登记两个回调地址），并把 EVE_SKILL_PLAN_TOKEN_DIR
  指回同一个目录。
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATIC_DIR = os.path.join(BASE_DIR, "static")
APP_DB = os.path.join(DATA_DIR, "app.db")
# 技能索引（build_index.py 生成）
SKILLS_DB = os.environ.get("EVE_SKILL_PLAN_DB") or os.path.join(DATA_DIR, "skills.db")
# 组装完整 SDE 索引的来源库/压缩包（复用既有装配站的产物，避免重复解析 95MB SDE）
SDE_DB = os.environ.get("EVE_SDE_DB") or "/root/eve-skill-planner/data/staticdata.db"
SDE_ZIP = os.environ.get("EVE_SDE_ZIP") or "/root/eve_esi/sde.zip"

# 角色 token 目录：本站独立（见文件头说明，换应用后不能与旧应用的站点共用）
TOKEN_DIR = os.environ.get("EVE_SKILL_PLAN_TOKEN_DIR") or os.path.expanduser(
    "~/.eve-skill-plan/tokens")

_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 公网基址：OAuth 成功后回跳前端（本站挂在 nginx 的 /skills/ 子路径下）
PUBLIC_BASE = os.environ.get("EVE_SKILL_PLAN_URL", "http://8.156.88.102/skills").rstrip("/")

PORT = int(os.environ.get("EVE_SKILL_PLAN_PORT") or 8091)

SSO_AUTHORIZE = "https://login.eveonline.com/v2/oauth/authorize"
TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
SSO_REVOKE = "https://login.eveonline.com/v2/oauth/revoke"
ESI_BASE = "https://esi.evetech.net"

# 授权范围：本站只需要「读技能 / 读训练队列」（attributes 接口也要求 skills 读权限）
SCOPES = [
    "esi-skills.read_skills.v1",
    "esi-skills.read_skillqueue.v1",
]

# 技能索引相关 SDE attributeID（2026-09 SDE 实测）
A_REQ_SKILL = (182, 183, 184, 1285, 1289)      # requiredSkill1..5
A_REQ_LEVEL = (277, 278, 279, 1286, 1287)      # requiredSkill1..5Level
A_PRIMARY, A_SECONDARY, A_RANK = 180, 181, 275  # 主属性 / 副属性 / 训练时间倍增系数
CAT_SKILL = 16
# 「需求技能」查询覆盖的类型类别：舰船 / 装备 / 弹药 / 无人机 / 建筑 / 技能自身
CAT_REQ_SOURCES = (6, 7, 8, 18, 65, 16)


def _file_config():
    """读取项目根目录 config.json（缺失/损坏时返回空 dict）。"""
    if not os.path.exists(_CONFIG_PATH):
        return {}
    try:
        with open(_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def eve_credentials():
    """返回 (client_id, client_secret, callback_url)。

    callback_url 必须与 EVE 应用后台登记的回调逐字符一致，且已由 nginx 反代到本站
    （见 README「OAuth 接线」）。三个字段各自独立回退，避免只设部分环境变量时丢字段。
    """
    cfg = _file_config()
    client_id = os.environ.get("EVE_CLIENT_ID") or cfg.get("client_id")
    client_secret = os.environ.get("EVE_CLIENT_SECRET") or cfg.get("client_secret")
    callback_url = os.environ.get("EVE_CALLBACK_URL") or cfg.get("callback_url")
    if not client_id or not client_secret:
        raise RuntimeError("EVE 应用凭据未配置（EVE_CLIENT_ID/EVE_CLIENT_SECRET 或 config.json）")
    if not callback_url:
        raise RuntimeError("EVE 回调地址未配置（EVE_CALLBACK_URL 或 config.json 的 callback_url）")
    return client_id, client_secret, callback_url
