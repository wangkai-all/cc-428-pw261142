"""端到端测试：虚构数据的真实加工与诊断。

运行：python3 -I -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import catalog, engine, store  # noqa: E402
from app.dataset import TRUTH  # noqa: E402


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = store.init_db(self.tmp.name, reset=True)
        self.conn = store.connect(self.db)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def execute(self, name):
        return engine.run_report(self.conn, catalog.get_spec(name))


class TestCorrectResults(BaseCase):
    """合法报表的结果必须与人工核算真值一致。"""

    def test_r1_category_matches_truth(self):
        out = self.execute("R1 行金额按品类")
        self.assertTrue(out["atj"]["eligible"])
        got = {r["group"][0]: r["line_amt"] for r in out["atj"]["result"]}
        self.assertEqual(got, TRUTH["line_amt_by_category"])
        # 三条路径在无扇出场景下结果一致
        self.assertEqual(out["naive"]["result"], out["atj"]["result"])
        self.assertEqual(out["dja"]["result"], out["atj"]["result"])

    def test_r2_region_matches_truth(self):
        out = self.execute("R2 订单金额按区域")
        got = {r["group"][0]: r["order_amt"] for r in out["atj"]["result"]}
        self.assertEqual(got, TRUTH["order_amt_by_region"])

    def test_r3_cost_center_matches_truth(self):
        out = self.execute("R3 分摊金额按成本中心")
        got = {r["group"][0]: r["alloc_amt"] for r in out["atj"]["result"]}
        self.assertEqual(got, TRUTH["alloc_amt_by_cost_center"])

    def test_r6_dja_conservation(self):
        out = self.execute("R6 订单金额按成本中心(粗粒度下推)")
        # ATJ 必须拒绝扇出
        self.assertFalse(out["atj"]["eligible"])
        self.assertIn("FANOUT_EDGE", [r["code"] for r in out["atj"]["reasons"]])
        # DJA 合法且总量守恒
        self.assertTrue(out["dja"]["eligible"])
        self.assertTrue(all(out["dja"]["conservation_check"].values()))
        self.assertEqual(out["dja"]["allocated_totals"]["line_amt"],
                         TRUTH["total_line_amt"])
        # 朴素连接确实放大了
        self.assertGreater(out["naive"]["naive_totals"]["line_amt"],
                           TRUTH["total_line_amt"])


class TestPathEligibility(BaseCase):
    """适用条件识别：不满足条件的查询必须被拒绝并解释，而非静默去重。"""

    def test_r4_mn_rejected_by_both_safe_paths(self):
        out = self.execute("R4 行金额按活动(M:N错误关联)")
        atj_codes = {r["code"] for r in out["atj"]["reasons"]}
        dja_codes = {r["code"] for r in out["dja"]["reasons"]}
        self.assertIn("FANOUT_EDGE", atj_codes)
        self.assertIn("MISSING_ALLOCATION_BASIS", dja_codes)
        self.assertEqual(out["diagnosis"]["valid_paths"], [])
        self.assertFalse(out["diagnosis"]["rebuildable"])

    def test_duplicate_dimension_rejected_at_runtime(self):
        # 登记上是 N:1，但运行期数据出现重复键 -> 必须运行期抓出
        store.set_scenario(self.conn, "duplicate_product", True)
        out = self.execute("R5 行金额按品类(维度重复场景)")
        for path in ("atj", "dja"):
            codes = [r["code"] for r in out[path]["reasons"]]
            self.assertIn("NON_UNIQUE_PARENT_KEY", codes)
            sample = out[path]["reasons"][0]["sample"]
            self.assertEqual(sample[0]["key"], ["P-A"])
        # 朴素连接把 P-A 的行翻倍，金额从 3300 放大
        self.assertEqual(out["naive"]["naive_totals"]["line_amt"], 4550.0)

    def test_incomplete_weights_rejected_not_normalized(self):
        store.set_scenario(self.conn, "incomplete_weights", True)
        out = self.execute("R6 订单金额按成本中心(粗粒度下推)")
        codes = [r["code"] for r in out["dja"]["reasons"]]
        self.assertIn("INCOMPLETE_WEIGHTS", codes)
        # 明确指出问题父键 L-1 与权重和 0.7
        sample = next(r for r in out["dja"]["reasons"]
                      if r["code"] == "INCOMPLETE_WEIGHTS")["sample"]
        self.assertEqual(sample[0]["parent_key"], ["L-1"])
        self.assertAlmostEqual(sample[0]["weight_sum"], 0.7)

    def test_non_additive_measure_rejected(self):
        # weight 是不可加度量，对它求和应被两条安全路径拒绝
        spec = {
            "report_name": "T 不可加度量",
            "fact_table": "allocations",
            "measures": [{"col": "weight", "agg": "sum"}],
            "group_by": ["cost_center"],
        }
        out = engine.run_report(self.conn, spec)
        for path in ("atj", "dja"):
            self.assertTrue(any(r["code"] == "NON_ADDITIVE_MEASURE"
                                for r in out[path]["reasons"]))


class TestDiagnosis(BaseCase):
    """差异分析要同时展示重复放大与孤儿丢失，定位到具体事实行。"""

    def test_r4_splits_duplication_and_orphan_loss(self):
        out = self.execute("R4 行金额按活动(M:N错误关联)")
        n = out["naive"]
        # L-1 挂两个活动 -> 重复放大 600
        self.assertEqual(n["repeated_extra"]["line_amt"], 600.0)
        # L-3/L-4/L-6 未挂活动 -> 丢失 350+250+300=900
        self.assertEqual(n["lost_totals"]["line_amt"], 900.0)
        # 净差异 -300 是两者抵消，诊断必须两个方向都报
        self.assertEqual(n["net_delta"]["line_amt"], -300.0)
        codes = {f["code"] for f in out["diagnosis"]["findings"]}
        self.assertIn("DUPLICATE_CONTRIBUTION", codes)
        self.assertIn("ORPHAN_LOSS", codes)

    def test_duplicate_contribution_ledger_identifies_row(self):
        out = self.execute("R4 行金额按活动(M:N错误关联)")
        dup = out["naive"]["duplicate_contributions"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["fact_key"], ["L-1"])
        self.assertEqual(dup[0]["copies"], 2)
        # 单值 600，被重复累计成 1200
        self.assertEqual(dup[0]["measures"]["line_amt"]["single_value"], 600.0)
        self.assertEqual(dup[0]["measures"]["line_amt"]["repeated_total"], 1200.0)

    def test_no_fanout_has_no_findings(self):
        out = self.execute("R1 行金额按品类")
        codes = {f["code"] for f in out["diagnosis"]["findings"]}
        self.assertIn("NAIVE_OK", codes)
        self.assertNotIn("DUPLICATE_CONTRIBUTION", codes)


class TestRegistryLinkage(BaseCase):
    """网页修改维度关系后，执行器与诊断必须联动。"""

    def test_deactivate_relationship_blocks_path(self):
        store.update_relationship(self.conn, "rel_line_product",
                                  {"active": False}, reason="测试停用")
        out = self.execute("R1 行金额按品类")
        codes = [r["code"] for r in out["atj"]["reasons"]]
        self.assertIn("NO_PATH", codes)

    def test_change_cardinality_to_mn_marks_fanout(self):
        # 把产品维度关系改成 M:N（即使数据实际唯一），ATJ 应按登记拒绝
        store.update_relationship(self.conn, "rel_line_product",
                                  {"cardinality": "M:N"}, reason="测试基数修正")
        out = self.execute("R1 行金额按品类")
        self.assertFalse(out["atj"]["eligible"])
        self.assertIn("FANOUT_EDGE", [r["code"] for r in out["atj"]["reasons"]])

    def test_audit_trail_recorded(self):
        store.update_relationship(self.conn, "rel_line_product",
                                  {"active": False}, reason="人工修正")
        audit = store.list_relationship_audit(self.conn)
        self.assertEqual(audit[0]["relationship_name"], "rel_line_product")
        self.assertEqual(audit[0]["reason"], "人工修正")


class TestDiagnoseFixRebuildLoop(BaseCase):
    """完整闭环：诊断发现问题 -> 修正数据/登记 -> 报表恢复可重建，旧任务保留。"""

    def test_duplicate_dimension_loop(self):
        # 阶段1：故障开启，两条安全路径拒绝，不可重建
        store.set_scenario(self.conn, "duplicate_product", True)
        broken = self.execute("R5 行金额按品类(维度重复场景)")
        self.assertFalse(broken["diagnosis"]["rebuildable"])
        j_bad = store.insert_job(self.conn, "R5 行金额按品类(维度重复场景)",
                                 "ATJ", broken["spec"], "REJECTED", None,
                                 broken["diagnosis"])
        self.assertEqual(j_bad["status"], "REJECTED")

        # 阶段2：修复（关闭重复维度场景），同一报表重新变为合法可重建
        store.set_scenario(self.conn, "duplicate_product", False)
        fixed = self.execute("R5 行金额按品类(维度重复场景)")
        self.assertTrue(fixed["diagnosis"]["rebuildable"])
        self.assertEqual(fixed["atj"]["atj_totals"]["line_amt"],
                         TRUTH["total_line_amt"])
        j_good = store.insert_job(self.conn, "R5 行金额按品类(维度重复场景)",
                                  "ATJ", fixed["spec"], "OK", fixed["atj"],
                                  fixed["diagnosis"])
        store.mark_superseded(self.conn, j_bad, j_good)

        # 阶段3：两条任务都在，旧的 REJECTED 未被覆盖
        jobs = [j for j in store.list_jobs(self.conn)
                if j["report_name"] == "R5 行金额按品类(维度重复场景)"]
        self.assertEqual([j["version"] for j in jobs], [1, 2])
        self.assertEqual(jobs[0]["status"], "REJECTED")
        self.assertEqual(jobs[1]["status"], "OK")
        self.assertEqual(jobs[0]["new_job_id"], j_good["job_id"])

    def test_incomplete_weight_loop_restores_conservation(self):
        store.set_scenario(self.conn, "incomplete_weights", True)
        broken = self.execute("R6 订单金额按成本中心(粗粒度下推)")
        self.assertFalse(broken["dja"]["eligible"])
        store.set_scenario(self.conn, "incomplete_weights", False)
        fixed = self.execute("R6 订单金额按成本中心(粗粒度下推)")
        self.assertTrue(fixed["dja"]["eligible"])
        self.assertTrue(all(fixed["dja"]["conservation_check"].values()))


class TestJobImmutability(unittest.TestCase):
    """旧任务不能被修正版覆盖：只增表 + 取代关系。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = store.init_db(self.tmp.name, reset=True)
        self.conn = store.connect(self.db)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def test_jobs_are_append_only_and_versioned(self):
        import sqlite3
        spec = catalog.get_spec("R1 行金额按品类")
        full = engine.run_report(self.conn, spec)
        j1 = store.insert_job(self.conn, spec["report_name"], "ATJ", spec,
                              "OK", full["atj"], full["diagnosis"])
        j2 = store.insert_job(self.conn, spec["report_name"], "DJA", spec,
                              "OK", full["dja"], full["diagnosis"])
        self.assertEqual((j1["version"], j2["version"]), (1, 2))
        self.assertNotEqual(j1["job_id"], j2["job_id"])
        # 数据库触发器拒绝 UPDATE / DELETE
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE report_jobs SET status='X' WHERE job_id=?",
                              (j1["job_id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM report_jobs WHERE job_id=?", (j1["job_id"],))
        # 旧任务内容原样可读
        self.assertEqual(store.get_job(self.conn, j1["job_id"])["status"], "OK")

    def test_supersession_links_old_to_new(self):
        spec = catalog.get_spec("R6 订单金额按成本中心(粗粒度下推)")
        full = engine.run_report(self.conn, spec)
        old = store.insert_job(self.conn, spec["report_name"], "NAIVE", spec,
                               "OK", full["naive"], full["diagnosis"])
        new = store.insert_job(self.conn, spec["report_name"], "DJA", spec,
                               "OK", full["dja"], full["diagnosis"])
        store.mark_superseded(self.conn, old, new)
        refreshed = store.get_job(self.conn, old["job_id"])
        self.assertEqual(refreshed["new_job_id"], new["job_id"])
        # 旧任务仍保留 NAIVE 路径与原结果，没有被新结果覆盖
        self.assertEqual(refreshed["path"], "NAIVE")


if __name__ == "__main__":
    unittest.main()
