from __future__ import annotations

import argparse
import socket
import sys
import tkinter as tk
from tkinter import messagebox

from scanner_app import APP_VERSION
from scanner_app.config import LOG_DIR
from scanner_app.kiosk import KioskApp, enable_dpi
from scanner_app.logging_setup import logger, setup_logging


class SingleInstance:
    def __init__(self, port: int = 47331) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind(("127.0.0.1", port))
            self.sock.listen(1)
            self.ok = True
        except OSError:
            self.ok = False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _already_running() -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showinfo("Учёт рабочего времени", "Программа уже запущена.")
    root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Киоск учёта рабочего времени")
    parser.add_argument("--windowed", action="store_true", help="Открыть в окне, а не на весь экран")
    parser.add_argument("--smoke", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    setup_logging(LOG_DIR)

    def _hook(exc_type, value, traceback) -> None:
        logger().critical("Необработанная ошибка", exc_info=(exc_type, value, traceback))

    sys.excepthook = _hook
    enable_dpi()
    lock = SingleInstance()
    if not lock.ok:
        _already_running()
        return 1
    try:
        logger().info("Старт версии %s", APP_VERSION)
        app = KioskApp(windowed=args.windowed or args.smoke, enable_scanner=not args.smoke)
        if args.smoke:
            app.root.update_idletasks()
            app.root.update()
            app.shutdown()
            return 0
        app.run()
        return 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
