"""整改闭环核心服务：措施依赖、证据版本核验、结案门槛与决定链。"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Any, Callable

from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    ACTIVE_MEASURE_STATUSES,
    CASE_STATE_ARCHIVED,
    CASE_STATE_DECIDED,
    CASE_STATE_HANDLING,
    CASE_STATE_PENDING,
    CASE_STATE_REGISTERED,
    CLOSED_CASE_STATES,
    DECIDE_ARCHIVE,
    DECIDE_CLOSE,
    DECIDE_ISSUE,
    DECIDE_PARTIAL,
    DECIDE_PASS,
    DECIDE_REGISTER,
    DECIDE_REJECT,
    DECIDE_REOPEN,
    DECIDE_SUBMIT,
    DECIDE_SUBSTITUTE,
    DECIDE_EXTEND,
    DECISION_ROLES,
    MEASURE_APPROVED,
    MEASURE_BLOCKED,
    MEASURE_KINDS,
    MEASURE_READY,
    MEASURE_REJECTED,
    MEASURE_SUBMITTED,
    MEASURE_SUBSTITUTED,
    OPEN_CASE_STATES,
    RESULT_PARTIAL,
    RESULT_PASS,
    RESULT_REJECT,
    ROLES,
    SUBMITTER_ROLES,
    VERIFY_RESULTS,
    Case,
    Decision,
    Evidence,
    Extension,
    Measure,
    Requirement,
    Verification,
)
from .store import JsonStore


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RemediationService:
    """整改案件的应用服务；clock 可注入以便测试。"""

    def __init__(self, store: JsonStore, clock: Callable[[], datetime] | None = None) -> None:
        self.store = store
        self._clock = clock or _utcnow

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return self._clock()

    def _today(self) -> date:
        return self._now().date()

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")

    @staticmethod
    def _require_role(role: str, allowed: tuple[str, ...], action: str) -> None:
        if role not in allowed:
            raise ValidationError(f"{action}需要角色：{'或'.join(allowed)}，当前为：{role}")

    @staticmethod
    def _parse_deadline(value: Any, field_name: str = "deadline") -> date:
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError):
            raise ValidationError(f"{field_name} 必须是 YYYY-MM-DD 格式：{value!r}") from None

    @staticmethod
    def _get_case(data: dict[str, Any], case_id: str) -> Case:
        raw = data["cases"].get(case_id)
        if raw is None:
            raise NotFoundError(f"案件不存在：{case_id}")
        return Case.from_dict(raw)

    @staticmethod
    def _get_measure(case: Case, measure_id: str) -> Measure:
        measure = case.measures.get(measure_id)
        if measure is None:
            raise NotFoundError(f"措施不存在：{measure_id}")
        return measure

    def _save_case(self, data: dict[str, Any], case: Case) -> None:
        data["cases"][case.case_id] = case.to_dict()

    def _record(self, case: Case, kind: str, actor: str, role: str, detail: dict[str, Any]) -> None:
        case.decisions.append(
            Decision(
                seq=len(case.decisions) + 1,
                kind=kind,
                actor=actor,
                role=role,
                detail=detail,
                at=self._stamp(),
            )
        )

    @staticmethod
    def _check_cycles(measures: dict[str, Measure]) -> None:
        color: dict[str, int] = {mid: 0 for mid in measures}  # 0=未访问 1=在栈上 2=已完成

        def visit(mid: str, path: list[str]) -> None:
            color[mid] = 1
            for dep in measures[mid].depends_on:
                if color[dep] == 1:
                    raise ValidationError("措施依赖存在环：" + "→".join(path + [dep]))
                if color[dep] == 0:
                    visit(dep, path + [dep])
            color[mid] = 2

        for mid in measures:
            if color[mid] == 0:
                visit(mid, [mid])

    def _dependency_met(self, case: Case, measure_id: str) -> bool:
        """依赖是否解除：措施已通过，或其替代链末端已通过。"""
        measure = case.measures[measure_id]
        seen: set[str] = set()
        while measure.status == MEASURE_SUBSTITUTED and measure.substituted_by:
            if measure.measure_id in seen:
                return False
            seen.add(measure.measure_id)
            measure = case.measures[measure.substituted_by]
        return measure.status == MEASURE_APPROVED

    def _unmet_dependencies(self, case: Case, measure: Measure) -> list[str]:
        return [dep for dep in measure.depends_on if not self._dependency_met(case, dep)]

    def _refresh_blocked(self, case: Case) -> None:
        """依赖全部解除后，把待启动措施推进为可提交。"""
        for measure in case.measures.values():
            if measure.status == MEASURE_BLOCKED and not self._unmet_dependencies(case, measure):
                measure.status = MEASURE_READY

    # ------------------------------------------------------------------
    # 案件生命周期
    # ------------------------------------------------------------------
    def create_case(
        self,
        *,
        case_id: str,
        title: str,
        requirements: list[dict[str, Any]],
        actor: str,
        role: str,
    ) -> dict[str, Any]:
        """登记案件：把整改要求拆成带依赖和期限的措施。"""
        self._require_role(role, ROLES, "登记案件")
        if not case_id or not isinstance(case_id, str):
            raise ValidationError("case_id 不能为空")
        if not title:
            raise ValidationError("title 不能为空")
        if not isinstance(requirements, list) or not requirements:
            raise ValidationError("至少需要一个整改要求")

        with self.store.transaction() as data:
            if case_id in data["cases"]:
                raise ConflictError(f"案件已存在：{case_id}")

            measures: dict[str, Measure] = {}
            reqs: list[Requirement] = []
            seen_req: set[str] = set()
            for req in requirements:
                if not isinstance(req, dict):
                    raise ValidationError("整改要求格式错误")
                rid = req.get("requirement_id")
                if not rid:
                    raise ValidationError("整改要求缺少 requirement_id")
                if rid in seen_req:
                    raise ValidationError(f"整改要求编号重复：{rid}")
                seen_req.add(rid)
                entries = req.get("measures")
                if not isinstance(entries, list) or not entries:
                    raise ValidationError(f"整改要求 {rid} 至少包含一条措施")
                measure_ids: list[str] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValidationError(f"整改要求 {rid} 的措施格式错误")
                    mid = entry.get("measure_id")
                    if not mid:
                        raise ValidationError(f"整改要求 {rid} 存在缺少 measure_id 的措施")
                    if mid in measures:
                        raise ValidationError(f"措施编号重复：{mid}")
                    kind = entry.get("kind", "其他")
                    if kind not in MEASURE_KINDS:
                        raise ValidationError(f"措施 {mid} 类型无效：{kind}")
                    deadline = self._parse_deadline(entry.get("deadline"), f"措施 {mid} 的 deadline")
                    depends_on = entry.get("depends_on") or []
                    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
                        raise ValidationError(f"措施 {mid} 的 depends_on 必须是字符串列表")
                    measures[mid] = Measure(
                        measure_id=mid,
                        requirement_id=rid,
                        title=entry.get("title", ""),
                        kind=kind,
                        required=bool(entry.get("required", True)),
                        deadline=deadline.isoformat(),
                        depends_on=list(dict.fromkeys(depends_on)),
                        status=MEASURE_READY,
                        created_at=self._stamp(),
                    )
                    measure_ids.append(mid)
                reqs.append(Requirement(requirement_id=rid, title=req.get("title", ""), measure_ids=measure_ids))

            for measure in measures.values():
                for dep in measure.depends_on:
                    if dep not in measures:
                        raise ValidationError(f"措施 {measure.measure_id} 依赖未知措施：{dep}")
            self._check_cycles(measures)
            for measure in measures.values():
                if measure.depends_on:
                    measure.status = MEASURE_BLOCKED

            case = Case(
                case_id=case_id,
                title=title,
                state=CASE_STATE_REGISTERED,
                created_at=self._stamp(),
                requirements=reqs,
                measures=measures,
                decisions=[],
                closed_at=None,
            )
            self._record(case, DECIDE_REGISTER, actor, role, {"title": title})
            self._save_case(data, case)
            return self._progress_view(case)

    def issue_decision(self, case_id: str, *, actor: str, role: str) -> dict[str, Any]:
        """下达检查决定：登记 → 待核验。"""
        self._require_role(role, DECISION_ROLES, "下达检查决定")
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state != CASE_STATE_REGISTERED:
                raise ConflictError(f"当前状态为{case.state}，不能下达检查决定")
            case.state = CASE_STATE_PENDING
            self._record(case, DECIDE_ISSUE, actor, role, {})
            self._save_case(data, case)
            return self._progress_view(case)

    def close_case(self, case_id: str, *, decided_by: str, role: str) -> dict[str, Any]:
        """结案门槛：全部必需措施通过后才允许结案。"""
        self._require_role(role, DECISION_ROLES, "结案")
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state not in OPEN_CASE_STATES:
                raise ConflictError(f"当前状态为{case.state}，不能结案")
            view = self._progress_view(case)
            if view["blocking"]:
                raise ConflictError("存在未完成的整改要求，不能结案：" + "；".join(view["blocking"]))
            case.state = CASE_STATE_DECIDED
            case.closed_at = self._stamp()
            self._record(case, DECIDE_CLOSE, decided_by, role, {"closed_at": case.closed_at})
            self._save_case(data, case)
            return self._progress_view(case)

    def archive_case(self, case_id: str, *, actor: str, role: str) -> dict[str, Any]:
        """归档：已决定 → 已归档。"""
        self._require_role(role, DECISION_ROLES, "归档")
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state != CASE_STATE_DECIDED:
                raise ConflictError(f"当前状态为{case.state}，只有已决定案件可以归档")
            case.state = CASE_STATE_ARCHIVED
            self._record(case, DECIDE_ARCHIVE, actor, role, {})
            self._save_case(data, case)
            return self._progress_view(case)

    def reopen_case(
        self,
        case_id: str,
        *,
        reason: str,
        actor: str,
        role: str,
        reset_measure_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """复开：已决定/已归档 → 处置中，决定链保留完整历史。"""
        self._require_role(role, DECISION_ROLES, "复开案件")
        if not reason:
            raise ValidationError("复开必须说明理由")
        reset_ids = list(reset_measure_ids or [])
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state not in CLOSED_CASE_STATES:
                raise ConflictError(f"当前状态为{case.state}，无需复开")
            for mid in reset_ids:
                measure = self._get_measure(case, mid)
                if measure.status == MEASURE_SUBSTITUTED:
                    raise ConflictError(f"措施 {mid} 已被替代，不能重置")
                if measure.status == MEASURE_APPROVED:
                    measure.status = MEASURE_READY
            case.state = CASE_STATE_HANDLING
            case.closed_at = None
            self._record(case, DECIDE_REOPEN, actor, role, {"reason": reason, "reset_measures": reset_ids})
            self._refresh_blocked(case)
            self._save_case(data, case)
            return self._progress_view(case)

    # ------------------------------------------------------------------
    # 证据提交与核验
    # ------------------------------------------------------------------
    def submit_evidence(
        self,
        case_id: str,
        measure_id: str,
        *,
        items: list[str],
        submitted_by: str,
        role: str,
        note: str = "",
    ) -> dict[str, Any]:
        """提交一版证据；同一措施版本号递增，历史版本全部保留。"""
        self._require_role(role, SUBMITTER_ROLES, "提交证据")
        if not items or not all(isinstance(i, str) and i for i in items):
            raise ValidationError("证据条目不能为空")
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state == CASE_STATE_REGISTERED:
                raise ConflictError("案件尚未下达检查决定，不能提交证据")
            if case.state not in OPEN_CASE_STATES:
                raise ConflictError(f"案件已结案（{case.state}），如需继续整改请先复开")
            measure = self._get_measure(case, measure_id)
            if measure.status == MEASURE_SUBSTITUTED:
                raise ConflictError(
                    f"措施 {measure_id} 已被替代（{measure.substituted_by}），请向替代措施提交证据"
                )
            if measure.status == MEASURE_APPROVED:
                raise ConflictError(f"措施 {measure_id} 已通过核验，无需再提交")
            if measure.status == MEASURE_SUBMITTED:
                raise ConflictError(
                    f"措施 {measure_id} 存在待核验证据 v{measure.latest_evidence.version}，请先完成核验"
                )
            if measure.status == MEASURE_BLOCKED:
                unmet = self._unmet_dependencies(case, measure)
                raise ConflictError(f"措施 {measure_id} 依赖未完成：{'、'.join(unmet)}")

            version = len(measure.evidence) + 1
            evidence = Evidence(
                evidence_id=f"{measure_id}-V{version}",
                version=version,
                submitted_by=submitted_by,
                role=role,
                items=list(items),
                note=note,
                submitted_at=self._stamp(),
            )
            measure.evidence.append(evidence)
            measure.status = MEASURE_SUBMITTED
            if case.state == CASE_STATE_PENDING:
                case.state = CASE_STATE_HANDLING
            self._record(
                case,
                DECIDE_SUBMIT,
                submitted_by,
                role,
                {"measure_id": measure_id, "version": version, "items": list(items)},
            )
            self._save_case(data, case)
            return {
                "evidence": evidence.to_dict(),
                "measure_status": measure.status,
                "case_state": case.state,
            }

    def verify_evidence(
        self,
        case_id: str,
        measure_id: str,
        *,
        version: int,
        verifier: str,
        role: str,
        result: str,
        comment: str = "",
        rejected_items: list[str] | None = None,
    ) -> dict[str, Any]:
        """核验最新一版证据：通过 / 驳回 / 部分驳回，记录核验人与复查结果。"""
        self._require_role(role, DECISION_ROLES, "核验证据")
        if result not in VERIFY_RESULTS:
            raise ValidationError(f"核验结论无效：{result}，应为{'/'.join(VERIFY_RESULTS)}")
        rejected = list(rejected_items or [])
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state not in OPEN_CASE_STATES:
                raise ConflictError(f"当前状态为{case.state}，不能核验证据")
            measure = self._get_measure(case, measure_id)
            if measure.status != MEASURE_SUBMITTED or measure.latest_evidence is None:
                raise ConflictError(f"措施 {measure_id} 当前没有待核验证据")
            latest = measure.latest_evidence
            if version != latest.version:
                raise ConflictError(f"证据版本已过期：最新为 v{latest.version}，收到 v{version}")
            if result == RESULT_PARTIAL:
                if not rejected:
                    raise ValidationError("部分驳回必须给出驳回条目")
                unknown = [item for item in rejected if item not in latest.items]
                if unknown:
                    raise ValidationError(f"驳回条目不在本次提交中：{'、'.join(unknown)}")
            elif rejected:
                raise ValidationError("仅部分驳回可以携带驳回条目")

            latest.verification = Verification(
                verifier=verifier,
                role=role,
                result=result,
                comment=comment,
                rejected_items=rejected,
                verified_at=self._stamp(),
            )
            if result == RESULT_PASS:
                measure.status = MEASURE_APPROVED
                kind = DECIDE_PASS
            elif result == RESULT_REJECT:
                measure.status = MEASURE_REJECTED
                kind = DECIDE_REJECT
            else:
                measure.status = MEASURE_REJECTED
                kind = DECIDE_PARTIAL
            self._record(
                case,
                kind,
                verifier,
                role,
                {
                    "measure_id": measure_id,
                    "version": version,
                    "comment": comment,
                    "rejected_items": rejected,
                },
            )
            if result == RESULT_PASS:
                self._refresh_blocked(case)
            self._save_case(data, case)
            return {
                "evidence": latest.to_dict(),
                "measure_status": measure.status,
                "case_state": case.state,
            }

    # ------------------------------------------------------------------
    # 延期与替代
    # ------------------------------------------------------------------
    def extend_deadline(
        self,
        case_id: str,
        measure_id: str,
        *,
        new_deadline: str,
        reason: str,
        decided_by: str,
        role: str,
    ) -> dict[str, Any]:
        """批准延期：原期限与每次延期都留在决定链中。"""
        self._require_role(role, DECISION_ROLES, "批准延期")
        new_date = self._parse_deadline(new_deadline, "new_deadline")
        if not reason:
            raise ValidationError("延期必须说明理由")
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state not in OPEN_CASE_STATES:
                raise ConflictError(f"当前状态为{case.state}，不能延期")
            measure = self._get_measure(case, measure_id)
            if measure.status == MEASURE_SUBSTITUTED:
                raise ConflictError(f"措施 {measure_id} 已被替代，不能延期")
            if measure.status == MEASURE_APPROVED:
                raise ConflictError(f"措施 {measure_id} 已通过核验，无需延期")
            current = date.fromisoformat(measure.effective_deadline)
            if new_date <= current:
                raise ValidationError(f"新期限必须晚于当前有效期限 {current.isoformat()}")
            measure.extensions.append(
                Extension(
                    new_deadline=new_date.isoformat(),
                    reason=reason,
                    decided_by=decided_by,
                    decided_at=self._stamp(),
                )
            )
            self._record(
                case,
                DECIDE_EXTEND,
                decided_by,
                role,
                {"measure_id": measure_id, "new_deadline": new_date.isoformat(), "reason": reason},
            )
            self._save_case(data, case)
            return self._measure_view(case, measure)

    def substitute_measure(
        self,
        case_id: str,
        measure_id: str,
        *,
        alternative: dict[str, Any],
        reason: str,
        decided_by: str,
        role: str,
    ) -> dict[str, Any]:
        """以替代措施接管原措施义务；下游依赖同步改指替代措施。"""
        self._require_role(role, DECISION_ROLES, "批准替代措施")
        if not reason:
            raise ValidationError("替代必须说明理由")
        if not isinstance(alternative, dict):
            raise ValidationError("alternative 必须是对象")
        with self.store.transaction() as data:
            case = self._get_case(data, case_id)
            if case.state not in OPEN_CASE_STATES:
                raise ConflictError(f"当前状态为{case.state}，不能替代措施")
            measure = self._get_measure(case, measure_id)
            if measure.status == MEASURE_SUBSTITUTED:
                raise ConflictError(f"措施 {measure_id} 已是被替代措施")
            if measure.status == MEASURE_APPROVED:
                raise ConflictError(f"措施 {measure_id} 已通过核验，无需替代")

            alt_id = alternative.get("measure_id")
            if not alt_id:
                raise ValidationError("替代措施缺少 measure_id")
            if alt_id in case.measures:
                raise ValidationError(f"措施编号重复：{alt_id}")
            kind = alternative.get("kind", measure.kind)
            if kind not in MEASURE_KINDS:
                raise ValidationError(f"替代措施类型无效：{kind}")
            deadline = self._parse_deadline(
                alternative.get("deadline", measure.effective_deadline), "替代措施的 deadline"
            )
            alt_deps = list(dict.fromkeys(alternative.get("depends_on") or measure.depends_on))
            if measure_id in alt_deps or alt_id in alt_deps:
                raise ValidationError("替代措施不能依赖被替代的措施或自身")
            for dep in alt_deps:
                if dep not in case.measures:
                    raise ValidationError(f"替代措施依赖未知措施：{dep}")

            alt = Measure(
                measure_id=alt_id,
                requirement_id=measure.requirement_id,
                title=alternative.get("title", measure.title),
                kind=kind,
                required=measure.required,
                deadline=deadline.isoformat(),
                depends_on=alt_deps,
                status=MEASURE_READY,
                created_at=self._stamp(),
                alternative_for=measure_id,
            )
            case.measures[alt_id] = alt
            self._check_cycles(case.measures)

            measure.status = MEASURE_SUBSTITUTED
            measure.substituted_by = alt_id
            for other in case.measures.values():
                if other.measure_id in (measure_id, alt_id):
                    continue
                if measure_id in other.depends_on:
                    other.depends_on = list(
                        dict.fromkeys(alt_id if dep == measure_id else dep for dep in other.depends_on)
                    )
            for req in case.requirements:
                if req.requirement_id == measure.requirement_id:
                    req.measure_ids.append(alt_id)
                    break
            if self._unmet_dependencies(case, alt):
                alt.status = MEASURE_BLOCKED
            self._refresh_blocked(case)
            self._record(
                case,
                DECIDE_SUBSTITUTE,
                decided_by,
                role,
                {"measure_id": measure_id, "alternative": alt_id, "reason": reason},
            )
            self._save_case(data, case)
            return {
                "substituted": self._measure_view(case, measure),
                "alternative": self._measure_view(case, alt),
            }

    # ------------------------------------------------------------------
    # 查询视图
    # ------------------------------------------------------------------
    def case_progress(self, case_id: str) -> dict[str, Any]:
        """案件进度：每项要求的完成度与阻塞原因。"""
        data = self.store.snapshot()
        return self._progress_view(self._get_case(data, case_id))

    def list_cases(self) -> dict[str, Any]:
        data = self.store.snapshot()
        cases = [self._progress_view(Case.from_dict(raw)) for raw in data["cases"].values()]
        cases.sort(key=lambda c: c["created_at"])
        return {"cases": cases, "count": len(cases)}

    def measure_detail(self, case_id: str, measure_id: str) -> dict[str, Any]:
        """措施详情：完整证据版本、核验人与复查结果。"""
        data = self.store.snapshot()
        case = self._get_case(data, case_id)
        measure = self._get_measure(case, measure_id)
        view = self._measure_view(case, measure)
        view["evidence"] = [e.to_dict() for e in measure.evidence]
        view["extensions"] = [asdict(e) for e in measure.extensions]
        return view

    def decision_chain(self, case_id: str) -> dict[str, Any]:
        """完整决定链：延期、替代、部分驳回、复开全部留痕。"""
        data = self.store.snapshot()
        case = self._get_case(data, case_id)
        return {"case_id": case_id, "decisions": [asdict(d) for d in case.decisions]}

    def case_reminders(self, case_id: str) -> dict[str, Any]:
        data = self.store.snapshot()
        if case_id not in data["cases"]:
            raise NotFoundError(f"案件不存在：{case_id}")
        items = [r for r in data["reminders"].values() if r["case_id"] == case_id]
        items.sort(key=lambda r: (r["created_at"], r["key"]))
        return {"case_id": case_id, "reminders": items, "count": len(items)}

    # ------------------------------------------------------------------
    # 视图构造
    # ------------------------------------------------------------------
    def _measure_view(self, case: Case, measure: Measure) -> dict[str, Any]:
        latest = measure.latest_evidence
        effective = measure.effective_deadline
        overdue = (
            measure.status in ACTIVE_MEASURE_STATUSES
            and date.fromisoformat(effective) < self._today()
        )
        return {
            "measure_id": measure.measure_id,
            "requirement_id": measure.requirement_id,
            "title": measure.title,
            "kind": measure.kind,
            "required": measure.required,
            "status": measure.status,
            "deadline": measure.deadline,
            "effective_deadline": effective,
            "overdue": overdue,
            "depends_on": list(measure.depends_on),
            "substituted_by": measure.substituted_by,
            "alternative_for": measure.alternative_for,
            "evidence_count": len(measure.evidence),
            "latest_version": latest.version if latest else 0,
            "latest_result": latest.verification.result if latest and latest.verification else None,
        }

    def _blocking_reasons(self, case: Case, measure: Measure) -> list[str]:
        reasons: list[str] = []
        if measure.status == MEASURE_BLOCKED:
            unmet = self._unmet_dependencies(case, measure)
            reasons.append("依赖未完成：" + "、".join(unmet))
        elif measure.status == MEASURE_READY:
            reasons.append("尚未提交证据")
        elif measure.status == MEASURE_SUBMITTED:
            reasons.append(f"证据 v{measure.latest_evidence.version} 待核验")
        elif measure.status == MEASURE_REJECTED:
            verification = measure.latest_evidence.verification if measure.latest_evidence else None
            if verification and verification.result == RESULT_PARTIAL:
                reasons.append("部分驳回（" + "、".join(verification.rejected_items) + "），待重新提交")
            else:
                reasons.append("证据被驳回，待重新提交")
        if measure.status in ACTIVE_MEASURE_STATUSES and date.fromisoformat(
            measure.effective_deadline
        ) < self._today():
            reasons.append(f"已逾期（有效期限 {measure.effective_deadline}）")
        return reasons

    def _requirement_view(self, case: Case, requirement: Requirement) -> dict[str, Any]:
        measures = [case.measures[mid] for mid in requirement.measure_ids]
        effective = [m for m in measures if m.status != MEASURE_SUBSTITUTED]
        required = [m for m in effective if m.required]
        approved = [m for m in required if m.status == MEASURE_APPROVED]
        blocking = [
            {
                "measure_id": m.measure_id,
                "title": m.title,
                "reasons": self._blocking_reasons(case, m),
            }
            for m in required
            if m.status != MEASURE_APPROVED
        ]
        return {
            "requirement_id": requirement.requirement_id,
            "title": requirement.title,
            "total_required": len(required),
            "approved_required": len(approved),
            "completion": (len(approved) / len(required)) if required else 1.0,
            "blocking": blocking,
            "measures": [self._measure_view(case, m) for m in measures],
        }

    def _progress_view(self, case: Case) -> dict[str, Any]:
        req_views = [self._requirement_view(case, req) for req in case.requirements]
        total_required = sum(r["total_required"] for r in req_views)
        approved = sum(r["approved_required"] for r in req_views)
        flat_blocking = [
            f"{req['requirement_id']}:{item['measure_id']} {'；'.join(item['reasons'])}"
            for req in req_views
            for item in req["blocking"]
        ]
        can_close = case.state in OPEN_CASE_STATES and not any(r["blocking"] for r in req_views)
        return {
            "case_id": case.case_id,
            "title": case.title,
            "state": case.state,
            "created_at": case.created_at,
            "closed_at": case.closed_at,
            "can_close": can_close,
            "total_required": total_required,
            "approved_required": approved,
            "completion": (approved / total_required) if total_required else 1.0,
            "requirements": req_views,
            "blocking": flat_blocking,
        }
