"""跑一轮期限提醒（幂等，可安全重跑，适合 cron 定时执行）。

用法：python3 tools/run_reminders.py --data data/store.json --soon-days 7
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from remediation.reminders import run_reminders
from remediation.store import JsonStore

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="整改期限提醒（幂等）")
    parser.add_argument("--data", default=str(ROOT / "data" / "store.json"), help="JSON 数据文件路径")
    parser.add_argument("--soon-days", type=int, default=7, help="临期提醒窗口（天）")
    args = parser.parse_args()

    result = run_reminders(JsonStore(args.data), soon_days=args.soon_days)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
