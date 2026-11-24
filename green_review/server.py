"""仅依赖标准库的 JSON HTTP 接收服务。

运行：``python -m green_review serve --db data.json --host 127.0.0.1 --port 8765``
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import GreenReviewError
from .utils import _json_default


def _json_bytes(data: Any, status: int = 200) -> tuple[bytes, int, dict[str, str]]:
    body = json.dumps(data, ensure_ascii=False, indent=2, default=_json_default).encode("utf-8")
    return body, status, {"Content-Type": "application/json; charset=utf-8", "Content-Length": str(len(body))}


def create_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GreenReview/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 保持测试输出安静
            return

        def _send(self, data: Any, status: int = 200) -> None:
            body, code, headers = _json_bytes(data, status)
            self.send_response(code)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise GreenReviewError(f"请求体不是合法 JSON: {exc}") from exc
            if not isinstance(data, dict):
                raise GreenReviewError("请求体必须是 JSON 对象")
            return data

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                if parsed.path in {"/", "/health"}:
                    self._send({"ok": True, "service": "green-review", "time": app.now().isoformat()})
                elif parsed.path == "/api/dashboard":
                    self._send(app.dashboard(as_of=query.get("as_of"), site_id=query.get("site")))
                elif parsed.path == "/api/assess":
                    if not query.get("site"):
                        raise GreenReviewError("缺少 site 参数")
                    self._send(app.assess(query["site"], query.get("period"), as_of=query.get("as_of")))
                elif parsed.path.startswith("/api/decisions/"):
                    decision_id = parsed.path.rsplit("/", 1)[-1]
                    self._send(app.get_decision(decision_id))
                else:
                    self._send({"ok": False, "error": "not_found"}, 404)
            except GreenReviewError as exc:
                self._send({"ok": False, "error": type(exc).__name__, "message": str(exc)}, 400)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                body = self._body()
                if parsed.path == "/api/events":
                    source = body.pop("source", None)
                    if not source:
                        raise GreenReviewError("事件缺少 source")
                    payload = body.pop("payload", body)
                    result = app.ingest(source, payload, **body)
                    status = 200 if result.ok else 422
                    self._send(result.to_dict(), status)
                    return
                prefixes = {
                    "/api/portal": "portal_form",
                    "/api/attachments": "attachment_metadata",
                    "/api/factors": "public_factor",
                    "/api/authorizations": "authorization_verification",
                }
                source = prefixes.get(parsed.path)
                if source:
                    result = app.ingest(source, body)
                    self._send(result.to_dict(), 200 if result.ok else 422)
                    return
                if parsed.path.startswith("/api/decisions/") and parsed.path.endswith("/issue"):
                    parts = parsed.path.strip("/").split("/")
                    decision_id = parts[2]
                    result = app.issue_decision(decision_id, **body)
                    self._send({"ok": True, "decision": result})
                    return
                self._send({"ok": False, "error": "not_found"}, 404)
            except GreenReviewError as exc:
                self._send({"ok": False, "error": type(exc).__name__, "message": str(exc)}, 400)

    return Handler


def serve(app, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), create_handler(app))
    print(f"green-review listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
