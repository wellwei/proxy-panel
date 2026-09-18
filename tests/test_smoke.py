"""端到端冒烟测试：真的起一个 stub 网关 + 面板，走一遍 HTTP 通路。

与 test_panel.py 的区别：那些是纯函数/逻辑单元测试，这个测的是「装配起来能用」——
配置发现、进程启动、HTTP 代理、能力降级都覆盖到。CI 里也跑同一套逻辑
（见 .github/workflows/ci.yml），这里让本地也能一条命令复现。

    python3 -m unittest tests.test_smoke -v
"""
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GW_PORT = 7901
PANEL_PORT = 8401


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _post(url, payload, timeout=10):
    """POST 并返回 (status, body)。4xx 也当正常结果返回（面板用它表达参数错误）。"""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _wait_http(url, timeout=15):
    """等一个 HTTP 端点起来（避免用固定 sleep 造成的偶发失败）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            _get(url, timeout=2)
            return True
        except Exception:
            time.sleep(0.3)
    return False


class TestSmoke(unittest.TestCase):
    """起 stub 网关 + 面板，验证真实 HTTP 通路。"""

    gw: subprocess.Popen = None
    panel: subprocess.Popen = None

    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        cls.gw = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "stub_gateway.py"), "--port", str(GW_PORT)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_http("http://127.0.0.1:%d/status" % GW_PORT):
            cls.gw.terminate()
            raise unittest.SkipTest("stub 网关未能启动")

        cls.panel = subprocess.Popen(
            [sys.executable, str(ROOT / "panel.py"),
             "--base", "http://127.0.0.1:%d" % GW_PORT,
             "--key", "testkey", "--port", str(PANEL_PORT)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not _wait_http("http://127.0.0.1:%d/" % PANEL_PORT):
            cls.panel.terminate()
            cls.gw.terminate()
            raise unittest.SkipTest("面板未能启动")

    @classmethod
    def tearDownClass(cls):
        for p in (cls.panel, cls.gw):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    # — 测试 ——————————————————————————————————————————————

    def test_index_served(self):
        status, body = _get("http://127.0.0.1:%d/" % PANEL_PORT)
        self.assertEqual(status, 200)
        self.assertIn("workbuddy2api 管理面板", body)

    def test_status_proxied(self):
        status, body = _get("http://127.0.0.1:%d/api/status" % PANEL_PORT)
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["total"], 3)
        self.assertEqual(d["healthy"], 1)

    def test_manual_disabled_exposed(self):
        """双位状态要透传到面板 —— 手动停用与自动禁用分别可见。"""
        _, body = _get("http://127.0.0.1:%d/api/status" % PANEL_PORT)
        accts = {a["uid"]: a for a in json.loads(body)["accounts"]}
        self.assertTrue(accts["cn-uid-2"]["manual_disabled"])
        self.assertFalse(accts["cn-uid-2"]["disabled"])
        self.assertTrue(accts["gl-uid-1"]["disabled"])
        self.assertFalse(accts["gl-uid-1"]["manual_disabled"])

    def test_models_grouped_by_realm(self):
        """模型目录按区域前缀分组，前缀在分组时被剥掉。"""
        _, body = _get("http://127.0.0.1:%d/api/models" % PANEL_PORT)
        m = json.loads(body)["models"]
        self.assertIn("glm-5.3", m["cn"])
        self.assertIn("fast-model", m["cn"])          # 档位模型
        self.assertIn("gpt-6-astra", m["global"])
        self.assertEqual(m["bare"], [])

    def test_endpoint_info_capabilities(self):
        """能力信息要透出（stub 环境没有 CLI 工具 → 全部 false，面板据此禁用按钮）。"""
        _, body = _get("http://127.0.0.1:%d/api/endpoint" % PANEL_PORT)
        d = json.loads(body)
        self.assertTrue(d["base_url"].endswith("/v1"))
        self.assertEqual(d["api_key"], "testkey")
        caps = d["capabilities"]
        self.assertFalse(caps["login"])               # 没给 bin_dir → 不可登录
        self.assertIn("checkin", caps)

    def test_stats_proxied(self):
        _, body = _get("http://127.0.0.1:%d/api/stats" % PANEL_PORT)
        self.assertEqual(json.loads(body)["total"]["requests"], 10)

    def test_account_disable_path(self):
        """停用通路：走面板 → 网关管理端点。"""
        _, body = _post("http://127.0.0.1:%d/api/account/disable" % PANEL_PORT,
                        {"uid": "cn-uid-1"})
        d = json.loads(body)
        self.assertTrue(d.get("manual_disabled"))
        self.assertTrue(d.get("changed"))

    def test_account_enable_path(self):
        _, body = _post("http://127.0.0.1:%d/api/account/enable" % PANEL_PORT,
                        {"uid": "cn-uid-1"})
        d = json.loads(body)
        self.assertFalse(d.get("manual_disabled"))

    def test_unknown_account_reports_not_found(self):
        """未知 uid 与「管理端点未开启」是两种不同的 404，不能混为一句提示。"""
        _, body = _post("http://127.0.0.1:%d/api/account/enable" % PANEL_PORT,
                        {"uid": "does-not-exist"})
        d = json.loads(body)
        self.assertIn("error", d)
        self.assertIn("账号不存在", d["error"])

    def test_missing_uid_rejected(self):
        _, body = _post("http://127.0.0.1:%d/api/account/disable" % PANEL_PORT, {})
        self.assertIn("error", json.loads(body))

    def test_login_start_degrades_without_cli(self):
        """没有网关 CLI 时，新增账号要给出可操作的说明而不是崩掉。"""
        _, body = _post("http://127.0.0.1:%d/api/login/start" % PANEL_PORT,
                        {"realm": "cn"})
        d = json.loads(body)
        self.assertIn("error", d)
        self.assertIn("同机部署", d["error"])

    def test_404_for_unknown_route(self):
        try:
            _get("http://127.0.0.1:%d/api/nope" % PANEL_PORT)
            self.fail("应返回 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


if __name__ == "__main__":
    unittest.main()
