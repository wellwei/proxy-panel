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
        for p in (cls.panel, cls.plain, cls.cline, cls.gw):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    p.kill()

    def _api(self, path):
        return _get("http://127.0.0.1:%d%s" % (CLINE_PANEL_PORT, path))

    def _post(self, path, payload):
        return _post("http://127.0.0.1:%d%s" % (CLINE_PANEL_PORT, path), payload)

    def test_cline_status_proxied(self):
        """闸门台账要透出，且 exposed 字段能区分在服/关停两类模型。"""
        status, body = self._api("/api/cline/status")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["accounts_total"], 2)
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

    def test_cline_recheck(self):
        status, body = self._post("/api/cline/recheck", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["started"])

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


if __name__ == "__main__":
    unittest.main()
