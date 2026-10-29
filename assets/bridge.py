# -*- coding: utf-8 -*-
"""鲸鱼娘 <-> 小龙虾(OpenClaw) 薄壳网关
只做三件事：收任务、限流熔断、把结果原样还回去。
"""
import json, os, re, subprocess, sys, threading, time, hashlib
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BR_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(BR_DIR)
CFG = json.load(open(os.path.join(BR_DIR, "config.json"), encoding="utf-8"))
NODE_HOME = os.path.join(BASE, CFG["node_dir"])
PREFIX = os.path.join(BASE, "npm-global")
OC = os.path.join(PREFIX, "openclaw.cmd")
LOG = os.path.join(BASE, "logs", "bridge.log")

ENV = os.environ.copy()
ENV["PATH"] = NODE_HOME + ";" + PREFIX + ";" + ENV.get("PATH", "")
ENV["OPENCLAW_HOME"] = os.path.join(BASE, "home")

_lock = threading.Lock()
_busy = False
_calls = deque()
_last_prompt = (None, 0.0)
_fail_streak = 0
_cooldown_until = 0.0


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse_reply(raw):
    """从 openclaw --json 的输出里抠出最终可见文本。"""
    for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
        for m in re.finditer(r'"%s"\s*:\s*("(?:[^"\\]|\\.)*")' % key, raw):
            try:
                v = json.loads(m.group(1))
                if v.strip():
                    return v.strip()
            except Exception:
                pass
    m = re.search(r'"terminalReply"\s*:\s*\{[^{}]*?"text"\s*:\s*("(?:[^"\\]|\\.)*")', raw, re.S)
    if m:
        try:
            return json.loads(m.group(1)).strip()
        except Exception:
            pass
    return ""


def kill_tree(pid):
    subprocess.run("taskkill /PID %d /T /F" % pid, shell=True, capture_output=True)


def gateway_port():
    """从 home/.openclaw/openclaw.json 里抠网关端口，抠不到就用 18789。"""
    try:
        c = json.load(open(os.path.join(BASE, "home", ".openclaw", "openclaw.json"), encoding="utf-8"))
        return int((c.get("gateway") or {}).get("port") or 18789)
    except Exception:
        return 18789


def gateway_alive():
    """只捅一下端口，秒回，不去 fork 一遍 CLI。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", gateway_port()), timeout=1.5):
            return True
    except Exception:
        return False


def ensure_gateway():
    """常驻网关不在就拉起来，省掉每单的冷启动。"""
    if gateway_alive():
        return True
    try:
        r = subprocess.run([OC, "health"], capture_output=True, timeout=25, env=ENV)
        if r.returncode == 0:
            return True
    except Exception:
        pass
    log("gateway down, starting")
    f = open(os.path.join(BASE, "logs", "gateway.log"), "ab")
    subprocess.Popen([OC, "gateway", "run"], stdout=f, stderr=subprocess.STDOUT, env=ENV,
                     cwd=os.path.join(BASE, "work"),
                     creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    for _ in range(45):
        time.sleep(2)
        try:
            r = subprocess.run([OC, "health"], capture_output=True, timeout=25, env=ENV)
            if r.returncode == 0:
                log("gateway up")
                return True
        except Exception:
            pass
    log("gateway start failed")
    return False


def run_task(prompt, timeout=None, model=None):
    global _busy, _fail_streak, _cooldown_until, _last_prompt
    now = time.time()
    timeout = min(int(timeout or CFG["task_timeout_seconds"]), CFG["max_task_timeout_seconds"])
    prompt = (prompt or "").strip()

    # --- 守门：一条条卡死，防止我俩互相喂话 ---
    if not prompt:
        return 400, {"ok": False, "error": "任务为空"}
    if len(prompt) > CFG["max_prompt_chars"]:
        return 413, {"ok": False, "error": "任务太长，超过 %d 字" % CFG["max_prompt_chars"]}
    low = prompt.lower()
    for bad in CFG["forbid_patterns"]:
        if bad.lower() in low:
            log("BLOCK forbid", bad)
            return 403, {"ok": False, "error": "任务里含递归/自调用关键词：%s" % bad}

    with _lock:
        if now < _cooldown_until:
            return 429, {"ok": False, "error": "熔断中，%d 秒后再说" % int(_cooldown_until - now)}
        if _busy:
            return 429, {"ok": False, "error": "上一单还没干完"}
        while _calls and now - _calls[0] > 3600:
            _calls.popleft()
        if len(_calls) >= CFG["rate_limit_per_hour"]:
            return 429, {"ok": False, "error": "本小时额度用完(%d 单)" % CFG["rate_limit_per_hour"]}
        h = hashlib.md5(re.sub(r"\s+", " ", prompt).encode()).hexdigest()
        if _last_prompt[0] == h and now - _last_prompt[1] < CFG["dedup_seconds"]:
            return 429, {"ok": False, "error": "同一句话刚发过，拒绝复读"}
        _busy = True
        _calls.append(now)
        _last_prompt = (h, now)

    ensure_gateway()
    args = [OC, "agent", "-m", prompt, "--json", "--timeout", str(timeout)]
    if model:
        args += ["--model", model]
    t0 = time.time()
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=ENV,
                             cwd=os.path.join(BASE, "work"),
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            out, _ = p.communicate(timeout=timeout + 20)
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            out, _ = p.communicate()
            raise TimeoutError("超时 %ds 已强杀" % timeout)
        raw = out.decode("utf-8", errors="replace")
        reply = parse_reply(raw)
        if not reply:
            reply = raw[-800:]
        reply = reply[: CFG["max_reply_chars"]]
        with _lock:
            _fail_streak = 0
        log("OK %.1fs" % (time.time() - t0), prompt[:60].replace("\n", " "))
        return 200, {"ok": True, "reply": reply, "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        with _lock:
            _fail_streak += 1
            if _fail_streak >= CFG["fail_streak_limit"]:
                _cooldown_until = time.time() + CFG["cooldown_seconds"]
                _fail_streak = 0
        log("FAIL", repr(e)[:200], prompt[:60].replace("\n", " "))
        return 500, {"ok": False, "error": str(e)[:300]}
    finally:
        with _lock:
            _busy = False


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            with _lock:
                self._send(200, {"ok": True, "busy": _busy, "calls_last_hour": len(_calls),
                                 "cooldown": max(0, int(_cooldown_until - time.time())),
                                 "model": CFG["default_model"]})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/task"):
            return self._send(404, {"ok": False, "error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._send(400, {"ok": False, "error": "body 不是 JSON"})
        code, obj = run_task(data.get("prompt"), data.get("timeout"), data.get("model"))
        self._send(code, obj)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    port = CFG["port"]
    log("bridge start port", port)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
