"""技能训练时间计算（与游戏/pyfa 口径一致，全离线）。

- 累计技能点：SP(level) = 250 × rank × 2^(2.5·level − 2.5)
  即 L1 250×rank、L2 1414、L3 8000、L4 45255、L5 256000（×rank），与客户端一致。
- 训练速率：SP/分钟 = 主属性 + 副属性 / 2（属性取 ESI 有效值，已含植入体）。
- rank 取技能 attr 275 skillTimeConstant，主/副属性取 attr 180/181。

属性 ID → ESI attributes 键（SDE primaryAttribute/secondaryAttribute 的取值）：
    164 魅力 charisma / 165 智力 intelligence / 166 记忆 memory
    167 感知 perception / 168 毅力 willpower
"""

import math

ATTR_KEYS = {
    164: "charisma",
    165: "intelligence",
    166: "memory",
    167: "perception",
    168: "willpower",
}
ATTR_NAMES = {
    "charisma": "魅力", "intelligence": "智力", "memory": "记忆",
    "perception": "感知", "willpower": "毅力",
}
DEFAULT_ATTRS = {"charisma": 17, "intelligence": 17, "memory": 17,
                 "perception": 17, "willpower": 17}

LEVELS = (1, 2, 3, 4, 5)
# 每级累计技能点系数（×rank）：250 × 2^(2.5·(level−1)) —— 与 sp_to_level 同源，
# 这里预先算好便于前端展示「升到该级需要多少 SP」。
LEVEL_SP_FACTOR = {lv: 250.0 * math.pow(2.0, 2.5 * (lv - 1)) for lv in LEVELS}


def sp_to_level(rank, level):
    """从 0 级练到 level 级所需累计技能点。"""
    level = int(level or 0)
    if level <= 0:
        return 0.0
    return 250.0 * float(rank or 1.0) * math.pow(2.0, 2.5 * level - 2.5)


def sp_between(rank, from_level, to_level):
    """from_level → to_level 的技能点增量（不跨级也可用）。"""
    return max(0.0, sp_to_level(rank, to_level) - sp_to_level(rank, from_level))


def normalize_attrs(attrs):
    """补齐缺项/非法值的属性表（基础 17）。"""
    eff = dict(DEFAULT_ATTRS)
    for key, val in (attrs or {}).items():
        try:
            val = float(val)
        except (TypeError, ValueError):
            continue
        if key in eff and val > 0:
            eff[key] = val
    return eff


def skill_attrs(skill):
    """技能 dict → (主属性键, 副属性键)；SDE 缺项时回退 感知/毅力（游戏默认）。"""
    p = ATTR_KEYS.get(int((skill or {}).get("primary_attr") or 167), "perception")
    s = ATTR_KEYS.get(int((skill or {}).get("secondary_attr") or 168), "willpower")
    return p, s


def level_seconds(skill, attrs, from_level, to_level):
    """某技能 from_level → to_level 的训练秒数（attrs 为有效属性值）。"""
    from_level, to_level = int(from_level or 0), int(to_level or 0)
    if to_level <= from_level:
        return 0.0
    eff = normalize_attrs(attrs)
    p_key, s_key = skill_attrs(skill)
    rate = eff[p_key] + eff[s_key] / 2.0            # SP / 分钟
    if rate <= 0:
        return 0.0
    return sp_between((skill or {}).get("rank") or 1.0, from_level, to_level) / rate * 60.0


def level_table(skill, attrs, from_level=0):
    """{1: 秒, 2: 秒, ...}：从 from_level 起，升到每一级所需秒数（已到级 → 0）。"""
    return {lv: round(level_seconds(skill, attrs, from_level, lv), 1) for lv in LEVELS}


def format_duration(seconds):
    """秒 → 中文时长（「3天 4小时 12分」「45秒」），None/0 处理见调用方。"""
    if seconds is None:
        return "未知"
    seconds = int(round(seconds))
    if seconds <= 0:
        return "0秒"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}天 {hours}小时 {minutes}分"
    if hours:
        return f"{hours}小时 {minutes}分"
    if minutes:
        return f"{minutes}分 {secs}秒"
    return f"{secs}秒"


def encode_duration(seconds):
    """秒 → 紧凑编码（如 1d4h12m / 5h30m / 45s），表格窄列展示用。"""
    if not seconds:
        return "-"
    parts, rem = [], int(round(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        val, rem = divmod(rem, size)
        parts.append(f"{val}{unit}")
    if parts[0].startswith("0"):
        parts = parts[1:]
    return "".join(parts) or "0s"
