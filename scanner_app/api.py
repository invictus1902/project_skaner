from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from scanner_app.config import LOCAL_API_URL, server_urls
from scanner_app.logging_setup import logger
from scanner_app.logic import parse_iso
from scanner_app.shifts import duration_minutes, is_open

UNKNOWN_CODES = {
    "unknown_badge",
    "unknown",
    "not_found",
    "employee_not_found",
    "badge_not_found",
}


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


def _shift_open(row: dict) -> bool:
    return is_open(row)


def _event_moment(value) -> datetime:
    """Время самой отметки, а не время, когда очередь дошла до сервера."""
    parsed = parse_iso(str(value or ""))
    if parsed is None:
        return datetime.now().astimezone()
    return parsed


def _open_session(sessions: list, user_id) -> dict | None:
    wanted = str(user_id)
    rows = [
        row
        for row in sessions
        if isinstance(row, dict) and str(row.get("userId")) == wanted and is_open(row)
    ]
    if not rows:
        return None

    def sort_key(row: dict) -> tuple:
        text = str(row.get("id") or "")
        if text.isdigit():
            return (1, int(text))
        return (0, 0)

    rows.sort(key=sort_key)
    return rows[-1]


def _checkin_result(name: str, event: str) -> HttpResult:
    action = "start" if event == "in" else "end"
    return HttpResult(
        True,
        200,
        {"success": True, "action": action, "event": event, "fullName": name, "name": name},
        None,
        False,
    )


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

    def update(self, settings: dict) -> None:
        self.settings = settings

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
            users = self._directory_users()
            if isinstance(users.data, list) and not self._blocked(users):
                return "online"
            return "offline"
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
        users, sessions = self.load_people()
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

    def load_people(self) -> tuple[HttpResult, HttpResult]:
        """Сотрудники и смены сайта. Пароли в ответ не входят.

        Сначала читает адрес получения данных. Если хостинг закрыт страницей Vercel,
        берёт тот же список из общей базы, которую показывает сайт.
        """
        empty = HttpResult(False, None, None, "Смены не загружены", True)
        if not self.is_site:
            return HttpResult(False, None, None, "Список сотрудников доступен в протоколе сайта учёта.", False), empty
        users = self._directory_users()
        if not isinstance(users.data, list) or self._blocked(users):
            return users, empty
        sessions = self._directory_sessions()
        if not isinstance(sessions.data, list) or self._blocked(sessions):
            sessions = HttpResult(True, users.status, [], None, False)
        return users, sessions

    def _directory_users(self) -> HttpResult:
        users = self._load_users()
        if isinstance(users.data, list) and not users.network_error and not self._blocked(users):
            return users
        hosted = self._supabase_get("users", "id,fullName,login,email,badgeId,role")
        if isinstance(hosted.data, list) and not self._blocked(hosted):
            return hosted
        return users

    def _directory_sessions(self) -> HttpResult:
        sessions = self._request("GET", build_url(self.read_url(), "/workSessions"), send_auth=False)
        if isinstance(sessions.data, list) and not self._blocked(sessions):
            sessions.data = [_without_secrets(row) for row in sessions.data]
            sessions.raw_text = ""
            return sessions
        hosted = self._supabase_get(
            "workSessions",
            "id,userId,date,startTime,endTime,durationMinutes,status,source,editedBy",
            order="id.desc",
        )
        if isinstance(hosted.data, list) and not self._blocked(hosted):
            return hosted
        return sessions

    def write_session(self, op: dict) -> HttpResult:
        """Создать или изменить смену. Если хостинг закрыт, пишет в ту же базу, что и сайт."""
        if not self.is_site:
            return HttpResult(False, None, None, "Запись смен работает в протоколе сайта учёта.", False)
        express = self._express_write(op)
        if express.ok and not self._blocked(express):
            return express
        retry = express.network_error or self._blocked(express) or express.status in (401, 403, 408, 429) or (
            express.status is not None and express.status >= 500
        )
        if retry:
            hosted = self._supabase_write(op)
            if hosted is not None:
                return hosted
        return express

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

    def _supabase_get(self, table: str, select: str, order: str = "") -> HttpResult:
        base, key = self._hosted_database()
        if not base or not key:
            return HttpResult(False, None, None, "Общая база сайта не настроена", True)
        url = base + "/rest/v1/" + table + "?select=" + quote(select, safe=",") + "&limit=1000"
        if order:
            url += "&order=" + quote(order, safe=".")
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
            first = result.data[0] if result.data and isinstance(result.data[0], dict) else {}
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
        sessions = self._directory_sessions()
        if sessions.network_error or self._blocked(sessions) or not isinstance(sessions.data, list):
            return self._closed(sessions)
        user_id = str(found.get("id"))
        active = any(isinstance(row, dict) and str(row.get("userId")) == user_id and _shift_open(row) for row in sessions.data)
        name = str(found.get("fullName") or "").strip()
        return HttpResult(True, 200, {"last_event": "in" if active else "out", "name": name, "fullName": name}, None, False)

    def _find_user(self, badge: str) -> tuple[dict | None, HttpResult]:
        result = self._directory_users()
        if not isinstance(result.data, list):
            return None, result
        wanted = str(badge or "").strip()
        folded = wanted.casefold()
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

    def _post_checkin(self, payload: dict) -> HttpResult:
        express = self._express_checkin(payload)
        if express.ok and not self._blocked(express) and not _checkin_failed(express):
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

    def _write_checkin_shift(self, payload: dict) -> HttpResult | None:
        """Та же запись смены, что сработала вчера из окна «Смены», если приём сканера закрыт."""
        if not all(self._hosted_database()):
            return None
        badge = str(payload.get("badge") or payload.get("badgeId") or "").strip()
        found, result = self._find_user(badge)
        if found is None and (result.network_error or self._blocked(result) or not isinstance(result.data, list)):
            return self._closed(result)
        if found is None:
            return HttpResult(False, 404, {"error": "unknown_badge", "message": "Сотрудник не найден"}, None, False)
        sessions = self._directory_sessions()
        if sessions.network_error or self._blocked(sessions) or not isinstance(sessions.data, list):
            return self._closed(sessions)
        name = str(found.get("fullName") or found.get("login") or "").strip()
        event = str(payload.get("event") or "").strip().lower()
        if event not in ("in", "out"):
            event = "out" if _open_session(sessions.data, found.get("id")) else "in"
        moment = _event_moment(payload.get("timestamp"))
        logger().info("Отметка %s пишется в базу сайта", badge[:80])
        if event == "in":
            if _open_session(sessions.data, found.get("id")) is not None:
                return _checkin_result(name, "in")
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
            current = _open_session(sessions.data, found.get("id"))
            if current is None or not str(current.get("id") or "").strip():
                return HttpResult(False, None, None, "Открытая смена ещё не появилась на сервере.", True)
            minutes = duration_minutes(
                str(current.get("date") or ""),
                str(current.get("startTime") or ""),
                moment.strftime("%Y-%m-%d"),
                moment.strftime("%H:%M:%S"),
            )
            written = self.write_session(
                {
                    "op": "update",
                    "session_id": str(current.get("id")),
                    "payload": {
                        "endTime": moment.strftime("%H:%M:%S"),
                        "durationMinutes": 0 if minutes is None else minutes,
                        "status": "completed",
                        "source": "qr",
                    },
                }
            )
        if written.ok and isinstance(written.data, dict):
            written.data["fullName"] = name or extract_name(written.data)
            written.data["name"] = written.data["fullName"]
            written.data["action"] = "start" if event == "in" else "end"
            written.data["event"] = event
        return written

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
