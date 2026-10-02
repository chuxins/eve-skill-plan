"""应用库（data/app.db）：保存的技能计划。

技能索引是只读的（build_index.py 生成），用户数据只放这里：
计划名 / 目标列表 / 快照的当前技能与属性，便于离线复算与历史对比。
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
    targets_json TEXT NOT NULL DEFAULT '[]',
    skills_json TEXT NOT NULL DEFAULT '{}',
    attrs_json TEXT NOT NULL DEFAULT '{}',
    options_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '');
"""


def connect():
    """打开应用库（必要时建表）。"""
    os.makedirs(os.path.dirname(config.APP_DB), exist_ok=True)
    conn = sqlite3.connect(config.APP_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
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


def list_plans():
    conn = connect()
    try:
        return [_row_to_plan(r) for r in conn.execute(
            "SELECT * FROM plans ORDER BY updated_at DESC, plan_id DESC")]
    finally:
        conn.close()


def get_plan(plan_id):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM plans WHERE plan_id=?", (int(plan_id),)).fetchone()
        return _row_to_plan(row) if row else None
    finally:
        conn.close()


def save_plan(name, targets, skills=None, attrs=None, options=None,
              character_id=None, plan_id=None):
    """新建或更新计划，返回计划 dict。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    payload = (name or "未命名计划", json.dumps(targets or [], ensure_ascii=False),
               json.dumps(skills or {}, ensure_ascii=False),
               json.dumps(attrs or {}, ensure_ascii=False),
               json.dumps(options or {}, ensure_ascii=False),
               int(character_id) if character_id else None)
    conn = connect()
    try:
        if plan_id:
            conn.execute("UPDATE plans SET name=?,targets_json=?,skills_json=?,attrs_json=?,"
                         "options_json=?,character_id=?,updated_at=? WHERE plan_id=?",
                         payload + (now, int(plan_id)))
        else:
            cur = conn.execute("INSERT INTO plans(name,targets_json,skills_json,attrs_json,"
                               "options_json,character_id,created_at,updated_at) "
                               "VALUES(?,?,?,?,?,?,?,?)", payload + (now, now))
            plan_id = cur.lastrowid
        conn.commit()
        row = conn.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        return _row_to_plan(row)
    finally:
        conn.close()


def delete_plan(plan_id):
    conn = connect()
    try:
        cur = conn.execute("DELETE FROM plans WHERE plan_id=?", (int(plan_id),))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
