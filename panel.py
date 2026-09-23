#!/usr/bin/env python3
"""wb2a-panel — workbuddy2api 的轻量 Web 管理面板（可带 cline2api 第二网关）。

设计取舍（四句话）：
  1. **零依赖**：只用 Python 标准库。不需要 pip install，不需要编译。
  2. **不 fork 网关**：纯外部面板，只通过网关自己的 HTTP 接口工作，
     上游怎么更新都不用跟着改。
  3. **优雅降级**：核心功能（账号池 / 模型目录 / 请求统计 / 停用启用）只需
     网关地址 + api_key；增强功能（新增账号 / 签到 / 领试用 / 实时积分）需要
     与网关同机部署、能调到它的 CLI 工具，检测到才启用。
  4. **多网关可选**：配置了 cline2api 就多一节「Cline 免费层」（账号池 + 定价闸门
     台账 + 设备授权登录），没配置则整节不出现，行为与单网关版完全一致。

跑起来：
    python3 panel.py --base http://127.0.0.1:7863 --key sk-xxx
或让它自己找网关配置：
    python3 panel.py
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
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

__version__ = "0.5.0"

HERE = Path(__file__).resolve().parent
DEFAULT_PORT = 8321
DEFAULT_HOST = "127.0.0.1"     # 默认只监听回环：面板持有网关 api_key
LOGIN_TIMEOUT = 300          # 登录会话有效期（秒）
CLI_TIMEOUT = 150            # 运维 CLI 超时
TASK_LOG_CAP = 2000          # 任务作业日志环形缓冲行数上限
TASK_ITEMS_CAP = 400         # 任务项表格上限（超出只截断展示，不影响执行）

# 不走任何 HTTP 代理：面板只访问本机网关与本机网络。
# 宿主机常有 http_proxy（如 127.0.0.1:7890），不加这行会把回环请求也代理出去。
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ──────────────────────────────────────────────────────────────────────
# 配置发现：命令行 > 环境变量 > 配置文件 > 自动探测网关 config.json
# ──────────────────────────────────────────────────────────────────────

class Config:
    def __init__(self, gateway: str, api_key: str, auth_dir: str, bin_dir: str,
                 port: int, gateway_config: str = "", host: str = "127.0.0.1",
                 cline: dict | None = None, script_dir: str = ""):
        self.gateway = gateway.rstrip("/")
        self.api_key = api_key
        self.auth_dir = Path(auth_dir).expanduser() if auth_dir else None
        self.bin_dir = Path(bin_dir).expanduser() if bin_dir else None
        self.port = port
        self.gateway_config = gateway_config
        self.host = host
        # 第二网关（cline2api）: {"base", "api_key", "admin_token", "config_path"}。
        # None 时面板行为与单网关版完全一致（老部署零改动）。
        self.cline = cline or None
        # 任务脚本目录（网关 app/scripts/）。显式覆盖优先，否则从 bin_dir 推导。
        self.script_dir = Path(script_dir).expanduser() if script_dir else None

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

    # — 任务脚本（一键完成成长任务）—————————————————————————
    @property
    def resolved_script_dir(self):
        """任务脚本目录。显式指定优先；否则从 bin_dir / gateway_config 推导。

        做成属性而不是在构造时算死：显式构造 Config 的调用方（测试、嵌入式用法）
        不必自己推导；同时也让「先设 bin_dir 再问脚本」这种顺序更自然。
        """
        if self.script_dir:
            return self.script_dir
        for base in (self.bin_dir, Path(self.gateway_config).parent if self.gateway_config else None):
            if not base:
                continue
            for cand in (Path(base) / "scripts", Path(base)):
                if (cand / "task_runner.py").is_file():
                    return cand
        return None

    def task_script(self, name: str):
        """返回网关 scripts/ 下某个任务脚本的路径（不存在则 None）。

        脚本是 .py（不是加执行位的二进制），所以不能用 tool() 那套「有执行位」
        判定；找到文件即可，解释器由 python_exe 提供。
        """
        d = self.resolved_script_dir
        if not d:
            return None
        p = Path(d) / name
        return p if p.is_file() else None

    @property
    def python_exe(self):
        """跑任务脚本的解释器路径（不存在则 None）。

        sys.executable 优先（面板自己在 python3 里跑，用它最稳）；某些打包/嵌入
        场景下它是空串，再退回 PATH 上的 python3。
        """
        if sys.executable and Path(sys.executable).is_file():
            return sys.executable
        return shutil.which("python3") or None

    @property
    def can_tasks(self) -> bool:
        """一键任务需要：脚本在位 + 能读凭据 + 有解释器。

        三者缺一都会让子进程直接失败，所以前置判定，避免点了才报错。
        """
        return (self.task_script("task_runner.py") is not None
                and self.auth_dir is not None
                and self.python_exe is not None)


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

    # 任务脚本目录：显式指定优先（--script-dir / WB2A_SCRIPT_DIR），
    # 否则由 Config.resolved_script_dir 从 bin_dir / gateway_config 推导。
    script_dir = pick(getattr(args, "script_dir", ""), "WB2A_SCRIPT_DIR", "script_dir", "")

    cline = load_cline_config(args, panel_cfg)

    return Config(base, api_key, auth_dir, bin_dir, port, gw_cfg_path, host, cline, script_dir)


def load_cline_config(args, panel_cfg: dict) -> dict | None:
    """第二网关（cline2api）的连接参数。没配置就返回 None（面板退回单网关形态）。

    与 wb2a 一样走四级发现：命令行 > 环境变量 > panel.json > cline2api 的 config.json。
    最后一级能自动凑齐：listen→base、api_key、admin_token 都在它的 config.json 里。
    """
    explicit = (getattr(args, "cline_config", "") or os.environ.get("CLINE2API_CONFIG", "")
                or panel_cfg.get("cline_config") or "")
    cfg_path = Path(explicit).expanduser() if explicit else Path("/opt/cline2api/config.json")
    gw = {}
    if cfg_path.exists():
        try:
            gw = json.loads(cfg_path.read_text(encoding="utf-8"))
        except PermissionError as e:
            # 这是最容易被静默吞掉的一种失败：文件存在、路径对，只是本进程读不到。
            # 表现为「面板未配置 cline2api」，跟没部署这个网关长得一样，很难查。
            # cline2api 的 deploy 脚本每次都重设权限，所以务必说清怎么办。
            print("✗ 读不到 cline2api 配置 %s：%s" % (cfg_path, e), file=sys.stderr, flush=True)
            print("  该文件应是 640 且属组含本进程用户（面板以 wb2a 运行、属 cline2api 组）。"
                  "修：sudo chgrp cline2api %s && sudo chmod 640 %s" % (cfg_path, cfg_path),
                  file=sys.stderr, flush=True)
        except (OSError, ValueError) as e:
            print("! 读取 cline2api 配置失败（%s）：%s" % (cfg_path, e), file=sys.stderr, flush=True)

    def pick(cli_value, env_name, cfg_key, fallback=""):
        if cli_value:
            return cli_value
        env = os.environ.get(env_name, "").strip()
        if env:
            return env
        if cfg_key in panel_cfg and panel_cfg[cfg_key]:
            return panel_cfg[cfg_key]
        return fallback

    base = pick(getattr(args, "cline_base", ""), "CLINE2API_BASE", "cline_base")
    if not base and gw.get("listen"):
        base = normalize_listen(gw["listen"])
    api_key = pick(getattr(args, "cline_key", ""), "CLINE2API_API_KEY", "cline_api_key",
                   gw.get("api_key", ""))
    admin = pick(getattr(args, "cline_admin_token", ""), "CLINE2API_ADMIN_TOKEN",
                 "cline_admin_token", gw.get("admin_token", "") or api_key)
    if not base or not admin:
        return None
    return {"base": base.rstrip("/"), "api_key": api_key, "admin_token": admin,
            "config_path": str(cfg_path) if cfg_path.is_file() else ""}


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


# ──────────────────────────────────────────────────────────────────────
# 任务脚本输出解析
# ──────────────────────────────────────────────────────────────────────
#
# 脚本没有结构化输出，界面进度只能从日志行解析。所以这个解析器是「尽力而为」的：
# 认不出的行一律降级成原始日志，绝不猜状态 —— 界面宁可少显示，也不能显示错的进度。
# 脚本随上游（Sliverkiss）演进会改文案，识别失败时 degrade 而不是报错。

# 账号表头：== 00e26541（昵称 A） ==
_ACC_HEAD = re.compile(r"^==\s*([0-9a-zA-Z_-]+)\s*(?:[（(](.*?)[）)])?\s*==\s*$")
# 任务项：[task_runner] 00e26541 chat_5: report 1/5 200 code=0
# 也覆盖 [school2026] 前缀，与无 code 的账号级事件（query/lottery/draw）。
_TASK_LINE = re.compile(
    r"^\[(?:task_runner|school2026)\]\s+([0-9a-zA-Z_-]+)\s+(.+)$")
# 汇总行：task_runner done: accounts=3 total=57 ok=20 ... credit=+1200 energy=+40
_SUMMARY = re.compile(r"^(?:task_runner|school2026)\s+done:\s*(.*)$")
# 账号级 skip / 错误
_SKIP_GLOBAL = re.compile(r"^\[skip\]\s+([0-9a-zA-Z_-]+)\s")
_CODE_TOKEN = re.compile(r"^([A-Za-z][A-Za-z0-9_.]*)\s*:\s*(.*)$")
_REWARD = re.compile(r"(credit|energy)=([+-]?\d+)")
# 任务码里含「:」的形态（如 Expert_team_use_3）不会出现；但 "query 任务不存在" 这类
# 无 code 的账号级事件必须能落回账号而不是被当成任务。

# 状态判定：按「先具体后笼统」顺序匹配，命中即停。顺序即优先级。
_STATUS_RULES = [
    # 失败
    ("error", ("-> ERR", "claim 失败", "report 失败", "处理失败", "拉取失败",
               "激活失败", "list_tasks 失败", "报告失败", "query 失败")),
    # 已完成（本轮入账）
    ("done", ("claim 200 ok(", "claim 200 ok", "-> claimed（本轮已入账）",
              "（点亮）", "-> claimed", "buddy/first -> 200")),
    # 已领 / 已完成（无需动作）
    ("already", ("已领，跳过", "已完成/已领", "already_claimed", "无需上报", "无需抽奖",
                 "余额=0", "balance=0")),
    # 待下次（有进展但未达目标：服务端异步归账、时段未到、登记未生效）
    ("pending", ("未达 target", "WARN 待下次", "skip pending", "未登记生效",
                 "only_claim 跳过", "可稍后补领", "部分点亮", "未变化")),
    # 跳过（不可伪造 / 未映射 / 不存在）
    ("skip", ("不可伪造", "任务不存在", "非映射任务", "未映射(人工/未知)", "人工环节",
              "不存在，skip", "skip")),
    # 扫描态（dry-run：只报告将做什么）
    ("planned", ("dry-run 跳过", "dry-run 不写", "dry-run：将", "dry-run")),
    # 进行中（上报/激活过程中）
    ("running", ("report ", "viewed 激活", "accept 尝试", "前置解锁", "share-complete")),
]


def classify_line(text: str) -> str:
    """把一条任务描述判成状态。命中第一条规则即返回；都不中返回空串（未知）。

    未知返回空串而不是 "unknown"：调用方据此把整行原样留在日志里，不生成假的
    进度条目 —— 脚本改了文案时，界面显示的是「日志里有这行」而不是「这行是错误」。
    """
    for status, markers in _STATUS_RULES:
        for m in markers:
            if m in text:
                return status
    return ""


def parse_rewards(text: str) -> tuple[int, int]:
    """从一行里提 (credit, energy) 增量。脚本的形态是 credit=+1200 energy=+40。"""
    credit = energy = 0
    for kind, val in _REWARD.findall(text):
        try:
            n = int(val)
        except ValueError:
            continue
        if kind == "credit":
            credit += n
        else:
            energy += n
    return credit, energy


def parse_summary(text: str) -> dict:
    """解析汇总行的 k=v 序列。认不出的 key 原样带上，界面按需取用。"""
    out = {}
    for k, v in re.findall(r"([a-z_]+)=([+-]?\d+)", text):
        try:
            out[k] = int(v)
        except ValueError:
            continue
    return out


class TaskProgress:
    """一个任务作业的进度累积器。

    与 HTTP 层分离、与 subprocess 也分离：喂给它一行文本即可，便于单测。
    线程安全：只有作业线程写、HTTP 线程读，用锁保护。
    """

    def __init__(self, log_cap: int = TASK_LOG_CAP, items_cap: int = TASK_ITEMS_CAP):
        self._lock = threading.Lock()
        self._log = []               # [(seq, text)]
        self._seq = 0
        self._items = {}             # (uid8, code) -> item dict
        self._order = []             # item key 的稳定顺序
        self._accounts = {}          # uid8 -> 昵称
        self.summary = {}            # 汇总行解析结果
        self.error = ""              # 解析/执行层面的错误（非任务失败）
        self.log_cap = log_cap
        self.items_cap = items_cap

    # — 写（作业线程）————————————————————————————————————
    def feed(self, line: str):
        """喂一行输出。不抛异常：单行解析问题不能中断作业。"""
        text = (line or "").rstrip("\n\r")
        with self._lock:
            self._seq += 1
            self._log.append((self._seq, text))
            if len(self._log) > self.log_cap:
                # 环形：丢最早的一半，保留近端。整段删比逐行删省事且不抖。
                self._log = self._log[len(self._log) - self.log_cap:]
        try:
            self._interpret(text)
        except Exception as e:                      # 解析永不影响执行
            with self._lock:
                if not self.error:
                    self.error = "解析告警：%s" % e

    def _interpret(self, text: str):
        m = _SUMMARY.match(text)
        if m:
            with self._lock:
                self.summary.update(parse_summary(m.group(1)))
            return

        m = _ACC_HEAD.match(text)
        if m:
            uid, nick = m.group(1), (m.group(2) or "").strip()
            with self._lock:
                if nick:
                    self._accounts[uid] = nick
            return

        m = _SKIP_GLOBAL.match(text)
        if m:
            self._add_item(m.group(1), "", text, "skip", nick_hint=True)
            return

        if text.startswith("ERR:"):
            # 账号级错误（如 list_tasks 失败）：挂到 uid（若有）而不是丢进虚无
            uid = ""
            inner = re.search(r"\[(?:task_runner|school2026)\]\s+([0-9a-zA-Z_-]+)", text)
            if inner:
                uid = inner.group(1)
            self._add_item(uid, "", text, "error")
            return

        m = _TASK_LINE.match(text)
        if not m:
            return                                   # 无关行：留在原始日志里
        uid, rest = m.group(1), m.group(2)
        status = classify_line(rest)
        cm = _CODE_TOKEN.match(rest)
        # 只有「像任务码」的才认成任务：排除 query/lottery/viewed/accept 这类动作词。
        # 否则 "query energy balance=3" 会被当成名为 query 的任务。
        code = ""
        if cm and _looks_like_code(cm.group(1)):
            code = cm.group(1)
        self._add_item(uid, code, rest, status or "")

    def _add_item(self, uid, code, text, status, nick_hint=False):
        if not uid and not code:
            return
        key = (uid, code)
        credit, energy = parse_rewards(text)
        with self._lock:
            it = self._items.get(key)
            if it is None:
                it = {"uid": uid, "code": code, "status": status or "",
                      "message": text, "credit": 0, "energy": 0, "events": 0}
                if len(self._items) >= self.items_cap:
                    return                            # 超上限：只留日志，不加表
                self._items[key] = it
                self._order.append(key)
            # 后写覆盖先写：同一任务的终态总在最后出现（脚本是先后报后回读再领奖）。
            # 但 running 不该覆盖已经落定的终态 —— 脚本偶尔会补一行中间态。
            if status and not (it["status"] in ("done", "already", "error") and status == "running"):
                it["status"] = status
            it["message"] = text
            it["events"] += 1
            it["credit"] += credit
            it["energy"] += energy

    # — 读（HTTP 线程）————————————————————————————————————
    def snapshot(self) -> dict:
        with self._lock:
            items = []
            for key in self._order:
                it = self._items.get(key)
                if not it:
                    continue
                d = dict(it)
                d["nickname"] = self._accounts.get(it["uid"], "")
                items.append(d)
            return {
                "items": items,
                "accounts": dict(self._accounts),
                "summary": dict(self.summary),
                "error": self.error,
                "log_lines": self._seq,
            }

    def since(self, seq: int):
        """增量日志：seq 之后的 (seq, text) 列表。轮询用，避免整段重传。"""
        with self._lock:
            return [(s, t) for (s, t) in self._log if s > seq]


def _looks_like_code(token: str) -> bool:
    """判断一个 token 是不是任务码，而不是动作词。

    脚本的动作词是 query/report/claim/accept/viewed/draw/lottery 等（小写、无下划线
    或全小写），任务码则形如 chat_5 / RichMeow_Chat / Expert_team_use_3 /
    Sequential_Tasks_1 —— 含下划线+数字，或含大写字母。这条判定不追求完备，
    错了也只是把一个动作词当任务显示，不会影响执行。
    """
    if "_" in token or any(c.isupper() for c in token):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────
# 任务作业（一键完成成长任务）
# ──────────────────────────────────────────────────────────────────────

class TaskJob:
    """一次任务脚本执行。生命周期：pending → running → done/failed/cancelled。

    为什么不像 run_tool() 那样同步阻塞：单账号全量含真实对话任务（专家召唤 5 次、
    每次间隔 6 秒）实测 1–4 分钟，全账号是十几分钟级。HTTP 层挂这么久会被浏览器、
    反代、systemd 任何一环断开。所以后台线程跑进程、HTTP 只做启动与轮询 ——
    与登录会话（Panel.session + _login_worker）同一套模式。
    """

    def __init__(self, kind: str, argv: list, cwd: str, env: dict, label: str,
                 prog: TaskProgress, lockfile: str = "", on_exit=None):
        self.id = "%s-%d" % (kind, int(time.time() * 1000))
        self.kind = kind                # scan | run | school
        self.label = label
        self.argv = argv
        self.cwd = cwd
        self.env = env
        self.prog = prog
        self.lockfile = lockfile
        self.on_exit = on_exit          # 收尾回调（面板用它释放作业互斥）
        self.state = "pending"          # pending | running | done | failed | cancelled
        self.started_at = time.time()
        self.ended_at = 0.0
        self.exit_code = None
        self.write = "--yes" in argv    # 是否含真实写操作（界面据此提示）
        self._proc = None
        self._cancelling = False
        self._lock = threading.Lock()

    # — 状态 —————————————————————————————————————————————
    @property
    def elapsed(self) -> float:
        end = self.ended_at or time.time()
        return max(0.0, end - self.started_at)

    @property
    def running(self) -> bool:
        """是否还没收尾。

        只认「进程真的结束了」才翻 False —— 取消时短暂置中间态会让调用方以为
        可以启动新作业，而旧进程还在跑（上游副作用还在发）。
        """
        with self._lock:
            return self.state in ("pending", "running")

    def snapshot(self, since=None) -> dict:
        """作业快照。since 非 None 时附带 seq > since 的增量日志。

        用 None 而不是 0 作默认：前端首次轮询传的就是 0（要拿全量日志），
        把 0 当假值会让它一行都收不到。
        """
        snap = self.prog.snapshot()
        with self._lock:
            state, code = self.state, self.exit_code
        snap.update({
            "job": self.id, "kind": self.kind, "label": self.label,
            "state": state, "running": self.running,
            "write": self.write, "elapsed_sec": round(self.elapsed, 1),
            "exit_code": code,
            "pending": snap.get("summary", {}).get("pending", 0),
            "counts": _count_statuses(snap.get("items") or []),
        })
        if since is not None:
            snap["lines"] = [{"seq": s, "text": t} for (s, t) in self.prog.since(since)]
        return snap

    # — 执行 —————————————————————————————————————————————
    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        """读子进程 stdout 逐行喂给进度累积器，直到进程退出。

        输出按 bytes 读、errors='replace' 解码：与 run_tool() 同口径 —— 脚本输出
        可能含非 UTF-8 字节，text=True 会直接抛 UnicodeDecodeError。

        收尾顺序是有讲究的：先释放锁与回调，再落终态。这样「running=False」就蕴含
        「互斥锁已释放」，调用方看到作业结束即可安全启动下一个（不必轮询等锁）。
        """
        lock_fd = None
        outcome, code = "failed", None
        try:
            if self.lockfile:
                lock_fd = _acquire_lock(self.lockfile)
                if lock_fd is None:
                    self.prog.feed("ERR: 另一个任务作业正在运行（%s 被占用）" % self.lockfile)
                    return
            with self._lock:
                self.state = "running"
            self.prog.feed("$ %s" % " ".join(self.argv))
            self._proc = subprocess.Popen(
                self.argv, cwd=self.cwd, env=self.env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
            if self._cancelling:                  # 启动前就被取消：立刻收手
                self._proc.terminate()
            for raw in iter(self._proc.stdout.readline, b""):
                self.prog.feed(raw.decode("utf-8", "replace"))
            self._proc.stdout.close()
            self._proc.wait()
            code = self._proc.returncode
            outcome = "cancelled" if self._cancelling else ("done" if code == 0 else "failed")
        except FileNotFoundError as e:
            self.prog.feed("ERR: 无法启动任务脚本：%s" % e)
        except Exception as e:
            self.prog.feed("ERR: 任务作业异常：%s" % e)
        finally:
            if lock_fd is not None:
                _release_lock(lock_fd)
            cb, self.on_exit = self.on_exit, None
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass
            self._settle(outcome, code)

    def _settle(self, state: str, code):
        """落终态。只有这里能把状态改成终态，保证收尾动作已经跑完。"""
        with self._lock:
            if self.state in ("done", "failed", "cancelled"):
                return                        # 已收尾（重复调用无害）
            self.state = state
            self.exit_code = code
            self.ended_at = time.time()

    def cancel(self) -> dict:
        """终止子进程。先 SIGTERM 让它自己收尾，超时再 SIGKILL。

        脚本对 SIGTERM 没有专门处理，但 Python 的默认行为就是退出；给它时间是为了
        让已发出的上报请求走完（半途丢弃会让上游状态不明）。
        """
        with self._lock:
            if self.state not in ("pending", "running"):
                return {"ok": False, "error": "没有正在运行的作业"}
            self._cancelling = True
            proc = self._proc
        self.prog.feed("ERR: 收到取消请求，正在终止（已完成未领奖的可稍后「只领奖」补）")
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception as e:
                return {"ok": False, "error": "终止失败：%s" % e}
        # 等 _run 观察到进程退出并落终态（它负责释放面板互斥）
        deadline = time.time() + 10
        while self.running and time.time() < deadline:
            time.sleep(0.05)
        return {"ok": True}


def _count_statuses(items) -> dict:
    out = {}
    for it in items:
        s = it.get("status") or "unknown"
        out[s] = out.get(s, 0) + 1
    return out


def _acquire_lock(path: str):
    """非阻塞抢一把 flock。抢不到返回 None（已有作业在跑）。

    面板进程内的互斥用 _task_busy；这把锁是给「同一台机器上另一个面板实例」
    或运维手动跑的脚本看的 —— 脚本本身不持锁，所以它挡不住网关排程（见设计文档
    第七节的已知边界）。
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────
# 面板业务逻辑
# ──────────────────────────────────────────────────────────────────────

class Panel:
    """面板的全部业务逻辑。与 HTTP 层分离，便于测试。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        # 登录会话：gen 是会话代号，取消或新开时 +1，后台线程据此自行退出
        self._lock = threading.Lock()
        self._login_busy = threading.Lock()
        self.session = {"gen": 0, "stage": "idle", "message": "", "url": "", "realm": ""}
        # 任务作业：同一时刻只允许一个（重复点击 409）。job 是当前/最近一次。
        self._task_busy = threading.Lock()
        self.job: "TaskJob | None" = None

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
                "tasks": self.cfg.can_tasks,
                "auth_dir": str(self.cfg.auth_dir) if self.cfg.auth_dir else "",
                "bin_dir": str(self.cfg.bin_dir) if self.cfg.bin_dir else "",
            },
            "tasks": self.task_capability(),
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

    # — 一键任务（跑网关自带的 task_runner.py / school_open_day_2026.py）———
    #
    # 这是既有的「面板调网关 CLI」通路的延伸（签到/积分/试用同一条路），不是新架构：
    # 脚本自己读 auths/，凭据既不经过面板也不进面板进程内存。
    def _task_env(self) -> dict:
        """子进程环境。

        PYTHONUNBUFFERED 是这套方案里最容易踩的坑：不设的话脚本照常跑完，但面板在
        整个运行期间一行输出都收不到，界面看起来像卡死 —— 而进程其实完全正常。
        PYTHONIOENCODING 兜住服务器 LANG=C 的场景（非 ASCII 打印会炸）。
        WB2A_AUTHS 让脚本按面板解析到的同一处找凭据（面板若用 --auth-dir 覆盖过）。
        """
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        if self.cfg.auth_dir:
            env["WB2A_AUTHS"] = str(self.cfg.auth_dir)
        return env

    def _task_cwd(self):
        """子进程工作目录：脚本按 cwd 找 auths/ 与 scripts/，所以用 bin_dir（网关 app/）。

        bin_dir 缺失也不能用面板目录 —— 脚本会找不到凭据。返回 None 让调用方报错。
        """
        return str(self.cfg.bin_dir) if self.cfg.bin_dir else None

    def _script_argv(self, script: str, args: list) -> list:
        script_path = self.cfg.task_script(script)
        return [self.cfg.python_exe, str(script_path)] + args

    def _account_args(self, accounts) -> list:
        """账号入参：用完整 auths 文件名而不是 uid 前缀。

        load_auth() 对含路径分隔或 .json 结尾的入参按文件名精确匹配 —— 用 uid 前缀
        会撞号（abc123 命中 abc1234 的凭据）。
        """
        out = []
        for a in (accounts or []):
            a = str(a).strip()
            if not a:
                continue
            if a.upper() == "ALL":
                out.append("ALL")
            elif a.endswith(".json"):
                out.append(a)
            else:
                out.append("workbuddy-%s.json" % a)
        return out or ["ALL"]

    def task_start(self, kind: str, accounts=None, only_claim: bool = False,
                   only=None, gap=None, mode: str = "run") -> dict:
        """启动一个任务作业。立即返回，不阻塞到脚本结束。"""
        if not self.cfg.can_tasks:
            missing = []
            if self.cfg.task_script("task_runner.py") is None:
                missing.append("任务脚本（scripts/task_runner.py）")
            if not self.cfg.auth_dir:
                missing.append("凭据目录（--auth-dir）")
            if not self.cfg.python_exe:
                missing.append("python3 解释器")
            return {"error": "一键任务需要与网关同机部署，当前缺少：%s。"
                             "可改用网关自带的 ./scripts/task_runner.py 直接在命令行跑。"
                             % "、".join(missing)}

        cwd = self._task_cwd()
        if not cwd:
            return {"error": "未检测到网关程序目录（--bin-dir），无法确定脚本的工作目录。"
                             "脚本要靠它找到 auths/。"}
        if not self._task_busy.acquire(blocking=False):
            cur = self.job
            return {"error": "已有任务作业在执行（%s）。等它结束或先取消。" % (
                cur.label if cur else "未知"),
                "busy": True, "job": cur.id if cur else ""}

        if kind == "school":
            act = {"list": ["--list"], "run": ["--run", "--yes"],
                   "lottery": ["--lottery-only", "--yes"]}.get(mode, ["--list"])
            argv = self._script_argv("school_open_day_2026.py",
                                     self._account_args(accounts) + act)
            label = {"list": "开学季盘点", "run": "开学季执行",
                     "lottery": "开学季抽奖"}.get(mode, "开学季")
            script = "school_open_day_2026.py"
        else:
            argv = self._script_argv("task_runner.py", self._account_args(accounts))
            if kind == "scan":
                label = "扫描待办（只读）"
            else:
                argv.append("--yes")
                label = "一键完成（只领奖）" if only_claim else "一键完成全部任务"
            if only_claim:
                argv.append("--only-claim")
            for code in (only or []):
                if str(code).strip():
                    argv += ["--only", str(code).strip()]
            if gap:
                try:
                    g = float(gap)
                    if g >= 1.0:
                        argv += ["--gap", str(g)]
                except (TypeError, ValueError):
                    pass
            script = "task_runner.py"

        # 脚本指纹：真实存在才可能跑起来（can_tasks 已判 task_runner；school 单独确认）
        if self.cfg.task_script(script) is None:
            self._task_busy.release()
            return {"error": "缺少脚本 %s（网关 scripts/ 目录下未找到）" % script}

        # 收尾回调在作业线程里释放互斥：与「running 翻 False」同一时刻发生，
        # 不存在「界面以为结束了、锁还没放」的窗口（早先的 reaper 轮询有 0.3s 竞态）。
        job = TaskJob(kind, argv, cwd, self._task_env(), label, TaskProgress(),
                      lockfile=str(HERE / "tasks.lock"), on_exit=self._release_task_busy)
        self.job = job
        try:
            job.start()
        except Exception as e:
            self._release_task_busy()
            self.job = None
            return {"error": "启动失败：%s" % e}

        return {"ok": True, "job": job.id, "label": label, "argv": argv,
                "write": job.write, "started": job.started_at}

    def _release_task_busy(self):
        try:
            self._task_busy.release()
        except RuntimeError:
            pass

    def task_status(self, since=None) -> dict:
        job = self.job
        if job is None:
            out = {"job": "", "state": "idle", "running": False, "items": [],
                   "summary": {}, "accounts": {}, "counts": {}, "log_lines": 0}
            if since is not None:
                out["lines"] = []
            return out
        return job.snapshot(since)

    def task_cancel(self) -> dict:
        if self.job is None or not self.job.running:
            return {"ok": False, "error": "没有正在运行的任务作业"}
        return self.job.cancel()

    def task_capability(self) -> dict:
        """任务中心的能力与前置条件，供界面决定按钮可用性。"""
        script = self.cfg.task_script("task_runner.py")
        school = self.cfg.task_script("school_open_day_2026.py")
        return {
            "enabled": self.cfg.can_tasks,
            "script_dir": str(self.cfg.resolved_script_dir or ""),
            "task_runner": str(script) if script else "",
            "school_script": str(school) if school else "",
            "python": self.cfg.python_exe or "",
            "has_school": school is not None,
            "auth_dir": str(self.cfg.auth_dir) if self.cfg.auth_dir else "",
        }

    # — 账号运维（走网关管理端点，需上游支持）————————————————
    def account_op(self, action: str, uid: str, reason: str = ""):
        # enable 分支不带 body，disable 才带 reason —— 必须显式初始化，
        # 否则 enable 走到 http() 时会 UnboundLocalError（曾因删掉这行踩过）。
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
# 公开面：/cline/* —— 任何人可读的账号池状态 + 贡献账号
# ──────────────────────────────────────────────────────────────────────
#
# 与 /api/cline/*（管理员面）的关系是**两条独立通路，不是同一接口的两种权限**：
# 公开面自己向后端要数据、自己投影字段，管理员面一行都没改。这样公开面能做到
# 只读且只暴露该暴露的，而管理面照旧全量 —— 也不需要给网关加「半权限令牌」。
#
# 两条硬边界（都由代码结构保证，不是靠记得别调错）：
#   1. **只读**：公开面只发 GET 到网关，唯一的写操作是 POST /public/login/*（贡献账号）。
#      账号与模型的启停、清冷却、复测全在 /admin/*，公开面连拼都拼不出来（白名单）。
#   2. **不泄漏**：所有对外字段都经过下面的 _public_* 投影函数**逐个具名挑选**，
#      不是「拿回来再删几个字段」。网关加字段不会自动流到公开面 —— 新字段默认不可见，
#      这是唯一安全的默认方向。投影函数的单测就是这条红线的执行点。

# 公开面允许出现的模型状态文案：只说「能不能用」，不说闸门内部状态名
# （unverified / manual / disable_reason 这些是运营内部口径）。
_PUBLIC_STATE = {
    "free": "active", "exposed": "active",
    "unverified": "checking", "disabled": "off",
}


def _public_model(m: dict) -> dict:
    """公开面的模型条目：只有名字、分组、能不能用、有没有人在接。"""
    return {
        "name": m.get("bare") or m.get("upstream") or "",
        "group": m.get("group") or "",
        "state": _PUBLIC_STATE.get(m.get("state") or "", "unknown"),
        "accounts": m.get("accounts_available") or 0,
    }


def _public_status(st: dict) -> dict:
    """公开面的只读快照。字段是**白名单**，网关的新字段不会自动出现在这里。

    刻意不含：账号邮箱/id（贡献者身份只归管理员）、上游模型名（含分区与线路）、
    闸门台账的技术细节（last_cost / disable_reason / probe_streak）、
    网关地址与 api_key（接入信息只归管理员）。
    """
    models = [_public_model(m) for m in (st.get("models") or []) if isinstance(m, dict)]
    active = [m for m in models if m["state"] == "active"]
    accounts = st.get("accounts") or []
    return {
        "version": st.get("version") or "",
        "uptime_seconds": st.get("uptime_seconds") or 0,
        "accounts_total": st.get("accounts_total") or 0,
        "accounts_available": st.get("accounts_available") or 0,
        "accounts_in_cooldown": sum(
            1 for a in accounts
            if isinstance(a, dict) and (a.get("status") or "active") != "active"),
        "models_total": len(models),
        "models_active": len(active),
        "models": models,
        "catalog_last_sync": (st.get("catalog") or {}).get("last_sync") or "",
        # 目录同步失败对公开面有意义（模型列表可能是旧的），但错误原文是上游细节
        "catalog_stale": bool((st.get("catalog") or {}).get("error")),
    }


class ClinePanel:
    """cline2api 的展示与操作入口。

    与 wb2a 的差别：它的「运营决策存储位」是定价闸门台账，所以面板这边提供的是
    账号池 + 闸门台账（模型启停）两类操作，外加设备授权登录代理。
    鉴权分两套：/v1/* 用 api_key，/status 与 /admin/* 用 admin_token。
    """

    def __init__(self, cfg: dict):
        self.base = cfg["base"].rstrip("/")
        self.api_key = cfg.get("api_key", "")
        self.admin_token = cfg.get("admin_token", "")
        self.config_path = cfg.get("config_path", "")

    def _req(self, method: str, path: str, body=None, token=None, timeout=30):
        headers = {"Authorization": "Bearer " + (token or self.admin_token)}
        return http(method, self.base + path, body, headers, timeout)

    # — 读 —————————————————————————————————————————————
    def status(self):
        code, st = self._req("GET", "/status", timeout=15)
        if code != 200:
            return {"error": "cline2api %d: %s" % (code, extract_error_message(st) or str(st)[:160])}
        if isinstance(st, dict):
            st["gateway"] = self.base
            st["api_key"] = self.api_key
        return st

    def models(self):
        """对外可服务的上游模型名（闸门放行的）。"""
        code, res = self._req("GET", "/v1/models", token=self.api_key, timeout=15)
        if code != 200:
            return {"error": "cline2api %d: %s" % (code, str(res)[:160])}
        return {"data": (res or {}).get("data", [])}

    # — 写 —————————————————————————————————————————————
    def login_start(self):
        code, res = self._req("POST", "/admin/login/start")
        if code != 200:
            return {"error": "发起登录失败：%s" % (extract_error_message(res) or str(res)[:160])}
        return res if isinstance(res, dict) else {"error": str(res)}

    def login_poll(self, device_code: str):
        code, res = self._req("GET", "/admin/login/poll?device_code="
                              + urllib.parse.quote(device_code, safe=""))
        if code != 200:
            return {"state": "error", "error": extract_error_message(res) or str(res)[:160]}
        return res if isinstance(res, dict) else {"state": "error", "error": str(res)}

    def login_cancel(self, device_code: str):
        # cline2api 的 cancel 从 query 读 device_code（不是 body）
        code, res = self._req("POST", "/admin/login/cancel?device_code="
                              + urllib.parse.quote(device_code, safe=""))
        if code != 200:
            return {"error": str(res)[:160]}
        return {"ok": True}

    def model_op(self, enable: bool, model_id: str, reason: str = "") -> dict:
        # 上游名含 / 与 :，必须转义成单段——Go 1.22 ServeMux 的 {id} 只吃一段
        quoted = urllib.parse.quote(model_id, safe="")
        action = "enable" if enable else "disable"
        payload = json.dumps({"reason": reason or "panel"}).encode()
        code, res = self._req("POST", "/admin/models/%s/%s" % (quoted, action), payload)
        if code != 200:
            return {"error": "cline2api %d: %s" % (code, extract_error_message(res) or str(res)[:160])}
        return {"ok": True, "model": model_id, "enabled": enable}

    def account_op(self, enable: bool, account_id: str) -> dict:
        quoted = urllib.parse.quote(account_id, safe="")
        action = "enable" if enable else "disable"
        code, res = self._req("POST", "/admin/accounts/%s/%s" % (quoted, action))
        if code != 200:
            return {"error": "cline2api %d: %s" % (code, extract_error_message(res) or str(res)[:160])}
        return {"ok": True, "account": account_id, "enabled": enable}

    def clear_cooldown(self, account_id: str) -> dict:
        """清掉账号的冷却（账号级 + 全部模型级限额）。"""
        quoted = urllib.parse.quote(account_id, safe="")
        code, res = self._req("POST", "/admin/accounts/%s/clear-cooldown" % quoted)
        if code != 200:
            return {"error": "cline2api %d: %s" % (code, extract_error_message(res) or str(res)[:160])}
        return {"ok": True, "account": account_id, "cleared": True}

    def recheck(self):
        code, res = self._req("POST", "/admin/gate/recheck")
        if code != 200:
            return {"error": "cline2api %d: %s" % (code, str(res)[:160])}
        return {"ok": True, "started": True}

    # — 公开面（未鉴权）————————————————————————————————————
    #
    # 这两个方法**只**服务 /cline/* 公开面：一个是只读投影，一个是贡献账号代理。
    # 它们与管理面共用 _req()，所以网关地址与令牌只在一处 —— 公开面不额外持有凭据。

    def public_status(self) -> dict:
        """公开面的只读快照（已投影，可直接对外）。"""
        code, st = self._req("GET", "/status", timeout=15)
        if code != 200 or not isinstance(st, dict):
            return {"error": "账号池状态暂时读不到（网关 %d）" % code}
        return _public_status(st)

    def public_contribute_start(self) -> tuple[dict, int]:
        """发起一次贡献登录。返回 (响应体, HTTP 状态)。"""
        code, res = self._req("POST", "/public/login/start", timeout=30)
        if code == 404:
            # 网关比面板旧：它还没有公开面。说清楚，别让用户以为是自己的问题。
            return {"error": "本面板所连的网关版本不支持账号贡献，请先升级 cline2api。"}, 501
        if code == 429:
            wait = extract_error_message(res) or "稍后再试"
            return {"error": wait, "retry_after": _retry_after_seconds(wait)}, 429
        if code != 200:
            return {"error": extract_error_message(res) or "发起贡献失败"}, 502
        return (res if isinstance(res, dict) else {"error": "上游返回异常"}), 200

    def public_contribute_poll(self, device_code: str) -> tuple[dict, int]:
        code, res = self._req("GET", "/public/login/poll?device_code="
                              + urllib.parse.quote(device_code, safe=""), timeout=15)
        if code != 200 or not isinstance(res, dict):
            return {"state": "error", "error": "登录状态查询失败"}, 502
        return res, 200

    def public_contribute_cancel(self, device_code: str) -> dict:
        self._req("POST", "/public/login/cancel?device_code="
                  + urllib.parse.quote(device_code, safe=""), timeout=15)
        return {"ok": True}


def _retry_after_seconds(msg: str) -> int:
    """从网关的限流文案里抠出建议等待秒数（抠不到回 60）。

    面板只是把网关那句「N 秒后再试」翻成秒数给前端做倒计时，不自己发明节流规则
    —— 节流是网关的事，面板照抄它的判断。
    """
    m = re.search(r"(\d+)\s*秒", msg or "")
    return int(m.group(1)) if m else 60


# ──────────────────────────────────────────────────────────────────────
# HTTP 服务
# ──────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "wb2a-panel/" + __version__
    panel: Panel = None                      # 由 main 注入
    cline: "ClinePanel" = None               # 由 main 注入；未配置 cline2api 时 None

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
        if path == "/api/tasks/status":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                since = int((q.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            return self._send(200, p.task_status(since))
        if path.startswith("/api/cline/"):
            return self._cline_get(path)
        if path.startswith("/cline/"):
            return self._public_get(path)
        return self._send(404, {"error": "not found"})

    # — 公开面 ————————————————————————————————————————————
    #
    # 路由与 /api/* 完全分开：公开面有自己的前缀，admin 面的路由一条不改。
    # 每个分支都是**字面量匹配**而不是前缀转发 —— 网关里有什么、公开面能摸到什么，
    # 看这几个 if 就数得清，不存在「拼个路径试试」的余地。

    def _public_cline(self):
        """公开面用的 cline 客户端；未配置时统一 503。"""
        c, err = self._cline()
        if c is None:
            return None, err
        return c, None

    def _public_get(self, path: str):
        c, err = self._public_cline()
        if c is None:
            return err
        if path in ("/cline/", "/cline/index.html"):
            page = HERE / "public.html"
            if not page.is_file():
                # 缺件（比如镜像漏拷）时说清楚缺的是哪个文件 —— 默认的
                # FileNotFoundError 会变成 500，看不出该补什么。
                return self._send(500, {"error": "面板缺 public.html（公开页文件），"
                                                 "请重新部署 proxy-panel。"})
            return self._send(200, page.read_text(encoding="utf-8"),
                              "text/html; charset=utf-8")
        if path == "/cline/api/status":
            return self._send(200, c.public_status())
        if path == "/cline/api/login/poll":
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            body, code = c.public_contribute_poll((q.get("device_code") or [""])[0])
            return self._send(code, body)
        return self._send(404, {"error": "not found"})

    def _public_post(self, path: str, body: dict):
        c, err = self._public_cline()
        if c is None:
            return err
        if path == "/cline/api/login/start":
            payload, code = c.public_contribute_start()
            return self._send(code, payload)
        if path == "/cline/api/login/cancel":
            return self._send(200, c.public_contribute_cancel(body.get("device_code") or ""))
        # 公开面没有其它写操作。「停用账号/关停模型」在这里**不存在对应分支**，
        # 不是在别处被拒 —— 请求走到这里就是 404。
        return self._send(404, {"error": "not found"})

    def _cline(self):
        """cline2api 子面板；未配置时统一回一个可读的错误。"""
        if self.cline is None:
            return None, self._send(503, {"error": "面板未配置 cline2api（缺 base/admin_token）。"
                                                   "用 --cline-config 指向它的 config.json，"
                                                   "或设 CLINE2API_CONFIG 环境变量。"})
        return self.cline, None

    def _cline_get(self, path: str):
        c, err = self._cline()
        if c is None:
            return err
        q = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        if path == "/api/cline/status":
            return self._send(200, c.status())
        if path == "/api/cline/models":
            return self._send(200, c.models())
        if path == "/api/cline/login/poll":
            return self._send(200, c.login_poll((q.get("device_code") or [""])[0]))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        p = self.panel
        body = self._body()

        # 公开面在 CSRF 闸门**之前**分流，理由不是省事，而是它压根没有可被 CSRF 的东西：
        # 它的写操作只有「发起一次账号捐赠」，任何人都能发起、也随时能取消，
        # 借他人浏览器提交得到的只是「这个人自己发起了一次捐赠」——攻击者一无所获。
        # 而带上自定义头会在跨站预检时被挡，等于把公开页面上的贡献按钮废掉。
        # 反过来：管理员面（/api/*）的闸门一个字没动，仍要求 X-Panel-Request。
        if path.startswith("/cline/"):
            return self._public_post(path, body)

        # CSRF 闸门：状态变更接口只认带自定义头的请求。
        # 反向代理上的 Basic 认证凭据是浏览器自动附带的，跨站页面也能触发 POST
        # （表单 + sendBeacon 都不需要预检）；而自定义头会强制预检，被 CORS 挡下。
        # 纯 API 调用者补一个 -H 'X-Panel-Request: 1' 即可。
        if self.headers.get("X-Panel-Request") != "1":
            return self._send(403, {"error": "拒绝跨站请求：缺少 X-Panel-Request: 1 头。"
                                             "用脚本调用时请显式加上；浏览器里正常点击不受影响。"})

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
        # — 一键任务 ——————————————————————————————————————
        # 写操作（run/school）会向腾讯发真实上报请求；scan 全程只读。
        if path in ("/api/tasks/scan", "/api/tasks/run"):
            accounts = body.get("accounts") or []
            if isinstance(accounts, str):
                accounts = [a for a in accounts.split(",") if a.strip()]
            only = body.get("only") or []
            if isinstance(only, str):
                only = [a for a in only.split(",") if a.strip()]
            kind = "scan" if path.endswith("scan") else "run"
            return self._send(200, p.task_start(
                kind, accounts=accounts, only_claim=bool(body.get("only_claim")),
                only=only, gap=body.get("gap")))
        if path == "/api/tasks/school":
            mode = (body.get("mode") or "list").strip().lower()
            if mode not in ("list", "run", "lottery"):
                return self._send(400, {"error": "mode 只能是 list / run / lottery"})
            return self._send(200, p.task_start(
                "school", accounts=body.get("accounts") or [], mode=mode))
        if path == "/api/tasks/cancel":
            r = p.task_cancel()
            return self._send(200 if r.get("ok") else 409, r)
        if path.startswith("/api/cline/"):
            c, err = self._cline()
            if c is None:
                return err
            return self._cline_post(path, body)
        return self._send(404, {"error": "not found"})

    def _cline_post(self, path: str, body: dict):
        c = self.cline
        if path == "/api/cline/login/start":
            return self._send(200, c.login_start())
        if path == "/api/cline/login/cancel":
            return self._send(200, c.login_cancel(body.get("device_code") or ""))
        if path == "/api/cline/recheck":
            return self._send(200, c.recheck())
        if path in ("/api/cline/model/enable", "/api/cline/model/disable"):
            mid = (body.get("id") or "").strip()
            if not mid:
                return self._send(400, {"error": "缺少 id"})
            return self._send(200, c.model_op(path.endswith("enable"), mid, body.get("reason") or ""))
        if path in ("/api/cline/account/enable", "/api/cline/account/disable"):
            aid = (body.get("id") or "").strip()
            if not aid:
                return self._send(400, {"error": "缺少 id"})
            return self._send(200, c.account_op(path.endswith("enable"), aid))
        if path == "/api/cline/account/clear-cooldown":
            aid = (body.get("id") or "").strip()
            if not aid:
                return self._send(400, {"error": "缺少 id"})
            return self._send(200, c.clear_cooldown(aid))
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
    ap.add_argument("--script-dir", help="网关任务脚本目录（默认从 bin-dir 推 scripts/）")
    ap.add_argument("--gateway-config", help="网关 config.json 路径（默认自动探测）")
    ap.add_argument("--cline-config",
                    help="cline2api 网关的 config.json 路径（默认 /opt/cline2api/config.json）")
    ap.add_argument("--cline-base", help="cline2api 网关地址（覆盖配置文件）")
    ap.add_argument("--cline-key", help="cline2api 的 api_key（覆盖配置文件）")
    ap.add_argument("--cline-admin-token", help="cline2api 的 admin_token（覆盖配置文件）")
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
    if cfg.can_tasks:
        caps.append("一键任务")

    # flush=True 是必要的：stdout 非 tty（重定向 / Docker 日志）时 Python 会缓冲，
    # 用户看不到启动信息会以为卡死。
    print("✓ 已连接网关 %s（%s 个账号）" % (cfg.gateway, st.get("total", "?")), flush=True)
    if cfg.gateway_config:
        print("  配置来自 %s" % cfg.gateway_config, flush=True)
    print("  增强功能：%s" % ("、".join(caps) if caps else "无（核心功能可用；"
          "把面板部署到网关同机可解锁新增账号/签到/实时积分）"), flush=True)

    # cline2api 是可选第二网关：连不上只提示，不影响 wb2a 面板可用
    if cfg.cline:
        Handler.cline = ClinePanel(cfg.cline)
        cst = Handler.cline.status()
        if isinstance(cst, dict) and cst.get("error"):
            print("⚠ cline2api 子面板不可用（%s）：%s" % (cfg.cline["base"], cst["error"]),
                  file=sys.stderr, flush=True)
        else:
            print("✓ 已连接 cline2api %s（%s 个账号、%s 个模型暴露）" % (
                cfg.cline["base"], cst.get("accounts_total", "?"),
                sum(1 for m in (cst.get("models") or []) if m.get("exposed"))), flush=True)
    else:
        # 别让「没配」和「配了但读不到」都静默走同一条路：
        # 后者是权限问题，只会在页面上表现成整块 cline 区域消失，很难联想到原因。
        print("  第二网关 cline2api：未配置（用 --cline-config 指定其 config.json 可启用）",
              file=sys.stderr, flush=True)

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
