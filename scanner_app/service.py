from __future__ import annotations

import copy
import json
import os
import queue
import threading
import time
from datetime import datetime

from scanner_app.api import classify_post, extract_active, extract_event, extract_name
from scanner_app.config import server_configured
from scanner_app.logging_setup import logger
from scanner_app.logic import (
    decide_event,
    event_phrase,
    format_when,
    manual_badge,
    normalize_badge,
    parse_iso,
    render_speech,
    speak_time,
)

UNKNOWN_CODES = {
    "unknown_badge",
    "unknown",
    "not_found",
    "employee_not_found",
    "badge_not_found",
}


def beep(kind: str, enabled: bool) -> None:
    if not enabled or os.name != "nt" or not kind:
        return
    try:
        import winsound

        sounds = {
            "ok": winsound.MB_OK,
            "warn": winsound.MB_ICONEXCLAMATION,
            "error": winsound.MB_ICONHAND,
        }
        winsound.MessageBeep(sounds.get(kind, winsound.MB_OK))
    except Exception:
        logger().debug("Не удалось подать сигнал", exc_info=True)


class AttendanceService:
    def __init__(self, settings: dict, database, api, speaker, ui) -> None:
        self.settings = settings
        self.db = database
        self.api = api
        self.speaker = speaker
        self.ui = ui
        self._scans: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._retry: threading.Thread | None = None
        self._cooldown: dict[str, float] = {}
        self._cooldown_lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._send_slots = threading.Semaphore(4)

    def start(self) -> None:
        self._stop.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="attendance", daemon=True)
        self._worker.start()
        self._retry = threading.Thread(target=self._retry_loop, name="retry", daemon=True)
        self._retry.start()

    def stop(self) -> None:
        self._stop.set()
        self._scans.put(None)

    def update(self, settings: dict) -> None:
        self.settings = settings

    def submit_scan(self, raw: str) -> None:
        self._scans.put(str(raw))

    def submit_manual(self, raw: str) -> None:
        """Код, вписанный с клавиатуры, когда сканер недоступен."""
        self._scans.put(("manual", str(raw)))

    def on_burst(self, text: str, duration: float, terminated: bool, fast: bool) -> None:
        del terminated
        short = " ".join(str(text).split())[:180]
        if fast:
            self.ui.debug(f"принят: {short} ({int(duration)} мс)")
            self.submit_scan(text)
            return
        self.ui.debug(f"медленно, проигнорирован: {short} ({int(duration)} мс)")

    def flush_queue(self) -> None:
        threading.Thread(target=self._flush_once, name="flush", daemon=True).start()

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            raw = self._scans.get()
            if raw is None:
                return
            manual = False
            if isinstance(raw, tuple):
                manual = raw[0] == "manual"
                raw = raw[1] if len(raw) > 1 else ""
            try:
                event_id = None
                with self._io_lock:
                    event_id = self._process(str(raw), manual=manual, background=True)
                if event_id:
                    self._send_later(int(event_id))
            except Exception:
                logger().exception("Ошибка обработки кода")
                self.ui.show(
                    {
                        "state": "error",
                        "title": "ОШИБКА",
                        "name": "",
                        "when": "",
                        "detail": "Сбой программы при обработке кода. Подробности в журнале.",
                        "badge": "",
                    }
                )

    def _retry_loop(self) -> None:
        self._stop.wait(4)
        while not self._stop.is_set():
            try:
                self._flush_once()
            except Exception:
                logger().exception("Ошибка очереди отправки")
            delay = int((self.settings.get("attendance") or {}).get("retry_sec") or 20)
            if self._stop.wait(max(5, delay)):
                return

    def _process(self, raw: str, manual: bool = False, background: bool = False) -> int | None:
        settings = copy.deepcopy(self.settings)
        scanner = settings.get("scanner") or {}
        badge = manual_badge(raw, scanner) if manual else normalize_badge(raw, scanner)
        if badge:
            badge = self._canonical_badge(badge)
        if not badge:
            logger().warning("Код не распознан: %s", " ".join(str(raw).split())[:200])
            self._present(
                settings,
                state="error",
                title="Введите код" if manual else "КОД НЕ РАСПОЗНАН",
                name="",
                when="",
                detail="Напишите код бейджа: цифры, буквы или символы." if manual else "Сканер передал код, который не похож на бейдж.",
                badge="",
                template="unknown",
                beep_kind="error",
                event="",
                speech_name="",
            )
            return
        if manual:
            logger().info("Код введён вручную: %s", badge[:80])

        cooldown = float(scanner.get("cooldown_sec") or 0)
        now_mono = time.monotonic()
        with self._cooldown_lock:
            previous = self._cooldown.get(badge, 0.0)
            if cooldown and now_mono - previous < cooldown:
                cached = self.db.get_employee(badge)
                self._present(
                    settings,
                    state="duplicate",
                    title="УЖЕ ОТМЕЧЕН",
                    name=(cached or {}).get("name") or badge,
                    when="",
                    detail="Подождите несколько секунд. Повторное считывание того же бейджа пока не учитывается.",
                    badge=badge,
                    template="duplicate",
                    beep_kind="warn",
                    event="",
                    speech_name=(cached or {}).get("name") or f"Бейдж {badge}",
                )
                return
            self._cooldown[badge] = now_mono

        cached = self.db.get_employee(badge)
        cached_name = (cached or {}).get("name") or ""
        action, employee_name = self._lookup_employee(badge, cached_name, settings)
        if action == "unknown":
            self._reject_visible(settings, badge, badge, "unknown", "БЕЙДЖ НЕ НАЙДЕН", "Такого бейджа нет в справочнике.", "unknown")
            return
        if action == "disabled":
            self._reject_visible(
                settings,
                badge,
                employee_name or badge,
                "disabled",
                "БЕЙДЖ ОТКЛЮЧЁН",
                "Этот бейдж отключён и не может отмечаться.",
                "disabled",
            )
            return

        now = datetime.now().astimezone()
        event, abandoned, employee_name = self._choose_event(badge, employee_name, now, settings)
        if abandoned:
            logger().warning("Предыдущий приход бейджа %s не был закрыт. Начата новая смена.", badge)
        if employee_name and not cached_name:
            self.db.upsert_employee(badge, employee_name, "server")

        speech_name = employee_name or f"Бейдж {badge}"
        screen_name = employee_name or badge
        spoken = speak_time(now)
        payload = {
            "badge": badge,
            "event": event,
            "timestamp": now.isoformat(timespec="seconds"),
            "device_id": str(settings.get("server", {}).get("device_id") or ""),
            "name": employee_name,
        }
        event_id = self.db.insert_event(badge, screen_name, event, now, payload)
        when = format_when(now)
        if not server_configured(settings.get("server")):
            self._finish_local(settings, screen_name, speech_name, badge, event, when, spoken, "Сервер не настроен. Запись сохранена на этом компьютере.")
            return None
        # Сайт не смотрит тип отметки, а переключает открытую смену.
        # Нельзя отправить уход, пока предыдущий приход ещё лежит в очереди.
        if self.db.has_older_pending(badge, event_id):
            message = "Отметка сохранена. Она уйдёт на сервер сразу после предыдущей."
            self.db.mark_retry(event_id, message)
            if background:
                self._announce_choice(settings, screen_name, speech_name, badge, event, when, spoken)
                return None
            self._finish_local(settings, screen_name, speech_name, badge, event, when, spoken, message)
            return None

        if background:
            # Экран и голос не ждут сеть: следующий человек отмечается сразу.
            self._announce_choice(settings, screen_name, speech_name, badge, event, when, spoken)
            return event_id

        if not self.db.claim_event(event_id):
            return None
        try:
            result = self.api.post_event(payload)
        except Exception:
            self.db.mark_retry(event_id, "Сбой отправки")
            raise
        kind, message = classify_post(result)
        self._apply_server_result(
            settings,
            event_id,
            badge,
            event,
            screen_name,
            speech_name,
            when,
            spoken,
            result,
            kind,
            message,
        )
        return None

    def _lookup_employee(self, badge: str, cached_name: str, settings: dict) -> tuple[str, str]:
        result = self.api.get_employee(badge)
        if result is None or result.network_error:
            return "skip", cached_name
        data = result.data if isinstance(result.data, dict) else {}
        err_code = str(data.get("error") or data.get("code") or "").lower()
        missing = result.status == 404 or err_code in UNKNOWN_CODES
        if missing:
            if settings.get("server", {}).get("strict_employee_check"):
                return "unknown", ""
            return "skip", cached_name
        if result.status and 200 <= result.status < 300:
            if extract_active(data) is False:
                return "disabled", extract_name(data) or cached_name
            name = extract_name(data) or cached_name
            if name:
                self.db.upsert_employee(badge, name, "server")
            return "ok", name
        return "skip", cached_name

    def _canonical_badge(self, badge: str) -> str:
        resolve = getattr(self.api, "canonical_badge", None)
        if resolve is None:
            return badge
        try:
            resolved = str(resolve(badge) or "").strip()
        except Exception:
            logger().exception("Не удалось сверить код бейджа со справочником")
            return badge
        return resolved or badge

    def _choose_event(self, badge: str, employee_name: str, now: datetime, settings: dict) -> tuple[str, bool, str]:
        name = employee_name
        server_event = None
        status = self.api.get_status(badge)
        if status is not None and not status.network_error and status.ok and isinstance(status.data, dict):
            data = status.data
            server_name = extract_name(data)
            if server_name:
                name = name or server_name
                self.db.upsert_employee(badge, server_name, "server")
            if any(key in data for key in ("last_event", "event", "type", "event_type", "action")):
                server_event = extract_event(data)

        # Пока отметка лежит в очереди, сервер о ней не знает и отвечает «не на работе».
        # Иначе каждый следующий скан снова открывает смену.
        self.db.suppress_repeated_pending(badge)
        last = self.db.last_counted_event(badge)
        last_event = last["event_type"] if last else None
        last_time = parse_iso(last["scanned_at"]) if last else None
        if last_time is not None and last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=now.tzinfo)
        max_hours = float(settings.get("attendance", {}).get("shift_max_hours") or 16)
        if last and last.get("status") in ("pending", "sending"):
            event = decide_event(last_event, last_time, now, max_hours)
            abandoned = last_event == "in" and event == "in"
            return event, abandoned, name
        if server_event == "in":
            return "out", False, name
        if server_event == "out":
            return "in", False, name
        if server_event == "":
            return "in", False, name
        event = decide_event(last_event, last_time, now, max_hours)
        abandoned = last_event == "in" and event == "in"
        return event, abandoned, name

    def _apply_server_result(
        self,
        settings,
        event_id,
        badge,
        event,
        screen_name,
        speech_name,
        when,
        spoken,
        result,
        kind,
        message,
        announce: bool = True,
    ) -> None:
        data = result.data if isinstance(result.data, dict) else {}
        if kind == "sent":
            final_event = extract_event(data) or event
            final_name = extract_name(data) or screen_name
            if final_name and final_name != badge:
                self.db.upsert_employee(badge, final_name, "server")
            self.db.mark_sent(event_id, final_name, final_event)
            title = "ПРИХОД" if final_event == "in" else "УХОД"
            detail = "Отправлено на сервер"
            if message and message not in ("Отправлено", "Уже зарегистрировано"):
                detail = message
            elif message == "Уже зарегистрировано":
                detail = "Уже зарегистрировано на сервере"
            logger().info("Скан %s → %s, %s, отправлено", badge, final_event, final_name)
            if announce:
                self._present(
                    settings,
                    state=final_event,
                    title=title,
                    name=final_name,
                    when=when,
                    detail=detail,
                    badge=badge,
                    template=final_event,
                    beep_kind="ok",
                    event=final_event,
                    speech_name=final_name if final_name != badge else speech_name,
                    spoken=spoken,
                )
            else:
                self._touch_ui()
            return
        if kind == "unknown":
            self.db.mark_rejected(event_id, message)
            logger().info("Скан %s → бейдж не найден", badge)
            if not announce:
                self._touch_ui()
                return
            self._present(
                settings,
                state="unknown",
                title="БЕЙДЖ НЕ НАЙДЕН",
                name=badge,
                when=when,
                detail=message or "Такого бейджа нет на сервере.",
                badge=badge,
                template="unknown",
                beep_kind="error",
                event="",
                speech_name="",
            )
            return
        if kind == "rejected":
            self.db.mark_rejected(event_id, message)
            logger().warning("Скан %s отклонён: %s", badge, message)
            if not announce:
                self._touch_ui()
                return
            self._present(
                settings,
                state="error",
                title="ОШИБКА",
                name=screen_name,
                when=when,
                detail=message or "Сервер отклонил отметку.",
                badge=badge,
                template="error",
                beep_kind="error",
                event=event,
                speech_name=speech_name,
            )
            return
        self.db.mark_retry(event_id, message)
        logger().warning("Скан %s сохранён в очередь: %s", badge, message)
        if not announce:
            self._touch_ui()
            return
        self._finish_local(settings, screen_name, speech_name, badge, event, when, spoken, "Нет связи с сервером. Запись будет отправлена позже.")

    def _announce_choice(self, settings, screen_name, speech_name, badge, event, when, spoken) -> None:
        if event == "in":
            title = "ПРИХОД"
        elif event == "out":
            title = "УХОД"
        else:
            title = "СОХРАНЕНО"
        state = event if event in ("in", "out") else "offline"
        logger().info("Скан %s → %s, %s, на экране", badge, event, screen_name)
        self._present(
            settings,
            state=state,
            title=title,
            name=screen_name,
            when=when,
            detail="Отмечено",
            badge=badge,
            template=state if state in ("in", "out") else "offline",
            beep_kind="ok",
            event=event or "in",
            speech_name=speech_name,
            spoken=spoken,
        )

    def _touch_ui(self) -> None:
        self.ui.refresh_history()
        self.ui.set_pending(self.db.count_pending())

    def _send_later(self, event_id: int) -> None:
        def run() -> None:
            with self._send_slots:
                self._send_saved(event_id)

        threading.Thread(target=run, name="send", daemon=True).start()

    def _send_saved(self, event_id: int) -> str:
        """Отправить уже показанную отметку. Большой экран при этом не меняется."""
        try:
            kind = self._deliver(event_id)
        except Exception:
            logger().exception("Ошибка отправки отметки")
            try:
                row = self.db.get_event(event_id)
                if row and row.get("status") in ("pending", "sending"):
                    self.db.mark_retry(event_id, "Сбой отправки")
            except Exception:
                logger().exception("Не удалось вернуть отметку в очередь")
            self._touch_ui()
            return "retry"
        if kind in ("skip", "retry"):
            return kind
        row = self.db.get_event(event_id)
        if row is None:
            return kind
        nxt = self.db.next_pending_id(str(row["badge"]))
        if nxt and nxt != event_id:
            return self._send_saved(nxt)
        return kind

    def _deliver(self, event_id: int) -> str:
        row = self.db.get_event(event_id)
        if row is None or row.get("status") != "pending":
            return "skip"
        badge = str(row["badge"])
        if self.db.has_older_pending(badge, event_id):
            return "skip"
        if not self.db.claim_event(event_id):
            return "skip"
        try:
            payload = json.loads(row.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = {
                "badge": badge,
                "event": row["event_type"],
                "timestamp": row["scanned_at"],
                "device_id": (self.settings.get("server") or {}).get("device_id") or "",
                "name": row.get("name") or "",
            }
        result = self.api.post_event(payload)
        kind, message = classify_post(result)
        moment = parse_iso(str(row.get("scanned_at") or "")) or datetime.now().astimezone()
        self._apply_server_result(
            copy.deepcopy(self.settings),
            event_id,
            badge,
            str(row.get("event_type") or ""),
            row.get("name") or badge,
            row.get("name") or badge,
            format_when(moment),
            "",
            result,
            kind,
            message,
            announce=False,
        )
        return kind

    def _finish_local(self, settings, screen_name, speech_name, badge, event, when, spoken, detail) -> None:
        if event == "in":
            title = "ПРИХОД"
        elif event == "out":
            title = "УХОД"
        else:
            title = "СОХРАНЕНО"
        logger().info("Скан %s → %s, %s, локально", badge, event, screen_name)
        self._present(
            settings,
            state="offline",
            title=title,
            name=screen_name,
            when=when,
            detail=detail,
            badge=badge,
            template="offline",
            beep_kind="warn",
            event=event,
            speech_name=speech_name,
            spoken=spoken,
        )

    def _reject_visible(self, settings, badge, screen_name, event_type, title, detail, template) -> None:
        now = datetime.now().astimezone()
        event_id = self.db.insert_event(
            badge,
            screen_name,
            event_type,
            now,
            {"badge": badge, "event": event_type},
        )
        self.db.mark_rejected(event_id, detail)
        logger().info("Скан %s → %s", badge, detail)
        self._present(
            settings,
            state="unknown" if event_type == "unknown" else "disabled",
            title=title,
            name=screen_name,
            when=format_when(now),
            detail=detail,
            badge=badge,
            template=template,
            beep_kind="error",
            event="",
            speech_name="",
        )

    def _present(
        self,
        settings,
        *,
        state,
        title,
        name,
        when,
        detail,
        badge,
        template,
        beep_kind,
        event,
        speech_name,
        spoken="",
    ) -> None:
        self.ui.show(self._view(state, title, name, when, detail, badge))
        self.ui.refresh_history()
        self.ui.set_pending(self.db.count_pending())
        voice = settings.get("voice") or {}
        beep(beep_kind, bool(voice.get("beep")))
        if not voice.get("enabled"):
            return
        values = {
            "name": speech_name or name or "Сотрудник",
            "time": spoken or speak_time(datetime.now().astimezone()),
            "badge": badge,
            "event_phrase": event_phrase(event or "in"),
        }
        phrase = render_speech((voice.get("templates") or {}).get(template, ""), values)
        self.speaker.say(phrase, voice)

    def _view(self, state, title, name, when, detail, badge) -> dict:
        return {
            "state": state,
            "title": title,
            "name": name,
            "when": when,
            "detail": detail,
            "badge": badge,
        }

    def _flush_once(self) -> None:
        self._flush_pending()

    def _flush_pending(self) -> None:
        settings = copy.deepcopy(self.settings)
        self.api.update(settings)
        pending_count = self.db.count_pending()
        self.ui.set_pending(pending_count)
        if not server_configured(settings.get("server")):
            return
        seen = []
        for row in self.db.pending_events(limit=50):
            if row["badge"] not in seen:
                seen.append(row["badge"])
                self.db.suppress_repeated_pending(row["badge"])
        for row in self.db.pending_events(limit=20):
            if self._stop.is_set():
                return
            if self.db.has_older_pending(row["badge"], int(row["id"])):
                continue
            kind = self._send_saved(int(row["id"]))
            if kind == "retry":
                break
        self.ui.set_pending(self.db.count_pending())
        self.ui.refresh_history()
        try:
            from scanner_app.shifts import ShiftBook

            ShiftBook(self.db, self.api).flush_pending()
        except Exception:
            logger().exception("Ошибка очереди смен")

    def run_shift(self, callback):
        """Правка смены под тем же замком, что и решение «приход или уход»."""
        with self._io_lock:
            self.api.update(copy.deepcopy(self.settings))
            from scanner_app.shifts import ShiftBook

            return callback(ShiftBook(self.db, self.api))
