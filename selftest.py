# -*- coding: utf-8 -*-
"""自测：模拟 AstrBot 插件生命周期，验 开=起 / 关=停+清残存 / 看门狗兜底。

用法（在装了 AstrBot 的 python 里跑）：
    python selftest.py            # 默认 installer.default_base()
    python selftest.py D:\\xxx     # 指定安装目录

流程：stop → initialize()（等同 AstrBot 加载插件）→ 探健康 → terminate()（等同 AstrBot 关闭）
      → 复核进程/端口/残存文件 → 再 start 回生产状态。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

import installer  # noqa: E402


class _Ctx:
    """够用的 context 替身。"""

    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append(str(chain))
        return True


def _procs(base: str) -> list:
    """有没有还挂着的 openclaw / bridge / lifecycle 进程。"""
    out = []
    try:
        import subprocess

        ps = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='node.exe' OR Name='python.exe'\" | "
                "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            timeout=40,
            creationflags=0x08000000,
        )
        data = json.loads((ps.stdout or b"{}").decode("utf-8", "replace") or "{}")
        if isinstance(data, dict):
            data = [data]
        for row in data or []:
            cmd = str(row.get("CommandLine") or "")
            if base.lower() in cmd.lower():
                out.append((row.get("ProcessId"), cmd[:80]))
    except Exception as exc:  # noqa: BLE001
        out.append(("?", repr(exc)))
    return out


def _ports() -> dict:
    import socket

    res = {}
    for port in (18789, 18790):
        s = socket.socket()
        s.settimeout(0.6)
        try:
            res[port] = s.connect_ex(("127.0.0.1", port)) == 0
        finally:
            s.close()
    return res


async def main() -> int:
    from main import OpenClawBridge  # 插件本体

    base = sys.argv[1] if len(sys.argv) > 1 else installer.default_base()
    ctx = _Ctx()
    cfg = {"install_dir": base, "prewarm": True, "stop_on_exit": True}
    plug = OpenClawBridge(ctx, cfg)
    ok_all = True

    print("== 0. 先全停，假装 AstrBot 没启动 ==")
    installer.svc(base, "stop", ["--reason", "selftest-pre"], 180)
    print("   进程:", _procs(base) or "无", "端口:", _ports())

    print("== 1. 调 initialize()，等同 AstrBot 加载插件 ==")
    t0 = time.time()
    await plug.initialize()          # 内部起线程跑 svc.py start --parent-pid 本进程
    health = None
    for _ in range(60):
        await asyncio.sleep(1)
        health = installer.health(base)
        if health:
            break
    print("   health:", health, "用时 %.1fs" % (time.time() - t0))
    ok = bool(health and health.get("ok"))
    ok_all &= ok
    print("   [%s] AstrBot 开 → 小龙虾起" % ("OK" if ok else "失败"))
    print("   进程:", [p[0] for p in _procs(base)])

    print("== 2. 调 terminate()，等同 AstrBot 关闭 ==")
    await plug.terminate()
    await asyncio.sleep(2)
    left = _procs(base)
    ports = _ports()
    run_files = []
    for root, _dirs, files in os.walk(os.path.join(base, "run")):
        run_files += [os.path.join(root, f) for f in files]
    ok2 = not left and not any(ports.values()) and not run_files
    ok_all &= ok2
    print("   进程:", left or "无")
    print("   端口:", ports, "run 残存:", run_files or "无")
    print("   [%s] AstrBot 关 → 全停 + 清残存" % ("OK" if ok2 else "失败"))

    print("== 3. 看门狗兜底（父进程消失后自清）==")
    print("   说明：本测试进程退出即等效于 AstrBot 崩溃，看门狗应在 5s 内收摊。")
    print("   本步交由第 4 步之后的巡检确认（run/watchdog.log 里应出现 parent-gone）。")

    print("== 4. 拉回生产状态（父进程=真实 AstrBot）==")
    import installer as _ins

    parent = os.environ.get("ASTRBOT_PID") or ""
    extra = ["--parent-pid", parent] if parent.isdigit() else []
    if not extra:
        print("   没给 ASTRBOT_PID，先只起服务不挂看门狗")
    res = _ins.svc(base, "start", extra, 300)
    print("   ", json.dumps(res, ensure_ascii=False)[:300])
    print("   health:", _ins.health(base))

    print("\n结论：%s" % ("全部通过" if ok_all else "有失败项，看上面"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
