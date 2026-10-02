"""技能索引：把 data/skills.db（build_index.py 生成）载入内存并提供查询。

索引很小（技能 ~511 / 类型 ~6k / 需求行 ~9.5k），启动时一次性载入内存，
之后所有查询都在内存里做：不反复开 SQLite，也不依赖请求线程。

对外入口：get_index() 返回进程级单例。
"""

import json
import os
import sqlite3
import threading
import time

import config

_lock = threading.Lock()
_index = None

# 类型类别 → 中文名（「需求技能」选择器分组用；只列 CAT_REQ_SOURCES 覆盖到的）
CAT_NAMES = {
    6: "舰船", 7: "装备", 8: "弹药与装料", 9: "舰船改装件",
    16: "技能", 18: "无人机", 65: "建筑", 87: "战斗机",
}


class SkillIndex:
    """只读技能索引（内存结构）。"""

    def __init__(self, db_path=None):
        self.db_path = db_path or config.SKILLS_DB
        self.meta = {}
        self.skills = {}          # 技能 tid -> 技能 dict
        self.types = {}           # 类型 tid -> 类型 dict（含技能）
        self.groups = {}          # 技能组 gid -> {id, name, skills:[tid, ...]}
        self.reqs = {}            # 类型 tid -> [(前置技能 tid, 等级), ...]
        self.dependents = {}      # 技能 tid -> [(需要它的类型 tid, 所需等级), ...]
        self.career_plans = []
        self.loaded_at = 0.0
        self.reload()

    # ------------------------------------------------------------ 载入
    def reload(self):
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(
                f"技能索引不存在：{self.db_path}（请先运行 python3 build_index.py）")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            self.meta = {k: v for k, v in conn.execute("SELECT key,value FROM meta")}
            self.types = {}
            for tid, name, en, gid, gname, cat, meta in conn.execute(
                    "SELECT type_id,name,name_en,group_id,group_name,category_id,meta_group_id "
                    "FROM types"):
                self.types[tid] = {"tid": tid, "name": name, "name_en": en,
                                   "group_id": gid, "group": gname,
                                   "category_id": cat or 0,
                                   "category": CAT_NAMES.get(cat or 0, ""),
                                   "meta_group_id": meta, "is_skill": (cat or 0) == config.CAT_SKILL}
            self.skills = {}
            for tid, name, en, gid, gname, rank, pa, sa, desc in conn.execute(
                    "SELECT type_id,name,name_en,group_id,group_name,rank,primary_attr,"
                    "secondary_attr,description FROM skills"):
                self.skills[tid] = {"tid": tid, "name": name, "name_en": en,
                                    "group_id": gid, "group": gname,
                                    "rank": float(rank or 1.0),
                                    "primary_attr": pa, "secondary_attr": sa,
                                    "description": desc,
                                    "category_id": config.CAT_SKILL,
                                    "category": CAT_NAMES[config.CAT_SKILL],
                                    "meta_group_id": None, "is_skill": True}
            self.groups = {}
            for sk in self.skills.values():
                grp = self.groups.setdefault(sk["group_id"],
                                             {"id": sk["group_id"], "name": sk["group"],
                                              "skills": []})
                grp["skills"].append(sk["tid"])
            for grp in self.groups.values():
                grp["skills"].sort(key=lambda t: (self.skills[t]["name_en"],
                                                  self.skills[t]["name"]))
            self.reqs = {}
            self.dependents = {}
            for tid, sk, lv in conn.execute(
                    "SELECT type_id,skill_id,level FROM type_reqs"):
                self.reqs.setdefault(tid, []).append((sk, lv))
                self.dependents.setdefault(sk, []).append((tid, lv))
            for rows in self.reqs.values():
                rows.sort(key=lambda r: self.skills.get(r[0], {}).get("name", ""))
            self.career_plans = []
            for pid, name, en, fid, cpid, internal, desc, reqs, miles in conn.execute(
                    "SELECT plan_id,name,name_en,faction_id,career_path_id,internal_name,"
                    "description,reqs_json,milestones_json FROM career_plans ORDER BY "
                    "CASE career_path_id WHEN 1 THEN 0 WHEN 2 THEN 1 WHEN 3 THEN 2 WHEN 4 THEN 3 "
                    "WHEN 5 THEN 4 ELSE 5 END, name"):
                self.career_plans.append({
                    "plan_id": pid, "name": name, "name_en": en, "faction_id": fid,
                    "career_path_id": cpid, "internal_name": internal, "description": desc,
                    "skills": [{"tid": t, "level": l} for t, l in json.loads(reqs)],
                    "milestones": json.loads(miles),
                })
        finally:
            conn.close()
        self.loaded_at = time.time()
        return self

    # ------------------------------------------------------------ 基本查询
    def name(self, tid):
        t = self.types.get(int(tid or 0))
        return t["name"] if t else f"#{tid}"

    def info(self, tid):
        """技能优先，其次普通类型；都查不到给占位 dict。"""
        tid = int(tid or 0)
        return self.skills.get(tid) or self.types.get(tid) or {
            "tid": tid, "name": f"#{tid}", "name_en": "", "group": "",
            "group_id": 0, "category_id": 0, "category": "", "is_skill": False}

    def groups_tree(self):
        """技能组列表（含组内技能），技能树 UI 用。"""
        out = []
        for grp in sorted(self.groups.values(), key=lambda g: g["id"]):
            skills = []
            for tid in grp["skills"]:
                sk = self.skills[tid]
                skills.append({"tid": tid, "name": sk["name"], "name_en": sk["name_en"],
                               "rank": sk["rank"]})
            out.append({"id": grp["id"], "name": grp["name"], "skills": skills})
        return out

    def search_skills(self, query, limit=50, group_id=None):
        """按名称搜索技能（中文/英文，不区分大小写）。"""
        q = (query or "").strip().lower()
        hits = []
        for sk in self.skills.values():
            if group_id and sk["group_id"] != int(group_id):
                continue
            if q and q not in sk["name"].lower() and q not in sk["name_en"].lower():
                continue
            hits.append(self._skill_brief(sk["tid"]))
        hits.sort(key=lambda s: (q not in s["name"].lower(), s["rank"], s["name"]))
        return hits[:limit]

    def search_types(self, query, categories=None, limit=40):
        """按名称搜索类型（舰船/装备/…），供「需求技能」查询选择。"""
        cats = set(categories or config.CAT_REQ_SOURCES)
        q = (query or "").strip().lower()
        if not q:
            return []
        hits = []
        for t in self.types.values():
            if t["category_id"] not in cats:
                continue
            if q not in t["name"].lower() and q not in t["name_en"].lower():
                continue
            hits.append({"tid": t["tid"], "name": t["name"], "name_en": t["name_en"],
                         "group": t["group"], "category_id": t["category_id"],
                         "category": t["category"], "is_skill": t["is_skill"]})
        hits.sort(key=lambda t: (t["category_id"] != config.CAT_SKILL,
                                 t["category_id"], t["name"]))
        return hits[:limit]

    def _skill_brief(self, tid):
        sk = self.skills.get(tid)
        if not sk:
            return {"tid": tid, "name": f"#{tid}", "name_en": "", "group": "", "rank": 1,
                    "group_id": 0}
        return {"tid": tid, "name": sk["name"], "name_en": sk["name_en"],
                "group": sk["group"], "group_id": sk["group_id"], "rank": sk["rank"]}

    def type_brief(self, tid):
        t = self.info(tid)
        return {"tid": t["tid"], "name": t["name"], "name_en": t.get("name_en", ""),
                "group": t.get("group", ""), "category_id": t.get("category_id", 0),
                "category": t.get("category", ""), "is_skill": bool(t.get("is_skill")),
                "rank": t.get("rank"), "meta_group_id": t.get("meta_group_id")}

    # ------------------------------------------------------------ 需求关系
    def direct_reqs(self, tid):
        """直接需求技能 [(skill_tid, level), ...]。"""
        return list(self.reqs.get(int(tid or 0), []))

    def closure(self, targets, base=None):
        """递归展开需求技能：{技能 tid: 所需等级}（同一技能取最高等级）。

        targets 可以是类型（舰船/装备/技能）或 (tid, level) 元组（用于按等级取值）。
        技能自身的 requiredSkill 也递归展开，得到可自上而下依次训练的完整前置集。
        """
        need = dict(base or {})
        stack = []
        for item in targets or []:
            if isinstance(item, (list, tuple)):
                tid, lv = int(item[0]), int(item[1] or 1)
                if lv > need.get(tid, 0):
                    need[tid] = lv
                stack.append(tid)          # 技能目标也要递归展开它自身的前置技能
            else:
                stack.append(int(item))
        seen = set()
        while stack:
            tid = stack.pop()
            if tid in seen:
                continue
            seen.add(tid)
            for sk, lv in self.direct_reqs(tid):
                if lv > need.get(sk, 0):
                    need[sk] = lv
                stack.append(sk)
        return need

    def consumers(self, skill_tid):
        """谁需要这个技能：按类别分组的类型列表。"""
        out = {}
        for tid, lv in self.dependents.get(int(skill_tid or 0), []):
            t = self.info(tid)
            key = t.get("category") or "其他"
            out.setdefault(key, []).append({"tid": tid, "name": t["name"],
                                            "group": t.get("group", ""), "level": lv})
        for items in out.values():
            items.sort(key=lambda i: (i["level"], i["name"]))
        return [{"category": k, "items": v} for k, v in sorted(out.items())]

    def topo_order(self, skills):
        """前置优先排序（Kahn）：前置技能先出现，保证计划可依次训练。"""
        skills = set(int(s) for s in skills)
        deps = {}
        for sk in skills:
            deps[sk] = [p for p, _ in self.direct_reqs(sk) if p in skills and p != sk]
        order, done, remaining = [], set(), set(skills)
        while remaining:
            ready = sorted(sk for sk in remaining if all(d in done for d in deps[sk]))
            if not ready:                     # 数据成环（异常）→ 兜底按技能名排序，避免死循环
                ready = sorted(remaining, key=lambda s: self.name(s))
            for sk in ready:
                order.append(sk)
                done.add(sk)
                remaining.discard(sk)
        return order


def get_index(force=False):
    """进程级索引单例（首次访问时载入）。"""
    global _index
    with _lock:
        if _index is None or force:
            _index = SkillIndex()
        return _index
