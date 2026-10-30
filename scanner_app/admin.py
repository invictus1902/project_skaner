from __future__ import annotations

import logging
import os
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

from scanner_app.api import ApiClient
from scanner_app.autostart import autostart_installed, disable_autostart, enable_autostart
from scanner_app.config import (
    HOSTED_API_URL,
    LOCAL_API_URL,
    LOG_DIR,
    hash_password,
    normalize_settings,
    password_is_default,
    server_urls,
    validate_settings,
    verify_password,
)
from scanner_app.logging_setup import clear_log_files, logger, memory_handler
from scanner_app.fonts import faces
from scanner_app.logic import format_history_time, parse_iso
from scanner_app.scanner import list_serial_ports, probe_serial_port
from scanner_app.theme import THEME_LABELS, apply_ttk_style, theme_by_name
from scanner_app.ui_helpers import (
    Form,
    ScrollFrame,
    center_over,
    ensure_independent,
    pack_mark,
    place_window,
    present_window,
    widget_alive,
)

TEMPLATE_LABELS = (
    ("in", "Приход"),
    ("out", "Уход"),
    ("unknown", "Неизвестный бейдж"),
    ("disabled", "Бейдж отключён"),
    ("offline", "Нет связи, запись сохранена"),
    ("duplicate", "Повторное считывание"),
    ("error", "Ошибка"),
)
API_HELP = (
    "Два адреса выше меняются здесь, без правки кода. "
    "Получение данных — список сотрудников и кто сейчас на работе. "
    "Отправка данных — отметки прихода и ухода. "
    "Пока сервер один, адреса одинаковые. Если его перенесут на другой хост, впишите новые и нажмите «Сохранить».\n\n"
    f"«Рабочий сервер» подставляет {HOSTED_API_URL}. "
    f"«Локальный сервер» подставляет {LOCAL_API_URL}. Локальный сервер пишет в ту же базу, что и сайт.\n\n"
    "Если сервер недоступен, программа сама решает приход или уход по локальной истории и ставит отметку в очередь. "
    "Когда связь есть, смену открывает и закрывает сервер.\n\n"
    "Отметка бейджем на сайте требует вход администратора или сканера. "
    "Список в «Сменах» открывается и без этого входа. "
    "Пароль профиля сохраняется только на этом компьютере и только после того, как сервер его принял.\n\n"
    "Адрес и четыре пути внизу нужны только если снять галочку протокола сайта и подключить другой сервер:\n"
    "GET /api/health; GET /api/employees/by-badge/{badge}; GET /api/attendance/status/{badge}; "
    "POST /api/attendance/events с полями badge, event, timestamp, device_id, name."
)


def _profile_label(person: dict) -> str:
    name = str(person.get("fullName") or person.get("login") or "Без имени")
    login = str(person.get("login") or "")
    role = "администратор" if person.get("role") == "admin" else "сканер"
    if login and login != name:
        return f"{name} — {login} ({role})"
    return f"{name} ({role})"


def _paint_text(widget, theme: dict) -> None:
    widget.configure(
        bg=theme["entry"],
        fg=theme["text"],
        insertbackground=theme["text"],
        relief="flat",
        highlightthickness=0,
        bd=0,
    )
    bar = getattr(widget, "vbar", None)
    if bar is not None:
        bar.configure(
            bg=theme["row"],
            troughcolor=theme["bg"],
            activebackground=theme["line"],
            highlightthickness=0,
            bd=0,
            relief="flat",
            width=12,
        )


def _paint_button(widget: tk.Button, theme: dict, primary: bool) -> None:
    if primary:
        bg = theme["button"]
        fg = theme["button_text"]
        active = theme["button_hover"]
    else:
        bg = theme["card"]
        fg = theme["text"]
        active = theme["row"]
    widget.configure(
        bg=bg,
        fg=fg,
        activebackground=active,
        activeforeground=fg,
        relief="flat",
        bd=0,
        highlightthickness=1,
        highlightbackground=theme["line"],
        highlightcolor=theme["accent"],
        cursor="hand2",
        padx=10,
        pady=4,
    )


def prompt_password(master, settings: dict, title: str = "Подтверждение") -> bool:
    holder = {"ok": False}

    def success() -> None:
        holder["ok"] = True

    dialog = LoginDialog(master, settings, success, title=title)
    master.wait_window(dialog)
    return holder["ok"]


class LoginDialog(tk.Toplevel):
    def __init__(self, master, settings: dict, on_success, title: str = "Вход в настройки") -> None:
        super().__init__(master)
        self.settings = settings
        self.on_success = on_success
        self.attempts = 0
        theme = theme_by_name(settings["ui"].get("theme") or "dark")
        serif, _serif_bold, sans = faces(self)
        self.title(title)
        self.resizable(False, False)
        self.configure(bg=theme["bg"])
        self.attributes("-topmost", True)
        tk.Label(self, text=title, bg=theme["bg"], fg=theme["text"], font=(serif, 26)).pack(padx=28, pady=(24, 8))
        pack_mark(self, theme, pady=(0, 14))
        tk.Label(self, text="Пароль настроек на этом компьютере", bg=theme["bg"], fg=theme["muted"], font=(sans, 11)).pack(padx=28, anchor="w")
        tk.Label(
            self,
            text="Это не пароль профиля на сайте.",
            bg=theme["bg"],
            fg=theme["muted"],
            font=(sans, 10),
        ).pack(padx=28, anchor="w", pady=(2, 0))
        self.variable = tk.StringVar()
        self.entry = tk.Entry(
            self,
            textvariable=self.variable,
            show="*",
            width=32,
            font=(sans, 14),
            bg=theme["entry"],
            fg=theme["text"],
            insertbackground=theme["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=theme["line"],
            highlightcolor=theme["accent"],
        )
        self.entry.pack(padx=28, pady=8, ipady=6)
        self.error = tk.Label(self, text="", bg=theme["bg"], fg=theme["error_text"], font=(sans, 11))
        self.error.pack(padx=28)
        buttons = tk.Frame(self, bg=theme["bg"])
        buttons.pack(pady=18)
        ok_button = tk.Button(buttons, text="Войти", command=self._ok, width=12, font=(sans, 11))
        cancel_button = tk.Button(buttons, text="Отмена", command=self.destroy, width=12, font=(sans, 11))
        _paint_button(ok_button, theme, primary=True)
        _paint_button(cancel_button, theme, primary=False)
        ok_button.pack(side="left", padx=6)
        cancel_button.pack(side="left", padx=6)
        self.bind("<Return>", lambda _event: self._ok())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        center_over(self, master)
        self.entry.focus_set()
        self.grab_set()

    def _ok(self) -> None:
        security = self.settings.get("security") or {}
        if verify_password(self.variable.get(), security.get("password_salt", ""), security.get("password_hash", "")):
            callback = self.on_success
            self.grab_release()
            self.destroy()
            callback()
            return
        self.attempts += 1
        logger().warning("Неудачная попытка входа в админку")
        self.error.configure(text="Неверный пароль")
        self.variable.set("")
        if self.attempts >= 5:
            self.destroy()


class AdminWindow(tk.Toplevel):
    def __init__(self, master, app) -> None:
        super().__init__(master)
        self.app = app
        self.settings = app.settings
        self.title("Настройки · учёт времени")
        place_window(self, 980, 680, min_width=760, min_height=520, align="right")
        self.serif, _serif_bold, self.sans = faces(self)
        self._style_widgets()
        self._init_vars()
        self._voices_ready = False
        self._voice_touched = False
        self._filling_voices = False
        self._build()
        self._load_voices()
        self._refresh_ports()
        self._refresh_directory()
        self._refresh_queue()
        self._refresh_log()
        self.app.subscribe_debug(self._on_debug)
        self.app.subscribe_result(self._on_result)
        self.bind_all("<MouseWheel>", self._wheel)
        self._refresh_staff()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._bind_dirty()
        self._sync_save()
        ensure_independent(self)
        present_window(self)

    def _open_shifts(self) -> None:
        self.app.open_shifts()

    def _style_widgets(self) -> None:
        theme = theme_by_name(self.settings["ui"].get("theme") or "dark")
        self._theme = theme
        self.configure(bg=theme["bg"])
        apply_ttk_style(ttk.Style(self), theme, self.sans)
        if getattr(self, "log_text", None) is not None:
            _paint_text(self.log_text, theme)
            self._paint_log_tags()

    def _init_vars(self) -> None:
        server = self.settings["server"]
        paths = server["paths"]
        scanner = self.settings["scanner"]
        attendance = self.settings["attendance"]
        voice = self.settings["voice"]
        ui = self.settings["ui"]
        read_url, write_url = server_urls(server)
        self.read_url = tk.StringVar(value=read_url)
        self.write_url = tk.StringVar(value=write_url)
        self.base_url = tk.StringVar(value=server.get("base_url") or "")
        self.site_mode = tk.BooleanVar(value=str(server.get("profile") or "site") == "site")
        self.token = tk.StringVar(value=server.get("token") or "")
        self.auth_login = tk.StringVar(value=server.get("login") or "")
        self.auth_password = tk.StringVar()
        self.auth_text = tk.StringVar(value=self._auth_status_text())
        self._auth_by_label = {}
        self._auth_combo = None
        self._auth_password_entry = None
        self.timeout = tk.StringVar(value=str(server.get("timeout_sec")))
        self.device_id = tk.StringVar(value=server.get("device_id") or "")
        self.verify_ssl = tk.BooleanVar(value=bool(server.get("verify_ssl")))
        self.strict = tk.BooleanVar(value=bool(server.get("strict_employee_check")))
        self.path_health = tk.StringVar(value=paths.get("health") or "")
        self.path_employee = tk.StringVar(value=paths.get("employee") or "")
        self.path_status = tk.StringVar(value=paths.get("status") or "")
        self.path_event = tk.StringVar(value=paths.get("event") or "")
        self.shift_hours = tk.StringVar(value=str(attendance.get("shift_max_hours")))
        self.retry_sec = tk.StringVar(value=str(attendance.get("retry_sec")))
        self.keyboard = tk.BooleanVar(value=bool(scanner.get("keyboard_enabled")))
        self.serial = tk.BooleanVar(value=bool(scanner.get("serial_enabled")))
        self.port = tk.StringVar(value=scanner.get("serial_port") or "")
        self.baud = tk.StringVar(value=str(scanner.get("baudrate")))
        self.min_len = tk.StringVar(value=str(scanner.get("min_length")))
        self.max_len = tk.StringVar(value=str(scanner.get("max_length")))
        self.interval = tk.StringVar(value=str(scanner.get("max_interval_ms")))
        self.idle = tk.StringVar(value=str(scanner.get("idle_flush_ms")))
        terminators = scanner.get("terminators") or []
        self.term_enter = tk.BooleanVar(value="enter" in terminators)
        self.term_tab = tk.BooleanVar(value="tab" in terminators)
        self.aim = tk.BooleanVar(value=bool(scanner.get("strip_aim_prefix")))
        self.prefix = tk.StringVar(value=scanner.get("strip_prefix") or "")
        self.suffix = tk.StringVar(value=scanner.get("strip_suffix") or "")
        self.regex = tk.StringVar(value=scanner.get("badge_regex") or "")
        self.upper = tk.BooleanVar(value=bool(scanner.get("uppercase")))
        self.cooldown = tk.StringVar(value=str(scanner.get("cooldown_sec")))
        self.voice_enabled = tk.BooleanVar(value=bool(voice.get("enabled")))
        self.prefer_russian = tk.BooleanVar(value=bool(voice.get("prefer_russian", True)))
        self.beep = tk.BooleanVar(value=bool(voice.get("beep")))
        self.rate = tk.StringVar(value=str(voice.get("rate")))
        self.volume = tk.StringVar(value=str(voice.get("volume")))
        self.voice_choice = tk.StringVar(value="По умолчанию")
        self.title_text = tk.StringVar(value=ui.get("title") or "")
        current_theme = next((label for key, label in THEME_LABELS if key == ui.get("theme")), "Тёмная")
        self.theme_choice = tk.StringVar(value=current_theme)
        self.font_scale = tk.StringVar(value=str(ui.get("font_scale")))
        self.fullscreen = tk.BooleanVar(value=bool(ui.get("fullscreen")))
        self.show_badge = tk.BooleanVar(value=bool(ui.get("show_badge")))
        self.warn_layout = tk.BooleanVar(value=bool(ui.get("warn_layout")))
        self.lock_exit = tk.BooleanVar(value=bool(ui.get("lock_exit")))
        self.hold_sec = tk.StringVar(value=str(attendance.get("result_hold_sec")))
        self.new_password = tk.StringVar()
        self.confirm_password = tk.StringVar()
        self.show_token = tk.BooleanVar(value=False)
        self.show_auth_password = tk.BooleanVar(value=False)
        self.staff_text = tk.StringVar(value="Считаю сотрудников…")
        self.ports_text = tk.StringVar(value="")
        self.status_text = tk.StringVar(value="")
        self.sim_code = tk.StringVar()
        self.emp_badge = tk.StringVar()
        self.emp_name = tk.StringVar()
        self.only_errors = tk.BooleanVar(value=False)
        self._voice_ids = {"По умолчанию": ""}
        self._token_entry = None

    def _build(self) -> None:
        theme = self._theme
        head = tk.Frame(self, bg=theme["bg"])
        head.pack(fill="x", padx=18, pady=(14, 2))
        tk.Label(head, text="Настройки", bg=theme["bg"], fg=theme["text"], font=(self.serif, 28)).pack(anchor="w")
        pack_mark(head, theme, anchor="w", pady=(10, 0))
        tk.Label(
            head,
            text="Это окно сворачивается отдельно: экран отметок остаётся на месте. F12 открывает настройки снова.",
            bg=theme["bg"],
            fg=theme["muted"],
            font=(self.sans, 11),
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=12, pady=(8, 12))
        ttk.Label(bar, textvariable=self.status_text).pack(side="left")
        ttk.Button(bar, text="Смены", command=self._open_shifts).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="Закрыть", command=self._close).pack(side="right")
        self.save_button = ttk.Button(bar, text="Сохранить", command=self._save, style="Accent.TButton")
        self.save_button.pack(side="right", padx=8)
        self.save_button.state(["disabled"])
        tk.Frame(self, height=1, bg=self._theme["line"]).pack(side="bottom", fill="x", padx=12)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(8, 4))
        self.notebook.add(self._server_tab(), text="Сервер")
        self.notebook.add(self._scanner_tab(), text="Сканер")
        self.notebook.add(self._voice_tab(), text="Голос")
        self.notebook.add(self._look_tab(), text="Экран")
        self.notebook.add(self._log_tab(), text="Журнал")
        self.notebook.add(self._tools_tab(), text="Обслуживание")
        self._load_profiles()

    def _scroll_form(self, title: str) -> tuple[ScrollFrame, Form]:
        scroll = ScrollFrame(self.notebook)
        scroll.canvas.configure(bg=self._theme["bg"])
        scroll.inner.columnconfigure(0, weight=1)
        form = Form(scroll.inner)
        form.grid(row=0, column=0, sticky="ew")
        ttk.Frame(scroll.inner, height=16).grid(row=1, column=0)
        if title:
            form.label(title)
        return scroll, form

    def _server_tab(self) -> ScrollFrame:
        scroll, form = self._scroll_form("")
        addresses = form.card("Адреса")
        addresses.label(
            "Адреса меняются здесь, если сервер перенесут на другой хост. Код программы открывать не нужно.",
            muted=True,
        )
        addresses.entry("Получение данных: сотрудники и кто на работе", self.read_url)
        addresses.entry("Отправка данных: отметки прихода и ухода", self.write_url)
        addresses.buttons(("Рабочий сервер", self._use_hosted), ("Локальный сервер", self._use_local))
        addresses.check("Протокол сайта учёта: сотрудники, смены и отметка бейджем", self.site_mode)
        ttk.Label(addresses, textvariable=self.staff_text, style="Card.TLabel", wraplength=820, justify="left").grid(
            row=addresses.row, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 8)
        )
        addresses.row += 1
        addresses.buttons(("Проверить сервер и число сотрудников", self._test_server))
        login = form.card("Вход")
        login.label(
            "Вход в профиль сайта. Нужен, чтобы отметки прихода и ухода принимал сервер. "
            "Сотрудники в «Сменах» видны и без входа. Пароль в программу не зашит.",
            muted=True,
        )
        self._auth_combo = login.combo("Профиль администратора", self.auth_login)
        self._auth_password_entry = login.entry("Пароль этого профиля", self.auth_password, show="*")
        login.check("Показывать пароль", self.show_auth_password)
        self.show_auth_password.trace_add("write", self._toggle_auth_password)
        ttk.Label(login, textvariable=self.auth_text, style="Card.TLabel", wraplength=820, justify="left").grid(
            row=login.row, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 8)
        )
        login.row += 1
        login.buttons(
            ("Войти", self._sign_in, "Accent.TButton"),
            ("Выйти из профиля", self._sign_out),
            ("Обновить список профилей", self._load_profiles),
        )
        link = form.card("Связь")
        link.entry("Таймаут, секунды", self.timeout)
        link.entry("Идентификатор этого компьютера", self.device_id)
        link.check("Проверять сертификат HTTPS", self.verify_ssl)
        link.check("Не пропускать бейдж, если сервер его не знает", self.strict)
        self._token_entry = link.entry("Ключ доступа, если сервер его требует", self.token, show="*")
        link.check("Показывать ключ", self.show_token)
        self.show_token.trace_add("write", self._toggle_token)
        other = form.card("Другой сервер")
        other.entry("Адрес другого сервера, если протокол сайта выключен", self.base_url)
        other.entry("Путь проверки связи", self.path_health)
        other.entry("Путь сотрудника, с {badge}", self.path_employee)
        other.entry("Путь статуса смены, с {badge}", self.path_status)
        other.entry("Путь отправки события", self.path_event)
        other.entry("Длина смены, часы", self.shift_hours)
        other.entry("Повторная отправка очереди, секунды", self.retry_sec)
        other.label(API_HELP, muted=True)
        return scroll

    def _scanner_tab(self) -> ScrollFrame:
        scroll, form = self._scroll_form("")
        keyboard = form.card("Клавиатура")
        keyboard.label(
            "Почти все USB-сканеры штрихкодов и QR работают как клавиатура: драйвер не нужен. "
            "Подключите сканер и отсканируйте бейдж на вкладке «Обслуживание» — код появится в протоколе. "
            "Если код рвётся на части, увеличьте интервал между символами.",
            muted=True,
        )
        keyboard.check("Принимать сканер как клавиатуру", self.keyboard)
        keyboard.check("Завершать код клавишей Enter", self.term_enter)
        keyboard.check("Завершать код клавишей Tab", self.term_tab)
        keyboard.entry("Минимальная длина кода", self.min_len)
        keyboard.entry("Максимальная длина кода", self.max_len)
        keyboard.entry("Максимальный интервал между символами, мс", self.interval)
        keyboard.entry("Пауза, после которой код считается законченным, мс", self.idle)
        keyboard.entry("Игнорировать повтор того же бейджа, секунды", self.cooldown)
        parsed = form.card("Разбор кода")
        parsed.check("Убирать служебный префикс сканера (]C1, ]Q3…)", self.aim)
        parsed.entry("Свой префикс, который нужно отрезать", self.prefix)
        parsed.entry("Свой суффикс, который нужно отрезать", self.suffix)
        parsed.entry("Регулярное выражение для кода бейджа", self.regex)
        parsed.label("Если QR содержит лишний текст, укажите выражение с группой. Пример: badge=([A-Za-z0-9]+)", muted=True)
        parsed.check("Переводить код в верхний регистр", self.upper)
        port = form.card("COM-порт")
        port.label("COM-порт нужен только сканеру, который не умеет работать как клавиатура.", muted=True)
        port.check("Читать сканер через COM-порт", self.serial)
        port.entry("COM-порт", self.port)
        port.entry("Скорость порта", self.baud)
        port.buttons(("Обновить список портов", self._refresh_ports), ("Проверить порт", self._probe_port))
        ttk.Label(port, textvariable=self.ports_text, style="Card.TLabel", wraplength=820).grid(
            row=port.row, column=0, columnspan=2, sticky="w", padx=16, pady=4
        )
        port.row += 1
        return scroll

    def _voice_tab(self) -> ScrollFrame:
        scroll, form = self._scroll_form("")
        speech = form.card("Речь")
        speech.label(
            "Голос нужен тем, кто не видит экран. Для нормальной русской речи выберите голос Irina или другой русский голос Windows.",
            muted=True,
        )
        speech.check("Произносить результат", self.voice_enabled)
        speech.check("Если голос не выбран, говорить по-русски", self.prefer_russian)
        speech.check("Короткий сигнал динамика", self.beep)
        speech.entry("Скорость голоса, от −10 до 10", self.rate)
        speech.entry("Громкость, от 0 до 100", self.volume)
        speech.label("Голос")
        self.voice_box = ttk.Combobox(speech, textvariable=self.voice_choice, state="readonly", width=58)
        self.voice_box.grid(row=speech.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 4))
        speech.row += 1
        speech.buttons(("Произнести пример", self._test_voice), ("Обновить список голосов", self._load_voices))
        phrases = form.card("Фразы")
        phrases.label("Фразы можно менять. Доступны вставки: {name}, {time}, {badge}, {event_phrase}.", muted=True)
        self.template_boxes = {}
        for key, label in TEMPLATE_LABELS:
            phrases.label(label)
            box = tk.Text(phrases, height=2, width=60, wrap="word", relief="flat", bd=0)
            box.configure(
                bg=self._theme["entry"],
                fg=self._theme["text"],
                insertbackground=self._theme["text"],
                font=(self.sans, 11),
                highlightthickness=1,
                highlightbackground=self._theme["line"],
                highlightcolor=self._theme["accent"],
            )
            box.insert("1.0", (self.settings["voice"].get("templates") or {}).get(key, ""))
            box.grid(row=phrases.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 4))
            self.template_boxes[key] = box
            phrases.row += 1
        return scroll

    def _look_tab(self) -> ScrollFrame:
        scroll, form = self._scroll_form("")
        screen = form.card("Экран")
        screen.entry("Заголовок", self.title_text)
        screen.label("Тема")
        theme_box = ttk.Combobox(
            screen,
            textvariable=self.theme_choice,
            state="readonly",
            values=[label for _key, label in THEME_LABELS],
            width=40,
        )
        theme_box.grid(row=screen.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 4))
        screen.row += 1
        screen.entry("Размер шрифта, от 0.7 до 2.2", self.font_scale)
        screen.entry("Сколько секунд показывать результат", self.hold_sec)
        screen.check("Полный экран", self.fullscreen)
        screen.check("Показывать номер бейджа на экране", self.show_badge)
        screen.check("Предупреждать о русской раскладке", self.warn_layout)
        screen.check("Спрашивать пароль при закрытии программы", self.lock_exit)
        password = form.card("Пароль закрытия")
        note = "Пустые поля оставляют старый пароль. Новый пароль — не короче 4 символов."
        if password_is_default(self.settings):
            note = "Сейчас стоит пароль по умолчанию: admin. Смените его. " + note
        password.label(note, muted=True)
        pair = ttk.Frame(password, style="Card.TFrame")
        pair.grid(row=password.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 8))
        pair.columnconfigure(0, weight=1)
        pair.columnconfigure(1, weight=1)
        ttk.Label(pair, text="Новый пароль", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(pair, text="Повтор нового пароля", style="Card.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Entry(pair, textvariable=self.new_password, show="*").grid(row=1, column=0, sticky="ew", pady=(2, 0))
        ttk.Entry(pair, textvariable=self.confirm_password, show="*").grid(
            row=1, column=1, sticky="ew", padx=(12, 0), pady=(2, 0)
        )
        password.row += 1
        return scroll

    def _log_tab(self) -> ttk.Frame:
        frame = ttk.Frame(self.notebook)
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=8, pady=8)
        ttk.Checkbutton(bar, text="Только ошибки", variable=self.only_errors, command=self._refresh_log).pack(side="left")
        ttk.Button(bar, text="Обновить", command=self._refresh_log).pack(side="left", padx=8)
        ttk.Button(bar, text="Открыть папку", command=self._open_logs).pack(side="left", padx=4)
        ttk.Button(bar, text="Очистить журнал", command=self._clear_logs).pack(side="left", padx=4)
        self.log_text = scrolledtext.ScrolledText(frame, wrap="word", font=(self.sans, 10), relief="flat")
        _paint_text(self.log_text, self._theme)
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log_text.configure(state="disabled")
        return frame

    def _tools_tab(self) -> ScrollFrame:
        scroll, form = self._scroll_form("")
        trial = form.card("Проверка отметки")
        trial.label("Проверка без настоящего сервера.", muted=True)
        trial.entry("Код бейджа: цифры, буквы или символы", self.sim_code)
        trial.buttons(("Отметить этим кодом", self._simulate, "Accent.TButton"))
        self.result_label = ttk.Label(
            trial,
            text="Последний результат появится здесь.",
            style="Card.TLabel",
            wraplength=820,
            justify="left",
        )
        self.result_label.grid(row=trial.row, column=0, columnspan=2, sticky="w", padx=16, pady=4)
        trial.row += 1
        protocol = form.card("Протокол сканера")
        self.debug_text = scrolledtext.ScrolledText(protocol, height=6, wrap="word", font=(self.sans, 10), relief="flat")
        _paint_text(self.debug_text, self._theme)
        self.debug_text.grid(row=protocol.row, column=0, columnspan=2, sticky="ew", padx=16, pady=4)
        self.debug_text.configure(state="disabled")
        protocol.row += 1
        directory = form.card("Локальный справочник")
        directory.label("Используется, когда сервер не прислал имя. Это не замена серверу.", muted=True)
        directory.entry("Бейдж", self.emp_badge)
        directory.entry("Имя", self.emp_name)
        directory.buttons(("Добавить в справочник", self._add_employee), ("Удалить выбранного", self._delete_employee))
        self.employee_tree = self._tree(directory, ("badge", "name"), ("Бейдж", "Имя"), height=5)
        queue = form.card("Очередь")
        queue.label("Неотправленные отметки.", muted=True)
        queue.buttons(("Обновить очередь", self._refresh_queue), ("Отправить сейчас", self._flush))
        self.queue_tree = self._tree(
            queue,
            ("when", "badge", "name", "event", "error"),
            ("Время", "Бейдж", "Имя", "Событие", "Ошибка"),
            height=5,
        )
        computer = form.card("Этот компьютер")
        self.autostart_label = ttk.Label(computer, text=self._autostart_text(), style="Card.TLabel")
        self.autostart_label.grid(row=computer.row, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 4))
        computer.row += 1
        computer.buttons(
            ("Добавить в автозагрузку Windows", self._enable_autostart),
            ("Убрать из автозагрузки", self._disable_autostart),
        )
        computer.label(f"Папка данных: {LOG_DIR.parent}", muted=True)
        return scroll

    def _tree(self, form: Form, columns: tuple[str, ...], headings: tuple[str, ...], height: int):
        tree = ttk.Treeview(form, columns=columns, show="headings", height=height)
        for column, heading in zip(columns, headings):
            tree.heading(column, text=heading)
            tree.column(column, width=140, anchor="w")
        tree.grid(row=form.row, column=0, columnspan=2, sticky="ew", padx=10, pady=4)
        form.row += 1
        return tree

    def _toggle_token(self, *_args) -> None:
        hidden = "" if self.show_token.get() else "*"
        if self._token_entry is not None:
            self._token_entry.configure(show=hidden)

    def _wheel(self, event) -> None:
        widget = getattr(event, "widget", None)
        try:
            if widget is None or widget.winfo_toplevel() is not self:
                return
        except tk.TclError:
            return
        target = widget
        while target is not None:
            if isinstance(target, (tk.Text, ttk.Treeview)):
                return
            if isinstance(target, ScrollFrame):
                step = int(-event.delta / 120) or (-1 if event.delta < 0 else 1)
                target.yview_scroll(step)
                return "break"
            target = getattr(target, "master", None)

    def collect(self) -> tuple[dict, list[str]]:
        import copy

        data = copy.deepcopy(self.settings)
        server = data["server"]
        server["read_url"] = self.read_url.get().strip()
        server["write_url"] = self.write_url.get().strip()
        server["base_url"] = self.base_url.get().strip()
        server["profile"] = "site" if self.site_mode.get() else "generic"
        server["token"] = self.token.get().strip()
        server["timeout_sec"] = self.timeout.get().strip()
        server["device_id"] = self.device_id.get().strip()
        server["verify_ssl"] = self.verify_ssl.get()
        server["strict_employee_check"] = self.strict.get()
        server["paths"] = {
            "health": self.path_health.get().strip(),
            "employee": self.path_employee.get().strip(),
            "status": self.path_status.get().strip(),
            "event": self.path_event.get().strip(),
        }
        scanner = data["scanner"]
        scanner["keyboard_enabled"] = self.keyboard.get()
        scanner["serial_enabled"] = self.serial.get()
        scanner["serial_port"] = self.port.get().strip()
        scanner["baudrate"] = self.baud.get().strip()
        scanner["min_length"] = self.min_len.get().strip()
        scanner["max_length"] = self.max_len.get().strip()
        scanner["max_interval_ms"] = self.interval.get().strip()
        scanner["idle_flush_ms"] = self.idle.get().strip()
        scanner["terminators"] = [name for name, flag in (("enter", self.term_enter.get()), ("tab", self.term_tab.get())) if flag]
        scanner["strip_aim_prefix"] = self.aim.get()
        scanner["strip_prefix"] = self.prefix.get()
        scanner["strip_suffix"] = self.suffix.get()
        scanner["badge_regex"] = self.regex.get().strip()
        scanner["uppercase"] = self.upper.get()
        scanner["cooldown_sec"] = self.cooldown.get().strip()
        data["attendance"]["shift_max_hours"] = self.shift_hours.get().strip()
        data["attendance"]["retry_sec"] = self.retry_sec.get().strip()
        data["attendance"]["result_hold_sec"] = self.hold_sec.get().strip()
        voice = data["voice"]
        voice["enabled"] = self.voice_enabled.get()
        voice["prefer_russian"] = self.prefer_russian.get()
        voice["beep"] = self.beep.get()
        voice["rate"] = self.rate.get().strip()
        voice["volume"] = self.volume.get().strip()
        voice["voice_id"] = self._voice_ids.get(self.voice_choice.get(), "")
        voice["templates"] = {key: self.template_boxes[key].get("1.0", "end").strip() for key, _label in TEMPLATE_LABELS}
        ui = data["ui"]
        ui["title"] = self.title_text.get().strip()
        ui["theme"] = next((key for key, label in THEME_LABELS if label == self.theme_choice.get()), "dark")
        ui["font_scale"] = self.font_scale.get().strip()
        ui["fullscreen"] = self.fullscreen.get()
        ui["show_badge"] = self.show_badge.get()
        ui["warn_layout"] = self.warn_layout.get()
        ui["lock_exit"] = self.lock_exit.get()
        errors = validate_settings(data)
        new_password = self.new_password.get()
        confirm = self.confirm_password.get()
        if new_password or confirm:
            if new_password != confirm:
                errors.append("Пароли не совпадают")
            elif len(new_password) < 4:
                errors.append("Новый пароль должен быть не короче 4 символов")
        return data, errors

    def _bind_dirty(self) -> None:
        variables = (
            self.read_url,
            self.write_url,
            self.base_url,
            self.site_mode,
            self.token,
            self.timeout,
            self.device_id,
            self.verify_ssl,
            self.strict,
            self.path_health,
            self.path_employee,
            self.path_status,
            self.path_event,
            self.shift_hours,
            self.retry_sec,
            self.keyboard,
            self.serial,
            self.port,
            self.baud,
            self.min_len,
            self.max_len,
            self.interval,
            self.idle,
            self.term_enter,
            self.term_tab,
            self.aim,
            self.prefix,
            self.suffix,
            self.regex,
            self.upper,
            self.cooldown,
            self.voice_enabled,
            self.prefer_russian,
            self.beep,
            self.rate,
            self.volume,
            self.title_text,
            self.theme_choice,
            self.font_scale,
            self.fullscreen,
            self.show_badge,
            self.warn_layout,
            self.lock_exit,
            self.hold_sec,
            self.new_password,
            self.confirm_password,
        )
        for variable in variables:
            variable.trace_add("write", self._sync_save)
        self.voice_choice.trace_add("write", self._voice_edited)
        for box in self.template_boxes.values():
            box.bind("<KeyRelease>", self._sync_save)

    def _voice_edited(self, *_args) -> None:
        if not self._filling_voices:
            self._voice_touched = True
        self._sync_save()

    def _is_dirty(self) -> bool:
        import copy

        if self.new_password.get() or self.confirm_password.get():
            return True
        data, errors = self.collect()
        if errors:
            return True
        normalize_settings(data)
        saved = copy.deepcopy(self.settings)
        normalize_settings(saved)
        if not self._voice_touched:
            data["voice"]["voice_id"] = saved["voice"]["voice_id"]
        return data != saved

    def _sync_save(self, *_args) -> None:
        button = getattr(self, "save_button", None)
        if button is None:
            return
        try:
            dirty = self._is_dirty()
        except Exception:
            dirty = True
        if dirty:
            button.state(["!disabled"])
        else:
            button.state(["disabled"])

    def _save(self) -> None:
        data, errors = self.collect()
        if errors:
            messagebox.showerror("Проверьте настройки", "\n".join(errors), parent=self)
            self.status_text.set("Есть ошибки")
            return
        normalize_settings(data)
        new_password = self.new_password.get()
        if new_password:
            salt, digest = hash_password(new_password)
            data["security"]["password_salt"] = salt
            data["security"]["password_hash"] = digest
            self.new_password.set("")
            self.confirm_password.set("")
        self.settings = data
        self.app.apply_settings(data)
        self.status_text.set("Сохранено")
        self._style_widgets()
        self._sync_save()

    def _auth_status_text(self) -> str:
        server = self.settings.get("server") or {}
        login = str(server.get("login") or "").strip()
        password = str(server.get("password") or "")
        if not login or not password:
            return "Вход не выполнен. Выберите профиль и введите пароль — в программе его заранее нет."
        name = str(server.get("auth_name") or login)
        role = str(server.get("auth_role") or "")
        if role == "admin":
            role_text = "администратор"
        elif role == "scanner":
            role_text = "сканер"
        else:
            role_text = role
        suffix = f", {role_text}" if role_text else ""
        return f"Вход сохранён: {name}{suffix}. Пароль лежит только на этом компьютере."

    def _signed_in(self) -> bool:
        server = self.settings.get("server") or {}
        return bool(str(server.get("login") or "").strip() and str(server.get("password") or ""))

    def _toggle_auth_password(self, *_args) -> None:
        hidden = "" if self.show_auth_password.get() else "*"
        if self._auth_password_entry is not None:
            self._auth_password_entry.configure(show=hidden)

    def _load_profiles(self) -> None:
        data, errors = self.collect()
        if errors:
            self.auth_text.set("Профили не загружены: " + errors[0])
            return
        normalize_settings(data)

        def run() -> None:
            people, error = ApiClient(data).operator_profiles()
            try:
                self.after(0, lambda: self._apply_profiles(people, error))
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=run, daemon=True).start()

    def _apply_profiles(self, people: list, error: str) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        mapping = {}
        labels = []
        for person in people:
            label = _profile_label(person)
            identifier = person.get("login") or person.get("email") or person.get("fullName")
            if not identifier:
                continue
            if label in mapping:
                label = f"{label} #{person.get('id')}"
            mapping[label] = identifier
            labels.append(label)
        self._auth_by_label = mapping
        if self._auth_combo is not None:
            self._auth_combo.configure(values=labels)
        saved = str((self.settings.get("server") or {}).get("login") or "").strip()
        for label, identifier in mapping.items():
            if identifier == saved:
                self.auth_login.set(label)
                break
        if error and not self._signed_in():
            self.auth_text.set(error)
        elif not self._signed_in():
            if labels:
                self.auth_text.set(f"В списке {len(labels)}. Выберите профиль и введите пароль.")
            else:
                self.auth_text.set("Профили администраторов не найдены. Логин можно вписать вручную.")

    def _sign_in(self) -> None:
        import copy

        label = self.auth_login.get().strip()
        password = self.auth_password.get()
        identifier = self._auth_by_label.get(label, label)
        if not identifier or not password:
            self.auth_text.set("Выберите профиль и введите пароль.")
            return
        data, errors = self.collect()
        if errors:
            self.auth_text.set(errors[0])
            return
        normalize_settings(data)
        self.auth_text.set("Вхожу…")

        def run() -> None:
            trial = copy.deepcopy(data)
            trial["server"]["login"] = identifier
            trial["server"]["password"] = password
            client = ApiClient(trial)
            result = client.sign_in(identifier, password)

            def apply() -> None:
                try:
                    if not self.winfo_exists():
                        return
                except tk.TclError:
                    return
                if not result.ok:
                    self.auth_text.set(result.error or "Вход не выполнен.")
                    return
                profile = result.data if isinstance(result.data, dict) else {}
                data["server"]["login"] = identifier
                data["server"]["password"] = password
                data["server"]["auth_name"] = str(profile.get("fullName") or identifier)
                data["server"]["auth_role"] = str(profile.get("role") or "")
                normalize_settings(data)
                self.settings = data
                self.app.apply_settings(data)
                self.app.api.adopt_session(client)
                self.auth_password.set("")
                self.auth_text.set(self._auth_status_text())
                self.status_text.set("Вход выполнен")
                self._sync_save()

            try:
                self.after(0, apply)
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=run, daemon=True).start()

    def _sign_out(self) -> None:
        data, errors = self.collect()
        if errors:
            self.auth_text.set(errors[0])
            return
        normalize_settings(data)
        data["server"]["login"] = ""
        data["server"]["password"] = ""
        data["server"]["auth_name"] = ""
        data["server"]["auth_role"] = ""
        self.settings = data
        self.app.apply_settings(data)
        self.app.api.sign_out()
        self.auth_password.set("")
        self.auth_login.set("")
        self.auth_text.set("Вход сброшен. Отметки снова ждут, пока не войдёте профилем администратора.")
        self.status_text.set("Выход выполнен")
        self._sync_save()

    def _test_server(self) -> None:
        data, errors = self.collect()
        if errors:
            self.staff_text.set("Сначала исправьте настройки: " + errors[0])
            return
        normalize_settings(data)
        self.staff_text.set("Проверяем…")
        self._ask_server(data, self.staff_text)

    def _use_hosted(self) -> None:
        self._set_both_urls(HOSTED_API_URL)

    def _use_local(self) -> None:
        self._set_both_urls(LOCAL_API_URL)

    def _set_both_urls(self, url: str) -> None:
        self.read_url.set(url)
        self.write_url.set(url)
        self.base_url.set(url)
        self.site_mode.set(True)
        self._refresh_staff()

    def _refresh_staff(self) -> None:
        data, errors = self.collect()
        if errors:
            self.staff_text.set("Справочник не проверен: " + errors[0])
            return
        normalize_settings(data)
        self.staff_text.set("Считаю сотрудников…")
        self._ask_server(data, self.staff_text)

    def _ask_server(self, data: dict, target: tk.StringVar) -> None:
        def run() -> None:
            text = ApiClient(data).probe()
            try:
                self.after(0, lambda: target.set(text))
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=run, daemon=True).start()

    def _test_voice(self) -> None:
        data, errors = self.collect()
        voice_errors = [item for item in errors if item.startswith("Скорость") or item.startswith("Громкость") or item.startswith("Пустая")]
        if voice_errors:
            messagebox.showerror("Голос", "\n".join(voice_errors), parent=self)
            return
        if self.app.speaker.error:
            messagebox.showwarning("Голос", "Голос Windows недоступен:\n" + self.app.speaker.error, parent=self)
            return
        voice = {
            "enabled": True,
            "rate": int(self.rate.get() or 0),
            "volume": int(self.volume.get() or 100),
            "voice_id": self._voice_ids.get(self.voice_choice.get(), ""),
        }
        self.app.speaker.say("Проверка голоса. Иванов Иван. Приход на работу. 9 часов 5 минут.", voice)

    def _load_voices(self) -> None:
        def run() -> None:
            voices = self.app.speaker.list_voices(timeout=8)
            try:
                self.after(0, lambda: self._fill_voices(voices))
            except (tk.TclError, RuntimeError):
                pass

        threading.Thread(target=run, daemon=True).start()

    def _fill_voices(self, voices: list[dict]) -> None:
        labels = ["По умолчанию"]
        mapping = {"По умолчанию": ""}
        current_id = str(self.settings["voice"].get("voice_id") or "")
        selected = "По умолчанию"
        for index, voice in enumerate(voices):
            label = voice["name"] or f"Голос {index + 1}"
            if label in mapping:
                label = f"{label} ({index + 1})"
            mapping[label] = voice["id"]
            labels.append(label)
            if voice["id"] == current_id:
                selected = label
        self._voice_ids = mapping
        self.voice_box.configure(values=labels)
        self._filling_voices = True
        try:
            self.voice_choice.set(selected if selected in mapping else "По умолчанию")
        finally:
            self._filling_voices = False
        self._voices_ready = True
        self._sync_save()
        if self.app.speaker.error:
            self.status_text.set("Голос недоступен")

    def _refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.ports_text.set("Найденные порты: " + (", ".join(ports) if ports else "нет"))

    def _probe_port(self) -> None:
        try:
            baud = int(self.baud.get().strip() or "9600")
        except ValueError:
            messagebox.showerror("COM-порт", "Скорость порта должна быть числом", parent=self)
            return
        ok, message = probe_serial_port(self.port.get(), baud)
        if ok:
            messagebox.showinfo("COM-порт", message, parent=self)
        else:
            messagebox.showerror("COM-порт", message, parent=self)

    def _simulate(self) -> None:
        code = self.sim_code.get().strip()
        if not code:
            self.status_text.set("Введите код бейджа")
            return
        self.app.service.submit_manual(code)
        self.status_text.set("Код отправлен в обработку")

    def _on_debug(self, text: str) -> None:
        self._append_box(getattr(self, "debug_text", None), text)

    def _on_result(self, view: dict) -> None:
        label = getattr(self, "result_label", None)
        if not widget_alive(label):
            return
        name = view.get("name") or ""
        detail = view.get("detail") or ""
        try:
            label.configure(text=f"{view.get('title', '')}: {name}. {detail}".strip())
        except tk.TclError:
            return
        self._refresh_queue()

    def _append_box(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        if not widget_alive(widget):
            return
        try:
            widget.configure(state="normal")
            widget.insert("end", text + "\n")
            widget.see("end")
            widget.configure(state="disabled")
        except tk.TclError:
            return

    def _add_employee(self) -> None:
        badge = self.emp_badge.get().strip()
        name = self.emp_name.get().strip()
        if not badge or not name:
            messagebox.showerror("Справочник", "Нужны и бейдж, и имя", parent=self)
            return
        self.app.db.upsert_employee(badge, name, "manual")
        self.emp_badge.set("")
        self.emp_name.set("")
        self._refresh_directory()
        logger().info("В локальный справочник добавлен бейдж %s", badge)

    def _delete_employee(self) -> None:
        selected = self.employee_tree.selection()
        if not selected:
            return
        badge = self.employee_tree.item(selected[0], "values")[0]
        self.app.db.delete_employee(badge)
        self._refresh_directory()

    def _refresh_directory(self) -> None:
        if not widget_alive(getattr(self, "employee_tree", None)):
            return
        self._fill_tree(self.employee_tree, [(row["badge"], row["name"]) for row in self.app.db.list_employees()])

    def _refresh_queue(self) -> None:
        if not widget_alive(getattr(self, "queue_tree", None)):
            return
        rows = []
        for row in self.app.db.pending_events(limit=30):
            moment = parse_iso(row["scanned_at"])
            when = format_history_time(moment) if moment else row["scanned_at"]
            event = "Приход" if row["event_type"] == "in" else "Уход" if row["event_type"] == "out" else row["event_type"]
            rows.append((when, row["badge"], row.get("name") or "", event, row.get("error") or ""))
        self._fill_tree(self.queue_tree, rows)

    def _fill_tree(self, tree: ttk.Treeview, rows: list[tuple]) -> None:
        if not widget_alive(tree):
            return
        try:
            children = tree.get_children()
            if children:
                tree.delete(*children)
            for row in rows:
                tree.insert("", "end", values=row)
        except tk.TclError:
            return

    def _flush(self) -> None:
        self.app.service.flush_queue()
        self.status_text.set("Отправка очереди запущена")
        self.after(1200, self._refresh_queue)

    def _paint_log_tags(self) -> None:
        theme = self._theme
        self.log_text.tag_configure("INFO", foreground=theme["muted"])
        self.log_text.tag_configure("WARNING", foreground=theme["warn_text"])
        self.log_text.tag_configure("ERROR", foreground=theme["error_text"])

    def _log_level_tag(self, line: str) -> str:
        parts = line.split("  ", 2)
        level = parts[1] if len(parts) > 1 else ""
        if level in ("ERROR", "CRITICAL"):
            return "ERROR"
        if level == "WARNING":
            return "WARNING"
        return "INFO"

    def _refresh_log(self) -> None:
        if not widget_alive(getattr(self, "log_text", None)):
            return
        handler = memory_handler()
        lines = []
        if handler is not None:
            level = logging.ERROR if self.only_errors.get() else logging.INFO
            lines = handler.snapshot(level)
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self._paint_log_tags()
            if not lines:
                self.log_text.insert("1.0", "Журнал пуст")
            else:
                for index, line in enumerate(lines):
                    if index:
                        self.log_text.insert("end", "\n")
                    self.log_text.insert("end", line, self._log_level_tag(line))
            self.log_text.configure(state="disabled")
            self.log_text.see("end")
        except tk.TclError:
            return

    def _open_logs(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(LOG_DIR)

    def _clear_logs(self) -> None:
        if not messagebox.askyesno("Журнал", "Очистить журнал программы?", parent=self):
            return
        clear_log_files(LOG_DIR)
        self._refresh_log()
        self.status_text.set("Журнал очищен")

    def _autostart_text(self) -> str:
        return "Автозагрузка Windows: включена" if autostart_installed() else "Автозагрузка Windows: выключена"

    def _enable_autostart(self) -> None:
        try:
            path = enable_autostart()
        except OSError as exc:
            messagebox.showerror("Автозагрузка", str(exc), parent=self)
            return
        self.autostart_label.configure(text=self._autostart_text())
        self.status_text.set(f"Автозагрузка записана: {path.name}")

    def _disable_autostart(self) -> None:
        try:
            disable_autostart()
        except OSError as exc:
            messagebox.showerror("Автозагрузка", str(exc), parent=self)
            return
        self.autostart_label.configure(text=self._autostart_text())
        self.status_text.set("Автозагрузка выключена")

    def _status(self, text: str) -> None:
        self.status_text.set(text)

    def _close(self) -> None:
        self.app.unsubscribe_debug(self._on_debug)
        self.app.unsubscribe_result(self._on_result)
        try:
            self.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        app = self.app
        self.destroy()
        app._admin = None
        try:
            app._apply_window_mode()
        except tk.TclError:
            pass
