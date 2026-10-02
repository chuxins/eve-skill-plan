"""应用库（data/app.db）：保存的技能计划。

技能索引是只读的（build_index.py 生成），用户数据只放这里：
计划名 / 目标列表 / 快照的当前技能与属性，便于离线复算与历史对比。

SSO 隔离：每条计划带 owner_cid（创建它的 SSO 登录角色），
所有读取/更新/删除都按 owner_cid 过滤 —— 未登录看不到任何计划，
不同角色之间互不可见（2026-10 新增，存量无主计划自动转为不可见）。
"""

import json
import os
import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans(
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    character_id INTEGER,
    owner_cid INTEGER,
    targets_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '{}',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '');
"""


def _migrate(conn):
    """存量库补列：旧版 plans 表没有 owner_cid，隔离后旧记录视为无主、不再可见。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
    if "owner_cid" not in cols:
        conn.execute("ALTER TABLE plans ADD COLUMN owner_cid INTEGER")
        conn.commit()


def connect():
    """打开应用库（必要时建表 / 迁移）。"""
    os.makedirs(os.path.dirname(config.APP_DB), exist_ok=True)
    conn = sqlite3.connect(config.APP_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _loads(text, fallback):
    try:
        val = json.loads(text or "")
    except ValueError:
        return fallback
    return val if val is not None else fallback


def _row_to_plan(row):
    return {
        "plan_id": row["plan_id"],
        "name": row["name"],
        "character_id": row["character_id"],
        "targets": _loads(row["targets_json"], []),
        "skills": _loads(row["skills_json"], {}),
        "attrs": _loads(row["attrs_json"], {}),
        "options": _loads(row["options_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_plans(owner_cid=None):
    """某登录角色可见的计划列表（未登录/无主 → 空，不泄露他人数据）。"""
    if owner_cid is None:
        return []
    conn = connect()
    try:
        return [_row_to_plan(r) for r in conn.execute(
            "SELECT * FROM plans WHERE owner_cid=? ORDER BY updated_at DESC, plan_id DESC",
            (int(owner_cid),))]
    finally:
        conn.close()


def get_plan(plan_id, owner_cid=None):
    """按 owner 取计划；不是本人创建（或未登录）一律返回 None。"""
    if owner_cid is None:
        return None
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM plans WHERE plan_id=? AND owner_cid=?",
                           (int(plan_id), int(owner_cid))).fetchone()
        return _row_to_plan(row) if row else None
    finally:
        conn.close()


def save_plan(name, targets, skills=None, attrs=None, options=None,
              character_id=None, owner_cid=None, plan_id=None):
    """新建或更新计划（owner_cid 是 SSO 登录角色，保存的隔离边界），返回计划 dict。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    owner = int(owner_cid) if owner_cid else None
    payload = (name or "未命名计划", json.dumps(targets or [], ensure_ascii=False),
               json.dumps(skills or {}, ensure_ascii=False),
               json.dumps(attrs or {}, ensure_ascii=False),
               json.dumps(options or {}, ensure_ascii=False),
               int(character_id) if character_id else None)
    conn = connect()
    try:
        if plan_id:
            conn.execute("UPDATE plans SET name=?,targets_json=?,skills_json=?,attrs_json=?,"
                         "options_json=?,character_id=?,owner_cid=?,updated_at=? "
                         "WHERE plan_id=? AND owner_cid=?",
                         payload + (owner, now, int(plan_id), owner))
        else:
            cur = conn.execute("INSERT INTO plans(name,targets_json,skills_json,attrs_json,"
                               "options_json,character_id,owner_cid,created_at,updated_at) "
                               "VALUES(?,?,?,?,?,?,?,?,?)",
                               payload + (owner, now, now))
            plan_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM plans WHERE plan_id=? AND owner_cid=?",
                           (plan_id, owner)).fetchone()
        return _row_to_plan(row) if row else None
    finally:
        conn.close()


def delete_plan(plan_id, owner_cid=None):
    """仅删除本人创建的计划；返回是否真的删掉了。"""
    if owner_cid is None:
        return False
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM plans WHERE plan_id=? AND owner_cid=?",
                           (int(plan_id), int(owner_cid)))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
