"""启动整改闭环 HTTP 服务。

用法：python3 tools/serve.py --host 127.0.0.1 --port 8000 --data data/store.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remediation.api import build_service, serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="整改措施闭环 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=str(ROOT / "data" / "store.json"), help="JSON 数据文件路径")
    args = parser.parse_args()

    service = build_service(args.data)
    httpd = serve(args.host, args.port, service)
    print(f"整改闭环服务已启动：http://{args.host}:{args.port}（数据文件：{args.data}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()
