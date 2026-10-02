"""技能计划构建：把目标（舰船 / 装备 / 技能 / 职业路线）+ 当前技能 → 可执行清单。

核心逻辑：
1. 目标展开成「需求技能 → 等级」（engine.skills.SkillIndex.closure，同技能取最高等级）；
2. 与当前技能等级比对得到缺口（按级拆分，便于显示 4 级 → 5 级需要多久）；
3. 按「前置优先」拓扑排序，逐条累加时间轴（EVE 同一时间只能训一项技能，故时间相加）；
4. 汇总总时长 / 总 SP / 缺口数量 / 每个目标的需求规模，供 UI 展示与导出。
"""

from engine import training


def _level_breakdown(skill, attrs, current, required):
    """技能当前等级 → 目标等级的逐级明细（只列还需要练的级）。"""
    out, prev = [], int(current or 0)
    for lv in training.LEVELS:
        if lv <= prev or lv > required:
            continue
        secs = training.level_seconds(skill, attrs, prev, lv)
        out.append({"level": lv, "seconds": round(secs, 1),
                    "sp": round(training.sp_between(skill["rank"], prev, lv), 1)})
        prev = lv
    return out


def _row(index, skill_tid, required, current, attrs):
    """单个技能的计划行。"""
    skill = index.skills.get(skill_tid)
    if not skill:
        return None
    current = int(current or 0)
    required = int(required or 1)
    p_key, s_key = training.skill_attrs(skill)
    levels = _level_breakdown(skill, attrs, current, required)
    row = {
        "tid": skill_tid,
        "name": skill["name"], "name_en": skill["name_en"],
        "group": skill["group"], "group_id": skill["group_id"],
        "rank": round(skill["rank"], 2),
        "primary": p_key, "secondary": s_key,
        "primary_name": training.ATTR_NAMES.get(p_key, p_key),
        "secondary_name": training.ATTR_NAMES.get(s_key, s_key),
        "required": required, "current": current, "level": max(current, required),
        "ok": current >= required,
        "levels": levels,
        "seconds": round(sum(l["seconds"] for l in levels), 1),
        "sp": round(sum(l["sp"] for l in levels), 1),
        "sp_total": round(training.sp_to_level(skill["rank"], required), 1),
        "prereqs": [{"tid": p, "name": index.name(p), "level": lv} for p, lv in
                    index.direct_reqs(skill_tid)],
    }
    row["duration"] = training.format_duration(row["seconds"])
    row["duration_short"] = training.encode_duration(row["seconds"])
    row["level_table"] = training.level_table(skill, attrs, current)
    return row


def _targets(index, targets):
    """规范化目标列表：type（舰船/装备）/ skill（指定等级）/ career（职业路线）。"""
    out = []
    for raw in targets or []:
        if not raw:
            continue
        kind = (raw.get("kind") or "type").lower()
        if kind == "career":
            pid = int(raw.get("plan_id") or raw.get("tid") or 0)
            plan = next((p for p in index.career_plans if p["plan_id"] == pid), None)
            if plan:
                out.append({"kind": "career", "tid": pid, "level": None,
                            "name": plan["name"], "name_en": plan["name_en"],
                            "category": "职业路线", "group": plan["internal_name"],
                            "pairs": [(s["tid"], s["level"]) for s in plan["skills"]]})
            continue
        tid = int(raw.get("tid") or raw.get("type_id") or 0)
        if not tid:
            continue
        info = index.info(tid)
        is_skill = bool(info.get("is_skill"))
        lv = int(raw["level"]) if raw.get("level") else None
        out.append({"kind": "skill" if is_skill else "type", "tid": tid,
                    "level": lv if is_skill else None,
                    "name": info["name"], "name_en": info.get("name_en", ""),
                    "category": info.get("category", ""), "group": info.get("group", ""),
                    # 技能目标：指定等级本身也要练（UI 总会带 level，缺省按满级 5）；
                    # 舰船/装备目标：只展开其需求技能
                    "pairs": [(tid, lv or 5)] if is_skill else []})
    return out


def build_plan(index, targets, current=None, attrs=None, options=None):
    """构建技能计划。

    targets : [{"kind": "type"|"skill"|"career", "tid"/"plan_id": int, "level": int?}, ...]
    current : {技能 tid: 当前等级}（ESI read_skills）
    attrs   : {"perception": 24, ...} 有效属性
    options : {"include_owned": bool, "order": "prereq"|"shortest"|"longest"|"group"}
    """
    options = options or {}
    attrs = training.normalize_attrs(attrs)
    current = {int(k): int(v or 0) for k, v in (current or {}).items()}
    tgt_list = _targets(index, targets)

    need, origin = {}, {}
    for tgt in tgt_list:
        tgt["need"] = index.closure(tgt["pairs"] or [tgt["tid"]])
        for sk, lv in tgt["need"].items():
            if lv > need.get(sk, 0):
                need[sk] = lv
            origin.setdefault(sk, []).append(tgt["tid"])

    rows, by_tid = [], {}
    for tid in index.topo_order(need):
        row = _row(index, tid, need[tid], current.get(tid, 0), attrs)
        if row is None:
            continue
        row["targets"] = sorted(set(origin.get(tid, [])))
        by_tid[tid] = row
        if row["ok"] and not options.get("include_owned"):
            continue
        rows.append(row)

    order = options.get("order") or "prereq"
    if order == "shortest":
        rows.sort(key=lambda r: (r["seconds"], index.name(r["tid"])))
    elif order == "longest":
        rows.sort(key=lambda r: (-r["seconds"], index.name(r["tid"])))
    elif order == "group":
        rows.sort(key=lambda r: (r["group"], r["name"]))

    elapsed, missing = 0.0, []
    for row in rows:
        row["start_seconds"] = round(elapsed, 1)
        if not row["ok"]:
            elapsed += row["seconds"]
            missing.append(row)
        row["end_seconds"] = round(elapsed, 1)
        row["start_human"] = training.format_duration(row["start_seconds"])
        row["end_human"] = training.format_duration(row["end_seconds"])
        row["end_short"] = training.encode_duration(row["end_seconds"])

    summary = {
        "skills_total": len(need),
        "skills_missing": len(missing),
        "skills_owned": len(need) - len(missing),
        "sp": round(sum(r["sp"] for r in missing), 1),
        "seconds": round(elapsed, 1),
        "duration": training.format_duration(elapsed),
        "attributes": attrs,
        "rate_note": "训练速率 = 主属性 + 副属性 / 2",
    }
    if options.get("start_at"):                        # 可选：给出完成时间戳
        try:
            start_at = float(options["start_at"])
            summary["start_at"] = start_at
            summary["finish_at"] = start_at + elapsed
        except (TypeError, ValueError):
            pass
    return {
        "targets": [{"kind": t["kind"], "tid": t["tid"], "level": t.get("level"),
                     "name": t["name"], "name_en": t.get("name_en", ""),
                     "category": t.get("category", ""), "group": t.get("group", ""),
                     "skills": len(t["need"]),
                     "seconds": round(sum(by_tid[s]["seconds"] for s in t["need"]
                                          if by_tid.get(s)), 1)} for t in tgt_list],
        "attributes": attrs,
        "rows": rows,
        "summary": summary,
    }


def plan_tsv(index, plan):
    """计划 → 制表符分隔文本（可直接粘进 Excel / 表格软件）。"""
    head = ["技能", "英文名", "技能组", "需求等级", "当前等级", "缺口", "训练时间",
            "SP", "主属性", "副属性", "累计完成"]
    lines = ["\t".join(head)]
    for row in plan["rows"]:
        gap = "" if row["ok"] else "+".join(f"{l['level']}级" for l in row["levels"])
        lines.append("\t".join(str(x) for x in (
            row["name"], row["name_en"], row["group"], row["required"], row["current"],
            gap or "-", row["duration"], int(row["sp"]), row["primary_name"],
            row["secondary_name"], row["end_human"])))
    lines.append("")
    s = plan["summary"]
    lines.append(f"汇总\t技能 {s['skills_total']} 项（缺 {s['skills_missing']}）\t"
                 f"总时长 {s['duration']}\t总 SP {int(s['sp'])}")
    return "\n".join(lines)


def skill_pairs(index, plan):
    """计划 → [(技能 tid, 目标等级)]，供导入游戏/其他工具或另存为计划。"""
    return [(r["tid"], r["level"]) for r in plan["rows"]]


def attributes_grid():
    """属性组合预设（新角色常见值 / 满配 / 平均），UI 快捷按钮用。"""
    return [
        {"key": "default", "name": "默认 17 / 17 / 17 / 17 / 17",
         "attrs": dict(training.DEFAULT_ATTRS)},
        {"key": "balanced", "name": "27 / 21 / 21 / 17 / 17（常见新角色）",
         "attrs": {"intelligence": 27, "memory": 21, "perception": 21,
                   "willpower": 17, "charisma": 17}},
        {"key": "max", "name": "全 27（含植入体满配）",
         "attrs": {k: 27 for k in training.DEFAULT_ATTRS}},
    ]

