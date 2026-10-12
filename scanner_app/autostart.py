from __future__ import annotations

import os
import sys
from pathlib import Path

from scanner_app.config import APP_DIR

STARTUP_NAME = "time_scanner.bat"


def startup_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_NAME


def autostart_installed() -> bool:
    return startup_path().exists()


def enable_autostart() -> Path:
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python
    script = APP_DIR / "main.py"
    content = f'@echo off\r\ncd /d "{APP_DIR}"\r\nstart "" "{exe}" "{script}"\r\n'
    path = startup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def disable_autostart() -> None:
    path = startup_path()
    if path.exists():
        path.unlink()
