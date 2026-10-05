"""整改闭环服务层测试：依赖、证据版本、结案门槛、延期/替代/复开。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remediation import ConflictError, JsonStore, RemediationService, ValidationError

FIXED_NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def make_service() -> RemediationService:
    return RemediationService(JsonStore(), clock=lambda: FIXED_NOW)


def standard_requirements() -> list[dict]:
    return [
        {
            "requirement_id": "REQ-1",
            "title": "人员与设备",
            "measures": [
                {"measure_id": "M-STOP", "title": "涉事人员停岗", "kind": "人员停岗", "deadline": "2026-10-10"},
                {
                    "measure_id": "M-EQUIP",
                    "title": "设备检修",
                    "kind": "设备整改",
                    "deadline": "2026-10-20",
                    "depends_on": ["M-STOP"],
                },
            ],
        },
        {
            "requirement_id": "REQ-2",
            "title": "制度修订",
            "measures": [
                {"measure_id": "M-POLICY", "title": "修订操作规程", "kind": "制度修订", "deadline": "2026-10-15"},
                {"measure_id": "M-OPT", "title": "建议性培训", "kind": "其他", "deadline": "2026-10-15", "required": False},
            ],
        },
    ]


def make_case(service: RemediationService, case_id: str = "CASE-1", requirements: list[dict] | None = None) -> str:
    service.create_case(
        case_id=case_id,
        title="专项检查",
        requirements=requirements or standard_requirements(),
        actor="reg",
        role="监管人员",
    )
    service.issue_decision(case_id, actor="reg", role="监管人员")
    return case_id


def submit_and_verify(
    service: RemediationService,
    case_id: str,
    measure_id: str,
    items: tuple[str, ...] = ("材料A",),
    result: str = "通过",
    **verify_kwargs,
) -> dict:
    submitted = service.submit_evidence(
        case_id, measure_id, items=list(items), submitted_by="clerk", role="机构合规员"
    )
    version = submitted["evidence"]["version"]
    return service.verify_evidence(
        case_id, measure_id, version=version, verifier="rev", role="复核专家", result=result, **verify_kwargs
    )


def measure_view(view: dict, measure_id: str) -> dict:
    for req in view["requirements"]:
        for measure in req["measures"]:
            if measure["measure_id"] == measure_id:
                return measure
    raise AssertionError(f"视图中找不到措施 {measure_id}")


class CreateCaseTest(unittest.TestCase):
    def test_duplicate_case_rejected(self) -> None:
        service = make_service()
        make_case(service)
        with self.assertRaises(ConflictError):
            make_case(service)

    def test_unknown_dependency_rejected(self) -> None:
        service = make_service()
        requirements = [
            {
                "requirement_id": "R",
                "title": "t",
                "measures": [
                    {"measure_id": "M1", "title": "t", "kind": "其他", "deadline": "2026-10-10", "depends_on": ["GHOST"]}
                ],
            }
        ]
        with self.assertRaises(ValidationError) as ctx:
            make_case(service, requirements=requirements)
        self.assertIn("GHOST", str(ctx.exception))

    def test_dependency_cycle_rejected(self) -> None:
        service = make_service()
        requirements = [
            {
                "requirement_id": "R",
                "title": "t",
                "measures": [
                    {"measure_id": "A", "title": "t", "kind": "其他", "deadline": "2026-10-10", "depends_on": ["B"]},
                    {"measure_id": "B", "title": "t", "kind": "其他", "deadline": "2026-10-10", "depends_on": ["A"]},
                ],
            }
        ]
        with self.assertRaises(ValidationError) as ctx:
            make_case(service, requirements=requirements)
        self.assertIn("环", str(ctx.exception))

    def test_duplicate_measure_id_rejected(self) -> None:
        service = make_service()
        requirements = [
            {
                "requirement_id": "R",
                "title": "t",
                "measures": [
                    {"measure_id": "M1", "title": "t", "kind": "其他", "deadline": "2026-10-10"},
                    {"measure_id": "M1", "title": "t", "kind": "其他", "deadline": "2026-10-11"},
                ],
            }
        ]
        with self.assertRaises(ValidationError):
            make_case(service, requirements=requirements)

    def test_invalid_kind_and_deadline_rejected(self) -> None:
        service = make_service()
        base = {"requirement_id": "R", "title": "t"}
        with self.assertRaises(ValidationError):
            make_case(service, requirements=[{**base, "measures": [{"measure_id": "M1", "kind": "非法", "deadline": "2026-10-10"}]}])
        with self.assertRaises(ValidationError):
            make_case(service, requirements=[{**base, "measures": [{"measure_id": "M1", "kind": "其他", "deadline": "10月1日"}]}])


class DependencyTest(unittest.TestCase):
    def test_blocked_until_dependency_approved(self) -> None:
        service = make_service()
        make_case(service)
        with self.assertRaises(ConflictError) as ctx:
            service.submit_evidence("CASE-1", "M-EQUIP", items=["检修报告"], submitted_by="clerk", role="机构合规员")
        self.assertIn("M-STOP", str(ctx.exception))
        self.assertEqual(measure_view(service.case_progress("CASE-1"), "M-EQUIP")["status"], "待启动")

        submit_and_verify(service, "CASE-1", "M-STOP")
        self.assertEqual(measure_view(service.case_progress("CASE-1"), "M-EQUIP")["status"], "可提交")
        submit_and_verify(service, "CASE-1", "M-EQUIP")
        self.assertEqual(measure_view(service.case_progress("CASE-1"), "M-EQUIP")["status"], "已通过")


class EvidenceTest(unittest.TestCase):
    def test_versions_increment_and_stale_rejected(self) -> None:
        service = make_service()
        make_case(service)
        first = service.submit_evidence("CASE-1", "M-STOP", items=["停岗通知"], submitted_by="clerk", role="机构合规员")
        self.assertEqual(first["evidence"]["version"], 1)
        with self.assertRaises(ConflictError):
            service.submit_evidence("CASE-1", "M-STOP", items=["补充"], submitted_by="clerk", role="机构合规员")

        service.verify_evidence("CASE-1", "M-STOP", version=1, verifier="rev", role="复核专家", result="驳回", comment="材料不全")
        second = service.submit_evidence("CASE-1", "M-STOP", items=["停岗通知", "签收记录"], submitted_by="clerk", role="机构合规员")
        self.assertEqual(second["evidence"]["version"], 2)

        with self.assertRaises(ConflictError) as ctx:
            service.verify_evidence("CASE-1", "M-STOP", version=1, verifier="rev", role="复核专家", result="通过")
        self.assertIn("已过期", str(ctx.exception))

        service.verify_evidence("CASE-1", "M-STOP", version=2, verifier="rev", role="复核专家", result="通过")
        detail = service.measure_detail("CASE-1", "M-STOP")
        self.assertEqual(detail["status"], "已通过")
        self.assertEqual([e["version"] for e in detail["evidence"]], [1, 2])
        self.assertEqual(detail["evidence"][0]["verification"]["result"], "驳回")
        self.assertEqual(detail["evidence"][1]["verification"]["verifier"], "rev")

    def test_partial_rejection_preserves_items(self) -> None:
        service = make_service()
        make_case(service)
        service.submit_evidence(
            "CASE-1", "M-POLICY", items=["制度草案", "培训记录", "签发页"], submitted_by="clerk", role="机构合规员"
        )
        with self.assertRaises(ValidationError):
            service.verify_evidence("CASE-1", "M-POLICY", version=1, verifier="rev", role="复核专家", result="部分驳回")
        with self.assertRaises(ValidationError):
            service.verify_evidence(
                "CASE-1", "M-POLICY", version=1, verifier="rev", role="复核专家",
                result="部分驳回", rejected_items=["不存在的条目"],
            )

        service.verify_evidence(
            "CASE-1", "M-POLICY", version=1, verifier="rev", role="复核专家",
            result="部分驳回", rejected_items=["签发页"], comment="缺少签发",
        )
        self.assertEqual(measure_view(service.case_progress("CASE-1"), "M-POLICY")["status"], "已驳回")
        partial = [d for d in service.decision_chain("CASE-1")["decisions"] if d["kind"] == "部分驳回"]
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0]["detail"]["rejected_items"], ["签发页"])

        submit_and_verify(service, "CASE-1", "M-POLICY", items=("签发页",))
        self.assertEqual(measure_view(service.case_progress("CASE-1"), "M-POLICY")["status"], "已通过")


class ClosureTest(unittest.TestCase):
    def test_cannot_close_until_all_required_approved(self) -> None:
        service = make_service()
        make_case(service)
        with self.assertRaises(ConflictError) as ctx:
            service.close_case("CASE-1", decided_by="reg", role="监管人员")
        self.assertIn("REQ-1", str(ctx.exception))

        submit_and_verify(service, "CASE-1", "M-STOP")
        with self.assertRaises(ConflictError):
            service.close_case("CASE-1", decided_by="reg", role="监管人员")

        submit_and_verify(service, "CASE-1", "M-EQUIP")
        submit_and_verify(service, "CASE-1", "M-POLICY")
        # 可选措施 M-OPT 未提交，不阻塞结案
        result = service.close_case("CASE-1", decided_by="reg", role="监管人员")
        self.assertEqual(result["state"], "已决定")
        self.assertIsNotNone(result["closed_at"])

        with self.assertRaises(ConflictError):
            service.submit_evidence("CASE-1", "M-OPT", items=["培训记录"], submitted_by="clerk", role="机构合规员")
        with self.assertRaises(ConflictError):
            service.close_case("CASE-1", decided_by="reg", role="监管人员")

    def test_archive_after_close(self) -> None:
        service = make_service()
        make_case(service)
        for mid in ("M-STOP", "M-EQUIP", "M-POLICY"):
            submit_and_verify(service, "CASE-1", mid)
        with self.assertRaises(ConflictError):
            service.archive_case("CASE-1", actor="reg", role="监管人员")
        service.close_case("CASE-1", decided_by="reg", role="监管人员")
        result = service.archive_case("CASE-1", actor="reg", role="监管人员")
        self.assertEqual(result["state"], "已归档")


class ExtensionTest(unittest.TestCase):
    def test_extension_updates_effective_deadline_and_chain(self) -> None:
        service = make_service()
        make_case(service)
        with self.assertRaises(ValidationError):
            service.extend_deadline(
                "CASE-1", "M-STOP", new_deadline="2026-10-09", reason="提前不算延期", decided_by="reg", role="监管人员"
            )
        service.extend_deadline(
            "CASE-1", "M-STOP", new_deadline="2026-11-01", reason="设备到货延迟", decided_by="reg", role="监管人员"
        )
        view = measure_view(service.case_progress("CASE-1"), "M-STOP")
        self.assertEqual(view["deadline"], "2026-10-10")
        self.assertEqual(view["effective_deadline"], "2026-11-01")

        extensions = [d for d in service.decision_chain("CASE-1")["decisions"] if d["kind"] == "延期"]
        self.assertEqual(len(extensions), 1)
        self.assertEqual(extensions[0]["detail"]["reason"], "设备到货延迟")

        with self.assertRaises(ValidationError):
            service.extend_deadline(
                "CASE-1", "M-STOP", new_deadline="2026-10-15", reason="必须更晚", decided_by="reg", role="监管人员"
            )


class SubstitutionTest(unittest.TestCase):
    def _make_case(self, service: RemediationService) -> None:
        requirements = [
            {
                "requirement_id": "REQ-1",
                "title": "设备",
                "measures": [
                    {"measure_id": "M-A", "title": "更换老旧设备", "kind": "设备整改", "deadline": "2026-10-20"},
                    {"measure_id": "M-C", "title": "验收", "kind": "其他", "deadline": "2026-10-25", "depends_on": ["M-A"]},
                ],
            }
        ]
        make_case(service, requirements=requirements)

    def test_substitution_transfers_obligation_and_dependencies(self) -> None:
        service = make_service()
        self._make_case(service)
        service.substitute_measure(
            "CASE-1",
            "M-A",
            alternative={"measure_id": "M-B", "title": "加装临时防护", "kind": "设备整改", "deadline": "2026-10-18"},
            reason="采购周期过长",
            decided_by="reg",
            role="监管人员",
        )
        view = service.case_progress("CASE-1")
        self.assertEqual(measure_view(view, "M-A")["status"], "已替代")
        self.assertEqual(measure_view(view, "M-A")["substituted_by"], "M-B")
        self.assertEqual(measure_view(view, "M-B")["alternative_for"], "M-A")
        self.assertEqual(measure_view(view, "M-C")["depends_on"], ["M-B"])
        # 被替代措施不再计入完成度
        req = view["requirements"][0]
        self.assertEqual(req["total_required"], 2)

        with self.assertRaises(ConflictError):
            service.submit_evidence("CASE-1", "M-A", items=["x"], submitted_by="clerk", role="机构合规员")

        submit_and_verify(service, "CASE-1", "M-B")
        submit_and_verify(service, "CASE-1", "M-C")
        result = service.close_case("CASE-1", decided_by="reg", role="监管人员")
        self.assertEqual(result["state"], "已决定")

        chain = service.decision_chain("CASE-1")["decisions"]
        substituted = [d for d in chain if d["kind"] == "替代"]
        self.assertEqual(substituted[0]["detail"], {"measure_id": "M-A", "alternative": "M-B", "reason": "采购周期过长"})

    def test_approved_measure_cannot_be_substituted(self) -> None:
        service = make_service()
        self._make_case(service)
        submit_and_verify(service, "CASE-1", "M-A")
        with self.assertRaises(ConflictError):
            service.substitute_measure(
                "CASE-1", "M-A",
                alternative={"measure_id": "M-B", "title": "t", "kind": "其他", "deadline": "2026-10-18"},
                reason="r", decided_by="reg", role="监管人员",
            )


class ReopenTest(unittest.TestCase):
    def test_reopen_restores_handling_and_preserves_chain(self) -> None:
        service = make_service()
        make_case(service)
        for mid in ("M-STOP", "M-EQUIP", "M-POLICY"):
            submit_and_verify(service, "CASE-1", mid)
        service.close_case("CASE-1", decided_by="reg", role="监管人员")
        service.archive_case("CASE-1", actor="reg", role="监管人员")

        with self.assertRaises(ValidationError):
            service.reopen_case("CASE-1", reason="", actor="reg", role="监管人员")
        result = service.reopen_case(
            "CASE-1", reason="复查发现停岗未落实", actor="reg", role="监管人员", reset_measure_ids=["M-STOP"]
        )
        self.assertEqual(result["state"], "处置中")
        self.assertIsNone(result["closed_at"])
        stop = measure_view(result, "M-STOP")
        self.assertEqual(stop["status"], "可提交")
        self.assertEqual(stop["evidence_count"], 1)  # 历史证据保留

        kinds = [d["kind"] for d in service.decision_chain("CASE-1")["decisions"]]
        self.assertEqual(kinds[-1], "复开")
        self.assertIn("结案", kinds)
        self.assertIn("归档", kinds)

        resubmitted = service.submit_evidence(
            "CASE-1", "M-STOP", items=["新停岗证明"], submitted_by="clerk", role="机构合规员"
        )
        self.assertEqual(resubmitted["evidence"]["version"], 2)

        with self.assertRaises(ConflictError):
            service.reopen_case("CASE-1", reason="重复复开", actor="reg", role="监管人员")


class RoleTest(unittest.TestCase):
    def test_roles_enforced(self) -> None:
        service = make_service()
        make_case(service)
        with self.assertRaises(ValidationError):
            service.submit_evidence("CASE-1", "M-STOP", items=["a"], submitted_by="reg", role="监管人员")
        service.submit_evidence("CASE-1", "M-STOP", items=["a"], submitted_by="clerk", role="机构合规员")
        with self.assertRaises(ValidationError):
            service.verify_evidence("CASE-1", "M-STOP", version=1, verifier="clerk", role="机构合规员", result="通过")
        with self.assertRaises(ValidationError):
            service.close_case("CASE-1", decided_by="clerk", role="执业人员")
        with self.assertRaises(ValidationError):
            service.extend_deadline(
                "CASE-1", "M-STOP", new_deadline="2026-11-01", reason="r", decided_by="clerk", role="执业人员"
            )
        with self.assertRaises(ValidationError):
            service.issue_decision("CASE-1", actor="clerk", role="机构合规员")


class ProgressViewTest(unittest.TestCase):
    def test_blocking_reasons_and_completion(self) -> None:
        service = make_service()
        requirements = [
            {
                "requirement_id": "REQ-1",
                "title": "t",
                "measures": [
                    {"measure_id": "M-LATE", "title": "逾期措施", "kind": "其他", "deadline": "2026-10-01"},
                    {"measure_id": "M-OK", "title": "正常措施", "kind": "其他", "deadline": "2026-12-01"},
                ],
            }
        ]
        make_case(service, requirements=requirements)
        view = service.case_progress("CASE-1")
        req = view["requirements"][0]
        self.assertEqual(req["completion"], 0.0)
        self.assertFalse(view["can_close"])
        blocking = {b["measure_id"]: b["reasons"] for b in req["blocking"]}
        self.assertIn("尚未提交证据", blocking["M-LATE"][0])
        self.assertTrue(any("已逾期" in reason for reason in blocking["M-LATE"]))
        self.assertEqual(blocking["M-OK"], ["尚未提交证据"])

        service.submit_evidence("CASE-1", "M-OK", items=["a"], submitted_by="clerk", role="机构合规员")
        view = service.case_progress("CASE-1")
        blocking = {b["measure_id"]: b["reasons"] for b in view["requirements"][0]["blocking"]}
        self.assertEqual(blocking["M-OK"], ["证据 v1 待核验"])

        submit_and_verify(service, "CASE-1", "M-LATE")
        service.verify_evidence("CASE-1", "M-OK", version=1, verifier="rev", role="复核专家", result="通过")
        view = service.case_progress("CASE-1")
        self.assertTrue(view["can_close"])
        self.assertEqual(view["completion"], 1.0)
        self.assertEqual(view["blocking"], [])


if __name__ == "__main__":
    unittest.main()
