"""整改闭环的领域模型。

状态取值与 domain/contract.json 中的契约保持一致：
案件状态使用 登记/处置中/待核验/已决定/已归档。
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any


class MeasureKind(str, Enum):
    """整改措施类型：机构分批提交证据的三类对象。"""

    PERSONNEL_SUSPENSION = "人员停岗"
    EQUIPMENT_RECTIFICATION = "设备整改"
    POLICY_REVISION = "制度修订"


class MeasureStatus(str, Enum):
    PENDING = "待启动"
    IN_PROGRESS = "整改中"
    SUBMITTED = "待核验"
    PASSED = "已通过"
    REJECTED = "已驳回"
    SUPERSEDED = "已替代"


class CaseStatus(str, Enum):
    REGISTERED = "登记"
    IN_PROGRESS = "处置中"
    PENDING_VERIFICATION = "待核验"
    CLOSED = "已决定"
    ARCHIVED = "已归档"


class EvidenceStatus(str, Enum):
    PENDING = "待核验"
    ACCEPTED = "通过"
    PARTIAL = "部分通过"
    REJECTED = "已驳回"
    SUPERSEDED = "已替代"


class ReviewVerdict(str, Enum):
    APPROVED = "通过"
    PARTIAL = "部分驳回"
    REJECTED = "驳回"


class ExtensionStatus(str, Enum):
    PENDING = "待审批"
    GRANTED = "已批准"
    DENIED = "已拒绝"


class AlternativeStatus(str, Enum):
    PENDING = "待审批"
    ACCEPTED = "已采纳"
    REJECTED = "已否决"


class DecisionKind(str, Enum):
    """决定链条目类型：延期、替代、部分驳回、复开等关键决定全部留痕。"""

    DECISION_ISSUED = "检查决定下达"
    REVIEW_APPROVED = "核验通过"
    REVIEW_PARTIAL = "部分驳回"
    REVIEW_REJECTED = "核验驳回"
    EXTENSION_GRANTED = "延期批准"
    EXTENSION_DENIED = "延期拒绝"
    ALTERNATIVE_ACCEPTED = "替代措施采纳"
    ALTERNATIVE_REJECTED = "替代措施否决"
    CASE_CLOSED = "结案"
    CASE_ARCHIVED = "案件归档"
    CASE_REOPENED = "案件复开"
    MEASURE_REOPENED = "措施复开"


ITEM_PASS = "通过"
ITEM_REJECT = "驳回"
ITEM_VERDICTS = (ITEM_PASS, ITEM_REJECT)

REMINDER_DUE_SOON = "临期提醒"
REMINDER_OVERDUE = "逾期提醒"


@dataclass
class Case:
    id: str
    decision_ref: str
    institution: str
    status: CaseStatus
    created_at: datetime
    created_by: str
    closed_at: datetime | None = None
    closed_by: str | None = None
    archived_at: datetime | None = None
    reopen_count: int = 0


@dataclass
class Requirement:
    id: str
    case_id: str
    title: str
    detail: str = ""


@dataclass
class EvidenceItem:
    name: str
    uri: str = ""
    note: str = ""


@dataclass
class EvidenceVersion:
    id: str
    measure_id: str
    version: int
    batch: str
    submitted_by: str
    submitted_at: datetime
    items: list[EvidenceItem] = field(default_factory=list)
    status: EvidenceStatus = EvidenceStatus.PENDING


@dataclass
class ItemVerdict:
    item: str
    verdict: str
    comment: str = ""


@dataclass
class ReviewRecord:
    id: str
    measure_id: str
    evidence_version_id: str
    verifier: str
    verdict: ReviewVerdict
    item_results: list[ItemVerdict] = field(default_factory=list)
    comment: str = ""
    reviewed_at: datetime | None = None


@dataclass
class Measure:
    id: str
    case_id: str
    requirement_id: str
    kind: MeasureKind
    title: str
    deadline: date
    detail: str = ""
    required: bool = True
    depends_on: list[str] = field(default_factory=list)
    status: MeasureStatus = MeasureStatus.PENDING
    created_at: datetime | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    evidence: list[EvidenceVersion] = field(default_factory=list)
    reviews: list[ReviewRecord] = field(default_factory=list)


@dataclass
class ExtensionRequest:
    id: str
    case_id: str
    measure_id: str
    requested_by: str
    reason: str
    old_deadline: date
    new_deadline: date
    requested_at: datetime
    status: ExtensionStatus = ExtensionStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str = ""


@dataclass
class AlternativeProposal:
    id: str
    case_id: str
    original_measure_id: str
    proposed_by: str
    reason: str
    title: str
    deadline: date
    proposed_at: datetime
    kind: MeasureKind | None = None
    detail: str = ""
    depends_on: list[str] | None = None
    status: AlternativeStatus = AlternativeStatus.PENDING
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str = ""
    replacement_measure_id: str | None = None


@dataclass
class DecisionRecord:
    seq: int
    case_id: str
    kind: DecisionKind
    actor: str
    reason: str
    payload: dict
    created_at: datetime


def to_jsonable(value: Any) -> Any:
    """把领域对象递归转换为可 JSON 序列化的结构。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: to_jsonable(item) for key, item in value.items()}
    return value
