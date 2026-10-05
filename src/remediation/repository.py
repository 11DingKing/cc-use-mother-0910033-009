"""内存仓储：线程安全，接口与关系型实现保持可替换。"""
from __future__ import annotations

import threading
from collections import defaultdict

from .models import (
    AlternativeProposal,
    Case,
    DecisionRecord,
    ExtensionRequest,
    Measure,
    Requirement,
)


class InMemoryStore:
    """聚合所有实体与提醒幂等账本的线程安全容器。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cases: dict[str, Case] = {}
        self.requirements: dict[str, Requirement] = {}
        self.measures: dict[str, Measure] = {}
        self.extensions: dict[str, ExtensionRequest] = {}
        self.alternatives: dict[str, AlternativeProposal] = {}
        self.decisions: dict[str, list[DecisionRecord]] = defaultdict(list)
        # 期限提醒幂等账本：key 已发送过就不再重复发送，任务可安全重跑。
        self.reminder_ledger: set[str] = set()
        self.counters: dict[str, int] = defaultdict(int)

    def next_seq(self, key: str) -> int:
        self.counters[key] += 1
        return self.counters[key]
