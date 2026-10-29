# -*- coding: utf-8 -*-
"""OpenClaw 自动薄壳插件

两条主线：
1. 派活：/claw <任务> 或 LLM 工具 openclaw_task，转给本地薄壳(127.0.0.1:18790)执行。
2. 装/养：/claw_install 一条指令自动装 Node+OpenClaw+薄壳并连上；
         AstrBot 起它就起（prewarm），AstrBot 关它就关（terminate + 看门狗兜底），
         关完顺手清掉 pid/lock/tmp 残存，不给后台留垃圾。

装完这套，别人只要装了本插件、填好模型和 Key，喊一声就能自动装起来。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

try:
    import installer
except Exception as _exc:  # noqa: BLE001
    installer = None
    _INSTALLER_ERR = repr(_exc)

DEFAULT_URL = "http://127.0.0.1:18790"
DEFAULT_SCRIPT_NAME = "start_openclaw.bat"


@register(
    "openclaw_bridge",
    "鲸鱼娘",
    "OpenClaw自动薄壳插件：一条指令自动装好 OpenClaw 并连上，开关跟着 AstrBot 走",
    "1.1.0",
)
class OpenClawBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        cfg = config or {}
        self.enabled = bool(cfg.get("enable", True))
        self.base_url = str(cfg.get("bridge_url") or DEFAULT_URL).strip().rstrip("/")
        self.model = str(cfg.get("default_model") or "").strip()
        self.timeout = max(int(cfg.get("task_timeout", 120) or 120), 10)
        self.max_prompt = max(int(cfg.get("max_prompt_chars", 4000) or 4000), 100)
        self.chunk = max(int(cfg.get("reply_chunk", 1200) or 1200), 200)
        self.auto_start = bool(cfg.get("auto_start", True))
        self.start_script = str(cfg.get("start_script") or "").strip()
        self.owner_only = bool(cfg.get("owner_only", True))
        owners = cfg.get("owner_ids") or []
        if isinstance(owners, (str, int)):
            owners = [owners]
        self.owner_ids = {str(o).strip() for o in owners if str(o).strip()}
        self.allow_group = bool(cfg.get("allow_group", False))
        self.tool_enable = bool(cfg.get("llm_tool_enable", True))

        # —— 安装 / 生命周期 ——
        raw_dir = str(cfg.get("install_dir") or "").strip()
        if installer is not None:
            self.base = os.path.abspath(os.path.expanduser(raw_dir)) if raw_dir else installer.default_base()
        else:
            self.base = raw_dir or os.path.join(os.path.expanduser("~"), "OpenClaw")
        self.node_version = str(cfg.get("node_version") or "v24.22.2").strip()
        self.node_mirror = str(cfg.get("node_mirror") or "").strip()
        self.registry = str(cfg.get("npm_registry") or "").strip()
        self.provider_base_url = str(cfg.get("provider_base_url") or "").strip()
        self.provider_api_key = str(cfg.get("provider_api_key") or "").strip()
        self.provider_model = str(cfg.get("provider_model") or self.model or "").strip()
        self.gateway_port = int(cfg.get("gateway_port", 18789) or 18789)
        self.bridge_port = int(cfg.get("bridge_port", 18790) or 18790)
        self.prewarm = bool(cfg.get("prewarm", True))
        self.stop_on_exit = bool(cfg.get("stop_on_exit", True))
        self.auto_install = bool(cfg.get("auto_install", False))

        self._starting = False
        self._installing = False
        self._loop: asyncio.AbstractEventLoop | None = None

    # ================= 生命周期：AstrBot 开它开 =================
    async def initialize(self) -> None:
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        if installer is None:
            logger.error(f"[openclaw] 安装器没导入成功：{getattr(self, '_INSTALLER_ERR', '?')}")
            return
        try:
            info = installer.installed(self.base)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[openclaw] 检查安装状态失败：{exc}")
            return
        if not info.get("ready"):
            if self.auto_install:
                logger.info("[openclaw] 没装齐，开工自动安装")
                threading.Thread(target=self._install_worker, args=(None,), daemon=True).start()
            else:
                logger.info(f"[openclaw] 还没装齐（{self.base}），发 /claw_install 我来装")
            return
        if self.prewarm:
            logger.info("[openclaw] 开机预热：拉起网关+薄壳，并挂上跟命看门狗")
            threading.Thread(target=self._prewarm_worker, daemon=True).start()

    def _prewarm_worker(self) -> None:
        try:
            res = installer.svc(
                self.base,
                "start",
                ["--parent-pid", str(os.getpid())],
                timeout=300,
            )
            logger.info(f"[openclaw] 预热结果：{json.dumps(res, ensure_ascii=False)[:400]}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[openclaw] 预热失败：{exc}")

    async def terminate(self) -> None:
        """AstrBot 关闭/重载插件时，把小龙虾整套收掉并清残存。"""
        if installer is None or not self.stop_on_exit:
            return
        try:
            if not os.path.isfile(installer.paths(self.base)["svc"]):
                return
            res = await asyncio.to_thread(
                installer.svc, self.base, "stop", ["--reason", "astrbot-exit"], 180
            )
            logger.info(f"[openclaw] 收摊：{json.dumps(res, ensure_ascii=False)[:500]}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[openclaw] 收摊失败：{exc}")

    # ================= 底层请求 =================
    def _request(self, path: str, payload: dict | None = None, timeout: float = 5.0) -> dict:
        url = self.base_url + path
        try:
            if payload is None:
                raw = urllib.request.urlopen(url, timeout=timeout).read()
            else:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                raw = urllib.request.urlopen(req, timeout=timeout).read()
            return json.loads(raw.decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8", errors="replace"))
            except Exception:
                return {"ok": False, "error": f"HTTP {exc.code}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def _health(self) -> dict | None:
        res = await asyncio.to_thread(self._request, "/health", None, 4.0)
        return res if res.get("ok") else None

    async def _ensure_bridge(self) -> bool:
        if await self._health():
            return True
        if not self.auto_start or self._starting:
            return False
        self._starting = True
        try:
            logger.info("[openclaw] 薄壳没响应，交给 svc.py 拉起来")
            if installer is not None and os.path.isfile(installer.paths(self.base)["svc"]):
                res = await asyncio.to_thread(
                    installer.svc,
                    self.base,
                    "start",
                    ["--parent-pid", str(os.getpid())],
                    300,
                )
                if res.get("ok"):
                    logger.info("[openclaw] 薄壳已就绪")
                    return True
            # 兜底：老路子，跑 bat
            script = self.start_script or os.path.join(self.base, DEFAULT_SCRIPT_NAME)
            if os.path.isfile(script):
                subprocess.Popen(
                    ["cmd", "/c", script],
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for _ in range(20):
                    await asyncio.sleep(1.5)
                    if await self._health():
                        return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[openclaw] 拉起失败：{exc}")
        finally:
            self._starting = False
        return False

    async def _ask(self, prompt: str) -> dict:
        if not self.enabled:
            return {"ok": False, "error": "插件总开关是关的"}
        prompt = (prompt or "").strip()
        if not prompt:
            return {"ok": False, "error": "任务内容为空"}
        if len(prompt) > self.max_prompt:
            return {"ok": False, "error": f"任务太长（{len(prompt)} 字 > {self.max_prompt}）"}
        if not await self._ensure_bridge():
            return {"ok": False, "error": f"薄壳没起来，也没自动拉起成功，看 {self.base}\\logs"}
        payload: dict = {"prompt": prompt, "timeout": self.timeout}
        if self.model:
            payload["model"] = self.model
        res = await asyncio.to_thread(self._request, "/task", payload, self.timeout + 45)
        return res or {"ok": False, "error": "无返回"}

    # ================= 权限 =================
    def _allowed(self, event: AstrMessageEvent) -> bool:
        # owner_ids 留空 = 没配白名单，就不按人拦，交给 allow_group 管
        if self.owner_only and self.owner_ids and str(event.get_sender_id()) not in self.owner_ids:
            return False
        if not self.allow_group and not event.is_private_chat():
            return False
        return True

    # ================= 安装 =================
    def _install_opts(self) -> dict:
        return {
            "install_dir": self.base,
            "node_version": self.node_version,
            "node_mirror": self.node_mirror,
            "registry": self.registry,
            "model": self.provider_model or self.model,
            "base_url": self.provider_base_url,
            "api_key": self.provider_api_key,
            "gateway_port": self.gateway_port,
            "bridge_port": self.bridge_port,
        }

    def _install_log(self, msg: str) -> None:
        try:
            os.makedirs(os.path.join(self.base, "logs"), exist_ok=True)
            with open(os.path.join(self.base, "logs", "install.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + str(msg) + "\n")
        except Exception:
            pass
        logger.info(f"[openclaw/install] {msg}")

    def _install_worker(self, umo: str | None) -> None:
        if installer is None:
            return
        self._installing = True
        try:
            res = installer.install(self.base, self._install_opts(), log=self._install_log)
        except Exception as exc:  # noqa: BLE001
            res = {"ok": False, "error": repr(exc)}
        finally:
            self._installing = False
        if umo:
            ok = res.get("ok")
            if ok:
                text = (
                    f"小龙虾装好了，网关+薄壳都在跑（目录 {self.base}）。\n"
                    f"现在发 /claw 要它干活就行。用时 {res.get('elapsed')}s。"
                )
            else:
                text = f"没装成：{res.get('error') or '看日志'}\n日志：{self.base}\\logs\\install.log"
            self._push(umo, text)

    def _push(self, umo: str, text: str) -> None:
        """从线程里往会话推一条消息。"""
        if not self._loop:
            return
        async def _send():
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
            except Exception as exc:  # noqa: BLE001
                logger.error(f"[openclaw] 推送结果失败：{exc}")
        try:
            asyncio.run_coroutine_threadsafe(_send(), self._loop)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[openclaw] 推送调度失败：{exc}")

    # ================= 指令入口 =================
    @filter.command("claw", alias={"小龙虾"})
    async def claw_cmd(self, event: AstrMessageEvent) -> None:
        """把任务交给小龙虾执行。用法：/claw 帮我查一下今天的新闻标题"""
        if not self._allowed(event):
            yield event.plain_result("这活儿只认主人私聊，别人喊不动我。")
            return
        text = re.sub(r"^\s*\S+\s*", "", event.get_message_str() or "", count=1).strip()
        if not text:
            yield event.plain_result("用法：/claw 要它干的活")
            return
        yield event.plain_result("丢给小龙虾了，等它干完……")
        res = await self._ask(text)
        for piece in self._format(res):
            yield event.plain_result(piece)

    @filter.command("claw_install", alias={"安装小龙虾"})
    async def claw_install(self, event: AstrMessageEvent) -> None:
        """一条指令自动装 OpenClaw 整套（Node + 本体 + 薄壳）并连上。"""
        if not self._allowed(event):
            yield event.plain_result("安装这种事只认主人。")
            return
        if installer is None:
            yield event.plain_result("安装器没加载起来，看看插件目录里 installer.py 在不在。")
            return
        if self._installing:
            yield event.plain_result("正在装呢，装完我喊你。")
            return
        if installer.installed(self.base).get("ready"):
            h = await self._health()
            if h:
                yield event.plain_result(f"已经整装待发（{self.base}），不用再装。要重装就说一声。")
                return
        umo = getattr(event, "unified_msg_origin", None) or ""
        if self.provider_model and not self.provider_api_key and not os.path.isfile(
            installer.paths(self.base)["oc_config"]
        ):
            yield event.plain_result(
                "先把模型和 Key 填上（插件配置里 provider_base_url / provider_api_key / provider_model），"
                "不然装完连不上模型。填好再喊我装。"
            )
            return
        threading.Thread(target=self._install_worker, args=(umo,), daemon=True).start()
        yield event.plain_result(
            f"开始装了：目录 {self.base}\n顺序是 铺脚本 → 下 Node → npm 装 openclaw → 写配置 → 起服务，"
            "几分钟，装完我回你。"
        )

    @filter.command("claw_start", alias={"开小龙虾"})
    async def claw_start(self, event: AstrMessageEvent) -> None:
        """手动把网关+薄壳拉起来。"""
        if not self._allowed(event):
            yield event.plain_result("只认主人。")
            return
        if installer is None:
            yield event.plain_result("安装器没加载。")
            return
        res = await asyncio.to_thread(
            installer.svc, self.base, "start", ["--parent-pid", str(os.getpid())], 300
        )
        if res.get("ok"):
            yield event.plain_result(
                f"起来了：网关 {res.get('gateway_pid')}，薄壳 {res.get('bridge_pid')}。"
            )
        else:
            yield event.plain_result(f"没起来：{json.dumps(res, ensure_ascii=False)[:300]}")

    @filter.command("claw_stop", alias={"关小龙虾"})
    async def claw_stop(self, event: AstrMessageEvent) -> None:
        """全停并清残存（含看门狗）。"""
        if not self._allowed(event):
            yield event.plain_result("只认主人。")
            return
        if installer is None:
            yield event.plain_result("安装器没加载。")
            return
        res = await asyncio.to_thread(
            installer.svc, self.base, "stop", ["--reason", "owner-stop"], 180
        )
        if res.get("ok"):
            yield event.plain_result(
                f"关干净了，清了 {len(res.get('removed') or [])} 个残存文件，端口也放了。"
            )
        else:
            yield event.plain_result(f"没关净：{json.dumps(res, ensure_ascii=False)[:300]}")

    @filter.command("claw_status", alias={"小龙虾状态"})
    async def claw_status(self, event: AstrMessageEvent) -> None:
        """看看装没装、网关/薄壳/看门狗是不是活着。"""
        if not self._allowed(event):
            yield event.plain_result("这活儿只认主人私聊。")
            return
        h = await self._health()
        info = installer.installed(self.base) if installer else {}
        st = await asyncio.to_thread(installer.svc, self.base, "status", [], 60) if installer else {}
        wd = "在" if st.get("watchdog_alive") else "不在"
        head = (
            f"目录 {self.base}｜装齐：{info.get('ready')}\n"
            f"薄壳：{'在线' if h else '没响应'}｜忙：{h.get('busy') if h else '-'}｜"
            f"本小时 {h.get('calls_last_hour') if h else '-'} 单｜看门狗：{wd}"
        )
        yield event.plain_result(head)
        if h:
            yield event.plain_result(f"模型 {self.model or h.get('model') or '-'}")

    # ================= 工具入口 =================
    @filter.llm_tool(name="openclaw_task")
    async def openclaw_task(self, event: AstrMessageEvent, prompt: str) -> str:
        """把一件具体活儿交给本机的 OpenClaw(小龙虾) 执行，适合查资料、整理文件、跑脚本这类需要动手的活。

        Args:
            prompt(string): 要它执行的任务，写清楚目标、位置和交付物
        """
        if not self.tool_enable:
            return "工具已关闭"
        if not self._allowed(event):
            return "这个工具只对主人开放"
        res = await self._ask(prompt)
        if not res.get("ok"):
            return f"派活失败：{res.get('error', '未知错误')}"
        return str(res.get("reply") or "")[: self.max_prompt]

    @filter.llm_tool(name="openclaw_setup")
    async def openclaw_setup(self, event: AstrMessageEvent, action: str) -> str:
        """管理本机 OpenClaw(小龙虾) 的安装与运行状态：装整套、开、关、看状态。

        Args:
            action(string): install（自动安装整套并启动）/ start（启动）/ stop（全部停止并清理残存）/ status（查看状态）
        """
        if not self.tool_enable:
            return "工具已关闭"
        if not self._allowed(event):
            return "这个工具只对主人开放"
        if installer is None:
            return "安装器没加载成功，检查插件目录 installer.py"
        act = (action or "").strip().lower()
        if act in ("install", "setup", "安装"):
            if self._installing:
                return "已经在装了，别急"
            umo = getattr(event, "unified_msg_origin", None) or ""
            if umo:
                threading.Thread(target=self._install_worker, args=(umo,), daemon=True).start()
                return "开始装了，装完我主动告诉你。"
            res = await asyncio.to_thread(
                installer.install, self.base, self._install_opts(), self._install_log
            )
            return ("装好了" if res.get("ok") else f"没装成：{res.get('error')}")
        if act in ("start", "开"):
            res = await asyncio.to_thread(
                installer.svc, self.base, "start", ["--parent-pid", str(os.getpid())], 300
            )
            return f"启动结果：{json.dumps(res, ensure_ascii=False)[:300]}"
        if act in ("stop", "关"):
            res = await asyncio.to_thread(installer.svc, self.base, "stop", ["--reason", "tool"], 180)
            return f"停止结果：{json.dumps(res, ensure_ascii=False)[:300]}"
        res = await asyncio.to_thread(installer.svc, self.base, "status", [], 60)
        return f"状态：{json.dumps(res, ensure_ascii=False)[:600]}"

    # ================= 收尾 =================
    def _format(self, res: dict) -> list[str]:
        if not res.get("ok"):
            return [f"没干成：{res.get('error', '未知错误')}"]
        reply = str(res.get("reply") or "").strip() or "（它啥也没说）"
        elapsed = res.get("elapsed")
        head = f"小龙虾回话（{elapsed}s）：\n" if elapsed is not None else ""
        out, buf = [], ""
        for line in reply.splitlines(keepends=True):
            if len(buf) + len(line) > self.chunk:
                out.append(buf)
                buf = ""
            buf += line
        if buf:
            out.append(buf)
        if head and out:
            out[0] = head + out[0]
        return out or ["（空）"]
