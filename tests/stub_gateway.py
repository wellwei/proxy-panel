"""最小 stub 网关 —— 用于测试与 CI 冒烟。

不依赖真实 workbuddy2api，只按面板需要的形状返回：
  /status      账号池状态（--auth-dir 里的 workbuddy-*.json 也会并入，模拟网关热加载）
  /v1/models   模型目录（含区域前缀 + 一个档位模型）
  /v1/stats    请求统计
  /admin/...   管理端点（用于验证停用/恢复通路）

用法：
    python3 tests/stub_gateway.py --port 7999 [--auth-dir /tmp/auths]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# 响应里刻意包含：区域前缀、档位模型、一个被手动停用的账号、
# 一条 11102 限流台账（面板要把它过滤掉，不当限流显示）
STATUS = {
    "accounts": [
        {
            "uid": "cn-uid-1", "realm": "cn", "nickname": "CN 一号",
            "credits": 100, "disabled": False, "manual_disabled": False,
            "cooling": False, "success_count": 5, "consecutive_fails": 0,
            "in_flight": 0, "breaker_fails": 0,
        },
        {
            "uid": "cn-uid-2", "realm": "cn", "nickname": "CN 二号",
            "credits": 0, "disabled": False, "manual_disabled": True,
            "manual_reason": "面板停用", "cooling": False, "success_count": 0,
            "consecutive_fails": 0, "in_flight": 0, "breaker_fails": 0,
            "rate_limited_models": [
                {"model": "gpt-6-astra", "reason": "11102 model not available"},
            ],
        },
        {
            "uid": "gl-uid-1", "realm": "global", "nickname": "Global 一号",
            "credits": 50, "disabled": True, "disabled_reason": "12153 session dead",
            "manual_disabled": False, "cooling": False, "success_count": 2,
            "consecutive_fails": 0, "in_flight": 0, "breaker_fails": 0,
        },
    ],
    "total": 3, "healthy": 1, "cooling": 0, "disabled": 2,
    "in_flight_full": 0, "sticky_sessions": 0, "redis_mode": "noop",
    "cost_explore": {"events_total": 0, "per_model": {}},
    "realm_totals": {"cn": {"total": 2, "healthy": 1, "disabled": 1},
                     "global": {"total": 1, "healthy": 0, "disabled": 1}},
}

AUTH_DIR = ""  # 由 --auth-dir 注入：模拟网关热加载，目录里的凭据并入 /status


def _status() -> dict:
    """动态 /status：AUTH_DIR 里的 workbuddy-*.json 追加成账号条目。

    面板「贡献去重」靠 /status 内存态兜底（磁盘检查之外的一道）；测试预放一个
    auth 文件，stub 就得像真网关那样把它列出来，否则去重分支走不到。
    """
    st = json.loads(json.dumps(STATUS))
    if AUTH_DIR:
        known = {a["uid"] for a in st["accounts"]}
        for f in sorted(glob.glob(os.path.join(AUTH_DIR, "workbuddy-*.json"))):
            try:
                doc = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            acct = doc.get("account") or {}
            uid = acct.get("uid") or os.path.basename(f)[len("workbuddy-"):-len(".json")]
            if uid in known:
                continue
            known.add(uid)
            st["accounts"].append({
                "uid": uid, "realm": (doc.get("auth") or {}).get("realm") or "cn",
                "nickname": acct.get("nickname") or uid,
                "credits": 0, "disabled": False, "manual_disabled": False,
                "cooling": False, "success_count": 0, "consecutive_fails": 0,
                "in_flight": 0, "breaker_fails": 0,
            })
        st["total"] = len(st["accounts"])
    return st

MODELS = {
    "object": "list",
    "data": [
        {"id": "cn:glm-5.3", "object": "model", "owned_by": "workbuddy"},
        {"id": "cn:fast-model", "object": "model", "owned_by": "workbuddy"},
        {"id": "cn:deepseek-v4.1-flash", "object": "model", "owned_by": "workbuddy"},
        {"id": "global:gpt-6-astra", "object": "model", "owned_by": "workbuddy"},
        {"id": "global:primary-model", "object": "model", "owned_by": "workbuddy"},
    ],
}

STATS = {
    "enabled": True, "since": "2026-01-01T00:00:00Z", "now": "2026-01-01T01:00:00Z",
    "uptime_sec": 3600,
    "total": {"model": "total", "requests": 10, "success": 9, "failed": 1,
              "avg_latency_ms": 800, "total_tokens": 1234, "credit": 0.5},
    "models": [{"model": "cn:glm-5.3", "requests": 10, "success": 9, "failed": 1,
                "avg_latency_ms": 800, "total_tokens": 1234, "credit": 0.5}],
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/status":
            return self._send(200, _status())
        if path == "/v1/models":
            return self._send(200, MODELS)
        if path == "/v1/stats":
            return self._send(200, STATS)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if "/admin/accounts/" in path:
            parts = path.rstrip("/").split("/")
            uid, action = parts[-2], parts[-1]
            known = {a["uid"] for a in _status()["accounts"]}
            if uid not in known:
                return self._send(404, {"error": {"code": "not_found",
                                                  "message": "account not found: " + uid}})
            return self._send(200, {
                "uid": uid,
                "manual_disabled": action == "disable",
                "manual_reason": "stub" if action == "disable" else "",
                "disabled": False, "changed": True,
            })
        return self._send(404, {"error": "not found"})

    def log_message(self, *args):
        pass


def main(argv=None):
    global AUTH_DIR
    ap = argparse.ArgumentParser(description="wb2a-panel stub 网关")
    ap.add_argument("--port", type=int, default=7999)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--auth-dir", default="",
                    help="可选：该目录里的 workbuddy-*.json 会并入 /status（模拟网关热加载）")
    args = ap.parse_args(argv)
    AUTH_DIR = args.auth_dir
    print("stub gateway on http://%s:%d" % (args.host, args.port), flush=True)
    HTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
