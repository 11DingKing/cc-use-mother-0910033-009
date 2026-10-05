"""HTTP API 端到端测试：完整整改闭环流程。"""
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

from remediation import JsonStore, RemediationService
from remediation.api import serve

FIXED_NOW = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

REQUIREMENTS = [
    {
        "requirement_id": "REQ-1",
        "title": "人员与制度",
        "measures": [
            {"measure_id": "M1", "title": "涉事人员停岗", "kind": "人员停岗", "deadline": "2026-10-10"},
            {"measure_id": "M2", "title": "修订操作规程", "kind": "制度修订", "deadline": "2026-10-15"},
        ],
    }
]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        service = RemediationService(JsonStore(), clock=lambda: FIXED_NOW)
        cls.server = serve("127.0.0.1", 0, service)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        return self._request("POST", path, payload)

    def _get(self, path: str) -> tuple[int, dict]:
        return self._request("GET", path)

    def test_full_remediation_flow(self) -> None:
        status, case = self._post(
            "/cases",
            {
                "case_id": "C-API",
                "title": "API 流程案件",
                "requirements": REQUIREMENTS,
                "actor": "reg",
                "role": "监管人员",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(case["state"], "登记")

        status, case = self._post("/cases/C-API/issue", {"actor": "reg", "role": "监管人员"})
        self.assertEqual((status, case["state"]), (200, "待核验"))

        # 结案门槛：措施未通过时拦截
        status, error = self._post("/cases/C-API/close", {"decided_by": "reg", "role": "监管人员"})
        self.assertEqual(status, 409)
        self.assertIn("REQ-1", error["error"])

        status, view = self._get("/cases/C-API")
        self.assertEqual(status, 200)
        self.assertFalse(view["can_close"])
        self.assertEqual(view["completion"], 0.0)
        self.assertTrue(view["blocking"])

        # M1：驳回后重新提交，版本递增
        status, submitted = self._post(
            "/cases/C-API/measures/M1/evidence",
            {"items": ["停岗通知"], "submitted_by": "clerk", "role": "机构合规员"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(submitted["evidence"]["version"], 1)
        status, _ = self._post(
            "/cases/C-API/measures/M1/verify",
            {"version": 1, "verifier": "rev", "role": "复核专家", "result": "驳回", "comment": "缺签收"},
        )
        self.assertEqual(status, 200)
        status, resubmitted = self._post(
            "/cases/C-API/measures/M1/evidence",
            {"items": ["停岗通知", "签收记录"], "submitted_by": "clerk", "role": "机构合规员"},
        )
        self.assertEqual(resubmitted["evidence"]["version"], 2)
        status, _ = self._post(
            "/cases/C-API/measures/M1/verify",
            {"version": 2, "verifier": "rev", "role": "复核专家", "result": "通过"},
        )
        self.assertEqual(status, 200)

        # M2：部分驳回 → 重新提交 → 通过
        self._post(
            "/cases/C-API/measures/M2/evidence",
            {"items": ["制度草案", "签发页"], "submitted_by": "clerk", "role": "机构合规员"},
        )
        status, _ = self._post(
            "/cases/C-API/measures/M2/verify",
            {"version": 1, "verifier": "rev", "role": "复核专家", "result": "部分驳回", "rejected_items": ["签发页"]},
        )
        self.assertEqual(status, 200)
        self._post(
            "/cases/C-API/measures/M2/evidence",
            {"items": ["签发页"], "submitted_by": "clerk", "role": "机构合规员"},
        )
        self._post(
            "/cases/C-API/measures/M2/verify",
            {"version": 2, "verifier": "rev", "role": "复核专家", "result": "通过"},
        )

        # 完成度与阻塞原因
        status, view = self._get("/cases/C-API")
        self.assertTrue(view["can_close"])
        self.assertEqual(view["completion"], 1.0)
        self.assertEqual(view["requirements"][0]["blocking"], [])

        # 证据版本、核验人、复查结果可查
        status, detail = self._get("/cases/C-API/measures/M1")
        self.assertEqual([e["version"] for e in detail["evidence"]], [1, 2])
        self.assertEqual(detail["evidence"][1]["verification"]["verifier"], "rev")

        # 结案 → 归档 → 复开，决定链完整
        status, view = self._post("/cases/C-API/close", {"decided_by": "reg", "role": "监管人员"})
        self.assertEqual(view["state"], "已决定")
        status, view = self._post("/cases/C-API/archive", {"actor": "reg", "role": "监管人员"})
        self.assertEqual(view["state"], "已归档")
        status, view = self._post(
            "/cases/C-API/reopen", {"actor": "reg", "role": "监管人员", "reason": "抽查复核"}
        )
        self.assertEqual(view["state"], "处置中")

        status, chain = self._get("/cases/C-API/decisions")
        kinds = [d["kind"] for d in chain["decisions"]]
        for expected in ("登记", "决定下达", "证据提交", "核验驳回", "部分驳回", "核验通过", "结案", "归档", "复开"):
            self.assertIn(expected, kinds)

        # 提醒任务幂等
        status, first = self._post("/reminders/run", {"now": FIXED_NOW.isoformat()})
        self.assertEqual(status, 200)
        status, second = self._post("/reminders/run", {"now": FIXED_NOW.isoformat()})
        self.assertEqual(second["created_count"], 0)
        self.assertEqual(second["total"], first["total"])

    def test_not_found_and_validation(self) -> None:
        status, error = self._get("/cases/NOPE")
        self.assertEqual(status, 404)
        self.assertEqual(error["code"], "not_found")

        status, error = self._post("/cases", {"case_id": "C-BAD"})
        self.assertEqual(status, 400)
        self.assertIn("缺少字段", error["error"])

        status, error = self._get("/no/such/route")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
