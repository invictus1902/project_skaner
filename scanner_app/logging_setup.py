from __future__ import annotations

import logging
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "scanner"
_memory_handler: "MemoryLogHandler | None" = None


class MemoryLogHandler(logging.Handler):
    """Последние записи журнала для окна админки."""

    def __init__(self, capacity: int = 800) -> None:
        super().__init__()
        self.buffer: deque[tuple[int, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            return
        with self._lock:
            self.buffer.append((record.levelno, message))

    def snapshot(self, min_level: int = logging.INFO) -> list[str]:
        with self._lock:
            return [message for level, message in self.buffer if level >= min_level]

    def clear(self) -> None:
        with self._lock:
            self.buffer.clear()


def logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def memory_handler() -> MemoryLogHandler | None:
    return _memory_handler


def setup_logging(log_dir: Path) -> MemoryLogHandler:
    global _memory_handler
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(LOGGER_NAME)
    log.setLevel(logging.DEBUG)
    log.propagate = False
    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = RotatingFileHandler(
        log_dir / "app.log",
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    memory = MemoryLogHandler()
    memory.setLevel(logging.DEBUG)
    memory.setFormatter(formatter)

    log.addHandler(file_handler)
    log.addHandler(memory)
    _memory_handler = memory
    return memory


def clear_log_files(log_dir: Path) -> None:
    log = logging.getLogger(LOGGER_NAME)
    for handler in log.handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.acquire()
            try:
                if handler.stream:
                    handler.stream.close()
                    handler.stream = None
            finally:
                handler.release()
    for path in log_dir.glob("app.log*"):
        try:
            path.unlink()
        except OSError:
            pass
    for handler in log.handlers:
        if isinstance(handler, RotatingFileHandler):
            handler.acquire()
            try:
                handler.stream = handler._open()
            finally:
                handler.release()
    if _memory_handler is not None:
        _memory_handler.clear()
