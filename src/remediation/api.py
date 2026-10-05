"""基于标准库的 HTTP 接口层：薄路由 + JSON 错误映射。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .errors import DomainError, ValidationError
from .service import RemediationService


def _require(body: dict, *names: str):
    values = []
    for name in names:
        value = body.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValidationError(f"缺少必填字段：{name}")
        values.append(value)
    return values[0] if len(values) == 1 else values


class _Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern, object]] = []

    def add(self, method: str, pattern: str, handler) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), handler))

    def match(self, method: str, path: str):
        allowed: list[str] = []
        for route_method, regex, handler in self.routes:
            matched = regex.match(path)
            if not matched:
                continue
            if route_method == method:
                return handler, matched.groupdict()
            allowed.append(route_method)
        return None, allowed


def _h_create_case(service: RemediationService, body: dict):
    data = service.create_case(
        case_id=_require(body, "case_id"),
        decision_ref=_require(body, "decision_ref"),
        institution=_require(body, "institution"),
        requirements=_require(body, "requirements"),
        actor=body.get("actor") or "系统",
    )
    return 201, data


def _h_submit_evidence(service: RemediationService, body: dict, measure_id: str):
    return 201, service.submit_evidence(
        measure_id,
        submitted_by=_require(body, "submitted_by"),
        batch=body.get("batch") or "",
        items=_require(body, "items"),
    )


def _h_review(service: RemediationService, body: dict, measure_id: str):
    return 201, service.review_evidence(
        measure_id,
        verifier=_require(body, "verifier"),
        item_results=_require(body, "items"),
        comment=body.get("comment") or "",
    )


def _h_request_extension(service: RemediationService, body: dict, measure_id: str):
    return 201, service.request_extension(
        measure_id,
        requested_by=_require(body, "requested_by"),
        new_deadline=_require(body, "new_deadline"),
        reason=_require(body, "reason"),
    )


def _h_decide_extension(service: RemediationService, body: dict, extension_id: str):
    return service.decide_extension(
        extension_id,
        approver=_require(body, "approver"),
        approve=bool(body.get("approve")),
        note=body.get("note") or "",
    )


def _h_propose_alternative(service: RemediationService, body: dict, measure_id: str):
    return 201, service.propose_alternative(
        measure_id,
        proposed_by=_require(body, "proposed_by"),
        reason=_require(body, "reason"),
        measure=_require(body, "measure"),
    )


def _h_decide_alternative(service: RemediationService, body: dict, alt_id: str):
    return service.decide_alternative(
        alt_id,
        approver=_require(body, "approver"),
        approve=bool(body.get("approve")),
        note=body.get("note") or "",
    )


def _h_close_case(service: RemediationService, body: dict, case_id: str):
    return service.close_case(case_id, actor=_require(body, "actor"), note=body.get("note") or "")


def _h_archive_case(service: RemediationService, body: dict, case_id: str):
    return service.archive_case(case_id, actor=_require(body, "actor"), note=body.get("note") or "")


def _h_reopen_case(service: RemediationService, body: dict, case_id: str):
    return service.reopen_case(
        case_id,
        actor=_require(body, "actor"),
        reason=_require(body, "reason"),
        measure_ids=body.get("measure_ids"),
    )


def _h_reopen_measure(service: RemediationService, body: dict, measure_id: str):
    return service.reopen_measure(
        measure_id,
        actor=_require(body, "actor"),
        reason=_require(body, "reason"),
    )


def _h_run_reminders(service: RemediationService, body: dict):
    return service.run_reminders(
        today=body.get("date"),
        due_within_days=int(body.get("due_within_days", 7)),
    )


def build_router() -> _Router:
    router = _Router()
    router.add("GET", r"/health", lambda service, body: {"status": "ok"})
    router.add("POST", r"/cases", _h_create_case)
    router.add("GET", r"/cases", lambda service, body: {"cases": service.list_cases()})
    router.add("GET", r"/cases/(?P<case_id>[^/]+)", lambda s, b, case_id: s.case_detail(case_id))
    router.add("GET", r"/cases/(?P<case_id>[^/]+)/progress", lambda s, b, case_id: s.case_progress(case_id))
    router.add("GET", r"/cases/(?P<case_id>[^/]+)/decisions", lambda s, b, case_id: {"decisions": s.list_decisions(case_id)})
    router.add("POST", r"/cases/(?P<case_id>[^/]+)/close", _h_close_case)
    router.add("POST", r"/cases/(?P<case_id>[^/]+)/archive", _h_archive_case)
    router.add("POST", r"/cases/(?P<case_id>[^/]+)/reopen", _h_reopen_case)
    router.add("GET", r"/measures/(?P<measure_id>[^/]+)", lambda s, b, measure_id: s.measure_detail(measure_id))
    router.add("POST", r"/measures/(?P<measure_id>[^/]+)/evidence", _h_submit_evidence)
    router.add("POST", r"/measures/(?P<measure_id>[^/]+)/reviews", _h_review)
    router.add("POST", r"/measures/(?P<measure_id>[^/]+)/extensions", _h_request_extension)
    router.add("POST", r"/measures/(?P<measure_id>[^/]+)/alternatives", _h_propose_alternative)
    router.add("POST", r"/measures/(?P<measure_id>[^/]+)/reopen", _h_reopen_measure)
    router.add("POST", r"/extensions/(?P<extension_id>[^/]+)/decision", _h_decide_extension)
    router.add("POST", r"/alternatives/(?P<alt_id>[^/]+)/decision", _h_decide_alternative)
    router.add("POST", r"/jobs/reminders", _h_run_reminders)
    return router


def make_handler(service: RemediationService):
    router = build_router()

    class RemediationHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "RemediationLoop/0.1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def log_message(self, *args) -> None:  # 静默访问日志
            return

        def _dispatch(self, method: str) -> None:
            path = urlparse(self.path).path
            body: dict = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        return self._send(400, {"error": "请求体不是合法 JSON", "code": "bad_json"})
                    if not isinstance(body, dict):
                        return self._send(422, {"error": "请求体必须是 JSON 对象", "code": "validation_error"})
            handler, params = router.match(method, path)
            if handler is None:
                if params:
                    return self._send(405, {"error": "方法不允许", "code": "method_not_allowed", "allowed": params})
                return self._send(404, {"error": "接口不存在", "code": "not_found"})
            try:
                result = handler(service, body, **params)
                status, payload = result if isinstance(result, tuple) else (200, result)
                self._send(status, payload)
            except DomainError as exc:
                self._send(exc.status, {"error": str(exc), "code": exc.code, **exc.details})
            except Exception as exc:  # pragma: no cover - 兜底
                self._send(500, {"error": f"服务内部错误：{exc}", "code": "internal_error"})

        def _send(self, status: int, payload) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return RemediationHandler


def create_server(service: RemediationService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service))
