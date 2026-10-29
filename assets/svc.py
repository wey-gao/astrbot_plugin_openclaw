# -*- coding: utf-8 -*-
r"""小龙虾(OpenClaw) 生命周期管家 —— 让它的命跟 AstrBot 绑在一起。

用法（都在安装目录下跑，纯标准库）：
    python svc.py start --parent-pid <AstrBot的pid>   # 起网关+薄壳，并挂一只看门狗
    python svc.py stop [--reason xxx]                 # 全杀 + 清残存文件
    python svc.py status                              # 看状态(JSON)
    python svc.py check                               # 看装没装齐

设计要点：
1. 网关和薄壳都带 DETACHED_PROCESS 起，AstrBot 重启时不会跟着被系统顺手弄死，
   由看门狗决定什么时候收摊。
2. stop 幂等：pid 文件 → 端口占用 → 命令行特征三重兜底。
3. 判定谁该杀时，只认 OpenClaw 自己的路径特征，并排除自己的整条父进程链，
   免得把"正在敲命令的 shell"一起带走。
4. stop 顺手清 pid/lock/tmp 残存，并轮转超大日志。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import socket
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
BR_DIR = os.path.join(BASE, "bridge")
RUN_DIR = os.path.join(BASE, "run")
LOG_DIR = os.path.join(BASE, "logs")
HOME_DIR = os.path.join(BASE, "home", ".openclaw")
WORK_DIR = os.path.join(BASE, "work")
CFG_PATH = os.path.join(BR_DIR, "config.json")
LOG = os.path.join(LOG_DIR, "lifecycle.log")

PY = sys.executable
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200

STILL_ACTIVE = 259
LOG_ROTATE_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------- 基础设施
def log(*args) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(a) for a in args)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_cfg() -> dict:
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CFG = load_cfg()
NODE_HOME = os.path.join(BASE, CFG.get("node_dir") or "node-v24.22.2-win-x64")
NODE_EXE = os.path.join(NODE_HOME, "node.exe")
NPM_CMD = os.path.join(NODE_HOME, "npm.cmd")
NPM_PREFIX = os.path.join(BASE, "npm-global")
OPENCLAW_MJS = os.path.join(NPM_PREFIX, "node_modules", "openclaw", "openclaw.mjs")


def bridge_port() -> int:
    try:
        return int(CFG.get("port") or 18790)
    except Exception:
        return 18790


def gateway_port() -> int:
    try:
        with open(os.path.join(HOME_DIR, "openclaw.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        return int((cfg.get("gateway") or {}).get("port") or 18789)
    except Exception:
        return 18789


def child_env() -> dict:
    env = os.environ.copy()
    env["PATH"] = NODE_HOME + ";" + NPM_PREFIX + ";" + env.get("PATH", "")
    env["OPENCLAW_HOME"] = os.path.join(BASE, "home")
    return env


def port_open(port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def wait_port(port: int, seconds: float, step: float = 1.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(step)
    return bool(port_open(port))


def port_owner(port: int) -> int:
    """谁在监听这个端口，返回 pid，取不到返回 0。走 netstat，不依赖 psutil。"""
    try:
        raw = subprocess.run(
            ["netstat", "-ano"], capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW
        ).stdout
    except Exception:
        return 0
    text = None
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return 0
    suffix = ":%d" % int(port)
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        if parts[1].endswith(suffix) and parts[3].upper().startswith("LISTEN"):
            try:
                return int(parts[4])
            except Exception:
                pass
    return 0


def pid_alive(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def kill_tree(pid) -> bool:
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except Exception:
        return False


def _pid_file(name: str) -> str:
    return os.path.join(RUN_DIR, name)


def read_pid(name: str) -> int:
    try:
        with open(_pid_file(name), encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except Exception:
        return 0


def write_pid(name: str, pid) -> None:
    try:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(_pid_file(name), "w", encoding="utf-8") as f:
            f.write(str(int(pid)))
    except Exception:
        pass


def drop_pid(name: str) -> None:
    try:
        os.remove(_pid_file(name))
    except Exception:
        pass


def _ps_json(script: str):
    """在 PowerShell 里跑一段丢 UTF-16 编码的脚本，拿 JSON 回来（绕开编码坑）。"""
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            capture_output=True,
            timeout=40,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        return None
    out = p.stdout.decode("utf-8", errors="replace").lstrip("\ufeff").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def ps_snapshot() -> list:
    """抓一次全量进程表：[(pid, ppid, name, cmdline)]。"""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress -Depth 3"
    )
    data = _ps_json(script) or []
    if isinstance(data, dict):
        data = [data]
    return [
        (
            int(x.get("ProcessId") or 0),
            int(x.get("ParentProcessId") or 0),
            str(x.get("Name") or ""),
            str(x.get("CommandLine") or ""),
        )
        for x in data
    ]


def _ancestors(pid: int, snap: list) -> set:
    """自己的整条父进程链——这些是"正在敲命令的人"，绝不能误杀。"""
    ppid_of = {p: pp for p, pp, _n, _c in snap}
    out, cur, guard = set(), int(pid), 0
    while cur and cur not in out and guard < 64:
        out.add(cur)
        cur = ppid_of.get(cur, 0)
        guard += 1
    return out


def _markers() -> tuple:
    return (
        os.path.join(BASE, "bridge") + os.sep,
        os.path.join(NPM_PREFIX, "node_modules", "openclaw"),
        OPENCLAW_MJS,
    )


def openclaw_processes(exclude_self: bool = True) -> list:
    """OpenClaw 自己的进程（gateway、各 worker、薄壳），排除自己和父进程链。"""
    snap = ps_snapshot()
    safe = _ancestors(os.getpid(), snap) if exclude_self else set()
    marks = [m.lower() for m in _markers()]
    out = []
    for pid, _ppid, name, cmd in snap:
        if pid <= 0 or pid in safe:
            continue
        if name.lower() in ("powershell.exe", "pwsh.exe", "conhost.exe", "wsmprovhost.exe"):
            continue
        low = cmd.lower()
        if not low or "svc.py" in low or "lifecycle.py" in low:
            continue
        if any(m in low for m in marks):
            out.append((pid, name, cmd))
    return out


def installed() -> dict:
    """装齐了没有。"""
    info = {
        "node": os.path.isfile(NODE_EXE),
        "openclaw": os.path.isfile(OPENCLAW_MJS),
        "bridge": os.path.isfile(os.path.join(BR_DIR, "bridge.py")),
        "config": os.path.isfile(os.path.join(HOME_DIR, "openclaw.json")),
        "base": BASE,
    }
    info["ready"] = all(info[k] for k in ("node", "openclaw", "bridge", "config"))
    return info


class FileLock:
    """run/svc.lock，防止 start/stop 打架；超过 180s 的当僵尸清掉。"""

    def __init__(self, timeout: float = 180.0, wait: float = 60.0):
        self.timeout = timeout
        self.wait = wait
        self.path = os.path.join(RUN_DIR, "svc.lock")
        self.held = False

    def __enter__(self):
        os.makedirs(RUN_DIR, exist_ok=True)
        deadline = time.time() + self.wait
        while time.time() < deadline:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.timeout:
                        os.remove(self.path)
                        continue
                except Exception:
                    pass
                time.sleep(0.3)
        log("lock timeout, 强行往下走")
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except Exception:
                pass
        return False


# ---------------------------------------------------------------- 启动
def _spawn_gateway():
    os.makedirs(WORK_DIR, exist_ok=True)
    out = open(os.path.join(LOG_DIR, "gateway.log"), "ab")
    try:
        p = subprocess.Popen(
            [NODE_EXE, OPENCLAW_MJS, "gateway", "run"],
            cwd=WORK_DIR,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            close_fds=True,
        )
        log("spawn gateway pid=%s" % p.pid)
        return p.pid
    except Exception as exc:
        log("spawn gateway FAILED", repr(exc))
        return 0


def _spawn_bridge():
    out = open(os.path.join(LOG_DIR, "bridge_stdout.log"), "ab")
    try:
        p = subprocess.Popen(
            [PY, os.path.join(BR_DIR, "bridge.py")],
            cwd=BASE,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            close_fds=True,
        )
        log("spawn bridge pid=%s" % p.pid)
        return p.pid
    except Exception as exc:
        log("spawn bridge FAILED", repr(exc))
        return 0


def _spawn_watchdog(parent_pid: int) -> int:
    try:
        p = subprocess.Popen(
            [PY, os.path.join(BASE, "lifecycle.py"), "--parent-pid", str(int(parent_pid))],
            cwd=BASE,
            env=child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
            close_fds=True,
        )
        write_pid("watchdog.pid", p.pid)
        log("spawn watchdog pid=%s -> parent %s" % (p.pid, parent_pid))
        return p.pid
    except Exception as exc:
        log("spawn watchdog FAILED", repr(exc))
        return 0


def _replace_watchdog(parent_pid: int) -> int:
    old = read_pid("watchdog.pid")
    if old and pid_alive(old):
        kill_tree(old)
        log("旧看门狗 %s 已换掉" % old)
    drop_pid("watchdog.pid")
    return _spawn_watchdog(parent_pid)


def start(parent_pid: int = 0) -> dict:
    os.makedirs(RUN_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    result = {"ok": False, "gateway": False, "bridge": False,
              "gateway_pid": 0, "bridge_pid": 0, "missing": []}
    need = installed()
    if not need["ready"]:
        result["missing"] = [k for k, v in need.items() if v is False and k != "ready"]
        log("没装齐，先装", result["missing"])
        return result

    with FileLock():
        if port_open(gateway_port()):
            result["gateway"] = True
            result["gateway_pid"] = port_owner(gateway_port())
            log("gateway 已在线 pid=%s" % result["gateway_pid"])
        else:
            _spawn_gateway()
            result["gateway"] = wait_port(gateway_port(), 120, 1.5)
            result["gateway_pid"] = port_owner(gateway_port())
            log("gateway %s pid=%s" % ("起来了" if result["gateway"] else "没起来", result["gateway_pid"]))
        if result["gateway_pid"]:
            write_pid("gateway.pid", result["gateway_pid"])

        if port_open(bridge_port()):
            result["bridge"] = True
            result["bridge_pid"] = port_owner(bridge_port())
            log("bridge 已在线 pid=%s" % result["bridge_pid"])
        else:
            _spawn_bridge()
            result["bridge"] = wait_port(bridge_port(), 40, 1.0)
            result["bridge_pid"] = port_owner(bridge_port())
            log("bridge %s pid=%s" % ("起来了" if result["bridge"] else "没起来", result["bridge_pid"]))
        if result["bridge_pid"]:
            write_pid("bridge.pid", result["bridge_pid"])

        if parent_pid:
            _replace_watchdog(parent_pid)

        result["ok"] = bool(result["gateway"] and result["bridge"])
        log("start 完成", json.dumps(result, ensure_ascii=False))
    return result


# ---------------------------------------------------------------- 停止 + 清理
def cleanup_files() -> list:
    removed = []
    import shutil

    def rm(path):
        try:
            os.remove(path)
            removed.append(path)
        except FileNotFoundError:
            pass
        except Exception:
            pass

    for name in ("gateway.pid", "bridge.pid"):
        rm(_pid_file(name))

    for root in (RUN_DIR, HOME_DIR):
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.endswith(".lock") or fn.endswith(".lock.tmp"):
                    rm(os.path.join(dirpath, fn))

    tmp_dir = os.path.join(HOME_DIR, "tmp")
    if os.path.isdir(tmp_dir):
        for name in os.listdir(tmp_dir):
            p = os.path.join(tmp_dir, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
                removed.append(p)
            except Exception:
                pass

    for name in ("gateway.log", "bridge.log", "bridge_stdout.log", "lifecycle.log"):
        p = os.path.join(LOG_DIR, name)
        try:
            if os.path.isfile(p) and os.path.getsize(p) > LOG_ROTATE_BYTES:
                old = p + ".old"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(p, old)
                removed.append(old)
        except Exception:
            pass
    return removed


def stop(reason: str = "manual", clean: bool = True) -> dict:
    os.makedirs(RUN_DIR, exist_ok=True)
    killed, me = [], os.getpid()
    with FileLock():
        bp = read_pid("bridge.pid") or port_owner(bridge_port())
        if bp and pid_alive(bp):
            kill_tree(bp)
            killed.append(("bridge", bp))
        drop_pid("bridge.pid")

        gp = read_pid("gateway.pid") or port_owner(gateway_port())
        if gp and pid_alive(gp):
            kill_tree(gp)
            killed.append(("gateway", gp))
        drop_pid("gateway.pid")

        # 扫尾：孤儿 node / python；看门狗也一起收
        wd = read_pid("watchdog.pid")
        for pid, name, _cmd in openclaw_processes():
            if pid != me and pid_alive(pid):
                kill_tree(pid)
                killed.append((name or "proc", pid))
        if wd and wd != me and pid_alive(wd):
            kill_tree(wd)
            killed.append(("watchdog", wd))
        drop_pid("watchdog.pid")

        for port in (bridge_port(), gateway_port()):
            deadline = time.time() + 10
            while time.time() < deadline and port_open(port, 0.4):
                time.sleep(0.4)

        removed = cleanup_files() if clean else []
        left_ports = [p for p in (bridge_port(), gateway_port()) if port_open(p, 0.4)]
        left_procs = [(pid, name) for pid, name, _c in openclaw_processes() if pid_alive(pid)]

        result = {
            "ok": not left_ports and not left_procs,
            "killed": killed,
            "removed": removed,
            "ports_left": left_ports,
            "procs_left": left_procs,
            "reason": reason,
        }
        log("stop", reason, json.dumps(result, ensure_ascii=False))
    return result


def status() -> dict:
    gw, br = gateway_port(), bridge_port()
    wd = read_pid("watchdog.pid")
    return {
        "installed": installed(),
        "gateway_port": gw,
        "gateway": port_open(gw),
        "gateway_pid": port_owner(gw),
        "bridge_port": br,
        "bridge": port_open(br),
        "bridge_pid": port_owner(br),
        "watchdog_pid": wd,
        "watchdog_alive": pid_alive(wd),
        "procs": [{"pid": p, "name": n} for p, n, _c in openclaw_processes()],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="OpenClaw 生命周期管家")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("start", help="起网关+薄壳，并挂看门狗")
    sp.add_argument("--parent-pid", type=int, default=0, help="要盯的 AstrBot 进程 pid")
    so = sub.add_parser("stop", help="全杀并清理残存")
    so.add_argument("--reason", default="manual")
    so.add_argument("--no-clean", action="store_true", help="只杀进程，不清文件")
    sub.add_parser("status", help="看状态")
    sub.add_parser("check", help="看装没装齐")

    args = ap.parse_args()
    if args.cmd == "start":
        res = start(args.parent_pid)
        code = 0 if res.get("ok") else 1
    elif args.cmd == "stop":
        res = stop(args.reason, clean=not args.no_clean)
        code = 0 if res.get("ok") else 1
    elif args.cmd == "check":
        res = installed()
        code = 0 if res.get("ready") else 1
    else:
        res = status()
        code = 0
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
