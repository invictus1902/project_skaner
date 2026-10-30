from __future__ import annotations

import ctypes
import math
import os
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta

from scanner_app import APP_VERSION
from scanner_app.api import ApiClient
from scanner_app.config import DB_PATH, load_settings, save_settings
from scanner_app.db import Database
from scanner_app.logging_setup import logger
from scanner_app.logic import format_clock_date, format_history_time, parse_iso
from scanner_app.scanner import ScannerController, accepts_typing
from scanner_app.ui_helpers import arm_after
from scanner_app.service import AttendanceService
from scanner_app.fonts import faces
from scanner_app.shifts import is_open
from scanner_app.speech import Speaker
from scanner_app.theme import theme_by_name

EVENT_TEXT = {
    "in": "Приход",
    "out": "Уход",
    "repeat": "Повтор",
    "unknown": "Не найден",
    "disabled": "Отключён",
}


def history_mark_color(theme: dict, event_type: str, status: str) -> str:
    """Цвет точки в ленте. Повтор приглушён и не выглядит как новый приход."""
    if event_type in ("unknown", "disabled") or status == "rejected":
        return theme["error_text"]
    if event_type == "repeat":
        return theme["muted"]
    if event_type == "in":
        return theme["in_text"]
    return theme["out_text"]
STATUS_TEXT = {
    "pending": "в очереди",
    "sending": "отправляется",
    "sent": "отправлено",
    "rejected": "отклонено",
}
FONT_BASE = {
    "clock": 34,
    "title": 18,
    "date": 12,
    "status": 40,
    "name": 40,
    "when": 20,
    "detail": 16,
    "history": 13,
    "heading": 12,
    "kicker": 11,
    "caption": 22,
    "stat": 48,
    "small": 12,
    "footer": 15,
}
SERIF_FONTS = {"title", "status", "name"}
BOLD_FONTS = {"status", "name"}
MONO_FONTS = {"clock", "date", "kicker", "when", "stat"}


def enable_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _mix(a: str, b: str, t: float) -> str:
    """Смешать два цвета #rrggbb. t = 0 даёт первый, t = 1 — второй."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    color_a = a.lstrip("#")
    color_b = b.lstrip("#")
    channels = []
    for index in (0, 2, 4):
        left = int(color_a[index : index + 2], 16)
        right = int(color_b[index : index + 2], 16)
        channels.append(int(left + (right - left) * t))
    return "#{:02x}{:02x}{:02x}".format(channels[0], channels[1], channels[2])


def _same_color(a: str, b: str) -> bool:
    return str(a).strip().lower() == str(b).strip().lower()


def _ease(step: int, steps: int) -> float:
    """Плавный разгон: в начале медленно, к концу точно 1."""
    if step >= steps:
        return 1.0
    return 1 - (1 - step / float(steps)) ** 3


def _wrap_lines(text: str, font, width: int) -> list:
    words = str(text or "").split()
    if not words:
        return [""]
    lines = []
    current = ""
    limit = max(40, int(width))
    for word in words:
        trial = word if not current else current + " " + word
        if not current or font.measure(trial) <= limit:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


_LAYOUT_HINT = (
    "Русская раскладка: буквы в QR могут считаться неверно. Переключите на английскую — Alt+Shift."
)
_LAYOUT_HINT_MS = 6000
_RU_LAYOUT_LETTERS = frozenset("йцукенгшщзхъфывапролджэячсмитьбюё")


def history_day_title(moment: datetime, today: datetime) -> str:
    """Заголовок группы в ленте: сегодня, вчера или дата."""
    if moment.date() == today.date():
        return "Сегодня"
    if moment.date() == today.date() - timedelta(days=1):
        return "Вчера"
    return moment.strftime("%d.%m.%Y")


def looks_like_russian_layout(text: str) -> bool:
    """Код из букв русской клавиатуры: латиница QR прочиталась как кириллица."""
    letters = [char for char in str(text or "").casefold() if char.isalpha()]
    if not letters:
        return False
    russian = sum(char in _RU_LAYOUT_LETTERS for char in letters)
    return russian > 0 and russian * 2 >= len(letters)


class KioskApp:
    def __init__(self, windowed: bool = False, enable_scanner: bool = True) -> None:
        self.settings = load_settings()
        self.db = Database(DB_PATH)
        self.api = ApiClient(self.settings)
        # Справочник качается вместе со сборкой окна, а не после первого скана.
        self._health_lock = threading.Lock()
        self._pending_health = None
        self._health_busy = True
        threading.Thread(target=self._prefetch_health, name="health", daemon=True).start()
        self.speaker = Speaker()
        self._alive = True
        self._closing = False
        self._force_windowed = windowed
        self._generation = 0
        self._scale = 1.0
        self._state = "idle"
        self._link_state = "checking"
        self._shown_history = 0
        self._debug_subs: list = []
        self._result_subs: list = []
        self._admin = None
        self._login = None
        self._shifts = None
        self._hold_fullscreen = False
        self._window_placed = False
        self._date_text = ""
        self._banner_text = None
        self._layout_token = 0
        self._layout_after = None
        self._hold_after = None
        self._floor_key = None
        self._pending_count = 0
        self._saved_at_work = None
        self._saved_at_work_tick = -100
        self._floor_tick = 0
        self._configured_size = (0, 0)
        self._history_key = None
        self._anim_token = 0
        self._pulse_phase = 0.0
        self._status_text = ""
        self._button_hot = False
        self._pill_width = None
        self._pill_color = None
        self._pill_fg = None
        self._pill_anim = None
        self._pill_dirty = False
        self._glow_boost = 0.0
        self._status_animating = False
        self._fitting = False
        self._drawing_action = False
        self._banner_fit = False
        self._panel_stamp = None
        self._stack_key = None
        self._metrics_key = None
        self._metrics_value = None
        self._pill_glow: list = []
        self._pill_ring: list = []
        self._pill_body: list = []
        self._pill_text = None
        self._pill_text_key = None
        self._reveal_token = 0
        self._reveal_key = None
        self._history_motion = None
        self._history_fade_token = 0
        self._button_pressed = False
        self._button_press_token = 0
        self._ui_queue: list = []
        self._ui_queued = False
        self._history_token = None
        self._pending_token = None
        self._floor_busy = False
        self._pulse_job = None
        self._wash_token = 0
        self._wash_after = None
        self._wash_bg = None

        self.root = tk.Tk()
        self.root.title(self.settings["ui"]["title"])
        self.root.minsize(800, 560)
        self.root.report_callback_exception = self._report_error
        self.serif, self.serif_bold, self.sans = faces(self.root)
        self.mono = self._mono_family()
        self.fonts = {
            name: tkfont.Font(
                root=self.root,
                family=self._font_family(name),
                size=size,
                weight="bold" if name in BOLD_FONTS and self.serif_bold == self.serif else "normal",
            )
            for name, size in FONT_BASE.items()
        }
        self._build()
        self.service = AttendanceService(self.settings, self.db, self.api, self.speaker, self)
        self.scanner = ScannerController(self.settings, self.service.on_burst)
        self.root.bind_all("<FocusIn>", self._sync_scan_pause, add="+")
        self._scanner_wanted = enable_scanner
        self.service.start()
        if enable_scanner:
            self.scanner.start()
        self._install_key_fallback()
        self._apply_colors()
        self._apply_fonts()
        self._apply_window_mode()
        self._render_idle()
        self._render_history()
        self._request_pending()
        self._clock_loop()
        self._health_loop()
        self.root.after(2500, self._paint_voice_warning)
        self._ensure_pulse()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<F12>", lambda _event: self.open_admin())
        logger().info("Киоск запущен, версия %s", APP_VERSION)

    def run(self) -> None:
        self.root.mainloop()

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._alive = False
        self._layout_after = arm_after(self.root, self._layout_after, 0, None)
        self._hold_after = arm_after(self.root, self._hold_after, 0, None)
        self._wash_token += 1
        self._wash_after = arm_after(self.root, self._wash_after, 0, None)
        logger().info("Киоск останавливается")
        try:
            self.scanner.stop()
        except Exception:
            logger().exception("Ошибка остановки сканера")
        try:
            self.service.stop()
        except Exception:
            logger().exception("Ошибка остановки службы")
        try:
            self.speaker.stop()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _later(self, callback) -> None:
        if not self._alive:
            return
        self._ui_queue.append(callback)
        if self._ui_queued:
            return
        self._ui_queued = True
        try:
            self.root.after(0, self._flush_ui)
        except (tk.TclError, RuntimeError):
            self._ui_queued = False
            self._ui_queue.clear()

    def _flush_ui(self) -> None:
        self._ui_queued = False
        batch = self._ui_queue
        self._ui_queue = []
        for callback in batch:
            if not self._alive:
                return
            try:
                callback()
            except Exception:
                logger().exception("Ошибка обновления экрана")

    def show(self, view: dict) -> None:
        self._later(lambda payload=dict(view): self._render(payload))

    def refresh_history(self) -> None:
        if not self._alive:
            return
        if threading.current_thread() is threading.main_thread():
            self._request_history()
            return
        token = object()
        self._history_token = token
        limit = self._shown_history or 8
        try:
            rows = self.db.recent_events(limit)
        except Exception:
            logger().exception("Не удалось прочитать ленту")
            return
        self._later(lambda rows=rows, token=token: self._deliver_history(rows, token))

    def set_pending(self, count: int) -> None:
        token = object()
        self._pending_token = token
        self._later(lambda value=count, token=token: self._deliver_pending(value, token))

    def debug(self, text: str) -> None:
        def deliver(message=text) -> None:
            for callback in list(self._debug_subs):
                try:
                    callback(message)
                except Exception:
                    logger().exception("Ошибка подписчика отладки")

        self._later(deliver)

    def subscribe_debug(self, callback) -> None:
        self._debug_subs.append(callback)

    def unsubscribe_debug(self, callback) -> None:
        if callback in self._debug_subs:
            self._debug_subs.remove(callback)

    def subscribe_result(self, callback) -> None:
        self._result_subs.append(callback)

    def unsubscribe_result(self, callback) -> None:
        if callback in self._result_subs:
            self._result_subs.remove(callback)

    def apply_settings(self, new_settings: dict, restart_scanner: bool = True) -> None:
        self.settings = new_settings
        save_settings(new_settings)
        self.api.update(new_settings)
        self.service.update(new_settings)
        if restart_scanner:
            self.scanner.update(new_settings)
            self._install_key_fallback()
        self.root.title(new_settings["ui"]["title"])
        self.title_label.configure(text=new_settings["ui"]["title"])
        self._apply_colors()
        self._apply_fonts()
        self._apply_window_mode()
        self._paint_panel(self._state)
        self._render_history()
        self._request_pending()
        self._kick_health()
        logger().info("Настройки сохранены")

    def open_admin(self) -> None:
        from scanner_app.admin import AdminWindow, LoginDialog

        from scanner_app.ui_helpers import present_window, widget_alive

        if widget_alive(self._admin):
            present_window(self._admin)
            return
        if widget_alive(self._login):
            try:
                self._login.deiconify()
                self._login.lift()
                self._login.focus_force()
            except tk.TclError:
                pass
            return

        def opened() -> None:
            self._login = None
            self._hold_fullscreen = True
            self._apply_window_mode()
            self._admin = AdminWindow(self.root, self)
            self._hold_fullscreen = False
            self._admin.protocol("WM_DELETE_WINDOW", self._close_admin)

        self._login = LoginDialog(self.root, self.settings, opened)

    def open_shifts(self) -> None:
        from scanner_app.shifts_ui import ShiftsWindow
        from scanner_app.ui_helpers import present_window, widget_alive

        if widget_alive(self._shifts):
            present_window(self._shifts)
            return
        self._hold_fullscreen = True
        self._apply_window_mode()
        self._shifts = ShiftsWindow(self.root, self)
        self._hold_fullscreen = False

    def _close_admin(self) -> None:
        admin = self._admin
        if admin is None:
            return
        try:
            admin._close()
        except tk.TclError:
            self._admin = None

    def _build(self) -> None:
        self.header = tk.Frame(self.root)
        self.brand = tk.Frame(self.header)
        self.brand_mark = tk.Canvas(self.brand, width=14, height=14, highlightthickness=0, bd=0)
        self.title_label = tk.Label(self.brand, text=self.settings["ui"]["title"], anchor="w", font=self.fonts["title"])
        self.brand_mark.pack(side="left", padx=(0, 12))
        self.title_label.pack(side="left", anchor="w")
        self.status_row = tk.Frame(self.header)
        self.dot = tk.Canvas(self.status_row, width=14, height=14, highlightthickness=0, bd=0)
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, outline="")
        self.link_label = tk.Label(self.status_row, anchor="w", font=self.fonts["small"])
        self.device_label = tk.Label(self.status_row, text="", anchor="w", font=self.fonts["small"])
        self.device_label.pack(side="left", padx=(0, 16))
        self.dot.pack(side="left")
        self.link_label.pack(side="left", padx=(8, 0))
        self.brand.pack(side="left", padx=(28, 12), pady=16)
        self.status_row.pack(side="right", padx=(12, 28), pady=16)
        self.header_rule = tk.Frame(self.root, height=1, bd=0, highlightthickness=0)
        self.header_rule.pack_propagate(False)

        self.banner = tk.Frame(self.root)
        self.banner_canvas = tk.Canvas(self.banner, height=48, highlightthickness=0, bd=0)
        self.banner_canvas.pack(fill="x", padx=32, pady=(10, 0))
        self.banner_canvas.bind("<Configure>", self._draw_banner)

        self.body = tk.Frame(self.root)
        self.center_wrap = self.body
        self.rail = tk.Frame(self.body, width=292, bd=0, highlightthickness=0)
        self.rail.pack_propagate(False)
        self.history = self.rail
        self.rail_edge = tk.Frame(self.body, width=1, bd=0, highlightthickness=0)
        self.rail_edge.pack_propagate(False)
        self.stage = tk.Frame(self.body, bd=0, highlightthickness=0)
        self.center = self.stage
        self.clock_label = tk.Label(self.rail, anchor="w", font=self.fonts["clock"])
        self.date_label = tk.Label(self.rail, anchor="w", font=self.fonts["date"])
        self.rail_rule = tk.Frame(self.rail, height=1, bd=0, highlightthickness=0)
        self.rail_rule.pack_propagate(False)
        self.history_title = tk.Label(self.rail, text="Последние отметки", anchor="w", font=self.fonts["heading"])
        self.history_rows = tk.Frame(self.rail, bd=0, highlightthickness=0)
        self.clock_label.pack(anchor="w", padx=22, pady=(22, 0))
        self.date_label.pack(anchor="w", padx=22, pady=(2, 0))
        self.rail_rule.pack(fill="x", padx=22, pady=(16, 12))
        self.history_title.pack(anchor="w", padx=22, pady=(0, 10))
        self.history_rows.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.link_banner = tk.Frame(self.body, bd=0, highlightthickness=0)
        self.link_banner_label = tk.Label(
            self.link_banner,
            text="Нет связи с сервером",
            anchor="center",
            font=self.fonts["caption"],
            padx=16,
            pady=12,
        )
        self.link_banner_label.pack(fill="x")
        self.rail.pack(side="left", fill="y")
        self.rail_edge.pack(side="left", fill="y")
        self.stage.pack(side="left", fill="both", expand=True)

        self.stage_top = tk.Frame(self.stage, bd=0, highlightthickness=0)
        self.cluster = tk.Frame(self.stage, bd=0, highlightthickness=0)
        self.stage_bottom = tk.Frame(self.stage, bd=0, highlightthickness=0)
        self.stage_top.pack(fill="both", expand=True)
        self.cluster.pack(fill="x")
        self.stage_bottom.pack(fill="both", expand=True)
        self.copy = tk.Frame(self.cluster, bd=0, highlightthickness=0)
        self.copy.pack(fill="x", padx=48, pady=(0, 4))
        self.copy_top = tk.Frame(self.copy, height=0, bd=0, highlightthickness=0)
        self.copy_bottom = tk.Frame(self.copy, height=0, bd=0, highlightthickness=0)
        self.copy_top.pack_propagate(False)
        self.copy_bottom.pack_propagate(False)
        self.copy_top.pack(fill="x")
        self.copy_bottom.pack(fill="x")
        self.signal = tk.Canvas(self.copy, height=120, highlightthickness=0, bd=0)
        self.signal_body = self.signal
        self.signal.pack(fill="x", pady=(0, 14), before=self.copy_bottom)
        self.signal.bind("<Configure>", self._on_signal_configure)
        self.copy_kicker = tk.Label(self.copy, font=self.fonts["caption"], anchor="center", justify="center")
        self.name_label = tk.Label(self.copy, font=self.fonts["name"], anchor="center", justify="center")
        self.when_label = tk.Label(self.copy, font=self.fonts["when"], anchor="center", justify="center")
        self.length_label = tk.Label(self.copy, font=self.fonts["when"], anchor="center", justify="center")
        self.badge_label = tk.Label(self.copy, font=self.fonts["detail"], anchor="center", justify="center")
        self.detail_label = tk.Label(
            self.copy, font=self.fonts["detail"], anchor="center", justify="center", wraplength=640
        )
        self.cards = tk.Frame(self.cluster, bd=0, highlightthickness=0)
        self.cards.pack(anchor="center", pady=(22, 8))
        self.work_card, self.work_rule, self.floor_work_caption, self.floor_work_value = self._make_stat_card(
            "На работе", "—"
        )
        self.queue_card, self.queue_rule, self.floor_queue_caption, self.floor_queue_value = self._make_stat_card(
            "В очереди", "0"
        )
        self.work_card.pack(side="left", padx=(0, 18))
        self.queue_card.pack(side="left")

        self.footer = tk.Frame(self.root)
        self.action_row = tk.Frame(self.footer, bd=0, highlightthickness=0)
        self.meta_row = tk.Frame(self.footer)
        self.manual_var = tk.StringVar()
        self.manual_label = tk.Label(self.action_row, text="Код бейджа", anchor="w", font=self.fonts["small"])
        self.manual_entry = tk.Entry(
            self.action_row,
            textvariable=self.manual_var,
            font=self.fonts["footer"],
            relief="flat",
            highlightthickness=1,
        )
        self.manual_button = tk.Canvas(
            self.action_row,
            width=176,
            height=44,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
        )
        self.manual_button.bind("<Button-1>", self._button_press)
        self.manual_button.bind("<Enter>", self._button_enter)
        self.manual_button.bind("<Leave>", self._button_leave)
        self.manual_button.bind("<Configure>", lambda _event: self._draw_action())
        self.pending_label = tk.Label(self.meta_row, anchor="w", font=self.fonts["small"])
        self.voice_label = tk.Label(self.meta_row, anchor="w", font=self.fonts["small"])
        self.version_label = tk.Label(self.meta_row, text=f"v{APP_VERSION}", anchor="e", font=self.fonts["small"])
        self.admin_button = tk.Button(
            self.meta_row,
            text="Настройки",
            font=self.fonts["small"],
            relief="flat",
            padx=16,
            pady=4,
            cursor="hand2",
            command=self.open_admin,
        )
        self.action_row.columnconfigure(1, weight=1)
        self.manual_label.grid(row=0, column=0, sticky="w", padx=(16, 10), pady=8)
        self.manual_entry.grid(row=0, column=1, sticky="ew", pady=8, ipady=6, ipadx=8)
        self.manual_button.grid(row=0, column=2, padx=(12, 18), pady=10)
        self.pending_label.pack(side="left", padx=(16, 12), pady=(2, 6))
        self.voice_label.pack(side="left", padx=(0, 12), pady=(2, 6))
        self.admin_button.pack(side="right", padx=(8, 16), pady=(2, 6))
        self.version_label.pack(side="right", padx=(8, 4), pady=(2, 6))
        self.action_row.pack(fill="x", padx=32, pady=(2, 0))
        self.meta_row.pack(fill="x", padx=16)
        self.manual_entry.bind("<Return>", self._submit_manual)
        self.manual_entry.bind("<FocusIn>", self._manual_focus)
        self.manual_entry.bind("<FocusOut>", self._manual_blur)

        self.footer_rule = tk.Frame(self.root, height=1, bd=0, highlightthickness=0)
        self.footer_rule.pack_propagate(False)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(3, weight=1)
        self.header.grid(row=0, column=0, sticky="ew")
        self.header_rule.grid(row=1, column=0, sticky="ew")
        self.banner.grid(row=2, column=0, sticky="ew")
        self.banner.grid_remove()
        self.body.grid(row=3, column=0, sticky="nsew")
        self.footer_rule.grid(row=4, column=0, sticky="ew")
        self.footer.grid(row=5, column=0, sticky="ew")
        self.root.bind("<Configure>", self._on_configure)

    def _make_stat_card(self, caption: str, value: str):
        card = tk.Frame(self.cards, width=210, height=112, bd=0, highlightthickness=1)
        card.pack_propagate(False)
        rule = tk.Frame(card, height=4, bd=0, highlightthickness=0)
        rule.pack(side="top", fill="x")
        rule.pack_propagate(False)
        caption_label = tk.Label(card, text=caption, anchor="center", font=self.fonts["detail"])
        value_label = tk.Label(card, text=value, anchor="center", font=self.fonts["stat"])
        caption_label.pack(fill="x", pady=(16, 0))
        value_label.pack(fill="x")
        return card, rule, caption_label, value_label

    def _manual_focus(self, _event=None) -> None:
        scanner = getattr(self, "scanner", None)
        if scanner is not None:
            scanner.paused = True

    def _manual_blur(self, _event=None) -> None:
        scanner = getattr(self, "scanner", None)
        if scanner is not None:
            scanner.paused = False

    def _submit_manual(self, _event=None):
        code = self.manual_var.get().strip()
        if not code:
            self._render(
                {
                    "state": "error",
                    "title": "Введите код",
                    "name": "",
                    "when": "",
                    "detail": "Напишите код бейджа: цифры, буквы или символы.",
                    "badge": "",
                }
            )
            return "break"
        self.manual_var.set("")
        self.service.submit_manual(code)
        try:
            self.root.focus_set()
        except tk.TclError:
            pass
        return "break"

    def _render_idle(self) -> None:
        self._render(
            {
                "state": "idle",
                "title": "Поднесите бейдж",
                "name": "",
                "when": "",
                "detail": "Сканер готов. Первый скан — приход, второй — уход. Если сканер не работает, введите код внизу.",
                "badge": "",
            },
            hold=False,
        )

    def _render(self, view: dict, hold: bool = True) -> None:
        self._generation += 1
        generation = self._generation
        self._state = view.get("state") or "idle"
        self._status_text = view.get("title") or ""
        self._paint_panel(self._state)
        self.name_label.configure(text=view.get("name") or "")
        self.when_label.configure(text=view.get("when") or "")
        badge = view.get("badge") or ""
        if self.settings["ui"].get("show_badge") and badge:
            self.badge_label.configure(text=f"Бейдж {badge}")
        else:
            self.badge_label.configure(text="")
        self.detail_label.configure(text=view.get("detail") or "")
        self.length_label.configure(text=view.get("length") or "")
        self._stack_lines()
        self._start_wash()
        self._play_status()
        self._start_reveal()
        self._consider_layout_banner(view)
        for callback in list(self._result_subs):
            try:
                callback(view)
            except Exception:
                logger().exception("Ошибка подписчика результата")
        if hold and self._state not in ("idle", "progress"):
            delay = int(self.settings["attendance"].get("result_hold_sec") or 8) * 1000
            self._hold_after = arm_after(
                self.root,
                self._hold_after,
                delay,
                lambda token=generation: self._return_idle(token),
            )
        self._ensure_pulse()

    def _stack_lines(self) -> None:
        name = str(self.name_label.cget("text") or "").strip()
        kicker = "Сотрудник" if name else "Сканер"
        shown = tuple(
            widget
            for widget in (self.name_label, self.when_label, self.length_label, self.badge_label, self.detail_label)
            if str(widget.cget("text") or "").strip()
        )
        key = (kicker, shown)
        if key == self._stack_key:
            return
        self._stack_key = key
        self.copy_kicker.configure(text=kicker)
        for widget in (self.copy_kicker, self.name_label, self.when_label, self.length_label, self.badge_label, self.detail_label):
            widget.pack_forget()
        self.copy_kicker.pack(anchor="center", fill="x", pady=(12, 4), before=self.copy_bottom)
        previous = self.copy_kicker
        for widget in shown:
            if widget is self.detail_label:
                space = (14, 0)
            elif widget is self.length_label:
                space = (8, 0)
            else:
                space = (4, 0)
            widget.pack(anchor="center", fill="x", pady=space, after=previous)
            previous = widget

    def _return_idle(self, generation: int) -> None:
        if self._alive and generation == self._generation:
            self._render_idle()

    def _start_reveal(self) -> None:
        """Проявить строки результата из фона. В конце цвет темы, без промежуточного оттенка."""
        theme = self._theme()
        page = theme["bg"]
        targets = (
            (self.copy_kicker, theme["text"]),
            (self.name_label, theme["text"]),
            (self.when_label, theme["text"]),
            (self.length_label, theme["text"]),
            (self.badge_label, theme["muted"]),
            (self.detail_label, theme["muted"]),
        )
        key = tuple((str(widget.cget("text") or ""), color) for widget, color in targets)
        if key == self._reveal_key:
            return
        self._reveal_key = key
        self._reveal_token += 1
        token = self._reveal_token
        pairs = []
        for widget, color in targets:
            if not str(widget.cget("text") or "").strip():
                widget.configure(fg=color)
                continue
            widget.configure(fg=page)
            pairs.append((widget, color))
        if pairs:
            self._reveal_step(token, pairs, page, 0)

    def _reveal_step(self, token: int, pairs, page: str, step: int) -> None:
        if not self._alive or token != self._reveal_token:
            return
        steps = 9
        done = step >= steps
        t = _ease(step, steps)
        try:
            for widget, color in pairs:
                widget.configure(fg=color if done else _mix(page, color, t))
        except tk.TclError:
            return
        if done:
            return
        try:
            self.root.after(
                16,
                lambda token=token, pairs=pairs, page=page, step=step + 1: self._reveal_step(token, pairs, page, step),
            )
        except tk.TclError:
            pass

    def _mono_family(self) -> str:
        available = set(tkfont.families(self.root))
        for name in ("Cascadia Mono", "Consolas", "Lucida Console", "Courier New"):
            if name in available:
                return name
        return self.sans

    def _capsule(self, canvas, x1: float, y1: float, x2: float, y2: float, fill: str, tag: str, items=None) -> list:
        """Нарисовать капсулу. Повторный вызов двигает уже созданные фигуры, а не собирает их заново."""
        items = list(items or [])
        if x2 - x1 < 2 or y2 - y1 < 2:
            for item in items:
                canvas.itemconfigure(item, state="hidden")
            return items
        height = y2 - y1
        if x2 - x1 <= height:
            boxes = [(x1, y1, x2, y2)]
            kinds = ("oval",)
        else:
            boxes = [
                (x1, y1, x1 + height, y2),
                (x2 - height, y1, x2, y2),
                (x1 + height / 2, y1, x2 - height / 2, y2),
            ]
            kinds = ("oval", "oval", "rectangle")
        if len(items) != len(boxes):
            for item in items:
                canvas.delete(item)
            items = []
            for kind, box in zip(kinds, boxes):
                if kind == "oval":
                    items.append(canvas.create_oval(*box, fill=fill, outline="", tags=tag))
                else:
                    items.append(canvas.create_rectangle(*box, fill=fill, outline="", tags=tag))
            return items
        for item, box in zip(items, boxes):
            canvas.coords(item, *box)
            canvas.itemconfigure(item, fill=fill, state="normal")
        return items

    def _status_metrics(self, canvas_w: int):
        font = self.fonts["status"]
        key = (int(canvas_w), self._status_text, str(font.cget("family")), int(font.cget("size")))
        if key == self._metrics_key:
            return self._metrics_value
        pad = 108
        max_pill = max(160, canvas_w - 36)
        lines = _wrap_lines(self._status_text, font, max(80, max_pill - pad))
        text_w = 0
        for line in lines:
            text_w = max(text_w, font.measure(line))
        line_h = max(font.metrics("linespace"), 14)
        text_h = line_h * len(lines)
        pill_h = text_h + 42
        pill_w = min(max_pill, max(text_w + pad, int(pill_h * 2.8)))
        self._metrics_key = key
        self._metrics_value = (text_h, pill_w, pill_h)
        return self._metrics_value

    def _pill_spec(self):
        try:
            canvas_w = int(self.signal.winfo_width())
        except tk.TclError:
            return None
        if canvas_w < 40:
            return None
        theme = self._theme()
        panel = theme["panels"].get(self._state) or theme["panels"]["idle"]
        _text_h, pill_w, _pill_h = self._status_metrics(canvas_w)
        return pill_w, panel["bg"], panel["fg"]

    def _fit_signal(self) -> None:
        if self._fitting:
            return
        try:
            canvas_w = int(self.signal.winfo_width())
        except tk.TclError:
            return
        if canvas_w < 40:
            return
        self._fitting = True
        try:
            _text_h, _pill_w, pill_h = self._status_metrics(canvas_w)
            target = int(pill_h + 36)
            current = int(float(self.signal.cget("height")))
            if abs(current - target) > 2:
                self.signal.configure(height=target)
        finally:
            self._fitting = False

    def _draw_brand(self) -> None:
        theme = self._theme()
        canvas = self.brand_mark
        canvas.configure(bg=theme["header"])
        canvas.delete("all")
        canvas.create_oval(1, 1, 13, 13, fill=theme["accent2"], outline="")

    def _draw_pill(self) -> None:
        canvas = self.signal
        theme = self._theme()
        page = self._wash_bg or theme["bg"]
        panel = theme["panels"].get(self._state) or theme["panels"]["idle"]
        try:
            width = int(canvas.winfo_width())
            height = int(canvas.winfo_height())
        except tk.TclError:
            return
        if width < 20 or height < 20:
            return
        if str(canvas.cget("bg")) != page:
            canvas.configure(bg=page)
        try:
            self._paint_pill(canvas, theme, page, panel, width, height)
        except tk.TclError:
            self._pill_glow = []
            self._pill_ring = []
            self._pill_body = []
            self._pill_text = None
            self._pill_text_key = None
            try:
                canvas.delete("pill")
            except tk.TclError:
                pass

    def _paint_pill(self, canvas, theme, page, panel, width: int, height: int) -> None:
        _text_h, pill_w, pill_h = self._status_metrics(width)
        draw_w = pill_w if self._pill_width is None else self._pill_width
        draw_w = max(pill_h * 2.1, min(width - 28, draw_w))
        fill = self._pill_color or panel["bg"]
        fg = self._pill_fg or panel["fg"]
        idle = self._state in ("idle", "progress") and not self._status_animating
        wave = 0.5 + 0.5 * math.sin(self._pulse_phase)
        if idle:
            fill = _mix(fill, fg, 0.03 + 0.045 * wave)
        y1 = (height - pill_h) / 2.0
        y2 = y1 + pill_h
        x1 = (width - draw_w) / 2.0
        x2 = x1 + draw_w
        boost = max(0.0, self._glow_boost)
        glow_pad = 9 + 7 * boost + (3 * wave if idle else 0)
        glow = _mix(page, fill, min(0.7, 0.24 + (0.12 * wave if idle else 0.0) + 0.32 * boost))
        previous = (tuple(self._pill_glow), tuple(self._pill_ring), tuple(self._pill_body), self._pill_text)
        self._pill_glow = self._capsule(
            canvas,
            x1 - glow_pad,
            y1 - glow_pad * 0.72,
            x2 + glow_pad,
            y2 + glow_pad * 0.72,
            glow,
            "pill",
            self._pill_glow,
        )
        color_animating = False
        anim = self._pill_anim
        if self._status_animating and anim and len(anim) >= 6:
            color_animating = not (_same_color(anim[2], anim[3]) and _same_color(anim[4], anim[5]))
        show_ring = _same_color(fill, page) or (self._state in ("idle", "progress") and not color_animating)
        if show_ring:
            ring = fg if _same_color(fill, page) else theme["accent"]
            self._pill_ring = self._capsule(
                canvas, x1 - 1.5, y1 - 1.5, x2 + 1.5, y2 + 1.5, ring, "pill", self._pill_ring
            )
        else:
            for item in self._pill_ring:
                canvas.itemconfigure(item, state="hidden")
        self._pill_body = self._capsule(canvas, x1, y1, x2, y2, fill, "pill", self._pill_body)
        text_width = max(80, draw_w - 96)
        text_key = (self._status_text, fg, int(text_width), width, height, int(self.fonts["status"].cget("size")))
        if self._pill_text is None:
            self._pill_text = canvas.create_text(
                width / 2.0,
                height / 2.0,
                text=self._status_text,
                font=self.fonts["status"],
                fill=fg,
                width=text_width,
                justify="center",
                tags="pill",
            )
            self._pill_text_key = text_key
        elif text_key != self._pill_text_key:
            canvas.coords(self._pill_text, width / 2.0, height / 2.0)
            canvas.itemconfigure(
                self._pill_text,
                text=self._status_text,
                font=self.fonts["status"],
                fill=fg,
                width=text_width,
            )
            self._pill_text_key = text_key
        current = (tuple(self._pill_glow), tuple(self._pill_ring), tuple(self._pill_body), self._pill_text)
        if current != previous:
            for group in (self._pill_glow, self._pill_ring, self._pill_body):
                for item in group:
                    canvas.tag_raise(item)
            if self._pill_text is not None:
                canvas.tag_raise(self._pill_text)

    def _play_status(self) -> None:
        self._anim_token += 1
        token = self._anim_token
        spec = self._pill_spec()
        if spec is None:
            self._pill_dirty = True
            return
        self._pill_dirty = False
        target_w, target_bg, target_fg = spec
        start_w = target_w if self._pill_width is None else float(self._pill_width)
        start_bg = self._pill_color or target_bg
        start_fg = self._pill_fg or target_fg
        width_same = abs(start_w - target_w) < 3
        color_same = _same_color(start_bg, target_bg) and _same_color(start_fg, target_fg)
        moving = not (width_same and color_same)
        self._pill_width = float(target_w) if width_same else start_w
        self._pill_color = target_bg if color_same else start_bg
        self._pill_fg = target_fg if color_same else start_fg
        self._glow_boost = 1.15 if moving else 1.0
        self._status_animating = moving
        self._pill_anim = (start_w, float(target_w), start_bg, target_bg, start_fg, target_fg)
        self._fit_signal()
        if not moving:
            self._draw_pill()
            self._fade_glow(token, 0)
            return
        self._animate_pill(token, 0)

    def _animate_pill(self, token: int, step: int) -> None:
        if not self._alive or token != self._anim_token or not self._pill_anim:
            return
        start_w, target_w, start_bg, target_bg, start_fg, target_fg = self._pill_anim
        steps = 12
        done = step >= steps
        t = _ease(step, steps)
        if done:
            self._pill_width = float(target_w)
            self._pill_color = target_bg
            self._pill_fg = target_fg
            self._glow_boost = 0.0
            self._status_animating = False
        else:
            self._pill_width = start_w + (target_w - start_w) * t
            self._pill_color = _mix(start_bg, target_bg, t)
            self._pill_fg = _mix(start_fg, target_fg, t)
            self._glow_boost = (1.0 - t) * 1.15
        try:
            self._draw_pill()
        except tk.TclError:
            self._status_animating = False
            return
        if done:
            return
        try:
            self.root.after(16, lambda token=token, step=step + 1: self._animate_pill(token, step))
        except tk.TclError:
            self._status_animating = False

    def _fade_glow(self, token: int, step: int) -> None:
        if not self._alive or token != self._anim_token:
            return
        steps = 8
        self._glow_boost = 0.0 if step >= steps else 1.0 - step / float(steps)
        try:
            self._draw_pill()
        except tk.TclError:
            return
        if step >= steps:
            return
        try:
            self.root.after(22, lambda token=token, step=step + 1: self._fade_glow(token, step))
        except tk.TclError:
            pass

    def _snap_pill(self) -> None:
        spec = self._pill_spec()
        if spec is None:
            self._pill_dirty = True
            return
        self._pill_dirty = False
        self._anim_token += 1
        self._status_animating = False
        self._glow_boost = 0.0
        self._pill_width, self._pill_color, self._pill_fg = spec
        self._fit_signal()
        self._draw_pill()

    def _on_signal_configure(self, event) -> None:
        if event.width < 40 or self._fitting:
            return
        if self._status_animating:
            self._draw_pill()
            return
        if self._pill_dirty:
            self._play_status()
            return
        self._snap_pill()

    def _ensure_pulse(self) -> None:
        if not self._alive or self._pulse_job is not None:
            return
        try:
            self._pulse_job = self.root.after(48, self._pulse)
        except tk.TclError:
            self._pulse_job = None

    def _pulse(self) -> None:
        self._pulse_job = None
        if not self._alive:
            return
        breathing = self._state in ("idle", "progress")
        try:
            if breathing:
                self._pulse_phase += 0.075
                if not self._status_animating and self._glow_boost <= 0.01:
                    self._draw_pill()
            elif self._link_state == "online":
                self._pulse_phase += 0.25
            if self._link_state == "online":
                theme = self._theme()
                wave = 0.5 + 0.5 * math.sin(self._pulse_phase * 0.65)
                self.dot.itemconfigure(self.dot_id, fill=_mix(theme["online"], "#ffffff", 0.16 * wave))
            if not breathing and self._link_state != "online":
                return
            delay = 48 if breathing else 160
            self._pulse_job = self.root.after(delay, self._pulse)
        except tk.TclError:
            self._pulse_job = None

    def _button_size(self):
        font = self.fonts["footer"]
        width = max(176, font.measure("Отметить") + 68)
        height = max(44, font.metrics("linespace") + 20)
        return int(width), int(height)

    def _draw_action(self, _event=None) -> None:
        if self._drawing_action:
            return
        self._drawing_action = True
        try:
            canvas = self.manual_button
            theme = self._theme()
            width, height = self._button_size()
            if int(float(canvas.cget("width"))) != width or int(float(canvas.cget("height"))) != height:
                canvas.configure(width=width, height=height)
            canvas.configure(bg=theme["bg"])
            canvas.delete("btn")
            if self._button_pressed:
                fill = _mix(theme["button_hover"], "#ffffff", 0.35)
            elif self._button_hot:
                fill = theme["button_hover"]
            else:
                fill = theme["button"]
            self._capsule(canvas, 1, 1, width - 1, height - 1, fill, "btn")
            canvas.create_text(
                width / 2.0,
                height / 2.0,
                text="Отметить",
                fill=theme["button_text"],
                font=self.fonts["footer"],
                tags="btn",
            )
        except tk.TclError:
            pass
        finally:
            self._drawing_action = False

    def _button_press(self, _event=None):
        self._button_pressed = True
        self._button_press_token += 1
        token = self._button_press_token
        self._draw_action()
        try:
            self.root.after(130, lambda token=token: self._release_button(token))
        except tk.TclError:
            self._button_pressed = False
        return self._submit_manual()

    def _release_button(self, token: int) -> None:
        if token != self._button_press_token or not self._alive:
            return
        self._button_pressed = False
        self._draw_action()

    def _button_enter(self, _event=None) -> None:
        self._button_hot = True
        self._draw_action()

    def _button_leave(self, _event=None) -> None:
        self._button_hot = False
        self._draw_action()

    def _draw_banner(self, _event=None) -> None:
        if self._banner_fit or not getattr(self, "banner_canvas", None):
            return
        text = self._banner_text or ""
        canvas = self.banner_canvas
        theme = self._theme()
        page = theme["bg"]
        try:
            width = int(canvas.winfo_width())
            height = int(canvas.winfo_height())
        except tk.TclError:
            return
        if width < 40:
            return
        canvas.configure(bg=page)
        self.banner.configure(bg=page)
        canvas.delete("banner")
        if not text:
            return
        font = self.fonts["small"]
        inner = max(180, width - 96)
        lines = _wrap_lines(text, font, inner)
        line_h = max(font.metrics("linespace"), 12)
        pill_h = line_h * len(lines) + 18
        target = int(pill_h + 10)
        if abs(int(float(canvas.cget("height"))) - target) > 2:
            self._banner_fit = True
            try:
                canvas.configure(height=target)
            finally:
                self._banner_fit = False
            height = target
        y1 = max(2, (height - pill_h) / 2.0)
        self._capsule(canvas, 4, y1, width - 4, y1 + pill_h, theme["banner_bg"], "banner")
        canvas.create_text(
            width / 2.0,
            height / 2.0,
            text=text,
            fill=theme["banner_fg"],
            font=font,
            width=inner,
            justify="center",
            tags="banner",
        )

    def _paint_history_mark(self, event, color: str, line: str) -> None:
        canvas = event.widget
        height = event.height
        canvas.delete("all")
        if height < 4:
            return
        center = 7
        top = 4
        canvas.create_oval(center - 4, top, center + 4, top + 8, fill=color or line, outline="")

    def _paint_panel(self, state: str) -> None:
        del state
        theme = self._theme()
        page = theme["bg"]
        stamp = (self.settings["ui"].get("theme"), page, theme["text"], theme["muted"], theme["accent"], theme["line"])
        if stamp == self._panel_stamp:
            return
        self._panel_stamp = stamp
        self.body.configure(bg=page)
        self.stage.configure(bg=page)
        self.rail.configure(bg=page)
        self.rail_edge.configure(bg=theme["line"])
        self.rail_rule.configure(bg=theme["line"])
        self.clock_label.configure(bg=page, fg=theme["accent"])
        self.date_label.configure(bg=page, fg=theme["muted"])
        self.history_title.configure(bg=page, fg=theme["muted"])
        self.history_rows.configure(bg=page)
        self.stage_top.configure(bg=page)
        self.stage_bottom.configure(bg=page)
        self.cluster.configure(bg=page)
        self.signal.configure(bg=page)
        self.copy.configure(bg=page)
        self.copy_top.configure(bg=page)
        self.copy_bottom.configure(bg=page)
        self.copy_kicker.configure(bg=page, fg=theme["text"])
        self.name_label.configure(bg=page, fg=theme["text"])
        self.when_label.configure(bg=page, fg=theme["text"])
        self.length_label.configure(bg=page, fg=theme["text"])
        for widget in (self.badge_label, self.detail_label):
            widget.configure(bg=page, fg=theme["muted"])
        self._paint_cards(theme)

    def _request_history(self) -> None:
        if not self._alive:
            return
        token = object()
        self._history_token = token
        try:
            limit = self._shown_history or self._history_limit()
        except tk.TclError:
            limit = self._shown_history or 8

        def run(limit=limit, token=token) -> None:
            try:
                rows = self.db.recent_events(limit)
            except Exception:
                logger().exception("Не удалось прочитать ленту")
                return
            self._later(lambda rows=rows, token=token: self._deliver_history(rows, token))

        threading.Thread(target=run, name="history", daemon=True).start()

    def _deliver_history(self, rows, token) -> None:
        if not self._alive or token is not self._history_token:
            return
        self._render_history(rows)

    def _request_pending(self) -> None:
        if not self._alive:
            return
        token = object()
        self._pending_token = token

        def run(token=token) -> None:
            try:
                count = self.db.count_pending()
            except Exception:
                logger().exception("Не удалось посчитать очередь")
                return
            self._later(lambda count=count, token=token: self._deliver_pending(count, token))

        threading.Thread(target=run, name="pending", daemon=True).start()

    def _deliver_pending(self, count: int, token) -> None:
        if not self._alive or token is not self._pending_token:
            return
        self._set_pending(count)

    def _render_history(self, rows=None) -> None:
        if rows is None:
            self._request_history()
            return
        if not self._alive:
            return
        theme = self._theme()
        now = datetime.now().astimezone()
        pieces = self._history_pieces(rows, now)
        if not rows:
            key = ("empty", self.settings["ui"].get("theme"))
        else:
            key = (
                self.settings["ui"].get("theme"),
                tuple(self._history_piece_key(kind, row, now) for kind, row in pieces),
            )
        if key == self._history_key:
            return
        self._history_key = key
        self._history_fade_token += 1
        bg = theme["bg"]
        if not rows:
            self._history_motion = ("empty",)
            for child in self.history_rows.winfo_children():
                child.destroy()
            tk.Label(
                self.history_rows,
                text="Пока нет отметок",
                anchor="w",
                justify="left",
                font=self.fonts["history"],
                bg=bg,
                fg=theme["muted"],
                wraplength=230,
            ).pack(anchor="w", fill="x", padx=8, pady=4)
            return
        lead_sig = (rows[0]["id"], rows[0]["status"], rows[0]["event_type"])
        animate_lead = self._history_motion is not None and lead_sig != self._history_motion
        self._history_motion = lead_sig
        rows_by_id = {}
        days_by_key: dict[str, list] = {}
        for child in self.history_rows.winfo_children():
            day_key = getattr(child, "_day_key", None)
            if day_key:
                days_by_key.setdefault(day_key, []).append(child)
            elif getattr(child, "_hist_id", None) is not None:
                rows_by_id[child._hist_id] = child
        used = []
        lead_item = None
        for kind, payload in pieces:
            if kind == "day":
                pool = days_by_key.get(payload) or []
                item = pool.pop(0) if pool else self._create_history_day(bg, payload)
                self._update_history_day(item, payload, theme)
            else:
                item = rows_by_id.pop(payload["id"], None)
                if item is None or not item.winfo_exists():
                    item = self._create_history_row(bg)
                    item._hist_id = payload["id"]
                self._update_history_row(item, self._history_visual(payload, theme, now))
                if lead_item is None:
                    lead_item = item
            item.pack(fill="x", pady=0)
            used.append(item)
        for child in list(self.history_rows.winfo_children()):
            if child not in used:
                child.destroy()
        self.history_rows.update_idletasks()
        self._trim_history()
        if animate_lead and lead_item is not None:
            try:
                if lead_item.winfo_exists():
                    self._fade_history_lead(lead_item)
            except tk.TclError:
                pass

    def _history_pieces(self, rows, now: datetime) -> list:
        pieces = []
        last_day = None
        for row in rows:
            moment = parse_iso(row["scanned_at"]) or now
            title = history_day_title(moment, now)
            if title != last_day:
                pieces.append(("day", title))
                last_day = title
            pieces.append(("row", row))
        return pieces

    def _history_piece_key(self, kind: str, payload, now: datetime):
        if kind == "day":
            return ("day", payload)
        moment = parse_iso(payload["scanned_at"]) or now
        return (
            "row",
            payload["id"],
            payload["status"],
            payload["event_type"],
            payload.get("name") or payload["badge"],
            format_history_time(moment, now),
            history_day_title(moment, now),
        )

    def _create_history_day(self, bg: str, title: str):
        item = tk.Frame(self.history_rows, bg=bg)
        label = tk.Label(item, anchor="w", font=self.fonts["kicker"], bg=bg)
        label.pack(anchor="w", fill="x", padx=4, pady=(12, 2))
        item._day_key = title
        item._day_label = label
        return item

    def _update_history_day(self, item, title: str, theme: dict) -> None:
        bg = theme["bg"]
        item._day_key = title
        item.configure(bg=bg)
        item._day_label.configure(text=title, bg=bg, fg=theme["muted"])

    def _history_visual(self, row: dict, theme: dict, now: datetime) -> tuple:
        moment = parse_iso(row["scanned_at"]) or now
        event_name = EVENT_TEXT.get(row["event_type"], row["event_type"])
        status_name = STATUS_TEXT.get(row["status"], row["status"])
        event_color = history_mark_color(theme, row["event_type"], row["status"])
        status_color = theme["muted"]
        if row["status"] in ("pending", "sending"):
            status_color = theme["warn_text"]
        elif row["status"] == "rejected":
            status_color = theme["error_text"]
        show_status = row["status"] != "sent"
        return (
            theme["bg"],
            format_history_time(moment, now),
            theme["muted"],
            event_name,
            event_color,
            row.get("name") or row["badge"],
            theme["text"],
            show_status,
            status_name,
            status_color,
            theme["line"],
        )

    def _create_history_row(self, bg: str):
        item = tk.Frame(self.history_rows, bg=bg)
        mark = tk.Canvas(item, width=16, height=22, highlightthickness=0, bd=0, bg=bg)
        mark.mark_color = ""
        mark.mark_line = ""
        mark.bind(
            "<Configure>",
            lambda event, canvas=mark: self._paint_history_mark(
                event,
                getattr(canvas, "mark_color", "") or "",
                getattr(canvas, "mark_line", "") or "",
            ),
        )
        mark.pack(side="left", anchor="n", padx=(2, 6), pady=(2, 0))
        text = tk.Frame(item, bg=bg)
        text.pack(side="left", fill="x", expand=True)
        top = tk.Frame(text, bg=bg)
        top.pack(fill="x")
        time_label = tk.Label(top, anchor="w", font=self.fonts["kicker"], bg=bg)
        time_label.pack(side="left")
        event_label = tk.Label(top, anchor="w", font=self.fonts["history"], bg=bg)
        event_label.pack(side="left", padx=(8, 0))
        name_label = tk.Label(
            text,
            anchor="w",
            justify="left",
            font=self.fonts["history"],
            bg=bg,
            wraplength=230,
        )
        name_label.pack(anchor="w", fill="x", pady=(1, 2))
        status_label = tk.Label(text, anchor="w", font=self.fonts["kicker"], bg=bg)
        item._mark = mark
        item._text = text
        item._top = top
        item._time = time_label
        item._event = event_label
        item._name = name_label
        item._status = status_label
        item._visual = None
        return item

    def _update_history_row(self, item, visual: tuple) -> None:
        if item._visual == visual:
            return
        item._visual = visual
        (
            bg,
            when,
            time_color,
            event_name,
            event_color,
            name,
            name_color,
            show_status,
            status_name,
            status_color,
            line,
        ) = visual
        for widget in (item, item._text, item._top, item._mark):
            widget.configure(bg=bg)
        item._time.configure(text=when, bg=bg, fg=time_color)
        item._event.configure(text=event_name, bg=bg, fg=event_color)
        item._name.configure(text=name, bg=bg, fg=name_color)
        item._mark.mark_color = event_color
        item._mark.mark_line = line
        if show_status:
            item._status.configure(text=status_name, bg=bg, fg=status_color)
            if not item._status.winfo_ismapped():
                item._status.pack(anchor="w", pady=(0, 8))
        elif item._status.winfo_ismapped():
            item._status.pack_forget()
        self._paint_one_mark(item._mark, event_color)

    def _fade_history_lead(self, item) -> None:
        """Проявить только верхнюю, новую строку истории."""
        page = self._theme()["bg"]
        labels = []
        mark = None
        mark_color = ""
        stack = [item]
        while stack:
            widget = stack.pop()
            try:
                stack.extend(widget.winfo_children())
            except tk.TclError:
                continue
            if isinstance(widget, tk.Label):
                color = str(widget.cget("fg"))
                labels.append((widget, color))
                widget.configure(fg=page)
            elif mark is None and isinstance(widget, tk.Canvas) and getattr(widget, "mark_color", ""):
                mark = widget
                mark_color = str(widget.mark_color)
        self._history_fade_token += 1
        token = self._history_fade_token
        if mark is not None and mark_color:
            mark.mark_color = page
            self._paint_one_mark(mark, page)
        self._history_fade_step(token, labels, mark, mark_color, page, 0)

    def _history_fade_step(self, token: int, labels, mark, mark_color: str, page: str, step: int) -> None:
        if not self._alive or token != self._history_fade_token:
            return
        steps = 9
        done = step >= steps
        t = _ease(step, steps)
        try:
            for widget, color in labels:
                widget.configure(fg=color if done else _mix(page, color, t))
            if mark is not None and mark_color:
                color = mark_color if done else _mix(page, mark_color, t)
                mark.mark_color = color
                self._paint_one_mark(mark, color)
        except tk.TclError:
            return
        if done:
            return
        try:
            self.root.after(
                16,
                lambda token=token, labels=labels, mark=mark, mark_color=mark_color, page=page, step=step + 1: self._history_fade_step(
                    token, labels, mark, mark_color, page, step
                ),
            )
        except tk.TclError:
            pass

    def _paint_one_mark(self, canvas, color: str) -> None:
        line = getattr(canvas, "mark_line", "") or ""
        try:
            height = max(canvas.winfo_height(), canvas.master.winfo_height(), 18)
        except tk.TclError:
            return
        event = type("MarkEvent", (), {"widget": canvas, "height": height})()
        self._paint_history_mark(event, color, line)

    def _trim_history(self) -> None:
        """Убрать строки, которые уже не входят в колонку и иначе рисуются поверх первых."""
        parent_h = self.history_rows.winfo_height()
        if parent_h < 40:
            return
        used = 0
        for child in list(self.history_rows.winfo_children()):
            need = child.winfo_reqheight()
            if used and used + need > parent_h + 1:
                child.destroy()
                continue
            used += need
        self._drop_orphan_headers()
        self._repaint_history_marks()

    def _drop_orphan_headers(self) -> None:
        """После обрезки старых отметок убрать заголовок дня, под которым ничего не осталось."""
        changed = True
        while changed:
            changed = False
            children = list(self.history_rows.winfo_children())
            for index, child in enumerate(children):
                if not getattr(child, "_day_key", None):
                    continue
                nxt = children[index + 1] if index + 1 < len(children) else None
                if nxt is None or getattr(nxt, "_day_key", None):
                    child.destroy()
                    changed = True
                    break

    def _repaint_history_marks(self) -> None:
        for item in self.history_rows.winfo_children():
            for canvas in item.winfo_children():
                color = getattr(canvas, "mark_color", "")
                line = getattr(canvas, "mark_line", "")
                if not color:
                    continue
                height = max(canvas.winfo_height(), item.winfo_height(), 18)
                event = type("MarkEvent", (), {"widget": canvas, "height": height})()
                self._paint_history_mark(event, color, line)

    def _set_pending(self, count: int) -> None:
        theme = self._theme()
        self._pending_count = int(count or 0)
        if count:
            self.pending_label.configure(text=f"В очереди на отправку: {count}", fg=theme["warn_text"])
        else:
            self.pending_label.configure(text="", fg=theme["muted"])
        self._paint_floor()

    def _set_online(self, state: str) -> None:
        theme = self._theme()
        colors = {"online": theme["online"], "offline": theme["offline"], "unset": theme["unset"]}
        labels = {
            "online": "на связи",
            "offline": "нет связи",
            "unset": "сервер не задан",
            "checking": "проверяю связь",
        }
        self._link_state = state
        self.dot.itemconfigure(self.dot_id, fill=colors.get(state, theme["unset"]))
        self.link_label.configure(text=labels.get(state, ""))
        self._paint_link_banner()
        self._ensure_pulse()

    def _paint_clock(self) -> None:
        now = datetime.now()
        self.clock_label.configure(text=now.strftime("%H:%M:%S"))
        date_text = format_clock_date(now)
        if date_text != self._date_text:
            changed = bool(self._date_text)
            self._date_text = date_text
            self.date_label.configure(text=date_text)
            if changed:
                self._history_key = None
                self._request_history()
        self._paint_floor()

    def _consider_layout_banner(self, view: dict) -> None:
        if not self.settings["ui"].get("warn_layout"):
            return
        title = str(view.get("title") or "").casefold()
        detail = str(view.get("detail") or "").casefold()
        unrecognized = "не распознан" in title or "не распознан" in detail
        if not unrecognized and not looks_like_russian_layout(str(view.get("badge") or "")):
            return
        self._layout_token += 1
        token = self._layout_token
        self._set_banner(_LAYOUT_HINT)
        self._layout_after = arm_after(
            self.root,
            self._layout_after,
            int(_LAYOUT_HINT_MS),
            lambda token=token: self._hide_layout_banner(token),
        )

    def _hide_layout_banner(self, token: int) -> None:
        if not self._alive or token != self._layout_token:
            return
        self._set_banner("")

    def _at_work_count(self) -> int | None:
        counted = self.api.open_shift_count()
        if counted is not None:
            self._saved_at_work = counted
            self._saved_at_work_tick = self._floor_tick
            return counted
        self._floor_tick += 1
        if self._floor_tick - self._saved_at_work_tick < 10:
            return self._saved_at_work
        if not self._floor_busy:
            self._saved_at_work_tick = self._floor_tick
            self._request_floor_count()
        return self._saved_at_work

    def _request_floor_count(self) -> None:
        self._floor_busy = True

        def run() -> None:
            value = None
            try:
                _users, sessions = self.db.load_directory()
                value = len(
                    {
                        str(row.get("userId"))
                        for row in sessions
                        if isinstance(row, dict) and str(row.get("userId") or "").strip() and is_open(row)
                    }
                )
            except Exception:
                logger().exception("Не удалось посчитать открытые смены")

            def apply(value=value) -> None:
                self._floor_busy = False
                if not self._alive:
                    return
                if value is not None:
                    self._saved_at_work = value
                self._saved_at_work_tick = self._floor_tick
                self._floor_key = None
                self._paint_floor()

            self._later(apply)

        threading.Thread(target=run, name="floor", daemon=True).start()

    def _paint_floor(self) -> None:
        counted = self._at_work_count()
        work = "—" if counted is None else str(counted)
        queue = str(self._pending_count)
        key = (work, queue)
        if key == self._floor_key:
            return
        self._floor_key = key
        theme = self._theme()
        self.floor_work_value.configure(text=work, fg=theme["text"])
        queued = self._pending_count > 0
        self.floor_queue_value.configure(text=queue, fg=theme["warn_text"] if queued else theme["text"])
        self.queue_rule.configure(bg=theme["warn_text"] if queued else theme["accent"])

    def _paint_cards(self, theme: dict) -> None:
        self.cards.configure(bg=theme["bg"])
        cards = (
            (self.work_card, self.work_rule, self.floor_work_caption, self.floor_work_value, theme["in_text"]),
            (self.queue_card, self.queue_rule, self.floor_queue_caption, self.floor_queue_value, theme["accent"]),
        )
        for card, rule, caption, value, rule_color in cards:
            card.configure(
                bg=theme["card"],
                highlightbackground=theme["line"],
                highlightcolor=theme["line"],
                highlightthickness=1,
            )
            rule.configure(bg=rule_color)
            caption.configure(bg=theme["card"], fg=theme["muted"])
            value.configure(bg=theme["card"], fg=theme["text"])
        if self._pending_count:
            self.queue_rule.configure(bg=theme["warn_text"])
            self.floor_queue_value.configure(fg=theme["warn_text"])

    def _fit_cards(self) -> None:
        caption = self.fonts["detail"].metrics("linespace")
        value = self.fonts["stat"].metrics("linespace")
        height = max(104, int(caption + value + 44))
        width = max(196, int(self.fonts["stat"].measure("888") + 96))
        for card in (self.work_card, self.queue_card):
            card.configure(width=width, height=height)

    def _paint_link_banner(self) -> None:
        theme = self._theme()
        show = self._link_state == "offline"
        self.link_banner.configure(bg=theme["offline"])
        self.link_banner_label.configure(bg=theme["offline"], fg=theme["bg"])
        try:
            mapped = bool(self.link_banner.winfo_ismapped())
        except tk.TclError:
            return
        if show and not mapped:
            self.link_banner.pack(side="top", fill="x", before=self.rail)
        elif not show and mapped:
            self.link_banner.pack_forget()

    def _wash_targets(self):
        return (
            self.stage,
            self.stage_top,
            self.stage_bottom,
            self.cluster,
            self.cards,
            self.copy,
            self.copy_top,
            self.copy_bottom,
            self.signal,
            self.copy_kicker,
            self.name_label,
            self.when_label,
            self.length_label,
            self.badge_label,
            self.detail_label,
        )

    def _start_wash(self) -> None:
        """На секунду-две подсветить фон: зелёным при приходе, синим при уходе."""
        self._wash_token += 1
        token = self._wash_token
        self._wash_after = arm_after(self.root, self._wash_after, 0, None)
        if self._state not in ("in", "out"):
            if self._wash_bg:
                self._apply_wash(None)
            return
        theme = self._theme()
        page = theme["bg"]
        panel = (theme["panels"].get(self._state) or theme["panels"]["idle"])["bg"]
        tint = _mix(page, panel, 0.28)
        self._apply_wash(page)
        self._wash_after = arm_after(
            self.root,
            self._wash_after,
            16,
            lambda token=token, page=page, tint=tint, step=1: self._wash_step(token, page, tint, step),
        )

    def _wash_step(self, token: int, page: str, tint: str, step: int) -> None:
        if not self._alive or token != self._wash_token:
            return
        fade_in = 8
        hold_until = 70
        fade_out = 84
        if step < fade_in:
            color = _mix(page, tint, _ease(step, fade_in))
        elif step < hold_until:
            color = tint
        elif step < fade_out:
            color = _mix(tint, page, _ease(step - hold_until, fade_out - hold_until))
        else:
            self._apply_wash(None)
            return
        try:
            self._apply_wash(color)
        except tk.TclError:
            return
        self._wash_after = arm_after(
            self.root,
            self._wash_after,
            16,
            lambda token=token, page=page, tint=tint, step=step + 1: self._wash_step(token, page, tint, step),
        )

    def _apply_wash(self, color: str | None) -> None:
        if color is None:
            self._wash_bg = None
            self._panel_stamp = None
            self._paint_panel(self._state)
            if not self._status_animating:
                self._draw_pill()
            return
        self._wash_bg = color
        for widget in self._wash_targets():
            widget.configure(bg=color)
        if not self._status_animating:
            self._draw_pill()

    def _set_banner(self, text: str) -> None:
        if text == self._banner_text:
            return
        self._banner_text = text
        if text:
            self.banner.configure(bg=self._theme()["bg"])
            if not self.banner.winfo_ismapped():
                self.banner.grid()
            self._draw_banner()
        elif self.banner.winfo_ismapped():
            self.banner.grid_remove()
        self._history_key = None
        if self._alive:
            try:
                self.root.after_idle(self._render_history)
            except tk.TclError:
                pass

    def _paint_voice_warning(self) -> None:
        if not self._alive:
            return
        if self.settings["voice"].get("enabled") and self.speaker.error:
            self.voice_label.configure(text="Голос недоступен")
        else:
            self.voice_label.configure(text="")

    def _clock_loop(self) -> None:
        if not self._alive:
            return
        try:
            self._paint_clock()
            self.root.after(1000, self._clock_loop)
        except tk.TclError:
            pass

    def _health_loop(self) -> None:
        if not self._alive:
            return
        try:
            if self._health_busy:
                self._deliver_health()
            else:
                self._kick_health()
            self.root.after(15000, self._health_loop)
        except tk.TclError:
            pass

    def _prefetch_health(self) -> None:
        try:
            state = self.api.health_state()
        except Exception:
            logger().exception("Ошибка проверки сервера")
            state = "offline"
        with self._health_lock:
            self._pending_health = state
        self._deliver_health()

    def _deliver_health(self) -> None:
        root = getattr(self, "root", None)
        if root is None:
            return

        def apply() -> None:
            with self._health_lock:
                state = self._pending_health
                if state is None:
                    return
                self._pending_health = None
            self._health_busy = False
            if self._alive and state != self._link_state:
                self._set_online(state)

        try:
            root.after(0, apply)
        except (tk.TclError, RuntimeError):
            self._health_busy = False

    def _kick_health(self) -> None:
        if self._health_busy:
            return
        self._health_busy = True

        def run() -> None:
            try:
                state = self.api.health_state()
            except Exception:
                logger().exception("Ошибка проверки сервера")
                state = "offline"

            def apply() -> None:
                self._health_busy = False
                if self._alive and state != self._link_state:
                    self._set_online(state)

            try:
                self.root.after(0, apply)
            except (tk.TclError, RuntimeError):
                self._health_busy = False

        threading.Thread(target=run, name="health", daemon=True).start()

    def _apply_window_mode(self) -> None:
        from scanner_app.ui_helpers import place_window, widget_alive

        fullscreen = bool(self.settings["ui"].get("fullscreen")) and not self._force_windowed
        if self._hold_fullscreen or widget_alive(self._admin) or widget_alive(self._shifts):
            fullscreen = False
        was_fullscreen = bool(self.root.attributes("-fullscreen"))
        self.root.attributes("-fullscreen", fullscreen)
        if fullscreen:
            self._window_placed = False
            return
        if was_fullscreen or not self._window_placed:
            beside = self._hold_fullscreen or widget_alive(self._admin) or widget_alive(self._shifts)
            place_window(
                self.root,
                1100 if beside else 1200,
                720 if beside else 740,
                min_width=800,
                min_height=560,
                align="left" if beside else "center",
            )
            self._window_placed = True

    def _toggle_fullscreen(self, _event=None):
        self.settings["ui"]["fullscreen"] = not bool(self.settings["ui"].get("fullscreen"))
        save_settings(self.settings)
        self._apply_window_mode()
        return "break"

    def _on_configure(self, event) -> None:
        if event.widget is not self.root or event.width < 200:
            return
        size = (event.width, event.height)
        if size == self._configured_size:
            return
        self._configured_size = size
        scale = self._compute_scale(event.height)
        stage_w = max(320, event.width - 360)
        self.name_label.configure(wraplength=max(220, stage_w - 48))
        self.detail_label.configure(wraplength=max(220, min(680, stage_w - 80)))
        if abs(scale - self._scale) >= 0.04:
            self._scale = scale
            self._apply_fonts()
        limit = self._history_limit()
        if limit != self._shown_history:
            self._shown_history = limit
            self._render_history()
        else:
            self._trim_history()

    def _history_limit(self) -> int:
        height = self.root.winfo_height()
        if height < 640:
            return 4
        if height < 900:
            return 6
        return 8

    def _compute_scale(self, height: int) -> float:
        auto = max(0.85, min(1.55, height / 820))
        user = float(self.settings["ui"].get("font_scale") or 1)
        return auto * user

    def _apply_fonts(self) -> None:
        for name, base in FONT_BASE.items():
            self.fonts[name].configure(size=max(10, int(base * self._scale)))
        self._draw_brand()
        self._draw_action()
        self._fit_cards()
        self._snap_pill()
        if self._banner_text:
            self._draw_banner()

    def _apply_colors(self) -> None:
        theme = self._theme()
        self._wash_token += 1
        self._wash_after = arm_after(self.root, self._wash_after, 0, None)
        self._wash_bg = None
        self._reveal_token += 1
        self._reveal_key = None
        self._history_fade_token += 1
        self._panel_stamp = None
        self.root.configure(bg=theme["bg"])
        self.body.configure(bg=theme["bg"])
        self.header.configure(bg=theme["header"])
        self.brand.configure(bg=theme["header"])
        self.brand_mark.configure(bg=theme["accent2"])
        self.status_row.configure(bg=theme["header"])
        self.footer.configure(bg=theme["bg"])
        self.action_row.configure(bg=theme["bg"], highlightthickness=0)
        self.meta_row.configure(bg=theme["bg"])
        self.header_rule.configure(bg=theme["line"])
        self.footer_rule.configure(bg=theme["line"])
        self.banner.configure(bg=theme["bg"])
        self.dot.configure(bg=theme["header"])
        self._draw_brand()
        for label in (self.title_label, self.device_label, self.link_label):
            label.configure(bg=theme["header"], fg=theme["text"])
        self.device_label.configure(fg=theme["muted"])
        self.link_label.configure(fg=theme["muted"])
        self.footer.configure(bg=theme["bg"])
        self.pending_label.configure(bg=theme["bg"])
        self.voice_label.configure(bg=theme["bg"], fg=theme["warn_text"])
        self.version_label.configure(bg=theme["bg"], fg=theme["muted"])
        self.manual_label.configure(bg=theme["bg"], fg=theme["muted"])
        self.manual_entry.configure(
            bg=theme["entry"],
            fg=theme["text"],
            insertbackground=theme["accent"],
            highlightbackground=theme["line"],
            highlightcolor=theme["accent"],
            highlightthickness=1,
            bd=0,
        )
        self._draw_action()
        self.admin_button.configure(
            bg=theme["bg"],
            fg=theme["accent"],
            activebackground=theme["bg"],
            activeforeground=theme["button_hover"],
            highlightthickness=0,
            bd=0,
        )
        device = str(self.settings["server"].get("device_id") or "")
        self.device_label.configure(text=device)
        self._paint_panel(self._state)
        self._snap_pill()
        shown = "" if not self.settings["ui"].get("warn_layout") else (self._banner_text or "")
        self._banner_text = None
        self._history_key = None
        self._set_banner(shown)
        self._floor_key = None
        self._paint_floor()
        self._render_history()
        self._set_online(self._link_state)

    def _font_family(self, name: str) -> str:
        if name in MONO_FONTS:
            return self.mono
        if name in BOLD_FONTS:
            return self.serif_bold
        if name in SERIF_FONTS:
            return self.serif
        return self.sans

    def _theme(self) -> dict:
        return theme_by_name(self.settings["ui"].get("theme") or "dark")

    def _install_key_fallback(self) -> None:
        self.root.unbind("<Key>")
        if not getattr(self, "_scanner_wanted", True):
            return
        keyboard_on = bool(self.settings["scanner"].get("keyboard_enabled", True))
        if keyboard_on and not self.scanner.hook_ok:
            self.root.bind("<Key>", self._on_tk_key)
            logger().warning("Глобальный перехват клавиатуры недоступен. Сканер работает, только когда окно в фокусе.")

    def _sync_scan_pause(self, event) -> None:
        scanner = getattr(self, "scanner", None)
        if scanner is None:
            return
        scanner.paused = accepts_typing(getattr(event, "widget", None))

    def _on_tk_key(self, event):
        scanner = getattr(self, "scanner", None)
        if scanner is None or scanner.paused:
            return
        if event.keysym in ("Return", "KP_Enter"):
            self.scanner._assembler_call("push_terminator", "enter")
            return
        if event.keysym == "Tab":
            self.scanner._assembler_call("push_terminator", "tab")
            return
        if event.char and event.char >= " ":
            self.scanner._assembler_call("push_char", event.char)

    def _on_close(self) -> None:
        if self.settings["ui"].get("lock_exit"):
            from scanner_app.admin import prompt_password

            if not prompt_password(self.root, self.settings, "Закрытие программы"):
                return
        self.shutdown()

    def _report_error(self, exc_type, value, traceback) -> None:
        logger().critical("Ошибка интерфейса", exc_info=(exc_type, value, traceback))


