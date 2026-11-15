"""Native desktop window for the Sentinel dashboard.

The window shows the same dashboard the local servers already serve, so the desktop app
needs no build step and never drifts from the web UI.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import webview

API_URL = "http://127.0.0.1:8000"
DASHBOARD_URL = "http://127.0.0.1:8001/"
STARTUP_TASKS = ("Server Health Monitor API", "Server Health Monitor Dashboard")
STARTUP_TIMEOUT_SECONDS = 45
WINDOW_TITLE = "Sentinel"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_PATH = PROJECT_ROOT / "assets" / "sentinel.ico"

ERROR_PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sentinel</title>
<style>
  body { margin: 0; background: #0d1415; color: #d5dfdd; font-family: "Segoe UI", system-ui, sans-serif;
         display: grid; place-items: center; height: 100vh; }
  main { max-width: 460px; padding: 32px; text-align: center; }
  h1 { font-size: 20px; margin: 0 0 12px; color: #66d8c6; }
  p { font-size: 14px; line-height: 1.6; margin: 0; color: #aab8b7; }
  code { background: #141d1e; padding: 2px 6px; border-radius: 4px; color: #c1e879; }
</style>
</head>
<body>
<main>
  <h1>Sentinel could not start</h1>
  <p>The local services are not answering on <code>127.0.0.1:8000</code> and <code>127.0.0.1:8001</code>.
  Start MongoDB and re-run the installer, then open Sentinel again.</p>
</main>
</body>
</html>
"""


def is_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_scheduled_tasks() -> None:
    for task_name in STARTUP_TASKS:
        subprocess.run(
            ["schtasks", "/run", "/tn", task_name],
            check=False,
            capture_output=True,
        )


def wait_until_ready() -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if is_reachable(f"{API_URL}/health") and is_reachable(DASHBOARD_URL):
            return True
        time.sleep(1)
    return False


def apply_window_icon(title: str, icon_path: Path) -> None:
    """Sets the title bar and taskbar icon of the native window.

    pywebview has no icon option on Windows, so the icon is loaded with the shell API
    and pushed to the window once it exists.
    """
    if sys.platform != "win32" or not icon_path.is_file():
        return

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    image_icon = 1
    load_from_file = 0x0010
    icon_big = 1
    icon_small = 0
    set_icon = 0x0080

    handle = user32.LoadImageW(None, str(icon_path), image_icon, 0, 0, load_from_file)
    if not handle:
        return
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            window = user32.FindWindowW(None, title)
            if window:
                user32.SendMessageW(window, set_icon, icon_big, handle)
                user32.SendMessageW(window, set_icon, icon_small, handle)
                return
            time.sleep(0.25)
    finally:
        shell32.DestroyIcon(handle)


def main() -> int:
    if not (is_reachable(f"{API_URL}/health") and is_reachable(DASHBOARD_URL)):
        start_scheduled_tasks()
        if not wait_until_ready():
            webview.create_window(WINDOW_TITLE, html=ERROR_PAGE, width=520, height=260)
            webview.start()
            return 1

    webview.create_window(
        WINDOW_TITLE,
        f"{DASHBOARD_URL}?desktop=1",
        width=1440,
        height=900,
        min_size=(1024, 700),
    )
    icon_thread = threading.Thread(
        target=apply_window_icon, args=(WINDOW_TITLE, ICON_PATH), daemon=True
    )
    icon_thread.start()
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
