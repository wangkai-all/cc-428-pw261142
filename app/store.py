"""SQLite 存储层：业务数据表 + 粒度登记元数据 + 只增任务历史。

设计要点：
- 业务数据表从 dataset.py 播种，故障场景（重复维度行/不完整权重）按需生效。
- report_jobs 为只增表，用触发器禁止 UPDATE/DELETE；修正版以新任务写入，
  新旧关系记录在只增的 job_supersessions，旧任务永远不会被覆盖。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

from . import dataset

DB_PATH = os.environ.get("GRAIN_DB", os.path.join(os.path.dirname(__file__), "..", "grain.db"))

_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# 表结构登记：粒度标签、粒度键、主键、列角色与可加性
# additive=1 的度量才能做 SUM 聚合（先聚合再关联的前提之一）
# ---------------------------------------------------------------------------
TABLE_SPECS = {
    "orders": {
        "grain_label": "订单粒度",
        "grain_keys": ["order_id"],
        "pk": ["order_id"],
        "columns": [
            ("order_id", "key", 0), ("region", "dim", 0), ("channel", "dim", 0),
            ("order_amt", "measure", 1), ("order_cnt", "measure", 1),
            ("order_date", "dim", 0),
        ],
    },
    "order_lines": {
        "grain_label": "订单行粒度",
        "grain_keys": ["line_id"],
        "pk": ["line_id"],
        "columns": [
            ("line_id", "key", 0), ("order_id", "key", 0), ("product_id", "dim", 0),
            ("line_amt", "measure", 1), ("line_cnt", "measure", 1),
        ],
    },
    "allocations": {
        "grain_label": "分摊粒度",
        "grain_keys": ["alloc_id"],
        "pk": ["alloc_id"],
        "columns": [
            ("alloc_id", "key", 0), ("line_id", "key", 0),
            ("cost_center", "dim", 0), ("weight", "measure", 0),
            ("alloc_amt", "measure", 1),
        ],
    },
    "dim_product": {
        "grain_label": "产品维度",
        "grain_keys": ["product_id"],
        "pk": ["product_id"],
        "columns": [
            ("product_id", "key", 0), ("category", "dim", 0), ("brand", "dim", 0),
        ],
    },
    "campaigns": {
        "grain_label": "营销活动维度",
        "grain_keys": ["campaign_id"],
        "pk": ["campaign_id"],
        "columns": [
            ("campaign_id", "key", 0), ("campaign_name", "dim", 0),
        ],
    },
    "line_campaign": {
        "grain_label": "行-活动桥接(多对多)",
        "grain_keys": ["line_id", "campaign_id"],
        "pk": ["line_id", "campaign_id"],
        "columns": [
            ("line_id", "key", 0), ("campaign_id", "key", 0),
        ],
    },
}

# 关系登记。cardinality 为"左表 -> 右表"方向：
#   N:1  左是多端（子），右是一端（父），右表连接键必须唯一才不扇出
#   1:N  左是一端（父），右是多端（子），连接必然复制左表行
#   M:N  两端都被复制
# basis_table/basis_col：分摊基准（位于多端的权重列），DJA 路径的必要条件
RELATIONSHIP_SPECS = [
    {
        "name": "rel_line_order",
        "left_table": "order_lines", "left_cols": ["order_id"],
        "right_table": "orders", "right_cols": ["order_id"],
        "cardinality": "N:1", "basis_table": None, "basis_col": None,
        "note": "订单行属于订单",
    },
    {
        "name": "rel_alloc_line",
        "left_table": "allocations", "left_cols": ["line_id"],
        "right_table": "order_lines", "right_cols": ["line_id"],
        "cardinality": "N:1",
        "basis_table": "allocations", "basis_col": "weight",
        "note": "分摊行按 weight 归属于订单行；反向(行->分摊)是带基准的 1:N",
    },
    {
        "name": "rel_line_product",
        "left_table": "order_lines", "left_cols": ["product_id"],
        "right_table": "dim_product", "right_cols": ["product_id"],
        "cardinality": "N:1", "basis_table": None, "basis_col": None,
        "note": "订单行关联产品维度",
    },
    {
        "name": "rel_linecamp_line",
        "left_table": "line_campaign", "left_cols": ["line_id"],
        "right_table": "order_lines", "right_cols": ["line_id"],
        "cardinality": "N:1", "basis_table": None, "basis_col": None,
        "note": "桥接表侧；order_lines 与 campaigns 实际为 M:N，无分摊基准",
    },
    {
        "name": "rel_linecamp_campaign",
        "left_table": "line_campaign", "left_cols": ["campaign_id"],
        "right_table": "campaigns", "right_cols": ["campaign_id"],
        "cardinality": "N:1", "basis_table": None, "basis_col": None,
        "note": "桥接表关联活动名称",
    },
    {
        "name": "rel_line_campaign_mn",
        "left_table": "order_lines", "left_cols": ["line_id"],
        "right_table": "campaigns", "right_cols": ["campaign_id"],
        "cardinality": "M:N", "basis_table": None, "basis_col": None,
        "note": "订单行与营销活动经 line_campaign 桥接的真实 M:N 关系，无分摊基准",
        "join_through": {
            "bridge": "line_campaign",
            "left_bridge_cols": ["line_id"],
            "right_bridge_cols": ["campaign_id"],
        },
    },
]

DATA_TABLE_DDL = {
    "orders": "CREATE TABLE orders(order_id TEXT, region TEXT, channel TEXT, "
              "order_amt REAL, order_cnt INTEGER, order_date TEXT)",
    "order_lines": "CREATE TABLE order_lines(line_id TEXT, order_id TEXT, "
                   "product_id TEXT, line_amt REAL, line_cnt INTEGER)",
    "allocations": "CREATE TABLE allocations(alloc_id TEXT, line_id TEXT, "
                   "cost_center TEXT, weight REAL, alloc_amt REAL)",
    # 刻意不加 UNIQUE：重复 product_id 是运行期诊断要抓的故障
    "dim_product": "CREATE TABLE dim_product(product_id TEXT, category TEXT, brand TEXT)",
    "campaigns": "CREATE TABLE campaigns(campaign_id TEXT, campaign_name TEXT)",
    "line_campaign": "CREATE TABLE line_campaign(line_id TEXT, campaign_id TEXT)",
}


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | None = None, reset: bool = False) -> str:
    path = db_path or DB_PATH
    with _LOCK:
        if reset and os.path.exists(path):
            os.remove(path)
        conn = connect(path)
        try:
            _create_meta(conn)
            _seed_registry(conn)
            for name, ddl in DATA_TABLE_DDL.items():
                conn.execute(ddl)
            _seed_data(conn)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scenario_state(name TEXT PRIMARY KEY, enabled INTEGER NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
    return path


def _create_meta(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reg_tables(
            table_name TEXT PRIMARY KEY,
            grain_label TEXT NOT NULL,
            grain_keys TEXT NOT NULL,   -- json array
            pk_cols TEXT NOT NULL       -- json array
        );
        CREATE TABLE IF NOT EXISTS reg_columns(
            table_name TEXT NOT NULL,
            col_name TEXT NOT NULL,
            role TEXT NOT NULL,         -- key | dim | measure
            additive INTEGER NOT NULL,
            PRIMARY KEY(table_name, col_name)
        );
        CREATE TABLE IF NOT EXISTS reg_relationships(
            name TEXT PRIMARY KEY,
            left_table TEXT NOT NULL,
            left_cols TEXT NOT NULL,
            right_table TEXT NOT NULL,
            right_cols TEXT NOT NULL,
            cardinality TEXT NOT NULL,  -- N:1 | 1:N | M:N
            basis_table TEXT,
            basis_col TEXT,
            join_through TEXT,          -- 桥接信息 json，仅 M:N 使用
            active INTEGER NOT NULL DEFAULT 1,
            note TEXT
        );
        CREATE TABLE IF NOT EXISTS reg_relationship_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            relationship_name TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            reason TEXT
        );
        CREATE TABLE IF NOT EXISTS report_jobs(
            job_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            report_name TEXT NOT NULL,
            path TEXT NOT NULL,              -- NAIVE | ATJ | DJA
            spec_json TEXT NOT NULL,
            status TEXT NOT NULL,            -- OK | REJECTED | FAILED
            result_json TEXT,
            diagnosis_json TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY(job_id, version)
        );
        CREATE TABLE IF NOT EXISTS job_supersessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            old_job_id TEXT NOT NULL,
            old_version INTEGER NOT NULL,
            new_job_id TEXT NOT NULL,
            new_version INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        -- 任务表只增：任何 UPDATE/DELETE 都被数据库本身拒绝
        CREATE TRIGGER IF NOT EXISTS trg_jobs_no_update
        BEFORE UPDATE ON report_jobs
        BEGIN SELECT RAISE(ABORT, 'report_jobs 是只增表，修正版必须新建任务'); END;
        CREATE TRIGGER IF NOT EXISTS trg_jobs_no_delete
        BEFORE DELETE ON report_jobs
        BEGIN SELECT RAISE(ABORT, 'report_jobs 是只增表，禁止删除历史任务'); END;
        """
    )


def _seed_registry(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM reg_tables LIMIT 1").fetchone():
        return
    for name, spec in TABLE_SPECS.items():
        conn.execute(
            "INSERT INTO reg_tables(table_name, grain_label, grain_keys, pk_cols) VALUES(?,?,?,?)",
            (name, spec["grain_label"], json.dumps(spec["grain_keys"]), json.dumps(spec["pk"])),
        )
        for col_name, role, additive in spec["columns"]:
            conn.execute(
                "INSERT INTO reg_columns(table_name, col_name, role, additive) VALUES(?,?,?,?)",
                (name, col_name, role, additive),
            )
    for rel in RELATIONSHIP_SPECS:
        conn.execute(
            "INSERT INTO reg_relationships(name,left_table,left_cols,right_table,right_cols,"
            "cardinality,basis_table,basis_col,join_through,active,note) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
            (rel["name"], rel["left_table"], json.dumps(rel["left_cols"]),
             rel["right_table"], json.dumps(rel["right_cols"]), rel["cardinality"],
             rel["basis_table"], rel["basis_col"],
             json.dumps(rel["join_through"], ensure_ascii=False) if rel.get("join_through") else None,
             rel["note"]),
        )


def _seed_data(conn: sqlite3.Connection) -> None:
    for name in DATA_TABLE_DDL:
        rows = dataset.table_rows(name, duplicate_product=False)
        if not rows:
            continue
        placeholders = ",".join("?" * len(rows[0]))
        conn.executemany(f"INSERT INTO {name} VALUES({placeholders})", rows)


# ---------------------------------------------------------------------------
# 数据访问（故障场景在此生效）
# ---------------------------------------------------------------------------
def list_scenarios(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT name, enabled FROM scenario_state").fetchall()
    out = {r["name"]: bool(r["enabled"]) for r in rows}
    out.setdefault("duplicate_product", False)
    out.setdefault("incomplete_weights", False)
    return out


def set_scenario(conn: sqlite3.Connection, name: str, enabled: bool) -> None:
    valid = {"duplicate_product", "incomplete_weights"}
    if name not in valid:
        raise ValueError(f"未知场景 {name}")
    conn.execute(
        "INSERT INTO scenario_state(name, enabled) VALUES(?,?) "
        "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled",
        (name, 1 if enabled else 0),
    )
    conn.commit()


def get_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    if table not in DATA_TABLE_DDL:
        raise KeyError(f"未登记的数据表 {table}")
    scenarios = list_scenarios(conn)
    if table == "dim_product" and scenarios["duplicate_product"]:
        rows = dataset.table_rows("dim_product", duplicate_product=True)
        cols = [d[0] for d in TABLE_SPECS[table]["columns"]]
        return [dict(zip(cols, r)) for r in rows]
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    result = [dict(r) for r in rows]
    if table == "allocations" and scenarios["incomplete_weights"]:
        # A-2 的权重从 0.4 改成 0.1：L-1 的权重之和变为 0.7（不完整分摊）
        for row in result:
            if row["alloc_id"] == "A-2":
                row["weight"] = 0.1
    return result


def get_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    rows = conn.execute(
        "SELECT col_name, role, additive FROM reg_columns WHERE table_name=? ORDER BY rowid",
        (table,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 粒度登记 / 关系登记访问
# ---------------------------------------------------------------------------
def list_table_meta(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM reg_tables ORDER BY table_name").fetchall():
        item = dict(r)
        item["grain_keys"] = json.loads(item["grain_keys"])
        item["pk_cols"] = json.loads(item["pk_cols"])
        item["columns"] = get_columns(conn, item["table_name"])
        out.append(item)
    return out


def list_relationships(conn: sqlite3.Connection, active_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM reg_relationships"
    if active_only:
        sql += " WHERE active=1"
    rows = conn.execute(sql + " ORDER BY name").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["left_cols"] = json.loads(item["left_cols"])
        item["right_cols"] = json.loads(item["right_cols"])
        item["join_through"] = json.loads(item["join_through"]) if item["join_through"] else None
        item["active"] = bool(item["active"])
        out.append(item)
    return out


def get_relationship(conn: sqlite3.Connection, name: str) -> dict:
    r = conn.execute("SELECT * FROM reg_relationships WHERE name=?", (name,)).fetchone()
    if r is None:
        raise KeyError(f"关系 {name} 不存在")
    item = dict(r)
    item["left_cols"] = json.loads(item["left_cols"])
    item["right_cols"] = json.loads(item["right_cols"])
    item["join_through"] = json.loads(item["join_through"]) if item["join_through"] else None
    item["active"] = bool(item["active"])
    return item


def update_relationship(conn: sqlite3.Connection, name: str, changes: dict, reason: str = "") -> dict:
    before = get_relationship(conn, name)
    allowed = {"cardinality", "basis_table", "basis_col", "active", "note", "left_cols", "right_cols"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"不允许修改的字段: {sorted(unknown)}")
    if "cardinality" in changes and changes["cardinality"] not in ("N:1", "1:N", "M:N"):
        raise ValueError("cardinality 只能是 N:1 / 1:N / M:N")
    for key in ("left_cols", "right_cols"):
        if key in changes and not (isinstance(changes[key], list) and changes[key]):
            raise ValueError(f"{key} 必须是非空数组")

    merged = dict(before)
    merged.update(changes)
    conn.execute(
        "UPDATE reg_relationships SET left_table=?,left_cols=?,right_table=?,right_cols=?,"
        "cardinality=?,basis_table=?,basis_col=?,join_through=?,active=?,note=? WHERE name=?",
        (merged["left_table"], json.dumps(merged["left_cols"]),
         merged["right_table"], json.dumps(merged["right_cols"]),
         merged["cardinality"], merged["basis_table"], merged["basis_col"],
         json.dumps(merged["join_through"], ensure_ascii=False) if merged.get("join_through") else None,
         1 if merged["active"] else 0, merged["note"], name),
    )
    conn.execute(
        "INSERT INTO reg_relationship_audit(relationship_name,changed_at,before_json,after_json,reason)"
        " VALUES(?,?,?,?,?)",
        (name, time.strftime("%Y-%m-%d %H:%M:%S"),
         json.dumps(before, ensure_ascii=False),
         json.dumps(merged, ensure_ascii=False), reason),
    )
    conn.commit()
    return merged


def list_relationship_audit(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM reg_relationship_audit ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 任务历史（只增）
# ---------------------------------------------------------------------------
def insert_job(conn: sqlite3.Connection, report_name: str, path: str, spec: dict,
               status: str, result: dict | None, diagnosis: dict | None) -> dict:
    version = conn.execute(
        "SELECT COALESCE(MAX(version),0)+1 AS v FROM report_jobs WHERE report_name=?",
        (report_name,),
    ).fetchone()["v"]
    job_id = f"{report_name}-{uuid.uuid4().hex[:8]}"
    created_at = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO report_jobs(job_id,version,report_name,path,spec_json,status,"
        "result_json,diagnosis_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (job_id, version, report_name, path, json.dumps(spec, ensure_ascii=False),
         status, json.dumps(result, ensure_ascii=False) if result else None,
         json.dumps(diagnosis, ensure_ascii=False) if diagnosis else None, created_at),
    )
    conn.commit()
    return {"job_id": job_id, "version": version, "report_name": report_name,
            "path": path, "status": status, "created_at": created_at}


def mark_superseded(conn: sqlite3.Connection, old_job: dict, new_job: dict) -> None:
    conn.execute(
        "INSERT INTO job_supersessions(old_job_id,old_version,new_job_id,new_version,created_at)"
        " VALUES(?,?,?,?,?)",
        (old_job["job_id"], old_job["version"], new_job["job_id"], new_job["version"],
         time.strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()


def list_jobs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT j.*, s.new_job_id, s.new_version FROM report_jobs j "
        "LEFT JOIN job_supersessions s ON j.job_id=s.old_job_id AND j.version=s.old_version "
        "ORDER BY j.report_name, j.version"
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        for key in ("spec_json", "result_json", "diagnosis_json"):
            item[key[:-5] if key.endswith("_json") else key] = (
                json.loads(item.pop(key)) if item[key] is not None else None
            )
        out.append(item)
    return out


def get_job(conn: sqlite3.Connection, job_id: str) -> dict:
    r = conn.execute(
        "SELECT j.*, s.new_job_id, s.new_version FROM report_jobs j "
        "LEFT JOIN job_supersessions s ON j.job_id=s.old_job_id AND j.version=s.old_version "
        "WHERE j.job_id=?", (job_id,),
    ).fetchone()
    if r is None:
        raise KeyError(f"任务 {job_id} 不存在")
    item = dict(r)
    for key in ("spec_json", "result_json", "diagnosis_json"):
        new_key = key[:-5]
        item[new_key] = json.loads(item.pop(key)) if item[key] is not None else None
    return item
