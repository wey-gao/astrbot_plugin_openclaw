# -*- coding: utf-8 -*-
"""给鲸鱼娘用的调用脚本： python claw.py "要它干的活" """
import json, os, sys, subprocess, time, urllib.request

BR_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(BR_DIR)
CFG = json.load(open(os.path.join(BR_DIR, "config.json"), encoding="utf-8"))
URL = "http://127.0.0.1:%d" % CFG["port"]


def health():
    try:
        return json.loads(urllib.request.urlopen(URL + "/health", timeout=3).read().decode())
    except Exception:
        return None


def ensure_up():
    if health():
        return True
    bat = os.path.join(BASE, "start_bridge.bat")
    subprocess.Popen(["cmd", "/c", bat], cwd=BASE,
                     creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(20):
        time.sleep(0.5)
        if health():
            return True
    return False


def ask(prompt, timeout=None, model=None):
    body = {"prompt": prompt}
    if timeout:
        body["timeout"] = timeout
    if model:
        body["model"] = model
    req = urllib.request.Request(URL + "/task", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=(timeout or CFG["task_timeout_seconds"]) + 40) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "用法: python claw.py \"任务\""}, ensure_ascii=False))
        sys.exit(2)
    if not ensure_up():
        print(json.dumps({"ok": False, "error": "薄壳没起来，看 logs/bridge.log"}, ensure_ascii=False))
        sys.exit(3)
    res = ask(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
    print(json.dumps(res, ensure_ascii=False, indent=1))
