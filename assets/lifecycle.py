# -*- coding: utf-8 -*-
r"""看门狗：只干一件事 —— 盯着 AstrBot 的进程，它一没，小龙虾全套收摊。

AstrBot 正常关闭时会先调插件的 terminate() 自己收摊，这只是兜底：
崩溃、任务管理器结束、断电前强杀，这些情况插件没机会执行，就靠它。

用法：python lifecycle.py --parent-pid <AstrBot pid>
"""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.join(BASE, "svc.py")
LOG_DIR = os.path.join(BASE, "logs")
RUN_DIR = os.path.join(BASE, "run")
LOG = os.path.join(LOG_DIR, "lifecycle.log")
CREATE_NO_WINDOW = 0x08000000
SYNCHRONIZE = 0x00100000


def log(*args) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(a) for a in args)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def wait_for_exit(pid: int) -> bool:
    """阻塞等到这个 pid 结束。返回 False 表示一上来就找不到它了。"""
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        k32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
    except Exception:
        pass
    finally:
        try:
            k32.CloseHandle(handle)
        except Exception:
            pass
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent-pid", type=int, required=True)
    args = ap.parse_args()

    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(os.path.join(RUN_DIR, "watchdog.pid"), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    log("看门狗上岗 pid=%s 盯着 parent=%s" % (os.getpid(), args.parent_pid))
    seen = wait_for_exit(args.parent_pid)
    log("parent %s %s，开始收摊" % (args.parent_pid, "已退出" if seen else "一开始就不在"))

    # 给它 1.5 秒把文件句柄吐干净，再动手
    time.sleep(1.5)
    try:
        subprocess.run(
            [sys.executable, SVC, "stop", "--reason", "astrbot-exit"],
            cwd=BASE,
            timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as exc:
        log("收摊出岔子", repr(exc))
    log("看门狗下班")
    return 0


if __name__ == "__main__":
    sys.exit(main())
