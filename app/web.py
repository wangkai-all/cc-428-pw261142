"""Flask Web 服务：粒度登记 / 加工执行 / 差异分析 三服务联动的 HTTP 边界。

任务历史只增不改：每次"运行/重建"都写入一条新版本 report_jobs，
重建时把旧任务标记为 superseded（只增的关联表），旧任务内容永不被覆盖。
"""
from __future__ import annotations

import os
import sqlite3

from flask import Flask, jsonify, request, send_from_directory

from . import catalog, engine, store

WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__, static_folder=None)
    db_path = store.init_db(db_path)

    def conn():
        return store.connect(db_path)

    # ---- 静态前端 ----
    @app.get("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/static/<path:fname>")
    def static_files(fname):
        return send_from_directory(WEB_DIR, fname)

    # ---- 粒度登记：表与列 ----
    @app.get("/api/meta/tables")
    def get_tables():
        with conn() as c:
            return jsonify(store.list_table_meta(c))

    @app.get("/api/data/<table>")
    def get_data(table):
        with conn() as c:
            try:
                rows = store.get_rows(c, table)
            except KeyError as exc:
                return jsonify({"error": str(exc)}), 404
            return jsonify({"table": table, "rows": rows})

    # ---- 粒度登记：关系（可修改 + 审计）----
    @app.get("/api/meta/relationships")
    def get_relationships():
        with conn() as c:
            return jsonify(store.list_relationships(c))

    @app.patch("/api/meta/relationships/<name>")
    def patch_relationship(name):
        payload = request.get_json(force=True)
        reason = payload.pop("reason", "")
        with conn() as c:
            try:
                updated = store.update_relationship(c, name, payload, reason)
            except (KeyError, ValueError) as exc:
                return jsonify({"error": str(exc)}), 400
            return jsonify(updated)

    @app.get("/api/meta/audit")
    def get_audit():
        with conn() as c:
            return jsonify(store.list_relationship_audit(c))

    # ---- 故障场景开关 ----
    @app.get("/api/scenarios")
    def get_scenarios():
        with conn() as c:
            return jsonify(store.list_scenarios(c))

    @app.post("/api/scenarios/<name>")
    def post_scenario(name):
        enabled = bool(request.get_json(force=True).get("enabled", False))
        with conn() as c:
            try:
                store.set_scenario(c, name, enabled)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            return jsonify(store.list_scenarios(c))

    # ---- 报表目录 ----
    @app.get("/api/reports")
    def get_reports():
        return jsonify(catalog.CATALOG)

    # ---- 运行 / 重建：执行器 + 差异分析，并写入只增任务 ----
    @app.post("/api/reports/run")
    def run_report():
        body = request.get_json(force=True)
        spec = body.get("spec")
        if not spec:
            name = body.get("report_name")
            try:
                spec = catalog.get_spec(name)
            except KeyError:
                return jsonify({"error": f"未知报表 {name}"}), 404
        requested_path = body.get("path", engine.PATH_NAIVE)
        rebuild_from = body.get("rebuild_from_job")

        with conn() as c:
            try:
                full = engine.run_report(c, spec)
            except engine.EngineError as exc:
                return jsonify({"error": str(exc)}), 400

            # 选定落库路径的结果
            path_obj = full.get(requested_path.lower())
            if path_obj is None:
                return jsonify({"error": f"未知路径 {requested_path}"}), 400

            if path_obj.get("eligible"):
                status, result, diagnosis = "OK", path_obj, full["diagnosis"]
            else:
                status, result, diagnosis = "REJECTED", None, {
                    "rejected_path": requested_path,
                    "reasons": path_obj["reasons"],
                    "full_diagnosis": full["diagnosis"],
                }

            job = store.insert_job(c, spec["report_name"], requested_path, spec,
                                   status, result, diagnosis)

            # 重建：把旧任务标记为被新版本取代（旧任务本身不变）
            supersession = None
            if rebuild_from:
                try:
                    old = store.get_job(c, rebuild_from)
                    store.mark_superseded(c, old, job)
                    supersession = {"old_job_id": old["job_id"], "new_job_id": job["job_id"]}
                except KeyError as exc:
                    return jsonify({"error": str(exc)}), 404

            return jsonify({"job": job, "supersession": supersession,
                            "full": full})

    # ---- 任务历史（只增）----
    @app.get("/api/jobs")
    def get_jobs():
        with conn() as c:
            return jsonify(store.list_jobs(c))

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        with conn() as c:
            try:
                return jsonify(store.get_job(c, job_id))
            except KeyError as exc:
                return jsonify({"error": str(exc)}), 404

    @app.errorhandler(sqlite3.IntegrityError)
    def on_integrity(err):
        # 只增表触发器等约束被违反
        return jsonify({"error": f"存储约束拒绝操作: {err}"}), 409

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=8000, debug=False)
