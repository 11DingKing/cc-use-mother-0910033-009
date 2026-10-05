"""期限提醒幂等性测试：可安全重跑，延期后按新期限生成新键。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remediation import JsonStore, RemediationService
from remediation.reminders import run_reminders

FIXED_NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)


def make_store_with_case() -> tuple[JsonStore, RemediationService]:
    store = JsonStore()
    service = RemediationService(store, clock=lambda: FIXED_NOW)
    requirements = [
        {
            "requirement_id": "REQ-1",
            "title": "t",
            "measures": [
                {"measure_id": "M-OVERDUE", "title": "已逾期", "kind": "其他", "deadline": "2026-10-01"},
                {"measure_id": "M-SOON", "title": "临期", "kind": "其他", "deadline": "2026-10-09"},
                {"measure_id": "M-FAR", "title": "远期", "kind": "其他", "deadline": "2026-12-01"},
                {"measure_id": "M-DONE", "title": "已完成", "kind": "其他", "deadline": "2026-10-01"},
            ],
        }
    ]
    service.create_case(case_id="CASE-1", title="t", requirements=requirements, actor="reg", role="监管人员")
    service.issue_decision("CASE-1", actor="reg", role="监管人员")
    service.submit_evidence("CASE-1", "M-DONE", items=["a"], submitted_by="clerk", role="机构合规员")
    service.verify_evidence("CASE-1", "M-DONE", version=1, verifier="rev", role="复核专家", result="通过")
    return store, service


class ReminderTest(unittest.TestCase):
    def test_idempotent_rerun(self) -> None:
        store, _ = make_store_with_case()
        first = run_reminders(store, now=FIXED_NOW)
        kinds = {record["measure_id"]: record["kind"] for record in first["created"]}
        self.assertEqual(kinds, {"M-OVERDUE": "逾期提醒", "M-SOON": "临期提醒"})

        second = run_reminders(store, now=FIXED_NOW)
        self.assertEqual(second["created_count"], 0)
        self.assertEqual(second["total"], first["total"])

    def test_extension_generates_new_reminder_key(self) -> None:
        store, service = make_store_with_case()
        run_reminders(store, now=FIXED_NOW)
        service.extend_deadline(
            "CASE-1", "M-OVERDUE", new_deadline="2026-10-08", reason="宽限三天", decided_by="reg", role="监管人员"
        )
        result = run_reminders(store, now=FIXED_NOW)
        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["created"][0]["kind"], "临期提醒")
        self.assertEqual(result["created"][0]["deadline"], "2026-10-08")
        # 旧键保留为历史，再跑仍然幂等
        self.assertEqual(result["total"], 3)
        self.assertEqual(run_reminders(store, now=FIXED_NOW)["created_count"], 0)

    def test_closed_case_gets_no_reminders(self) -> None:
        store, service = make_store_with_case()
        for mid in ("M-OVERDUE", "M-SOON", "M-FAR"):
            service.submit_evidence("CASE-1", mid, items=["a"], submitted_by="clerk", role="机构合规员")
            service.verify_evidence("CASE-1", mid, version=1, verifier="rev", role="复核专家", result="通过")
        service.close_case("CASE-1", decided_by="reg", role="监管人员")
        result = run_reminders(store, now=FIXED_NOW)
        self.assertEqual(result["created_count"], 0)
        self.assertEqual(result["total"], 0)


if __name__ == "__main__":
    unittest.main()
