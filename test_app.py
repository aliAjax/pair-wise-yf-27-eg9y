import base64
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

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


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _resolved_claim(self, inventory="M-2001-9"):
        obj = self.store.create_object("staff", inventory, "石像", "石刻", "市博物馆", "公开简介。")
        evidence = self.store.upload_evidence(
            "staff", obj["id"], "protocol.pdf", base64.b64encode(b"signed return protocol").decode(), "internal")
        claim = self.store.create_claim("claimant1", obj["id"], "李氏家族", "返还石像")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        return obj, claim, evidence

    def test_handover_flow_changes_holder_once_and_appends_history(self):
        obj, claim, evidence = self._resolved_claim()
        staff_view = self.store.get_object("staff", obj["id"])
        before_version = staff_view["version"]
        pending = self.store.register_handover(
            "staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [evidence["id"]],
            "协议已签署")
        self.assertEqual(pending["status"], "pending")
        pending_view = self.store.get_object("staff", obj["id"])
        self.assertEqual(pending_view["current_holder"], "市博物馆")
        self.assertEqual(pending_view["display_status"], "在馆")
        confirmed = self.store.confirm_handover("reviewer2", pending["id"], "核对无误，准予出库。")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["object_version"], before_version + 1)
        staff_view = self.store.get_object("staff", obj["id"])
        self.assertEqual(staff_view["current_holder"], "李氏家族代表")
        self.assertTrue(staff_view["returned"])
        self.assertEqual(staff_view["handovers"][0]["confirmed_by"], "reviewer2")
        history = self.store.object_history("reviewer1", obj["id"])
        self.assertEqual(len(history), before_version + 1)
        listed = next(x for x in self.store.list_objects("staff") if x["id"] == obj["id"])
        self.assertEqual(listed["display_status"], "已返还")
        public = self.store.get_object("public", obj["id"])
        self.assertEqual(public["display_status"], "已返还")
        self.assertNotIn("current_holder", public)
        self.assertNotIn("handovers", public)
        public_blob = str(public)
        self.assertNotIn("李氏家族代表", public_blob)
        self.assertNotIn("protocol.pdf", public_blob)
        self.assertNotIn("核对无误", public_blob)
        claimant = self.store.get_object("claimant1", obj["id"])
        self.assertNotIn("handovers", claimant)

    def test_repeat_confirm_is_idempotent(self):
        obj, claim, evidence = self._resolved_claim()
        pending = self.store.register_handover(
            "staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [evidence["id"]])
        first = self.store.confirm_handover("reviewer2", pending["id"], "确认。")
        second = self.store.confirm_handover("reviewer1", pending["id"], "再次确认。")
        self.assertTrue(second.get("already_confirmed"))
        self.assertEqual(first["object_version"], second["object_version"])
        self.assertEqual(self.store.get_object("staff", obj["id"])["version"], first["object_version"])

    def test_concurrent_confirms_later_one_keeps_existing_result(self):
        obj, claim, evidence = self._resolved_claim()
        pending = self.store.register_handover(
            "staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [evidence["id"]])
        before_version = self.store.get_object("staff", obj["id"])["version"]
        results, errors = [], []

        def confirm(reviewer):
            try:
                results.append(self.store.confirm_handover(reviewer, pending["id"], "并发确认。"))
            except BusinessError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=confirm, args=("reviewer1",)),
                   threading.Thread(target=confirm, args=("reviewer2",))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for r in results if r.get("already_confirmed")), 1)
        self.assertEqual(self.store.get_object("staff", obj["id"])["current_holder"], "李氏家族代表")
        self.assertEqual(len(self.store.object_history("reviewer1", obj["id"])), before_version + 1)

    def test_registrant_cannot_confirm(self):
        obj, claim, evidence = self._resolved_claim()
        pending = self.store.register_handover(
            "staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [evidence["id"]])
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_handover("staff", pending["id"], "自己确认。")
        self.assertEqual(ctx.exception.status, 403)

    def test_missing_evidence_rejected_without_changing_holder_or_claim(self):
        obj, claim, evidence = self._resolved_claim()
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_handover("staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [])
        self.assertEqual(ctx.exception.code, "evidence_required")
        self.assertEqual(self.store.get_object("staff", obj["id"])["current_holder"], "市博物馆")
        claim_row = self.store.get_object("reviewer1", obj["id"])["claims"][0]
        self.assertEqual(claim_row["status"], "resolved_return")

    def test_date_before_claim_submission_rejected(self):
        obj, claim, evidence = self._resolved_claim()
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_handover("staff", obj["id"], claim["id"], "李氏家族代表", "2000-01-01", [evidence["id"]])
        self.assertEqual(ctx.exception.code, "date_before_claim")

    def test_public_evidence_not_allowed(self):
        obj, claim, _ = self._resolved_claim()
        public_ev = self.store.upload_evidence(
            "staff", obj["id"], "notice.pdf", base64.b64encode(b"public notice").decode(), "public")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_handover(
                "staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [public_ev["id"]])
        self.assertEqual(ctx.exception.code, "evidence_not_internal")

    def test_claim_must_be_resolved_and_foreign_evidence_rejected(self):
        obj = self.store.create_object("staff", "M-2002-1", "陶俑", "陶器", "库房", "公开简介。")
        other = self.store.create_object("staff", "M-2002-2", "陶罐", "陶器", "库房", "公开简介。")
        evidence = self.store.upload_evidence(
            "staff", other["id"], "other.pdf", base64.b64encode(b"other").decode(), "internal")
        claim = self.store.create_claim("claimant1", obj["id"], "后人", "归还")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_handover(
                "staff", obj["id"], claim["id"], "后人", date.today().isoformat(), [evidence["id"]])
        self.assertEqual(ctx.exception.code, "claim_not_resolved")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "受理主张材料。")
        self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "签署返还协议。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_handover(
                "staff", obj["id"], claim["id"], "后人", date.today().isoformat(), [evidence["id"]])
        self.assertEqual(ctx.exception.code, "evidence_not_found")

    def test_cannot_register_again_after_returned(self):
        obj, claim, evidence = self._resolved_claim()
        pending = self.store.register_handover(
            "staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [evidence["id"]])
        self.store.confirm_handover("reviewer2", pending["id"], "确认。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_handover(
                "staff", obj["id"], claim["id"], "另一人", date.today().isoformat(), [evidence["id"]])
        self.assertEqual(ctx.exception.code, "already_returned")
        self.assertEqual(self.store.get_object("staff", obj["id"])["current_holder"], "李氏家族代表")

    def test_only_staff_registers_and_only_reviewer_confirms(self):
        obj, claim, evidence = self._resolved_claim()
        with self.assertRaises(BusinessError) as ctx:
            self.store.register_handover(
                "reviewer1", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [evidence["id"]])
        self.assertEqual(ctx.exception.status, 403)
        pending = self.store.register_handover(
            "staff", obj["id"], claim["id"], "李氏家族代表", date.today().isoformat(), [evidence["id"]])
        with self.assertRaises(BusinessError) as ctx:
            self.store.confirm_handover("claimant1", pending["id"], "无权确认。")
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__":
    unittest.main()
