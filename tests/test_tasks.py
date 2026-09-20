"""任务中心（一键完成成长任务）的单元测试。

覆盖三块，都是纯逻辑、不起进程、不碰网络：
  1. 日志解析器 —— 面板的进度完全来自脚本日志，这是最需要锁住的一环；
  2. 命令构造 —— 账号入参、只读与写操作的区分（写错了会误发上报）；
  3. 能力探测与降级 —— 未同机部署时不能崩，也不能假装可用。

    python3 -m unittest tests.test_tasks -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import panel  # noqa: E402


class TestClassifyLine(unittest.TestCase):
    """状态分类：文案取自真实脚本（见 src/scripts/task_runner.py）。"""

    def test_done_variants(self):
        for line in (
            "chat_5: claim 200 ok(credit=+300 energy=+8)",
            "create_canvas: paid -> 0/1 -> 1/1（点亮）",
            "share_invite: completed -> claimed（本轮已入账）",
        ):
            self.assertEqual(panel.classify_line(line), "done", line)

    def test_already_variants(self):
        for line in (
            "first_buddy: query claimed(1/1) -> 已领，跳过",
            "x: query completed(1/1) -> 已完成/已领，跳过",
            "y: claim200 200 already_claimed(credit=+0 energy=+0)",
        ):
            self.assertEqual(panel.classify_line(line), "already", line)

    def test_planned_only_in_dry_run(self):
        """dry-run 的行必须归为「待执行」而不是「已完成」——扫描时不能显示成干完了。"""
        for line in (
            "create_canvas: query paid(0/1) -> 可点亮(canvas)，dry-run 跳过",
            "share_invite: pending -> share-complete 动作（dry-run 不写）",
        ):
            self.assertEqual(panel.classify_line(line), "planned", line)

    def test_running_and_pending(self):
        self.assertEqual(panel.classify_line("chat_5: report 3/5 200 code=0"), "running")
        self.assertEqual(panel.classify_line("skill_1: report 未达 target（0/1），WARN 待下次"),
                         "pending")
        self.assertEqual(panel.classify_line(
            "black_cat: query in_progress(1/3) -> 非夜猫窗口(23-08 CST)，skip pending"), "pending")

    def test_skip_and_error(self):
        self.assertEqual(panel.classify_line(
            "Expert_Philanthropy: query paid(0/1) -> 不可伪造(真实捐款动作(M8))，skip"), "skip")
        self.assertEqual(panel.classify_line("t: claim 失败: boom（可稍后补领）"), "error")
        self.assertEqual(panel.classify_line("t: claim 500 err -> ERR"), "error")

    def test_unknown_line_returns_empty(self):
        """认不出的文案返回空串 —— 界面据此不生成假的进度条目。

        脚本是上游在活跃维护的，文案会变。显示「未知」比显示错误的状态安全。
        """
        self.assertEqual(panel.classify_line("完全没见过的一行文案"), "")
        self.assertEqual(panel.classify_line(""), "")


class TestParseRewards(unittest.TestCase):
    def test_extracts_both(self):
        self.assertEqual(panel.parse_rewards("claim 200 ok(credit=+300 energy=+8)"), (300, 8))

    def test_handles_negative_and_absent(self):
        self.assertEqual(panel.parse_rewards("credit=-5"), (-5, 0))
        self.assertEqual(panel.parse_rewards("没有任何奖励"), (0, 0))

    def test_non_numeric_ignored(self):
        self.assertEqual(panel.parse_rewards("credit=abc energy=+2"), (0, 2))


class TestParseSummary(unittest.TestCase):
    def test_real_summary_line(self):
        s = panel.parse_summary(
            "accounts=3 total=57 ok=20 already=30 skipped=5 pending=1 fail=1 "
            "credit=+1200 energy=+40")
        self.assertEqual(s["accounts"], 3)
        self.assertEqual(s["total"], 57)
        self.assertEqual(s["credit"], 1200)
        self.assertEqual(s["energy"], 40)

    def test_school_summary_shape(self):
        s = panel.parse_summary("accounts=1 ok=8 already=2 skipped=1 pending=0 fail=0")
        self.assertEqual(s["ok"], 8)
        self.assertNotIn("total", s)      # school 脚本没有 total，不该凭空造


class TestTaskProgress(unittest.TestCase):
    """累积器：把日志流变成逐项状态。"""

    def _feed(self, *lines):
        p = panel.TaskProgress()
        for ln in lines:
            p.feed(ln)
        return p.snapshot()

    def test_account_header_gives_nickname(self):
        snap = self._feed("== 00e26541 (测试账号甲) ==",
                          "[task_runner] 00e26541 chat_5: report 1/5 200 code=0")
        self.assertEqual(snap["accounts"]["00e26541"], "测试账号甲")
        self.assertEqual(snap["items"][0]["nickname"], "测试账号甲")

    def test_account_without_nickname(self):
        snap = self._feed("== deadbeef ==",
                          "[task_runner] deadbeef chat_5: report 1/5 200")
        self.assertEqual(snap["items"][0]["uid"], "deadbeef")
        self.assertEqual(snap["items"][0]["nickname"], "")

    def test_later_state_wins(self):
        """同一任务先后报后领奖：最终状态必须是「已完成」。"""
        snap = self._feed(
            "[task_runner] u1 create_canvas: query paid(0/1) -> 可点亮(canvas)，dry-run 跳过",
            "[task_runner] u1 create_canvas: report 1/1 200 code=0",
            "[task_runner] u1 create_canvas: paid -> 0/1 -> 1/1（点亮）",
            "[task_runner] u1 create_canvas: claim 200 ok(credit=+300 energy=+0)",
        )
        it = snap["items"][0]
        self.assertEqual(it["status"], "done")
        self.assertEqual(it["events"], 4)
        self.assertEqual(it["credit"], 300)

    def test_running_does_not_overwrite_terminal_state(self):
        """脚本偶尔在终态后补一行中间态；不能把已完成的又打回进行中。"""
        snap = self._feed(
            "[task_runner] u1 chat_5: claim 200 ok(credit=+300 energy=+0)",
            "[task_runner] u1 chat_5: report 5/5 200 code=0",
        )
        self.assertEqual(snap["items"][0]["status"], "done")

    def test_action_words_are_not_tasks(self):
        """"query energy balance=42" 是账号级事件，不能被当成名为 query 的任务。"""
        snap = self._feed("[task_runner] u1 query energy balance=42")
        codes = [i["code"] for i in snap["items"]]
        self.assertEqual(codes, [""], snap["items"])

    def test_task_codes_recognized(self):
        snap = self._feed(
            "[task_runner] u1 chat_5: report 1/1 200",
            "[task_runner] u1 RichMeow_Chat: report 1/1 200",
            "[task_runner] u1 Expert_team_use_3: report 1/3 200",
        )
        codes = [i["code"] for i in snap["items"]]
        self.assertEqual(codes, ["chat_5", "RichMeow_Chat", "Expert_team_use_3"])

    def test_school_prefix_parsed_like_growth(self):
        snap = self._feed("== abcd1234 (乙) ==",
                          "[school2026] abcd1234 share_invite: completed -> claimed")
        self.assertEqual(snap["items"][0]["code"], "share_invite")
        self.assertEqual(snap["items"][0]["status"], "done")

    def test_global_skip_attributed_to_account(self):
        snap = self._feed("[skip] abcd1234 global realm 不适用 CN 任务")
        self.assertEqual(snap["items"][0]["uid"], "abcd1234")
        self.assertEqual(snap["items"][0]["code"], "")
        self.assertEqual(snap["items"][0]["status"], "skip")

    def test_err_line_kept_as_account_error(self):
        snap = self._feed("ERR: [task_runner] abcd1234 query list_tasks 失败: boom")
        self.assertEqual(snap["items"][0]["status"], "error")
        self.assertIn("boom", snap["items"][0]["message"])

    def test_unrelated_lines_only_in_log(self):
        """无关行只进日志，不产生进度条目（界面不显示假任务）。"""
        snap = self._feed("mode=DRY-RUN accounts=['ALL'] only=all", "一些普通输出")
        self.assertEqual(snap["items"], [])
        self.assertEqual(snap["log_lines"], 2)

    def test_summary_captured(self):
        snap = self._feed("task_runner done: accounts=2 total=18 ok=3 credit=+750 energy=+13")
        self.assertEqual(snap["summary"]["ok"], 3)
        self.assertEqual(snap["summary"]["credit"], 750)

    def test_log_ring_caps_growth(self):
        """日志环形封顶：长作业不能把内存吃光，但要保留近端。"""
        prog = panel.TaskProgress(log_cap=50)
        for i in range(200):
            prog.feed("line %d" % i)
        snap = prog.snapshot()
        self.assertEqual(snap["log_lines"], 200)          # seq 持续递增（增量轮询要用）
        lines = prog.since(0)
        self.assertLessEqual(len(lines), 50)
        self.assertEqual(lines[-1][1], "line 199")        # 尾部保留

    def test_items_cap_stops_table_growth(self):
        prog = panel.TaskProgress(items_cap=3)
        for i in range(10):
            prog.feed("[task_runner] u1 code_%d: report 1/1 200" % i)
        self.assertLessEqual(len(prog.snapshot()["items"]), 3)

    def test_since_returns_increment(self):
        prog = panel.TaskProgress()
        prog.feed("a")
        prog.feed("b")
        first = prog.since(0)
        self.assertEqual([t for _, t in first], ["a", "b"])
        prog.feed("c")
        self.assertEqual([t for _, t in prog.since(first[-1][0])], ["c"])

    def test_parser_failure_does_not_raise(self):
        """解析层任何异常都不能中断作业：错误记进 snapshot.error。"""
        prog = panel.TaskProgress()
        prog.feed(None)
        prog.feed("")
        prog.feed("🙂" * 500)
        self.assertIsInstance(prog.snapshot()["error"], str)


class TestTaskCapability(unittest.TestCase):
    """能力探测：三种缺失各自可辨，且都不崩。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "app"
        (self.app / "scripts").mkdir(parents=True)
        (self.app / "auths").mkdir()

    def _cfg(self, **over):
        kw = {"auth_dir": str(self.app / "auths"), "bin_dir": str(self.app)}
        kw.update(over)
        return panel.Config("http://x", "k", kw.pop("auth_dir"), kw.pop("bin_dir"),
                           8321, **kw)

    def test_enabled_when_script_and_auth_present(self):
        (self.app / "scripts" / "task_runner.py").write_text("# stub")
        cfg = self._cfg()
        self.assertTrue(cfg.can_tasks)
        self.assertEqual(cfg.resolved_script_dir, self.app / "scripts")

    def test_disabled_without_script(self):
        cfg = self._cfg()
        self.assertFalse(cfg.can_tasks)
        self.assertIsNone(cfg.task_script("task_runner.py"))

    def test_disabled_without_auth_dir(self):
        (self.app / "scripts" / "task_runner.py").write_text("# stub")
        cfg = panel.Config("http://x", "k", "", str(self.app), 8321)
        self.assertFalse(cfg.can_tasks)

    def test_no_bin_dir_is_safe(self):
        cfg = panel.Config("http://x", "k", str(self.app / "auths"), "", 8321)
        self.assertFalse(cfg.can_tasks)
        self.assertIsNone(cfg.resolved_script_dir)

    def test_script_dir_also_found_directly_under_bin_dir(self):
        """另一种布局：bin_dir 就是 scripts/（点错目录时也要认出来）。"""
        (self.app / "scripts" / "task_runner.py").write_text("# stub")
        cfg = self._cfg(bin_dir=str(self.app / "scripts"))
        self.assertEqual(cfg.resolved_script_dir, self.app / "scripts")

    def test_explicit_script_dir_wins(self):
        other = Path(self.tmp.name) / "elsewhere"
        other.mkdir()
        (other / "task_runner.py").write_text("# stub")
        cfg = self._cfg(script_dir=str(other))
        self.assertEqual(cfg.resolved_script_dir, other)

    def test_argv_lists_capability_detail(self):
        (self.app / "scripts" / "task_runner.py").write_text("# stub")
        cap = panel.Panel(self._cfg()).task_capability()
        self.assertTrue(cap["enabled"])
        self.assertTrue(cap["task_runner"].endswith("task_runner.py"))
        self.assertFalse(cap["has_school"])       # 没造 school 脚本
        self.assertTrue(cap["python"])


class TestTaskStartGuards(unittest.TestCase):
    """启动前的闸门：不可用时报错要可读，且不能留下「已占用」的假状态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "app"
        (self.app / "scripts").mkdir(parents=True)
        (self.app / "auths").mkdir()

    def _panel(self, with_script=True):
        if with_script:
            (self.app / "scripts" / "task_runner.py").write_text("# stub")
        cfg = panel.Config("http://x", "k", str(self.app / "auths"), str(self.app), 8321)
        return panel.Panel(cfg)

    def test_unavailable_reports_reason(self):
        p = self._panel(with_script=False)
        r = p.task_start("scan")
        self.assertIn("error", r)
        self.assertIn("task_runner.py", r["error"])
        self.assertIsNone(p.job)

    def test_missing_school_script_reported(self):
        p = self._panel()
        r = p.task_start("school")
        self.assertIn("error", r)
        self.assertIn("school_open_day_2026.py", r["error"])

    def test_missing_bin_dir_reports_cwd_problem(self):
        (self.app / "scripts" / "task_runner.py").write_text("# stub")
        cfg = panel.Config("http://x", "k", str(self.app / "auths"), "", 8321)
        r = panel.Panel(cfg).task_start("scan")
        self.assertIn("error", r)


class TestAccountArgs(unittest.TestCase):
    """账号入参必须是完整文件名：uid 前缀会撞号（abc123 命中 abc1234 的凭据）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name)
        (self.app / "scripts").mkdir()
        (self.app / "scripts" / "task_runner.py").write_text("# stub")
        (self.app / "auths").mkdir()
        cfg = panel.Config("http://x", "k", str(self.app / "auths"), str(self.app), 8321)
        self.p = panel.Panel(cfg)

    def test_defaults_to_all(self):
        self.assertEqual(self.p._account_args(None), ["ALL"])
        self.assertEqual(self.p._account_args([]), ["ALL"])

    def test_uid_becomes_filename(self):
        self.assertEqual(self.p._account_args(["abc123"]), ["workbuddy-abc123.json"])

    def test_filename_passed_through(self):
        self.assertEqual(self.p._account_args(["workbuddy-abc.json"]),
                         ["workbuddy-abc.json"])

    def test_all_keyword_preserved(self):
        self.assertEqual(self.p._account_args(["ALL"]), ["ALL"])
        self.assertEqual(self.p._account_args(["all"]), ["ALL"])

    def test_blanks_dropped(self):
        self.assertEqual(self.p._account_args(["", "  ", "abc"]), ["workbuddy-abc.json"])


class TestTaskArgvConstruction(unittest.TestCase):
    """命令构造：扫描不能带 --yes，写操作必须带。这是防误发上报的关键闸门。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "app"
        (self.app / "scripts").mkdir(parents=True)
        (self.app / "auths").mkdir()
        src = ROOT / "tests" / "stub_tasks.py"
        shutil.copy(src, self.app / "scripts" / "task_runner.py")
        shutil.copy(src, self.app / "scripts" / "school_open_day_2026.py")
        self.cfg = panel.Config("http://x", "k", str(self.app / "auths"),
                                str(self.app), 8321)
        self.p = panel.Panel(self.cfg)

    def test_scan_argv_is_read_only(self):
        r = self.p.task_start("scan")
        self.assertTrue(r.get("ok"), r)
        self.assertNotIn("--yes", r["argv"])
        self.assertEqual(r["argv"][-1], "ALL")
        self.assertFalse(r["write"])

    def test_run_argv_has_yes(self):
        r = self.p.task_start("run")
        self.assertIn("--yes", r["argv"])
        self.assertTrue(r["write"])

    def test_only_claim_argv(self):
        r = self.p.task_start("run", only_claim=True)
        self.assertIn("--yes", r["argv"])
        self.assertIn("--only-claim", r["argv"])
        self.assertIn("只领奖", r["label"])

    def test_only_and_gap_passed_through(self):
        r = self.p.task_start("run", only=["chat_5", "RichMeow_Chat"], gap=2.0)
        argv = r["argv"]
        self.assertEqual(argv[argv.index("--only") + 1], "chat_5")
        self.assertIn("--only", argv)
        self.assertEqual(argv[argv.index("--gap") + 1], "2.0")

    def test_gap_below_one_second_ignored(self):
        """脚本自己把 gap 下限卡在 1s（写操作间隔）；面板不该在参数上撒谎。"""
        r = self.p.task_start("run", gap=0.1)
        self.assertNotIn("--gap", r["argv"])

    def test_school_modes(self):
        for mode, flag, write in (("list", "--list", False),
                                  ("run", "--run", True),
                                  ("lottery", "--lottery-only", True)):
            r = self.p.task_start("school", mode=mode)
            self.assertTrue(r.get("ok"), (mode, r))
            self.assertIn(flag, r["argv"], mode)
            self.assertEqual(r["write"], write, mode)
            self._drain()

    def _drain(self):
        """等当前作业结束，让下一轮能力判断/锁释放干净。"""
        for _ in range(400):
            if not (self.p.job and self.p.job.running):
                time.sleep(0.05)      # 让收尾回调（释放互斥）落定
                return
            time.sleep(0.02)


class TestTaskExecution(unittest.TestCase):
    """真起进程跑 stub 脚本：验证解析、状态机、互斥、取消。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / "app"
        (self.app / "scripts").mkdir(parents=True)
        (self.app / "auths").mkdir()
        src = ROOT / "tests" / "stub_tasks.py"
        shutil.copy(src, self.app / "scripts" / "task_runner.py")
        shutil.copy(src, self.app / "scripts" / "school_open_day_2026.py")
        os.environ["STUB_TASK_DELAY"] = "0.02"
        self.addCleanup(os.environ.pop, "STUB_TASK_DELAY", None)
        cfg = panel.Config("http://x", "k", str(self.app / "auths"), str(self.app), 8321)
        self.p = panel.Panel(cfg)

    def _wait(self, timeout=25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.p.job and not self.p.job.running:
                return self.p.task_status()
            time.sleep(0.05)
        self.fail("作业未在 %ss 内结束" % timeout)

    def test_scan_reports_planned_items(self):
        self.p.task_start("scan")
        st = self._wait()
        self.assertEqual(st["state"], "done")
        self.assertEqual(st["exit_code"], 0)
        self.assertFalse(st["write"])
        by_code = {i["code"]: i for i in st["items"]}
        self.assertEqual(by_code["create_canvas"]["status"], "planned")
        self.assertEqual(by_code["black_cat"]["status"], "pending")
        self.assertEqual(by_code["Expert_Philanthropy"]["status"], "skip")
        self.assertEqual(by_code[""]["status"], "")            # 账号级事件
        self.assertEqual(st["summary"]["pending"], 3)

    def test_run_collects_rewards_and_nicknames(self):
        self.p.task_start("run")
        st = self._wait()
        self.assertTrue(st["write"])
        by_code = {i["code"]: i for i in st["items"]}
        self.assertEqual(by_code["chat_5"]["status"], "done")
        self.assertEqual(by_code["chat_5"]["credit"], 300)
        self.assertEqual(by_code["chat_5"]["energy"], 8)
        self.assertEqual(by_code["first_buddy"]["status"], "already")
        self.assertEqual(st["summary"]["credit"], 750)
        self.assertEqual(st["summary"]["energy"], 13)
        self.assertEqual(st["accounts"]["00e26541"], "测试账号甲")
        self.assertEqual(len(st["counts"]), len(set(
            i["status"] for i in st["items"])))

    def test_school_list_read_only(self):
        self.p.task_start("school", mode="list")
        st = self._wait()
        self.assertFalse(st["write"])
        codes = [i["code"] for i in st["items"]]
        self.assertIn("share_invite", codes)
        self.assertIn("task_student_verify", codes)

    def test_school_missing_script_is_guarded(self):
        (self.app / "scripts" / "school_open_day_2026.py").unlink()
        r = self.p.task_start("school", mode="list")
        self.assertIn("error", r)

    def test_second_start_is_rejected_while_running(self):
        """重复点击必须 409 —— 任务有真实上游副作用，不能重入。"""
        os.environ["STUB_TASK_DELAY"] = "0.4"
        first = self.p.task_start("scan")
        self.assertTrue(first.get("ok"))
        second = self.p.task_start("scan")
        self.assertIn("error", second)
        self.assertTrue(second.get("busy"))
        self._wait()

    def test_busy_released_after_finish(self):
        self.p.task_start("scan")
        self._wait()
        second = self.p.task_start("scan")
        self.assertTrue(second.get("ok"), second)
        self._wait()

    def test_cancel_stops_job(self):
        os.environ["STUB_TASK_DELAY"] = "0.5"
        self.p.task_start("run")
        time.sleep(0.3)
        r = self.p.task_cancel()
        self.assertTrue(r.get("ok"), r)
        st = self._wait()
        self.assertEqual(st["state"], "cancelled")
        self.assertFalse(st["running"])

    def test_cancel_without_job(self):
        r = self.p.task_cancel()
        self.assertFalse(r.get("ok"))
        self.assertIn("error", r)

    def test_status_when_idle(self):
        st = self.p.task_status()
        self.assertEqual(st["state"], "idle")
        self.assertFalse(st["running"])
        self.assertEqual(st["items"], [])

    def test_incremental_log_polling(self):
        self.p.task_start("scan")
        st = self._wait()
        total = st["log_lines"]
        self.assertGreater(total, 5)
        # since=total 时应无新增；since=0 时给全量（0 是合法起点，不能当假值）
        self.assertEqual(self.p.task_status(total).get("lines"), [])
        self.assertEqual(len(self.p.task_status(0)["lines"]), total)

    def test_argv_recorded_by_stub_matches(self):
        """stub 把收到的 argv 落盘 —— 验证面板真的按构造的命令调用了脚本。"""
        r = self.p.task_start("scan")
        self._wait()
        recorded = json.loads((self.app / "stub_args.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded, r["argv"][2:])      # 去掉 python 与脚本路径

    def test_nonzero_exit_marks_failed(self):
        """脚本非零退出要如实反映，不能显示成完成。"""
        script = self.app / "scripts" / "task_runner.py"
        script.write_text("#!/usr/bin/env python3\nimport sys\n"
                          "print('task_runner done: accounts=1 total=1 ok=0 fail=1')\n"
                          "sys.exit(2)\n")
        self.p.task_start("run")
        st = self._wait()
        self.assertEqual(st["state"], "failed")
        self.assertEqual(st["exit_code"], 2)
        self.assertEqual(st["summary"]["fail"], 1)


class TestTaskLockfile(unittest.TestCase):
    """flock 互斥：挡住「同机另一个面板实例」的并发作业。"""

    def test_second_acquire_fails(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lock = str(Path(tmp.name) / "tasks.lock")
        fd1 = panel._acquire_lock(lock)
        self.assertIsNotNone(fd1)
        self.assertIsNone(panel._acquire_lock(lock))     # 已被占
        panel._release_lock(fd1)
        fd2 = panel._acquire_lock(lock)
        self.assertIsNotNone(fd2)                       # 释放后可得
        panel._release_lock(fd2)

    def test_job_fails_when_lock_held(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = Path(tmp.name) / "app"
        (app / "scripts").mkdir(parents=True)
        (app / "auths").mkdir()
        shutil.copy(ROOT / "tests" / "stub_tasks.py", app / "scripts" / "task_runner.py")
        cfg = panel.Config("http://x", "k", str(app / "auths"), str(app), 8321)
        p = panel.Panel(cfg)
        # 把作业的锁指到我们已持有的文件上，模拟另一个实例在跑
        lock = str(Path(tmp.name) / "tasks.lock")
        held = panel._acquire_lock(lock)
        self.addCleanup(panel._release_lock, held)
        orig = panel.TaskJob.__init__

        def patched(self, *a, **kw):
            kw["lockfile"] = lock
            orig(self, *a, **kw)

        panel.TaskJob.__init__ = patched
        self.addCleanup(setattr, panel.TaskJob, "__init__", orig)
        p.task_start("scan")
        deadline = time.time() + 10
        while time.time() < deadline and p.job.running:
            time.sleep(0.05)
        self.assertEqual(p.job.state, "failed")
        self.assertIn("tasks.lock", " ".join(t for _, t in p.job.prog.since(0)))


class TestTaskEnv(unittest.TestCase):
    """子进程环境：PYTHONUNBUFFERED 缺了会让面板在整个运行期间看不到任何输出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        app = Path(self.tmp.name) / "app"
        (app / "scripts").mkdir(parents=True)
        (app / "auths").mkdir()
        (app / "scripts" / "task_runner.py").write_text("# stub")
        self.app = app
        self.cfg = panel.Config("http://x", "k", str(app / "auths"), str(app), 8321)

    def test_unbuffered_and_encoding_forced(self):
        env = panel.Panel(self.cfg)._task_env()
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_auths_dir_passed_to_script(self):
        """面板若用 --auth-dir 覆盖过，脚本必须找同一处凭据。"""
        env = panel.Panel(self.cfg)._task_env()
        self.assertEqual(env["WB2A_AUTHS"], str(self.app / "auths"))

    def test_cwd_is_bin_dir(self):
        self.assertEqual(panel.Panel(self.cfg)._task_cwd(), str(self.app))


if __name__ == "__main__":
    unittest.main()
