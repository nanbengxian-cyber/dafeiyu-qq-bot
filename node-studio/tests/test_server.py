import json
import socket
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from http.cookies import SimpleCookie

from dafeiyu_flow.server import Handler, RunStore, SafeThreadingHTTPServer, SessionStore


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = SafeThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def request(self, method, path, body=None, content_type="application/json", headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=3)
        request_headers = dict(headers or {})
        if body is not None:
            request_headers.setdefault("Content-Type", content_type)
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        payload = response.read()
        headers_out = dict(response.getheaders())
        conn.close()
        return response.status, headers_out, payload

    def session_headers(self):
        status, headers, body = self.request("GET", "/api/session")
        self.assertEqual(200, status)
        token = json.loads(body)["csrfToken"]
        cookie = SimpleCookie(headers["Set-Cookie"])
        return {"Cookie": "dafeiyu_node_session=" + cookie["dafeiyu_node_session"].value,
                "X-Node-Studio-CSRF": token,
                "Origin": "http://127.0.0.1:%d" % self.port}

    def test_root_and_security_headers(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(200, status)
        self.assertIn("大肥鱼行为节点".encode("utf-8"), body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])
        self.assertEqual("same-origin", headers["Cross-Origin-Resource-Policy"])

    def test_descriptors_and_graph_list(self):
        status, _, body = self.request("GET", "/api/node-types")
        data = json.loads(body)
        self.assertEqual(200, status); self.assertEqual(12, len(data["nodeTypes"]))
        status, _, body = self.request("GET", "/api/graphs")
        self.assertEqual(3, len(json.loads(body)["graphs"]))

    def test_run_store_get_and_recorded_replay(self):
        headers = self.session_headers()
        status, _, body = self.request("GET", "/api/graphs/01-basic-chat.json")
        graph = json.loads(body)
        status, _, body = self.request("POST", "/api/runs", json.dumps({"graph": graph}).encode(), headers=headers)
        accepted = json.loads(body)
        self.assertEqual(202, status)
        status, _, body = self.request("GET", "/api/runs/" + accepted["runId"], headers=headers)
        record = json.loads(body)
        self.assertEqual(200, status); self.assertEqual("success", record["status"])
        self.assertFalse(record["result"][0]["outputs"]["result"]["sent"])
        status, _, body = self.request("POST", "/api/runs/%s/replay" % accepted["runId"],
                                       b'{"mode":"recorded"}', headers=headers)
        replay = json.loads(body)
        self.assertEqual(200, status); self.assertEqual(accepted["runId"], replay["runId"])

    def test_missing_csrf_and_foreign_origin_rejected(self):
        status, _, _ = self.request("POST", "/api/runs", b'{"graph":{}}')
        self.assertEqual(403, status)
        headers = self.session_headers(); headers["Origin"] = "https://example.invalid"
        status, _, _ = self.request("POST", "/api/runs", b'{"graph":{}}', headers=headers)
        self.assertEqual(403, status)

    def test_reject_wrong_content_type_after_auth(self):
        status, _, _ = self.request("POST", "/api/runs", b"{}", "text/plain", self.session_headers())
        self.assertEqual(422, status)

    def test_cross_session_run_isolation_and_exact_route(self):
        owner = self.session_headers(); other = self.session_headers()
        _, _, body = self.request("GET", "/api/graphs/01-basic-chat.json")
        status, _, body = self.request("POST", "/api/runs", json.dumps({"graph": json.loads(body)}).encode(), headers=owner)
        run_id = json.loads(body)["runId"]
        status, _, _ = self.request("GET", "/api/runs/" + run_id, headers=other)
        self.assertEqual(404, status)
        status, _, _ = self.request("POST", "/api/runs/%s/replay" % run_id, b'{"mode":"recorded"}', headers=other)
        self.assertEqual(404, status)
        status, _, _ = self.request("POST", "/api/runs/%s/extra/replay" % run_id, b'{}', headers=owner)
        self.assertEqual(404, status)

    def test_missing_origin_and_malformed_cookie_rejected_cleanly(self):
        headers = self.session_headers(); headers.pop("Origin")
        status, _, _ = self.request("POST", "/api/runs", b'{"graph":{}}', headers=headers)
        self.assertEqual(403, status)
        status, _, _ = self.request("GET", "/api/session", headers={"Cookie": 'bad="unterminated'})
        self.assertEqual(200, status)

    def test_store_copy_integrity_and_bounded_retention(self):
        store = RunStore(maximum=3, ttl=60)
        record = store.create("owner", {"version": 1}, {"status": "success", "trace": [{"x": 1}], "result": {"y": 2}, "error": None})
        fetched = store.get("owner", record["runId"])
        fetched["trace"][0]["x"] = 999
        self.assertEqual(1, store.get("owner", record["runId"])["trace"][0]["x"])
        for index in range(10):
            store.create("owner-%d" % index, {"version": 1}, {"status": "success", "trace": [], "result": {}, "error": None})
        self.assertLessEqual(len(store._runs), 3)

    def test_session_exact_cap_and_lru_eviction(self):
        sessions = SessionStore(maximum=3, ttl=60)
        ids = [sessions.get_or_create(None)[0] for _ in range(3)]
        self.assertEqual(3, len(sessions._sessions))
        self.assertTrue(all(sessions.csrf(sid) for sid in ids))
        sessions.get_or_create(None)
        self.assertEqual(3, len(sessions._sessions))
        self.assertIsNone(sessions.csrf(ids[0]))

    def test_http_worker_limit_rejects_excess_connection(self):
        server = SafeThreadingHTTPServer(("127.0.0.1", 0), Handler, maximum_workers=1)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        first = socket.create_connection(server.server_address, timeout=2)
        first.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        deadline = time.time() + 2
        while server.active_workers != 1 and time.time() < deadline: time.sleep(0.01)
        second = socket.create_connection(server.server_address, timeout=2)
        second.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        try:
            second.settimeout(2)
            response = b""
            try:
                while b"\r\n\r\n" not in response:
                    chunk = second.recv(256)
                    if not chunk:
                        break
                    response += chunk
            except (ConnectionAbortedError, ConnectionResetError):
                # Windows may surface a close immediately after the complete response as WSAECONNABORTED.
                pass
            self.assertIn(b"503 Service Unavailable", response)
        finally:
            first.close(); second.close(); server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_unknown_run_and_path_traversal(self):
        status, _, _ = self.request("GET", "/api/runs/run-missing")
        self.assertEqual(404, status)
        status, _, _ = self.request("GET", "/static/%2e%2e/%2e%2e/etc/passwd")
        self.assertEqual(404, status)


if __name__ == "__main__": unittest.main()
