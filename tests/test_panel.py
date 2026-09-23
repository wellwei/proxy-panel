"""wb2a-panel 的单元测试。

只测纯函数与逻辑层（不依赖真实网关），用标准库 unittest —— 保持零依赖。
    python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import panel  # noqa: E402


class TestNormalizeListen(unittest.TestCase):
    """网关 config 的 listen 归一化。"""

    def test_common_forms(self):
        cases = {
            ":7863": "http://127.0.0.1:7863",
            "0.0.0.0:7863": "http://127.0.0.1:7863",
            "127.0.0.1:7863": "http://127.0.0.1:7863",
            "localhost:9999": "http://localhost:9999",
            "": "http://127.0.0.1:7863",
        }
        for listen, want in cases.items():
            self.assertEqual(panel.normalize_listen(listen), want, listen)

    def test_wildcard_hosts_collapse_to_loopback(self):
        # 通配地址不能拿去发请求：往 0.0.0.0 / :: 发请求在部分平台直接失败
        for listen in (":7863", "0.0.0.0:7863", "[::]:7863", "::"):
            self.assertEqual(panel.normalize_listen(listen), "http://127.0.0.1:7863", listen)

    def test_missing_port_falls_back(self):
        self.assertEqual(panel.normalize_listen("127.0.0.1"), "http://127.0.0.1:7863")


class TestFindGatewayConfig(unittest.TestCase):
    """网关配置探测：要能认出"这是网关配置"，并跳过无关的 config.json。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_config_in_cwd(self):
        (self.root / "config.json").write_text(
            json.dumps({"listen": ":7863", "api_key": "k"}), encoding="utf-8")
        old = os.getcwd()
        os.chdir(self.root)
        try:
            self.assertTrue(panel.find_gateway_config())
        finally:
            os.chdir(old)

    def test_ignores_unrelated_config_json(self):
        """面板自己的 config.json 没有 listen/api_key，不该被当成网关配置。"""
        (self.root / "config.json").write_text(
            json.dumps({"port": 8321, "base": "http://x"}), encoding="utf-8")
        old = os.getcwd()
        os.chdir(self.root)
        try:
            found = panel.find_gateway_config()
            self.assertFalse(found and Path(found).parent == self.root)
        finally:
            os.chdir(old)

    def test_explicit_path_respected(self):
        p = self.root / "gw.json"
        p.write_text(json.dumps({"listen": ":1"}), encoding="utf-8")
        self.assertEqual(panel.find_gateway_config(str(p)), str(p))

    def test_missing_explicit_path_returns_empty(self):
        self.assertEqual(panel.find_gateway_config(str(self.root / "nope.json")), "")


class TestConfigCapabilities(unittest.TestCase):
    """能力探测：没有 CLI 工具时必须干净地降级，而不是报错。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _touch_exec(self, name):
        p = self.bin / name
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return p

    def test_no_tools_all_disabled(self):
        cfg = panel.Config("http://x", "k", "/tmp", str(self.bin), 8321)
        self.assertFalse(cfg.can_login)
        self.assertFalse(cfg.can_credit)
        self.assertFalse(cfg.can_checkin)
        self.assertFalse(cfg.can_trial)

    def test_tools_detected_by_exec_bit(self):
        self._touch_exec("login")
        self._touch_exec("credit")
        cfg = panel.Config("http://x", "k", "/tmp", str(self.bin), 8321)
        self.assertTrue(cfg.can_login)
        self.assertTrue(cfg.can_credit)
        self.assertFalse(cfg.can_checkin)      # 没造这个

    def test_login_requires_auth_dir(self):
        """有 login 工具但没有 auths 目录 → 不能登录（不知道该往哪写）。"""
        self._touch_exec("login")
        cfg = panel.Config("http://x", "k", "", str(self.bin), 8321)
        self.assertFalse(cfg.can_login)

    def test_no_bin_dir_is_safe(self):
        cfg = panel.Config("http://x", "k", "/tmp", "", 8321)
        self.assertFalse(cfg.can_login)
        self.assertIsNone(cfg.tool("login"))

    def test_non_executable_tool_rejected(self):
        p = self.bin / "login"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o644)                          # 没有执行位
        cfg = panel.Config("http://x", "k", "/tmp", str(self.bin), 8321)
        self.assertFalse(cfg.can_login)


class TestExposureAndLanGating(unittest.TestCase):
    """默认只绑回环；局域网地址只在面板确实可被外部访问时才提示。"""

    def _cfg(self, host):
        return panel.Config("http://127.0.0.1:7863", "k", "", "", 8321, host=host)

    def test_loopback_is_not_exposed(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertFalse(self._cfg(host).exposed, host)

    def test_other_hosts_are_exposed(self):
        for host in ("0.0.0.0", "203.0.113.10", "::"):   # RFC 5737 文档保留地址
            self.assertTrue(self._cfg(host).exposed, host)

    def test_default_is_loopback(self):
        """不传 host 时必须落在回环上 —— 面板持有网关 api_key，
        默认开在局域网上等于把管理面敞开。"""
        cfg = panel.Config("http://x", "k", "", "", 8321)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertFalse(cfg.exposed)

    def test_lan_address_hidden_when_loopback_only(self):
        """绑回环时不提示内网地址：它既不可达（误导），也是多余的拓扑信息。"""
        p = panel.Panel(self._cfg("127.0.0.1"))
        self.assertEqual(p.endpoint_info()["base_url_lan"], "")
        self.assertFalse(p.endpoint_info()["panel_exposed"])

    def test_lan_address_shown_when_exposed(self):
        """绑全网卡时才给内网地址 —— 此时它才是"其他机器该填什么"的可操作信息。"""
        p = panel.Panel(self._cfg("0.0.0.0"))
        info = p.endpoint_info()
        self.assertTrue(info["panel_exposed"])
        # 取不到出口网卡（如无网络环境）时为空是允许的，能取到就必须是 http:// 开头
        if info["base_url_lan"]:
            self.assertTrue(info["base_url_lan"].startswith("http://"))


class TestRunTool(unittest.TestCase):
    """CLI 调用的输出解码。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _script(self, name, body):
        p = self.bin / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)

    def test_non_utf8_output_does_not_crash(self):
        """签到输出含非 UTF-8 字节：必须用 errors='replace' 解码而不是抛异常。

        这是实测踩过的坑——用 subprocess 的 text=True 会直接 UnicodeDecodeError。
        """
        self._script("signin_bin", "printf '\\xff\\xfe ok\\n'\n")
        cfg = panel.Config("http://x", "k", "/tmp", str(self.bin), 8321)
        p = panel.Panel(cfg)
        out = p.run_tool("signin_bin")
        self.assertIn("ok", out["output"])

    def test_missing_tool_degrades_gracefully(self):
        cfg = panel.Config("http://x", "k", "/tmp", str(self.bin), 8321)
        p = panel.Panel(cfg)
        out = p.run_tool("signin_bin")
        self.assertFalse(out["ok"])
        self.assertIn("未检测到", out["output"])

    def test_nonzero_exit_reported(self):
        self._script("signin_bin", "echo boom >&2\nexit 3\n")
        cfg = panel.Config("http://x", "k", "/tmp", str(self.bin), 8321)
        out = panel.Panel(cfg).run_tool("signin_bin")
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["output"])


class TestLoginSession(unittest.TestCase):
    """登录会话状态机：不能并发，取消要能让后台线程退出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self.auths = Path(self.tmp.name) / "auths"
        self.auths.mkdir()
        p = self.bin / "login"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def _panel(self):
        cfg = panel.Config("http://127.0.0.1:1", "k", str(self.auths), str(self.bin), 8321)
        return panel.Panel(cfg)

    def test_login_disabled_without_capability(self):
        cfg = panel.Config("http://x", "k", "", "", 8321)
        r = panel.Panel(cfg).login_start("cn")
        self.assertIn("error", r)
        self.assertIn("同机部署", r["error"])

    def test_cancel_bumps_generation(self):
        """取消要让 gen 前进 —— 后台线程据此发现自己被取代并退出。

        否则被取消的线程会继续 poll，而新会话已覆写同一个 state 文件，
        它会拿新会话的结果去落盘（写错账号）。
        """
        p = self._panel()
        with p._lock:
            before = p.session["gen"]
        p.login_cancel()
        with p._lock:
            self.assertGreater(p.session["gen"], before)
            self.assertEqual(p.session["stage"], "idle")

    def test_worker_exits_when_generation_changes(self):
        """worker 每轮核对 gen：被取代后立即返回，不写任何东西。"""
        p = self._panel()
        with p._lock:
            gen = p.session["gen"] + 1
            p.session = {"gen": gen, "stage": "waiting", "message": "", "url": "", "realm": "cn"}
        p.login_cancel()                     # gen 前进 → worker 应退出
        done = threading.Event()

        def run():
            p._login_worker(gen, "cn", "http://example.invalid")
            done.set()

        threading.Thread(target=run, daemon=True).start()
        # worker 第一轮 sleep 3s 后才核对 gen；给它足够时间
        self.assertTrue(done.wait(timeout=8), "worker 未在 gen 变化后退出")


class TestWriteAuthFile(unittest.TestCase):
    """凭据落盘：格式要与网关一致，权限 0600，原子写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "auths").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def _panel(self):
        cfg = panel.Config("http://x", "k", str(self.root / "auths"), "", 8321)
        return panel.Panel(cfg)

    def test_writes_expected_shape(self):
        p = self._panel()
        path = p._write_auth_file({
            "uid": "u1", "nickname": "nick", "access_token": "at",
            "refresh_token": "rt", "expires_in": 3600,
            "domain": "www.codebuddy.cn", "realm": "cn",
        })
        self.assertEqual(path.name, "workbuddy-u1.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["account"]["uid"], "u1")
        self.assertEqual(data["auth"]["accessToken"], "at")
        self.assertEqual(data["auth"]["realm"], "cn")
        self.assertGreater(data["auth"]["expiresAt"], 0)

    def test_permissions_are_0600(self):
        p = self._panel()
        path = p._write_auth_file({"uid": "u2", "access_token": "at"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_creates_auth_dir_if_missing(self):
        cfg = panel.Config("http://x", "k", str(self.root / "new" / "auths"), "", 8321)
        path = panel.Panel(cfg)._write_auth_file({"uid": "u3", "access_token": "at"})
        self.assertTrue(path.is_file())


class TestExtractErrorMessage(unittest.TestCase):
    """错误消息提取：网关把 message 嵌在 error 对象里，只读顶层会永远拿不到。

    这是实测踩到的坑——两种 404（管理端点未开启 / uid 不存在）曾经分不开。
    """

    def test_nested_error_object(self):
        self.assertEqual(
            panel.extract_error_message(
                {"error": {"code": "not_found", "message": "account not found: u1"}}),
            "account not found: u1")

    def test_top_level_message(self):
        self.assertEqual(panel.extract_error_message({"message": "boom"}), "boom")

    def test_string_error(self):
        self.assertEqual(panel.extract_error_message({"error": "plain reason"}), "plain reason")

    def test_falls_back_to_code(self):
        self.assertEqual(panel.extract_error_message({"error": {"code": "not_found"}}), "not_found")

    def test_plain_text(self):
        self.assertEqual(panel.extract_error_message("just text"), "just text")

    def test_empty(self):
        self.assertEqual(panel.extract_error_message({}), "")


class TestAccountOp(unittest.TestCase):
    """管理端点的错误映射：连接失败要返回 error 而不是抛异常。"""

    def setUp(self):
        self.p = panel.Panel(panel.Config("http://127.0.0.1:1", "k", "", "", 8321))

    def test_connection_failure_reported(self):
        r = self.p.account_op("disable", "u1")
        self.assertIn("error", r)


class TestAccountOp404Split(unittest.TestCase):
    """两种 404 必须给出不同的、可操作的提示。"""

    def _panel_with_stub(self, payload, code=404):
        """起一个只回固定响应的迷你服务，验证 account_op 的分支。"""
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        # shutdown() 只停循环，不关监听套接字 —— 不补 server_close() 会在解释器
        # 退出时刷 unclosed socket 警告，把测试输出盖住。
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        cfg = panel.Config("http://127.0.0.1:%d" % srv.server_port, "k", "", "", 8321)
        return panel.Panel(cfg)

    def test_account_not_found(self):
        p = self._panel_with_stub(
            {"error": {"code": "not_found", "message": "account not found: u1"}})
        r = p.account_op("disable", "u1")
        self.assertIn("账号不存在", r["error"])

    def test_admin_disabled(self):
        p = self._panel_with_stub(
            {"error": {"code": "not_found", "message": "not found"}})
        r = p.account_op("disable", "u1")
        self.assertIn("admin", r["error"])
        self.assertIn("enabled", r["error"])


class TestHttpHelper(unittest.TestCase):
    """http() 的返回契约：连接失败返回 status=0 而不是抛异常。"""

    def test_connection_error_is_status_zero(self):
        status, body = panel.http("GET", "http://127.0.0.1:1/nope", timeout=3)
        self.assertEqual(status, 0)
        self.assertIn("error", body)


class TestLoadClineConfig(unittest.TestCase):
    """cline2api 连接参数的四级发现。

    最值得锁住的是「文件在、但读不到」——它和「没部署这个网关」在页面上表现
    完全一样（整块 cline 区域消失），曾因 cline2api 的部署脚本把 config.json
    设成 600 而真实踩过一次。
    """

    class _Args:
        cline_config = ""
        cline_base = ""
        cline_key = ""
        cline_admin_token = ""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "cline.json"
        self.cfg.write_text(json.dumps({
            "listen": "127.0.0.1:8081", "api_key": "k-from-file",
            "admin_token": "a-from-file",
        }), encoding="utf-8")

    def _load(self, **over):
        args = self._Args()
        for k, v in over.items():
            setattr(args, k, v)
        return panel.load_cline_config(args, {})

    def test_reads_from_gateway_config(self):
        c = self._load(cline_config=str(self.cfg))
        self.assertEqual(c["base"], "http://127.0.0.1:8081")   # listen 归一化
        self.assertEqual(c["api_key"], "k-from-file")
        self.assertEqual(c["admin_token"], "a-from-file")

    def test_cli_overrides_file(self):
        c = self._load(cline_config=str(self.cfg), cline_base="http://10.0.0.9:9000",
                       cline_admin_token="cli-admin")
        self.assertEqual(c["base"], "http://10.0.0.9:9000")
        self.assertEqual(c["admin_token"], "cli-admin")
        self.assertEqual(c["api_key"], "k-from-file")   # 未覆盖的仍读文件

    def test_admin_token_falls_back_to_api_key(self):
        """admin_token 缺省时退用 api_key（老版本 cline2api 只有一个密钥）。"""
        self.cfg.write_text(json.dumps({"listen": ":8081", "api_key": "only"}),
                            encoding="utf-8")
        c = self._load(cline_config=str(self.cfg))
        self.assertEqual(c["admin_token"], "only")

    def test_unreadable_config_reports_and_disables(self):
        """读不到（权限）时必须明确报错，而不是静默当成「没配置」。"""
        if os.geteuid() == 0:
            self.skipTest("root 无视文件权限，测不了这个场景")
        self.cfg.chmod(0o000)
        self.addCleanup(self.cfg.chmod, 0o600)
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c = self._load(cline_config=str(self.cfg))
        self.assertIsNone(c)
        msg = err.getvalue()
        self.assertIn("读不到", msg)
        self.assertIn("chmod 640", msg)      # 要给出可执行的修复命令
        self.assertIn(str(self.cfg), msg)

    def test_missing_config_is_silent_none(self):
        """真没这个文件时安静返回 None（老部署不该刷错误）。"""
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            c = self._load(cline_config=str(Path(self.tmp.name) / "nope.json"))
        self.assertIsNone(c)
        self.assertEqual(err.getvalue(), "")


class TestPublicProjection(unittest.TestCase):
    """公开面投影：对外字段是**白名单**，网关新增字段不会自动流出去。

    这里的断言不是「检查几个敏感字段被删掉了」——那种测法漏掉一个字段就是一次泄漏。
    测法是反过来的：喂一份**塞满了敏感字段**的网关响应，然后断言公开面输出的
    完整键集合恰好等于预期。多一个键就失败，所以网关将来加什么字段都不会漏网。
    """

    # 一份尽量贴近真实的 /status：凡是公开面不该出现的，都塞进来
    RICH_STATUS = {
        "version": "1.2.3",
        "uptime_seconds": 4242,
        "accounts_total": 3,
        "accounts_available": 2,
        "accounts": [
            {"accountId": "acc_1", "email": "abc***@gmail.com", "status": "active",
             "refreshToken": "SECRET-RT", "requestsTotal": 12, "tokensTotal": 4567,
             "modelCooldowns": [{"model": "cline-free/deepseek-v4.1-flash",
                                 "until": "2026-09-21T18:03:18Z",
                                 "reason": "SECRET-REASON"}]},
            {"accountId": "acc_2", "email": "def***@qq.com", "status": "cooldown",
             "cooldownUntil": "2026-09-21T00:00:00Z",
             "lastReason": "429: Try again in 17h", "manualDisabled": True},
            # 短邮箱：网关自己的打码在这个形状上会**原样返回完整地址**
            # （MaskEmail 的兜底分支），是公开面最需要挡住的一种
            {"accountId": "acc_3", "email": "ab@x.com", "status": "active"},
        ],
        "models": [
            {"upstream": "z-ai/glm-5.3-flash", "bare": "glm-5.3-flash", "group": "free",
             "state": "free", "exposed": True, "last_cost": 0, "cost_total": 0,
             "requests": 3, "probe_streak": 2, "manual": True,
             "disable_reason": "SECRET-DISABLE", "accounts_available": 2},
            {"upstream": "anthropic/claude-opus-5", "bare": "claude-opus-5",
             "group": "recommended", "state": "disabled", "exposed": False,
             "last_cost": 65, "cost_total": 65, "disable_reason": "probe credits=65",
             "accounts_available": 0},
        ],
        "catalog": {"last_sync": "2026-09-20T01:00:00Z", "error": "SECRET-CATALOG-ERR"},
        "gateway": "http://127.0.0.1:7862",
        "api_key": "SECRET-API-KEY",
    }

    def test_model_projection_is_an_exact_whitelist(self):
        out = panel._public_model(self.RICH_STATUS["models"][0])
        self.assertEqual(set(out), {"name", "group", "state", "accounts"})

    def test_status_projection_is_an_exact_whitelist(self):
        out = panel._public_status(self.RICH_STATUS)
        self.assertEqual(set(out), {
            "version", "uptime_seconds", "accounts_total", "accounts_available",
            "accounts_in_cooldown", "accounts", "models_total", "models_active", "models",
            "catalog_last_sync", "catalog_stale",
        })
        for m in out["models"]:
            self.assertEqual(set(m), {"name", "group", "state", "accounts"})
        for a in out["accounts"]:
            self.assertEqual(set(a), {"name", "state", "until", "limits"})

    def test_no_sensitive_value_survives_projection(self):
        """整份输出序列化后搜哨兵值 —— 比逐字段断言更能挡住「加了个新字段」的回归。

        注意：打码邮箱是**有意**出现在公开面的（账号池下拉要用它认领自己的账号），
        所以这里搜的是**完整**地址与内部标识，不是「邮箱」这个词。
        """
        blob = json.dumps(panel._public_status(self.RICH_STATUS), ensure_ascii=False)
        for secret in ("SECRET-RT", "SECRET-REASON", "SECRET-DISABLE",
                       "SECRET-CATALOG-ERR", "SECRET-API-KEY",
                       "acc_1", "acc_2", "acc_3",
                       "ab@x.com",                     # 短邮箱：完整地址必须被挡住
                       "z-ai/glm-5.3-flash", "anthropic/claude-opus-5",
                       "127.0.0.1:7862"):
            self.assertNotIn(secret, blob, "公开面泄漏了 %s" % secret)

    def test_accounts_are_listed_with_masked_emails(self):
        """账号池下拉要有东西可显示：每个账号一条，邮箱是打码的。"""
        out = panel._public_status(self.RICH_STATUS)
        names = [a["name"] for a in out["accounts"]]
        # 能出力的两个在前（组内按打码名排），停用的在后
        self.assertEqual(names, ["ab***@x.com", "abc***@gmail.com", "def***@qq.com"])
        # 认得出的部分留着，认不出的部分打掉
        self.assertIn("abc***", names[1])
        self.assertNotIn("ab@x.com", json.dumps(out))

    def test_account_state_and_details(self):
        out = panel._public_status(self.RICH_STATUS)
        by = {a["name"]: a for a in out["accounts"]}
        # 手动停用折进 paused（公开面不必区分「运维摘除」与「凭据失效」）
        self.assertEqual(by["def***@qq.com"]["state"], "paused")
        # 冷却截止时间只对冷却中的账号给；限额数量只数真的到期时间
        self.assertEqual(by["abc***@gmail.com"]["limits"], 1)
        self.assertEqual(by["abc***@gmail.com"]["until"], "")
        self.assertEqual(by["ab***@x.com"]["limits"], 0)

    def test_accounts_sorted_active_first(self):
        """稳定排序：能出力的排前面，同组按打码名 —— 不暴露池子的内部顺序。"""
        out = panel._public_status(self.RICH_STATUS)
        self.assertEqual([a["state"] for a in out["accounts"]],
                         ["active", "active", "paused"])

    def test_upstream_names_are_replaced_by_bare_names(self):
        """上游名带分区/线路信息，公开面只给裸名（与模型广场同一口径）。"""
        out = panel._public_status(self.RICH_STATUS)
        self.assertEqual([m["name"] for m in out["models"]],
                         ["glm-5.3-flash", "claude-opus-5"])

    def test_state_is_translated_to_public_vocabulary(self):
        """闸门内部状态名（free/exposed/unverified/disabled）不直接对外。"""
        out = panel._public_status(self.RICH_STATUS)
        self.assertEqual([m["state"] for m in out["models"]], ["active", "off"])
        self.assertNotIn("exposed", json.dumps(out))

    def test_counts_are_derived(self):
        out = panel._public_status(self.RICH_STATUS)
        self.assertEqual(out["models_total"], 2)
        self.assertEqual(out["models_active"], 1)
        self.assertEqual(out["accounts_in_cooldown"], 1)   # acc_2 是 cooldown
        self.assertTrue(out["catalog_stale"])              # 目录同步报过错
        self.assertEqual(out["catalog_last_sync"], "2026-09-20T01:00:00Z")

    def test_empty_and_partial_payloads_do_not_crash(self):
        """网关字段缺失/为空时也要给出可渲染的结果（公开面不能 500）。"""
        for bad in ({}, {"models": None, "accounts": None}, {"models": [{}]},
                    {"catalog": None}, {"accounts": ["not-a-dict"]},
                    {"accounts": [{"email": None}]}, {"accounts": [{}]}):
            out = panel._public_status(bad)      # 不抛异常即通过
            self.assertIn("models", out)
            self.assertIsInstance(out["models"], list)
            self.assertIsInstance(out["accounts"], list)

    def test_unknown_state_is_not_passed_through(self):
        """认不出的状态一律 unknown —— 不把内部状态名原样端出去。"""
        out = panel._public_model({"bare": "x", "state": "some-internal-state"})
        self.assertEqual(out["state"], "unknown")


class TestPublicEmailMasking(unittest.TestCase):
    """公开面的邮箱打码：不超过 3 个字符，且**任何形状都不漏完整地址**。

    这条独立于网关：网关自己也打码，但它的兜底分支在短邮箱上会原样返回整个地址
    （`ab@x.com` / `a@b.co`）。管理面无所谓，公开面是硬要求。
    """

    def test_keeps_at_most_three_chars(self):
        cases = {
            "alice@example.com": "ali***@example.com",
            "alice.bob@example.com": "ali***@example.com",
            "ab@x.com": "ab***@x.com",
            "a@b.co": "a***@b.co",
        }
        for raw, want in cases.items():
            self.assertEqual(panel._public_email(raw), want, raw)

    def test_is_idempotent_on_already_masked(self):
        """网关已经打过的再过一遍不变（面板不重复打码）。"""
        for m in ("abc***@gmail.com", "ab***@x.com"):
            self.assertEqual(panel._public_email(m), m)

    def test_never_returns_the_whole_address(self):
        """凡是带 @ 的，输出里必须出现 *** —— 完整地址绝不放行。"""
        for raw in ("ab@x.com", "a@b.co", "ab@qq.com", "x@y.z", "aa@bb.cc"):
            out = panel._public_email(raw)
            self.assertIn("***", out, raw)
            self.assertNotEqual(out, raw, raw)

    def test_missing_or_odd_input_is_handled(self):
        self.assertEqual(panel._public_email(""), "（无邮箱）")
        self.assertEqual(panel._public_email(None), "（无邮箱）")
        self.assertEqual(panel._public_email("no-at-sign"), "no-***")
        self.assertEqual(panel._public_email("@" ), "***")


class TestRetryAfterParsing(unittest.TestCase):
    """限流等待秒数从网关文案里抠；抠不到要有兜底。"""

    def test_parses_seconds(self):
        self.assertEqual(panel._retry_after_seconds("发起太频繁，请 42 秒后再试"), 42)

    def test_falls_back_when_no_number(self):
        self.assertEqual(panel._retry_after_seconds("稍后再试"), 60)
        self.assertEqual(panel._retry_after_seconds(""), 60)


if __name__ == "__main__":
    unittest.main()
