"""基于标准库的 HTTP JSON API（无第三方依赖）。"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import urlparse

from .errors import ConflictError, DomainError, NotFoundError, ValidationError
from .reminders import run_reminders
from .service import RemediationService
from .store import JsonStore

Handler = Callable[..., Any]


def _need(body: dict[str, Any], *fields: str) -> None:
    missing = [f for f in fields if body.get(f) in (None, "")]
    if missing:
        raise ValidationError("缺少字段：" + "、".join(missing))


# ---------------------------------------------------------------------------
# 路由处理函数：返回 payload 或 (payload, status)
# ---------------------------------------------------------------------------
def _create_case(service: RemediationService, body: dict[str, Any]) -> tuple[Any, int]:
    _need(body, "case_id", "title", "requirements", "actor", "role")
    view = service.create_case(
        case_id=body["case_id"],
        title=body["title"],
        requirements=body["requirements"],
        actor=body["actor"],
        role=body["role"],
    )
    return view, 201


def _list_cases(service: RemediationService, body: dict[str, Any]) -> Any:
    return service.list_cases()


def _get_case(service: RemediationService, body: dict[str, Any], case_id: str) -> Any:
    return service.case_progress(case_id)


def _issue(service: RemediationService, body: dict[str, Any], case_id: str) -> Any:
    _need(body, "actor", "role")
    return service.issue_decision(case_id, actor=body["actor"], role=body["role"])


def _close(service: RemediationService, body: dict[str, Any], case_id: str) -> Any:
    _need(body, "decided_by", "role")
    return service.close_case(case_id, decided_by=body["decided_by"], role=body["role"])


def _archive(service: RemediationService, body: dict[str, Any], case_id: str) -> Any:
    _need(body, "actor", "role")
    return service.archive_case(case_id, actor=body["actor"], role=body["role"])


def _reopen(service: RemediationService, body: dict[str, Any], case_id: str) -> Any:
    _need(body, "actor", "role", "reason")
    return service.reopen_case(
        case_id,
        reason=body["reason"],
        actor=body["actor"],
        role=body["role"],
        reset_measure_ids=body.get("reset_measure_ids"),
    )


def _decisions(service: RemediationService, body: dict[str, Any], case_id: str) -> Any:
    return service.decision_chain(case_id)


def _case_reminders(service: RemediationService, body: dict[str, Any], case_id: str) -> Any:
    return service.case_reminders(case_id)


def _measure_detail(
    service: RemediationService, body: dict[str, Any], case_id: str, measure_id: str
) -> Any:
    return service.measure_detail(case_id, measure_id)


def _submit(
    service: RemediationService, body: dict[str, Any], case_id: str, measure_id: str
) -> tuple[Any, int]:
    _need(body, "items", "submitted_by", "role")
    result = service.submit_evidence(
        case_id,
        measure_id,
        items=body["items"],
        submitted_by=body["submitted_by"],
        role=body["role"],
        note=body.get("note", ""),
    )
    return result, 201


def _verify(
    service: RemediationService, body: dict[str, Any], case_id: str, measure_id: str
) -> Any:
    _need(body, "version", "verifier", "role", "result")
    return service.verify_evidence(
        case_id,
        measure_id,
        version=int(body["version"]),
        verifier=body["verifier"],
        role=body["role"],
        result=body["result"],
        comment=body.get("comment", ""),
        rejected_items=body.get("rejected_items"),
    )


def _extend(
    service: RemediationService, body: dict[str, Any], case_id: str, measure_id: str
) -> Any:
    _need(body, "new_deadline", "reason", "decided_by", "role")
    return service.extend_deadline(
        case_id,
        measure_id,
        new_deadline=body["new_deadline"],
        reason=body["reason"],
        decided_by=body["decided_by"],
        role=body["role"],
    )


def _substitute(
    service: RemediationService, body: dict[str, Any], case_id: str, measure_id: str
) -> tuple[Any, int]:
    _need(body, "alternative", "reason", "decided_by", "role")
    result = service.substitute_measure(
        case_id,
        measure_id,
        alternative=body["alternative"],
        reason=body["reason"],
        decided_by=body["decided_by"],
        role=body["role"],
    )
    return result, 201


def _run_reminders(service: RemediationService, body: dict[str, Any]) -> Any:
    now = None
    if body.get("now"):
        try:
            now = datetime.fromisoformat(str(body["now"]))
        except ValueError:
            raise ValidationError(f"now 必须是 ISO 时间格式：{body['now']!r}") from None
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
    return run_reminders(service.store, now=now, soon_days=int(body.get("soon_days", 7)))


def _health(service: RemediationService, body: dict[str, Any]) -> Any:
    return {"status": "ok"}


ROUTES: list[tuple[str, re.Pattern[str], Handler]] = [
    ("POST", re.compile(r"^/cases$"), _create_case),
    ("GET", re.compile(r"^/cases$"), _list_cases),
    ("GET", re.compile(r"^/cases/(?P<case_id>[^/]+)$"), _get_case),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/issue$"), _issue),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/close$"), _close),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/archive$"), _archive),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/reopen$"), _reopen),
    ("GET", re.compile(r"^/cases/(?P<case_id>[^/]+)/decisions$"), _decisions),
    ("GET", re.compile(r"^/cases/(?P<case_id>[^/]+)/reminders$"), _case_reminders),
    ("GET", re.compile(r"^/cases/(?P<case_id>[^/]+)/measures/(?P<measure_id>[^/]+)$"), _measure_detail),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/measures/(?P<measure_id>[^/]+)/evidence$"), _submit),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/measures/(?P<measure_id>[^/]+)/verify$"), _verify),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/measures/(?P<measure_id>[^/]+)/extensions$"), _extend),
    ("POST", re.compile(r"^/cases/(?P<case_id>[^/]+)/measures/(?P<measure_id>[^/]+)/substitutions$"), _substitute),
    ("POST", re.compile(r"^/reminders/run$"), _run_reminders),
    ("GET", re.compile(r"^/health$"), _health),
]


def make_handler() -> type[BaseHTTPRequestHandler]:
    class RemediationHandler(BaseHTTPRequestHandler):
        server_version = "RemediationLoop/0.1"

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise ValidationError("请求体不是合法 JSON") from None
            if not isinstance(body, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return body

        def _handle(self, method: str) -> None:
            try:
                body = self._read_body()
                path = urlparse(self.path).path
                for route_method, pattern, handler in ROUTES:
                    if route_method != method:
                        continue
                    match = pattern.match(path)
                    if match:
                        result = handler(self.server.service, body, **match.groupdict())  # type: ignore[attr-defined]
                        payload, status = result if isinstance(result, tuple) else (result, 200)
                        break
                else:
                    raise NotFoundError(f"接口不存在：{method} {path}")
            except NotFoundError as exc:
                payload, status = {"error": str(exc), "code": exc.code}, 404
            except ConflictError as exc:
                payload, status = {"error": str(exc), "code": exc.code}, 409
            except DomainError as exc:
                payload, status = {"error": str(exc), "code": exc.code}, 400
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接直接断开
                payload, status = {"error": f"内部错误：{exc}", "code": "internal"}, 500
            self._send(status, payload)

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, *args: Any) -> None:  # 静默访问日志
            pass

    return RemediationHandler


def build_service(data_path: str | None = None, clock: Any = None) -> RemediationService:
    return RemediationService(JsonStore(data_path), clock=clock)


def serve(host: str, port: int, service: RemediationService) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler())
    server.service = service  # type: ignore[attr-defined]
    return server
