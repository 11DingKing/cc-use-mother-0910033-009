"""整改闭环核心服务测试：依赖、证据版本、结案门槛、决定链与提醒幂等。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remediation import (
    ClosureBlocked,
    ConflictError,
    DependencyBlocked,
    RemediationService,
    ValidationError,
)

CASE_ID = "0910033-009-A"
START = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

REQUIREMENTS = [
    {
        "requirement_id": "R1",
        "title": "人员与设备整改",
        "measures": [
            {"measure_id": "M1", "kind": "人员停岗", "title": "张某暂停执业并离岗培训", "deadline": "2026-10-10"},
            {
                "measure_id": "M2",
                "kind": "设备整改",
                "title": "3 号设备停用校准并复检",
                "deadline": "2026-10-12",
                "depends_on": ["M1"],
            },
            {
                "measure_id": "M3",
                "kind": "设备整改",
                "title": "备件台账补录",
                "deadline": "2026-10-12",
                "required": False,
            },
        ],
    },
    {
        "requirement_id": "R2",
        "title": "制度修订",
        "measures": [
            {"measure_id": "M4", "kind": "制度修订", "title": "修订授权管理制度", "deadline": "2026-10-20"},
        ],
    },
]


def make_service():
    holder = {"now": START}
    service = RemediationService(clock=lambda: holder["now"])
    return service, holder


def create_base_case(service):
    return service.create_case(
        case_id=CASE_ID,
        decision_ref="检决字〔2026〕15号",
        institution="示例检测机构",
        requirements=REQUIREMENTS,
        actor="监管人员-陈",
    )


def pass_measure(service, measure_id, item_name="整改完成证明"):
    service.submit_evidence(
        measure_id,
        submitted_by="机构合规员-李",
        batch="批次1",
        items=[{"name": item_name, "uri": f"oss://evidence/{measure_id}.pdf"}],
    )
    return service.review_evidence(
        measure_id,
        verifier="复核专家-王",
        item_results=[{"item": item_name, "verdict": "通过"}],
    )


class CreateCaseTest(unittest.TestCase):
    def test_create_case_splits_requirements_into_measures(self):
        service, _ = make_service()
        detail = create_base_case(service)
        self.assertEqual(detail["status"], "登记")
        self.assertEqual(len(detail["requirements"]), 2)
        measures = {m["id"]: m for r in detail["requirements"] for m in r["measures"]}
        self.assertEqual(measures["M2"]["depends_on"], ["M1"])
        self.assertEqual(measures["M2"]["deadline"], "2026-10-12")
        self.assertFalse(measures["M3"]["required"])
        decisions = service.list_decisions(CASE_ID)
        self.assertEqual(decisions[0]["kind"], "检查决定下达")

    def test_duplicate_case_id_rejected(self):
        service, _ = make_service()
        create_base_case(service)
        with self.assertRaises(ConflictError):
            create_base_case(service)

    def test_unknown_dependency_rejected(self):
        service, _ = make_service()
        bad = [{"title": "要求", "measures": [
            {"measure_id": "X1", "kind": "设备整改", "title": "t", "deadline": "2026-10-10", "depends_on": ["X9"]},
        ]}]
        with self.assertRaises(ValidationError):
            service.create_case(case_id="C1", decision_ref="d", institution="i", requirements=bad, actor="a")

    def test_dependency_cycle_rejected(self):
        service, _ = make_service()
        bad = [{"title": "要求", "measures": [
            {"measure_id": "X1", "kind": "设备整改", "title": "t1", "deadline": "2026-10-10", "depends_on": ["X2"]},
            {"measure_id": "X2", "kind": "设备整改", "title": "t2", "deadline": "2026-10-10", "depends_on": ["X1"]},
        ]}]
        with self.assertRaises(ValidationError):
            service.create_case(case_id="C1", decision_ref="d", institution="i", requirements=bad, actor="a")

    def test_unknown_measure_kind_rejected(self):
        service, _ = make_service()
        bad = [{"title": "要求", "measures": [
            {"measure_id": "X1", "kind": "罚款", "title": "t", "deadline": "2026-10-10"},
        ]}]
        with self.assertRaises(ValidationError):
            service.create_case(case_id="C1", decision_ref="d", institution="i", requirements=bad, actor="a")


class EvidenceAndReviewTest(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        create_base_case(self.service)

    def test_evidence_versions_and_verifier_are_recorded(self):
        svc = self.service
        svc.submit_evidence("M1", submitted_by="机构合规员-李", batch="批次A",
                            items=[{"name": "停岗通知"}, {"name": "培训记录"}])
        review = svc.review_evidence("M1", verifier="复核专家-王", comment="培训记录不完整", item_results=[
            {"item": "停岗通知", "verdict": "通过"},
            {"item": "培训记录", "verdict": "驳回", "comment": "缺少签到表"},
        ])
        self.assertEqual(review["verdict"], "部分驳回")
        self.assertEqual(svc.measure_detail("M1")["status"], "已驳回")

        svc.submit_evidence("M1", submitted_by="机构合规员-李", batch="批次B",
                            items=[{"name": "培训记录"}])
        svc.review_evidence("M1", verifier="复核专家-王",
                            item_results=[{"item": "培训记录", "verdict": "通过"}])

        detail = svc.measure_detail("M1")
        self.assertEqual(detail["status"], "已通过")
        self.assertEqual([ev["version"] for ev in detail["evidence"]], [1, 2])
        self.assertEqual([ev["batch"] for ev in detail["evidence"]], ["批次A", "批次B"])
        self.assertEqual(detail["evidence"][0]["status"], "部分通过")
        self.assertEqual(detail["evidence"][1]["status"], "通过")
        self.assertEqual([rv["verifier"] for rv in detail["reviews"]], ["复核专家-王", "复核专家-王"])
        self.assertEqual(detail["reviews"][0]["item_results"][1]["comment"], "缺少签到表")

        kinds = [d["kind"] for d in svc.list_decisions(CASE_ID)]
        self.assertIn("部分驳回", kinds)
        self.assertIn("核验通过", kinds)
        partial = next(d for d in svc.list_decisions(CASE_ID) if d["kind"] == "部分驳回")
        self.assertEqual(partial["payload"]["rejected_items"], ["培训记录"])

    def test_full_rejection_marks_measure_rejected(self):
        svc = self.service
        svc.submit_evidence("M1", submitted_by="机构合规员-李", items=[{"name": "停岗通知"}])
        review = svc.review_evidence("M1", verifier="复核专家-王",
                                     item_results=[{"item": "停岗通知", "verdict": "驳回"}])
        self.assertEqual(review["verdict"], "驳回")
        self.assertEqual(svc.measure_detail("M1")["status"], "已驳回")
        self.assertIn("核验驳回", [d["kind"] for d in svc.list_decisions(CASE_ID)])

    def test_submit_while_pending_rejected(self):
        svc = self.service
        svc.submit_evidence("M1", submitted_by="机构合规员-李", items=[{"name": "停岗通知"}])
        with self.assertRaises(ConflictError):
            svc.submit_evidence("M1", submitted_by="机构合规员-李", items=[{"name": "补充"}])

    def test_review_without_pending_evidence_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.review_evidence("M1", verifier="复核专家-王",
                                         item_results=[{"item": "x", "verdict": "通过"}])

    def test_review_requires_full_item_coverage(self):
        svc = self.service
        svc.submit_evidence("M1", submitted_by="机构合规员-李",
                            items=[{"name": "停岗通知"}, {"name": "培训记录"}])
        with self.assertRaises(ValidationError):
            svc.review_evidence("M1", verifier="复核专家-王",
                                item_results=[{"item": "停岗通知", "verdict": "通过"}])
        with self.assertRaises(ValidationError):
            svc.review_evidence("M1", verifier="复核专家-王", item_results=[
                {"item": "停岗通知", "verdict": "通过"},
                {"item": "培训记录", "verdict": "通过"},
                {"item": "不存在的条目", "verdict": "通过"},
            ])

    def test_case_status_transitions(self):
        svc = self.service
        self.assertEqual(svc.case_detail(CASE_ID)["status"], "登记")
        svc.submit_evidence("M1", submitted_by="机构合规员-李", items=[{"name": "停岗通知"}])
        self.assertEqual(svc.case_detail(CASE_ID)["status"], "待核验")
        svc.review_evidence("M1", verifier="复核专家-王",
                            item_results=[{"item": "停岗通知", "verdict": "驳回"}])
        self.assertEqual(svc.case_detail(CASE_ID)["status"], "处置中")


class DependencyTest(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        create_base_case(self.service)

    def test_dependency_blocks_approval(self):
        svc = self.service
        svc.submit_evidence("M2", submitted_by="机构合规员-李", items=[{"name": "校准报告"}])
        with self.assertRaises(DependencyBlocked) as ctx:
            svc.review_evidence("M2", verifier="复核专家-王",
                                item_results=[{"item": "校准报告", "verdict": "通过"}])
        self.assertIn("M1", str(ctx.exception))
        pass_measure(svc, "M1")
        review = svc.review_evidence("M2", verifier="复核专家-王",
                                     item_results=[{"item": "校准报告", "verdict": "通过"}])
        self.assertEqual(review["verdict"], "通过")


class ClosureGateTest(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        create_base_case(self.service)

    def test_partial_completion_blocks_closure_with_reasons(self):
        svc = self.service
        pass_measure(svc, "M1")
        with self.assertRaises(ClosureBlocked) as ctx:
            svc.close_case(CASE_ID, actor="监管人员-陈")
        blockers = ctx.exception.blockers
        self.assertTrue(any("M2" in b for b in blockers))
        self.assertTrue(any("M4" in b for b in blockers))
        self.assertFalse(any("M3" in b for b in blockers), "可选措施不应阻塞结案")

    def test_closure_requires_all_required_measures_passed(self):
        svc = self.service
        pass_measure(svc, "M1")
        pass_measure(svc, "M2")
        pass_measure(svc, "M4")
        detail = svc.close_case(CASE_ID, actor="监管人员-陈", note="整改到位")
        self.assertEqual(detail["status"], "已决定")
        self.assertIn("结案", [d["kind"] for d in svc.list_decisions(CASE_ID)])
        with self.assertRaises(ConflictError):
            svc.close_case(CASE_ID, actor="监管人员-陈")

    def test_archive_after_closure(self):
        svc = self.service
        with self.assertRaises(ConflictError):
            svc.archive_case(CASE_ID, actor="监管人员-陈")
        for mid in ("M1", "M2", "M4"):
            pass_measure(svc, mid)
        svc.close_case(CASE_ID, actor="监管人员-陈")
        detail = svc.archive_case(CASE_ID, actor="监管人员-陈")
        self.assertEqual(detail["status"], "已归档")

    def test_closed_case_rejects_new_evidence(self):
        svc = self.service
        for mid in ("M1", "M2", "M4"):
            pass_measure(svc, mid)
        svc.close_case(CASE_ID, actor="监管人员-陈")
        with self.assertRaises(ConflictError):
            svc.submit_evidence("M3", submitted_by="机构合规员-李", items=[{"name": "台账"}])


class ExtensionTest(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        create_base_case(self.service)

    def test_extension_grant_updates_deadline_and_chain(self):
        svc = self.service
        ext = svc.request_extension("M4", requested_by="机构合规员-李",
                                    new_deadline="2026-10-25", reason="修订需征求一线意见")
        self.assertEqual(ext["status"], "待审批")
        decided = svc.decide_extension(ext["id"], approver="监管人员-陈", approve=True, note="同意")
        self.assertEqual(decided["status"], "已批准")
        self.assertEqual(svc.measure_detail("M4")["deadline"], "2026-10-25")
        record = next(d for d in svc.list_decisions(CASE_ID) if d["kind"] == "延期批准")
        self.assertEqual(record["payload"]["old_deadline"], "2026-10-20")
        self.assertEqual(record["payload"]["new_deadline"], "2026-10-25")

    def test_extension_denial_keeps_deadline(self):
        svc = self.service
        ext = svc.request_extension("M4", requested_by="机构合规员-李",
                                    new_deadline="2026-10-25", reason="人手不足")
        svc.decide_extension(ext["id"], approver="监管人员-陈", approve=False, note="理由不充分")
        self.assertEqual(svc.measure_detail("M4")["deadline"], "2026-10-20")
        self.assertIn("延期拒绝", [d["kind"] for d in svc.list_decisions(CASE_ID)])

    def test_extension_validation(self):
        svc = self.service
        with self.assertRaises(ValidationError):
            svc.request_extension("M4", requested_by="机构合规员-李",
                                  new_deadline="2026-10-15", reason="早于原期限")
        svc.request_extension("M4", requested_by="机构合规员-李",
                              new_deadline="2026-10-25", reason="第一次申请")
        with self.assertRaises(ConflictError):
            svc.request_extension("M4", requested_by="机构合规员-李",
                                  new_deadline="2026-10-28", reason="重复申请")

    def test_decided_extension_cannot_be_decided_again(self):
        svc = self.service
        ext = svc.request_extension("M4", requested_by="机构合规员-李",
                                    new_deadline="2026-10-25", reason="原因")
        svc.decide_extension(ext["id"], approver="监管人员-陈", approve=True)
        with self.assertRaises(ConflictError):
            svc.decide_extension(ext["id"], approver="监管人员-陈", approve=True)


class AlternativeTest(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        create_base_case(self.service)

    def test_accepted_alternative_supersedes_original(self):
        svc = self.service
        alt = svc.propose_alternative(
            "M4", proposed_by="机构合规员-李", reason="旧制度体系性失效，修订不可行",
            measure={"title": "废止旧制度并发布新版授权管理办法", "deadline": "2026-10-30"},
        )
        decided = svc.decide_alternative(alt["id"], approver="监管人员-陈", approve=True)
        replacement_id = decided["replacement_measure_id"]
        self.assertIsNotNone(replacement_id)

        original = svc.measure_detail("M4")
        self.assertEqual(original["status"], "已替代")
        self.assertEqual(original["superseded_by"], replacement_id)
        replacement = svc.measure_detail(replacement_id)
        self.assertEqual(replacement["kind"], "制度修订")
        self.assertTrue(replacement["required"])
        self.assertEqual(replacement["supersedes"], "M4")

        progress = svc.case_progress(CASE_ID)
        r2 = next(r for r in progress["requirements"] if r["requirement_id"] == "R2")
        self.assertEqual([m["id"] for m in r2["measures"]], [replacement_id])
        self.assertEqual([m["id"] for m in r2["superseded_measures"]], ["M4"])

        record = next(d for d in svc.list_decisions(CASE_ID) if d["kind"] == "替代措施采纳")
        self.assertEqual(record["payload"]["replacement_measure"], replacement_id)

        # 结案门槛跟随替代措施：原措施不再要求，新措施必须通过
        pass_measure(svc, "M1")
        pass_measure(svc, "M2")
        with self.assertRaises(ClosureBlocked):
            svc.close_case(CASE_ID, actor="监管人员-陈")
        pass_measure(svc, replacement_id)
        svc.close_case(CASE_ID, actor="监管人员-陈")

    def test_rejected_alternative_keeps_original(self):
        svc = self.service
        alt = svc.propose_alternative(
            "M4", proposed_by="机构合规员-李", reason="希望改为培训代替修订",
            measure={"title": "以专项培训替代制度修订", "deadline": "2026-10-25"},
        )
        svc.decide_alternative(alt["id"], approver="监管人员-陈", approve=False, note="不符合决定要求")
        self.assertEqual(svc.measure_detail("M4")["status"], "待启动")
        self.assertIn("替代措施否决", [d["kind"] for d in svc.list_decisions(CASE_ID)])

    def test_alternative_on_passed_measure_rejected(self):
        svc = self.service
        pass_measure(svc, "M1")
        with self.assertRaises(ConflictError):
            svc.propose_alternative("M1", proposed_by="机构合规员-李", reason="r",
                                    measure={"title": "t", "deadline": "2026-10-30"})

    def test_submit_to_superseded_measure_redirects(self):
        svc = self.service
        alt = svc.propose_alternative("M4", proposed_by="机构合规员-李", reason="r",
                                      measure={"title": "新制度", "deadline": "2026-10-30"})
        decided = svc.decide_alternative(alt["id"], approver="监管人员-陈", approve=True)
        with self.assertRaises(ConflictError) as ctx:
            svc.submit_evidence("M4", submitted_by="机构合规员-李", items=[{"name": "x"}])
        self.assertIn(decided["replacement_measure_id"], str(ctx.exception))


class ReopenTest(unittest.TestCase):
    def setUp(self):
        self.service, _ = make_service()
        create_base_case(self.service)

    def _close(self, svc):
        for mid in ("M1", "M2", "M4"):
            pass_measure(svc, mid)
        svc.close_case(CASE_ID, actor="监管人员-陈")

    def test_reopen_case_preserves_decision_chain(self):
        svc = self.service
        self._close(svc)
        detail = svc.reopen_case(CASE_ID, actor="监管人员-陈",
                                 reason="抽查发现 M1 证据造假", measure_ids=["M1"])
        self.assertEqual(detail["reopen_count"], 1)
        self.assertNotEqual(detail["status"], "已决定")
        self.assertEqual(svc.measure_detail("M1")["status"], "整改中")
        kinds = [d["kind"] for d in svc.list_decisions(CASE_ID)]
        self.assertIn("案件复开", kinds)
        self.assertIn("措施复开", kinds)
        record = next(d for d in svc.list_decisions(CASE_ID) if d["kind"] == "案件复开")
        self.assertEqual(record["reason"], "抽查发现 M1 证据造假")
        # 复开后必须重新通过才能再次结案
        with self.assertRaises(ClosureBlocked):
            svc.close_case(CASE_ID, actor="监管人员-陈")
        pass_measure(svc, "M1")
        svc.close_case(CASE_ID, actor="监管人员-陈")

    def test_reopen_requires_closed_case(self):
        with self.assertRaises(ConflictError):
            self.service.reopen_case(CASE_ID, actor="监管人员-陈", reason="尚未结案")

    def test_reopen_measure_auto_reopens_case(self):
        svc = self.service
        self._close(svc)
        svc.reopen_measure("M4", actor="复核专家-王", reason="制度发布版本有误")
        self.assertEqual(svc.measure_detail("M4")["status"], "整改中")
        case = svc.case_detail(CASE_ID)
        self.assertEqual(case["reopen_count"], 1)
        self.assertNotEqual(case["status"], "已决定")

    def test_reopen_non_passed_measure_rejected(self):
        with self.assertRaises(ConflictError):
            self.service.reopen_measure("M1", actor="监管人员-陈", reason="r")


class ReminderTest(unittest.TestCase):
    def setUp(self):
        self.service, self.holder = make_service()
        create_base_case(self.service)

    def test_reminders_are_idempotent_and_rerunnable(self):
        svc = self.service
        first = svc.run_reminders(due_within_days=7)
        self.assertEqual(first["emitted_count"], 3)  # M1(5天)、M2(7天)、M3(7天)，M4 还有 15 天
        self.assertEqual({e["kind"] for e in first["emitted"]}, {"临期提醒"})

        second = svc.run_reminders(due_within_days=7)
        self.assertEqual(second["emitted_count"], 0)
        self.assertEqual(second["duplicate_count"], 3)

    def test_overdue_reminder_after_deadline(self):
        svc = self.service
        svc.run_reminders(due_within_days=7)
        self.holder["now"] = START + timedelta(days=8)  # 2026-10-13
        result = svc.run_reminders(due_within_days=7)
        overdue = {e["measure_id"]: e for e in result["emitted"] if e["kind"] == "逾期提醒"}
        self.assertEqual(set(overdue), {"M1", "M2", "M3"})
        self.assertEqual(overdue["M1"]["overdue_days"], 3)
        self.assertTrue(any(e["measure_id"] == "M4" and e["kind"] == "临期提醒" for e in result["emitted"]))

    def test_passed_and_closed_measures_are_skipped(self):
        svc = self.service
        pass_measure(svc, "M1")
        result = svc.run_reminders(due_within_days=30)
        self.assertNotIn("M1", {e["measure_id"] for e in result["emitted"]})
        for mid in ("M2", "M4"):
            pass_measure(svc, mid)
        svc.close_case(CASE_ID, actor="监管人员-陈")
        result = svc.run_reminders(due_within_days=30)
        self.assertEqual(result["emitted_count"], 0)

    def test_extension_generates_new_reminder_key(self):
        svc = self.service
        svc.run_reminders(due_within_days=7)
        ext = svc.request_extension("M1", requested_by="机构合规员-李",
                                    new_deadline="2026-10-30", reason="培训排期")
        svc.decide_extension(ext["id"], approver="监管人员-陈", approve=True)
        result = svc.run_reminders(due_within_days=7)
        self.assertNotIn("M1", {e["measure_id"] for e in result["emitted"]})
        self.holder["now"] = START + timedelta(days=20)  # 2026-10-25
        result = svc.run_reminders(due_within_days=7)
        m1 = [e for e in result["emitted"] if e["measure_id"] == "M1"]
        self.assertEqual(len(m1), 1)
        self.assertEqual(m1[0]["deadline"], "2026-10-30")


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.service, self.holder = make_service()
        create_base_case(self.service)

    def test_progress_reports_completion_and_blockers(self):
        svc = self.service
        progress = svc.case_progress(CASE_ID)
        self.assertFalse(progress["can_close"])
        self.assertEqual(progress["overall"]["required_total"], 3)
        self.assertEqual(progress["overall"]["completion"], 0.0)
        r1 = next(r for r in progress["requirements"] if r["requirement_id"] == "R1")
        self.assertEqual(r1["required_total"], 2)
        self.assertTrue(any("M1" in b and "尚未启动" in b for b in r1["blockers"]))
        self.assertTrue(any("M2" in b and "M1" in b and "依赖" in b for b in r1["blockers"]))

        svc.submit_evidence("M1", submitted_by="机构合规员-李", items=[{"name": "停岗通知"}])
        progress = svc.case_progress(CASE_ID)
        self.assertTrue(any("待核验" in b for b in progress["blockers"]))

        svc.review_evidence("M1", verifier="复核专家-王",
                            item_results=[{"item": "停岗通知", "verdict": "驳回"}])
        progress = svc.case_progress(CASE_ID)
        self.assertTrue(any("驳回" in b for b in progress["blockers"]))

        pass_measure(svc, "M1")
        pass_measure(svc, "M2")
        pass_measure(svc, "M4")
        progress = svc.case_progress(CASE_ID)
        self.assertTrue(progress["can_close"])
        self.assertEqual(progress["overall"]["completion"], 1.0)
        self.assertEqual(progress["blockers"], [])

    def test_progress_marks_overdue(self):
        svc = self.service
        self.holder["now"] = START + timedelta(days=8)  # 2026-10-13
        progress = svc.case_progress(CASE_ID)
        self.assertTrue(any("M1" in b and "逾期 3 天" in b for b in progress["blockers"]))


if __name__ == "__main__":
    unittest.main()
