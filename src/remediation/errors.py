"""整改闭环的领域错误。"""
from __future__ import annotations


class DomainError(Exception):
    """领域规则被违反。"""

    code = "domain_error"


class ValidationError(DomainError):
    """请求参数不合法。"""

    code = "validation"


class NotFoundError(DomainError):
    """目标对象不存在。"""

    code = "not_found"


class ConflictError(DomainError):
    """当前状态不允许该操作（含结案门槛拦截）。"""

    code = "conflict"
