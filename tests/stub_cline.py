"""cline2api stub 网关 —— 用于测试面板的 cline 子面板。

按 cline2api 的真实形状返回：
  /status                            账号池 + 闸门台账（admin_token）
  /v1/models                         闸门放行的模型（api_key）
  /admin/login/start|poll|cancel     设备授权登录代理
  /admin/models/{id}/{enable,disable} 手动启停（id 含 / 与 :，必须 URL 转义）
  /admin/accounts/{id}/{enable,disable}
  /admin/gate/recheck

鉴权也照真实网关分两套（admin_token vs api_key），这样面板里用错 token 的
情况能被测出来，而不是等到生产才发现。

用法：
    python3 tests/stub_cline.py --port 7998
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

ADMIN_TOKEN = "stub-admin"
API_KEY = "stub-key"

STATUS = {
    "version": "stub",
    "uptime_seconds": 120,
    "accounts_total": 2,
    "accounts_available": 1,
    "accounts": [
        {"accountId": "acc_1", "email": "abc***@gmail.com", "status": "active",
         "requestsTotal": 12, "requestsToday": 3, "tokensTotal": 4567,
         "tokensToday": 100, "createdAt": "2026-09-20T00:00:00Z"},
        {"accountId": "acc_2", "email": "def***@qq.com", "status": "cooldown",
         "manualDisabled": True, "lastReason": "429: Try again in 17h 59m",
         "cooldownUntil": "2026-09-21T00:00:00Z",
         "requestsTotal": 5, "requestsToday": 1, "tokensTotal": 900,
         "createdAt": "2026-09-20T00:00:00Z"},
    ],
    "models": [
        {"upstream": "z-ai/glm-5.3-flash", "bare": "glm-5.3-flash", "group": "free",
         "state": "free", "exposed": True, "last_cost": 0, "cost_total": 0,
         "requests": 3, "last_observed": "2026-09-20T01:00:00Z", "probe_streak": 0},
        {"upstream": "anthropic/claude-opus-5", "bare": "claude-opus-5",
         "group": "recommended", "state": "disabled", "exposed": False,
         "last_cost": 0.0001, "cost_total": 0.0001, "requests": 1,
         "last_observed": "2026-09-20T01:00:00Z",
         "disable_reason": "probe cost=0.0001 billing=true", "probe_streak": 0},
    ],
    "catalog": {"last_sync": "2026-09-20T01:00:00Z", "error": ""},
}

MODELS = {
    "object": "list",
    "data": [{"id": "glm-5.3-flash", "object": "model", "owned_by": "cline"}],
}

# 被面板操作过的模型 id 记在这里，测试据此断言转义与转发都正确
TOGGLED: list = []


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self, want: str) -> bool:
        h = self.headers.get("Authorization") or ""
        return h == "Bearer " + want

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        if path == "/healthz":
            return self._send(200, {"ok": True, "version": "stub"})
        if path == "/__toggled":
            # 仅测试用：把收到的启停请求回给测试进程（跨进程断言转义是否正确）
            return self._send(200, {"toggled": TOGGLED})
        if path == "/status":
            if not self._auth_ok(ADMIN_TOKEN):
                return self._send(401, {"error": {"message": "invalid admin token",
                                                  "type": "unauthorized"}})
            return self._send(200, STATUS)
        if path == "/v1/models":
            if not self._auth_ok(API_KEY):
                return self._send(401, {"error": {"message": "invalid api key",
                                                  "type": "unauthorized"}})
            return self._send(200, MODELS)
        if path == "/admin/login/poll":
            code = (q.get("device_code") or [""])[0]
            if code != "dev-1":
                return self._send(404, {"error": {"message": "unknown device_code"}})
            return self._send(200, {"state": "done", "email": "abc***@gmail.com",
                                    "account_id": "acc_1"})
        return self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._auth_ok(ADMIN_TOKEN):
            return self._send(401, {"error": {"message": "invalid admin token",
                                              "type": "unauthorized"}})
        if path == "/admin/login/start":
            return self._send(200, {"device_code": "dev-1", "user_code": "ABCD-1234",
                                    "verify_url": "https://cline.bot/device?code=ABCD-1234",
                                    "expires_in": 300})
        if path == "/admin/login/cancel":
            return self._send(200, {"ok": True})
        if path == "/admin/gate/recheck":
            return self._send(200, {"ok": True, "started": True})
        if "/admin/models/" in path:
            rest = path[len("/admin/models/"):]
            quoted, _, action = rest.rpartition("/")
            TOGGLED.append((urllib.parse.unquote(quoted), action))
            return self._send(200, {"ok": True, "model": urllib.parse.unquote(quoted),
                                    "enabled": action == "enable"})
        if "/admin/accounts/" in path:
            rest = path[len("/admin/accounts/"):]
            quoted, _, action = rest.rpartition("/")
            return self._send(200, {"ok": True, "account": urllib.parse.unquote(quoted),
                                    "enabled": action == "enable"})
        return self._send(404, {"error": {"message": "not found"}})

    def log_message(self, *args):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="cline2api stub 网关")
    ap.add_argument("--port", type=int, default=7998)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    print("stub cline2api on http://%s:%d" % (args.host, args.port), flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
