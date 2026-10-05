"""期限提醒任务：按确定键去重，可安全重跑（期限提醒幂等）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .models import (
    ACTIVE_MEASURE_STATUSES,
    CLOSED_CASE_STATES,
    REMINDER_DUE_SOON,
    REMINDER_OVERDUE,
    Case,
)
from .store import JsonStore


def reminder_key(case_id: str, measure_id: str, kind: str, deadline: str) -> str:
    """提醒的确定键：同一措施、同一类型、同一有效期限只提醒一次。"""
    return f"{case_id}:{measure_id}:{kind}:{deadline}"


def run_reminders(
    store: JsonStore,
    *,
    now: datetime | None = None,
    soon_days: int = 7,
) -> dict[str, Any]:
    """扫描未结案案件的未完成措施，生成临期/逾期提醒。

    提醒键由（案件、措施、类型、有效期限）唯一确定，重复运行不会产生重复记录；
    延期后有效期限变化，会按新期限生成新提醒，旧记录保留为历史。
    """
    moment = now or datetime.now(timezone.utc)
    today = moment.date()
    created: list[dict[str, Any]] = []
    with store.transaction() as data:
        reminders = data["reminders"]
        for raw in data["cases"].values():
            case = Case.from_dict(raw)
            if case.state in CLOSED_CASE_STATES:
                continue
            for measure in case.measures.values():
                if measure.status not in ACTIVE_MEASURE_STATUSES:
                    continue
                effective = measure.effective_deadline
                due = datetime.fromisoformat(effective).date()
                if due < today:
                    kind = REMINDER_OVERDUE
                elif (due - today).days <= soon_days:
                    kind = REMINDER_DUE_SOON
                else:
                    continue
                key = reminder_key(case.case_id, measure.measure_id, kind, effective)
                if key in reminders:
                    continue
                record = {
                    "key": key,
                    "case_id": case.case_id,
                    "measure_id": measure.measure_id,
                    "kind": kind,
                    "deadline": effective,
                    "created_at": moment.isoformat(timespec="seconds"),
                }
                reminders[key] = record
                created.append(record)
        return {
            "created": created,
            "created_count": len(created),
            "total": len(reminders),
        }
