from __future__ import annotations

import threading
import time

from scanner_app.logging_setup import logger


class ScanAssembler:
    """Собирает быструю пачку символов от сканера и отсекает обычный набор текста."""

    def __init__(
        self,
        min_length: int,
        max_length: int,
        max_interval_ms: int,
        idle_flush_ms: int,
        terminators: list[str],
        on_burst,
    ) -> None:
        self.min_length = max(1, int(min_length))
        self.max_length = max(self.min_length, int(max_length))
        self.max_interval_ms = int(max_interval_ms)
        self.idle_flush_ms = int(idle_flush_ms)
        self.terminators = set(terminators or [])
        self.on_burst = on_burst
        self._lock = threading.Lock()
        self._chars: list[str] = []
        self._started = 0.0
        self._last = 0.0
        self._span_ms = 0.0

    def push_char(self, char: str, now: float | None = None) -> None:
        if not char or any(ord(ch) < 32 for ch in char):
            return
        moment = time.monotonic() if now is None else now
        finished = None
        overflow = None
        with self._lock:
            if self._chars and (moment - self._last) * 1000 > self.max_interval_ms:
                finished = self._snapshot()
            if not self._chars:
                self._started = moment
            self._chars.append(char)
            self._last = moment
            self._span_ms = max(0.0, (self._last - self._started) * 1000)
            if len(self._chars) >= self.max_length:
                overflow = self._snapshot()
        self._dispatch(finished, terminated=False)
        self._dispatch(overflow, terminated=False)

    def push_terminator(self, name: str, now: float | None = None) -> None:
        del now
        if name not in self.terminators:
            return
        with self._lock:
            payload = self._snapshot()
        self._dispatch(payload, terminated=True)

    def tick(self, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            idle = bool(self._chars) and (moment - self._last) * 1000 >= self.idle_flush_ms
            payload = self._snapshot() if idle else None
        self._dispatch(payload, terminated=False)

    def _reset(self) -> None:
        self._chars.clear()
        self._started = 0.0
        self._last = 0.0
        self._span_ms = 0.0

    def _snapshot(self):
        """Забрать накопленный код. Вызывать только под lock."""
        if not self._chars:
            return None
        payload = ("".join(self._chars).strip(), len(self._chars), self._span_ms)
        self._reset()
        return payload

    def _dispatch(self, payload, terminated: bool) -> None:
        if not payload:
            return
        text, count, elapsed = payload
        if count < self.min_length or not text:
            return
        # Скорость считаем до последнего символа, без паузы ожидания Enter.
        limit = self.max_interval_ms * max(count - 1, 1) + 80
        fast = elapsed <= limit
        try:
            self.on_burst(text, elapsed, terminated, fast)
        except Exception:
            logger().exception("Ошибка обработчика сканера")


def accepts_typing(widget) -> bool:
    """Поле, в которое человек вписывает текст. Пока оно в фокусе, сканер молчит."""
    try:
        return str(widget.winfo_class()) in {"Entry", "TEntry", "Text"}
    except Exception:
        return False


class ScannerController:
    def __init__(self, settings: dict, on_burst) -> None:
        self.settings = settings
        self.on_burst = on_burst
        self.hook_ok = False
        self.paused = False
        self._stop = threading.Event()
        self._listener = None
        self._tick_thread: threading.Thread | None = None
        self._serial_thread: threading.Thread | None = None
        self._assembler: ScanAssembler | None = None
        self._asm_lock = threading.Lock()

    def start(self) -> None:
        self._stop.clear()
        self.hook_ok = False
        self._rebuild()
        scanner = self.settings.get("scanner") or {}
        if scanner.get("keyboard_enabled", True):
            self._start_keyboard()
        self._tick_thread = threading.Thread(target=self._tick_loop, name="scan-tick", daemon=True)
        self._tick_thread.start()
        if scanner.get("serial_enabled"):
            self._serial_thread = threading.Thread(target=self._serial_loop, name="scan-serial", daemon=True)
            self._serial_thread.start()
        logger().info(
            "Сканер запущен: клавиатура=%s, COM=%s",
            "да" if scanner.get("keyboard_enabled", True) else "нет",
            scanner.get("serial_port") if scanner.get("serial_enabled") else "выключен",
        )

    def stop(self) -> None:
        self._stop.set()
        listener = self._listener
        self._listener = None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                logger().exception("Не удалось остановить перехват клавиатуры")
        if self._tick_thread and self._tick_thread.is_alive():
            self._tick_thread.join(timeout=1)
        if self._serial_thread and self._serial_thread.is_alive():
            self._serial_thread.join(timeout=1)
        self._tick_thread = None
        self._serial_thread = None

    def update(self, settings: dict) -> None:
        self.stop()
        self.settings = settings
        self._stop = threading.Event()
        self.start()

    def push_key(self, char: str = "", terminator: str = "") -> None:
        """Одна клавиша перехвата. В поле ввода она не становится отметкой."""
        if self.paused:
            return
        if terminator:
            self._assembler_call("push_terminator", terminator)
            return
        if char:
            self._assembler_call("push_char", char)

    def feed_text(self, text: str, trusted: bool = False) -> None:
        """Прямая подача кода: имитация в админке или строка из COM-порта."""
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        try:
            self.on_burst(cleaned, 0.0, True, True)
        except Exception:
            logger().exception("Ошибка прямой подачи кода")

    def _rebuild(self) -> None:
        scanner = self.settings.get("scanner") or {}
        assembler = ScanAssembler(
            min_length=int(scanner.get("min_length") or 3),
            max_length=int(scanner.get("max_length") or 512),
            max_interval_ms=int(scanner.get("max_interval_ms") or 80),
            idle_flush_ms=int(scanner.get("idle_flush_ms") or 100),
            terminators=list(scanner.get("terminators") or ["enter"]),
            on_burst=self.on_burst,
        )
        with self._asm_lock:
            self._assembler = assembler

    def _assembler_call(self, method: str, *args) -> None:
        with self._asm_lock:
            assembler = self._assembler
        if assembler is None:
            return
        getattr(assembler, method)(*args)

    def _start_keyboard(self) -> None:
        try:
            from pynput import keyboard
        except Exception as exc:
            self.hook_ok = False
            logger().error("Перехват клавиатуры недоступен: %s", exc)
            return

        def on_press(key):
            try:
                char = getattr(key, "char", None)
                if char in ("\r", "\n"):
                    self.push_key(terminator="enter")
                    return
                if char:
                    self.push_key(char=char)
                    return
                if key == keyboard.Key.enter:
                    self.push_key(terminator="enter")
                elif key == keyboard.Key.tab:
                    self.push_key(terminator="tab")
            except Exception:
                logger().exception("Ошибка чтения клавиши сканера")

        try:
            listener = keyboard.Listener(on_press=on_press)
            listener.start()
            self._listener = listener
            self.hook_ok = True
        except Exception as exc:
            self.hook_ok = False
            logger().error("Не удалось начать приём со сканера: %s", exc)

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            self._assembler_call("tick")
            self._stop.wait(0.05)

    def _serial_loop(self) -> None:
        scanner = self.settings.get("scanner") or {}
        port = str(scanner.get("serial_port") or "").strip()
        baud = int(scanner.get("baudrate") or 9600)
        max_length = int(scanner.get("max_length") or 512)
        while not self._stop.is_set():
            try:
                import serial
            except Exception as exc:
                logger().error("Модуль COM-порта не установлен: %s", exc)
                return
            try:
                handle = serial.Serial(port, baudrate=baud, timeout=0.3)
            except Exception as exc:
                logger().error("Не удалось открыть %s: %s", port, exc)
                self._stop.wait(3)
                continue
            buffer = bytearray()
            logger().info("COM-порт %s открыт", port)
            try:
                while not self._stop.is_set():
                    chunk = handle.read(256)
                    if not chunk:
                        continue
                    buffer.extend(chunk)
                    for line in _pop_lines(buffer):
                        text = line.decode("utf-8", errors="replace").strip()
                        if text:
                            self.feed_text(text, trusted=True)
                    if len(buffer) >= max_length:
                        text = buffer.decode("utf-8", errors="replace").strip()
                        buffer.clear()
                        if text:
                            self.feed_text(text, trusted=True)
            except Exception as exc:
                logger().error("Сбой COM-порта %s: %s", port, exc)
            finally:
                try:
                    handle.close()
                except Exception:
                    pass
            self._stop.wait(2)


def _pop_lines(buffer: bytearray) -> list[bytes]:
    lines = []
    while True:
        indexes = [pos for pos in (buffer.find(b"\r"), buffer.find(b"\n")) if pos >= 0]
        if not indexes:
            break
        index = min(indexes)
        part = bytes(buffer[:index])
        drop = index + 1
        if buffer[index : index + 1] == b"\r" and buffer[drop : drop + 1] == b"\n":
            drop += 1
        del buffer[:drop]
        lines.append(part)
    return lines


def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    try:
        return [item.device for item in list_ports.comports()]
    except Exception:
        logger().exception("Не удалось получить список COM-портов")
        return []


def probe_serial_port(port: str, baudrate: int) -> tuple[bool, str]:
    port = str(port or "").strip()
    if not port:
        return False, "Порт не указан"
    try:
        import serial
    except Exception as exc:
        return False, f"Модуль COM-порта не установлен: {exc}"
    try:
        handle = serial.Serial(port, baudrate=int(baudrate), timeout=0.2)
    except Exception as exc:
        return False, str(exc)
    try:
        handle.close()
    except Exception:
        pass
    return True, f"Порт {port} открывается"
