# -*- coding: utf-8 -*-
"""ETF 轮动网站服务器：静态文件 + /api/update 触发行情数据更新。

打开网页时前端会请求 /api/update，后台依次执行 fetch_data.py 和 export_json.py；
30 分钟内重复请求自动跳过，避免频繁抓取。
"""
import json
import random
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
SITE_DIR = ROOT / "site"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
LOG_FILE = ROOT / "update.log"
PORT = 8001
THROTTLE_SEC = 30 * 60  # 30 分钟内只更新一次

_update_lock = threading.Lock()
_last_run = 0.0
_running = False

_practice_df = None


def practice_df():
    """延迟加载 5 分钟练习数据（含 MACD柱/VWAP 指标）"""
    global _practice_df
    if _practice_df is None:
        sys.path.insert(0, str(ROOT))
        from practice import prepare
        _practice_df = prepare()
    return _practice_df


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
        elif self.path.startswith("/api/practice/day"):
            self.handle_practice_day()
        elif self.path.startswith("/api/practice/optimal"):
            self.handle_practice_optimal()
        elif self.path.startswith("/api/practice/stats"):
            self.handle_practice_stats()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/practice/log"):
            length = int(self.headers.get("Content-Length", 0))
            try:
                rec = json.loads(self.rfile.read(length).decode("utf-8"))
                from practice import append_log
                append_log(rec)
                self._send_json({"ok": True})
            except Exception as e:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(e)})
        else:
            self._send_json({"ok": False, "error": "unknown api"})

    # ---- 做T演练 API ----

    def _practice_day_df(self):
        """解析 ?date= 并返回当日数据，无 date 参数则随机抽一天"""
        import datetime as dt
        df = practice_df()
        days = sorted(set(df.index.date))
        date_str = parse_qs(urlparse(self.path).query).get("date", [""])[0]
        if date_str:
            day = dt.date.fromisoformat(date_str)
            if day not in days:
                return None, None, days
        else:
            day = random.choice(days)
        return day, df[df.index.date == day], days

    def handle_practice_day(self):
        day, d, days = self._practice_day_df()
        if day is None:
            return self._send_json({"ok": False, "error": "该日无数据"})
        bars = [{
            "t": t.strftime("%H:%M"),
            "o": round(float(r.open), 4), "h": round(float(r.high), 4),
            "l": round(float(r.low), 4), "c": round(float(r.close), 4),
            "vol": round(float(r.vol), 0),
            "hist": round(float(r["hist"]), 5), "vwap": round(float(r["vwap"]), 4),
            "dif": round(float(r["dif"]), 5), "dea": round(float(r["dea"]), 5),
            "bsig": int(r["buy_sig"]), "ssig": int(r["sell_sig"]),
        } for t, r in d.iterrows()]
        df = practice_df()
        prev = df[df.index.date < day]
        prev_close = round(float(prev["close"].iloc[-1]), 4) if len(prev) else None
        self._send_json({"ok": True, "date": str(day), "bars": bars,
                         "prev_close": prev_close,
                         "range": [str(days[0]), str(days[-1])]})

    def handle_practice_optimal(self):
        from t_optimal import optimal_t
        day, d, _ = self._practice_day_df()
        if day is None:
            return self._send_json({"ok": False, "error": "该日无数据"})
        ret, trades = optimal_t(d["close"])
        self._send_json({
            "ok": True, "ret": round(ret, 6),
            "trades": [[t.strftime("%H:%M"), act, round(float(p), 4)]
                       for t, act, p in trades],
        })

    def handle_practice_stats(self):
        log_path = ROOT / "practice_log.csv"
        rows = []
        if log_path.exists():
            import csv
            with log_path.open(encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self._send_json({"ok": True, "rows": rows})

    def handle_update(self):
        global _last_run, _running
        force = parse_qs(urlparse(self.path).query).get("force", [""])[0] == "1"
        result = {"ok": True, "skipped": False}
        with _update_lock:
            if _running:
                # 已有更新在进行中，视为重复打开页面，直接跳过
                result["skipped"] = True
            elif not force and time.time() - _last_run < THROTTLE_SEC:
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
    # 绑定 0.0.0.0：本机用 127.0.0.1 访问，手机等局域网设备用本机局域网 IP 访问
    import socket
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        lan_ip = "?"
    print(f"服务已启动: http://127.0.0.1:{PORT}/  (局域网: http://{lan_ip}:{PORT}/)")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
