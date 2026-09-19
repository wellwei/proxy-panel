#!/usr/bin/env python3
"""wb2a-panel — workbuddy2api 的轻量 Web 管理面板。

设计取舍（三句话）：
  1. **零依赖**：只用 Python 标准库。不需要 pip install，不需要编译。
  2. **不 fork 网关**：纯外部面板，只通过网关自己的 HTTP 接口工作，
     上游怎么更新都不用跟着改。
  3. **优雅降级**：核心功能（账号池 / 模型目录 / 请求统计 / 停用启用）只需
     网关地址 + api_key；增强功能（新增账号 / 签到 / 领试用 / 实时积分）需要
     与网关同机部署、能调到它的 CLI 工具，检测到才启用。

跑起来：
    python3 panel.py --base http://127.0.0.1:7863 --key sk-xxx
或让它自己找网关配置：
    python3 panel.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.2.0"

HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 8321
DEFAULT_HOST = "127.0.0.1"     # 默认只监听回环：面板持有网关 api_key
LOGIN_TIMEOUT = 300          # 登录会话有效期（秒）
CLI_TIMEOUT = 150            # 运维 CLI 超时

# 不走任何 HTTP 代理：面板只访问本机网关与本机网络。
# 宿主机常有 http_proxy（如 127.0.0.1:7890），不加这行会把回环请求也代理出去。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ──────────────────────────────────────────────────────────────────────
# 配置发现：命令行 > 环境变量 > 配置文件 > 自动探测网关 config.json
# ──────────────────────────────────────────────────────────────────────

class Config:
    def __init__(self, gateway: str, api_key: str, auth_dir: str, bin_dir: str,
                 port: int, gateway_config: str = "", host: str = "127.0.0.1"):
        self.gateway = gateway.rstrip("/")
        self.api_key = api_key
        self.auth_dir = Path(auth_dir).expanduser() if auth_dir else None
        self.bin_dir = Path(bin_dir).expanduser() if bin_dir else None
        self.port = port
        self.gateway_config = gateway_config
        self.host = host

    @property
    def exposed(self) -> bool:
        """面板是否监听在回环之外。用来决定要不要提示局域网地址——
        绑定回环时那个地址是无效信息，绑定全网卡时它才是可操作的信息。"""
        return self.host not in ("127.0.0.1", "localhost", "::1")

    # — 能力 —————————————————————————————————————————————
    def tool(self, name: str):
        """返回 bin_dir 下某个 CLI 工具的路径（不存在则 None）。"""
        if not self.bin_dir:
            return None
        p = self.bin_dir / name
        return p if p.is_file() and os.access(p, os.X_OK) else None

    @property
    def can_login(self) -> bool:
        return self.tool("login") is not None and self.auth_dir is not None

    @property
    def can_credit(self) -> bool:
        return self.tool("credit") is not None

    @property
    def can_checkin(self) -> bool:
        return self.tool("signin_bin") is not None

    @property
    def can_trial(self) -> bool:
        return self.tool("trial_bin") is not None


def normalize_listen(listen: str) -> str:
    """把网关 config 的 listen（":7863" / "0.0.0.0:7863"）转成本机可访问的基址。

    用 net 层工具而非手切冒号：IPv6 字面量（"::" / "[::]:7863"）本身含冒号，
    手切会拼出 "http://::7863" 这种非法地址。
    """
    listen = (listen or "").strip() or ":7863"
    host, port = "", ""
    # 借 urllib 解析而非自己切：net.SplitHostPort 的语义在这里要处理 "[::]:x" 这类写法
    if listen.startswith("["):
        end = listen.find("]")
        if end > 0:
            host, rest = listen[1:end], listen[end + 1:]
            port = rest.lstrip(":")
    elif listen.count(":") == 1:
        host, port = listen.split(":", 1)
    elif listen.count(":") == 0:
        host = listen
    else:
        host = listen          # 多个冒号且无方括号 → 裸 IPv6，无端口
    if not port:
        port = "7863"
    if host in ("", "0.0.0.0", "::", ":"):
        host = "127.0.0.1"
    if ":" in host:            # IPv6 字面量需要方括号
        host = "[" + host + "]"
    return "http://%s:%s" % (host, port)


def find_gateway_config(explicit: str = "") -> str:
    """找网关的 config.json（从中读 listen / api_key / auth_dir）。

    候选路径覆盖三种常见布局：同目录、面板在 wb2a/ 下一级、Docker 的 /app。
    """
    if explicit:
        return explicit if Path(explicit).is_file() else ""
    env = os.environ.get("WB2A_CONFIG", "").strip()
    if env and Path(env).is_file():
        return env
    candidates = []
    for root in (Path.cwd(), HERE, HERE.parent):
        candidates += [root / "config.json", root / "app" / "config.json"]
    candidates.append(Path("/app/config.json"))
    for p in candidates:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            # 用 listen/api_key 存在与否判定"这是网关配置"，避免误认面板自己的配置
            if isinstance(data, dict) and ("listen" in data or "api_key" in data):
                return str(p)
    return ""


def load_config(args) -> Config:
    gw_cfg_path = find_gateway_config(args.gateway_config)
    gw_cfg = {}
    if gw_cfg_path:
        try:
            gw_cfg = json.loads(Path(gw_cfg_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print("! 读取网关配置失败（%s）：%s" % (gw_cfg_path, e), file=sys.stderr, flush=True)

    # 面板自己的配置文件（可选）：与网关配置分离，便于 Docker 里只挂这一个
    panel_cfg = {}
    panel_cfg_path = Path(args.config).expanduser() if args.config else HERE / "panel.json"
    if panel_cfg_path.is_file():
        try:
            panel_cfg = json.loads(panel_cfg_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print("! 读取面板配置失败（%s）：%s" % (panel_cfg_path, e), file=sys.stderr, flush=True)

    def pick(cli_value, env_name, cfg_key, fallback=""):
        if cli_value:
            return cli_value
        env = os.environ.get(env_name, "").strip()
        if env:
            return env
        if cfg_key in panel_cfg and panel_cfg[cfg_key]:
            return panel_cfg[cfg_key]
        return fallback

    base = pick(args.base, "WB2A_BASE", "base")
    if not base:
        base = normalize_listen(gw_cfg.get("listen", "")) if gw_cfg else "http://127.0.0.1:7863"

    api_key = pick(args.key, "WB2A_API_KEY", "api_key", gw_cfg.get("api_key", ""))

    auth_dir = pick(args.auth_dir, "WB2A_AUTH_DIR", "auth_dir", "")
    if not auth_dir:
        # 网关配置里的 auth_dir 是相对它自己工作目录的
        raw = gw_cfg.get("auth_dir", "")
        if raw:
            base_dir = Path(gw_cfg_path).parent if gw_cfg_path else HERE
            cand = (base_dir / raw).resolve() if not os.path.isabs(raw) else Path(raw)
            auth_dir = str(cand)

    bin_dir = pick(args.bin_dir, "WB2A_BIN_DIR", "bin_dir", "")
    if not bin_dir and gw_cfg_path:
        # 两种常见布局都要认：
        #   wb2a/app/config.json  → 程序与配置同级（本仓的源码部署形态）
        #   wb2a/config.json      → 程序在 app/ 子目录
        cfg_dir = Path(gw_cfg_path).parent
        for cand in (cfg_dir, cfg_dir / "app"):
            if (cand / "login").is_file():
                bin_dir = str(cand)
                break

    port = args.port or int(os.environ.get("WB2A_PANEL_PORT") or panel_cfg.get("port") or DEFAULT_PORT)
    host = pick(args.host, "WB2A_PANEL_HOST", "host", DEFAULT_HOST)

    return Config(base, api_key, auth_dir, bin_dir, port, gw_cfg_path, host)


# ──────────────────────────────────────────────────────────────────────
# 网关 HTTP 客户端
# ──────────────────────────────────────────────────────────────────────

def http(method: str, url: str, body=None, headers=None, timeout=60):
    """返回 (status, 解析后的 JSON 或原始文本)。status=0 表示连接层失败。"""
    data = None
    hdrs = {"Accept": "application/json"}
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def extract_error_message(res) -> str:
    """从网关错误响应里取 message。

    网关的错误体是 {"error":{"code":..,"message":..,"type":..}}，
    但管理端点在某些分支用的是顶层 {"error":"..."} 或纯文本，
    这里把三种形态都兜住。
    """
    if isinstance(res, dict):
        msg = str(res.get("message") or "")
        if msg:
            return msg
        err = res.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")
        if isinstance(err, str):
            return err
        return ""
    return str(res)


class Panel:
    """面板的全部业务逻辑。与 HTTP 层分离，便于测试。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # 登录会话：gen 是会话代号，取消或新开时 +1，后台线程据此自行退出
        self._lock = threading.Lock()
        self._login_busy = threading.Lock()
        self.session = {"gen": 0, "stage": "idle", "message": "", "url": "", "realm": ""}

    # — 网关转发 —————————————————————————————————————————
    def gateway(self, path: str, timeout=60):
        return http("GET", self.cfg.gateway + path, None,
                    {"Authorization": "Bearer " + self.cfg.api_key}, timeout)

    # — 运行时 CLI 工具 ————————————————————————————————————
    def run_tool(self, name: str, args=None, timeout=CLI_TIMEOUT):
        """跑网关自带 CLI，返回 {ok, output}。

        输出按 bytes 读、errors='replace' 解码：签到结果含非 UTF-8 字节，
        用 text=True 会直接抛 UnicodeDecodeError。
        """
        path = self.cfg.tool(name)
        if not path:
            return {"ok": False, "output": "该功能需要网关的 %s 工具（当前未检测到）" % name}
        try:
            p = subprocess.run([str(path)] + (args or []), cwd=str(self.cfg.bin_dir),
                               capture_output=True, timeout=timeout)
            out = (p.stdout or b"").decode("utf-8", "replace").strip()
            err = (p.stderr or b"").decode("utf-8", "replace").strip()
            return {"ok": p.returncode == 0, "output": (out or err)[-2000:] or "(无输出)"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": "执行超时"}
        except Exception as e:
            return {"ok": False, "output": str(e)}

    def run_login_cli(self, args, timeout=60):
        path = self.cfg.tool("login")
        if not path:
            return -1, "", "login 工具不可用"
        try:
            p = subprocess.run([str(path)] + args, cwd=str(self.cfg.bin_dir),
                               capture_output=True, text=True, timeout=timeout)
            return p.returncode, p.stdout.strip(), p.stderr.strip()
        except subprocess.TimeoutExpired:
            return -1, "", "timeout"
        except Exception as e:
            return -1, "", str(e)

    def credit_map(self) -> dict:
        """实时积分：跑 credit CLI 拿权威数据。

        网关 /status 的 credits 只在签到/冷却/报错等事件时被动更新，平时是 0；
        面板要显示真实余额就得主动查（CLI 一次查全部账号）。
        """
        out = {}
        path = self.cfg.tool("credit")
        if not path:
            return out
        try:
            p = subprocess.run([str(path)], cwd=str(self.cfg.bin_dir),
                               capture_output=True, timeout=CLI_TIMEOUT)
            if p.returncode != 0:
                return out
            for a in (json.loads(p.stdout.decode("utf-8", "replace")).get("accounts") or []):
                if a.get("uid"):
                    out[a["uid"]] = a
        except Exception:
            pass
        return out

    # — 组合数据 ——————————————————————————————————————————
    def status(self):
        """账号状态 + 实时积分合并。"""
        code, st = self.gateway("/status")
        if not isinstance(st, dict) or st.get("error"):
            return st
        credits = self.credit_map()
        if credits:
            for a in (st.get("accounts") or []):
                c = credits.get(a.get("uid"))
                if c:
                    a["credits"] = c.get("remain")
                    a["credits_used"] = c.get("used")
                    a["credits_size"] = c.get("size")
                    a["credits_live"] = True
            st["credits_total"] = sum((c.get("remain") or 0) for c in credits.values())
            st["credits_size_total"] = sum((c.get("size") or 0) for c in credits.values())
        return st

    def models(self):
        """模型目录，按区域前缀分组。"""
        code, raw = self.gateway("/v1/models")
        if not isinstance(raw, dict):
            return {"error": "网关返回异常", "raw": str(raw)[:200]}
        by_realm = {"cn": [], "global": [], "bare": []}
        for m in (raw.get("data") or []):
            mid = m.get("id", "")
            if ":" in mid:
                reg, _, bare = mid.partition(":")
                by_realm.setdefault(reg, []).append(bare)
            else:
                by_realm["bare"].append(mid)
        return {"models": by_realm}

    def endpoint_info(self):
        """下游接入信息。

        局域网地址只在面板确实监听在回环之外时给出：绑定回环时它不可达（是误导
        信息），何况内网地址属于拓扑信息，没必要默认展示给每个打开页面的人。
        """
        lan = ""
        if self.cfg.exposed:
            try:
                sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sk.connect(("223.5.5.5", 53))   # 不发包，只让内核选出口网卡
                lan = sk.getsockname()[0]
                sk.close()
            except Exception:
                lan = ""
        return {
            "gateway": self.cfg.gateway,
            "api_key": self.cfg.api_key,
            "base_url": self.cfg.gateway + "/v1",
            "base_url_lan": ("http://%s%s/v1" % (lan, self._port_suffix())) if lan else "",
            "panel_exposed": self.cfg.exposed,
            "capabilities": {
                "login": self.cfg.can_login,
                "credit": self.cfg.can_credit,
                "checkin": self.cfg.can_checkin,
                "trial": self.cfg.can_trial,
                "auth_dir": str(self.cfg.auth_dir) if self.cfg.auth_dir else "",
                "bin_dir": str(self.cfg.bin_dir) if self.cfg.bin_dir else "",
            },
        }

    def _port_suffix(self) -> str:
        m = re.search(r":(\d+)$", self.cfg.gateway)
        return ":" + m.group(1) if m else ""

    # — 登录流程 ——————————————————————————————————————————
    def login_start(self, realm: str):
        if not self.cfg.can_login:
            return {"error": "新增账号需要与网关同机部署（要能调到它的 login 工具与 auths 目录）。"
                             "可改用网关自带的 login.sh 登录，本面板会自动读到新账号。"}
        if not self._login_busy.acquire(blocking=False):
            return {"error": "另一个登录流程正在进行（login 工具的 state 是单文件，不能并发）。"
                             "等它结束或超时后再试。"}
        with self._lock:
            gen = self.session.get("gen", 0) + 1
            self.session = {"gen": gen, "stage": "starting", "message": "正在申请登录地址…",
                            "url": "", "realm": realm}
        rc, out, err = self.run_login_cli(["--realm=%s" % realm, "url"])
        url = ""
        for line in out.splitlines():
            m = re.search(r"https?://\S+", line)
            if m:
                url = m.group(0)
                break
        if rc != 0 or not url:
            self._login_busy.release()
            with self._lock:
                self.session = {"gen": gen, "stage": "error",
                                "message": "申请登录地址失败：%s" % (err or out)[:200],
                                "url": "", "realm": realm}
            return dict(self.session)
        with self._lock:
            self.session = {"gen": gen, "stage": "waiting", "message": "等待浏览器完成登录…",
                            "url": url, "realm": realm}
        threading.Thread(target=self._login_worker, args=(gen, realm, url), daemon=True).start()
        return {"ok": True, "url": url, "stage": "waiting"}

    def _login_worker(self, gen: int, realm: str, url: str):
        """后台轮询登录结果；成功后落盘。

        每轮先核对 gen：取消或新开会话都会 +1，发现自己被取代就退出。否则被取消的
        线程会继续 poll，而新会话已覆写同一个 state 文件 —— 它会拿新会话的结果去落盘。
        """
        deadline = time.time() + LOGIN_TIMEOUT
        while time.time() < deadline:
            time.sleep(3)
            with self._lock:
                if self.session.get("gen") != gen:
                    return
            rc, out, _ = self.run_login_cli(["--realm=%s" % realm, "poll"])
            if rc != 0:
                continue                    # 未完成：CLI 以非零退出
            try:
                payload = json.loads(out)
            except ValueError:
                continue
            if not payload.get("access_token"):
                continue
            with self._lock:
                if self.session.get("gen") != gen:
                    return
            try:
                self._write_auth_file(payload)
            except Exception as e:
                with self._lock:
                    if self.session.get("gen") == gen:
                        self.session = {"gen": gen, "stage": "error",
                                        "message": "写入凭据失败：%s" % e,
                                        "url": url, "realm": realm}
                return
            extra = ""
            if realm == "global":
                extra = self._complete_global_region()
            with self._lock:
                if self.session.get("gen") != gen:
                    return
                self.session = {"gen": gen, "stage": "done",
                                "message": "已加入账号池：%s%s" % (
                                    payload.get("nickname") or payload.get("uid"), extra),
                                "url": url, "realm": realm}
            return
        with self._lock:
            if self.session.get("gen") == gen:
                self.session = {"gen": gen, "stage": "timeout",
                                "message": "登录超时（%d 分钟），请重新发起" % (LOGIN_TIMEOUT // 60),
                                "url": url, "realm": realm}

    def _complete_global_region(self) -> str:
        """Global 账号需要完善注册地区，否则 chat 报 14017。"""
        script = (self.cfg.bin_dir or Path(".")) / "scripts" / "global_region.py"
        if not script.is_file():
            return ""
        try:
            subprocess.run([sys.executable, str(script)], cwd=str(self.cfg.bin_dir),
                           capture_output=True, text=True, timeout=90)
            return "（已尝试完善国际版注册地区）"
        except Exception:
            return "（注册地区完善未完成，若 chat 报 14017 请手动跑 scripts/global_region.py）"

    def _write_auth_file(self, payload: dict) -> Path:
        """按网关的格式原子写入 auth 文件（tmp + rename，权限 0600）。"""
        auth = {
            "account": {
                "uid": payload.get("uid", ""),
                "enterpriseId": payload.get("enterprise_id", ""),
                "nickname": payload.get("nickname", ""),
            },
            "auth": {
                "accessToken": payload.get("access_token", ""),
                "refreshToken": payload.get("refresh_token", ""),
                "expiresAt": int(time.time()) + int(payload.get("expires_in") or 0),
                "domain": payload.get("domain", ""),
                "realm": payload.get("realm", ""),
            },
        }
        auth_dir = self.cfg.auth_dir
        auth_dir.mkdir(parents=True, exist_ok=True)
        target = auth_dir / ("workbuddy-%s.json" % payload.get("uid", ""))
        fd, tmp = tempfile.mkstemp(prefix=".workbuddy-auth-", dir=str(auth_dir))
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(auth, fh, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target

    def login_cancel(self):
        if self._login_busy.locked():
            try:
                self._login_busy.release()
            except RuntimeError:
                pass
        with self._lock:
            # gen+1 让后台线程下一轮自行退出
            self.session = {"gen": self.session.get("gen", 0) + 1, "stage": "idle",
                            "message": "", "url": "", "realm": ""}

    # — 账号运维（走网关管理端点，需上游支持）————————————————
    def account_op(self, action: str, uid: str, reason: str = ""):
        payload = None
        if action == "disable":
            payload = json.dumps({"reason": reason or "panel"}).encode()
        code, res = http("POST", "%s/admin/accounts/%s/%s" % (
            self.cfg.gateway, urllib.parse.quote(uid), action),
            payload, {"Authorization": "Bearer " + self.cfg.api_key}, timeout=30)
        if code == 404:
            msg = extract_error_message(res)
            # 两种 404 要区分开：网关用 message 区分「开关没开」与「uid 不存在」，
            # 但 message 嵌在 error 对象里（{"error":{"code":..,"message":..}}），
            # 只读顶层会永远拿不到，两种 404 就分不开了。
            if "account not found" in msg:
                return {"error": "账号不存在：%s" % uid}
            return {"error": "网关未开启管理端点（404）。需在网关 config.json 里设置 "
                             '"admin": {"enabled": true} 并重启。'
                             + ("　网关原文：%s" % msg[:120] if msg else "")}
        if code == 401:
            return {"error": "api_key 不对（401）"}
        if code != 200:
            return {"error": "%d: %s" % (code, str(res)[:200])}
        return res if isinstance(res, dict) else {"error": str(res)}


# ──────────────────────────────────────────────────────────────────────
# HTTP 服务
# ──────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "wb2a-panel/" + __version__
    panel: Panel = None                      # 由 main 注入

    def log_message(self, fmt, *args):
        sys.stderr.write("[panel] %s %s\n" % (self.command, self.path.split("?")[0]))

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode()
        elif isinstance(payload, str):
            body = payload.encode()
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 面板可能被放在反向代理后面（含浏览器缓存的凭据），这三条都是零成本兜底：
        # nosniff 阻止把返回体当别的类型解释，DENY 挡掉点击劫持，no-referrer 不泄漏地址
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except ValueError:
            return {}

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        p = self.panel
        if path in ("/", "/index.html"):
            html = (HERE / "index.html").read_text(encoding="utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if path == "/api/status":
            return self._send(200, p.status())
        if path == "/api/models":
            return self._send(200, p.models())
        if path == "/api/stats":
            return self._send(200, p.gateway("/v1/stats")[1])
        if path == "/api/endpoint":
            return self._send(200, p.endpoint_info())
        if path == "/api/login/status":
            with p._lock:
                return self._send(200, dict(p.session))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        # CSRF 闸门：状态变更接口只认带自定义头的请求。
        # 反向代理上的 Basic 认证凭据是浏览器自动附带的，跨站页面也能触发 POST
        # （表单 + sendBeacon 都不需要预检）；而自定义头会强制预检，被 CORS 挡下。
        # 纯 API 调用者补一个 -H 'X-Panel-Request: 1' 即可。
        if self.headers.get("X-Panel-Request") != "1":
            return self._send(403, {"error": "拒绝跨站请求：缺少 X-Panel-Request: 1 头。"
                                             "用脚本调用时请显式加上；浏览器里正常点击不受影响。"})
        path = self.path.split("?", 1)[0]
        p = self.panel
        body = self._body()

        if path == "/api/login/start":
            realm = (body.get("realm") or "cn").strip().lower()
            if realm not in ("cn", "global"):
                return self._send(400, {"error": "realm 只能是 cn 或 global"})
            return self._send(200, p.login_start(realm))
        if path == "/api/login/cancel":
            p.login_cancel()
            return self._send(200, {"ok": True})
        if path in ("/api/account/disable", "/api/account/enable"):
            uid = (body.get("uid") or "").strip()
            if not uid:
                return self._send(400, {"error": "缺少 uid"})
            action = "disable" if path.endswith("disable") else "enable"
            return self._send(200, p.account_op(action, uid, body.get("reason") or ""))
        if path == "/api/checkin":
            return self._send(200, p.run_tool("signin_bin"))
        if path == "/api/trial":
            return self._send(200, p.run_tool("trial_bin"))
        return self._send(404, {"error": "not found"})


# ──────────────────────────────────────────────────────────────────────
# 启动
# ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="panel.py", description="workbuddy2api 轻量 Web 管理面板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  python3 panel.py                                   # 自动探测网关配置
  python3 panel.py --base http://127.0.0.1:7863 --key sk-xxx
  WB2A_API_KEY=sk-xxx python3 panel.py               # 用环境变量
""")
    ap.add_argument("--base", help="网关地址（默认自动探测，或取自网关 config.json 的 listen）")
    ap.add_argument("--key", help="网关 api_key")
    ap.add_argument("--auth-dir", help="账号凭据目录（启用「新增账号」需要）")
    ap.add_argument("--bin-dir", help="网关程序目录（含 login/credit 等工具）")
    ap.add_argument("--gateway-config", help="网关 config.json 路径（默认自动探测）")
    ap.add_argument("--config", help="面板自己的配置（默认 ./panel.json，可无）")
    ap.add_argument("--port", type=int, help="面板监听端口（默认 %d）" % DEFAULT_PORT)
    ap.add_argument("--host", default=None,
                    help="面板监听地址（默认 %s，仅本机可访问）。"
                         "要让局域网其他机器访问填 0.0.0.0 —— "
                         "注意面板持有网关 api_key，仅在可信网络下这样做。" % DEFAULT_HOST)
    ap.add_argument("--version", action="version", version="wb2a-panel " + __version__)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args)

    if not cfg.api_key:
        print("✗ 没找到网关 api_key。用 --key / WB2A_API_KEY 传入，"
              "或确保能读到网关的 config.json。", file=sys.stderr, flush=True)
        return 1

    panel = Panel(cfg)
    code, st = panel.gateway("/status", timeout=15)
    if code != 200:
        print("✗ 连不上网关 %s：HTTP %s %s" % (cfg.gateway, code, str(st)[:200]), file=sys.stderr)
        print("  确认网关在运行，且 --base 指向正确。", file=sys.stderr, flush=True)
        return 1

    Handler.panel = panel
    caps = []
    if cfg.can_login:
        caps.append("新增账号")
    if cfg.can_credit:
        caps.append("实时积分")
    if cfg.can_checkin:
        caps.append("签到")
    if cfg.can_trial:
        caps.append("试用领取")

    # flush=True 是必要的：stdout 非 tty（重定向 / Docker 日志）时 Python 会缓冲，
    # 用户看不到启动信息会以为卡死。
    print("✓ 已连接网关 %s（%s 个账号）" % (cfg.gateway, st.get("total", "?")), flush=True)
    if cfg.gateway_config:
        print("  配置来自 %s" % cfg.gateway_config, flush=True)
    print("  增强功能：%s" % ("、".join(caps) if caps else "无（核心功能可用；"
          "把面板部署到网关同机可解锁新增账号/签到/实时积分）"), flush=True)
    print("✓ 面板地址：http://127.0.0.1:%d" % cfg.port, flush=True)
    if cfg.exposed:
        print("⚠ 正在监听 %s（局域网可访问）。面板持有网关 api_key，"
              "请确认处于可信网络，且不要暴露到公网。" % cfg.host, file=sys.stderr, flush=True)

    try:
        ThreadingHTTPServer((cfg.host, cfg.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
