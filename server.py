# -*- coding: utf-8 -*-
"""ETF 轮动网站服务器：静态文件 + /api/update 触发行情数据更新 + 盘中实时。

打开网页时前端请求 /api/update，后台线程依次执行 fetch_data.py、export_json.py，
接口立刻返回，前端用 /api/status 轮询进度；30 秒内重复请求自动跳过。
live.py 作为守护线程在交易时段刷新 site/live.json（ETF 快照 + 平均股价信号预览）。
"""
import datetime as dt
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
STATUS_FILE = ROOT / "data" / "_status.json"
PROGRESS_FILE = ROOT / "data" / "_progress.json"
PORT = 8001
THROTTLE_SEC = 30  # 30 秒内只更新一次（force=1 不受限）

_update_lock = threading.Lock()
_last_run = 0.0
_running = False
_run_started = 0.0
_last_result = {}

_practice_df = None


def practice_df():
    """延迟加载 5 分钟练习数据（含 MACD柱/VWAP 指标）"""
    global _practice_df
    if _practice_df is None:
        sys.path.insert(0, str(ROOT))
        from practice import prepare
        _practice_df = prepare()
    return _practice_df


def publish_allowed():
    """盘中不推送 gh-pages：收盘后（15:05 起）或显式 PUBLISH_ALWAYS=1 才发布"""
    import os
    return dt.datetime.now().time() >= dt.time(15, 5) or os.environ.get("PUBLISH_ALWAYS") == "1"


def run_update():
    """依次执行 fetch_data.py、export_json.py，返回 (是否成功, 错误信息)。"""
    with LOG_FILE.open("a", encoding="utf-8") as log:
        for script in ("fetch_data.py", "export_json.py"):
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 运行 {script}\n")
            proc = subprocess.run(
                [str(PYTHON), script],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=600,
                # 服务器经计划任务无窗口运行，子进程是控制台程序会弹黑窗，此处禁止创建窗口
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0:
                return False, f"{script} 执行失败（退出码 {proc.returncode}），详见 update.log"
        # 数据导出成功后异步发布 gh-pages（不阻塞刷新；发布失败不影响本地更新结果）
        if publish_allowed():
            threading.Thread(target=_publish, daemon=True).start()
    return True, ""


def _publish():
    """后台推送静态站：git push 通常要几秒，放后台避免拖慢页面刷新"""
    try:
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 运行 publish_site.bat\n")
            subprocess.run(
                ["cmd.exe", "/c", "publish_site.bat"],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except Exception:  # noqa: BLE001 - 发布失败不阻断主流程
        pass


def _update_worker():
    """后台更新线程：结果写入 _last_result，供 /api/status 查询"""
    global _last_run, _running, _last_result
    try:
        ok, err = run_update()
        _last_result = {"ok": ok, "error": err, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:  # noqa: BLE001 - 线程内异常必须兜底
        _last_result = {"ok": False, "error": str(e), "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    finally:
        with _update_lock:
            _last_run = time.time()
            _running = False


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE_DIR), **kwargs)

    def do_GET(self):
        if self.path.startswith("/api/update"):
            self.handle_update()
        elif self.path.startswith("/api/status"):
            self.handle_status()
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
        global _last_run, _running, _run_started
        force = parse_qs(urlparse(self.path).query).get("force", [""])[0] == "1"
        with _update_lock:
            if _running:
                return self._send_json({"ok": True, "started": False, "running": True})
            if not force and time.time() - _last_run < THROTTLE_SEC:
                return self._send_json({"ok": True, "started": False, "skipped": True,
                                        "last": _last_result})
            _running = True
            _run_started = time.time()
        threading.Thread(target=_update_worker, daemon=True).start()
        self._send_json({"ok": True, "started": True})

    def handle_status(self):
        """更新进度：后台线程是否在跑 + fetch_data 的逐标的进度"""
        progress = {}
        try:
            progress = json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - 没有进度文件属正常情况
            pass
        status = {}
        try:
            status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        with _update_lock:
            running = _running
            started = _run_started
            last = _last_result
        self._send_json({
            "ok": True, "running": running,
            "elapsed": round(time.time() - started, 1) if running and started else 0,
            "progress": progress, "status": status, "last": last,
        })

    def end_headers(self):
        # 页面和 JSON 每天都会更新；不发缓存头时浏览器按 Last-Modified 的 10%
        # 时长做启发式缓存，会直接拿旧文件不重校验，表现为“网站不更新”
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # 静默访问日志
        pass


def startup_update_if_stale():
    """启动兜底：data.json 数据日期早于今天（周末回退到周五判断）时后台补跑一次更新。
    计划任务在关机/睡眠期间会错过，这里是双保险。"""
    global _last_run, _running
    try:
        last = json.loads((SITE_DIR / "data.json").read_text(encoding="utf-8"))["dates"][-1]
    except Exception:  # noqa: BLE001 - 读取失败一律视为需要更新
        last = ""
    import datetime as dt
    d = dt.date.today()
    if d.weekday() >= 5:
        d -= dt.timedelta(days=d.weekday() - 4)
    if last and last >= d.isoformat():
        return
    with _update_lock:
        if _running:
            return
        _running = True
    try:
        run_update()
    except Exception:  # noqa: BLE001 - 兜底逻辑不影响服务器启动
        pass
    finally:
        with _update_lock:
            _last_run = time.time()
            _running = False


def _live_loop():
    """盘中实时线程：失败只记日志，绝不拖垮网站服务"""
    try:
        import live
        live.main()
    except Exception as e:  # noqa: BLE001
        with LOG_FILE.open("a", encoding="utf-8") as log:
            log.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] live 线程退出: {e!r}\n")


if __name__ == "__main__":
    # 绑定 0.0.0.0：本机用 127.0.0.1 访问，手机等局域网设备用本机局域网 IP 访问
    import os
    import socket
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        lan_ip = "?"
    print(f"服务已启动: http://127.0.0.1:{PORT}/  (局域网: http://{lan_ip}:{PORT}/)")
    threading.Thread(target=startup_update_if_stale, daemon=True).start()
    if os.environ.get("LIVE_DISABLE") != "1":
        threading.Thread(target=_live_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
