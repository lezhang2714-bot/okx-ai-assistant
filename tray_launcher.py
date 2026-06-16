#!/usr/bin/env python3
"""Windows tray + optional taskbar launcher for OKX AI Assistant."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Optional

APP_DIR = Path(__file__).resolve().parent
APP_NAME = "OKX AI Assistant"
PANEL_SCRIPT = APP_DIR / "web_control_panel.py"
ICON_ICO = APP_DIR / "web_assets" / "app.ico"
ICON_PNG = APP_DIR / "web_assets" / "app_icon.png"
HOST = os.getenv("WEB_CONTROL_PANEL_HOST", "127.0.0.1")
PORT = int(os.getenv("WEB_CONTROL_PANEL_PORT", "8765"))
PANEL_URL = f"http://127.0.0.1:{PORT}" if HOST in ("127.0.0.1", "localhost", "0.0.0.0") else f"http://{HOST}:{PORT}"
SHUTDOWN_URL = f"{PANEL_URL}/api/tray/shutdown"
TRAY_LAUNCH_ENV = "OKX_LAUNCHED_BY_TRAY"
PANEL_EXIT_TRAY_SHUTDOWN = 100
PANEL_EXIT_TRAY_RESTART = 101
LOCK_FILE = APP_DIR / "local_state" / "tray_launcher.pid"
LOG_FILE = APP_DIR / "local_state" / "tray_launcher.log"

PANEL_PROCESS: Optional[subprocess.Popen] = None
TRAY_ICON: Any = None
ROOT: Any = None
STATUS_VAR: Any = None
SHUTTING_DOWN = False
TK_AVAILABLE = False


def log_line(text: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {text}\n")
    except OSError:
        pass


def show_message(text: str, *, error: bool = False) -> None:
    log_line(("ERROR: " if error else "INFO: ") + text)
    try:
        import ctypes

        flags = 0x10 if error else 0x40
        ctypes.windll.user32.MessageBoxW(0, text, APP_NAME, flags)
    except Exception:
        pass


def resolve_python() -> Path:
    candidates = [
        APP_DIR / ".venv" / "Scripts" / "pythonw.exe",
        APP_DIR / ".venv" / "Scripts" / "python.exe",
        APP_DIR / ".python" / "pythonw.exe",
        APP_DIR / ".python" / "python.exe",
        APP_DIR / "build" / "python_runtime" / "pythonw.exe",
        APP_DIR / "build" / "python_runtime" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    for name in ("pythonw", "python", "py"):
        found = shutil.which(name)
        if found:
            return Path(found)
    raise RuntimeError("Python runtime not found. Run setup_windows_runtime.bat first.")


def panel_reachable() -> bool:
    try:
        with urllib.request.urlopen(PANEL_URL + "/login", timeout=1.5) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def wait_for_panel(timeout_seconds: float = 45.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if PANEL_PROCESS is not None and PANEL_PROCESS.poll() is not None:
            return False
        if panel_reachable():
            return True
        time.sleep(0.4)
    return False


def start_panel_process() -> None:
    global PANEL_PROCESS
    if PANEL_PROCESS is not None and PANEL_PROCESS.poll() is None:
        return
    python_exe = resolve_python()
    env = os.environ.copy()
    env["OKX_WEB_SKIP_BROWSER"] = "1"
    env[TRAY_LAUNCH_ENV] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    PANEL_PROCESS = subprocess.Popen(
        [str(python_exe), str(PANEL_SCRIPT)],
        cwd=str(APP_DIR),
        env=env,
        creationflags=creationflags,
    )


def request_panel_shutdown() -> None:
    try:
        request = urllib.request.Request(SHUTDOWN_URL, method="POST", data=b"")
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, timeout=5):
            pass
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        pass


def stop_panel_process(force_after_seconds: float = 12.0) -> None:
    global PANEL_PROCESS
    if panel_reachable():
        request_panel_shutdown()
        deadline = time.time() + force_after_seconds
        while time.time() < deadline:
            if not panel_reachable():
                break
            time.sleep(0.3)
    if PANEL_PROCESS is None:
        return
    if PANEL_PROCESS.poll() is None:
        PANEL_PROCESS.terminate()
        try:
            PANEL_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            PANEL_PROCESS.kill()
            PANEL_PROCESS.wait(timeout=3)
    PANEL_PROCESS = None


def open_panel_in_browser() -> None:
    webbrowser.open(PANEL_URL)


def set_status(text: str) -> None:
    if STATUS_VAR is not None:
        STATUS_VAR.set(text)


def release_instance_lock() -> None:
    try:
        if LOCK_FILE.is_file():
            LOCK_FILE.unlink()
    except OSError:
        pass


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_instance_lock() -> bool:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.is_file():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            old_pid = 0
        if process_alive(old_pid):
            return False
        try:
            LOCK_FILE.unlink()
        except OSError:
            return False
    try:
        LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except OSError:
        return False


def restart_panel_process() -> None:
    global PANEL_PROCESS
    PANEL_PROCESS = None
    start_panel_process()
    if wait_for_panel():
        set_status("服务运行中。关闭窗口即可停止服务。")
        return
    code = PANEL_PROCESS.poll() if PANEL_PROCESS is not None else None
    set_status(f"服务重启失败（exit={code}）。")
    show_message(f"Web 控制台重启失败（exit={code}）。", error=True)


def monitor_panel_lifecycle() -> None:
    global PANEL_PROCESS
    while not SHUTTING_DOWN:
        proc = PANEL_PROCESS
        if proc is None:
            time.sleep(0.5)
            continue
        code = proc.poll()
        if code is None:
            time.sleep(0.5)
            continue
        if SHUTTING_DOWN:
            return
        if code == PANEL_EXIT_TRAY_SHUTDOWN:
            log_line("panel requested shutdown, quitting tray")
            quit_application()
            return
        if code == PANEL_EXIT_TRAY_RESTART:
            log_line("panel requested restart, relaunching panel")
            restart_panel_process()
            continue
        log_line(f"panel exited unexpectedly code={code}")
        set_status(f"服务异常退出（exit={code}）。")
        show_message(
            f"Web 控制台异常退出（exit={code}）。\n\n请使用托盘「退出」后重新打开应用。",
            error=True,
        )
        PANEL_PROCESS = None


def start_panel_monitor() -> None:
    threading.Thread(target=monitor_panel_lifecycle, daemon=True).start()


def quit_application() -> None:
    global SHUTTING_DOWN, TRAY_ICON
    if SHUTTING_DOWN:
        return
    SHUTTING_DOWN = True
    log_line("quit requested")
    set_status("正在关闭服务…")
    stop_panel_process()
    if TRAY_ICON is not None:
        try:
            TRAY_ICON.stop()
        except Exception:
            pass
        TRAY_ICON = None
    if ROOT is not None:
        try:
            ROOT.quit()
            ROOT.destroy()
        except Exception:
            pass
    release_instance_lock()
    os._exit(0)


def on_window_close() -> None:
    if TK_AVAILABLE:
        import tkinter.messagebox as messagebox

        if messagebox.askyesno(APP_NAME, "关闭窗口将停止 OKX AI Assistant 服务，是否继续？"):
            quit_application()
        return
    quit_application()


def on_open_clicked() -> None:
    if panel_reachable():
        open_panel_in_browser()
        return
    if TK_AVAILABLE:
        import tkinter.messagebox as messagebox

        messagebox.showwarning(APP_NAME, "服务尚未就绪，请稍候再试。")
    else:
        show_message("服务尚未就绪，请稍候再试。")


def build_taskbar_window() -> Any:
    global ROOT, STATUS_VAR
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    ROOT = root
    root.title(APP_NAME)
    root.geometry("420x180")
    root.minsize(420, 180)
    root.protocol("WM_DELETE_WINDOW", on_window_close)

    if ICON_ICO.is_file():
        try:
            root.iconbitmap(default=str(ICON_ICO))
        except Exception:
            pass

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)

    title = ttk.Label(frame, text=APP_NAME, font=("Segoe UI", 13, "bold"))
    title.pack(anchor="w")

    STATUS_VAR = tk.StringVar(value="正在启动服务…")
    status = ttk.Label(frame, textvariable=STATUS_VAR, wraplength=360)
    status.pack(anchor="w", pady=(10, 12))

    url_label = ttk.Label(frame, text=PANEL_URL, foreground="#2563eb")
    url_label.pack(anchor="w", pady=(0, 12))

    buttons = ttk.Frame(frame)
    buttons.pack(anchor="w")
    ttk.Button(buttons, text="打开控制台", command=on_open_clicked).pack(side="left")
    ttk.Button(buttons, text="退出", command=on_window_close).pack(side="left", padx=(8, 0))

    hint = ttk.Label(
        frame,
        text="关闭此窗口或托盘菜单「退出」都会停止后台服务。",
        wraplength=360,
        foreground="#64748b",
    )
    hint.pack(anchor="w", pady=(12, 0))
    return root


def create_tray_image():
    from PIL import Image

    if ICON_PNG.is_file():
        return Image.open(ICON_PNG)
    if ICON_ICO.is_file():
        return Image.open(ICON_ICO)
    return Image.new("RGB", (64, 64), color=(220, 38, 38))


def start_tray_icon(blocking: bool = True) -> bool:
    global TRAY_ICON
    try:
        import pystray
    except ImportError:
        log_line("pystray not installed")
        return False

    image = create_tray_image()
    menu = pystray.Menu(
        pystray.MenuItem("打开控制台", lambda _icon, _item: on_open_clicked()),
        pystray.MenuItem("退出", lambda _icon, _item: quit_application()),
    )
    icon = pystray.Icon(APP_NAME, image, APP_NAME, menu)
    TRAY_ICON = icon
    if blocking:
        icon.run()
    else:
        threading.Thread(target=icon.run, daemon=True).start()
    return True


def bootstrap_panel(*, notify: bool) -> None:
    if panel_reachable():
        set_status("服务已在运行，已打开控制台。")
        open_panel_in_browser()
        if notify and not TK_AVAILABLE:
            show_message("OKX AI Assistant 已在运行。\n\n已打开控制台页面。")
        return
    start_panel_process()
    if wait_for_panel():
        set_status("服务运行中。关闭窗口即可停止服务。")
        open_panel_in_browser()
        if notify and not TK_AVAILABLE:
            show_message(
                "OKX AI Assistant 已启动。\n\n"
                "请使用系统托盘图标打开控制台或退出。\n"
                "关闭浏览器不会停止服务。"
            )
        return
    code = PANEL_PROCESS.poll() if PANEL_PROCESS is not None else None
    set_status(f"服务启动失败（exit={code}）。")
    show_message(
        f"服务启动失败（exit={code}）。\n\n请重新运行 setup_windows_runtime.bat。",
        error=True,
    )


def run_with_taskbar_window() -> int:
    root = build_taskbar_window()
    if not start_tray_icon(blocking=False):
        log_line("tray icon unavailable in taskbar mode")
    threading.Thread(target=bootstrap_panel, kwargs={"notify": False}, daemon=True).start()
    root.mainloop()
    return 0


def run_with_tray_only() -> int:
    threading.Thread(target=bootstrap_panel, kwargs={"notify": True}, daemon=True).start()
    if not start_tray_icon(blocking=True):
        show_message("无法加载托盘组件，请运行 setup_windows_runtime.bat 安装 pystray。", error=True)
        return 1
    return 0


def main() -> int:
    global TK_AVAILABLE
    try:
        if not PANEL_SCRIPT.is_file():
            show_message(f"找不到 {PANEL_SCRIPT.name}", error=True)
            return 1

        if not acquire_instance_lock():
            if panel_reachable():
                open_panel_in_browser()
            show_message("OKX AI Assistant 已在运行。")
            return 0
            # stale lock with dead process falls through after acquire fix above

        start_panel_monitor()

        try:
            import tkinter  # noqa: F401

            TK_AVAILABLE = True
        except ImportError:
            TK_AVAILABLE = False
            log_line("tkinter unavailable, using tray-only mode")

        if TK_AVAILABLE:
            return run_with_taskbar_window()
        return run_with_tray_only()
    except Exception as exc:
        log_line(f"fatal: {exc!r}")
        show_message(f"启动失败：{exc}\n\n详情见 {LOG_FILE}", error=True)
        release_instance_lock()
        return 1


if __name__ == "__main__":
    sys.exit(main())
