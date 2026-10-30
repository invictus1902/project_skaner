from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from scanner_app.api import ApiClient
from scanner_app.config import default_settings
from scanner_app.db import Database
from scanner_app.shifts import (
    STATE_AT_WORK,
    STATE_AWAY,
    STATE_NEVER,
    ShiftBook,
    duration_minutes,
    end_clock_label,
    format_date_ru,
    keep_shift_span,
    person_state,
    session_end_day,
    sessions_for_user,
)


class ShiftLogicTests(unittest.TestCase):
    def test_states_and_duration(self) -> None:
        self.assertEqual(person_state([]), STATE_NEVER)
        self.assertEqual(person_state([{"endTime": "18:00:00"}]), STATE_AWAY)
        self.assertEqual(person_state([{"endTime": None}, {"endTime": "18:00:00"}]), STATE_AT_WORK)
        self.assertEqual(duration_minutes("2026-10-12", "22:00", "2026-10-13", "06:00"), 480)
        self.assertEqual(duration_minutes("12.10.2026", "22:00", "12.10.2026", "06:00"), 480)
        self.assertEqual(duration_minutes("13.10.2026", "08:00", "14.10.2026", "10:00"), 1560)
        self.assertIsNone(duration_minutes("12.10.2026", "10:00", "10.10.2026", "09:00"))
        self.assertIsNone(duration_minutes("2026-10-12", "08:00", "2026-10-11", "10:00"))
        overnight = {
            "date": "2026-10-13",
            "startTime": "08:00:00",
            "endTime": "10:00:00",
            "durationMinutes": 1560,
        }
        self.assertEqual(session_end_day(overnight), "2026-10-14")
        self.assertEqual(end_clock_label(overnight), "14.10 10:00")
        self.assertEqual(end_clock_label({**overnight, "durationMinutes": 120}), "10:00")
        echoed = {"date": "2026-10-13", "endTime": "10:00:00", "durationMinutes": 120}
        keep_shift_span(echoed, {"endDate": "2026-10-14", "endTime": "10:00:00", "durationMinutes": 1560})
        self.assertEqual(echoed["durationMinutes"], 1560)
        self.assertEqual(echoed["endDate"], "2026-10-14")
        self.assertEqual(person_state([{"status": "active", "startTime": "09:00:00"}]), STATE_AT_WORK)
        self.assertEqual(person_state([{"status": "completed", "startTime": "09:00:00"}]), STATE_AWAY)


class ShiftQueueTests(unittest.TestCase):
    def _client(self, transport) -> ApiClient:
        settings = default_settings()
        settings["server"]["read_url"] = "http://read"
        settings["server"]["write_url"] = "http://write"
        settings["server"]["base_url"] = "http://ignored"
        return ApiClient(settings, transport=transport)

    def test_offline_edit_is_one_request_to_the_write_host(self) -> None:
        online = {"value": False}
        seen = []

        def transport(request, timeout, context):
            del timeout, context
            seen.append((request.method, request.full_url, request.data))
            if request.full_url.startswith("http://read/users"):
                return 200, '[{"id": 2, "fullName": "Иванова", "badgeId": "55001", "password": "secret"}]'.encode("utf-8")
            if request.method == "GET" and request.full_url.startswith("http://read/workSessions"):
                if online["value"]:
                    return 200, b'[{"id": 9, "userId": 2, "date": "2026-10-12", "startTime": "09:30:00", "endTime": null, "status": "active"}]'
                return 200, b"[]"
            if not online["value"]:
                raise TimeoutError("timed out")
            if request.method == "POST" and request.full_url.startswith("http://write/workSessions"):
                body = json.loads(request.data.decode("utf-8"))
                self.assertEqual(body["startTime"], "09:30:00")
                self.assertEqual(body["userId"], 2)
                self.assertNotIn("password", body)
                return 200, json.dumps(
                    {"id": 9, "userId": 2, "date": body["date"], "startTime": body["startTime"], "endTime": None, "status": "active"}
                ).encode("utf-8")
            return 500, b""

        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "t.db")
            try:
                book = ShiftBook(db, self._client(transport))
                loaded = book.load()
                self.assertEqual(loaded["users"][0]["fullName"], "Иванова")
                self.assertNotIn("password", loaded["users"][0])
                cached_users, _sessions = db.load_directory()
                self.assertNotIn("password", cached_users[0])
                kind, text = book.start(2, "12.10.2026", "08:00")
                self.assertEqual(kind, "queued")
                self.assertIn("этом компьютере", text)
                view = book.load()
                self.assertEqual(person_state(sessions_for_user(view["sessions"], 2)), STATE_AT_WORK)
                session = sessions_for_user(view["sessions"], 2)[0]
                kind, _text = book.change_arrival(session, "12.10.2026", "09:30")
                self.assertEqual(kind, "queued")
                self.assertEqual(db.count_pending_session_ops(), 1)
                online["value"] = True
                book.flush_pending()
                self.assertEqual(db.count_pending_session_ops(), 0)
                posts = [item for item in seen if item[0] == "POST"]
                self.assertGreaterEqual(len(posts), 1)
                self.assertTrue(all(item[1].startswith("http://write/workSessions") for item in posts))
                self.assertEqual(json.loads(posts[-1][2].decode("utf-8"))["startTime"], "09:30:00")
                self.assertFalse(any("ignored" in item[1] for item in posts))
            finally:
                db.close()

    def test_closed_shift_patch_and_open_shift_keeps_end_empty(self) -> None:
        seen = []

        def transport(request, timeout, context):
            del timeout, context
            seen.append((request.method, request.full_url, request.data))
            if request.full_url.startswith("http://read/users"):
                return 200, b'[{"id": 3, "fullName": "\xd0\x9f\xd0\xb5\xd1\x82\xd1\x80\xd0\xbe\xd0\xb2"}]'
            if request.method == "GET" and request.full_url.startswith("http://read/workSessions"):
                body = (
                    '[{"id": 4, "userId": 3, "date": "2026-10-12", "startTime": "09:00:00", "endTime": "18:00:00", "status": "completed"},'
                    '{"id": 5, "userId": 3, "date": "2026-10-13", "startTime": "09:00:00", "endTime": null, "status": "active"}]'
                )
                return 200, body.encode("utf-8")
            if request.method == "PATCH":
                return 200, request.data
            return 500, b""

        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "t.db")
            try:
                book = ShiftBook(db, self._client(transport))
                view = book.load()
                rows = sessions_for_user(view["sessions"], 3)
                closed = next(row for row in rows if row["id"] == 4)
                opened = next(row for row in rows if row["id"] == 5)
                self.assertEqual(person_state(rows), STATE_AT_WORK)
                kind, text = book.change_closed(closed, "12.10.2026", "08:15", "12.10.2026", "17:40")
                self.assertEqual(kind, "sent")
                self.assertEqual(text, "Отправлено на сервер.")
                closed_body = json.loads(seen[-1][2].decode("utf-8"))
                self.assertEqual(seen[-1][0], "PATCH")
                self.assertTrue(seen[-1][1].startswith("http://write/workSessions/4"))
                self.assertEqual(closed_body["startTime"], "08:15:00")
                self.assertEqual(closed_body["endTime"], "17:40:00")
                self.assertEqual(closed_body["durationMinutes"], 565)
                kind, _text = book.change_arrival(opened, "13.10.2026", "09:10")
                self.assertEqual(kind, "sent")
                arrival_body = json.loads(seen[-1][2].decode("utf-8"))
                self.assertTrue(seen[-1][1].startswith("http://write/workSessions/5"))
                self.assertEqual(arrival_body["startTime"], "09:10:00")
                self.assertNotIn("endTime", arrival_body)
                self.assertEqual(db.count_pending_session_ops(), 0)
            finally:
                db.close()

    def test_overnight_shift_keeps_its_length_when_the_end_date_is_dropped(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        posted = []

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            if "example.test" in url:
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                return 200, b'[{"id": 3, "fullName": "\xd0\x9f\xd0\xb5\xd1\x82\xd1\x80\xd0\xbe\xd0\xb2", "badgeId": "55001"}]'
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                body = (
                    '[{"id": 4, "userId": 3, "date": "2026-10-13", "startTime": "08:00:00", '
                    '"endTime": "10:00:00", "durationMinutes": 120, "status": "completed"}]'
                )
                return 200, body.encode("utf-8")
            if request.method == "PATCH" and "id=eq.4" in url:
                body = json.loads(request.data.decode("utf-8"))
                posted.append(body)
                echo = {
                    "id": 4,
                    "userId": 3,
                    "date": "2026-10-13",
                    "startTime": body.get("startTime") or "08:00:00",
                    "endTime": body.get("endTime"),
                    "durationMinutes": 120,
                    "status": body.get("status") or "manually_edited",
                }
                return 200, json.dumps([echo]).encode("utf-8")
            return 500, b""

        settings = default_settings()
        settings["server"]["base_url"] = "https://example.test"
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["supabase_url"] = "https://db.test"
        settings["server"]["supabase_key"] = "anon-key"
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "t.db")
            try:
                book = ShiftBook(db, ApiClient(settings, transport=transport))
                view = book.load()
                closed = next(row for row in sessions_for_user(view["sessions"], 3) if row["id"] == 4)
                kind, text = book.change_closed(closed, "13.10.2026", "08:00", "14.10.2026", "10:00")
                self.assertEqual(kind, "sent")
                self.assertEqual(text, "Отправлено на сервер.")
                self.assertEqual(posted[0]["durationMinutes"], 1560)
                self.assertEqual(posted[0]["startTime"], "08:00:00")
                self.assertEqual(posted[0]["endTime"], "10:00:00")
                self.assertEqual(posted[0]["date"], "2026-10-13")
                self.assertNotIn("endDate", posted[0])
                _users, stored = db.load_directory()
                remembered = next(row for row in stored if row["id"] == 4)
                self.assertEqual(remembered["durationMinutes"], 1560)
                self.assertEqual(remembered["endDate"], "2026-10-14")
                server_row = {
                    "id": 4,
                    "userId": 3,
                    "date": "2026-10-13",
                    "startTime": "08:00:00",
                    "endTime": "10:00:00",
                    "durationMinutes": 1560,
                    "status": "completed",
                }
                end_day = format_date_ru(session_end_day(server_row))
                self.assertEqual(end_day, "14.10.2026")
                kind, _text = book.change_closed(server_row, "13.10.2026", "08:00", end_day, "10:00")
                self.assertEqual(kind, "sent")
                self.assertEqual(posted[-1]["durationMinutes"], 1560)
                self.assertNotIn("endDate", posted[-1])
            finally:
                db.close()

    def test_pending_shift_is_posted_once_and_survives_a_restart(self) -> None:
        posts = []

        def transport(request, timeout, context):
            del timeout, context
            if request.method == "POST" and "/workSessions" in request.full_url:
                posts.append(request.full_url)
                time.sleep(0.2)
                body = '{"id": 9, "userId": 2, "date": "2026-10-14", "startTime": "09:00:00", "endTime": null, "status": "active"}'
                return 200, body.encode("utf-8")
            return 500, b""

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "t.db"
            db = Database(path)
            try:
                book = ShiftBook(db, self._client(transport))
                op_id = db.insert_session_op(
                    "create",
                    "2",
                    "",
                    "local-1",
                    {
                        "userId": 2,
                        "date": "2026-10-14",
                        "startTime": "09:00:00",
                        "endTime": None,
                        "status": "active",
                        "source": "manual",
                    },
                )
                errors = []

                def run() -> None:
                    try:
                        book._flush_one(op_id)
                    except Exception as exc:
                        errors.append(exc)

                workers = [threading.Thread(target=run), threading.Thread(target=run)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(3)
                self.assertEqual(errors, [])
                self.assertEqual(len(posts), 1)
                self.assertEqual(db.get_session_op(op_id)["status"], "sent")
                stuck = db.insert_session_op(
                    "create",
                    "2",
                    "",
                    "local-2",
                    {"userId": 2, "date": "2026-10-14", "startTime": "10:00:00", "status": "active"},
                )
                self.assertTrue(db.claim_session_op(stuck))
                self.assertEqual(db.get_session_op(stuck)["status"], "sending")
            finally:
                db.close()
            reopened = Database(path)
            try:
                self.assertEqual(reopened.get_session_op(stuck)["status"], "pending")
            finally:
                reopened.close()
