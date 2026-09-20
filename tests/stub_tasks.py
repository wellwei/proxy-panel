#!/usr/bin/env python3
"""假任务脚本 —— 用于测试任务中心（tests/ 专用，不进生产）。

按真实 task_runner.py / school_open_day_2026.py 的**日志文案与 CLI 形态**输出，
但完全不发网络请求、不需要凭据。这样测试锁的是「面板的解析与编排」这一侧，
不依赖真实上游，也不会误发上报。

形态按**调用它的文件名**分派（与真实布局一致：面板调 task_runner.py 或
school_open_day_2026.py）：
    task_runner.py           [ALL] [--yes] [--only-claim] [--only CODE] [--gap N]
    school_open_day_2026.py  [ALL] [--list|--run|--lottery-only] [--yes]

把本文件拷成上述任一名字即可；测试里就是这么做的。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ARGS_LOG_NAME = "stub_args.json"


def _record(argv):
    """把入参记到「调用者所在目录」，供测试断言命令构造是否正确。

    真实脚本没有这个副作用；测试需要看 argv 时不靠猜，直接读这个文件。
    """
    try:
        (Path(os.getcwd()) / ARGS_LOG_NAME).write_text(
            json.dumps(argv, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _delay():
    """模拟真实运行的耗时，让面板轮询有机会看到 running。CI 上可用环境变量压到 0。"""
    try:
        return float(os.environ.get("STUB_TASK_DELAY", "0.05"))
    except ValueError:
        return 0.05


def run_growth(argv) -> int:
    yes = "--yes" in argv
    only_claim = "--only-claim" in argv
    only = [argv[i + 1] for i, a in enumerate(argv) if a == "--only" and i + 1 < len(argv)]
    accounts = [a for a in argv if not a.startswith("--")
                and a not in ("--yes", "--only-claim")
                and (a.endswith(".json") or a == "ALL")]
    d = _delay()

    mode = "only-claim" if only_claim else ("REAL" if yes else "DRY-RUN")
    print("mode=%s accounts=%s only=%s only_claim=%s gap=1.0"
          % (mode, accounts, only or "all", only_claim), flush=True)

    uid = "00e26541"
    print("== %s (测试账号甲) ==" % uid, flush=True)
    print("[task_runner] %s query energy balance=42" % uid, flush=True)

    steps = [
        ("create_canvas",
         "query paid(0/1) -> 可点亮(canvas)，dry-run 跳过" if not yes else "report 1/1 200 code=0 id=wb-1"),
        ("chat_5",
         "query in_progress(2/5) -> 可点亮 need=3 src=无 ids=[], dry-run 跳过" if not yes
         else "report 3/5 200 code=0"),
        ("RichMeow_Chat",
         "query paid(0/1) -> 可点亮(richmeow)，dry-run 跳过" if not yes
         else "report 1/1 200 code=0 （桌面指纹对话链）"),
        ("black_cat", "query in_progress(1/3) -> 非夜猫窗口(23-08 CST)，skip pending"),
        ("Expert_Philanthropy", "query paid(0/1) -> 不可伪造(真实捐款动作(M8))，skip"),
    ]
    for code, msg in steps:
        print("[task_runner] %s %s: %s" % (uid, code, msg), flush=True)
        if d:
            time.sleep(d)

    if yes:
        print("[task_runner] %s chat_5: claim 200 ok(credit=+300 energy=+8)" % uid, flush=True)
        print("[task_runner] %s first_buddy: query claimed(1/1) -> 已领，跳过" % uid, flush=True)
        if only_claim:
            print("[task_runner] %s expert_5: query completed(5/5) -> 可领(claim)，only_claim 跳过"
                  % uid, flush=True)
        uid2 = "00e2abcd"
        print("== %s (测试账号乙) ==" % uid2, flush=True)
        print("[task_runner] %s skill_1: report 未达 target（0/1），WARN 待下次" % uid2, flush=True)
        print("[skip] %s global realm 不适用 CN 任务" % uid2, flush=True)

    print("task_runner done: accounts=%d total=18 ok=%d already=%d skipped=%d "
          "pending=%d fail=0 credit=+%d energy=+%d"
          % (2 if yes else 1, 2 if yes else 0, 1 if yes else 0, 1, 1 if yes else 3,
             750 if yes else 0, 13 if yes else 0), flush=True)
    return 0


def run_school(argv) -> int:
    yes = "--yes" in argv and ("--run" in argv or "--lottery-only" in argv)
    d = _delay()
    if "--lottery-only" in argv:
        mode = "LOTTERY"
    elif "--run" in argv:
        mode = "RUN"
    else:
        mode = "LIST"
    print("mode=%s dry-run 见下" % mode, flush=True)

    uid = "00e26541"
    print("== %s (测试账号甲) ==" % uid, flush=True)
    print("[school2026] %s school/tasks in_period=True tasks=4" % uid, flush=True)
    items = [
        ("share_invite", "completed -> claimed" if yes else "pending -> share-complete 动作（dry-run 不写）"),
        ("chat_3_times", "1/3 -> 可点亮 need=2，dry-run 跳过" if not yes else "3/3 -> claimed"),
        ("task_student_verify", "pending -> 人工环节（学生认证），跳过"),
    ]
    for code, msg in items:
        print("[school2026] %s %s: %s" % (uid, code, msg), flush=True)
        if d:
            time.sleep(d)
    if mode == "LOTTERY" and yes:
        print("[school2026] %s lottery balance=2（将 POST /wheel/draw 抽 2 次）" % uid, flush=True)
        print("[school2026] %s draw -> 50 积分（balance 1）" % uid, flush=True)
    print("[school2026] 说明：非人工任务（share/chat/desktop/expert）自动点亮并领奖；"
          "task_student_verify 学生认证为人工环节，--run 跳过。", flush=True)
    print("school2026 done: accounts=1 ok=%d already=%d skipped=%d pending=%d fail=0"
          % (1 if yes else 0, 1, 1, 0 if yes else 1), flush=True)
    return 0


def main() -> int:
    _record(sys.argv[1:])
    name = Path(sys.argv[0]).name
    if name.startswith("school"):
        return run_school(sys.argv[1:])
    return run_growth(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
