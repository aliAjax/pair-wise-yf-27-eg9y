import base64
import tempfile
import threading
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        with self.store.connect() as conn:
            conn.execute("INSERT INTO users(id,name,role) VALUES('reviewer2','第二审查员','reviewer')")

    def tearDown(self):
        self.tmp.cleanup()

    def _resolved_claim(self, inventory_no="M-1999-7"):
        obj = self.store.create_object("staff", inventory_no, "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal")
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        return obj, evidence, claim

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)

    def test_delivery_register_confirm_flow(self):
        obj, evidence, claim = self._resolved_claim()
        delivery = self.store.register_delivery(
            "staff", obj["id"], claim["id"], "王氏家族代表", "2026-09-26", "依返还协议第一条约定的现场交接。", [evidence["id"]])
        self.assertEqual(delivery["status"], "pending")
        # 待确认阶段持有人不变，公众也看不到交割细节。
        staff_view = self.store.get_object("staff", obj["id"])
        self.assertEqual(staff_view["current_holder"], "市博物馆")
        self.assertEqual(staff_view["deliveries"][0]["recipient"], "王氏家族代表")
        version_before = staff_view["version"]
        confirmed = self.store.confirm_delivery("reviewer1", delivery["id"], "复核交割单据与证据一致。")
        self.assertEqual(confirmed["status"], "confirmed")
        staff_view = self.store.get_object("staff", obj["id"])
        # 持有人只改一次，追加一个历史版本。
        self.assertEqual(staff_view["current_holder"], "王氏家族代表")
        self.assertEqual(staff_view["version"], version_before + 1)
        self.assertEqual(staff_view["display_status"], "已返还")
        # 列表和公众详情显示“已返还”，但受领人、证据和审查说明不公开。
        self.assertEqual(self.store.list_objects("public")[0]["display_status"], "已返还")
        public_view = self.store.get_object("public", obj["id"])
        self.assertEqual(public_view["display_status"], "已返还")
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(public_view["deliveries"][0]["status"], "confirmed")
        for hidden in ("recipient", "note", "evidence", "registered_by", "confirmed_by"):
            self.assertNotIn(hidden, public_view["deliveries"][0])
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertNotIn("recipient", claimant_view["deliveries"][0])
        # 确认后不能再登记新的交割单。
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_delivery("staff", obj["id"], claim["id"], "他人", "2026-09-27", "重复出库尝试。", [evidence["id"]])
        self.assertEqual(ctx.exception.code, "already_returned")

    def test_delivery_register_validation(self):
        obj, evidence, claim = self._resolved_claim()
        other = self.store.create_object("staff", "M-2002-1", "画卷", "纸质", "库房", "简介。")
        open_claim = self.store.create_claim("claimant1", other["id"], "李氏家族", "返还画卷")
        # 主张未到 resolved_return 不能登记。
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_delivery("staff", other["id"], open_claim["id"], "李某", "2026-09-26", "提前登记。", [evidence["id"]])
        self.assertEqual(ctx.exception.code, "claim_not_resolved")
        # 缺证据不能登记。
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_delivery("staff", obj["id"], claim["id"], "王某", "2026-09-26", "没有附证据。", [])
        self.assertEqual(ctx.exception.code, "evidence_required")
        # 交接日期早于主张提交日不能登记。
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_delivery("staff", obj["id"], claim["id"], "王某", "1990-01-01", "日期异常。", [evidence["id"]])
        self.assertEqual(ctx.exception.code, "invalid_handover_date")
        # 其他藏品的证据不能关联。
        other_evidence = self.store.upload_evidence("staff", other["id"], "other.pdf", base64.b64encode(b"other").decode(), "internal")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_delivery("staff", obj["id"], claim["id"], "王某", "2026-09-26", "证据张冠李戴。", [other_evidence["id"]])
        self.assertEqual(ctx.exception.code, "evidence_not_found")
        # 校验失败不改变原持有人和主张状态。
        staff_view = self.store.get_object("staff", obj["id"])
        self.assertEqual(staff_view["current_holder"], "市博物馆")
        self.assertEqual(staff_view["claims"][0]["status"], "resolved_return")
        self.assertEqual(staff_view["deliveries"], [])

    def test_delivery_self_confirm_rejected_and_concurrent_confirm(self):
        obj, evidence, claim = self._resolved_claim()
        # 审查员登记后不能自己确认，必须由另一名审查员确认。
        delivery = self.store.register_delivery(
            "reviewer1", obj["id"], claim["id"], "王氏家族代表", "2026-09-26", "审查员代登记交割。", [evidence["id"]])
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_delivery("reviewer1", delivery["id"], "自己确认自己的单子。")
        self.assertEqual(ctx.exception.code, "self_confirm_forbidden")
        self.assertEqual(self.store.get_object("staff", obj["id"])["deliveries"][0]["status"], "pending")
        confirmed = self.store.confirm_delivery("reviewer2", delivery["id"], "第二审查员复核通过。")
        self.assertEqual(confirmed["confirmed_by"], "reviewer2")

        # 两人同时确认：后到者返回已有结果，持有人只改一次。
        obj2, evidence2, claim2 = self._resolved_claim("M-1999-8")
        delivery2 = self.store.register_delivery(
            "staff", obj2["id"], claim2["id"], "王氏家族代表", "2026-09-26", "并发确认演练。", [evidence2["id"]])
        version_before = self.store.get_object("staff", obj2["id"])["version"]
        results, errors = [], []
        def confirm(uid):
            try:
                results.append(self.store.confirm_delivery(uid, delivery2["id"], "并发确认复核通过。"))
            except BusinessError as exc:
                errors.append(exc)
        threads = [threading.Thread(target=confirm, args=(uid,)) for uid in ("reviewer1", "reviewer2")]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for r in results if r.get("already_confirmed")), 1)
        staff_view = self.store.get_object("staff", obj2["id"])
        self.assertEqual(staff_view["current_holder"], "王氏家族代表")
        self.assertEqual(staff_view["version"], version_before + 1)


if __name__ == "__main__":
    unittest.main()
