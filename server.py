# -*- coding: utf-8 -*-
"""ETF 轮动网站服务器：静态文件 + /api/update 触发行情数据更新。

打开网页时前端会请求 /api/update，后台依次执行 fetch_data.py 和 export_json.py；
30 分钟内重复请求自动跳过，避免频繁抓取。
"""
import json
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "site"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG_FILE = ROOT / "update.log"
PORT = 8002
THROTTLE_SEC = 30 * 60  # 30 分钟内只更新一次

_update_lock = threading.Lock()
_last_run = 0.0
_running = False


def run_update():
    """依次执行 fetch_data.py 和 export_json.py，返回 (是否成功, 错误信息)。"""
    with LOG_FILE.open("a", encoding="utf-8") as log:
        for script in ("fetch_data.py", "export_json.py"):
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 运行 {script}\n")
            proc = subprocess.run(
                [str(PYTHON), script],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
            )
            if proc.returncode != 0:
                return False, f"{script} 执行失败（退出码 {proc.returncode}），详见 update.log"
    return True, ""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/update"):
            self.handle_update()
        else:
            super().do_GET()

    def handle_update(self):
        global _last_run, _running
        result = {"ok": True, "skipped": False}
        with _update_lock:
            if _running:
                # 已有更新在进行中，视为重复打开页面，直接跳过
                result["skipped"] = True
            elif time.time() - _last_run < THROTTLE_SEC:
                result["skipped"] = True
            else:
                _running = True
        if result["skipped"]:
            return self._send_json(result)
        try:
            ok, err = run_update()
            result = {"ok": ok, "skipped": False}
            if not ok:
                result["error"] = err
        except Exception as e:  # noqa: BLE001 - 接口需兜底返回 JSON
            result = {"ok": False, "skipped": False, "error": str(e)}
        finally:
            with _update_lock:
                _last_run = time.time()
                _running = False
        self._send_json(result)

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


if __name__ == "__main__":
    print(f"服务已启动: http://127.0.0.1:{PORT}/")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
