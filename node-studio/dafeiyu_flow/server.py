from __future__ import annotations

import argparse
import copy
import hashlib
import json
import mimetypes
import secrets
import socket
import threading
import time
import uuid
from collections import OrderedDict
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from .engine import Engine, GraphError, _reject_json_constant
from .registry import build_registry

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "dafeiyu_flow" / "static"
GRAPHS = ROOT / "graphs"
MAX_BODY = 1024 * 1024
MAX_RUNS_PER_SESSION = 100
MAX_RUNS_TOTAL = 512
RUN_TTL_SECONDS = 3600.0
MAX_SESSIONS = 256
SESSION_TTL_SECONDS = 3600.0
MAX_HTTP_WORKERS = 32
SOCKET_TIMEOUT_SECONDS = 5.0
SESSION_COOKIE = "dafeiyu_node_session"
REGISTRY = build_registry()
ENGINE = Engine(REGISTRY)
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


class SessionStore:
    def __init__(self, maximum: int = MAX_SESSIONS, ttl: float = SESSION_TTL_SECONDS) -> None:
        self._sessions = OrderedDict()  # type: OrderedDict[str, Dict[str, Any]]
        self._lock = threading.Lock()
        self._maximum = maximum
        self._ttl = ttl

    def _prune_expired(self, now: float) -> None:
        expired = [sid for sid, record in self._sessions.items() if now - record["last"] > self._ttl]
        for sid in expired:
            self._sessions.pop(sid, None)

    def get_or_create(self, session_id: Optional[str]) -> tuple:
        now = time.monotonic()
        with self._lock:
            self._prune_expired(now)
            record = self._sessions.get(session_id) if session_id else None
            if record:
                record["last"] = now
                self._sessions.move_to_end(session_id)
                return session_id, record["csrf"], False
            while len(self._sessions) >= self._maximum:
                self._sessions.popitem(last=False)
            session_id = secrets.token_urlsafe(24)
            token = secrets.token_urlsafe(32)
            self._sessions[session_id] = {"csrf": token, "last": now}
            return session_id, token, True

    def csrf(self, session_id: Optional[str]) -> Optional[str]:
        if not session_id:
            return None
        now = time.monotonic()
        with self._lock:
            self._prune_expired(now)
            record = self._sessions.get(session_id)
            if not record:
                return None
            record["last"] = now
            self._sessions.move_to_end(session_id)
            return record["csrf"]


class RunStore:
    def __init__(self, maximum: int = MAX_RUNS_TOTAL, ttl: float = RUN_TTL_SECONDS) -> None:
        self._runs = OrderedDict()  # type: OrderedDict[str, Dict[str, Any]]
        self._lock = threading.Lock()
        self._maximum = maximum
        self._ttl = ttl

    def _prune(self, now: float) -> None:
        expired = [run_id for run_id, item in self._runs.items() if now - item["monotonicCreated"] > self._ttl]
        for run_id in expired:
            self._runs.pop(run_id, None)
        while len(self._runs) > self._maximum:
            self._runs.popitem(last=False)

    @staticmethod
    def _digest(record: Dict[str, Any]) -> str:
        unsigned = {key: value for key, value in record.items() if key != "integrityHash"}
        payload = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def create(self, owner: str, graph: Dict[str, Any], result: Dict[str, Any], replay_of: Optional[str] = None) -> Dict[str, Any]:
        graph_copy = _clone(graph)
        run_id = "run-" + uuid.uuid4().hex
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        normalized = json.dumps(graph_copy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record = {
            "runId": run_id, "owner": owner, "status": result["status"],
            "graphHash": "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "startedAt": now, "completedAt": now, "trace": _clone(result.get("trace", [])),
            "result": _clone(result.get("result")), "error": result.get("error"),
            "graph": graph_copy, "replayOf": replay_of, "monotonicCreated": time.monotonic(),
        }
        record["integrityHash"] = self._digest(record)
        with self._lock:
            self._runs[run_id] = record
            owner_ids = [key for key, item in self._runs.items() if item["owner"] == owner]
            for old_id in owner_ids[:-MAX_RUNS_PER_SESSION]:
                self._runs.pop(old_id, None)
            self._prune(time.monotonic())
        return self.get(owner, run_id) or {}

    def get(self, owner: str, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            self._prune(time.monotonic())
            record = self._runs.get(run_id)
            if not record or record["owner"] != owner or record.get("integrityHash") != self._digest(record):
                return None
            self._runs.move_to_end(run_id)
            return _clone(record)


SESSIONS = SessionStore()
RUNS = RunStore()


def graph_list():
    result = []
    for path in sorted(GRAPHS.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
            result.append({"file": path.name, "name": str(raw.get("name", path.stem))})
        except (OSError, ValueError):
            pass
    return result


class SafeThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, server_address, handler_class, maximum_workers: int = MAX_HTTP_WORKERS):
        self._worker_slots = threading.BoundedSemaphore(maximum_workers)
        self._active_workers = 0
        self._active_lock = threading.Lock()
        super().__init__(server_address, handler_class)

    @property
    def active_workers(self) -> int:
        with self._active_lock:
            return self._active_workers

    def process_request(self, request, client_address) -> None:
        if not self._worker_slots.acquire(False):
            try:
                body = b'{"error":"server busy","code":"SERVER_BUSY"}'
                response = (b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                            b"Content-Type: application/json\r\nContent-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
                request.sendall(response)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        with self._active_lock:
            self._active_workers += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release_worker()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_worker()

    def _release_worker(self) -> None:
        with self._active_lock:
            self._active_workers -= 1
        self._worker_slots.release()

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(SOCKET_TIMEOUT_SECONDS)
        return request, client_address


class Handler(BaseHTTPRequestHandler):
    server_version = "DafeiyuNodeStudio/0.3"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("[studio] " + fmt % args)

    def _headers(self, status: int, content_type: str, length: int, cookie: Optional[str] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        if cookie:
            self.send_header("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (SESSION_COOKIE, cookie))
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()

    def json_response(self, payload: Any, status: int = 200, cookie: Optional[str] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), cookie)
        self.wfile.write(body)

    def error_json(self, message: str, status: int = 400, code: str = "REQUEST_ERROR") -> None:
        self.json_response({"error": message, "code": code}, status)

    def _session_id(self) -> Optional[str]:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _expected_origin(self) -> str:
        return "http://" + self.headers.get("Host", "")

    def authenticated_session(self, require_origin: bool = False) -> Optional[str]:
        host = self.headers.get("Host", "")
        accepted = {"127.0.0.1:%d" % self.server.server_port, "localhost:%d" % self.server.server_port}
        if host not in accepted:
            self.error_json("Host 不受信任", 403, "HOST_REJECTED")
            return None
        if require_origin and self.headers.get("Origin") != self._expected_origin():
            self.error_json("Origin 不受信任或缺失", 403, "ORIGIN_REJECTED")
            return None
        session_id = self._session_id()
        expected = SESSIONS.csrf(session_id)
        supplied = self.headers.get("X-Node-Studio-CSRF")
        if expected is None or supplied is None or not secrets.compare_digest(expected, supplied):
            self.error_json("CSRF 校验失败", 403, "CSRF_REJECTED")
            return None
        return session_id

    def read_json(self) -> Dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise GraphError("只接受 application/json", "UNSUPPORTED_MEDIA")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise GraphError("Content-Length 无效")
        if length <= 0 or length > MAX_BODY:
            raise GraphError("请求为空或超过 1 MiB", "BODY_LIMIT")
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise GraphError("请求体不完整", "INCOMPLETE_BODY")
            data = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except socket.timeout:
            raise GraphError("请求体读取超时", "BODY_TIMEOUT")
        except (UnicodeDecodeError, ValueError) as exc:
            if isinstance(exc, GraphError):
                raise
            raise GraphError("JSON 格式错误")
        if not isinstance(data, dict):
            raise GraphError("请求必须是 JSON 对象")
        return data

    def public_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {key: value for key, value in record.items() if key not in {"owner", "graph", "integrityHash", "monotonicCreated"}}

    def serve_file(self, path: Path) -> None:
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            self.error_json("未找到", 404, "NOT_FOUND"); return
        if STATIC not in resolved.parents and resolved != STATIC:
            self.error_json("未找到", 404, "NOT_FOUND"); return
        body = resolved.read_bytes()
        ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._headers(200, ctype, len(body)); self.wfile.write(body)

    def do_GET(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/": return self.serve_file(STATIC / "index.html")
        if path == "/api/session":
            sid, token, created = SESSIONS.get_or_create(self._session_id())
            return self.json_response({"csrfToken": token, "limits": {"maxNodes": 200, "maxEdges": 1000, "maxRequestBytes": MAX_BODY}}, cookie=sid if created else None)
        if path == "/api/node-types": return self.json_response({"nodeTypes": REGISTRY.describe()})
        if path == "/api/graphs": return self.json_response({"graphs": graph_list()})
        parts = path.split("/")
        if len(parts) == 4 and parts[1:3] == ["api", "runs"] and parts[3]:
            sid = self._session_id()
            record = RUNS.get(sid or "", parts[3])
            if not record: return self.error_json("运行记录不存在", 404, "RUN_NOT_FOUND")
            return self.json_response(self.public_record(record))
        if path.startswith("/api/graphs/"):
            name = path.rsplit("/", 1)[-1]
            if not name.endswith(".json") or Path(name).name != name: return self.error_json("图不存在", 404, "NOT_FOUND")
            try:
                graph = json.loads((GRAPHS / name).read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
                ENGINE.validate(graph)
            except (OSError, ValueError, GraphError): return self.error_json("图不存在或无效", 404, "NOT_FOUND")
            return self.json_response(graph)
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if ".." in Path(rel).parts or not rel: return self.error_json("未找到", 404, "NOT_FOUND")
            return self.serve_file(STATIC / rel)
        self.error_json("未找到", 404, "NOT_FOUND")

    def do_POST(self) -> None:
        path = unquote(urlparse(self.path).path)
        sid = self.authenticated_session(require_origin=True)
        if not sid: return
        try:
            payload = self.read_json()
            if path in ("/api/validate", "/api/graphs/validate"):
                graph = payload.get("graph")
                if not isinstance(graph, dict): raise GraphError("缺少 graph 对象")
                return self.json_response({"valid": True, "executionOrder": ENGINE.validate(graph)})
            if path in ("/api/run", "/api/runs"):
                graph = payload.get("graph")
                if not isinstance(graph, dict): raise GraphError("缺少 graph 对象")
                result = ENGINE.execute(graph)
                if result.get("status") != "success": return self.json_response({"error": result.get("error"), "code": "RUN_FAILED", "trace": result.get("trace", [])}, 422)
                record = RUNS.create(sid, graph, result)
                return self.json_response({"runId": record["runId"], "status": record["status"]}, 202)
            parts = path.split("/")
            if len(parts) == 5 and parts[1:3] == ["api", "runs"] and parts[3] and parts[4] == "replay":
                record = RUNS.get(sid, parts[3])
                if not record: return self.error_json("运行记录不存在", 404, "RUN_NOT_FOUND")
                mode = payload.get("mode", "recorded")
                if mode == "recorded": return self.json_response(self.public_record(record))
                if mode == "reexecute":
                    result = ENGINE.execute(record["graph"])
                    replay = RUNS.create(sid, record["graph"], result, parts[3])
                    return self.json_response({"runId": replay["runId"], "status": replay["status"], "replayOf": parts[3]}, 202)
                raise GraphError("回放模式只能是 recorded 或 reexecute", "INVALID_REPLAY_MODE")
            self.error_json("未找到", 404, "NOT_FOUND")
        except GraphError as exc:
            self.json_response({"error": str(exc), "code": exc.code, "nodeId": exc.node_id}, 422)
        except Exception:
            self.error_json("执行失败，服务端已隐藏内部细节", 500, "INTERNAL_ERROR")


def main() -> int:
    parser = argparse.ArgumentParser(description="大肥鱼行为节点离线 Studio")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = SafeThreadingHTTPServer((args.host, args.port), Handler)
    print("大肥鱼行为节点 Studio: http://%s:%d" % (args.host, args.port))
    print("仅离线预览：不连接 QQ、AstrBot、Docker 或生产机器人。按 Ctrl+C 停止。")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
