"""启动整改闭环后端服务。

用法：python3 tools/run_server.py [--host 127.0.0.1] [--port 8080] [--demo]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remediation import RemediationService, create_server


def seed_demo(service: RemediationService) -> None:
    """写入与领域契约样例一致的演示案件。"""
    service.create_case(
        case_id="0910033-009-A",
        decision_ref="检决字〔2026〕15号",
        institution="示例检测机构",
        actor="监管人员-陈",
        requirements=[
            {
                "requirement_id": "0910033-009-A-R01",
                "title": "人员与设备整改",
                "measures": [
                    {
                        "measure_id": "0910033-009-A-M01",
                        "kind": "人员停岗",
                        "title": "张某暂停执业并离岗培训",
                        "deadline": "2026-10-10",
                    },
                    {
                        "measure_id": "0910033-009-A-M02",
                        "kind": "设备整改",
                        "title": "3 号设备停用校准并复检",
                        "deadline": "2026-10-12",
                        "depends_on": ["0910033-009-A-M01"],
                    },
                ],
            },
            {
                "requirement_id": "0910033-009-A-R02",
                "title": "制度修订",
                "measures": [
                    {
                        "measure_id": "0910033-009-A-M03",
                        "kind": "制度修订",
                        "title": "修订授权管理制度并发布",
                        "deadline": "2026-10-20",
                    }
                ],
            },
        ],
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="整改措施闭环后端服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="写入演示案件 0910033-009-A")
    args = parser.parse_args()

    service = RemediationService()
    if args.demo:
        seed_demo(service)
    server = create_server(service, args.host, args.port)
    print(f"整改闭环服务已启动：http://{args.host}:{args.port}（Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
