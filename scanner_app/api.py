from __future__ import annotations

import copy
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote

from scanner_app.config import LOCAL_API_URL, server_urls
from scanner_app.logging_setup import logger
from scanner_app.logic import parse_iso
from scanner_app.shifts import duration_minutes, is_open, keep_shift_span, normalize_clock, normalize_date

UNKNOWN_CODES = {
    "unknown_badge",
    "unknown",
    "not_found",
    "employee_not_found",
    "badge_not_found",
}
DIRECTORY_CACHE_SEC = 30.0
CLOSED_HOST_SEC = 75.0


@dataclass
class HttpResult:
    ok: bool
    status: int | None
    data: dict | list | None
    error: str | None
    network_error: bool
    raw_text: str = ""


def build_url(base: str, path: str, badge: str | None = None) -> str:
    base = str(base or "").strip().rstrip("/")
    path = str(path or "").strip()
    if not base or not path:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    if badge is not None:
        path = path.replace("{badge}", quote(str(badge), safe=""))
    return base + path


def extract_name(data) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("fullName", "name", "employee_name", "full_name", "fio"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    employee = data.get("employee")
    if isinstance(employee, dict):
        return extract_name(employee)
    return ""


def _hm(value) -> str:
    clock = normalize_clock(str(value or ""))
    return clock[:5] if clock else ""


def _clock_on(day, clock: str, fallback: datetime) -> datetime | None:
    """Собрать дату и часы ответа в один момент. Часовой пояс берётся у скана."""
    normal = normalize_clock(str(clock or ""))
    if not normal:
        return None
    date_text = normalize_date(str(day or "")) or fallback.strftime("%Y-%m-%d")
    try:
        parsed = datetime.strptime(date_text + "T" + normal, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    if fallback.tzinfo is not None:
        return parsed.replace(tzinfo=fallback.tzinfo)
    return parsed


def _stamp_mark(data: dict, kind: str, moment: datetime) -> None:
    data["event"] = kind
    data["mark_time"] = moment.isoformat(timespec="seconds")


def server_mark(data, fallback_event: str, fallback_moment: datetime) -> tuple[str, datetime]:
    """Тип и время отметки из ответа сервера. Повтор открытой смены — не новый приход."""
    if not isinstance(data, dict):
        kind = fallback_event if fallback_event in ("in", "out", "repeat") else "in"
        return kind, fallback_moment
    aliases = {
        "repeat": "repeat",
        "already": "repeat",
        "duplicate": "repeat",
        "повтор": "repeat",
        "start": "in",
        "in": "in",
        "arrival": "in",
        "приход": "in",
        "end": "out",
        "out": "out",
        "departure": "out",
        "уход": "out",
    }
    if data.get("alreadyWorking") is True or data.get("already") is True:
        kind = "repeat"
    else:
        event_field = str(data.get("event") or "").strip().lower()
        action_field = str(data.get("action") or "").strip().lower()
        kind = aliases.get(event_field) or aliases.get(action_field) or extract_event(data) or ""
    if kind not in ("in", "out", "repeat"):
        kind = fallback_event if fallback_event in ("in", "out", "repeat") else "in"
    stamped = parse_iso(str(data.get("mark_time") or ""))
    if stamped is not None:
        if stamped.tzinfo is None and fallback_moment.tzinfo is not None:
            stamped = stamped.replace(tzinfo=fallback_moment.tzinfo)
        return kind, stamped
    clock = _hm(data.get("startTime") if kind in ("in", "repeat") else data.get("endTime"))
    combined = _clock_on(data.get("date"), clock, fallback_moment) if clock else None
    return kind, combined or fallback_moment


def extract_event(data) -> str:
    if not isinstance(data, dict):
        return ""
    action = data.get("action")
    if isinstance(action, str):
        mapped = {"start": "in", "end": "out", "in": "in", "out": "out"}.get(action.strip().lower())
        if mapped:
            return mapped
    aliases = {
        "in": "in",
        "out": "out",
        "arrival": "in",
        "enter": "in",
        "checkin": "in",
        "check_in": "in",
        "приход": "in",
        "departure": "out",
        "leave": "out",
        "checkout": "out",
        "check_out": "out",
        "уход": "out",
    }
    for key in ("event", "type", "event_type", "last_event"):
        value = data.get(key)
        if isinstance(value, str):
            mapped = aliases.get(value.strip().lower())
            if mapped:
                return mapped
    return ""


def extract_active(data) -> bool | None:
    if not isinstance(data, dict) or "active" not in data:
        return None
    return bool(data.get("active"))


def classify_post(result: HttpResult) -> tuple[str, str]:
    """Возвращает (sent|unknown|rejected|retry, сообщение)."""
    if result.network_error:
        return "retry", result.error or "Нет связи с сервером"
    data = result.data if isinstance(result.data, dict) else {}
    err_code = str(data.get("error") or data.get("code") or "").lower()
    message = str(data.get("message") or data.get("detail") or "")
    if isinstance(result.data, dict) and "ok" in data:
        ok_flag = bool(data.get("ok"))
    elif isinstance(result.data, dict) and "success" in data:
        ok_flag = bool(data.get("success"))
    else:
        ok_flag = None
    status = result.status

    if err_code in UNKNOWN_CODES:
        return "unknown", message or "Бейдж не найден"
    if status in (401, 403):
        return "retry", message or "Сервер отклонил ключ доступа"
    if status == 409:
        return "sent", message or "Уже зарегистрировано"
    if status in (408, 429) or (status is not None and status >= 500):
        return "retry", message or f"Сервер временно недоступен ({status})"
    if ok_flag is True or (ok_flag is None and status is not None and 200 <= status < 300):
        return "sent", message or "Отправлено"
    if ok_flag is False:
        if "не найден" in message.lower():
            return "unknown", message or "Бейдж не найден"
        return "rejected", message or "Сервер отклонил событие"
    if status == 404:
        return "retry", "Адрес приёма событий не найден (404). Проверьте путь в настройках."
    if status is not None and 400 <= status < 500:
        return "rejected", message or f"Ошибка запроса ({status})"
    return "retry", result.error or message or "Неизвестная ошибка"


def _without_secrets(value):
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if str(key).lower() != "password"}
    return value


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _event_moment(value) -> datetime:
    """Время самой отметки, а не время, когда очередь дошла до сервера."""
    parsed = parse_iso(str(value or ""))
    if parsed is None:
        return datetime.now().astimezone()
    return parsed


def _session_rank(row: dict) -> tuple:
    text = str(row.get("id") or "")
    if text.isdigit():
        return (1, int(text))
    return (0, 0)


def _open_session(sessions: list, user_id) -> dict | None:
    wanted = str(user_id)
    rows = [
        row
        for row in sessions
        if isinstance(row, dict) and str(row.get("userId")) == wanted and is_open(row)
    ]
    if not rows:
        return None
    rows.sort(key=_session_rank)
    return rows[-1]


def _badge_indexes(rows: list) -> tuple[dict, dict]:
    """Точный код и код без регистра. При повторе точный берётся первый, прочие — последний."""
    exact: dict = {}
    folded: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        current = str(row.get("badgeId") or "").strip()
        if not current:
            continue
        exact.setdefault(current, row)
        folded[current.casefold()] = row
    return exact, folded


def _open_index(rows: list) -> dict:
    """Открытая смена с наибольшим номером для каждого сотрудника."""
    best: dict = {}
    rank: dict = {}
    for row in rows:
        if not isinstance(row, dict) or not is_open(row):
            continue
        key = str(row.get("userId"))
        score = _session_rank(row)
        previous = rank.get(key)
        if previous is None or score >= previous:
            rank[key] = score
            best[key] = row
    return best


def _session_started(session: dict, moment: datetime) -> datetime | None:
    """Начало смены в той же зоне, что и отметка. В базе лежит местное время без пояса."""
    day = normalize_date(str(session.get("date") or ""))
    clock = normalize_clock(str(session.get("startTime") or ""))
    if not day or not clock:
        return None
    try:
        parsed = datetime.strptime(day + "T" + clock, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    if moment.tzinfo is not None:
        return parsed.replace(tzinfo=moment.tzinfo)
    return parsed


def _recorded_minutes(start_date: str, start_time: str, end_date: str, end_time: str) -> int | None:
    """Минуты закрытия. Повтор в ту же минуту не должен оставлять смену на ноль."""
    minutes = duration_minutes(start_date, start_time, end_date, end_time)
    if minutes is None:
        return None
    if minutes <= 0:
        return 1
    return minutes


def _shift_is_stale(session: dict, moment: datetime, max_hours: float) -> bool:
    """Смена открыта дольше окна: это уже не текущий рабочий день."""
    started = _session_started(session, moment)
    if started is None:
        return False
    return moment - started > timedelta(hours=float(max_hours))


def _merge_by_id(primary: list, extra: list) -> list:
    merged = []
    seen = set()
    for row in list(primary) + list(extra):
        if not isinstance(row, dict):
            continue
        key = str(row.get("id") or "")
        if key:
            if key in seen:
                continue
            seen.add(key)
        merged.append(row)
    return merged


def _checkin_unknown(result: HttpResult) -> bool:
    data = result.data if isinstance(result.data, dict) else {}
    code = str(data.get("error") or data.get("code") or "").lower()
    message = str(data.get("message") or "").lower()
    if code in UNKNOWN_CODES:
        return True
    return result.ok and ("не найден" in message or data.get("success") is False and "найден" in message)


def _checkin_failed(result: HttpResult) -> bool:
    data = result.data if isinstance(result.data, dict) else {}
    if data.get("success") is False or data.get("ok") is False:
        return True
    return False


def _checkin_needs_direct_write(result: HttpResult) -> bool:
    if result.network_error or result.status in (401, 403, 408, 429):
        return True
    if result.status is not None and result.status >= 500:
        return True
    return False


class ApiClient:
    def __init__(self, settings: dict, transport=None) -> None:
        self.settings = settings
        self._transport = transport or _default_transport
        self._session_token = ""
        self._session_at = 0.0
        self._auth_error = ""
        self._cache_lock = threading.Lock()
        self._users_cache: tuple | None = None
        self._sessions_cache: tuple | None = None
        # Поколение списка смен. Запись во время долгой загрузки делает ответ устаревшим.
        self._sessions_epoch = 0
        # Смены, записанные только что. Старый ответ сервера не должен их затирать.
        self._held_sessions: dict = {}
        self._wall_until = 0.0
        self._cached_server = self._server_key()
        # Один и тот же список не скачивается двумя потоками сразу.
        self._flight_lock = threading.Lock()
        self._flights: dict[str, threading.Event] = {}

    def update(self, settings: dict) -> None:
        """Новые настройки. Справочник сбрасывается только если сменился сервер."""
        self.settings = settings
        identity = self._server_key()
        with self._cache_lock:
            if identity != self._cached_server:
                self._cached_server = identity
                self._users_cache = None
                self._sessions_cache = None
                self._wall_until = 0.0

    def _server_key(self) -> tuple:
        server = self.settings.get("server") or {}
        return (
            str(server.get("profile") or ""),
            self.read_url(),
            self.write_url(),
            str(server.get("supabase_url") or "").strip(),
            str(server.get("supabase_key") or "").strip(),
        )

    def _cache_get(self, kind: str) -> list | None:
        with self._cache_lock:
            slot = self._users_cache if kind == "users" else self._sessions_cache
            if slot is None:
                return None
            saved_at = slot[0]
            if time.monotonic() - saved_at > DIRECTORY_CACHE_SEC:
                return None
            return copy.deepcopy(slot[1])

    def _cache_put(self, kind: str, rows: list, identity: tuple, epoch: int | None = None) -> bool:
        with self._cache_lock:
            if identity != self._cached_server:
                return False
            # Скачивание началось раньше, чем смена записалась. Старый список её сотрёт.
            if kind == "sessions" and epoch is not None and epoch != self._sessions_epoch:
                return False
            stored = copy.deepcopy(rows)
            if kind == "users":
                self._users_cache = (time.monotonic(), stored, _badge_indexes(stored))
            else:
                stored = self._apply_held_locked(stored)
                self._sessions_cache = (time.monotonic(), stored, _open_index(stored))
            return True

    def _hold_locked(self, row: dict) -> None:
        session_id = str(row.get("id") or "").strip()
        if not session_id:
            return
        self._held_sessions[session_id] = (time.monotonic(), copy.deepcopy(row))

    def _apply_held_locked(self, rows: list) -> list:
        now = time.monotonic()
        expired = [
            session_id
            for session_id, (saved_at, _row) in self._held_sessions.items()
            if now - saved_at > DIRECTORY_CACHE_SEC
        ]
        for session_id in expired:
            self._held_sessions.pop(session_id, None)
        merged = [row for row in rows if isinstance(row, dict)]
        for session_id, (_saved_at, row) in self._held_sessions.items():
            updated = []
            found = False
            for item in merged:
                if str(item.get("id") or "") == session_id:
                    updated.append(copy.deepcopy(row))
                    found = True
                else:
                    updated.append(item)
            if not found:
                updated.append(copy.deepcopy(row))
            merged = updated
        return merged

    def _prefer_fresh_sessions(self, result: HttpResult) -> HttpResult:
        """Если за время загрузки смену уже записали, наружу отдаём свежую память, а не старый ответ."""
        if not isinstance(getattr(result, "data", None), list):
            return result
        cached = self._cache_get("sessions")
        if cached is not None:
            result.data = cached
            return result
        with self._cache_lock:
            result.data = self._apply_held_locked(result.data)
        return result

    def _skip_express(self) -> bool:
        """Хостинг уже ответил стеной Vercel: пока не стучаться в него на каждого человека."""
        with self._cache_lock:
            closed = time.monotonic() < self._wall_until
        return closed and all(self._hosted_database())

    def _note_wall(self, result: HttpResult) -> None:
        # Обычный отказ без ключа — не стена. Иначе отметки перестанут ходить на рабочий сервер.
        if not self._blocked(result):
            return
        with self._cache_lock:
            self._wall_until = time.monotonic() + CLOSED_HOST_SEC

    def open_shift_count(self) -> int | None:
        """Сколько сотрудников с открытой сменой уже лежит в памяти. Сеть не вызывается."""
        with self._cache_lock:
            slot = self._sessions_cache
            if slot is None or len(slot) < 3 or not isinstance(slot[2], dict):
                return None
            return sum(1 for key in slot[2] if str(key).strip())

    def _cache_alive(self, kind: str) -> bool:
        with self._cache_lock:
            slot = self._users_cache if kind == "users" else self._sessions_cache
            if slot is None:
                return False
            return time.monotonic() - slot[0] <= DIRECTORY_CACHE_SEC

    def _drop_sessions(self) -> None:
        with self._cache_lock:
            self._sessions_cache = None
            self._sessions_epoch += 1
            self._held_sessions.clear()

    def _peek_sessions(self) -> tuple[float, list] | None:
        with self._cache_lock:
            slot = self._sessions_cache
            if slot is None:
                return None
            return slot[0], copy.deepcopy(slot[1])

    def _restore_sessions(self, peeked: tuple[float, list] | None, only_if_held: bool = False) -> None:
        """Вернуть список смен, если сервер его не изменил или строка уже вписана в память.

        Полное скачивание перед следующим сканом держит экран. Срок жизни списка не продлевается.
        """
        if not peeked:
            return
        saved_at, rows = peeked
        with self._cache_lock:
            if self._sessions_cache is not None:
                return
            if only_if_held and not self._held_sessions:
                return
            if time.monotonic() - saved_at > DIRECTORY_CACHE_SEC:
                return
            merged = self._apply_held_locked(rows)
            self._sessions_cache = (saved_at, merged, _open_index(merged))

    def _join_flight(self, key: str) -> tuple[bool, threading.Event]:
        with self._flight_lock:
            current = self._flights.get(key)
            if current is not None and not current.is_set():
                return False, current
            event = threading.Event()
            self._flights[key] = event
            return True, event

    def _finish_flight(self, key: str, event: threading.Event) -> None:
        event.set()
        with self._flight_lock:
            if self._flights.get(key) is event:
                self._flights.pop(key, None)

    def _load_shared(self, key: str, kind: str, loader) -> HttpResult:
        """Второй поток ждёт уже идущую загрузку и берёт её из памяти."""
        leader, event = self._join_flight(key)
        if not leader:
            event.wait()
            cached = self._cache_get(kind)
            if cached is not None:
                return HttpResult(True, 200, cached, None, False)
            leader, event = self._join_flight(key)
            if not leader:
                event.wait()
                cached = self._cache_get(kind)
                if cached is not None:
                    return HttpResult(True, 200, cached, None, False)
                return loader()
        try:
            return loader()
        finally:
            self._finish_flight(key, event)

    @property
    def is_site(self) -> bool:
        return str((self.settings.get("server") or {}).get("profile") or "generic") == "site"

    def read_url(self) -> str:
        return server_urls(self.settings.get("server") or {})[0]

    def write_url(self) -> str:
        return server_urls(self.settings.get("server") or {})[1]

    def health_state(self) -> str:
        """online / offline / unset."""
        server = self.settings.get("server") or {}
        base = self.read_url() if self.is_site else str(server.get("base_url") or "").strip()
        if not base and not (self.is_site and self._hosted_database()[0]):
            return "unset"
        if self.is_site:
            # Пока списки свежие, проверка связи не копирует их и не ходит в сеть.
            if not self._cache_alive("users"):
                users = self._directory_users()
                if not isinstance(users.data, list) or self._blocked(users):
                    return "offline"
            if not self._cache_alive("sessions"):
                self._directory_sessions()
            return "online"
        path = str((server.get("paths") or {}).get("health") or "").strip() or "/"
        result = self._request("GET", build_url(base, path))
        if result.network_error or result.status is None or result.status >= 400 or self._blocked(result):
            return "offline"
        return "online"

    def probe(self) -> str:
        """Текст для админки: связь и сколько людей в справочнике сайта."""
        server = self.settings.get("server") or {}
        base = self.read_url() if self.is_site else str(server.get("base_url") or "").strip()
        if not base:
            return "Адрес получения данных пустой. Отметки будут копиться на этом компьютере."
        if not self.is_site:
            users = self._request("GET", build_url(base, str((server.get("paths") or {}).get("health") or "/")))
            if users.network_error:
                return users.error or "Сервер не ответил."
            if users.status is not None and users.status < 400:
                return "Сервер ответил. Связь есть."
            return "Сервер не ответил. Проверьте адрес, сеть и путь проверки связи."
        users, sessions = self.load_people(refresh=True)
        if isinstance(users.data, list) and not self._blocked(users):
            people = [row for row in users.data if isinstance(row, dict)]
            with_badge = [row for row in people if str(row.get("badgeId") or "").strip()]
            at_work = 0
            if isinstance(sessions.data, list):
                at_work = len(
                    {
                        str(row.get("userId"))
                        for row in sessions.data
                        if isinstance(row, dict) and is_open(row)
                    }
                )
            return f"В справочнике {len(people)} человек, с бейджем {len(with_badge)}, сейчас на работе {at_work}."
        if users.network_error and not self._blocked(users):
            return users.error or "Сервер не ответил."
        if self._blocked(users):
            return self._wall_message()
        return f"Сервер ответил не списком сотрудников (код {users.status})."

    def get_employee(self, badge: str) -> HttpResult | None:
        if self.is_site:
            return self._site_employee(badge)
        url = self._url("employee", badge)
        if not url:
            return None
        return self._request("GET", url)

    def get_status(self, badge: str) -> HttpResult | None:
        if self.is_site:
            return self._site_status(badge)
        url = self._url("status", badge)
        if not url:
            return None
        return self._request("GET", url)

    def load_people(self, refresh: bool = False) -> tuple[HttpResult, HttpResult]:
        """Сотрудники и смены сайта. Пароли в ответ не входят.

        Сначала читает адрес получения данных. Если хостинг закрыт страницей Vercel,
        берёт тот же список из общей базы, которую показывает сайт.
        Окно «Смены» передаёт refresh=True, чтобы не показать устаревший список.
        """
        empty = HttpResult(False, None, None, "Смены не загружены", True)
        if not self.is_site:
            return HttpResult(False, None, None, "Список сотрудников доступен в протоколе сайта учёта.", False), empty
        users = self._directory_users(refresh=refresh)
        if not isinstance(users.data, list) or self._blocked(users):
            return users, empty
        sessions = self._directory_sessions(refresh=refresh)
        if not isinstance(sessions.data, list) or self._blocked(sessions):
            sessions = HttpResult(True, users.status, [], None, False)
        return users, sessions

    def _directory_users(self, refresh: bool = False) -> HttpResult:
        if not refresh:
            cached = self._cache_get("users")
            if cached is not None:
                return HttpResult(True, 200, cached, None, False)
        return self._load_shared("users", "users", self._fetch_users)

    def _fetch_users(self) -> HttpResult:
        identity = self._server_key()
        users = None
        if not self._skip_express():
            users = self._load_users()
            self._note_wall(users)
            if isinstance(users.data, list) and not users.network_error and not self._blocked(users):
                self._cache_put("users", users.data, identity)
                return users
        hosted = self._supabase_get("users", "id,fullName,login,email,badgeId,role")
        if isinstance(hosted.data, list) and not self._blocked(hosted):
            self._cache_put("users", hosted.data, identity)
            return hosted
        if users is not None:
            return users
        return HttpResult(False, None, None, "Список сотрудников недоступен", True)

    def _directory_sessions(self, refresh: bool = False) -> HttpResult:
        if not refresh:
            cached = self._cache_get("sessions")
            if cached is not None:
                return HttpResult(True, 200, cached, None, False)
        with self._cache_lock:
            epoch = self._sessions_epoch
        return self._load_shared(f"sessions:{epoch}", "sessions", lambda epoch=epoch: self._fetch_sessions(epoch))

    def _fetch_sessions(self, epoch: int) -> HttpResult:
        identity = self._server_key()
        sessions = None
        if not self._skip_express():
            sessions = self._request("GET", build_url(self.read_url(), "/workSessions"), send_auth=False)
            self._note_wall(sessions)
            if isinstance(sessions.data, list) and not self._blocked(sessions):
                sessions.data = [_without_secrets(row) for row in sessions.data]
                sessions.raw_text = ""
                self._cache_put("sessions", sessions.data, identity, epoch)
                return self._prefer_fresh_sessions(sessions)
        hosted = self._supabase_get(
            "workSessions",
            "id,userId,date,startTime,endTime,durationMinutes,status,source,editedBy",
            order="id.desc",
        )
        if isinstance(hosted.data, list) and not self._blocked(hosted):
            # У базы потолок 1000 строк. Старая открытая смена иначе выпадает
            # из последних записей, и следующий скан открывает вторую.
            if len(hosted.data) >= 1000:
                opened = self._supabase_get(
                    "workSessions",
                    "id,userId,date,startTime,endTime,durationMinutes,status,source,editedBy",
                    extra_query="or=(status.eq.active,endTime.is.null)",
                )
                if isinstance(opened.data, list) and not self._blocked(opened):
                    hosted.data = _merge_by_id(hosted.data, opened.data)
            self._cache_put("sessions", hosted.data, identity, epoch)
            return self._prefer_fresh_sessions(hosted)
        if sessions is not None:
            return sessions
        return HttpResult(False, None, None, "Смены недоступны", True)

    def write_session(self, op: dict) -> HttpResult:
        """Создать или изменить смену. Если хостинг закрыт, пишет в ту же базу, что и сайт."""
        if not self.is_site:
            return HttpResult(False, None, None, "Запись смен работает в протоколе сайта учёта.", False)
        express = None
        if not self._skip_express():
            express = self._express_write(op)
            self._note_wall(express)
            if express.ok and not self._blocked(express):
                self._remember_session(op, express.data)
                return express
            retry = express.network_error or self._blocked(express) or express.status in (401, 403, 408, 429) or (
                express.status is not None and express.status >= 500
            )
            if not retry:
                return express
        hosted = self._supabase_write(op)
        if hosted is not None:
            if hosted.ok and not self._blocked(hosted):
                self._remember_session(op, hosted.data)
            return hosted
        if express is not None:
            return express
        return HttpResult(False, None, None, "Общая база сайта не настроена", True)

    def _remember_session(self, op: dict, data) -> None:
        """Вписать только что сохранённую смену в память, не скачивая весь список заново."""
        if not isinstance(data, dict):
            return
        payload = _json_object(op.get("payload"))
        op_name = str(op.get("op") or "")
        with self._cache_lock:
            slot = self._sessions_cache
            rows = [] if slot is None else copy.deepcopy(slot[1])
            if op_name == "create":
                row = {key: value for key, value in payload.items() if str(key).lower() != "password"}
                row.update({key: value for key, value in data.items() if str(key).lower() != "password"})
                if not str(row.get("id") or "").strip():
                    return
                keep_shift_span(row, payload)
                rows = [item for item in rows if str(item.get("id")) != str(row.get("id"))]
                rows.append(row)
                self._hold_locked(row)
            else:
                session_id = str(data.get("id") or op.get("session_id") or "").strip()
                if not session_id:
                    return
                found = False
                updated = []
                kept = None
                for item in rows:
                    if str(item.get("id")) != session_id:
                        updated.append(item)
                        continue
                    merged = dict(item)
                    merged.update({key: value for key, value in payload.items() if str(key).lower() != "password"})
                    merged.update({key: value for key, value in data.items() if str(key).lower() != "password"})
                    keep_shift_span(merged, payload)
                    updated.append(merged)
                    kept = merged
                    found = True
                if not found:
                    kept = {key: value for key, value in payload.items() if str(key).lower() != "password"}
                    kept.update({key: value for key, value in data.items() if str(key).lower() != "password"})
                    kept["id"] = session_id
                    keep_shift_span(kept, payload)
                    updated.append(kept)
                rows = updated
                if kept is not None:
                    self._hold_locked(kept)
            # Даже без готового списка загрузка, которая уже идёт, не должна его подменить.
            self._sessions_epoch += 1
            if slot is None:
                return
            self._sessions_cache = (time.monotonic(), rows, _open_index(rows))

    def _express_write(self, op: dict) -> HttpResult:
        base = self.write_url()
        if not base:
            return HttpResult(False, None, None, "Адрес отправки данных пустой", True)
        payload = _json_object(op.get("payload"))
        if str(op.get("op") or "") == "create":
            return self._request("POST", build_url(base, "/workSessions"), payload)
        session_id = str(op.get("session_id") or "").strip()
        if not session_id:
            return HttpResult(False, None, None, "Нет номера смены", False)
        return self._request("PATCH", build_url(base, "/workSessions/" + quote(session_id, safe="")), payload)

    def _hosted_database(self) -> tuple[str, str]:
        server = self.settings.get("server") or {}
        return str(server.get("supabase_url") or "").strip().rstrip("/"), str(server.get("supabase_key") or "").strip()

    def _supabase_headers(self) -> dict | None:
        _base, key = self._hosted_database()
        if not key:
            return None
        return {"apikey": key, "Authorization": "Bearer " + key}

    def canonical_badge(self, badge: str) -> str:
        """Код как в справочнике, чтобы ручной ввод и скан попали в одну смену."""
        text = str(badge or "").strip()
        if not self.is_site or not text:
            return text
        found, _result = self._find_user(text)
        if not isinstance(found, dict):
            return text
        current = str(found.get("badgeId") or "").strip()
        return current or text

    def _supabase_get(self, table: str, select: str, order: str = "", extra_query: str = "") -> HttpResult:
        base, key = self._hosted_database()
        if not base or not key:
            return HttpResult(False, None, None, "Общая база сайта не настроена", True)
        url = base + "/rest/v1/" + table + "?select=" + quote(select, safe=",") + "&limit=1000"
        if order:
            url += "&order=" + quote(order, safe=".")
        if extra_query:
            url += "&" + extra_query
        result = self._request("GET", url, send_auth=False, extra_headers=self._supabase_headers())
        if isinstance(result.data, list):
            result.data = [_without_secrets(row) for row in result.data]
            result.raw_text = ""
        return result

    def _supabase_write(self, op: dict) -> HttpResult | None:
        base, key = self._hosted_database()
        if not base or not key:
            return None
        payload = _json_object(op.get("payload"))
        allowed = ("userId", "date", "startTime", "endTime", "durationMinutes", "status", "source", "editedBy")
        body = {name: payload[name] for name in allowed if name in payload}
        headers = self._supabase_headers() or {}
        headers["Prefer"] = "return=representation"
        if str(op.get("op") or "") == "create":
            result = self._request("POST", base + "/rest/v1/workSessions", body, send_auth=False, extra_headers=headers)
        else:
            session_id = quote(str(op.get("session_id") or "").strip(), safe="")
            if not session_id:
                return HttpResult(False, None, None, "Нет номера смены", False)
            result = self._request(
                "PATCH",
                base + "/rest/v1/workSessions?id=eq." + session_id,
                body,
                send_auth=False,
                extra_headers=headers,
            )
        if isinstance(result.data, list):
            if not result.data:
                # Пустой список при return=representation — строка не записана.
                # Код 2xx иначе выглядит как успех, и смена остаётся открытой.
                result.ok = False
                if result.status is not None and 200 <= result.status < 300:
                    result.status = 422
                result.error = "Сервер не вернул строку смены"
                result.data = {"message": result.error}
                result.raw_text = ""
                return result
            first = result.data[0] if isinstance(result.data[0], dict) else {}
            result.data = _without_secrets(first) if first else {}
            result.raw_text = ""
        return result

    def post_event(self, payload: dict) -> HttpResult:
        if self.is_site:
            return self._post_checkin(payload)
        url = self._url("event", None)
        if not url:
            return HttpResult(False, None, None, "Сервер не настроен", True)
        return self._request("POST", url, payload)

    def _load_users(self) -> HttpResult:
        base = self.read_url()
        if not base:
            return HttpResult(False, None, None, "Сервер не настроен", True)
        result = self._request("GET", build_url(base, "/users"), send_auth=False)
        if isinstance(result.data, list):
            result.data = [_without_secrets(row) for row in result.data]
            result.raw_text = ""
        return result

    def _site_employee(self, badge: str) -> HttpResult:
        found, result = self._find_user(badge)
        if found is None and (result.network_error or self._blocked(result) or not isinstance(result.data, list)):
            return self._closed(result)
        if found is None:
            return HttpResult(False, 404, {"error": "unknown_badge", "message": "Сотрудник не найден"}, None, False)
        name = str(found.get("fullName") or found.get("login") or "").strip()
        return HttpResult(True, 200, {"name": name, "fullName": name, "active": True}, None, False)

    def _site_status(self, badge: str) -> HttpResult:
        found, result = self._find_user(badge)
        if found is None and (result.network_error or self._blocked(result) or not isinstance(result.data, list)):
            return self._closed(result)
        if found is None:
            return HttpResult(False, 404, {"error": "unknown_badge", "message": "Сотрудник не найден"}, None, False)
        opened, failure = self._open_for_user(found.get("id"))
        if failure is not None:
            return self._closed(failure)
        name = str(found.get("fullName") or "").strip()
        payload = {"last_event": "in" if opened else "out", "name": name, "fullName": name}
        if opened is not None:
            started = _session_started(opened, datetime.now().astimezone())
            if started is not None:
                payload["last_time"] = started.isoformat(timespec="seconds")
        return HttpResult(True, 200, payload, None, False)

    def _find_user(self, badge: str) -> tuple[dict | None, HttpResult]:
        wanted = str(badge or "").strip()
        folded = wanted.casefold()
        with self._cache_lock:
            slot = self._users_cache
            fresh = slot is not None and time.monotonic() - slot[0] <= DIRECTORY_CACHE_SEC
            found = None
            if fresh:
                exact, folded_index = slot[2]
                found = exact.get(wanted) if wanted else None
                if found is None and wanted:
                    found = folded_index.get(folded)
                found = dict(found) if isinstance(found, dict) else None
        if fresh:
            return found, HttpResult(True, 200, [], None, False)
        result = self._directory_users()
        if not isinstance(result.data, list):
            return None, result
        fallback = None
        for row in result.data:
            if not isinstance(row, dict):
                continue
            current = str(row.get("badgeId") or "").strip()
            if current == wanted:
                return row, result
            if current and current.casefold() == folded:
                fallback = row
        return fallback, result

    def _open_for_user(self, user_id) -> tuple[dict | None, HttpResult | None]:
        """Открытая смена из памяти. Второй элемент — сбой загрузки, не «смены нет»."""
        wanted = str(user_id)
        with self._cache_lock:
            slot = self._sessions_cache
            fresh = slot is not None and time.monotonic() - slot[0] <= DIRECTORY_CACHE_SEC
            found = None
            if fresh:
                found = slot[2].get(wanted)
                found = dict(found) if isinstance(found, dict) else None
        if fresh:
            return found, None
        sessions = self._directory_sessions()
        if sessions.network_error or self._blocked(sessions) or not isinstance(sessions.data, list):
            return None, sessions
        opened = _open_session(sessions.data, user_id)
        return (dict(opened) if opened else None), None

    def _post_checkin(self, payload: dict) -> HttpResult:
        if self._skip_express():
            direct = self._write_checkin_shift(payload)
            if direct is not None:
                return direct
        opened_before = self._tracked_open_shift(payload)
        # Приём сканера видит только status=active и не применяет предел 16 часов.
        # Такую смену закрываем сами, а зависшую ещё и сменяем новой.
        if self._checkin_bypasses_express(payload, opened_before):
            direct = self._write_checkin_shift(payload, direct=True)
            if direct is not None:
                return direct
        express = self._express_checkin(payload)
        self._note_wall(express)
        if express.ok and not self._blocked(express) and not _checkin_failed(express):
            # Приём сканера ставит своё время. Новый приход подгоняем под скан.
            # Повтор уже открытой смены не двигает её приход и не считается новым.
            # Список не выбрасываем насовсем: иначе следующий скан ждёт скачивание до экрана.
            peeked = self._peek_sessions()
            self._drop_sessions()
            repeat_at = self._repeat_arrival(payload, express, opened_before)
            if repeat_at is not None:
                self._restore_sessions(peeked)
                data = express.data if isinstance(express.data, dict) else {}
                express.data = data
                data["action"] = "repeat"
                data["alreadyWorking"] = True
                data["startTime"] = repeat_at.strftime("%H:%M")
                data["date"] = repeat_at.strftime("%Y-%m-%d")
                _stamp_mark(data, "repeat", repeat_at)
                return express
            try:
                self._align_express_clock(payload, express, opened_before)
            except Exception:
                logger().exception("Не удалось записать время отметки в смену")
            self._restore_sessions(peeked, only_if_held=True)
            return express
        if _checkin_unknown(express):
            return express
        if _checkin_needs_direct_write(express):
            direct = self._write_checkin_shift(payload)
            if direct is not None:
                return direct
        return express

    def _express_checkin(self, payload: dict) -> HttpResult:
        base = self.write_url()
        if not base:
            return HttpResult(False, None, None, "Сервер не настроен", True)
        badge = str(payload.get("badge") or payload.get("badgeId") or "").strip()
        url = build_url(base, "/api/qr-checkin")
        token = self.ensure_token()
        if not token:
            message = self._auth_error or "Для отметки нужен ключ доступа. Запись останется в очереди."
            return HttpResult(False, 401, {"message": message}, None, False)
        result = self._request("POST", url, {"badgeId": badge}, bearer=token)
        if result.status == 401:
            self._session_token = ""
            self._session_at = 0.0
            token = self.ensure_token()
            if token:
                result = self._request("POST", url, {"badgeId": badge}, bearer=token)
        return result

    def _checkin_bypasses_express(self, payload: dict, opened: dict | None) -> bool:
        """Прямая запись, если приём сканера эту смену не закроет как надо."""
        if not isinstance(opened, dict):
            return False
        moment = _event_moment(payload.get("timestamp"))
        if _shift_is_stale(opened, moment, self._shift_max_hours()):
            return True
        return str(opened.get("status") or "").strip().lower() != "active"

    def _write_checkin_shift(self, payload: dict, direct: bool = False) -> HttpResult | None:
        """Та же запись смены, что сработала вчера из окна «Смены», если приём сканера закрыт."""
        if not all(self._hosted_database()):
            if not (direct and self.write_url()):
                return None
        badge = str(payload.get("badge") or payload.get("badgeId") or "").strip()
        found, result = self._find_user(badge)
        if found is None and (result.network_error or self._blocked(result) or not isinstance(result.data, list)):
            return self._closed(result)
        if found is None:
            return HttpResult(False, 404, {"error": "unknown_badge", "message": "Сотрудник не найден"}, None, False)
        opened, failure = self._open_for_user(found.get("id"))
        if failure is not None:
            return self._closed(failure)
        name = str(found.get("fullName") or found.get("login") or "").strip()
        event = str(payload.get("event") or "").strip().lower()
        if event not in ("in", "out"):
            event = "out" if opened is not None else "in"
        moment = _event_moment(payload.get("timestamp"))
        logger().info("Отметка %s пишется в базу сайта", badge[:80])
        if event == "in" and opened is not None:
            if _shift_is_stale(opened, moment, self._shift_max_hours()):
                # Вчерашняя смена так и висит. Закрываем её и открываем новую,
                # иначе человек остаётся «на работе» со вчерашнего прихода.
                closed = self._close_shift_row(opened, moment)
                if closed is None or not closed.ok or self._blocked(closed):
                    return closed
                opened = None
            else:
                # Свежая смена уже открыта: повторный приход её закрывает, а не дублирует.
                event = "out"
        if event == "in":
            user_id = found.get("id")
            text = str(user_id if user_id is not None else "").strip()
            written = self.write_session(
                {
                    "op": "create",
                    "payload": {
                        "userId": int(text) if text.isdigit() else user_id,
                        "date": moment.strftime("%Y-%m-%d"),
                        "startTime": moment.strftime("%H:%M:%S"),
                        "endTime": None,
                        "durationMinutes": None,
                        "status": "active",
                        "source": "qr",
                    },
                }
            )
        else:
            current = opened
            if current is None or not str(current.get("id") or "").strip():
                return HttpResult(False, None, None, "Открытая смена ещё не появилась на сервере.", True)
            written = self._close_shift_row(current, moment)
        if written is not None and written.ok and isinstance(written.data, dict):
            written.data["fullName"] = name or extract_name(written.data)
            written.data["name"] = written.data["fullName"]
            written.data["action"] = "start" if event == "in" else "end"
            written.data["event"] = event
            _stamp_mark(written.data, event, moment)
        return written

    def _shift_max_hours(self) -> float:
        try:
            return float((self.settings.get("attendance") or {}).get("shift_max_hours") or 16)
        except (TypeError, ValueError):
            return 16.0

    def _close_shift_row(self, session: dict, moment: datetime) -> HttpResult:
        end_day = moment.strftime("%Y-%m-%d")
        end_clock = moment.strftime("%H:%M:%S")
        minutes = _recorded_minutes(
            str(session.get("date") or ""),
            str(session.get("startTime") or ""),
            end_day,
            end_clock,
        )
        if minutes is None:
            # Раньше сюда писался ноль. Повтор по сети тоже нельзя: отметка так и останется в очереди.
            return HttpResult(
                False,
                422,
                {"message": "Не удалось посчитать длительность смены."},
                "Не удалось посчитать длительность смены.",
                False,
            )
        return self.write_session(
            {
                "op": "update",
                "session_id": str(session.get("id")),
                "payload": {
                    "endDate": end_day,
                    "endTime": end_clock,
                    "durationMinutes": minutes,
                    "status": "completed",
                    "source": "qr",
                },
            }
        )

    def _tracked_open_shift(self, payload: dict) -> dict | None:
        """Открытая смена до отметки. Нужен её номер, чтобы потом записать время ухода."""
        try:
            badge = str(payload.get("badge") or payload.get("badgeId") or "").strip()
            found, result = self._find_user(badge)
            if not isinstance(found, dict) or result.network_error or self._blocked(result):
                return None
            opened, failure = self._open_for_user(found.get("id"))
            if failure is not None or not isinstance(opened, dict):
                return None
            if not str(opened.get("id") or "").strip():
                return None
            return opened
        except Exception:
            logger().exception("Не удалось прочитать открытую смену перед отметкой")
            return None

    def _repeat_arrival(self, payload: dict, express: HttpResult, opened_before: dict | None) -> datetime | None:
        """Уже открытая смена, которую этот скан не начал заново. None — это обычный приход или уход."""
        if not isinstance(opened_before, dict):
            return None
        data = express.data if isinstance(express.data, dict) else {}
        action = str(data.get("action") or "").strip().lower()
        if action in ("end", "out"):
            return None
        scan = _event_moment(payload.get("timestamp"))
        started = _session_started(opened_before, scan)
        if started is None or _shift_is_stale(opened_before, scan, self._shift_max_hours()):
            return None
        same_minute = started.strftime("%H:%M") == scan.strftime("%H:%M") and started.date() == scan.date()
        if same_minute:
            return None
        echoed = _hm(data.get("startTime"))
        prior = started.strftime("%H:%M")
        if echoed and echoed == prior:
            return started
        if action not in ("start", "in"):
            return None
        current = self._tracked_open_shift(payload)
        if not isinstance(current, dict):
            return None
        if str(current.get("id") or "") != str(opened_before.get("id") or ""):
            return None
        current_start = _session_started(current, scan)
        if current_start is None:
            return None
        if current_start.strftime("%H:%M") == scan.strftime("%H:%M") and current_start.date() == scan.date():
            return None
        return current_start

    def _align_express_clock(self, payload: dict, express: HttpResult, opened_before: dict | None) -> None:
        """Сервер отмечает своим временем. Новый приход и уход в смене остаются на часах скана."""
        data = express.data if isinstance(express.data, dict) else {}
        action = str(data.get("action") or "").strip().lower()
        if action not in ("start", "end"):
            return
        moment = _event_moment(payload.get("timestamp"))
        day = moment.strftime("%Y-%m-%d")
        clock = moment.strftime("%H:%M:%S")
        if action == "end":
            session = opened_before if isinstance(opened_before, dict) else None
            session_id = str((session or {}).get("id") or "").strip()
            if not session_id:
                return
            minutes = _recorded_minutes(
                str(session.get("date") or ""),
                str(session.get("startTime") or ""),
                day,
                clock,
            )
            if minutes is None:
                return
            if str(session.get("endTime") or "") == clock and session.get("durationMinutes") == minutes:
                _stamp_mark(data, "out", moment)
                return
            written = self.write_session(
                {
                    "op": "update",
                    "session_id": session_id,
                    "payload": {
                        "endDate": day,
                        "endTime": clock,
                        "durationMinutes": minutes,
                        "status": "completed",
                        "source": "qr",
                    },
                }
            )
            if written is not None and written.ok:
                _stamp_mark(data, "out", moment)
            return
        badge = str(payload.get("badge") or payload.get("badgeId") or "").strip()
        found, result = self._find_user(badge)
        if not isinstance(found, dict) or result.network_error or self._blocked(result):
            return
        sessions = self._directory_sessions(refresh=True)
        if not isinstance(sessions.data, list):
            return
        opened = _open_session(sessions.data, found.get("id"))
        if not isinstance(opened, dict) or not str(opened.get("id") or "").strip():
            return
        if str(opened.get("date") or "") == day and str(opened.get("startTime") or "") == clock:
            _stamp_mark(data, "in", moment)
            return
        written = self.write_session(
            {
                "op": "update",
                "session_id": str(opened.get("id")),
                "payload": {"date": day, "startTime": clock, "source": "qr"},
            }
        )
        if written is not None and written.ok:
            _stamp_mark(data, "in", moment)

    def operator_profiles(self) -> tuple[list[dict], str]:
        """Профили, которыми можно отмечать смены. Пароли в список не входят."""
        result = self._directory_users()
        if not isinstance(result.data, list) or self._blocked(result):
            if self._blocked(result):
                return [], self._wall_message()
            return [], result.error or "Список профилей недоступен."
        people = []
        for row in result.data:
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "").strip().lower()
            if role not in ("admin", "scanner"):
                continue
            people.append(
                {
                    "id": row.get("id"),
                    "fullName": str(row.get("fullName") or "").strip(),
                    "login": str(row.get("login") or "").strip(),
                    "email": str(row.get("email") or "").strip(),
                    "role": role,
                }
            )
        people.sort(key=lambda item: (0 if item["role"] == "admin" else 1, item["fullName"].casefold()))
        return people, ""

    def sign_in(self, login: str, password: str) -> HttpResult:
        """Вход профилем сайта. Пароль уходит только в теле запроса и в журнал не пишется."""
        login = str(login or "").strip()
        password = str(password or "")
        if not login or not password:
            return HttpResult(False, None, None, "Выберите профиль и введите пароль.", False)
        if not self.is_site:
            return HttpResult(False, None, None, "Вход в профиль нужен только для протокола сайта учёта.", False)
        url = build_url(self.write_url(), "/login")
        if not url:
            return HttpResult(False, None, None, "Адрес отправки данных пустой.", True)
        result = self._request("POST", url, {"email": login, "password": password}, send_auth=False)
        if self._blocked(result):
            self._session_token = ""
            self._session_at = 0.0
            return HttpResult(False, 401, {"message": self._login_wall_message()}, self._login_wall_message(), False, "")
        if result.network_error:
            self._session_token = ""
            self._session_at = 0.0
            return HttpResult(False, None, None, result.error or "Сервер не ответил.", True)
        data = result.data if isinstance(result.data, dict) else {}
        token = str(data.get("token") or "").strip()
        role = str(data.get("role") or "").strip().lower()
        if not result.ok or not token:
            self._session_token = ""
            self._session_at = 0.0
            self._auth_error = "Сервер не принял логин или пароль."
            return HttpResult(False, result.status, _without_secrets(data) if data else None, self._auth_error, result.network_error, "")
        if role not in ("admin", "scanner"):
            self._session_token = ""
            self._session_at = 0.0
            message = "Этот профиль не может отмечать смены. Нужен администратор или сканер."
            return HttpResult(False, 403, _without_secrets(data), message, False, "")
        self._session_token = token
        self._session_at = time.time()
        self._auth_error = ""
        safe = {key: value for key, value in _without_secrets(data).items() if str(key).lower() != "token"}
        return HttpResult(True, result.status, safe, None, False, "")

    def adopt_session(self, other: ApiClient) -> None:
        self._session_token = other._session_token
        self._session_at = other._session_at
        self._auth_error = ""

    def sign_out(self) -> None:
        self._session_token = ""
        self._session_at = 0.0
        self._auth_error = ""

    def ensure_token(self) -> str:
        server = self.settings.get("server") or {}
        manual = str(server.get("token") or "").strip()
        login = str(server.get("login") or "").strip()
        password = str(server.get("password") or "")
        if not self.is_site or not login or not password:
            return manual
        if self._session_token and time.time() - self._session_at < 50 * 60:
            return self._session_token
        result = self._login_request()
        if self._blocked(result):
            self._auth_error = self._login_wall_message()
            return ""
        token = str(result.data.get("token") or "") if isinstance(result.data, dict) else ""
        if token:
            self._session_token = token
            self._session_at = time.time()
            self._auth_error = ""
            return token
        self._auth_error = "Сервер не принял логин или пароль."
        return ""

    def _login_request(self) -> HttpResult:
        server = self.settings.get("server") or {}
        login = str(server.get("login") or "").strip()
        password = str(server.get("password") or "")
        url = build_url(self.write_url(), "/login")
        return self._request("POST", url, {"email": login, "password": password}, send_auth=False)

    def _login_wall_message(self) -> str:
        return (
            "Адрес отправки закрыт защитой Vercel: логин и пароль до сервера не дошли. "
            "Список сотрудников при этом уже открыт. "
            f"Вход заработает на локальном сервере ({LOCAL_API_URL}) или когда защиту Vercel снимут."
        )

    def _wall_message(self) -> str:
        if all(self._hosted_database()):
            return (
                "Рабочий адрес закрыт входом Vercel. "
                "Отметки и смены пишутся в ту же базу, которую показывает сайт."
            )
        return (
            "Рабочий адрес есть, но Vercel пускает туда только после входа в команду. "
            "Пока защита включена, сканер с этого компьютера туда не попадёт. "
            f"Локальный сервер пишет в ту же базу: {LOCAL_API_URL}"
        )

    def _closed(self, result: HttpResult) -> HttpResult:
        """Страница входа Vercel — это не справочник и не найденный сотрудник."""
        if self._blocked(result):
            return HttpResult(False, 401, {"message": "Сервер закрыт входом Vercel"}, "Сервер закрыт входом Vercel", False, "")
        return result

    def _blocked(self, result: HttpResult) -> bool:
        if isinstance(result.data, dict):
            protection = result.data.get("protection")
            if isinstance(protection, dict) and protection.get("vercel_auth_enabled"):
                return True
            error = result.data.get("error")
            error_message = str(error.get("message") or "") if isinstance(error, dict) else ""
            blob = (str(result.data.get("message") or "") + " " + error_message).lower()
            return "vercel" in blob or "protected deployment" in blob
        if isinstance(result.data, list):
            return False
        text = (result.raw_text or "").lower()
        return "<html" in text or "vercel" in text or "sso-api" in text

    def _url(self, path_name: str, badge: str | None) -> str:
        server = self.settings.get("server") or {}
        base = str(server.get("base_url") or "").strip()
        path = str((server.get("paths") or {}).get(path_name) or "").strip()
        return build_url(base, path, badge)

    def _request(
        self,
        method: str,
        url: str,
        body: dict | None = None,
        *,
        bearer: str | None = None,
        send_auth: bool = True,
        extra_headers: dict | None = None,
    ) -> HttpResult:
        server = self.settings.get("server") or {}
        timeout = int(server.get("timeout_sec") or 5)
        if self.is_site:
            timeout = max(timeout, 12)
        token = ""
        if send_auth:
            token = bearer if bearer is not None else str(server.get("token") or "").strip()
        device_id = str(server.get("device_id") or "").strip()
        headers = {
            "Accept": "application/json",
            "User-Agent": "TimeScanner/1.0",
        }
        if device_id:
            headers["X-Device-Id"] = device_id
        if token:
            headers["Authorization"] = "Bearer " + token
        if extra_headers:
            headers.update({str(key): str(value) for key, value in extra_headers.items()})
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        context = None
        if url.lower().startswith("https"):
            if server.get("verify_ssl", True):
                context = ssl.create_default_context()
            else:
                context = ssl._create_unverified_context()
        log = logger()
        log.info("%s %s", method, url)
        try:
            status, raw = self._transport(request, timeout, context)
        except Exception as exc:
            message = _network_message(exc)
            log.warning("Нет связи с %s: %s", url, message)
            return HttpResult(False, None, None, message, True)
        if not raw:
            raw = b""
        text = raw.decode("utf-8", errors="replace")
        parsed = None
        if text.strip():
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
        had_password = False
        if isinstance(parsed, list):
            had_password = any(isinstance(item, dict) and any(str(key).lower() == "password" for key in item) for item in parsed)
            parsed = [_without_secrets(item) for item in parsed]
        elif isinstance(parsed, dict):
            had_password = any(str(key).lower() == "password" for key in parsed)
            parsed = _without_secrets(parsed)
        if had_password:
            text = ""
        ok = status is not None and 200 <= status < 300
        if not ok:
            log.warning("Ответ %s от %s", status, url)
        return HttpResult(ok, status, parsed if isinstance(parsed, (dict, list)) else None, None, False, text[:500])


def _default_transport(request, timeout, context):
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.getcode(), response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return exc.code, body


def _network_message(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if "timed out" in lowered or "timeout" in lowered:
        return "Сервер не ответил вовремя"
    if "getaddrinfo" in lowered or "name or service not known" in lowered or "no such host" in lowered:
        return "Адрес сервера не найден"
    if "connection refused" in lowered or "10061" in lowered:
        return "Сервер отклонил соединение"
    if "certificate" in lowered or "ssl" in lowered:
        return "Ошибка сертификата HTTPS"
    return "Нет связи с сервером: " + text[:180]
