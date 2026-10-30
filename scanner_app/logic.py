from __future__ import annotations

import re
from datetime import datetime, timedelta

# Префикс AIM, которым сканер помечает тип кода: ]C1, ]Q3, ]d2 и т.п.
AIM_PREFIX = re.compile(r"^\][A-Za-z][0-9A-Za-z]")

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def plural(value: int, one: str, few: str, many: str) -> str:
    number = abs(int(value)) % 100
    if 11 <= number <= 14:
        return many
    tail = number % 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def speak_time(moment: datetime) -> str:
    hours = moment.hour
    minutes = moment.minute
    hour_word = plural(hours, "час", "часа", "часов")
    minute_word = plural(minutes, "минута", "минуты", "минут")
    return f"{hours} {hour_word} {minutes} {minute_word}"


def format_when(moment: datetime) -> str:
    month = MONTHS[moment.month - 1]
    return f"{moment.day} {month}, {moment.strftime('%H:%M')}"


def format_clock_date(moment: datetime) -> str:
    return f"{WEEKDAYS[moment.weekday()]}, {moment.day} {MONTHS[moment.month - 1]}"


def format_history_time(moment: datetime, today: datetime | None = None) -> str:
    current = today or datetime.now(moment.tzinfo)
    if moment.date() == current.date():
        return moment.strftime("%H:%M")
    return moment.strftime("%d.%m %H:%M")


def normalize_badge(raw: str, scanner: dict) -> str:
    text = "".join(ch for ch in str(raw) if ch >= " " or ch == "\t").strip()
    if not text:
        return ""
    if scanner.get("strip_aim_prefix", True):
        text = AIM_PREFIX.sub("", text, count=1).strip()
    prefix = str(scanner.get("strip_prefix") or "")
    suffix = str(scanner.get("strip_suffix") or "")
    if prefix and text.startswith(prefix):
        text = text[len(prefix) :]
    if suffix and text.endswith(suffix):
        text = text[: -len(suffix)]
    text = text.strip()
    pattern = str(scanner.get("badge_regex") or "").strip()
    if pattern:
        match = re.search(pattern, text)
        if not match:
            return ""
        text = (match.group(1) if match.groups() else match.group(0)).strip()
    if scanner.get("uppercase"):
        text = text.upper()
    if not text:
        return ""
    min_length = int(scanner.get("min_length") or 1)
    max_length = int(scanner.get("max_length") or 512)
    if len(text) < min_length or len(text) > max_length:
        return ""
    return text


def manual_badge(raw: str, scanner: dict) -> str:
    """Код, который человек вписал сам, а не тот, что прислал сканер.

    Сначала действуют обычные правила сканера. Если из-за шаблона или
    минимальной длины код отбрасывается, остаётся вписанный текст:
    цифры, буквы и символы.
    """
    text = "".join(ch for ch in str(raw) if ch >= " " or ch == "\t").strip()
    if not text:
        return ""
    recognized = normalize_badge(text, scanner)
    if recognized:
        return recognized
    loose = dict(scanner)
    loose["badge_regex"] = ""
    loose["min_length"] = 1
    return normalize_badge(text, loose)


def decide_event(
    last_event: str | None,
    last_time: datetime | None,
    now: datetime,
    max_hours: float,
) -> str:
    """Следующая отметка: уход, если смена ещё открыта, иначе новый приход."""
    if last_event in ("in", "repeat") and last_time is not None:
        age = now - last_time
        # Отрицательный возраст — часы ушли назад. Смена всё ещё открыта,
        # второй приход её продублирует.
        if age <= timedelta(hours=float(max_hours)):
            return "out"
    return "in"


def event_phrase(event: str) -> str:
    if event == "out":
        return "Уход с работы"
    if event == "repeat":
        return "Уже на работе"
    return "Приход на работу"


def render_speech(template: str, values: dict[str, str]) -> str:
    result = str(template or "")
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return " ".join(result.split())


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
