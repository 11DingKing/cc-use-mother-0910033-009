"""整改闭环核心服务。

实现契约中的四条关键不变量：
- 整改措施依赖：措施可声明依赖，依赖未通过前不得核验通过；
- 证据版本核验：证据按版本留存，记录核验人与逐条复查结果；
- 结案完整性门槛：全部必需措施通过才允许结案；
- 期限提醒幂等：提醒以 (措施, 类型, 期限) 为键去重，任务可安全重跑。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Callable

from .errors import (
    ClosureBlocked,
    ConflictError,
    DependencyBlocked,
    NotFoundError,
    ValidationError,
)
from .models import (
    ITEM_REJECT,
    ITEM_VERDICTS,
    REMINDER_DUE_SOON,
    REMINDER_OVERDUE,
    AlternativeProposal,
    AlternativeStatus,
    Case,
    CaseStatus,
    DecisionKind,
    DecisionRecord,
    EvidenceItem,
    EvidenceStatus,
    EvidenceVersion,
    ExtensionRequest,
    ExtensionStatus,
    ItemVerdict,
    Measure,
    MeasureKind,
    MeasureStatus,
    Requirement,
    ReviewRecord,
    ReviewVerdict,
    to_jsonable,
)

MEASURE_KINDS_TEXT = "、".join(kind.value for kind in MeasureKind)


def _parse_date(value: object, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValidationError(f"{field_name} 必须是 ISO 日期（如 2026-10-20）")


def _parse_kind(value: object) -> MeasureKind:
    try:
        return MeasureKind(value)
    except ValueError:
        raise ValidationError(f"未知措施类型：{value}，可选：{MEASURE_KINDS_TEXT}") from None


class RemediationService:
    """整改闭环应用服务，所有写操作都在锁内完成。"""

    def __init__(
        self,
        store: "InMemoryStore | None" = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        from .repository import InMemoryStore

        self.store = store or InMemoryStore()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _today(self) -> date:
        return self._clock().date()

    def _get_case(self, case_id: str) -> Case:
        case = self.store.cases.get(case_id)
        if case is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return case

    def _get_measure(self, measure_id: str) -> Measure:
        measure = self.store.measures.get(measure_id)
        if measure is None:
            raise NotFoundError(f"措施不存在：{measure_id}")
        return measure

    def _get_extension(self, extension_id: str) -> ExtensionRequest:
        ext = self.store.extensions.get(extension_id)
        if ext is None:
            raise NotFoundError(f"延期申请不存在：{extension_id}")
        return ext

    def _get_alternative(self, alt_id: str) -> AlternativeProposal:
        alt = self.store.alternatives.get(alt_id)
        if alt is None:
            raise NotFoundError(f"替代措施申请不存在：{alt_id}")
        return alt

    def _case_measures(self, case_id: str) -> list[Measure]:
        return [m for m in self.store.measures.values() if m.case_id == case_id]

    def _case_requirements(self, case_id: str) -> list[Requirement]:
        return [r for r in self.store.requirements.values() if r.case_id == case_id]

    def _record(
        self,
        case_id: str,
        kind: DecisionKind,
        actor: str,
        *,
        reason: str = "",
        payload: dict | None = None,
    ) -> DecisionRecord:
        chain = self.store.decisions[case_id]
        record = DecisionRecord(
            seq=len(chain) + 1,
            case_id=case_id,
            kind=kind,
            actor=actor,
            reason=reason,
            payload=payload or {},
            created_at=self._now(),
        )
        chain.append(record)
        return record

    def _assert_case_open(self, case: Case) -> None:
        if case.status in (CaseStatus.CLOSED, CaseStatus.ARCHIVED):
            raise ConflictError(f"案件 {case.id} 已结案，请先复开再操作")

    def _refresh_case_status(self, case: Case) -> None:
        """案件状态由措施状态推导，保证与契约状态机一致。"""
        if case.archived_at is not None:
            case.status = CaseStatus.ARCHIVED
            return
        if case.closed_at is not None:
            case.status = CaseStatus.CLOSED
            return
        measures = [m for m in self._case_measures(case.id) if m.status != MeasureStatus.SUPERSEDED]
        if any(m.status == MeasureStatus.SUBMITTED for m in measures):
            case.status = CaseStatus.PENDING_VERIFICATION
        elif any(m.status in (MeasureStatus.IN_PROGRESS, MeasureStatus.REJECTED) or m.evidence for m in measures):
            case.status = CaseStatus.IN_PROGRESS
        else:
            case.status = CaseStatus.REGISTERED

    def _resolve_effective(self, measure_id: str) -> Measure:
        """沿替代链找到当前生效的措施。"""
        seen: set[str] = set()
        measure = self._get_measure(measure_id)
        while measure.superseded_by:
            if measure.id in seen:
                raise ConflictError(f"措施 {measure_id} 的替代链存在环")
            seen.add(measure.id)
            measure = self._get_measure(measure.superseded_by)
        return measure

    def _unpassed_dependencies(self, measure: Measure) -> list[Measure]:
        return [
            self._resolve_effective(dep)
            for dep in measure.depends_on
            if self._resolve_effective(dep).status != MeasureStatus.PASSED
        ]

    @staticmethod
    def _assert_acyclic(edges: dict[str, list[str]]) -> None:
        color: dict[str, int] = {}

        def visit(node: str, trail: list[str]) -> None:
            color[node] = 1
            for nxt in edges.get(node, []):
                if nxt not in edges:
                    continue
                if color.get(nxt) == 1:
                    raise ValidationError("措施依赖存在环：" + " → ".join(trail + [node, nxt]))
                if color.get(nxt, 0) == 0:
                    visit(nxt, trail + [node])
            color[node] = 2

        for node in edges:
            if color.get(node, 0) == 0:
                visit(node, [])

    def _next_measure_id(self, case_id: str) -> str:
        while True:
            seq = self.store.next_seq(f"measure:{case_id}")
            candidate = f"{case_id}-M{seq:02d}"
            if candidate not in self.store.measures:
                return candidate

    # ------------------------------------------------------------------
    # 案件登记
    # ------------------------------------------------------------------
    def create_case(
        self,
        *,
        case_id: str,
        decision_ref: str,
        institution: str,
        requirements: list[dict],
        actor: str,
    ) -> dict:
        """检查决定下达：登记案件，并把整改要求拆成带依赖和期限的措施。"""
        with self.store.lock:
            if case_id in self.store.cases:
                raise ConflictError(f"案件编号已存在：{case_id}")
            if not isinstance(requirements, list) or not requirements:
                raise ValidationError("至少需要一个整改要求")

            now = self._now()
            case = Case(
                id=case_id,
                decision_ref=decision_ref,
                institution=institution,
                status=CaseStatus.REGISTERED,
                created_at=now,
                created_by=actor,
            )
            req_objs: list[Requirement] = []
            measure_objs: list[Measure] = []
            for index, req in enumerate(requirements, 1):
                if not isinstance(req, dict):
                    raise ValidationError("整改要求必须是对象")
                rid = req.get("requirement_id") or f"{case_id}-R{index:02d}"
                title = (req.get("title") or "").strip()
                if not title:
                    raise ValidationError(f"整改要求 {rid} 缺少标题")
                if rid in self.store.requirements:
                    raise ConflictError(f"整改要求编号已存在：{rid}")
                measures = req.get("measures")
                if not isinstance(measures, list) or not measures:
                    raise ValidationError(f"整改要求 {rid} 至少包含一项措施")
                req_objs.append(Requirement(id=rid, case_id=case_id, title=title, detail=req.get("detail") or ""))
                for raw in measures:
                    if not isinstance(raw, dict):
                        raise ValidationError("整改措施必须是对象")
                    mid = raw.get("measure_id") or self._next_measure_id(case_id)
                    if mid in self.store.measures or any(m.id == mid for m in measure_objs):
                        raise ConflictError(f"措施编号已存在：{mid}")
                    mtitle = (raw.get("title") or "").strip()
                    if not mtitle:
                        raise ValidationError(f"措施 {mid} 缺少标题")
                    depends_on = raw.get("depends_on") or []
                    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
                        raise ValidationError(f"措施 {mid} 的 depends_on 必须是字符串列表")
                    measure_objs.append(
                        Measure(
                            id=mid,
                            case_id=case_id,
                            requirement_id=rid,
                            kind=_parse_kind(raw.get("kind")),
                            title=mtitle,
                            detail=raw.get("detail") or "",
                            required=bool(raw.get("required", True)),
                            deadline=_parse_date(raw.get("deadline"), f"措施 {mid} 的期限"),
                            depends_on=list(depends_on),
                            created_at=now,
                        )
                    )

            ids = {m.id for m in measure_objs}
            for measure in measure_objs:
                for dep in measure.depends_on:
                    if dep not in ids:
                        raise ValidationError(f"措施 {measure.id} 依赖了不存在的措施：{dep}")
            self._assert_acyclic({m.id: list(m.depends_on) for m in measure_objs})

            self.store.cases[case_id] = case
            for req in req_objs:
                self.store.requirements[req.id] = req
            for measure in measure_objs:
                self.store.measures[measure.id] = measure
            self._record(
                case_id,
                DecisionKind.DECISION_ISSUED,
                actor,
                payload={
                    "decision_ref": decision_ref,
                    "institution": institution,
                    "requirement_count": len(req_objs),
                    "measure_count": len(measure_objs),
                },
            )
            return self.case_detail(case_id)

    # ------------------------------------------------------------------
    # 证据提交与核验
    # ------------------------------------------------------------------
    def submit_evidence(self, measure_id: str, *, submitted_by: str, batch: str = "", items: list[dict]) -> dict:
        """机构分批提交证据，每次提交生成一个新的证据版本。"""
        with self.store.lock:
            measure = self._get_measure(measure_id)
            case = self._get_case(measure.case_id)
            self._assert_case_open(case)
            if measure.status == MeasureStatus.SUPERSEDED:
                raise ConflictError(f"措施 {measure_id} 已被替代，请向替代措施 {measure.superseded_by} 提交证据")
            if measure.status == MeasureStatus.PASSED:
                raise ConflictError(f"措施 {measure_id} 已通过，如需补交证据请先复开")
            if any(ev.status == EvidenceStatus.PENDING for ev in measure.evidence):
                raise ConflictError(f"措施 {measure_id} 存在待核验的证据版本，请先完成核验")
            if not isinstance(items, list) or not items:
                raise ValidationError("证据条目不能为空")

            norm_items: list[EvidenceItem] = []
            seen: set[str] = set()
            for raw in items:
                if not isinstance(raw, dict):
                    raise ValidationError("证据条目必须是对象")
                name = (raw.get("name") or "").strip()
                if not name:
                    raise ValidationError("证据条目缺少 name")
                if name in seen:
                    raise ValidationError(f"证据条目重复：{name}")
                seen.add(name)
                norm_items.append(EvidenceItem(name=name, uri=raw.get("uri") or "", note=raw.get("note") or ""))

            version = len(measure.evidence) + 1
            evidence = EvidenceVersion(
                id=f"{measure_id}-EV{version:02d}",
                measure_id=measure_id,
                version=version,
                batch=batch or f"批次{version}",
                submitted_by=submitted_by,
                submitted_at=self._now(),
                items=norm_items,
            )
            measure.evidence.append(evidence)
            measure.status = MeasureStatus.SUBMITTED
            self._refresh_case_status(case)
            return to_jsonable(evidence)

    def review_evidence(
        self,
        measure_id: str,
        *,
        verifier: str,
        item_results: list[dict],
        comment: str = "",
    ) -> dict:
        """核验人逐条复查最新证据版本：通过 / 部分驳回 / 驳回。"""
        with self.store.lock:
            measure = self._get_measure(measure_id)
            case = self._get_case(measure.case_id)
            self._assert_case_open(case)
            if measure.status == MeasureStatus.SUPERSEDED:
                raise ConflictError(f"措施 {measure_id} 已被替代，请核验替代措施 {measure.superseded_by}")
            pending = next((ev for ev in reversed(measure.evidence) if ev.status == EvidenceStatus.PENDING), None)
            if pending is None:
                raise ConflictError(f"措施 {measure_id} 没有待核验的证据版本")
            if not isinstance(item_results, list) or not item_results:
                raise ValidationError("核验结论不能为空")

            valid_names = {item.name for item in pending.items}
            item_verdicts: list[ItemVerdict] = []
            seen: set[str] = set()
            for raw in item_results:
                if not isinstance(raw, dict):
                    raise ValidationError("核验结论必须是对象")
                name = raw.get("item")
                verdict = raw.get("verdict")
                if name not in valid_names:
                    raise ValidationError(f"证据版本 {pending.id} 中不存在条目：{name}")
                if name in seen:
                    raise ValidationError(f"核验结论重复：{name}")
                seen.add(name)
                if verdict not in ITEM_VERDICTS:
                    raise ValidationError(f"条目结论只能是 {' 或 '.join(ITEM_VERDICTS)}")
                item_verdicts.append(
                    ItemVerdict(item=name, verdict=verdict, comment=raw.get("comment") or "")
                )
            missing = valid_names - seen
            if missing:
                raise ValidationError("以下证据条目缺少核验结论：" + "、".join(sorted(missing)))

            rejected = [iv for iv in item_verdicts if iv.verdict == ITEM_REJECT]
            if not rejected:
                overall = ReviewVerdict.APPROVED
            elif len(rejected) == len(item_verdicts):
                overall = ReviewVerdict.REJECTED
            else:
                overall = ReviewVerdict.PARTIAL

            if overall == ReviewVerdict.APPROVED:
                blocked_by = self._unpassed_dependencies(measure)
                if blocked_by:
                    blockers = [
                        f"措施 {dep.id}「{dep.title}」未通过（当前状态：{dep.status.value}）" for dep in blocked_by
                    ]
                    raise DependencyBlocked(
                        f"措施 {measure_id} 的依赖措施未通过，不能核验通过：" + "；".join(blockers),
                        details={"blockers": blockers},
                    )

            review = ReviewRecord(
                id=f"{measure_id}-RV{len(measure.reviews) + 1:02d}",
                measure_id=measure_id,
                evidence_version_id=pending.id,
                verifier=verifier,
                verdict=overall,
                item_results=item_verdicts,
                comment=comment,
                reviewed_at=self._now(),
            )
            measure.reviews.append(review)
            if overall == ReviewVerdict.APPROVED:
                pending.status = EvidenceStatus.ACCEPTED
                measure.status = MeasureStatus.PASSED
                kind = DecisionKind.REVIEW_APPROVED
            elif overall == ReviewVerdict.PARTIAL:
                pending.status = EvidenceStatus.PARTIAL
                measure.status = MeasureStatus.REJECTED
                kind = DecisionKind.REVIEW_PARTIAL
            else:
                pending.status = EvidenceStatus.REJECTED
                measure.status = MeasureStatus.REJECTED
                kind = DecisionKind.REVIEW_REJECTED
            self._record(
                case.id,
                kind,
                verifier,
                reason=comment,
                payload={
                    "measure_id": measure_id,
                    "evidence_version_id": pending.id,
                    "rejected_items": [iv.item for iv in rejected],
                },
            )
            self._refresh_case_status(case)
            return to_jsonable(review)

    # ------------------------------------------------------------------
    # 延期
    # ------------------------------------------------------------------
    def request_extension(self, measure_id: str, *, requested_by: str, new_deadline, reason: str) -> dict:
        with self.store.lock:
            measure = self._get_measure(measure_id)
            case = self._get_case(measure.case_id)
            self._assert_case_open(case)
            if measure.status == MeasureStatus.SUPERSEDED:
                raise ConflictError(f"措施 {measure_id} 已被替代，请对替代措施 {measure.superseded_by} 申请延期")
            if measure.status == MeasureStatus.PASSED:
                raise ConflictError(f"措施 {measure_id} 已通过，无需延期")
            if not reason:
                raise ValidationError("延期必须说明理由")
            new_deadline = _parse_date(new_deadline, "新期限")
            if new_deadline <= measure.deadline:
                raise ValidationError(f"新期限必须晚于当前期限 {measure.deadline.isoformat()}")
            if any(
                ext.measure_id == measure_id and ext.status == ExtensionStatus.PENDING
                for ext in self.store.extensions.values()
            ):
                raise ConflictError(f"措施 {measure_id} 已有待审批的延期申请")
            seq = self.store.next_seq(f"extension:{measure_id}")
            ext = ExtensionRequest(
                id=f"{measure_id}-EX{seq:02d}",
                case_id=case.id,
                measure_id=measure_id,
                requested_by=requested_by,
                reason=reason,
                old_deadline=measure.deadline,
                new_deadline=new_deadline,
                requested_at=self._now(),
            )
            self.store.extensions[ext.id] = ext
            return to_jsonable(ext)

    def decide_extension(self, extension_id: str, *, approver: str, approve: bool, note: str = "") -> dict:
        with self.store.lock:
            ext = self._get_extension(extension_id)
            if ext.status != ExtensionStatus.PENDING:
                raise ConflictError(f"延期申请 {extension_id} 已审批，不能重复决定")
            measure = self._get_measure(ext.measure_id)
            ext.decided_by = approver
            ext.decided_at = self._now()
            ext.decision_note = note
            if approve:
                ext.status = ExtensionStatus.GRANTED
                measure.deadline = ext.new_deadline
                kind = DecisionKind.EXTENSION_GRANTED
            else:
                ext.status = ExtensionStatus.DENIED
                kind = DecisionKind.EXTENSION_DENIED
            self._record(
                ext.case_id,
                kind,
                approver,
                reason=note or ext.reason,
                payload={
                    "extension_id": ext.id,
                    "measure_id": ext.measure_id,
                    "old_deadline": ext.old_deadline.isoformat(),
                    "new_deadline": ext.new_deadline.isoformat(),
                    "request_reason": ext.reason,
                },
            )
            return to_jsonable(ext)

    # ------------------------------------------------------------------
    # 替代措施
    # ------------------------------------------------------------------
    def propose_alternative(self, measure_id: str, *, proposed_by: str, reason: str, measure: dict) -> dict:
        with self.store.lock:
            original = self._get_measure(measure_id)
            case = self._get_case(original.case_id)
            self._assert_case_open(case)
            if original.status == MeasureStatus.SUPERSEDED:
                raise ConflictError(f"措施 {measure_id} 已被替代，不能再次提出替代")
            if original.status == MeasureStatus.PASSED:
                raise ConflictError(f"措施 {measure_id} 已通过，如需替代请先复开")
            if not reason:
                raise ValidationError("替代措施必须说明理由")
            if not isinstance(measure, dict):
                raise ValidationError("替代措施规格必须是对象")
            title = (measure.get("title") or "").strip()
            if not title:
                raise ValidationError("替代措施缺少标题")
            if any(
                alt.original_measure_id == measure_id and alt.status == AlternativeStatus.PENDING
                for alt in self.store.alternatives.values()
            ):
                raise ConflictError(f"措施 {measure_id} 已有待审批的替代措施申请")
            depends_on = measure.get("depends_on")
            if depends_on is not None and (
                not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on)
            ):
                raise ValidationError("替代措施的 depends_on 必须是字符串列表")
            seq = self.store.next_seq(f"alternative:{measure_id}")
            alt = AlternativeProposal(
                id=f"{measure_id}-ALT{seq:02d}",
                case_id=case.id,
                original_measure_id=measure_id,
                proposed_by=proposed_by,
                reason=reason,
                title=title,
                detail=measure.get("detail") or "",
                kind=_parse_kind(measure["kind"]) if measure.get("kind") else None,
                deadline=_parse_date(measure.get("deadline"), "替代措施期限"),
                depends_on=list(depends_on) if depends_on is not None else None,
                proposed_at=self._now(),
            )
            self.store.alternatives[alt.id] = alt
            return to_jsonable(alt)

    def _assert_alternative_acyclic(self, case_id: str, original_id: str, new_id: str, new_deps: list[str]) -> None:
        """在假设替代已生效的图上做环检测（原措施的出边被替代边取代）。"""
        measures = {m.id: m for m in self._case_measures(case_id)}

        def resolve(mid: str) -> str:
            seen: set[str] = set()
            while True:
                if mid == original_id:
                    return new_id
                node = measures.get(mid)
                if node is None or not node.superseded_by or mid in seen:
                    return mid
                seen.add(mid)
                mid = node.superseded_by

        edges: dict[str, list[str]] = {}
        for m in measures.values():
            if m.id == original_id:
                edges[m.id] = [new_id]
            elif m.status == MeasureStatus.SUPERSEDED and m.superseded_by:
                edges[m.id] = [resolve(m.superseded_by)]
            else:
                edges[m.id] = [resolve(dep) for dep in m.depends_on]
        edges[new_id] = [resolve(dep) for dep in new_deps]
        self._assert_acyclic(edges)

    def decide_alternative(self, alt_id: str, *, approver: str, approve: bool, note: str = "") -> dict:
        with self.store.lock:
            alt = self._get_alternative(alt_id)
            if alt.status != AlternativeStatus.PENDING:
                raise ConflictError(f"替代措施申请 {alt_id} 已审批，不能重复决定")
            original = self._get_measure(alt.original_measure_id)
            case = self._get_case(alt.case_id)
            self._assert_case_open(case)
            alt.decided_by = approver
            alt.decided_at = self._now()
            alt.decision_note = note
            if not approve:
                alt.status = AlternativeStatus.REJECTED
                self._record(
                    alt.case_id,
                    DecisionKind.ALTERNATIVE_REJECTED,
                    approver,
                    reason=note or alt.reason,
                    payload={"alternative_id": alt.id, "original_measure": original.id},
                )
                return to_jsonable(alt)

            new_deps = list(alt.depends_on) if alt.depends_on is not None else list(original.depends_on)
            known = {m.id for m in self._case_measures(case.id)}
            for dep in new_deps:
                if dep not in known:
                    raise ValidationError(f"替代措施依赖了不存在的措施：{dep}")
            new_id = self._next_measure_id(case.id)
            self._assert_alternative_acyclic(case.id, original.id, new_id, new_deps)

            replacement = Measure(
                id=new_id,
                case_id=case.id,
                requirement_id=original.requirement_id,
                kind=alt.kind or original.kind,
                title=alt.title,
                detail=alt.detail,
                required=original.required,
                deadline=alt.deadline,
                depends_on=new_deps,
                created_at=self._now(),
                supersedes=original.id,
            )
            self.store.measures[new_id] = replacement
            original.superseded_by = new_id
            original.status = MeasureStatus.SUPERSEDED
            for ev in original.evidence:
                if ev.status == EvidenceStatus.PENDING:
                    ev.status = EvidenceStatus.SUPERSEDED
            alt.status = AlternativeStatus.ACCEPTED
            alt.replacement_measure_id = new_id
            self._record(
                alt.case_id,
                DecisionKind.ALTERNATIVE_ACCEPTED,
                approver,
                reason=note or alt.reason,
                payload={
                    "alternative_id": alt.id,
                    "original_measure": original.id,
                    "replacement_measure": new_id,
                    "request_reason": alt.reason,
                },
            )
            self._refresh_case_status(case)
            return to_jsonable(alt)

    # ------------------------------------------------------------------
    # 结案、归档与复开
    # ------------------------------------------------------------------
    def close_case(self, case_id: str, *, actor: str, note: str = "") -> dict:
        """结案完整性门槛：全部必需措施通过才允许结案。"""
        with self.store.lock:
            case = self._get_case(case_id)
            if case.status in (CaseStatus.CLOSED, CaseStatus.ARCHIVED):
                raise ConflictError(f"案件 {case_id} 已结案")
            blockers = self._case_blockers(case)
            if blockers:
                raise ClosureBlocked(blockers)
            case.closed_at = self._now()
            case.closed_by = actor
            self._refresh_case_status(case)
            self._record(case_id, DecisionKind.CASE_CLOSED, actor, reason=note)
            return self.case_detail(case_id)

    def archive_case(self, case_id: str, *, actor: str, note: str = "") -> dict:
        with self.store.lock:
            case = self._get_case(case_id)
            if case.status != CaseStatus.CLOSED:
                raise ConflictError(f"案件 {case_id} 尚未结案，不能归档")
            case.archived_at = self._now()
            self._refresh_case_status(case)
            self._record(case_id, DecisionKind.CASE_ARCHIVED, actor, reason=note)
            return self.case_detail(case_id)

    def reopen_case(
        self,
        case_id: str,
        *,
        actor: str,
        reason: str,
        measure_ids: list[str] | None = None,
    ) -> dict:
        """复开已结案案件，可顺带复开指定措施，决定链全程留痕。"""
        with self.store.lock:
            case = self._get_case(case_id)
            if case.status not in (CaseStatus.CLOSED, CaseStatus.ARCHIVED):
                raise ConflictError(f"案件 {case_id} 未结案，无需复开")
            if not reason:
                raise ValidationError("复开必须说明理由")
            case.closed_at = None
            case.closed_by = None
            case.archived_at = None
            case.reopen_count += 1
            reopened: list[str] = []
            for mid in measure_ids or []:
                measure = self._get_measure(mid)
                if measure.case_id != case_id:
                    raise ValidationError(f"措施 {mid} 不属于案件 {case_id}")
                if measure.status == MeasureStatus.PASSED:
                    measure.status = MeasureStatus.IN_PROGRESS
                    reopened.append(mid)
                    self._record(
                        case_id,
                        DecisionKind.MEASURE_REOPENED,
                        actor,
                        reason=reason,
                        payload={"measure_id": mid},
                    )
            self._record(
                case_id,
                DecisionKind.CASE_REOPENED,
                actor,
                reason=reason,
                payload={"reopened_measures": reopened},
            )
            self._refresh_case_status(case)
            return self.case_detail(case_id)

    def reopen_measure(self, measure_id: str, *, actor: str, reason: str) -> dict:
        """复开单项已通过措施；若案件已结案则连带复开案件。"""
        with self.store.lock:
            measure = self._get_measure(measure_id)
            if measure.status != MeasureStatus.PASSED:
                raise ConflictError(f"措施 {measure_id} 当前状态为 {measure.status.value}，只有已通过的措施可以复开")
            if not reason:
                raise ValidationError("复开必须说明理由")
            case = self._get_case(measure.case_id)
            measure.status = MeasureStatus.IN_PROGRESS
            self._record(
                case.id,
                DecisionKind.MEASURE_REOPENED,
                actor,
                reason=reason,
                payload={"measure_id": measure_id},
            )
            if case.status in (CaseStatus.CLOSED, CaseStatus.ARCHIVED):
                case.closed_at = None
                case.closed_by = None
                case.archived_at = None
                case.reopen_count += 1
                self._record(
                    case.id,
                    DecisionKind.CASE_REOPENED,
                    actor,
                    reason=reason,
                    payload={"trigger": f"措施 {measure_id} 复开"},
                )
            self._refresh_case_status(case)
            return self.measure_detail(measure_id)

    # ------------------------------------------------------------------
    # 期限提醒（幂等，可安全重跑）
    # ------------------------------------------------------------------
    def run_reminders(self, *, today=None, due_within_days: int = 7) -> dict:
        """扫描临期/逾期措施并发送提醒。

        提醒键为 (措施, 提醒类型, 期限)，已发送过的键直接跳过：
        任务重复执行不会产生重复提醒，延期后新期限自然生成新键。
        """
        with self.store.lock:
            run_date = _parse_date(today, "提醒运行日期") if today is not None else self._today()
            emitted: list[dict] = []
            duplicates = 0
            for case in self.store.cases.values():
                if case.status in (CaseStatus.CLOSED, CaseStatus.ARCHIVED):
                    continue
                for measure in self._case_measures(case.id):
                    if measure.status in (MeasureStatus.PASSED, MeasureStatus.SUPERSEDED):
                        continue
                    days = (measure.deadline - run_date).days
                    if days < 0:
                        kind = REMINDER_OVERDUE
                    elif days <= due_within_days:
                        kind = REMINDER_DUE_SOON
                    else:
                        continue
                    key = f"{measure.id}|{kind}|{measure.deadline.isoformat()}"
                    if key in self.store.reminder_ledger:
                        duplicates += 1
                        continue
                    self.store.reminder_ledger.add(key)
                    entry = {
                        "key": key,
                        "kind": kind,
                        "case_id": case.id,
                        "requirement_id": measure.requirement_id,
                        "measure_id": measure.id,
                        "title": measure.title,
                        "deadline": measure.deadline.isoformat(),
                    }
                    if days < 0:
                        entry["overdue_days"] = -days
                    else:
                        entry["days_left"] = days
                    emitted.append(entry)
            return {"emitted": emitted, "emitted_count": len(emitted), "duplicate_count": duplicates}

    # ------------------------------------------------------------------
    # 进度与阻塞原因
    # ------------------------------------------------------------------
    def _measure_blockers(self, measure: Measure, today: date) -> list[str]:
        blockers: list[str] = []
        if measure.status == MeasureStatus.PENDING:
            blockers.append(f"措施 {measure.id}「{measure.title}」尚未启动整改")
        elif measure.status == MeasureStatus.IN_PROGRESS:
            blockers.append(f"措施 {measure.id}「{measure.title}」整改中，待提交证据")
        elif measure.status == MeasureStatus.SUBMITTED:
            pending = next((ev for ev in reversed(measure.evidence) if ev.status == EvidenceStatus.PENDING), None)
            version = f"第 {pending.version} 版" if pending else "最新"
            blockers.append(f"措施 {measure.id}「{measure.title}」{version}证据待核验")
        elif measure.status == MeasureStatus.REJECTED:
            last = measure.reviews[-1] if measure.reviews else None
            if last and last.verdict == ReviewVerdict.PARTIAL:
                rejected_items = "、".join(iv.item for iv in last.item_results if iv.verdict == ITEM_REJECT)
                blockers.append(
                    f"措施 {measure.id}「{measure.title}」最近一次核验部分驳回（驳回项：{rejected_items}），待重新提交"
                )
            else:
                blockers.append(f"措施 {measure.id}「{measure.title}」最近一次核验被驳回，待重新提交")
        for dep in measure.depends_on:
            effective = self._resolve_effective(dep)
            if effective.status != MeasureStatus.PASSED:
                blockers.append(
                    f"措施 {measure.id} 依赖的 {effective.id}「{effective.title}」未通过（当前状态：{effective.status.value}）"
                )
        if measure.status != MeasureStatus.PASSED and measure.deadline < today:
            blockers.append(
                f"措施 {measure.id} 已逾期 {(today - measure.deadline).days} 天（期限 {measure.deadline.isoformat()}）"
            )
        if any(
            ext.measure_id == measure.id and ext.status == ExtensionStatus.PENDING
            for ext in self.store.extensions.values()
        ):
            blockers.append(f"措施 {measure.id} 存在待审批的延期申请")
        return blockers

    def _case_blockers(self, case: Case) -> list[str]:
        today = self._today()
        blockers: list[str] = []
        for measure in self._case_measures(case.id):
            if measure.status == MeasureStatus.SUPERSEDED or not measure.required:
                continue
            blockers.extend(self._measure_blockers(measure, today))
        return blockers

    def _measure_view(self, measure: Measure, today: date) -> dict:
        view = to_jsonable(measure)
        view.pop("evidence", None)
        view.pop("reviews", None)
        view["evidence_version_count"] = len(measure.evidence)
        view["overdue_days"] = (
            max(0, (today - measure.deadline).days) if measure.status != MeasureStatus.PASSED else 0
        )
        view["pending_extension"] = any(
            ext.measure_id == measure.id and ext.status == ExtensionStatus.PENDING
            for ext in self.store.extensions.values()
        )
        last_review = measure.reviews[-1] if measure.reviews else None
        view["last_review"] = (
            {
                "id": last_review.id,
                "verifier": last_review.verifier,
                "verdict": last_review.verdict.value,
                "reviewed_at": last_review.reviewed_at.isoformat() if last_review.reviewed_at else None,
            }
            if last_review
            else None
        )
        return view

    def case_progress(self, case_id: str) -> dict:
        """每项整改要求的完成度与阻塞原因。"""
        with self.store.lock:
            case = self._get_case(case_id)
            today = self._today()
            req_views = []
            total_required = passed_required = 0
            all_blockers: list[str] = []
            for req in self._case_requirements(case_id):
                measures = [m for m in self._case_measures(case_id) if m.requirement_id == req.id]
                effective = [m for m in measures if m.status != MeasureStatus.SUPERSEDED]
                superseded = [m for m in measures if m.status == MeasureStatus.SUPERSEDED]
                required = [m for m in effective if m.required]
                optional = [m for m in effective if not m.required]
                passed = [m for m in required if m.status == MeasureStatus.PASSED]
                blockers = [b for m in required for b in self._measure_blockers(m, today)]
                total_required += len(required)
                passed_required += len(passed)
                all_blockers.extend(blockers)
                req_views.append(
                    {
                        "requirement_id": req.id,
                        "title": req.title,
                        "required_total": len(required),
                        "required_passed": len(passed),
                        "optional_total": len(optional),
                        "optional_passed": sum(1 for m in optional if m.status == MeasureStatus.PASSED),
                        "completion": round(len(passed) / len(required), 4) if required else 1.0,
                        "blocked": bool(blockers),
                        "blockers": blockers,
                        "measures": [self._measure_view(m, today) for m in effective],
                        "superseded_measures": [self._measure_view(m, today) for m in superseded],
                    }
                )
            return {
                "case_id": case.id,
                "status": case.status.value,
                "can_close": not all_blockers and case.status not in (CaseStatus.CLOSED, CaseStatus.ARCHIVED),
                "overall": {
                    "required_total": total_required,
                    "required_passed": passed_required,
                    "completion": round(passed_required / total_required, 4) if total_required else 1.0,
                },
                "requirements": req_views,
                "blockers": all_blockers,
            }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_cases(self) -> list[dict]:
        with self.store.lock:
            return [
                {
                    "case_id": case.id,
                    "decision_ref": case.decision_ref,
                    "institution": case.institution,
                    "status": case.status.value,
                    "created_at": case.created_at.isoformat(),
                    "closed_at": case.closed_at.isoformat() if case.closed_at else None,
                    "reopen_count": case.reopen_count,
                }
                for case in self.store.cases.values()
            ]

    def measure_detail(self, measure_id: str) -> dict:
        with self.store.lock:
            measure = self._get_measure(measure_id)
            view = to_jsonable(measure)
            view["effective_measure_id"] = self._resolve_effective(measure_id).id
            view["blockers"] = (
                []
                if measure.status == MeasureStatus.SUPERSEDED
                else self._measure_blockers(measure, self._today())
            )
            return view

    def case_detail(self, case_id: str) -> dict:
        with self.store.lock:
            case = self._get_case(case_id)
            return {
                **to_jsonable(case),
                "requirements": [
                    {
                        **to_jsonable(req),
                        "measures": [
                            self.measure_detail(m.id)
                            for m in self._case_measures(case_id)
                            if m.requirement_id == req.id
                        ],
                    }
                    for req in self._case_requirements(case_id)
                ],
            }

    def list_decisions(self, case_id: str) -> list[dict]:
        with self.store.lock:
            self._get_case(case_id)
            return [to_jsonable(record) for record in self.store.decisions[case_id]]
