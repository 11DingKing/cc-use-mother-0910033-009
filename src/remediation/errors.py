"""整改闭环的领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """领域错误基类，携带 HTTP 状态码与机器可读代码。"""

    status = 400
    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ValidationError(DomainError):
    status = 422
    code = "validation_error"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


class DependencyBlocked(ConflictError):
    """依赖措施未通过，当前操作被阻塞。"""

    code = "dependency_blocked"


class ClosureBlocked(ConflictError):
    """结案完整性门槛未满足。"""

    code = "closure_blocked"

    def __init__(self, blockers: list[str]) -> None:
        super().__init__("存在未完成的必需整改措施，无法结案", details={"blockers": blockers})
        self.blockers = blockers
