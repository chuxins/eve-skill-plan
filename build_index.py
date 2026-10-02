#!/usr/bin/env python3
"""构建技能索引 data/skills.db（本站只需要技能相关内容，故不复制整个 SDE）。

数据来源（两者都可用时取长补短，全离线）：
1. `--sde-db`  既有 SDE 索引 SQLite（默认复用 /root/eve-skill-planner/data/staticdata.db）：
   读取 types / groups / type_attrs，快（秒级）。
2. `--sde-zip` 官方 SDE JSONL 压缩包（默认 /root/eve_esi/sde.zip），用于补：
   - 技能/舰船/装备的中文描述（types.jsonl）
   - 官方职业路线技能计划（skillPlans.jsonl）
   仅当来源库缺失时才用它解析 types/groups/typeDogma 建基础表（慢，约 1 分钟）。

产出表：
  skills       技能元数据（名称/组/训练时间倍增系数/主副属性/描述）
  type_reqs    任意类型（舰船/装备/弹药/无人机/技能…）→ 直接需求技能 + 等级
  types        需求技能查询会用到的类型（上述类型 + 全部技能）
  career_plans 官方职业路线（技能清单 + 里程碑）
  meta         构建信息（来源、时间、计数）

用法:
    python3 build_index.py                 # 复用既有 SDE 索引 + zip 补描述/路线
    python3 build_index.py --no-zip        # 只从 SDE 索引构建（无描述/职业路线）
    python3 build_index.py --stats         # 只看统计
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import zipfile

import config

BATCH = 5000
_TAG_RE = re.compile(r"<[^>]+>")
_CJK_SPACE_RE = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")

# 有需求技能语义的属性（182/183/184/1285/1289 与 277/278/279/1286/1287 成对）
A_ALL = tuple(config.A_REQ_SKILL) + tuple(config.A_REQ_LEVEL) + (
    config.A_PRIMARY, config.A_SECONDARY, config.A_RANK)

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS skills(
    type_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    name_en TEXT NOT NULL DEFAULT '',
    group_id INTEGER NOT NULL DEFAULT 0,
    group_name TEXT NOT NULL DEFAULT '',
    rank REAL NOT NULL DEFAULT 1,
    primary_attr INTEGER,
    secondary_attr INTEGER,
    description TEXT NOT NULL DEFAULT '',
    published INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS type_reqs(
    type_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(type_id, skill_id));
CREATE TABLE IF NOT EXISTS types(
    type_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    name_en TEXT NOT NULL DEFAULT '',
    group_id INTEGER NOT NULL DEFAULT 0,
    group_name TEXT NOT NULL DEFAULT '',
    category_id INTEGER NOT NULL DEFAULT 0,
    meta_group_id INTEGER,
    description TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS career_plans(
    plan_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    name_en TEXT NOT NULL DEFAULT '',
    faction_id INTEGER,
    career_path_id INTEGER,
    internal_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    reqs_json TEXT NOT NULL DEFAULT '[]',
    milestones_json TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_reqs_skill ON type_reqs(skill_id);
"""


def clean_zh(text):
    """去掉描述里的 HTML 标签、中文之间多余空格，压缩空白。"""
    text = _TAG_RE.sub("", str(text or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CJK_SPACE_RE.sub("", text.replace("\u3000", " "))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def zh_of(names):
    names = names or {}
    if isinstance(names, str):
        return clean_zh(names)
    return clean_zh(names.get("zh") or names.get("en") or "")


def en_of(names):
    return str(names.get("en") or "") if isinstance(names, dict) else ""


def iter_jsonl(zf, name):
    with zf.open(name) as f:
        for raw in f:
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def insert_many(conn, sql, rows, ncols):
    """按批写入；列数不符的行跳过（防脏数据）。"""
    total, batch = 0, []
    for row in rows:
        if len(row) != ncols:
            continue
        batch.append(row)
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        total += len(batch)
    conn.commit()
    return total


def load_base_from_db(src_path):
    """从既有 SDE 索引读出 groups / types / type_attrs（快路径）。"""
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    try:
        groups = {gid: (name, cat) for gid, cat, name in
                  src.execute("SELECT group_id,category_id,name FROM groups")}
        types = [(tid, gid, cat or 0, name, en, meta)
                 for tid, gid, cat, name, en, pub, meta in src.execute(
                     "SELECT type_id,group_id,category_id,name,name_en,published,meta_group_id "
                     "FROM types") if pub]
        attrs = {}
        marks = ",".join("?" * len(A_ALL))
        for tid, aid, val in src.execute(
                f"SELECT type_id,attribute_id,value FROM type_attrs "
                f"WHERE attribute_id IN ({marks})", A_ALL):
            attrs.setdefault(tid, {})[aid] = val
    finally:
        src.close()
    return groups, types, attrs


def load_base_from_zip(zip_path):
    """从官方 SDE zip 解析基础表（慢路径：来源库缺失时用）。"""
    groups, types, attrs = {}, [], {}
    with zipfile.ZipFile(zip_path) as zf:
        for g in iter_jsonl(zf, "groups.jsonl"):
            groups[int(g["_key"])] = (zh_of(g.get("name")), int(g.get("categoryID") or 0))
        for t in iter_jsonl(zf, "types.jsonl"):
            if not t.get("published"):
                continue
            tid, gid = int(t["_key"]), int(t.get("groupID") or 0)
            types.append((tid, gid, groups.get(gid, ("", 0))[1], zh_of(t.get("name")),
                          en_of(t.get("name")), t.get("metaGroupID")))
        for td in iter_jsonl(zf, "typeDogma.jsonl"):
            tid = int(td["_key"])
            for a in td.get("dogmaAttributes") or []:
                aid = int(a["attributeID"])
                if aid in A_ALL:
                    attrs.setdefault(tid, {})[aid] = a.get("value")
    return groups, types, attrs


def build_sources(conn, groups, types, attrs):
    """写入 skills / types / type_reqs。"""
    out_types, skills, reqs = [], [], []
    for tid, gid, cat, name, en, meta in types:
        gname = groups.get(gid, ("", 0))[0]
        if cat == config.CAT_SKILL:
            a = attrs.get(tid, {})
            skills.append((tid, name, en, gid, gname,
                           float(a.get(config.A_RANK) or 1.0),
                           int(a.get(config.A_PRIMARY) or 0) or None,
                           int(a.get(config.A_SECONDARY) or 0) or None))
        if cat == config.CAT_SKILL or cat in config.CAT_REQ_SOURCES:
            out_types.append((tid, name, en, gid, gname, cat, meta))
    skill_ids = {s[0] for s in skills}
    for tid, gid, cat, name, en, meta in types:
        a = attrs.get(tid, {})
        for sa, la in zip(config.A_REQ_SKILL, config.A_REQ_LEVEL):
            sk = a.get(sa)
            if not sk:
                continue
            sk = int(sk)
            if sk not in skill_ids:          # 需求指向非技能（脏数据）→ 跳过
                continue
            lv = int(a.get(la) or 1)
            if 1 <= lv <= 5:
                reqs.append((tid, sk, lv))
    n_t = insert_many(conn, "INSERT OR REPLACE INTO types"
                            "(type_id,name,name_en,group_id,group_name,category_id,meta_group_id)"
                            " VALUES(?,?,?,?,?,?,?)", out_types, 7)
    n_s = insert_many(conn, "INSERT OR REPLACE INTO skills"
                            "(type_id,name,name_en,group_id,group_name,rank,primary_attr,secondary_attr)"
                            " VALUES(?,?,?,?,?,?,?,?)", skills, 8)
    n_r = insert_many(conn, "INSERT OR REPLACE INTO type_reqs(type_id,skill_id,level)"
                            " VALUES(?,?,?)", reqs, 3)
    print(f"  types {n_t} / skills {n_s} / type_reqs {n_r}")
    return n_t, n_s, n_r


def fill_descriptions(conn, zip_path):
    """用 zip 的 types.jsonl 补中文描述（只取已收录的类型），再回填到 skills。"""
    want = {r[0] for r in conn.execute("SELECT type_id FROM types")}
    rows = []
    with zipfile.ZipFile(zip_path) as zf:
        for t in iter_jsonl(zf, "types.jsonl"):
            tid = int(t["_key"])
            if tid in want:
                desc = zh_of(t.get("description"))
                if desc:
                    rows.append((desc, tid))
    n = insert_many(conn, "UPDATE types SET description=? WHERE type_id=?", rows, 2) if rows else 0
    conn.execute("UPDATE skills SET description=COALESCE((SELECT t.description FROM types t "
                 "WHERE t.type_id=skills.type_id),'')")
    conn.commit()
    print(f"  描述 {n}")
    return n


def fill_career_plans(conn, zip_path):
    """官方职业路线（Agency 里的「职业」技能计划）：技能清单 + 里程碑。"""
    plans = []
    with zipfile.ZipFile(zip_path) as zf:
        for p in iter_jsonl(zf, "skillPlans.jsonl"):
            reqs = {}
            for r in p.get("skillRequirements") or []:
                tid, lv = int(r.get("typeID") or 0), int(r.get("level") or 0)
                if tid and 1 <= lv <= 5 and lv > reqs.get(tid, 0):
                    reqs[tid] = lv
            if not reqs:
                continue
            plans.append((
                int(p["_key"]), zh_of(p.get("name")), en_of(p.get("name")),
                p.get("factionID"), p.get("careerPathID"),
                str(p.get("internalName") or ""), zh_of(p.get("description")),
                json.dumps([[k, v] for k, v in sorted(reqs.items())]),
                json.dumps([{"level": int(m.get("level") or 0), "type_id": int(m.get("typeID") or 0)}
                            for m in p.get("milestones") or []]),
            ))
    n = insert_many(conn, "INSERT OR REPLACE INTO career_plans"
                          "(plan_id,name,name_en,faction_id,career_path_id,internal_name,"
                          "description,reqs_json,milestones_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    plans, 9)
    print(f"  职业路线 {n}")
    return n


def build_meta(conn, src, counts):
    rows = [(k, str(v)) for k, v in counts.items()] + [
        ("source", src), ("built_at", time.strftime("%Y-%m-%d %H:%M:%S"))]
    insert_many(conn, "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", rows, 2)


def stats(conn):
    one = lambda sql: conn.execute(sql).fetchone()[0]
    n_skills = one("SELECT COUNT(*) FROM skills")
    n_groups = one("SELECT COUNT(DISTINCT group_id) FROM skills")
    n_reqs = one("SELECT COUNT(*) FROM type_reqs")
    n_src = one("SELECT COUNT(DISTINCT type_id) FROM type_reqs")
    n_types = one("SELECT COUNT(*) FROM types")
    n_career = one("SELECT COUNT(*) FROM career_plans")
    n_desc = one("SELECT COUNT(*) FROM skills WHERE description <> ''")
    print(f"技能 {n_skills} | 技能组 {n_groups} | 需求行 {n_reqs} | 需求来源类型 {n_src}")
    print(f"类型 {n_types} | 职业路线 {n_career} | 带描述技能 {n_desc}")


def main():
    ap = argparse.ArgumentParser(description="构建技能索引 data/skills.db")
    ap.add_argument("--sde-db", default=config.SDE_DB,
                    help="既有 SDE 索引 SQLite（默认 %(default)s）")
    ap.add_argument("--sde-zip", default=config.SDE_ZIP, help="官方 SDE zip（默认 %(default)s）")
    ap.add_argument("--no-zip", action="store_true", help="完全不读 zip（无描述/职业路线）")
    ap.add_argument("--out", default=config.SKILLS_DB, help="输出库（默认 %(default)s）")
    ap.add_argument("--stats", action="store_true", help="只显示统计")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    conn = sqlite3.connect(args.out)
    conn.executescript(SCHEMA)

    if args.stats:
        stats(conn)
        return

    started = time.time()
    have_db = os.path.exists(args.sde_db)
    have_zip = (not args.no_zip) and os.path.exists(args.sde_zip)
    if have_db:
        print(f"来源库：{args.sde_db}")
        groups, types, attrs = load_base_from_db(args.sde_db)
    elif have_zip:
        print(f"来源库缺失，改从 zip 解析基础表：{args.sde_zip}（较慢）")
        groups, types, attrs = load_base_from_zip(args.sde_zip)
    else:
        print(f"[错误] 既没有 SDE 索引（{args.sde_db}）也没有 SDE zip（{args.sde_zip}）。\n"
              f"  可先运行 /root/eve-skill-planner/build_index.py 生成索引，"
              f"或用 --sde-zip 指定 zip。", file=sys.stderr)
        sys.exit(1)

    print(f"读到类型 {len(types)} 个（含需求属性 {len(attrs)}）")
    n_t, n_s, n_r = build_sources(conn, groups, types, attrs)
    counts = {"types": n_t, "skills": n_s, "type_reqs": n_r}
    if have_zip:
        counts["descriptions"] = fill_descriptions(conn, args.sde_zip)
        counts["career_plans"] = fill_career_plans(conn, args.sde_zip)
    build_meta(conn, args.sde_db if have_db else args.sde_zip, counts)
    print(f"✅ 索引完成 -> {args.out}（{os.path.getsize(args.out) / 1048576:.1f} MB，"
          f"耗时 {time.time() - started:.1f}s）")
    stats(conn)
    conn.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)

