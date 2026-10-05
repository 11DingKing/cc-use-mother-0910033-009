"""整改措施闭环的领域模型与状态常量。

状态取值与 domain/contract.json 中的 states / actors 保持一致。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ---- 案件状态（对应契约 states）----
CASE_STATE_REGISTERED = "登记"
CASE_STATE_PENDING = "待核验"
CASE_STATE_HANDLING = "处置中"
CASE_STATE_DECIDED = "已决定"
CASE_STATE_ARCHIVED = "已归档"
CASE_STATES = (
    CASE_STATE_REGISTERED,
    CASE_STATE_PENDING,
    CASE_STATE_HANDLING,
    CASE_STATE_DECIDED,
    CASE_STATE_ARCHIVED,
)
OPEN_CASE_STATES = (CASE_STATE_PENDING, CASE_STATE_HANDLING)
CLOSED_CASE_STATES = (CASE_STATE_DECIDED, CASE_STATE_ARCHIVED)

# ---- 措施状态 ----
MEASURE_BLOCKED = "待启动"      # 依赖未解除，不能提交
MEASURE_READY = "可提交"        # 可以提交证据
MEASURE_SUBMITTED = "待核验"    # 最新一版证据待核验
MEASURE_APPROVED = "已通过"     # 核验通过
MEASURE_REJECTED = "已驳回"     # 被驳回（含部分驳回），待重新提交
MEASURE_SUBSTITUTED = "已替代"  # 已被替代措施接管
MEASURE_STATUSES = (
    MEASURE_BLOCKED,
    MEASURE_READY,
    MEASURE_SUBMITTED,
    MEASURE_APPROVED,
    MEASURE_REJECTED,
    MEASURE_SUBSTITUTED,
)
ACTIVE_MEASURE_STATUSES = (MEASURE_BLOCKED, MEASURE_READY, MEASURE_SUBMITTED, MEASURE_REJECTED)

# ---- 核验结论（复查结果）----
RESULT_PASS = "通过"
RESULT_REJECT = "驳回"
RESULT_PARTIAL = "部分驳回"
VERIFY_RESULTS = (RESULT_PASS, RESULT_REJECT, RESULT_PARTIAL)

# ---- 措施类型 ----
MEASURE_KINDS = ("人员停岗", "设备整改", "制度修订", "其他")

# ---- 角色（对应契约 actors）----
ROLE_COMPLIANCE = "机构合规员"
ROLE_PRACTITIONER = "执业人员"
ROLE_REGULATOR = "监管人员"
ROLE_EXPERT = "复核专家"
ROLES = (ROLE_COMPLIANCE, ROLE_PRACTITIONER, ROLE_REGULATOR, ROLE_EXPERT)
SUBMITTER_ROLES = (ROLE_COMPLIANCE, ROLE_PRACTITIONER)
DECISION_ROLES = (ROLE_REGULATOR, ROLE_EXPERT)

# ---- 决定链条目类型 ----
DECIDE_REGISTER = "登记"
DECIDE_ISSUE = "决定下达"
DECIDE_SUBMIT = "证据提交"
DECIDE_PASS = "核验通过"
DECIDE_REJECT = "核验驳回"
DECIDE_PARTIAL = "部分驳回"
DECIDE_EXTEND = "延期"
DECIDE_SUBSTITUTE = "替代"
DECIDE_CLOSE = "结案"
DECIDE_REOPEN = "复开"
DECIDE_ARCHIVE = "归档"

# ---- 提醒类型 ----
REMINDER_DUE_SOON = "临期提醒"
REMINDER_OVERDUE = "逾期提醒"


@dataclass
class Verification:
    """一次证据核验：核验人 + 复查结果。"""

    verifier: str
    role: str
    result: str
    comment: str
    rejected_items: list[str]
    verified_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Verification":
        return cls(
            verifier=raw["verifier"],
            role=raw["role"],
            result=raw["result"],
            comment=raw.get("comment", ""),
            rejected_items=list(raw.get("rejected_items", [])),
            verified_at=raw["verified_at"],
        )


@dataclass
class Evidence:
    """一版证据提交；同一措施每次提交版本号递增。"""

    evidence_id: str
    version: int
    submitted_by: str
    role: str
    items: list[str]
    note: str
    submitted_at: str
    verification: Verification | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "version": self.version,
            "submitted_by": self.submitted_by,
            "role": self.role,
            "items": list(self.items),
            "note": self.note,
            "submitted_at": self.submitted_at,
            "verification": self.verification.to_dict() if self.verification else None,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Evidence":
        verification = raw.get("verification")
        return cls(
            evidence_id=raw["evidence_id"],
            version=raw["version"],
            submitted_by=raw["submitted_by"],
            role=raw["role"],
            items=list(raw.get("items", [])),
            note=raw.get("note", ""),
            submitted_at=raw["submitted_at"],
            verification=Verification.from_dict(verification) if verification else None,
        )


@dataclass
class Extension:
    """一次延期决定。"""

    new_deadline: str
    reason: str
    decided_by: str
    decided_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Extension":
        return cls(
            new_deadline=raw["new_deadline"],
            reason=raw["reason"],
            decided_by=raw["decided_by"],
            decided_at=raw["decided_at"],
        )


@dataclass
class Measure:
    """一条整改措施：带依赖、期限与证据版本序列。"""

    measure_id: str
    requirement_id: str
    title: str
    kind: str
    required: bool
    deadline: str
    depends_on: list[str]
    status: str
    created_at: str
    substituted_by: str | None = None
    alternative_for: str | None = None
    extensions: list[Extension] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def effective_deadline(self) -> str:
        """有效期限：最后一次延期后的期限，否则为原期限。"""
        return self.extensions[-1].new_deadline if self.extensions else self.deadline

    @property
    def latest_evidence(self) -> Evidence | None:
        return self.evidence[-1] if self.evidence else None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Measure":
        return cls(
            measure_id=raw["measure_id"],
            requirement_id=raw["requirement_id"],
            title=raw.get("title", ""),
            kind=raw["kind"],
            required=bool(raw.get("required", True)),
            deadline=raw["deadline"],
            depends_on=list(raw.get("depends_on", [])),
            status=raw["status"],
            created_at=raw["created_at"],
            substituted_by=raw.get("substituted_by"),
            alternative_for=raw.get("alternative_for"),
            extensions=[Extension.from_dict(e) for e in raw.get("extensions", [])],
            evidence=[Evidence.from_dict(e) for e in raw.get("evidence", [])],
        )


@dataclass
class Requirement:
    """一项整改要求，由若干措施组成。"""

    requirement_id: str
    title: str
    measure_ids: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Requirement":
        return cls(
            requirement_id=raw["requirement_id"],
            title=raw.get("title", ""),
            measure_ids=list(raw.get("measure_ids", [])),
        )


@dataclass
class Decision:
    """决定链上的一条记录（只增不改）。"""

    seq: int
    kind: str
    actor: str
    role: str
    detail: dict[str, Any]
    at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Decision":
        return cls(
            seq=raw["seq"],
            kind=raw["kind"],
            actor=raw["actor"],
            role=raw["role"],
            detail=dict(raw.get("detail", {})),
            at=raw["at"],
        )


@dataclass
class Case:
    """整改案件：检查决定下达后的完整整改闭环。"""

    case_id: str
    title: str
    state: str
    created_at: str
    requirements: list[Requirement]
    measures: dict[str, Measure]
    decisions: list[Decision]
    closed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "state": self.state,
            "created_at": self.created_at,
            "requirements": [r.to_dict() for r in self.requirements],
            "measures": {mid: m.to_dict() for mid, m in self.measures.items()},
            "decisions": [d.to_dict() for d in self.decisions],
            "closed_at": self.closed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Case":
        return cls(
            case_id=raw["case_id"],
            title=raw.get("title", ""),
            state=raw["state"],
            created_at=raw["created_at"],
            requirements=[Requirement.from_dict(r) for r in raw.get("requirements", [])],
            measures={m["measure_id"]: Measure.from_dict(m) for m in raw.get("measures", {}).values()},
            decisions=[Decision.from_dict(d) for d in raw.get("decisions", [])],
            closed_at=raw.get("closed_at"),
        )
