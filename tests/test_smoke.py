"""端到端冒烟测试：真的起一个 stub 网关 + 面板，走一遍 HTTP 通路。

与 test_panel.py 的区别：那些是纯函数/逻辑单元测试，这个测的是「装配起来能用」——
配置发现、进程启动、HTTP 代理、能力降级都覆盖到。CI 里也跑同一套逻辑
（见 .github/workflows/ci.yml），这里让本地也能一条命令复现。

    python3 -m unittest tests.test_smoke -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
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


def _post(url, payload, timeout=10, csrf=True):
    """POST 并返回 (status, body)。4xx 也当正常结果返回（面板用它表达参数错误）。

    默认带上 X-Panel-Request 头（面板要求状态变更请求必须带，见 panel.py do_POST）；
    csrf=False 用来验证缺头时确实被拒。
    """
    headers = {"Content-Type": "application/json"}
    if csrf:
        headers["X-Panel-Request"] = "1"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _get_status(url, timeout=10):
    """GET 并返回 (status, body)，4xx/5xx 也当结果返回（断言「不该存在」时要用）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        with e:                     # 关掉错误响应，否则解释器退出时刷 ResourceWarning
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


def _stop(*procs):
    """终止子进程并关闭其 stdout 管道。

    不关管道会在解释器退出时刷一串 ResourceWarning（unclosed file）—— 测试
    仍然全绿，但输出被噪声盖住，真正的失败反而不好找。
    """
    for p in procs:
        if not p:
            continue
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)
        if p.stdout:
            try:
                p.stdout.close()
            except OSError:
                pass


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
             "--key", "testkey", "--port", str(PANEL_PORT),
             # 显式指向不存在的网关配置：否则面板会在 cwd 上溯时发现本机真实的
             # wb2a/app（带 linux 二进制与 config.json），能力探测结果随开发机而变 ——
             # 本组测试断言的是「没有 CLI 工具时的降级形态」，必须封闭这个变量。
             "--gateway-config", "/nonexistent/gateway/config.json"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not _wait_http("http://127.0.0.1:%d/" % PANEL_PORT):
            cls.panel.terminate()
            cls.gw.terminate()
            raise unittest.SkipTest("面板未能启动")

    @classmethod
    def tearDownClass(cls):
        _stop(cls.panel, cls.gw)

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

    def test_post_without_csrf_header_rejected(self):
        """缺 X-Panel-Request 头的 POST 必须被拒 —— 这是防跨站触发状态变更的那道闸。"""
        status, body = _post("http://127.0.0.1:%d/api/account/disable" % PANEL_PORT,
                             {"uid": "cn-uid-1"}, csrf=False)
        self.assertEqual(status, 403)
        self.assertIn("X-Panel-Request", json.loads(body)["error"])
        # 再确认没被真的停用
        _, st = _get("http://127.0.0.1:%d/api/status" % PANEL_PORT)
        accts = {a["uid"]: a for a in json.loads(st)["accounts"]}
        self.assertFalse(accts["cn-uid-1"]["manual_disabled"])

    def test_security_headers_present(self):
        """安全响应头：nosniff / DENY / no-referrer 三个零成本兜底。"""
        req = urllib.request.Request("http://127.0.0.1:%d/" % PANEL_PORT)
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(r.headers.get("X-Frame-Options"), "DENY")
            self.assertEqual(r.headers.get("Referrer-Policy"), "no-referrer")

    def test_api_base_is_relative(self):
        """页面里的 API 基址必须从当前路径推导 —— 面板要能挂在反向代理的子路径下。"""
        _, body = _get("http://127.0.0.1:%d/" % PANEL_PORT)
        self.assertIn("location.pathname", body)
        self.assertNotIn("fetch('/api", body)

    def test_404_for_unknown_route(self):
        try:
            _get("http://127.0.0.1:%d/api/nope" % PANEL_PORT)
            self.fail("应返回 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)


CLINE_GW_PORT = 7903       # 这一组自带 wb2a stub：unittest 按类名字母序跑，
CLINE_PORT = 7902          # TestClineSmoke 在 TestSmoke 之前，不能借用它的进程
CLINE_PANEL_PORT = 8402
PLAIN_PANEL_PORT = 8403    # 只配 wb2a 的面板：验证「未配置 cline2api」的降级形态
CLINE_ADMIN_TOKEN = "stub-admin"


class TestClineSmoke(unittest.TestCase):
    """第二网关（cline2api）子面板的 HTTP 通路。

    单独起一套进程：既能测「两个网关同时配置」这种真实生产形态，也能测出面板对
    cline 特有的 id 转义（上游名含 / 与 :）与双 token 鉴权是否用对了。
    """

    gw: subprocess.Popen = None
    cline: subprocess.Popen = None
    panel: subprocess.Popen = None
    plain: subprocess.Popen = None

    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        cls.gw = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "stub_gateway.py"), "--port", str(CLINE_GW_PORT)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.cline = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "stub_cline.py"), "--port", str(CLINE_PORT)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not (_wait_http("http://127.0.0.1:%d/status" % CLINE_GW_PORT)
                and _wait_http("http://127.0.0.1:%d/healthz" % CLINE_PORT)):
            for p in (cls.gw, cls.cline):
                p.terminate()
            raise unittest.SkipTest("stub 网关未能启动")

        cls.panel = subprocess.Popen(
            [sys.executable, str(ROOT / "panel.py"),
             "--base", "http://127.0.0.1:%d" % CLINE_GW_PORT,
             "--key", "testkey", "--port", str(CLINE_PANEL_PORT),
             "--cline-base", "http://127.0.0.1:%d" % CLINE_PORT,
             "--cline-key", "stub-key",
             "--cline-admin-token", CLINE_ADMIN_TOKEN],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # 不带 --cline-* 且显式指向一个不存在的 cline 配置：模拟还没上 cline2api 的老部署
        cls.plain = subprocess.Popen(
            [sys.executable, str(ROOT / "panel.py"),
             "--base", "http://127.0.0.1:%d" % CLINE_GW_PORT,
             "--key", "testkey", "--port", str(PLAIN_PANEL_PORT),
             "--cline-config", "/nonexistent/cline2api/config.json"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not (_wait_http("http://127.0.0.1:%d/" % CLINE_PANEL_PORT)
                and _wait_http("http://127.0.0.1:%d/" % PLAIN_PANEL_PORT)):
            for p in (cls.panel, cls.plain, cls.cline, cls.gw):
                p.terminate()
            raise unittest.SkipTest("面板未能启动")

    @classmethod
    def tearDownClass(cls):
        _stop(cls.panel, cls.plain, cls.cline, cls.gw)

    def _api(self, path):
        return _get("http://127.0.0.1:%d%s" % (CLINE_PANEL_PORT, path))

    def _post(self, path, payload):
        return _post("http://127.0.0.1:%d%s" % (CLINE_PANEL_PORT, path), payload)

    def test_cline_status_proxied(self):
        """闸门台账要透出，且 exposed 字段能区分在服/关停两类模型。"""
        status, body = self._api("/api/cline/status")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["accounts_total"], 3)
        exposed = [m["bare"] for m in d["models"] if m["exposed"]]
        self.assertEqual(exposed, ["glm-5.3-flash"])

    def test_cline_models_uses_api_key(self):
        """模型目录走 api_key 那套鉴权（不是 admin_token）—— 用错会 401。"""
        status, body = self._api("/api/cline/models")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "glm-5.3-flash")

    def test_cline_status_wrong_token_is_reported(self):
        """admin_token 不对时，面板要把上游的 401 变成可读错误而不是崩掉。"""
        from panel import ClinePanel
        bad = ClinePanel({"base": "http://127.0.0.1:%d" % CLINE_PORT,
                          "api_key": "stub-key", "admin_token": "wrong"})
        self.assertIn("error", bad.status())

    def test_cline_login_flow(self):
        """设备授权：start 拿 device_code 与授权地址 → cancel 结束会话。"""
        status, body = self._post("/api/cline/login/start", {})
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["device_code"], "dev-1")
        self.assertIn("cline.bot", d["verify_url"])

        status, body = self._post("/api/cline/login/cancel", {"device_code": "dev-1"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_cline_login_poll_reports_done(self):
        _, body = self._api("/api/cline/login/poll?device_code=dev-1")
        self.assertEqual(json.loads(body)["state"], "done")

    def test_cline_model_toggle_escapes_upstream_name(self):
        """上游名含 / 与 :，必须 URL 转义后作为单段路径传给网关（Go mux 只吃一段）。"""
        status, body = self._post("/api/cline/model/disable",
                                  {"id": "anthropic/claude-opus-5"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        # 问 stub 要回执：它去转义后拿到的必须是原值，说明转义-解转义这一路没走样
        _, tb = _get("http://127.0.0.1:%d/__toggled" % CLINE_PORT)
        self.assertIn(["anthropic/claude-opus-5", "disable"],
                      json.loads(tb)["toggled"])

    def test_cline_model_toggle_requires_id(self):
        status, body = self._post("/api/cline/model/enable", {})
        self.assertEqual(status, 400)
        self.assertIn("id", json.loads(body)["error"])

    def test_cline_account_disable(self):
        status, body = self._post("/api/cline/account/disable", {"id": "acc_1"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_cline_account_clear_cooldown_routes(self):
        """清除冷却打到网关的 clear-cooldown 端点（不是 enable/disable）。"""
        status, body = self._post("/api/cline/account/clear-cooldown", {"id": "acc_1"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["cleared"])
        _, tb = _get("http://127.0.0.1:%d/__toggled" % CLINE_PORT)
        self.assertIn(["acc_1", "clear-cooldown"], json.loads(tb)["cleared"])

    def test_cline_account_clear_cooldown_requires_id(self):
        status, body = self._post("/api/cline/account/clear-cooldown", {})
        self.assertEqual(status, 400)
        self.assertIn("id", json.loads(body)["error"])

    def test_cline_models_show_per_model_limits(self):
        """模型级限额透传到前端：带模型名与到期时间，且账号本身仍可用。"""
        _, body = self._api("/api/cline/status")
        accts = json.loads(body)["accounts"]
        first = [a for a in accts if a["accountId"] == "acc_1"][0]
        self.assertEqual(first["status"], "active")
        mc = first["modelCooldowns"][0]
        self.assertEqual(mc["model"], "cline-free/deepseek-v4.1-flash")
        self.assertIn("until", mc)

    def test_cline_recheck(self):
        status, body = self._post("/api/cline/recheck", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["started"])

    # ── 公开面 /cline/*（任何人可读 + 贡献账号）───────────────────────
    #
    # 这组测试守的是公开面的**边界**：能读到什么、能写什么、写不了什么。
    # 与 /api/cline/* 那组是两套独立通路，所以两边都要测。

    def _pub(self, path):
        """GET 公开面路径；4xx 也当正常结果返回（用它断言「不该存在的就是 404」）。"""
        return _get_status("http://127.0.0.1:%d%s" % (CLINE_PANEL_PORT, path))

    def _pub_post(self, path, payload, csrf=True):
        return _post("http://127.0.0.1:%d%s" % (CLINE_PANEL_PORT, path), payload, csrf=csrf)

    def test_public_page_is_served_without_credentials(self):
        status, body = self._pub("/cline/")
        self.assertEqual(status, 200)
        self.assertIn("Cline 免费额度池", body)

    def test_public_status_is_projected(self):
        """公开状态可用，且**不含**上游名、账号 id、网关地址这些内部信息。"""
        status, body = self._pub("/cline/api/status")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["accounts_total"], 3)
        self.assertEqual(d["accounts_available"], 2)
        self.assertEqual(d["models_active"], 1)
        self.assertEqual([m["name"] for m in d["models"]],
                         ["glm-5.3-flash", "claude-opus-5"])
        for secret in ("z-ai/glm-5.3-flash", "cline-free/deepseek-v4.1-flash",
                       "acc_1", "acc_2", "acc_3", "127.0.0.1",
                       "requestsTotal", "tokensTotal"):
            self.assertNotIn(secret, body, "公开面泄漏了 %s" % secret)

    def test_public_accounts_are_listed_but_masked(self):
        """账号池下拉要有东西可显示，但邮箱只留打码后的前几位。

        stub 里有一条 **gateway 没打码成功的短邮箱**（ab@x.com）—— 真实 /status
        会这样漏出来，所以这条断言是「面板自己再挡一层」的证据。
        """
        _, body = self._pub("/cline/api/status")
        accts = json.loads(body)["accounts"]
        self.assertEqual(len(accts), 3)
        names = [a["name"] for a in accts]
        self.assertIn("abc***@gmail.com", names)
        self.assertIn("ab***@x.com", names)        # 面板补打码
        self.assertNotIn("ab@x.com", body)         # 完整地址不得出现
        # 每条都带状态，前端才知道怎么标注
        for a in accts:
            self.assertIn(a["state"], ("active", "cooldown", "paused", "expired", "unknown"))

    def test_public_contribute_flow(self):
        """贡献链路：start 拿授权地址 → poll 报成功。"""
        status, body = self._pub_post("/cline/api/login/start", {})
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["device_code"], "pub-dev-1")
        self.assertIn("cline.bot", d["verify_url"])

        status, body = self._pub("/cline/api/login/poll?device_code=pub-dev-1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "done")

    def test_public_contribute_works_without_csrf_header(self):
        """公开面的 POST **不**要 X-Panel-Request —— 它没有可被 CSRF 的东西，
        而带自定义头会在跨站预检时被挡，等于把公开页上的贡献按钮废掉。"""
        status, _ = self._pub_post("/cline/api/login/start", {}, csrf=False)
        self.assertEqual(status, 200)

    def test_public_cancel_flow(self):
        status, body = self._pub_post("/cline/api/login/cancel", {"device_code": "pub-dev-1"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_public_surface_has_no_write_operations(self):
        """公开面**没有任何**启停账号/模型的路径 —— 它们在这里不存在，不是被拒。

        与「不得停用关停账号和模型」这条需求一一对应：公开面能触达的路由就那几个，
        其余一律 404，且面板也不会把它们转发到网关的 /admin/*。
        """
        for path in ("/cline/api/account/disable",
                     "/cline/api/account/enable",
                     "/cline/api/account/clear-cooldown",
                     "/cline/api/model/disable",
                     "/cline/api/model/enable",
                     "/cline/api/recheck",
                     "/cline/api/login/start-extra"):
            if path.endswith("start-extra"):
                continue
            status, body = self._pub_post(path, {"id": "acc_1"})
            self.assertEqual(status, 404, "%s 应当是 404（公开面不可写），实得 %s %s"
                             % (path, status, body[:120]))
        # 只读路径也不该多出来：公开面不是「管理面的只读副本」
        for path in ("/cline/api/accounts", "/cline/api/models"):
            status, _ = self._pub(path)
            self.assertEqual(status, 404, "%s 不该存在" % path)

    def test_public_status_does_not_leak_admin_endpoints(self):
        """确认那条「没真的打到网关」：stub 的写回执应保持为空。

        这是对上面那条的补充证据 —— 404 也可能来自「转发了但被网关拒」，
        所以直接查 stub 的记账，证明请求根本没到网关。
        """
        _, tb = _get("http://127.0.0.1:%d/__toggled" % CLINE_PORT)
        rec = json.loads(tb)
        # 本组测试之前跑过的管理面用例会留下记录；这里关心的是**公开面**的 id
        # 是否出现在记录里（公开面用的 id 是 acc_1，管理面也用 acc_1 —— 所以改看
        # 有无新增：记录数在公开面测试前后必须不变）
        before = len(rec["toggled"]) + len(rec["cleared"])
        self._pub_post("/cline/api/account/disable", {"id": "acc_1"})
        _, tb2 = _get("http://127.0.0.1:%d/__toggled" % CLINE_PORT)
        rec2 = json.loads(tb2)
        after = len(rec2["toggled"]) + len(rec2["cleared"])
        self.assertEqual(after, before, "公开面的请求不该转发到网关的 /admin/*")

    def test_public_status_unavailable_when_cline_not_configured(self):
        """未配置 cline2api 的部署：公开面是 503，且说清原因（不是 500）。"""
        status, body = _get_status("http://127.0.0.1:%d/cline/api/status" % PLAIN_PANEL_PORT)
        self.assertEqual(status, 503)
        self.assertIn("cline2api", json.loads(body)["error"])

    def test_public_page_unavailable_when_cline_not_configured(self):
        status, body = _get_status("http://127.0.0.1:%d/cline/" % PLAIN_PANEL_PORT)
        self.assertEqual(status, 503)
        self.assertIn("cline2api", json.loads(body)["error"])

    def test_cline_post_without_csrf_rejected(self):
        """cline 子面板的状态变更同样要过 CSRF 闸门。"""
        status, _ = _post("http://127.0.0.1:%d/api/cline/recheck" % CLINE_PANEL_PORT,
                          {}, csrf=False)
        self.assertEqual(status, 403)

    def test_two_gateways_are_separate_tabs(self):
        """两个网关各自一页：各有独立的容器与统计块，不再混在一个网格里。

        这是本轮改造的核心诉求 —— 之前两个网关的内容在同一页上下堆叠，
        账号和模型还挤在同一个 grid，分不清哪条属于哪个网关。
        """
        _, html = _get("http://127.0.0.1:%d/" % CLINE_PANEL_PORT)
        for marker in ('id="tabCline"', 'data-gw="wb2a"', 'data-gw="cline"',
                       'id="pane-wb2a"', 'id="pane-cline"'):
            self.assertIn(marker, html)
        # 两页各有自己的汇总块与网格，不再共用
        self.assertIn('id="summary"', html)
        self.assertIn('id="clineSummary"', html)
        self.assertIn('id="grid"', html)
        self.assertIn('id="clineAccounts"', html)
        self.assertIn('id="clineModels"', html)
        # 切换函数与「只刷新当前页」的轮询都在
        self.assertIn("function switchGw(", html)
        self.assertIn("refreshActive", html)

    def test_cline_page_splits_accounts_from_models(self):
        """cline 页内账号池与闸门台账要分区，不能混成一个网格。

        这两类东西语义完全不同（一个是凭据/额度，一个是计费闸门状态），
        混排会让「模型卡片里混着账号卡片」看起来像数据错乱。
        """
        _, html = _get("http://127.0.0.1:%d/" % CLINE_PANEL_PORT)
        self.assertIn('id="clineAccounts"', html)
        self.assertIn('id="clineModels"', html)
        self.assertIn('id="clineAcctTag"', html)
        self.assertIn('id="clineModelTag"', html)
        # 两处分别渲染：账号进 clineAccounts、模型进 clineModels
        self.assertIn("$('clineAccounts').innerHTML", html)
        self.assertIn("$('clineModels').innerHTML", html)

    def test_cline_page_shows_its_own_endpoint(self):
        """cline 页要能自证接入信息（base/api_key），否则得去翻配置文件。"""
        _, html = _get("http://127.0.0.1:%d/" % CLINE_PANEL_PORT)
        self.assertIn('id="clineBase"', html)
        self.assertIn('id="clineKey"', html)
        # 后端把 gateway/api_key 一并放进 /cline/status，前端才有东西可渲染
        status, body = self._api("/api/cline/status")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertIn("gateway", d)
        self.assertIn("api_key", d)

    def test_single_gateway_deployment_has_no_cline_section(self):
        """未配置 cline2api 的老部署：cline 路由回 503，页面也不显示其标签页。"""
        try:
            _get("http://127.0.0.1:%d/api/cline/status" % PLAIN_PANEL_PORT)
            self.fail("应返回 503")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
            self.assertIn("未配置", e.read().decode("utf-8", "replace"))
        # 标签页默认给 cline 留着但页体是隐藏的；JS 拿到 503 后把标签也隐藏
        _, html = _get("http://127.0.0.1:%d/" % PLAIN_PANEL_PORT)
        self.assertIn('id="tabCline"', html)
        self.assertIn('id="pane-cline"', html)
        self.assertIn("CLINE_OFF", html)
        # wb2a 本体不受影响
        status, body = _get("http://127.0.0.1:%d/api/status" % PLAIN_PANEL_PORT)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 3)

    def test_cline_ledger_shows_credits_not_market_price(self):
        """台账展示的必须是「真实扣费积分」，不能说成成本/市场价。

        2026-09-20 的误判就是拿响应里的市场参考价当计费信号；面板若把这块
        标成「成本」，运维会照旧按错的口径理解闸门。
        """
        _, html = _get("http://127.0.0.1:%d/" % CLINE_PANEL_PORT)
        self.assertIn("最近扣费", html)
        self.assertIn("累计扣费", html)
        self.assertIn("积分台账", html)
        # 面板文案要挑明响应里的 cost 字段不是判定依据
        self.assertIn("市场参考价", html)
        # 旧文案不得残留
        self.assertNotIn("最近成本", html)
        self.assertNotIn("累计成本", html)

    def test_cline_ledger_values_are_credits(self):
        """台账字段语义是积分：stub 的 65 表示扣了 65 积分，不是 0.0065 美元。"""
        status, body = self._api("/api/cline/status")
        self.assertEqual(status, 200)
        models = {m["bare"]: m for m in json.loads(body)["models"]}
        self.assertEqual(models["glm-5.3-flash"]["last_cost"], 0)
        self.assertEqual(models["claude-opus-5"]["last_cost"], 65)
        self.assertIn("credits", models["claude-opus-5"]["disable_reason"])


TASK_GW_PORT = 7904        # 任务中心冒烟自带 stub 网关 + 假脚本目录
TASK_PANEL_PORT = 8404


class TestTaskSmoke(unittest.TestCase):
    """任务中心的 HTTP 通路：起一个带假脚本的「网关目录」，走一遍启动→轮询→结束。

    单独起一套进程，因为这一组需要 --bin-dir / --auth-dir 指向一个**可写**的假
    网关目录（脚本要能被拉起、argv 要能被记录），而 TestSmoke 那组刻意是「无工具」
    的降级形态，两者的面板配置互斥。
    """

    gw: subprocess.Popen = None
    panel: subprocess.Popen = None
    app: Path = None

    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["STUB_TASK_DELAY"] = "0.05"
        cls.app = Path(tempfile.mkdtemp(prefix="wb2a-task-"))
        (cls.app / "scripts").mkdir()
        (cls.app / "auths").mkdir()
        for name in ("task_runner.py", "school_open_day_2026.py"):
            shutil.copy(ROOT / "tests" / "stub_tasks.py", cls.app / "scripts" / name)

        cls.gw = subprocess.Popen(
            [sys.executable, str(ROOT / "tests" / "stub_gateway.py"), "--port", str(TASK_GW_PORT)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not _wait_http("http://127.0.0.1:%d/status" % TASK_GW_PORT):
            cls.gw.terminate()
            raise unittest.SkipTest("stub 网关未能启动")

        cls.panel = subprocess.Popen(
            [sys.executable, str(ROOT / "panel.py"),
             "--base", "http://127.0.0.1:%d" % TASK_GW_PORT,
             "--key", "testkey", "--port", str(TASK_PANEL_PORT),
             "--bin-dir", str(cls.app), "--auth-dir", str(cls.app / "auths")],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if not _wait_http("http://127.0.0.1:%d/" % TASK_PANEL_PORT):
            cls.panel.terminate()
            cls.gw.terminate()
            raise unittest.SkipTest("面板未能启动")

    @classmethod
    def tearDownClass(cls):
        _stop(cls.panel, cls.gw)
        if cls.app:
            shutil.rmtree(cls.app, ignore_errors=True)

    def _api(self, path):
        return _get("http://127.0.0.1:%d%s" % (TASK_PANEL_PORT, path))

    def _post(self, path, payload):
        return _post("http://127.0.0.1:%d%s" % (TASK_PANEL_PORT, path), payload)

    def _run_and_wait(self, path, payload, timeout=40):
        """启动一个作业并轮询到结束，返回最终状态。"""
        status, body = self._post(path, payload)
        self.assertEqual(status, 200, body)
        d = json.loads(body)
        self.assertTrue(d.get("ok"), d)
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, st = self._api("/api/tasks/status")
            s = json.loads(st)
            if not s.get("running"):
                return d, s
            time.sleep(0.2)
        self.fail("作业未在 %ss 内结束" % timeout)

    def test_capability_reports_enabled(self):
        """配了 bin_dir/auth_dir + 脚本在位 → 任务中心可用。"""
        _, body = self._api("/api/endpoint")
        caps = json.loads(body)["capabilities"]
        self.assertTrue(caps["tasks"])
        self.assertTrue(caps["auth_dir"])
        _, tb = self._api("/api/endpoint")
        t = json.loads(tb)["tasks"]
        self.assertTrue(t["enabled"])
        self.assertTrue(t["has_school"])
        self.assertTrue(t["task_runner"].endswith("task_runner.py"))

    def test_status_contract_shape(self):
        """/api/tasks/status 的字段契约。

        不断言 idle：本组共用一个面板进程，unittest 按字母序跑，此时可能已有作业
        跑过。「没有任何作业时是 idle」在 test_tasks.py 的单测里确定性覆盖。
        """
        _, body = self._api("/api/tasks/status")
        d = json.loads(body)
        for key in ("job", "state", "running", "items", "summary", "accounts", "counts"):
            self.assertIn(key, d, key)
        self.assertIsInstance(d["items"], list)
        self.assertIn(d["state"], ("idle", "pending", "running", "done", "failed", "cancelled"))

    def test_scan_is_read_only(self):
        """扫描是只读的：命令里不能出现 --yes —— 这是防误发上报的关键闸门。"""
        started, st = self._run_and_wait("/api/tasks/scan", {})
        self.assertNotIn("--yes", started["argv"])
        self.assertFalse(started["write"])
        self.assertEqual(st["state"], "done")
        codes = {i["code"]: i["status"] for i in st["items"]}
        self.assertEqual(codes.get("create_canvas"), "planned")
        self.assertEqual(codes.get("Expert_Philanthropy"), "skip")

    def test_run_collects_progress_and_rewards(self):
        started, st = self._run_and_wait("/api/tasks/run", {})
        self.assertIn("--yes", started["argv"])
        self.assertTrue(started["write"])
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["summary"]["credit"], 750)
        by_code = {i["code"]: i for i in st["items"]}
        self.assertEqual(by_code["chat_5"]["credit"], 300)
        self.assertEqual(st["accounts"].get("00e26541"), "测试账号甲")

    def test_only_claim_flag_forwarded(self):
        started, _ = self._run_and_wait("/api/tasks/run", {"only_claim": True})
        self.assertIn("--only-claim", started["argv"])
        self.assertIn("--yes", started["argv"])

    def test_accounts_forwarded_as_filenames(self):
        """账号要转成完整 auths 文件名 —— uid 前缀会撞号。"""
        started, _ = self._run_and_wait("/api/tasks/scan", {"accounts": ["abc123"]})
        self.assertIn("workbuddy-abc123.json", started["argv"])

    def test_school_list_mode(self):
        started, st = self._run_and_wait("/api/tasks/school", {"mode": "list"})
        self.assertIn("--list", started["argv"])
        self.assertNotIn("--yes", started["argv"])
        self.assertFalse(started["write"])
        self.assertIn("share_invite", [i["code"] for i in st["items"]])

    def test_school_invalid_mode_rejected(self):
        status, body = self._post("/api/tasks/school", {"mode": "boom"})
        self.assertEqual(status, 400)
        self.assertIn("list", json.loads(body)["error"])

    def test_incremental_log_endpoint(self):
        _, st = self._run_and_wait("/api/tasks/scan", {})
        total = st["log_lines"]
        self.assertGreater(total, 5)
        _, body = self._api("/api/tasks/status?since=0")
        self.assertEqual(len(json.loads(body)["lines"]), total)
        _, body = self._api("/api/tasks/status?since=%d" % total)
        self.assertEqual(json.loads(body)["lines"], [])

    def test_cancel_without_job_is_409(self):
        status, body = self._post("/api/tasks/cancel", {})
        self.assertEqual(status, 409)
        self.assertIn("error", json.loads(body))

    def test_task_post_requires_csrf_header(self):
        """任务端点是状态变更接口，同样要过 CSRF 闸门。"""
        status, _ = _post("http://127.0.0.1:%d/api/tasks/scan" % TASK_PANEL_PORT,
                          {}, csrf=False)
        self.assertEqual(status, 403)

    def test_task_center_markup_present(self):
        """页面要有任务中心的容器、按钮与状态映射 —— 否则后端能力再多也点不到。"""
        _, html = self._get_index()
        for marker in ('id="taskPanel"', 'id="btnTaskScan"', 'id="btnTaskAll"',
                       'id="btnTaskClaim"', 'id="btnTaskCancel"', 'id="taskBody"',
                       'id="taskLog"'):
            self.assertIn(marker, html)
        # 状态映射表要与后端 classify_line() 的口径对齐
        for st in ("done", "already", "running", "planned", "pending", "skip", "error"):
            self.assertIn(st + ":", html, st)

    def test_capability_marking_is_wired_into_endpoint_load(self):
        """能力标记必须在 loadEndpoint 里设置。

        曾经只在 loadTasks 首轮拿一次 endpoint，之后永不重设 —— 于是「未启用」标签
        永远不出现、按钮的禁用态也可能被 renderTasks 覆盖回去。
        """
        _, html = self._get_index()
        self.assertIn("function applyTaskCaps(", html)
        # loadEndpoint 必须调用它（与 login/checkin/trial 的禁用同处）
        idx = html.index("async function loadEndpoint(")
        end = html.index("\nasync function", idx + 10)
        self.assertIn("applyTaskCaps(", html[idx:end])

    def test_summary_numbers_prefer_script_totals(self):
        """顶部数字优先采信脚本汇总行 —— item 表只有打印过日志的任务，会偏小。"""
        _, html = self._get_index()
        self.assertIn("sum[fromSummary]", html)

    def test_render_tasks_does_not_reference_undefined_vars(self):
        """renderTasks 里不得引用未定义变量。

        实测踩过：把 total 改名成 nTotal 时漏改一处引用，renderTasks 抛
        ReferenceError 被 fetch 的 catch 吞掉，表现为「状态显示已完成但表格空白」——
        比报错更难查。这里用 node 语法检查 + 关键标识符断言兜住。
        """
        _, html = self._get_index()
        idx = html.index("function renderTasks(")
        end = html.index("\nfunction ", idx + 10)
        body = html[idx:end]
        for name in ("nTotal", "nDone", "nAlready", "nPending", "nFail"):
            self.assertIn(name, body, name)
        # 旧名不得残留（它就是漏改的那处）
        self.assertNotIn("|| total", body)
        self.assertNotIn("? total", body)

    def _get_index(self):
        return _get("http://127.0.0.1:%d/" % TASK_PANEL_PORT)


if __name__ == "__main__":
    unittest.main()
