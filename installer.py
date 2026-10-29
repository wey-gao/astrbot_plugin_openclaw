# -*- coding: utf-8 -*-
"""OpenClaw 自动安装器 —— 别人只装这个插件，也能一条指令把小龙虾整套支棱起来。

干的事：
1. 建目录、把自带的桥/管家脚本铺进去（svc.py、lifecycle.py、bridge/*）
2. 没有便携 Node 就按镜像下载解压（不污染系统 PATH）
3. 没有 openclaw 就 npm 装到本地 prefix（带 --allow-scripts，不然 postinstall 被拦）
4. 生成 home/.openclaw/openclaw.json（网关 local 模式 + 模型/Key 走插件配置）
5. 交给 svc.py 起网关和薄壳

纯标准库，Windows 专用。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(PLUGIN_DIR, "assets")
CREATE_NO_WINDOW = 0x08000000

DEFAULT_NODE_VERSION = "v24.22.2"
OFFICIAL_NODE = "https://nodejs.org/dist"
MIRROR_NODE = "https://npmmirror.com/mirrors/node"
DEFAULT_REGISTRY = "https://registry.npmmirror.com"
ALLOW_SCRIPTS = "@google/genai,esbuild,koffi,protobufjs,openclaw"


# ------------------------------------------------------------------ 小工具
def _is_system_drive(path: str) -> bool:
    """判断某个路径是不是落在系统盘（Windows 上就是 C:）"""
    if os.name != "nt":
        return False
    sysdrive = (os.environ.get("SystemDrive") or "C:").rstrip("\\").upper() + "\\"
    try:
        return os.path.abspath(path).upper().startswith(sysdrive)
    except Exception:
        return False


def _other_drives() -> list:
    """列出存在的非系统盘，按剩余空间从大到小排（小盘装完 Node 会撑爆）"""
    out = []
    if os.name != "nt":
        return out
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        root = letter + ":\\"
        if not os.path.isdir(root):
            continue
        try:
            free = shutil.disk_usage(root).free
        except Exception:
            continue
        out.append((root, free))
    out.sort(key=lambda x: -x[1])
    return out


def astrbot_data_dir() -> str:
    """插件放在 <data>/plugins/<插件名>，往上两级就是 AstrBot 的 data 目录。"""
    d = os.path.dirname(os.path.dirname(PLUGIN_DIR))
    return d if os.path.isdir(d) else ""


def default_base() -> str:
    """没指定安装目录时挑一个，原则：不塞系统盘、跟 AstrBot 的插件数据住一块。

    1) AstrBot 的 data 目录不在系统盘 → <data>\\openclaw   （首选，跟别的插件同一个地盘）
    2) 否则挑剩余空间最大的非系统盘 → <盘>:\\AstrBot\\openclaw
    3) 实在没有别的盘 → <data>\\openclaw，最差落到 用户目录\\OpenClaw
    """
    data = astrbot_data_dir()
    if data and not _is_system_drive(data):
        return os.path.join(data, "openclaw")
    for root, free in _other_drives():
        if free >= 2 * 1024 ** 3:
            return os.path.join(root, "AstrBot", "openclaw")
    if data:
        return os.path.join(data, "openclaw")
    return os.path.join(os.path.expanduser("~"), "OpenClaw")


def _asset(name: str) -> str:
    return os.path.join(ASSETS, name)


def _ver_tuple(v: str) -> tuple:
    out = []
    for part in str(v).lstrip("v").replace("-", ".").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def asset_version() -> str:
    try:
        with open(_asset("VERSION"), encoding="utf-8") as f:
            return (f.read() or "").strip() or "0"
    except Exception:
        return "0"


def node_dir_name(node_version: str = DEFAULT_NODE_VERSION) -> str:
    """node-v24.22.2-win-x64 —— 注意 v 不能丢，解压出来的目录就是带 v 的。"""
    return "node-v%s-win-x64" % str(node_version or DEFAULT_NODE_VERSION).lstrip("v")


def paths(base: str) -> dict:
    ver = _node_ver_for(base)
    nd = node_dir_name(ver)
    return {
        "base": base,
        "bridge_dir": os.path.join(base, "bridge"),
        "bridge_py": os.path.join(base, "bridge", "bridge.py"),
        "bridge_cfg": os.path.join(base, "bridge", "config.json"),
        "svc": os.path.join(base, "svc.py"),
        "lifecycle": os.path.join(base, "lifecycle.py"),
        "node_dir": os.path.join(base, nd),
        "node_exe": os.path.join(base, nd, "node.exe"),
        "npm_cmd": os.path.join(base, nd, "npm.cmd"),
        "npm_prefix": os.path.join(base, "npm-global"),
        "openclaw_mjs": os.path.join(
            base, "npm-global", "node_modules", "openclaw", "openclaw.mjs"
        ),
        "home": os.path.join(base, "home"),
        "oc_config": os.path.join(base, "home", ".openclaw", "openclaw.json"),
        "logs": os.path.join(base, "logs"),
        "run": os.path.join(base, "run"),
        "work": os.path.join(base, "work"),
    }


def _node_ver_for(base: str) -> str:
    """优先级：bridge/config.json 里记的 node_dir > 目录里实际存在的 node-v* > 默认版本。"""
    try:
        with open(os.path.join(base, "bridge", "config.json"), encoding="utf-8") as f:
            nd = str(json.load(f).get("node_dir") or "")
        if nd.startswith("node-v") and nd.endswith("-win-x64"):
            return "v" + nd[len("node-v") : -len("-win-x64")]
    except Exception:
        pass
    try:
        for name in sorted(os.listdir(base)):
            if name.startswith("node-v") and name.endswith("-win-x64"):
                if os.path.isfile(os.path.join(base, name, "node.exe")):
                    return "v" + name[len("node-v") : -len("-win-x64")]
    except Exception:
        pass
    return DEFAULT_NODE_VERSION


def installed(base: str) -> dict:
    p = paths(base)
    info = {
        "base": base,
        "node": os.path.isfile(p["node_exe"]),
        "openclaw": os.path.isfile(p["openclaw_mjs"]),
        "bridge": os.path.isfile(p["bridge_py"]),
        "config": os.path.isfile(p["oc_config"]),
        "svc": os.path.isfile(p["svc"]),
    }
    info["ready"] = all(info[k] for k in ("node", "openclaw", "bridge", "config", "svc"))
    return info


def child_env(base: str) -> dict:
    p = paths(base)
    env = os.environ.copy()
    env["PATH"] = p["node_dir"] + ";" + p["npm_prefix"] + ";" + env.get("PATH", "")
    env["OPENCLAW_HOME"] = p["home"]
    env["npm_config_cache"] = os.path.join(base, "npm-cache")
    return env


def _run(cmd, base, log, timeout=1800, cwd=None):
    """跑一条命令，输出原样灌进日志，返回 (code, tail)。"""
    log("$ " + " ".join(str(c) for c in cmd))
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd or paths(base)["base"],
            env=child_env(base),
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            shell=isinstance(cmd, str),
        )
    except subprocess.TimeoutExpired:
        log("超时 %ss" % timeout)
        return -1, "timeout"
    out = (p.stdout or b"").decode("utf-8", errors="replace")
    err = (p.stderr or b"").decode("utf-8", errors="replace")
    tail = (out + "\n" + err).strip()
    for line in tail.splitlines()[-40:]:
        log("  | " + line)
    return p.returncode, tail


# ------------------------------------------------------------------ 文件铺设
def sync_assets(base: str, log=print) -> list:
    """把插件自带的脚本铺到安装目录；版本变了才覆盖，配置永远不动。"""
    p = paths(base)
    done = []
    for k in ("bridge_dir", "home", "logs", "run", "work"):
        os.makedirs(p[k], exist_ok=True)
    os.makedirs(os.path.join(p["home"], ".openclaw"), exist_ok=True)

    pairs = [
        ("bridge.py", p["bridge_py"], True),
        ("claw.py", os.path.join(p["bridge_dir"], "claw.py"), True),
        ("svc.py", p["svc"], True),
        ("lifecycle.py", p["lifecycle"], True),
        ("bridge_config.json", p["bridge_cfg"], False),  # 已有就不覆盖
    ]
    for name, dst, overwrite in pairs:
        src = _asset(name)
        if not os.path.isfile(src):
            continue
        if os.path.isfile(dst) and not overwrite:
            continue
        try:
            shutil.copy2(src, dst)
            done.append(os.path.basename(dst))
            log("铺好 " + os.path.basename(dst))
        except Exception as exc:
            log("铺 %s 失败：%r" % (dst, exc))

    # 两个双击就能用的 bat
    bat_py = sys.executable
    for name, args, tip in (
        ("start_openclaw.bat", "start", "起网关+薄壳"),
        ("stop_openclaw.bat", "stop", "全停+清残存"),
    ):
        try:
            with open(os.path.join(base, name), "w", encoding="gbk", newline="\r\n") as f:
                f.write("@echo off\r\n")
                f.write('rem %s\r\n' % tip)
                f.write('cd /d "%%~dp0"\r\n')
                f.write('"%s" "%%~dp0svc.py" %s\r\n' % (bat_py, args))
                f.write("pause\r\n")
            done.append(name)
        except Exception:
            pass

    try:
        with open(os.path.join(base, ".shell_version"), "w", encoding="utf-8") as f:
            f.write(asset_version())
    except Exception:
        pass
    return done


def write_configs(base: str, opts: dict, log=print) -> dict:
    """写 openclaw.json（没有才写，不碰用户已有配置）。"""
    p = paths(base)
    made = {}
    if os.path.isfile(p["oc_config"]):
        log("openclaw.json 已存在，保留原样")
        made["config_kept"] = True
    else:
        model = (opts.get("model") or "").strip() or "glm-5.3-flash"
        provider = model.split("/")[0] if "/" in model else "custom"
        model_id = model.split("/", 1)[1] if "/" in model else model
        base_url = (opts.get("base_url") or "").strip()
        api_key = (opts.get("api_key") or "").strip()
        cfg = {
            "agents": {"defaults": {"model": "%s/%s" % (provider, model_id)}},
            "gateway": {
                "bind": "loopback",
                "mode": "local",
                "port": int(opts.get("gateway_port") or 18789),
            },
        }
        if base_url and api_key:
            cfg["models"] = {
                "providers": {
                    provider: {
                        "baseUrl": base_url,
                        "apiKey": api_key,
                        "api": "openai-completions",
                        "models": [{"id": model_id, "name": model_id}],
                    }
                }
            }
        else:
            log("没配 base_url/api_key，先写个空壳，后面在插件配置里补")
        os.makedirs(os.path.dirname(p["oc_config"]), exist_ok=True)
        with open(p["oc_config"], "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        made["config"] = p["oc_config"]
        log("写好 openclaw.json")

    # 薄壳配置：端口/模型跟着插件配置走（已有文件只改这两个键）
    try:
        if os.path.isfile(p["bridge_cfg"]):
            with open(p["bridge_cfg"], encoding="utf-8") as f:
                bcfg = json.load(f)
        else:
            bcfg = {}
        if opts.get("bridge_port"):
            bcfg["port"] = int(opts["bridge_port"])
        if opts.get("model"):
            bcfg["default_model"] = str(opts["model"])
        bcfg.setdefault("node_dir", node_dir_name(DEFAULT_NODE_VERSION))
        bcfg.setdefault("task_timeout_seconds", 120)
        bcfg.setdefault("max_task_timeout_seconds", 300)
        bcfg.setdefault("max_prompt_chars", 6000)
        bcfg.setdefault("max_reply_chars", 4000)
        bcfg.setdefault("dedup_seconds", 90)
        bcfg.setdefault("rate_limit_per_hour", 20)
        bcfg.setdefault("max_concurrent", 1)
        bcfg.setdefault("fail_streak_limit", 3)
        bcfg.setdefault("cooldown_seconds", 600)
        bcfg.setdefault("forbid_patterns", ["openclaw agent", "~/.openclaw", "循环调用"])
        with open(p["bridge_cfg"], "w", encoding="utf-8") as f:
            json.dump(bcfg, f, ensure_ascii=False, indent=2)
        made["bridge_config"] = p["bridge_cfg"]
    except Exception as exc:
        log("薄壳配置写失败：%r" % exc)
    return made


# ------------------------------------------------------------------ 下载/解压
def download(url: str, dst: str, log=print, timeout: float = 60.0) -> bool:
    log("下载 " + url)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r, open(dst, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total and got % (4 * 1024 * 1024) < 256 * 1024:
                    log("  %d%% (%.0f/%.0f MB)" % (got * 100 / total, got / 1048576, total / 1048576))
        log("下载完成 %.1f MB" % (os.path.getsize(dst) / 1048576))
        return True
    except Exception as exc:
        log("下载失败：%r" % exc)
        try:
            os.remove(dst)
        except Exception:
            pass
        return False


def ensure_node(base: str, node_version: str, mirror: str, log=print) -> bool:
    ver = str(node_version or DEFAULT_NODE_VERSION).lstrip("v")
    nd = "node-v%s-win-x64" % ver
    node_exe = os.path.join(base, nd, "node.exe")
    if os.path.isfile(node_exe):
        log("Node 已在：" + os.path.join(base, nd))
        return True
    zip_name = nd + ".zip"
    os.makedirs(base, exist_ok=True)
    dst = os.path.join(base, zip_name)
    mirror = (mirror or "").strip().rstrip("/")
    urls = [u.rstrip("/") + "/v%s/%s" % (ver, zip_name) for u in (mirror or MIRROR_NODE, OFFICIAL_NODE)]
    urls = list(dict.fromkeys(urls))
    for u in urls:
        if download(u, dst, log):
            break
    else:
        return False
    try:
        log("解压中……")
        with zipfile.ZipFile(dst) as z:
            z.extractall(base)
        os.remove(dst)
    except Exception as exc:
        log("解压失败：%r" % exc)
        return False
    ok = os.path.isfile(node_exe)
    log("Node %s" % ("就位" if ok else "没解出来"))
    return ok


def ensure_openclaw(base: str, registry: str, log=print) -> bool:
    p = paths(base)
    if os.path.isfile(p["openclaw_mjs"]):
        log("openclaw 已在：" + p["npm_prefix"])
        return True
    if not os.path.isfile(p["npm_cmd"]):
        log("没有 npm，Node 没装好？")
        return False
    os.makedirs(p["npm_prefix"], exist_ok=True)
    cmd = [
        p["npm_cmd"],
        "install",
        "-g",
        "openclaw",
        "--prefix",
        p["npm_prefix"],
        "--registry",
        (registry or DEFAULT_REGISTRY).strip(),
        "--no-fund",
        "--no-audit",
        "--allow-scripts=" + ALLOW_SCRIPTS,
    ]
    code, tail = _run(cmd, base, log, timeout=2400)
    ok = os.path.isfile(p["openclaw_mjs"])
    log("openclaw %s（npm 返回 %s）" % ("装好了" if ok else "没装上", code))
    return ok


# ------------------------------------------------------------------ 主流程
def install(base: str | None = None, opts: dict | None = None, log=print) -> dict:
    opts = opts or {}
    base = base or opts.get("install_dir") or default_base()
    base = os.path.abspath(os.path.expanduser(base))
    t0 = time.time()
    result = {"ok": False, "base": base, "steps": []}

    def step(name, ok, extra=None):
        result["steps"].append({"step": name, "ok": bool(ok), **(extra or {})})
        log("[%s] %s" % ("OK" if ok else "失败", name))

    log("==== 开始安装，目录 %s ====" % base)
    os.makedirs(base, exist_ok=True)

    done = sync_assets(base, log)
    step("铺脚本", bool(done), {"files": done})

    ok_node = ensure_node(base, opts.get("node_version") or DEFAULT_NODE_VERSION, opts.get("node_mirror") or "", log)
    step("Node", ok_node)
    if not ok_node:
        result["error"] = "Node 没装成，检查网络或换 node_mirror"
        result["elapsed"] = round(time.time() - t0, 1)
        return result

    ok_oc = ensure_openclaw(base, opts.get("registry") or DEFAULT_REGISTRY, log)
    step("openclaw", ok_oc)
    if not ok_oc:
        result["error"] = "openclaw 没装上，看 logs 和 npm 日志"
        result["elapsed"] = round(time.time() - t0, 1)
        return result

    made = write_configs(base, opts, log)
    step("写配置", True, made)

    resp = svc(base, "start", log=log)
    step("起服务", resp.get("ok"), {"gateway": resp.get("gateway"), "bridge": resp.get("bridge")})

    result["ok"] = bool(resp.get("ok"))
    result["status"] = installed(base)
    result["elapsed"] = round(time.time() - t0, 1)
    log("==== 安装结束：%s，用时 %ss ====" % ("成功" if result["ok"] else "没成", result["elapsed"]))
    return result


def svc(base: str, action: str, extra: list | None = None, log=None, timeout: int = 600) -> dict:
    """调 svc.py：start / stop / status / check"""
    p = paths(base)
    if not os.path.isfile(p["svc"]):
        return {"ok": False, "error": "svc.py 不在，先跑安装"}
    cmd = [sys.executable, p["svc"], action] + list(extra or [])
    try:
        out = subprocess.run(
            cmd,
            cwd=base,
            env=child_env(base),
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", errors="replace").strip()
        try:
            return json.loads(text[text.index("{"): text.rindex("}") + 1])
        except Exception:
            return {"ok": out.returncode == 0, "raw": text[:2000], "code": out.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "svc %s 超时 %ss" % (action, timeout)}
    except Exception as exc:
        return {"ok": False, "error": repr(exc)}


def health(base: str) -> dict | None:
    import urllib.error

    p = paths(base)
    try:
        with open(p["bridge_cfg"], encoding="utf-8") as f:
            port = int(json.load(f).get("port") or 18790)
    except Exception:
        port = 18790
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8", errors="replace"))
        data["port"] = port
        return data
    except Exception:
        return None


if __name__ == "__main__":
    # 命令行直接用：python installer.py install --base D:\OpenClaw
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["install", "check", "status", "start", "stop"])
    ap.add_argument("--base", default="")
    ap.add_argument("--node-version", default=DEFAULT_NODE_VERSION)
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--node-mirror", default="")
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--model", default="")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--gateway-port", type=int, default=18789)
    a = ap.parse_args()

    b = a.base or default_base()
    if a.action == "install":
        r = install(b, vars(a))
    elif a.action in ("start",):
        r = svc(b, "start", ["--parent-pid", str(a.parent_pid)] if a.parent_pid else [])
    elif a.action == "stop":
        r = svc(b, "stop", ["--reason", "cli"])
    elif a.action == "status":
        r = svc(b, "status")
    else:
        r = installed(b)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if (r.get("ok") or r.get("ready")) else 1)
