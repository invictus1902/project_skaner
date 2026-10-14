from __future__ import annotations

import ctypes
import os
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime

from scanner_app import APP_VERSION
from scanner_app.api import ApiClient
from scanner_app.config import DB_PATH, load_settings, save_settings
from scanner_app.db import Database
from scanner_app.logging_setup import logger
from scanner_app.logic import format_clock_date, format_history_time, parse_iso
from scanner_app.scanner import ScannerController
from scanner_app.service import AttendanceService
from scanner_app.fonts import faces
from scanner_app.speech import Speaker
from scanner_app.theme import theme_by_name

EVENT_TEXT = {
    "in": "Приход",
    "out": "Уход",
    "unknown": "Не найден",
    "disabled": "Отключён",
}
STATUS_TEXT = {
    "pending": "в очереди",
    "sending": "отправляется",
    "sent": "отправлено",
    "rejected": "отклонено",
}
FONT_BASE = {
    "clock": 34,
    "title": 22,
    "date": 13,
    "status": 60,
    "name": 40,
    "when": 24,
    "detail": 18,
    "history": 16,
    "heading": 18,
    "small": 13,
    "footer": 16,
}
SERIF_FONTS = {"title", "status", "name"}
BOLD_FONTS = {"status", "name"}


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


def keyboard_language() -> int | None:
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        return int(user32.GetKeyboardLayout(thread_id) & 0xFFFF)
    except Exception:
        return None


class KioskApp:
    def __init__(self, windowed: bool = False, enable_scanner: bool = True) -> None:
        self.settings = load_settings()
        self.db = Database(DB_PATH)
        self.api = ApiClient(self.settings)
        self.speaker = Speaker()
        self._alive = True
        self._closing = False
        self._force_windowed = windowed
        self._generation = 0
        self._scale = 1.0
        self._state = "idle"
        self._health_busy = False
        self._link_state = "checking"
        self._shown_history = 0
        self._debug_subs: list = []
        self._result_subs: list = []
        self._admin = None
        self._login = None
        self._shifts = None
        self._hold_fullscreen = False
        self._window_placed = False

        self.root = tk.Tk()
        self.root.title(self.settings["ui"]["title"])
        self.root.minsize(800, 560)
        self.root.report_callback_exception = self._report_error
        self.serif, self.serif_bold, self.sans = faces(self.root)
        self.fonts = {
            name: tkfont.Font(
                root=self.root,
                family=self._font_family(name),
                size=size,
                weight="bold" if name == "clock" or (name in BOLD_FONTS and self.serif_bold == self.serif) else "normal",
            )
            for name, size in FONT_BASE.items()
        }
        self._build()
        self.service = AttendanceService(self.settings, self.db, self.api, self.speaker, self)
        self.scanner = ScannerController(self.settings, self.service.on_burst)
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
        self._set_pending(self.db.count_pending())
        self._clock_loop()
        self._health_loop()
        self.root.after(2500, self._paint_voice_warning)
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
        try:
            self.root.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass

    def show(self, view: dict) -> None:
        self._later(lambda payload=dict(view): self._render(payload))

    def refresh_history(self) -> None:
        self._later(self._render_history)

    def set_pending(self, count: int) -> None:
        self._later(lambda value=count: self._set_pending(value))

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
        self._set_pending(self.db.count_pending())
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
        self.title_label = tk.Label(self.header, text=self.settings["ui"]["title"], anchor="w", font=self.fonts["title"])
        self.device_label = tk.Label(self.header, text="", anchor="w", font=self.fonts["small"])
        self.status_row = tk.Frame(self.header)
        self.dot = tk.Canvas(self.status_row, width=14, height=14, highlightthickness=0, bd=0)
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, outline="")
        self.link_label = tk.Label(self.status_row, anchor="w", font=self.fonts["small"])
        self.dot.pack(side="left")
        self.link_label.pack(side="left", padx=(8, 0))
        self.clock_label = tk.Label(self.header, anchor="e", font=self.fonts["clock"])
        self.date_label = tk.Label(self.header, anchor="e", font=self.fonts["date"])
        self.title_label.grid(row=0, column=0, sticky="w", padx=(28, 12), pady=(12, 0))
        self.device_label.grid(row=1, column=0, sticky="w", padx=(28, 12), pady=(2, 0))
        self.status_row.grid(row=2, column=0, sticky="w", padx=(28, 12), pady=(4, 10))
        self.clock_label.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 28), pady=(10, 0))
        self.date_label.grid(row=2, column=1, sticky="e", padx=(8, 28), pady=(0, 10))
        self.header.columnconfigure(0, weight=1)
        self.header_rule = tk.Frame(self.root, height=2, bd=0, highlightthickness=0)
        self.header_rule.pack_propagate(False)

        self.banner = tk.Frame(self.root)
        self.banner_label = tk.Label(self.banner, anchor="w", justify="left", font=self.fonts["small"], wraplength=1100)
        self.banner_label.pack(fill="x", padx=24, pady=8)

        self.center_wrap = tk.Frame(self.root)
        self.center = tk.Frame(self.center_wrap)
        self.center_top = tk.Frame(self.center)
        self.center_bottom = tk.Frame(self.center)
        self.status_label = tk.Label(self.center, font=self.fonts["status"], anchor="center")
        self.name_label = tk.Label(self.center, font=self.fonts["name"], anchor="center", justify="center")
        self.when_label = tk.Label(self.center, font=self.fonts["when"], anchor="center")
        self.badge_label = tk.Label(self.center, font=self.fonts["detail"], anchor="center")
        self.detail_label = tk.Label(self.center, font=self.fonts["detail"], anchor="center", justify="center", wraplength=900)
        self.center_top.pack(fill="both", expand=True)
        self.status_label.pack(pady=(0, 4))
        self.name_label.pack(pady=2)
        self.when_label.pack(pady=2)
        self.badge_label.pack(pady=0)
        self.detail_label.pack(pady=(6, 0))
        self.center_bottom.pack(fill="both", expand=True)

        self.history = tk.Frame(self.root)
        self.history_title = tk.Label(self.history, text="Последние отметки", anchor="w", font=self.fonts["heading"])
        self.history_title.pack(fill="x", padx=28, pady=(4, 6))
        self.history_card = tk.Frame(self.history, highlightthickness=1, bd=0)
        self.history_card.pack(fill="x", padx=28, pady=(0, 12))
        self.history_rows = tk.Frame(self.history_card)
        self.history_rows.pack(fill="x", padx=8, pady=6)

        self.footer = tk.Frame(self.root)
        self.action_row = tk.Frame(self.footer)
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
        self.manual_button = tk.Button(
            self.action_row,
            text="Отметить",
            font=self.fonts["small"],
            relief="flat",
            padx=18,
            pady=4,
            cursor="hand2",
            command=self._submit_manual,
        )
        self.hint_label = tk.Label(self.meta_row, text="F12 — настройки", anchor="w", font=self.fonts["small"])
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
        self.manual_button.grid(row=0, column=2, sticky="nsew", padx=(10, 16), pady=8)
        self.hint_label.pack(side="left", padx=(16, 12), pady=(0, 8))
        self.pending_label.pack(side="left", padx=(0, 12), pady=(0, 8))
        self.voice_label.pack(side="left", padx=(0, 12), pady=(0, 8))
        self.admin_button.pack(side="right", padx=(8, 16), pady=(0, 8))
        self.version_label.pack(side="right", padx=(8, 4), pady=(0, 8))
        self.action_row.pack(fill="x")
        self.meta_row.pack(fill="x")
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
        self.center_wrap.grid(row=3, column=0, sticky="nsew")
        self.history.grid(row=4, column=0, sticky="ew")
        self.footer_rule.grid(row=5, column=0, sticky="ew")
        self.footer.grid(row=6, column=0, sticky="ew")
        self.center.pack(fill="both", expand=True, padx=36, pady=(8, 10))
        self.root.bind("<Configure>", self._on_configure)

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
        self._paint_panel(self._state)
        self.status_label.configure(text=view.get("title") or "")
        self.name_label.configure(text=view.get("name") or "")
        self.when_label.configure(text=view.get("when") or "")
        badge = view.get("badge") or ""
        if self.settings["ui"].get("show_badge") and badge:
            self.badge_label.configure(text=f"Бейдж {badge}")
        else:
            self.badge_label.configure(text="")
        self.detail_label.configure(text=view.get("detail") or "")
        self._stack_lines()
        for callback in list(self._result_subs):
            try:
                callback(view)
            except Exception:
                logger().exception("Ошибка подписчика результата")
        if hold and self._state not in ("idle", "progress"):
            delay = int(self.settings["attendance"].get("result_hold_sec") or 8) * 1000
            self.root.after(delay, lambda token=generation: self._return_idle(token))

    def _stack_lines(self) -> None:
        previous = None
        for widget in (self.status_label, self.name_label, self.when_label, self.badge_label, self.detail_label):
            widget.pack_forget()
            if not str(widget.cget("text") or "").strip():
                continue
            if previous is None:
                widget.pack(pady=2, before=self.center_bottom)
            else:
                widget.pack(pady=2, after=previous)
            previous = widget

    def _return_idle(self, generation: int) -> None:
        if self._alive and generation == self._generation:
            self._render_idle()

    def _paint_panel(self, state: str) -> None:
        theme = self._theme()
        panel = theme["panels"].get(state) or theme["panels"]["idle"]
        calm = state in ("idle", "progress")
        self.center.configure(
            bg=panel["bg"],
            highlightthickness=1 if calm else 0,
            highlightbackground=theme["line"],
            highlightcolor=theme["line"],
        )
        self.center_top.configure(bg=panel["bg"])
        self.center_bottom.configure(bg=panel["bg"])
        self.center_wrap.configure(bg=theme["bg"])
        for widget, color in (
            (self.status_label, panel["fg"]),
            (self.name_label, panel["fg"]),
            (self.when_label, panel["muted"]),
            (self.badge_label, panel["muted"]),
            (self.detail_label, panel["muted"]),
        ):
            widget.configure(bg=panel["bg"], fg=color)

    def _render_history(self) -> None:
        for child in self.history_rows.winfo_children():
            child.destroy()
        theme = self._theme()
        rows = self.db.recent_events(self._history_limit())
        if not rows:
            tk.Label(
                self.history_rows,
                text="Пока нет отметок",
                anchor="w",
                font=self.fonts["history"],
                bg=theme["card"],
                fg=theme["muted"],
            ).pack(fill="x", padx=8, pady=12)
            return
        now = datetime.now().astimezone()
        for index, row in enumerate(rows):
            bg = theme["card"] if index % 2 == 0 else theme["row"]
            line = tk.Frame(self.history_rows, bg=bg)
            line.pack(fill="x", pady=1)
            line.columnconfigure(1, weight=1)
            moment = parse_iso(row["scanned_at"]) or now
            event_name = EVENT_TEXT.get(row["event_type"], row["event_type"])
            status_name = STATUS_TEXT.get(row["status"], row["status"])
            event_color = theme["in_text"] if row["event_type"] == "in" else theme["out_text"]
            if row["event_type"] in ("unknown", "disabled") or row["status"] == "rejected":
                event_color = theme["error_text"]
            status_color = theme["muted"]
            if row["status"] in ("pending", "sending"):
                status_color = theme["warn_text"]
            elif row["status"] == "rejected":
                status_color = theme["error_text"]
            elif row["status"] == "sent":
                status_color = theme["in_text"]
            cells = (
                (0, format_history_time(moment, now), theme["muted"], "w"),
                (1, row.get("name") or row["badge"], theme["text"], "w"),
                (2, event_name, event_color, "w"),
                (3, status_name, status_color, "e"),
            )
            for column, text, color, anchor in cells:
                tk.Label(
                    line,
                    text=text,
                    anchor=anchor,
                    font=self.fonts["history"],
                    bg=bg,
                    fg=color,
                ).grid(row=0, column=column, sticky="ew", padx=(16 if column == 0 else 8, 16 if column == 3 else 8), pady=8)

    def _set_pending(self, count: int) -> None:
        theme = self._theme()
        if count:
            self.pending_label.configure(text=f"В очереди на отправку: {count}", fg=theme["warn_text"])
        else:
            self.pending_label.configure(text="", fg=theme["muted"])

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

    def _paint_clock(self) -> None:
        now = datetime.now()
        self.clock_label.configure(text=now.strftime("%H:%M:%S"))
        self.date_label.configure(text=format_clock_date(now))
        self._paint_layout()

    def _paint_layout(self) -> None:
        if not self.settings["ui"].get("warn_layout"):
            self._set_banner("")
            return
        language = keyboard_language()
        if language == 0x0419:
            self._set_banner(
                "Русская раскладка: буквы в QR могут считаться неверно. Переключите на английскую — Alt+Shift."
            )
        else:
            self._set_banner("")

    def _set_banner(self, text: str) -> None:
        theme = self._theme()
        if text:
            self.banner_label.configure(text=text, bg=theme["banner_bg"], fg=theme["banner_fg"])
            self.banner.configure(bg=theme["banner_bg"])
            if not self.banner.winfo_ismapped():
                self.banner.grid()
        elif self.banner.winfo_ismapped():
            self.banner.grid_remove()

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
            self._kick_health()
            self.root.after(15000, self._health_loop)
        except tk.TclError:
            pass

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
                if self._alive:
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
        scale = self._compute_scale(event.height)
        self.name_label.configure(wraplength=max(240, event.width - 140))
        self.detail_label.configure(wraplength=max(240, event.width - 180))
        self.banner_label.configure(wraplength=max(400, event.width - 80))
        if abs(scale - self._scale) >= 0.04:
            self._scale = scale
            self._apply_fonts()
        limit = self._history_limit()
        if limit != self._shown_history:
            self._shown_history = limit
            self._render_history()

    def _history_limit(self) -> int:
        height = self.root.winfo_height()
        if height < 760:
            return 3
        if height < 980:
            return 5
        return 8

    def _compute_scale(self, height: int) -> float:
        auto = max(0.85, min(1.55, height / 820))
        user = float(self.settings["ui"].get("font_scale") or 1)
        return auto * user

    def _apply_fonts(self) -> None:
        for name, base in FONT_BASE.items():
            self.fonts[name].configure(size=max(10, int(base * self._scale)))

    def _apply_colors(self) -> None:
        theme = self._theme()
        self.root.configure(bg=theme["bg"])
        frames = (self.history, self.center_wrap)
        for frame in frames:
            frame.configure(bg=theme["bg"])
        self.header.configure(bg=theme["header"])
        self.status_row.configure(bg=theme["header"])
        self.footer.configure(bg=theme["header"])
        self.action_row.configure(bg=theme["card"])
        self.meta_row.configure(bg=theme["header"])
        self.header_rule.configure(bg=theme["line"])
        self.footer_rule.configure(bg=theme["line"])
        self.dot.configure(bg=theme["header"])
        header_labels = (self.title_label, self.device_label, self.clock_label, self.date_label, self.link_label)
        for label in header_labels:
            label.configure(bg=theme["header"], fg=theme["text"])
        self.device_label.configure(fg=theme["muted"])
        self.date_label.configure(fg=theme["muted"])
        self.link_label.configure(fg=theme["muted"])
        self.history.configure(bg=theme["bg"])
        self.history_title.configure(bg=theme["bg"], fg=theme["text"])
        self.history_card.configure(bg=theme["card"], highlightbackground=theme["line"], highlightcolor=theme["line"])
        self.history_rows.configure(bg=theme["card"])
        self.footer.configure(bg=theme["header"])
        self.hint_label.configure(bg=theme["header"], fg=theme["muted"])
        self.pending_label.configure(bg=theme["header"])
        self.voice_label.configure(bg=theme["header"], fg=theme["warn_text"])
        self.version_label.configure(bg=theme["header"], fg=theme["muted"])
        self.manual_label.configure(bg=theme["card"], fg=theme["text"])
        self.manual_entry.configure(
            bg=theme["entry"],
            fg=theme["text"],
            insertbackground=theme["text"],
            highlightbackground=theme["line"],
            highlightcolor=theme["accent"],
        )
        self.manual_button.configure(
            bg=theme["button"],
            fg=theme["button_text"],
            activebackground=theme["button_hover"],
            activeforeground=theme["button_text"],
            highlightthickness=0,
        )
        self.admin_button.configure(
            bg=theme["card"],
            fg=theme["text"],
            activebackground=theme["row"],
            activeforeground=theme["text"],
            highlightthickness=1,
            highlightbackground=theme["line"],
            highlightcolor=theme["line"],
        )
        device = str(self.settings["server"].get("device_id") or "")
        self.device_label.configure(text=device)
        self._paint_panel(self._state)
        self._paint_layout()
        self._set_online(self._link_state)

    def _font_family(self, name: str) -> str:
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

    def _on_tk_key(self, event):
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


