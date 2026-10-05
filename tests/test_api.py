"""HTTP 接口端到端测试：真实端口上跑完整整改闭环。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remediation import RemediationService, create_server

CASE_ID = "0910033-009-B"

CASE_BODY = {
    "case_id": CASE_ID,
    "decision_ref": "检决字〔2026〕16号",
    "institution": "示例检测机构",
    "actor": "监管人员-陈",
    "requirements": [
        {
            "requirement_id": "R1",
            "title": "人员与设备整改",
            "measures": [
                {"measure_id": "M1", "kind": "人员停岗", "title": "张某暂停执业", "deadline": "2026-10-10"},
                {
                    "measure_id": "M2",
                    "kind": "设备整改",
                    "title": "3 号设备停用校准",
                    "deadline": "2026-10-12",
                    "depends_on": ["M1"],
                },
            ],
        },
        {
            "requirement_id": "R2",
            "title": "制度修订",
            "measures": [
                {"measure_id": "M3", "kind": "制度修订", "title": "修订授权管理制度", "deadline": "2026-10-20"},
            ],
        },
    ],
}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = RemediationService(clock=lambda: datetime(2026, 10, 5, tzinfo=timezone.utc))
        cls.server = create_server(cls.service, "127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_01_health(self):
        status, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_02_full_rectification_loop(self):
        # 检查决定下达：登记案件并拆分措施
        status, case = self.request("POST", "/cases", CASE_BODY)
        self.assertEqual(status, 201)
        self.assertEqual(case["status"], "登记")

        # 进度接口：每项要求的完成度与阻塞原因
        status, progress = self.request("GET", f"/cases/{CASE_ID}/progress")
        self.assertEqual(status, 200)
        self.assertFalse(progress["can_close"])
        self.assertEqual(progress["overall"]["required_total"], 3)
        self.assertTrue(any("M1" in b for b in progress["blockers"]))

        # 依赖未通过时不能核验通过
        self.request("POST", "/measures/M2/evidence",
                     {"submitted_by": "机构合规员-李", "batch": "批次1", "items": [{"name": "校准报告"}]})
        status, payload = self.request("POST", "/measures/M2/reviews",
                                       {"verifier": "复核专家-王", "items": [{"item": "校准报告", "verdict": "通过"}]})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "dependency_blocked")

        # 部分驳回：证据版本与复查结果留痕
        self.request("POST", "/measures/M1/evidence",
                     {"submitted_by": "机构合规员-李", "batch": "批次1",
                      "items": [{"name": "停岗通知"}, {"name": "培训记录"}]})
        status, review = self.request("POST", "/measures/M1/reviews",
                                      {"verifier": "复核专家-王", "comment": "培训记录不完整", "items": [
                                          {"item": "停岗通知", "verdict": "通过"},
                                          {"item": "培训记录", "verdict": "驳回", "comment": "缺少签到表"},
                                      ]})
        self.assertEqual(status, 201)
        self.assertEqual(review["verdict"], "部分驳回")

        status, progress = self.request("GET", f"/cases/{CASE_ID}/progress")
        self.assertTrue(any("部分驳回" in b for b in progress["blockers"]))

        # 重新提交第二版证据并通过
        self.request("POST", "/measures/M1/evidence",
                     {"submitted_by": "机构合规员-李", "batch": "批次2", "items": [{"name": "培训记录"}]})
        self.request("POST", "/measures/M1/reviews",
                     {"verifier": "复核专家-王", "items": [{"item": "培训记录", "verdict": "通过"}]})
        status, measure = self.request("GET", "/measures/M1")
        self.assertEqual(measure["status"], "已通过")
        self.assertEqual(len(measure["evidence"]), 2)

        # 延期：申请与批准进入决定链
        status, ext = self.request("POST", "/measures/M3/extensions",
                                   {"requested_by": "机构合规员-李", "new_deadline": "2026-10-25",
                                    "reason": "修订需征求意见"})
        self.assertEqual(status, 201)
        status, decided = self.request("POST", f"/extensions/{ext['id']}/decision",
                                       {"approver": "监管人员-陈", "approve": True})
        self.assertEqual(decided["status"], "已批准")

        # 部分措施未通过时结案被门槛拦截
        status, payload = self.request("POST", f"/cases/{CASE_ID}/close", {"actor": "监管人员-陈"})
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "closure_blocked")
        self.assertTrue(any("M2" in b for b in payload["blockers"]))
        self.assertTrue(any("M3" in b for b in payload["blockers"]))

        # 完成剩余必需措施
        self.request("POST", "/measures/M2/reviews",
                     {"verifier": "复核专家-王", "items": [{"item": "校准报告", "verdict": "通过"}]})
        self.request("POST", "/measures/M3/evidence",
                     {"submitted_by": "机构合规员-李", "items": [{"name": "制度发布文"}]})
        self.request("POST", "/measures/M3/reviews",
                     {"verifier": "复核专家-王", "items": [{"item": "制度发布文", "verdict": "通过"}]})

        status, progress = self.request("GET", f"/cases/{CASE_ID}/progress")
        self.assertTrue(progress["can_close"])
        self.assertEqual(progress["overall"]["completion"], 1.0)

        status, case = self.request("POST", f"/cases/{CASE_ID}/close", {"actor": "监管人员-陈", "note": "整改到位"})
        self.assertEqual(status, 200)
        self.assertEqual(case["status"], "已决定")

        # 决定链：下达、部分驳回、核验通过、延期批准、结案全部留痕
        status, chain = self.request("GET", f"/cases/{CASE_ID}/decisions")
        kinds = [d["kind"] for d in chain["decisions"]]
        for expected in ("检查决定下达", "部分驳回", "核验通过", "延期批准", "结案"):
            self.assertIn(expected, kinds)
        seqs = [d["seq"] for d in chain["decisions"]]
        self.assertEqual(seqs, sorted(seqs))

        # 复开保留决定链，且复开后需重新满足门槛
        status, case = self.request("POST", f"/cases/{CASE_ID}/reopen",
                                    {"actor": "监管人员-陈", "reason": "抽查复核", "measure_ids": ["M1"]})
        self.assertEqual(status, 200)
        self.assertEqual(case["reopen_count"], 1)
        status, payload = self.request("POST", f"/cases/{CASE_ID}/close", {"actor": "监管人员-陈"})
        self.assertEqual(status, 409)
        status, chain = self.request("GET", f"/cases/{CASE_ID}/decisions")
        self.assertIn("案件复开", [d["kind"] for d in chain["decisions"]])

    def test_03_reminder_job_is_idempotent(self):
        status, first = self.request("POST", "/jobs/reminders", {"date": "2026-10-08", "due_within_days": 7})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(first["emitted_count"], 1)
        status, second = self.request("POST", "/jobs/reminders", {"date": "2026-10-08", "due_within_days": 7})
        self.assertEqual(second["emitted_count"], 0)
        self.assertEqual(second["duplicate_count"], first["emitted_count"])

    def test_04_error_mapping(self):
        status, payload = self.request("GET", "/cases/NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "not_found")

        status, payload = self.request("POST", "/cases", {"case_id": "X"})
        self.assertEqual(status, 422)
        self.assertIn("缺少必填字段", payload["error"])

        status, payload = self.request("GET", "/nope")
        self.assertEqual(status, 404)

        req = urllib.request.Request(self.base + "/cases", data=b"{bad json", method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应当返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)


if __name__ == "__main__":
    unittest.main()
