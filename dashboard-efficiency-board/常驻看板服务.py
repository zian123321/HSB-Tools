#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
产出看板 · 常驻服务版（在自己/内网电脑上运行）
====================================================================
职责：
  1. 按 config.json 的间隔（默认 30 分钟）调用同目录「拉数脚本.py」拉三源数据并生成看板
     （对齐 :00/:30 自然点；电脑休眠唤醒后自动补拉一轮）
  2. 局域网内开 HTTP 服务：同事/手机打开 http://本机IP:端口 即看最新看板
  3. --install 注册 Windows 计划任务（登录时自动启动），崩溃由循环自愈，无需登录豆包

用法：
  python 常驻看板服务.py               前台运行（调试；Ctrl+C 退出）
  python 常驻看板服务.py --install     注册 Windows 开机/登录自启（后台运行）
  python 常驻看板服务.py --uninstall   取消自启
  python 常驻看板服务.py --port 9000   临时指定端口（默认读 config.json）

安全设计（重要，不要改）：
  HTTP 只开放「看板 HTML」一个文件；拉数脚本、凭证、缓存、模板等一律 404 不对外。
  凭证只存在本机 凭证.json，不随网页外传。
====================================================================
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PULL_SCRIPT = BASE_DIR / "拉数脚本.py"
DASHBOARD_FILE = BASE_DIR / "产出看板.html"
CONFIG_PATH = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LAST_RESULT_PATH = BASE_DIR / "last_result.json"
TASK_NAME = "efficiency-board-service"
PULL_TIMEOUT_SECONDS = 25 * 60
LOG_KEEP_DAYS = 14


# ---------- 配置读取（config.json 缺失时用默认值） ----------
def load_config():
    cfg = {"port": 8899, "interval_seconds": 1800}
    try:
        if CONFIG_PATH.exists():
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in user_cfg.items() if v not in (None, "")})
    except Exception:
        pass
    return cfg


CFG = load_config()
DEFAULT_PORT = int(CFG.get("port", 8899))
INTERVAL_SECONDS = int(CFG.get("interval_seconds", 1800))


# ---------- 日志：按天切割，保留 14 天 ----------
def _log_path():
    return BASE_DIR / ("服务日志-" + datetime.now().strftime("%Y%m%d") + ".txt")


def _clean_old_logs():
    try:
        cutoff = datetime.now() - timedelta(days=LOG_KEEP_DAYS)
        for f in BASE_DIR.glob("服务日志-*.txt"):
            m = re.match(r"服务日志-(\d{8})\.txt", f.name)
            if m:
                try:
                    if datetime.strptime(m.group(1), "%Y%m%d") < cutoff:
                        f.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------- state.json：调度与排障用 ----------
def write_state(**payload):
    try:
        state = {}
        if STATE_FILE.exists():
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        state.update(payload)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


# ---------- 拉数执行 ----------
LAST_OUT_FILE = BASE_DIR / "最近拉数输出.txt"
MAX_BACKOFF_SECONDS = 2 * 3600   # 连续失败退避上限 2 小时


def run_pull(verbose=True):
    """调用 拉数脚本.py。返回 (是否取到新数据, 状态)。状态 ok/expired/error/timeout。"""
    if verbose:
        log("开始拉数（调用 拉数脚本.py）…")
    updated, state = False, "error"
    try:
        proc = subprocess.run(
            [sys.executable, str(PULL_SCRIPT)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PULL_TIMEOUT_SECONDS,
        )
        out, err = proc.stdout or "", proc.stderr or ""
        # 完整落盘，便于事后排查（旧版只在窗口一闪而过）
        try:
            LAST_OUT_FILE.write_text(
                "退出码：%s\n更新时间：%s\n\n[stdout]\n%s\n[stderr]\n%s" % (
                    proc.returncode, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), out, err),
                encoding="utf-8")
        except Exception:
            pass

        auth_expired = any(k in out for k in ("登陆超时", "登录凭证失效", "凭证失效", "3001"))
        rc_ok = proc.returncode == 0

        # 逐源结果（新版 last_result.json；旧版没有则退回 stdout 尾部）
        detail_lines, all_ok = [], None
        try:
            res = json.loads(LAST_RESULT_PATH.read_text(encoding="utf-8")) if (LAST_RESULT_PATH.exists()) else None
            if res:
                srcs = (res.get("status", {}) or {}).get("sources", [])
                all_ok = True
                for s in srcs:
                    if s.get("ok"):
                        detail_lines.append("[正常] %s：%d 条" % (s["name"], s.get("count", 0)))
                    else:
                        all_ok = False
                        if s.get("state") == "expired":
                            detail_lines.append("[凭证失效] %s：%d 条（旧缓存兜底）｜%s" % (
                                s["name"], s.get("count", 0), (s.get("message") or "")[:160]))
                        else:
                            detail_lines.append("[异常] %s：%d 条（旧缓存兜底）｜%s" % (
                                s["name"], s.get("count", 0), (s.get("message") or "")[:160]))
        except Exception:
            detail_lines = [s.strip() for s in out.strip().splitlines() if s.strip()][-4:]

        if all_ok is True:
            updated, state = True, "ok"
        elif all_ok is False:
            updated, state = False, ("expired" if auth_expired else "error")
        else:
            # 旧版脚本：无 last_result.json，以退出码为准
            updated, state = rc_ok, ("expired" if auth_expired else ("ok" if rc_ok else "error"))

        if verbose:
            if updated:
                log("本轮数据已更新：")
                for line in detail_lines:
                    log("  " + line)
            elif state == "expired":
                log("凭证失效，未取到新数据（旧缓存兜底，看板不空白）：")
                for line in detail_lines:
                    log("  " + line)
                log("换证方法：python 更新凭证.py curl.txt（详见 换证步骤.txt）")
            elif state == "error":
                log("本轮未更新（原因见 最近拉数输出.txt）：")
                for line in detail_lines:
                    log("  " + line)
            else:
                log("退出码 %s；明细见 最近拉数输出.txt" % proc.returncode)

        write_state(last_success=(datetime.now().strftime("%Y-%m-%d %H:%M:%S") if updated else None),
                    last_pull_ok=updated, auth_state=state)
        return updated, state
    except subprocess.TimeoutExpired:
        if verbose:
            log("拉数超时（超过 25 分钟），本轮跳过。")
        write_state(last_pull_ok=False, auth_state="timeout")
        return False, "timeout"
    except Exception as e:
        if verbose:
            log("拉数异常：%s（明细见 最近拉数输出.txt）" % e)
        write_state(last_pull_ok=False, auth_state="error")
        return False, "error"


def next_aligned_time(interval):
    """下一个对齐点：向上取整到 interval 秒边界（interval=1800 即对齐 :00/:30）。"""
    now_ts = datetime.now().timestamp()
    return datetime.fromtimestamp(int(now_ts // interval + 1) * interval)


def pull_loop():
    """首轮立即拉；成功后按自然点对齐；连续失败自动退避拉长间隔降噪，成功一次即复位。"""
    consecutive_fail = 0

    def schedule_next(updated_now, fail):
        """返回 (下次时间戳, 模式)。成功对齐自然点；失败退避，封顶 2 小时。"""
        if updated_now:
            return next_aligned_time(INTERVAL_SECONDS).timestamp(), "aligned"
        backoff = min(INTERVAL_SECONDS * (2 ** min(fail, 2)), MAX_BACKOFF_SECONDS)
        return time.time() + backoff, "backoff"

    updated, _ = run_pull(verbose=True)
    consecutive_fail = 0 if updated else 1
    next_ts, mode = schedule_next(updated, consecutive_fail)
    if mode == "aligned":
        log("下一轮拉数时间：%s（间隔 %d 分钟）" % (
            datetime.fromtimestamp(next_ts).strftime("%H:%M:%S"), INTERVAL_SECONDS // 60))
    else:
        log("首轮未取到新数据，进入退避探活；换证成功后自动恢复正常间隔。")

    while True:
        time.sleep(15)
        if time.time() < next_ts:
            continue
        prev_fail = consecutive_fail
        # 失败前两轮详打日志，之后静默降噪（明细始终写入 最近拉数输出.txt）
        verbose = (consecutive_fail < 2)
        updated, state = run_pull(verbose=verbose)
        if updated:
            if prev_fail > 0:
                log("数据已恢复更新，回到正常间隔 %d 分钟。" % (INTERVAL_SECONDS // 60))
            consecutive_fail = 0
        else:
            consecutive_fail += 1
        next_ts, mode = schedule_next(updated, consecutive_fail)
        if mode == "aligned":
            log("下一轮拉数时间：%s" % datetime.fromtimestamp(next_ts).strftime("%Y-%m-%d %H:%M:%S"))
        else:
            mins = max(1, int((next_ts - time.time()) // 60))
            if consecutive_fail <= 2 or consecutive_fail % 4 == 1:
                log("连续第 %d 次未取到新数据，%d 分钟后再探；明细见 最近拉数输出.txt" % (consecutive_fail, mins))


# ---------- HTTP：只开放看板页面 ----------
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/", "/产出看板.html", "/index.html"):
            try:
                data = DASHBOARD_FILE.read_bytes()
            except Exception:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"dashboard not ready, wait for first pull")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"not found")

    def log_message(self, fmt, *args):
        try:
            if args and not str(args[0]).startswith("200"):
                log("HTTP " + (fmt % args))
        except Exception:
            pass


def start_http_server(port):
    server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


# ---------- Windows 计划任务自启（开机/登录自动启动） ----------
def install_startup():
    if sys.platform != "win32":
        log("当前系统不是 Windows，跳过自启注册（此功能仅 Windows 可用）。")
        return
    py = Path(sys.executable)
    pythonw = py.with_name("pythonw.exe")      # pythonw 无黑色控制台窗口
    launcher = str(pythonw if pythonw.exists() else py)
    script = str(Path(__file__).resolve())
    # 先清理旧版 VBS 启动项
    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    old_vbs = startup_dir / "efficiency-board-service.vbs"
    if old_vbs.exists():
        try:
            old_vbs.unlink()
        except Exception:
            pass
    cmd = 'schtasks /Create /TN "%s" /TR "\\"%s\\" \\"%s\\"" /SC ONLOGON /F' % (TASK_NAME, launcher, script)
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode == 0:
            log("已注册 Windows 计划任务「%s」：登录后自动后台运行，无需登录豆包。" % TASK_NAME)
            log("取消自启：python 常驻看板服务.py --uninstall")
        else:
            log("注册计划任务失败：%s %s" % (res.stdout, res.stderr))
            log("可改用启动文件夹方式：把本脚本快捷方式放入 shell:startup。")
    except Exception as e:
        log("注册计划任务异常：%s" % e)


def uninstall_startup():
    if sys.platform != "win32":
        log("当前系统不是 Windows，无可取消的自启项。")
        return
    subprocess.run('schtasks /Delete /TN "%s" /F' % TASK_NAME, shell=True,
                   capture_output=True, text=True)
    startup_dir = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    old_vbs = startup_dir / "efficiency-board-service.vbs"
    if old_vbs.exists():
        try:
            old_vbs.unlink()
        except Exception:
            pass
    log("已取消自启（计划任务 + 旧启动项均已移除）。")


def main():
    ap = argparse.ArgumentParser(description="产出看板常驻服务")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--install", action="store_true", help="注册 Windows 开机自启")
    ap.add_argument("--uninstall", action="store_true", help="取消开机自启")
    args = ap.parse_args()

    if args.install:
        install_startup()
        return
    if args.uninstall:
        uninstall_startup()
        return

    _clean_old_logs()
    port = args.port or DEFAULT_PORT
    log("产出看板 · 常驻服务启动")
    log("拉数脚本：%s" % PULL_SCRIPT)
    log("更新间隔：%d 分钟；服务端口：%d" % (INTERVAL_SECONDS // 60, port))

    if not PULL_SCRIPT.exists():
        log("找不到 %s —— 请把整个看板文件夹原样拷贝到本机再运行。" % PULL_SCRIPT)
        sys.exit(1)
    if not DASHBOARD_FILE.exists():
        log("尚未找到看板文件，首轮拉数完成后自动生成。")

    ip = get_lan_ip()
    log("局域网看板地址（同事/手机连同一 WiFi 即可打开）：")
    log("  http://%s:%d/" % (ip, port))
    log("本机打开：http://127.0.0.1:%d/" % port)

    server = start_http_server(port)
    log("HTTP 服务已启动（仅开放看板页面，其余文件一律不对外）。")
    try:
        pull_loop()
    except KeyboardInterrupt:
        log("收到停止信号，服务退出。")
        server.shutdown()


if __name__ == "__main__":
    main()
