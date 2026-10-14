from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
import shutil
import socket
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
DB_PATH = DATA_DIR / "attendance.db"
LOG_DIR = DATA_DIR / "logs"

THEMES = ("dark", "light", "contrast")
PBKDF2_ROUNDS = 120_000
# Рабочий сервер сайта учёта и тот же сервер, запущенный на этом компьютере.
# Оба смотрят в одну базу, поэтому отметка со сканера видна на сайте.
HOSTED_API_URL = "https://backend-ariskharovs-projects.vercel.app"
LOCAL_API_URL = "http://localhost:8080"


def default_settings() -> dict:
    host = socket.gethostname() or "kiosk"
    return {
        "server": {
            "profile": "site",
            "base_url": HOSTED_API_URL,
            "read_url": "",
            "write_url": "",
            "login": "",
            "password": "",
            "auth_name": "",
            "auth_role": "",
            "token": "",
            "supabase_url": "",
            "supabase_key": "",
            "timeout_sec": 12,
            "verify_ssl": True,
            "device_id": host,
            "strict_employee_check": False,
            "paths": {
                "health": "/api/health",
                "employee": "/api/employees/by-badge/{badge}",
                "status": "/api/attendance/status/{badge}",
                "event": "/api/attendance/events",
            },
        },
        "scanner": {
            "keyboard_enabled": True,
            "serial_enabled": False,
            "serial_port": "COM3",
            "baudrate": 9600,
            "min_length": 3,
            "max_length": 512,
            "max_interval_ms": 80,
            "idle_flush_ms": 100,
            "terminators": ["enter", "tab"],
            "strip_aim_prefix": True,
            "strip_prefix": "",
            "strip_suffix": "",
            "badge_regex": "",
            "uppercase": False,
            "cooldown_sec": 4,
        },
        "attendance": {
            "shift_max_hours": 16,
            "retry_sec": 20,
            "result_hold_sec": 8,
        },
        "voice": {
            "enabled": True,
            "rate": 0,
            "volume": 100,
            "voice_id": "",
            "prefer_russian": True,
            "beep": True,
            "templates": {
                "in": "{name}. Приход на работу. {time}.",
                "out": "{name}. Уход с работы. {time}.",
                "unknown": "Бейдж не распознан.",
                "disabled": "Бейдж отключён.",
                "offline": "{name}. {event_phrase}. {time}. Данные сохранены и будут отправлены позже.",
                "duplicate": "Этот бейдж уже отмечен. Подождите немного.",
                "error": "Не удалось обработать бейдж. Обратитесь к администратору.",
            },
        },
        "ui": {
            "title": "Учёт рабочего времени",
            "theme": "dark",
            "font_scale": 1.0,
            "fullscreen": True,
            "show_badge": False,
            "warn_layout": True,
            "lock_exit": False,
        },
        "security": {
            "password_hash": "",
            "password_salt": "",
        },
    }


def deep_merge(defaults: dict, loaded: dict) -> dict:
    result = copy.deepcopy(defaults)
    for key, value in loaded.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        PBKDF2_ROUNDS,
    )
    return salt, digest.hex()


def verify_password(password: str, salt: str, hashed: str) -> bool:
    if not salt or not hashed:
        return False
    _, digest = hash_password(password, salt)
    return secrets.compare_digest(digest, hashed)


def ensure_password(settings: dict) -> bool:
    """Ставит пароль admin, если его ещё нет. Возвращает True, если настройки изменились."""
    security = settings.setdefault("security", {})
    if security.get("password_hash") and security.get("password_salt"):
        return False
    salt, digest = hash_password("admin")
    security["password_salt"] = salt
    security["password_hash"] = digest
    return True


def password_is_default(settings: dict) -> bool:
    security = settings.get("security") or {}
    return verify_password("admin", security.get("password_salt", ""), security.get("password_hash", ""))


def _as_int(value, minimum: int, maximum: int, label: str, errors: list[str]) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        errors.append(f"{label}: нужно целое число")
        return None
    if number < minimum or number > maximum:
        errors.append(f"{label}: допустимо от {minimum} до {maximum}")
        return None
    return number


def _as_float(value, minimum: float, maximum: float, label: str, errors: list[str]) -> float | None:
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        errors.append(f"{label}: нужно число")
        return None
    if number < minimum or number > maximum:
        errors.append(f"{label}: допустимо от {minimum} до {maximum}")
        return None
    return number


def normalize_settings(settings: dict) -> dict:
    """Приводит поля к нужным типам. Вызывать после успешной проверки."""
    server = settings["server"]
    server["base_url"] = str(server.get("base_url") or "").strip().rstrip("/")
    server["read_url"] = str(server.get("read_url") or "").strip().rstrip("/")
    server["write_url"] = str(server.get("write_url") or "").strip().rstrip("/")
    profile = str(server.get("profile") or "site")
    server["profile"] = profile if profile in ("site", "generic") else "site"
    server["login"] = str(server.get("login") or "").strip()
    server["password"] = str(server.get("password") or "")
    server["auth_name"] = str(server.get("auth_name") or "").strip()
    server["auth_role"] = str(server.get("auth_role") or "").strip().lower()
    server["token"] = str(server.get("token") or "").strip()
    server["supabase_url"] = str(server.get("supabase_url") or "").strip().rstrip("/")
    server["supabase_key"] = str(server.get("supabase_key") or "").strip()
    server["timeout_sec"] = int(server.get("timeout_sec") or 5)
    server["device_id"] = str(server.get("device_id") or "").strip()
    server["verify_ssl"] = bool(server.get("verify_ssl"))
    server["strict_employee_check"] = bool(server.get("strict_employee_check"))
    paths = server.setdefault("paths", {})
    for key in ("health", "employee", "status", "event"):
        paths[key] = str(paths.get(key) or "").strip()

    scanner = settings["scanner"]
    scanner["keyboard_enabled"] = bool(scanner.get("keyboard_enabled"))
    scanner["serial_enabled"] = bool(scanner.get("serial_enabled"))
    scanner["serial_port"] = str(scanner.get("serial_port") or "").strip()
    scanner["baudrate"] = int(scanner.get("baudrate") or 9600)
    scanner["min_length"] = int(scanner.get("min_length") or 3)
    scanner["max_length"] = int(scanner.get("max_length") or 512)
    scanner["max_interval_ms"] = int(scanner.get("max_interval_ms") or 80)
    scanner["idle_flush_ms"] = int(scanner.get("idle_flush_ms") or 100)
    scanner["terminators"] = [item for item in (scanner.get("terminators") or []) if item in ("enter", "tab")]
    scanner["strip_aim_prefix"] = bool(scanner.get("strip_aim_prefix"))
    scanner["strip_prefix"] = str(scanner.get("strip_prefix") or "")
    scanner["strip_suffix"] = str(scanner.get("strip_suffix") or "")
    scanner["badge_regex"] = str(scanner.get("badge_regex") or "").strip()
    scanner["uppercase"] = bool(scanner.get("uppercase"))
    scanner["cooldown_sec"] = float(scanner.get("cooldown_sec") or 0)

    attendance = settings["attendance"]
    attendance["shift_max_hours"] = float(attendance.get("shift_max_hours") or 16)
    attendance["retry_sec"] = int(attendance.get("retry_sec") or 20)
    attendance["result_hold_sec"] = int(attendance.get("result_hold_sec") or 8)

    voice = settings["voice"]
    voice["enabled"] = bool(voice.get("enabled"))
    voice["prefer_russian"] = bool(voice.get("prefer_russian", True))
    voice["beep"] = bool(voice.get("beep"))
    voice["rate"] = int(voice.get("rate") or 0)
    voice["volume"] = int(voice.get("volume") or 100)
    voice["voice_id"] = str(voice.get("voice_id") or "")
    templates = voice.setdefault("templates", {})
    for key, value in list(templates.items()):
        templates[key] = str(value or "").strip()

    ui = settings["ui"]
    ui["title"] = str(ui.get("title") or "").strip()
    ui["theme"] = str(ui.get("theme") or "dark")
    ui["font_scale"] = float(ui.get("font_scale") or 1)
    ui["fullscreen"] = bool(ui.get("fullscreen"))
    ui["show_badge"] = bool(ui.get("show_badge"))
    ui["warn_layout"] = bool(ui.get("warn_layout"))
    ui["lock_exit"] = bool(ui.get("lock_exit"))
    return settings


def validate_settings(settings: dict) -> list[str]:
    errors: list[str] = []
    server = settings.get("server") or {}
    scanner = settings.get("scanner") or {}
    attendance = settings.get("attendance") or {}
    voice = settings.get("voice") or {}
    ui = settings.get("ui") or {}
    paths = server.get("paths") or {}

    for label, key in (
        ("Адрес получения данных", "read_url"),
        ("Адрес отправки данных", "write_url"),
        ("Адрес сервера", "base_url"),
        ("Адрес базы сайта", "supabase_url"),
    ):
        value = str(server.get(key) or "").strip()
        if value and not (value.startswith("http://") or value.startswith("https://")):
            errors.append(f"{label} должен начинаться с http:// или https://")

    _as_int(server.get("timeout_sec"), 1, 60, "Таймаут сервера", errors)
    device_id = str(server.get("device_id") or "").strip()
    if not device_id:
        errors.append("Укажите идентификатор устройства")
    elif len(device_id) > 80 or any(ch in device_id for ch in "\r\n\t"):
        errors.append("Идентификатор устройства слишком длинный или содержит перенос строки")

    for name, label in (
        ("health", "Путь проверки связи"),
        ("employee", "Путь сотрудника"),
        ("status", "Путь статуса"),
        ("event", "Путь события"),
    ):
        path = str(paths.get(name) or "").strip()
        if path and not path.startswith("/"):
            errors.append(f"{label} должен начинаться с /")
    for name, label in (("employee", "Путь сотрудника"), ("status", "Путь статуса")):
        path = str(paths.get(name) or "").strip()
        if path and "{badge}" not in path:
            errors.append(f"{label} должен содержать {{badge}}")

    _as_int(scanner.get("baudrate"), 1200, 256000, "Скорость COM-порта", errors)
    _as_int(scanner.get("min_length"), 1, 128, "Минимальная длина кода", errors)
    max_length = _as_int(scanner.get("max_length"), 4, 4000, "Максимальная длина кода", errors)
    min_length = scanner.get("min_length")
    try:
        if max_length is not None and int(min_length) > max_length:
            errors.append("Минимальная длина кода больше максимальной")
    except (TypeError, ValueError):
        pass
    _as_int(scanner.get("max_interval_ms"), 20, 500, "Интервал между символами", errors)
    _as_int(scanner.get("idle_flush_ms"), 50, 2000, "Пауза конца кода", errors)
    _as_float(scanner.get("cooldown_sec"), 0, 120, "Защита от повторного считывания", errors)
    if scanner.get("serial_enabled") and not str(scanner.get("serial_port") or "").strip():
        errors.append("Укажите COM-порт сканера")
    terminators = scanner.get("terminators") or []
    if not isinstance(terminators, list) or any(item not in ("enter", "tab") for item in terminators):
        errors.append("Завершение кода: допустимы только enter и tab")

    regex = str(scanner.get("badge_regex") or "").strip()
    if regex:
        try:
            re.compile(regex)
        except re.error as exc:
            errors.append(f"Регулярное выражение бейджа некорректно: {exc}")

    _as_float(attendance.get("shift_max_hours"), 1, 36, "Длина смены", errors)
    _as_int(attendance.get("retry_sec"), 5, 600, "Интервал повторной отправки", errors)
    _as_int(attendance.get("result_hold_sec"), 2, 60, "Время показа результата", errors)

    _as_int(voice.get("rate"), -10, 10, "Скорость голоса", errors)
    _as_int(voice.get("volume"), 0, 100, "Громкость", errors)
    templates = voice.get("templates") or {}
    for key in ("in", "out", "unknown", "disabled", "offline", "duplicate", "error"):
        if not str(templates.get(key) or "").strip():
            errors.append(f"Пустая голосовая фраза: {key}")

    if ui.get("theme") not in THEMES:
        errors.append("Неизвестная тема оформления")
    _as_float(ui.get("font_scale"), 0.7, 2.2, "Размер шрифта", errors)
    title = str(ui.get("title") or "").strip()
    if not title:
        errors.append("Укажите заголовок на экране")
    elif len(title) > 80:
        errors.append("Заголовок длиннее 80 символов")
    return errors


def server_urls(server: dict | None) -> tuple[str, str]:
    """Адрес чтения сотрудников и адрес отправки отметок.

    Пустое поле берёт второе, затем старый общий адрес. Так старые настройки
    продолжают работать, а в админке адреса можно развести по разным хостам.
    """
    server = server or {}
    read_url = str(server.get("read_url") or "").strip().rstrip("/")
    write_url = str(server.get("write_url") or "").strip().rstrip("/")
    base = str(server.get("base_url") or "").strip().rstrip("/")
    if not read_url:
        read_url = base or write_url
    if not write_url:
        write_url = base or read_url
    return read_url, write_url


def server_configured(server: dict | None) -> bool:
    server = server or {}
    if str(server.get("profile") or "site") == "site":
        return any(server_urls(server))
    return bool(str(server.get("base_url") or "").strip())


def load_settings(path: Path = SETTINGS_PATH) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    settings = default_settings()
    loaded_raw: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                loaded_raw = loaded
                settings = deep_merge(settings, loaded)
        except (OSError, json.JSONDecodeError):
            backup = path.with_suffix(".broken.json")
            try:
                shutil.copy2(path, backup)
            except OSError:
                pass
    changed = ensure_password(settings)
    server = settings.setdefault("server", {})
    raw_server = loaded_raw.get("server") if isinstance(loaded_raw.get("server"), dict) else {}
    profile = str(server.get("profile") or "site")
    # Раньше был один адрес. Он становится и чтением, и отправкой, пока их не задали отдельно.
    if "read_url" not in raw_server:
        server["read_url"] = str(server.get("base_url") or "").strip().rstrip("/")
        changed = True
    if "write_url" not in raw_server:
        server["write_url"] = str(server.get("read_url") or server.get("base_url") or "").strip().rstrip("/")
        changed = True
    if not str(server.get("base_url") or "").strip() and profile == "site":
        fallback = str(server.get("write_url") or server.get("read_url") or "").strip() or HOSTED_API_URL
        server["base_url"] = fallback
        if not str(server.get("read_url") or "").strip():
            server["read_url"] = fallback
        if not str(server.get("write_url") or "").strip():
            server["write_url"] = fallback
        server["profile"] = "site"
        changed = True
    if path.resolve() == SETTINGS_PATH.resolve() and _seed_site_database(server):
        changed = True
    if changed or not path.exists():
        save_settings(settings, path)
    return settings


def _seed_site_database(server: dict) -> bool:
    """Берёт адрес общей базы сайта из локального .env, если в настройках его ещё нет."""
    if str(server.get("supabase_url") or "").strip() and str(server.get("supabase_key") or "").strip():
        return False
    env_path = APP_DIR / "_style_ref" / "backend" / ".env"
    if not env_path.exists():
        return False
    found: dict[str, str] = {}
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        found[key.strip()] = value.strip().strip('"').strip("'")
    url = found.get("SUPABASE_URL", "").strip().rstrip("/")
    key = found.get("SUPABASE_ANON_KEY", "").strip()
    if not url or not key:
        return False
    server["supabase_url"] = url
    server["supabase_key"] = key
    return True


def save_settings(settings: dict, path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(".json.bak"))
        except OSError:
            pass
    payload = json.dumps(settings, ensure_ascii=False, indent=2)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    temporary.replace(path)
