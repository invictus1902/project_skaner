from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from scanner_app.logging_setup import logger

STATE_AT_WORK = "at_work"
STATE_AWAY = "away"
STATE_NEVER = "never"
STATE_LABELS = {
    STATE_AT_WORK: "На работе",
    STATE_AWAY: "Не на работе",
    STATE_NEVER: "Ещё не сканировался",
}


def normalize_clock(value: str) -> str | None:
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) not in (2, 3):
        return None
    if any(not part.isdigit() for part in parts):
        return None
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2]) if len(parts) == 3 else 0
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def clock_short(value: str) -> str:
    normal = normalize_clock(value)
    return normal[:5] if normal else ""


def normalize_date(value: str) -> str | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def format_date_ru(value: str) -> str:
    iso = normalize_date(value)
    if not iso:
        return str(value or "")
    year, month, day = iso.split("-")
    return f"{day}.{month}.{year}"


def duration_minutes(start_date: str, start_time: str, end_date: str, end_time: str) -> int | None:
    start_day = normalize_date(start_date)
    end_day = normalize_date(end_date)
    start_clock = normalize_clock(start_time)
    end_clock = normalize_clock(end_time)
    if not start_day or not end_day or not start_clock or not end_clock:
        return None
    start = datetime.strptime(start_day + "T" + start_clock, "%Y-%m-%dT%H:%M:%S")
    end = datetime.strptime(end_day + "T" + end_clock, "%Y-%m-%dT%H:%M:%S")
    if end < start:
        end = end + timedelta(days=1)
    if end < start:
        return None
    return max(0, int(round((end - start).total_seconds() / 60)))


def format_duration(minutes) -> str:
    if minutes is None:
        return ""
    total = int(minutes)
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def is_open(session: dict) -> bool:
    return not str(session.get("endTime") or "").strip()


def person_state(sessions: list) -> str:
    if any(isinstance(row, dict) and is_open(row) for row in sessions):
        return STATE_AT_WORK
    if any(isinstance(row, dict) for row in sessions):
        return STATE_AWAY
    return STATE_NEVER


def sessions_for_user(sessions: list, user_id) -> list[dict]:
    wanted = str(user_id)
    rows = [row for row in sessions if isinstance(row, dict) and str(row.get("userId")) == wanted]
    rows.sort(key=lambda row: (str(row.get("date") or ""), str(row.get("startTime") or ""), str(row.get("id") or "")), reverse=True)
    return rows


def person_name(user: dict) -> str:
    for key in ("fullName", "login", "email"):
        value = str(user.get(key) or "").strip()
        if value:
            return value
    return "Без имени"


def _id_value(value):
    text = str(value if value is not None else "").strip()
    if text.isdigit():
        return int(text)
    return value


def start_payload(user_id, date: str, clock: str) -> dict | None:
    day = normalize_date(date)
    moment = normalize_clock(clock)
    if not day or not moment:
        return None
    return {
        "userId": _id_value(user_id),
        "date": day,
        "startTime": moment,
        "endTime": None,
        "durationMinutes": None,
        "status": "active",
        "source": "manual",
    }


def arrival_payload(date: str, clock: str) -> dict | None:
    day = normalize_date(date)
    moment = normalize_clock(clock)
    if not day or not moment:
        return None
    return {
        "date": day,
        "startTime": moment,
        "status": "manually_edited",
        "source": "manual",
    }


def close_payload(start_date: str, start_time: str, end_date: str, end_time: str, keep_start: bool) -> dict | None:
    start_day = normalize_date(start_date)
    start_clock = normalize_clock(start_time)
    end_day = normalize_date(end_date)
    end_clock = normalize_clock(end_time)
    if not start_day or not start_clock or not end_day or not end_clock:
        return None
    minutes = duration_minutes(start_day, start_clock, end_day, end_clock)
    if minutes is None:
        return None
    body = {
        "endDate": end_day,
        "endTime": end_clock,
        "durationMinutes": minutes,
        "status": "manually_edited",
        "source": "manual",
    }
    if keep_start:
        body["date"] = start_day
        body["startTime"] = start_clock
    return body


def end_payload(session: dict, end_date: str, end_time: str) -> dict | None:
    return close_payload(str(session.get("date") or ""), str(session.get("startTime") or ""), end_date, end_time, False)


def apply_pending(sessions: list, ops: list) -> list[dict]:
    rows = [dict(row) for row in sessions if isinstance(row, dict)]
    by_id = {str(row.get("id")): row for row in rows if str(row.get("id") or "")}
    for op in ops:
        try:
            payload = json.loads(op.get("payload") or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(op.get("op") or "") == "create":
            local_key = str(op.get("local_key") or "")
            current = next((row for row in rows if str(row.get("localKey") or "") == local_key and local_key), None)
            if current is None:
                current = {"id": "", "localKey": local_key}
                rows.append(current)
            current.update(payload)
            current["localKey"] = local_key
            current["pending"] = True
            continue
        target = by_id.get(str(op.get("session_id") or ""))
        if target is None:
            continue
        target.update(payload)
        target["pending"] = True
    return rows


def classify_session_write(result, blocked: bool) -> tuple[str, str]:
    if getattr(result, "network_error", False) or blocked:
        return "retry", getattr(result, "error", None) or "Нет связи с сервером"
    data = result.data if isinstance(getattr(result, "data", None), dict) else {}
    message = str(data.get("message") or data.get("error") or getattr(result, "error", None) or "")
    status = getattr(result, "status", None)
    if getattr(result, "ok", False):
        return "sent", message or "Отправлено"
    if status in (401, 403, 408, 429) or (status is not None and status >= 500):
        return "retry", message or "Сервер временно недоступен"
    return "rejected", message or f"Сервер отклонил смену ({status})"


class ShiftBook:
    """Загрузка людей и запись смен. Без сети правка остаётся в очереди."""

    def __init__(self, db, api) -> None:
        self.db = db
        self.api = api

    def load(self) -> dict:
        users_result, sessions_result = self.api.load_people()
        offline = False
        message = ""
        if isinstance(getattr(users_result, "data", None), list) and not self.api._blocked(users_result):
            users = [row for row in users_result.data if isinstance(row, dict)]
            if isinstance(getattr(sessions_result, "data", None), list) and not self.api._blocked(sessions_result):
                sessions = [row for row in sessions_result.data if isinstance(row, dict)]
                self.db.save_directory(users, sessions)
            else:
                offline = True
                message = "Сотрудники загружены, а смены с сервера не пришли. Показаны последние сохранённые смены."
                _cached_users, sessions = self.db.load_directory()
                self.db.save_directory(users, sessions)
        else:
            offline = True
            message = _offline_text(users_result, self.api)
            users, sessions = self.db.load_directory()
        sessions = apply_pending(sessions, self.db.pending_session_ops())
        return {
            "users": users,
            "sessions": sessions,
            "offline": offline,
            "message": message,
            "pending": self.db.count_pending_session_ops(),
        }

    def start(self, user_id, date: str, clock: str) -> tuple[str, str]:
        payload = start_payload(user_id, date, clock)
        if payload is None:
            return "rejected", "Укажите дату как ДД.ММ.ГГГГ и время как ЧЧ:ММ."
        local_key = "local-" + uuid.uuid4().hex
        op_id = self.db.insert_session_op("create", str(user_id), "", local_key, payload)
        return self._flush_one(op_id)

    def change_arrival(self, session: dict, date: str, clock: str) -> tuple[str, str]:
        payload = arrival_payload(date, clock)
        if payload is None:
            return "rejected", "Укажите дату прихода как ДД.ММ.ГГГГ и время как ЧЧ:ММ."
        return self._edit(session, payload)

    def finish(self, session: dict, end_date: str, end_time: str) -> tuple[str, str]:
        payload = end_payload(session, end_date, end_time)
        if payload is None:
            return "rejected", "Время ухода раньше прихода или указано неверно. Дата — ДД.ММ.ГГГГ, время — ЧЧ:ММ."
        return self._edit(session, payload)

    def change_closed(self, session: dict, start_date: str, start_time: str, end_date: str, end_time: str) -> tuple[str, str]:
        payload = close_payload(start_date, start_time, end_date, end_time, True)
        if payload is None:
            return "rejected", "Проверьте дату и время прихода и ухода. Дата — ДД.ММ.ГГГГ, время — ЧЧ:ММ."
        return self._edit(session, payload)

    def flush_pending(self) -> None:
        for op in self.db.pending_session_ops():
            kind, _message = self._flush_one(int(op["id"]))
            if kind == "queued":
                break

    def _edit(self, session: dict, payload: dict) -> tuple[str, str]:
        local_key = str(session.get("localKey") or "")
        if local_key:
            existing = self.db.find_pending_create(local_key)
            if existing:
                merged = _stored_payload(existing)
                merged.update(payload)
                if "userId" not in merged and session.get("userId") is not None:
                    merged["userId"] = _id_value(session.get("userId"))
                self.db.replace_session_payload(int(existing["id"]), merged)
                return self._flush_one(int(existing["id"]))
        session_id = str(session.get("id") or "").strip()
        if not session_id:
            return "rejected", "У этой смены ещё нет номера на сервере."
        existing = self.db.find_pending_update(session_id)
        if existing:
            merged = _stored_payload(existing)
            merged.update(payload)
            self.db.replace_session_payload(int(existing["id"]), merged)
            return self._flush_one(int(existing["id"]))
        op_id = self.db.insert_session_op("update", str(session.get("userId") or ""), session_id, "", payload)
        return self._flush_one(op_id)

    def _flush_one(self, op_id: int) -> tuple[str, str]:
        op = self.db.get_session_op(op_id)
        if not op:
            return "rejected", "Запись очереди не найдена."
        if op.get("status") == "sent":
            return "sent", "Уже отправлено."
        result = self.api.write_session(op)
        kind, message = classify_session_write(result, self.api._blocked(result))
        if kind == "sent":
            self.db.mark_session_op_sent(op_id)
            saved = result.data if isinstance(result.data, dict) else {}
            self._remember(op, saved)
            logger().info("Смена отправлена: %s %s", op.get("op"), op.get("user_id"))
            return "sent", "Отправлено на сервер."
        if kind == "rejected":
            self.db.mark_session_op_rejected(op_id, message)
            logger().warning("Смена отклонена: %s", message)
            return "rejected", message
        self.db.mark_session_op_retry(op_id, message)
        logger().warning("Смена ждёт связи: %s", message)
        return "queued", "Связи нет. Изменение сохранено на этом компьютере и уйдёт на сервер само."

    def _remember(self, op: dict, saved: dict) -> None:
        users, sessions = self.db.load_directory()
        if str(op.get("op") or "") == "create":
            if not saved.get("id"):
                return
            row = dict(saved)
            if row.get("userId") is None and op.get("user_id"):
                row["userId"] = _id_value(op.get("user_id"))
            sessions = [item for item in sessions if str(item.get("id")) != str(row.get("id"))]
            sessions.append(row)
        elif saved:
            session_id = str(saved.get("id") or op.get("session_id") or "")
            found = False
            updated = []
            for item in sessions:
                if str(item.get("id")) == session_id:
                    merged = dict(item)
                    merged.update(saved)
                    updated.append(merged)
                    found = True
                else:
                    updated.append(item)
            sessions = updated
            if not found and session_id:
                row = dict(saved)
                row.setdefault("id", session_id)
                sessions.append(row)
        self.db.save_directory(users, sessions)


def _stored_payload(op: dict) -> dict:
    try:
        payload = json.loads(op.get("payload") or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _offline_text(result, api) -> str:
    if api._blocked(result):
        return api._wall_message()
    text = str(getattr(result, "error", None) or "").strip()
    if text:
        return text + " Показан последний сохранённый список."
    return "Сервер не ответил. Показан последний сохранённый список."
