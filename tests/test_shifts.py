from __future__ import annotations

import json
import tempfile
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
    person_state,
    sessions_for_user,
)


class ShiftLogicTests(unittest.TestCase):
    def test_states_and_duration(self) -> None:
        self.assertEqual(person_state([]), STATE_NEVER)
        self.assertEqual(person_state([{"endTime": "18:00:00"}]), STATE_AWAY)
        self.assertEqual(person_state([{"endTime": None}, {"endTime": "18:00:00"}]), STATE_AT_WORK)
        self.assertEqual(duration_minutes("2026-10-12", "22:00", "2026-10-13", "06:00"), 480)
        self.assertEqual(duration_minutes("12.10.2026", "22:00", "12.10.2026", "06:00"), 480)
        self.assertIsNone(duration_minutes("12.10.2026", "10:00", "10.10.2026", "09:00"))


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
