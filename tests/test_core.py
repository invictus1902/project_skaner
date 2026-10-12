from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scanner_app.api import ApiClient, HttpResult, build_url, classify_post, extract_event, extract_name
from scanner_app.config import (
    deep_merge,
    default_settings,
    hash_password,
    load_settings,
    server_urls,
    validate_settings,
    verify_password,
)
from scanner_app.db import Database
from scanner_app.logic import decide_event, normalize_badge, plural, render_speech, speak_time
from scanner_app.scanner import ScanAssembler
from scanner_app.service import AttendanceService


class LogicTests(unittest.TestCase):
    def test_plurals(self) -> None:
        self.assertEqual(plural(1, "час", "часа", "часов"), "час")
        self.assertEqual(plural(2, "час", "часа", "часов"), "часа")
        self.assertEqual(plural(5, "час", "часа", "часов"), "часов")
        self.assertEqual(plural(11, "минута", "минуты", "минут"), "минут")
        self.assertEqual(plural(21, "час", "часа", "часов"), "час")

    def test_speak_time(self) -> None:
        moment = datetime(2026, 10, 12, 9, 5)
        self.assertEqual(speak_time(moment), "9 часов 5 минут")

    def test_normalize_aim_and_regex(self) -> None:
        scanner = default_settings()["scanner"]
        self.assertEqual(normalize_badge("]Q312345", scanner), "12345")
        scanner = dict(scanner)
        scanner["badge_regex"] = r"badge=([A-Za-z0-9]+)"
        self.assertEqual(normalize_badge("https://corp.local/pass?badge=AB12", scanner), "AB12")
        self.assertEqual(normalize_badge("нет-кода", scanner), "")

    def test_shift_decision(self) -> None:
        now = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(decide_event(None, None, now, 16), "in")
        self.assertEqual(decide_event("in", now - timedelta(hours=2), now, 16), "out")
        self.assertEqual(decide_event("out", now - timedelta(hours=1), now, 16), "in")
        self.assertEqual(decide_event("in", now - timedelta(hours=20), now, 16), "in")

    def test_render_speech_ignores_unknown_braces(self) -> None:
        text = render_speech("{name}. {event_phrase}. {time}.", {"name": "Анна", "event_phrase": "Приход на работу", "time": "9 часов 0 минут"})
        self.assertEqual(text, "Анна. Приход на работу. 9 часов 0 минут.")


class AssemblerTests(unittest.TestCase):
    def _assembler(self, **kwargs):
        bursts = []

        def on_burst(text, duration, terminated, fast):
            bursts.append((text, terminated, fast, duration))

        options = dict(min_length=3, max_length=20, max_interval_ms=80, idle_flush_ms=200, terminators=["enter"])
        options.update(kwargs)
        return ScanAssembler(on_burst=on_burst, **options), bursts

    def test_fast_scanner_with_enter(self) -> None:
        assembler, bursts = self._assembler()
        for index, char in enumerate("12345"):
            assembler.push_char(char, now=index * 0.02)
        assembler.push_terminator("enter", now=0.12)
        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0][0], "12345")
        self.assertTrue(bursts[0][1])
        self.assertTrue(bursts[0][2])

    def test_human_typing_is_ignored(self) -> None:
        assembler, bursts = self._assembler()
        for index, char in enumerate("12345"):
            assembler.push_char(char, now=index * 0.3)
        assembler.push_terminator("enter", now=1.6)
        self.assertEqual(bursts, [])

    def test_idle_flush_keeps_fast_burst(self) -> None:
        assembler, bursts = self._assembler()
        for index, char in enumerate("998877"):
            assembler.push_char(char, now=index * 0.02)
        assembler.tick(now=0.12 + 0.25)
        self.assertEqual(len(bursts), 1)
        self.assertEqual(bursts[0][0], "998877")
        self.assertFalse(bursts[0][1])
        self.assertTrue(bursts[0][2])


class ConfigTests(unittest.TestCase):
    def test_merge_keeps_new_defaults(self) -> None:
        defaults = {"server": {"base_url": "", "timeout_sec": 5}, "ui": {"title": "A"}}
        loaded = {"server": {"base_url": "http://localhost"}}
        merged = deep_merge(defaults, loaded)
        self.assertEqual(merged["server"]["base_url"], "http://localhost")
        self.assertEqual(merged["server"]["timeout_sec"], 5)
        self.assertEqual(merged["ui"]["title"], "A")

    def test_password_roundtrip(self) -> None:
        salt, digest = hash_password("secret-1")
        self.assertTrue(verify_password("secret-1", salt, digest))
        self.assertFalse(verify_password("secret-2", salt, digest))

    def test_validate_url(self) -> None:
        settings = default_settings()
        settings["server"]["base_url"] = "ftp://bad"
        errors = validate_settings(settings)
        self.assertTrue(any("http" in item for item in errors))


class ApiTests(unittest.TestCase):
    def test_build_url_quotes_badge(self) -> None:
        self.assertEqual(
            build_url("http://host/app/", "/api/employees/{badge}", "A B/1"),
            "http://host/app/api/employees/A%20B%2F1",
        )

    def test_extract_nested_name_and_event(self) -> None:
        self.assertEqual(extract_name({"employee": {"full_name": "Петров"}}), "Петров")
        self.assertEqual(extract_event({"last_event": "уход"}), "out")

    def test_classify(self) -> None:
        sent = classify_post(HttpResult(True, 200, {"ok": True, "name": "А"}, None, False))
        unknown = classify_post(HttpResult(False, 200, {"ok": False, "error": "unknown_badge"}, None, False))
        retry = classify_post(HttpResult(False, None, None, "Сервер не ответил вовремя", True))
        missing_path = classify_post(HttpResult(False, 404, None, None, False))
        self.assertEqual(sent[0], "sent")
        self.assertEqual(unknown[0], "unknown")
        self.assertEqual(retry[0], "retry")
        self.assertEqual(missing_path[0], "retry")
        site_unknown = classify_post(HttpResult(False, 200, {"success": False, "message": "Сотрудник не найден"}, None, False))
        self.assertEqual(site_unknown[0], "unknown")
        self.assertEqual(extract_event({"action": "start", "fullName": "Иванова"}), "in")
        self.assertEqual(extract_name({"fullName": "Иванова Мария"}), "Иванова Мария")

    def test_site_checkin_and_directory(self) -> None:
        seen = []

        def transport(request, timeout, context):
            seen.append((request.method, request.full_url, request.data))
            if request.full_url.endswith("/users"):
                body = '[{"id": 1, "fullName": "Иванова", "badgeId": "55001", "password": "secret"}]'
                return 200, body.encode("utf-8")
            if request.full_url.endswith("/workSessions"):
                return 200, b'[{"userId": 1, "status": "active"}]'
            if request.full_url.endswith("/login"):
                return 200, '{"token": "jwt-1", "fullName": "Сканер"}'.encode("utf-8")
            if request.full_url.endswith("/api/qr-checkin"):
                self.assertEqual(request.get_header("Authorization"), "Bearer jwt-1")
                return 200, '{"success": true, "action": "end", "fullName": "Иванова"}'.encode("utf-8")
            return 500, b""

        settings = default_settings()
        settings["server"]["base_url"] = "https://example.test"
        settings["server"]["login"] = "scanner"
        settings["server"]["password"] = "hidden"
        client = ApiClient(settings, transport=transport)
        employee = client.get_employee("55001")
        self.assertEqual(employee.data["fullName"], "Иванова")
        self.assertNotIn("password", employee.data)
        status = client.get_status("55001")
        self.assertEqual(status.data["last_event"], "in")
        posted = client.post_event({"badge": "55001", "event": "out"})
        self.assertEqual(extract_event(posted.data), "out")
        self.assertTrue(any(item[0] == "POST" and item[1].endswith("/api/qr-checkin") for item in seen))
        checkin = next(item for item in seen if item[1].endswith("/api/qr-checkin"))
        self.assertIn(b"badgeId", checkin[2])
        self.assertNotIn(b"hidden", checkin[2])
        self.assertIn("с бейджем 1", client.probe())
        self.assertNotIn("secret", client.probe())

    def test_vercel_login_page_is_not_a_directory(self) -> None:
        def transport(request, timeout, context):
            del request, timeout, context
            return 200, b"<html><title>Login</title>vercel sso-api</html>"

        settings = default_settings()
        settings["server"]["base_url"] = "https://example.test"
        client = ApiClient(settings, transport=transport)
        self.assertIn("Vercel", client.probe())
        employee = client.get_employee("55001")
        self.assertFalse(employee.ok)
        self.assertNotEqual(employee.status, 200)

    def test_vercel_protection_json_is_not_a_directory(self) -> None:
        def transport(request, timeout, context):
            del request, timeout, context
            body = '{"message":"Protected by Vercel Authentication","error":{"code":"401","message":"Protected deployment"},"protection":{"vercel_auth_enabled":true}}'
            return 401, body.encode("utf-8")

        settings = default_settings()
        settings["server"]["base_url"] = "https://example.test"
        client = ApiClient(settings, transport=transport)
        text = client.probe()
        self.assertIn("Vercel", text)
        self.assertIn("localhost:8080", text)
        self.assertNotIn("не списком", text)

    def test_blocked_host_reads_the_site_database(self) -> None:
        seen = []
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'

        def transport(request, timeout, context):
            del timeout, context
            seen.append(
                (
                    request.method,
                    request.full_url,
                    request.data,
                    request.get_header("Apikey"),
                    request.get_header("Authorization"),
                    request.get_header("Prefer"),
                )
            )
            url = request.full_url
            if "example.test" in url:
                return 401, wall.encode("utf-8")
            if request.method == "GET" and url.startswith("https://db.test/rest/v1/users"):
                body = '[{"id": 1, "fullName": "Иванова", "badgeId": "55001", "role": "admin", "password": "secret"}]'
                return 200, body.encode("utf-8")
            if request.method == "GET" and url.startswith("https://db.test/rest/v1/workSessions"):
                return 200, b'[{"id": 7, "userId": 1, "date": "2026-10-13", "startTime": "09:00:00", "endTime": null, "status": "active"}]'
            if request.method == "POST" and url.startswith("https://db.test/rest/v1/workSessions"):
                return 201, b'[{"id": 11, "userId": 1, "startTime": "09:00:00", "endTime": null}]'
            return 500, b""

        settings = default_settings()
        settings["server"]["base_url"] = "https://example.test"
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["supabase_url"] = "https://db.test"
        settings["server"]["supabase_key"] = "anon-key"
        client = ApiClient(settings, transport=transport)
        users, sessions = client.load_people()
        self.assertEqual(users.data[0]["fullName"], "Иванова")
        self.assertNotIn("password", users.data[0])
        self.assertEqual(sessions.data[0]["id"], 7)
        self.assertIn("В справочнике 1 человек", client.probe())
        self.assertNotIn("secret", client.probe())
        self.assertNotIn("Vercel", client.probe())
        self.assertEqual(client.health_state(), "online")
        employee = client.get_employee("55001")
        self.assertEqual(employee.data["fullName"], "Иванова")
        status = client.get_status("55001")
        self.assertEqual(status.data["last_event"], "in")
        written = client.write_session(
            {"op": "create", "payload": {"userId": 1, "date": "2026-10-13", "startTime": "09:00:00", "endDate": "2026-10-13", "status": "active"}}
        )
        self.assertTrue(written.ok)
        self.assertEqual(written.data["id"], 11)
        self.assertIsInstance(written.data, dict)
        user_gets = [item for item in seen if item[0] == "GET" and item[1].startswith("https://db.test/rest/v1/users")]
        self.assertTrue(user_gets)
        self.assertTrue(all(item[3] == "anon-key" and item[4] == "Bearer anon-key" for item in user_gets))
        posts = [item for item in seen if item[0] == "POST" and item[1].startswith("https://db.test/rest/v1/workSessions")]
        self.assertEqual(len(posts), 1)
        posted = json.loads(posts[0][2].decode("utf-8"))
        self.assertEqual(posted["userId"], 1)
        self.assertNotIn("endDate", posted)
        self.assertNotIn("password", posted)
        self.assertEqual(posts[0][5], "return=representation")
        self.assertFalse(any("anon-key" in item[1] for item in seen))

    def test_admin_sign_in_authorizes_checkin_without_leaking_password(self) -> None:
        seen = []

        def transport(request, timeout, context):
            del timeout, context
            seen.append((request.method, request.full_url, request.data, request.get_header("Authorization")))
            url = request.full_url
            if url.endswith("/users"):
                body = (
                    '[{"id": 2, "fullName": "Эмир", "login": "emir", "role": "admin", "password": "hash"},'
                    '{"id": 3, "fullName": "Гость", "login": "guest", "role": "user", "password": "hash"}]'
                )
                return 200, body.encode("utf-8")
            if url.endswith("/login"):
                posted = json.loads(request.data.decode("utf-8"))
                self.assertEqual(posted["email"], "emir")
                self.assertEqual(posted["password"], "secret-pass")
                return 200, '{"token": "jwt-9", "fullName": "Эмир", "role": "admin", "password": "secret-pass"}'.encode("utf-8")
            if url.endswith("/api/qr-checkin"):
                return 200, '{"success": true, "action": "start", "fullName": "Эмир"}'.encode("utf-8")
            return 500, b""

        settings = default_settings()
        settings["server"]["read_url"] = "http://read"
        settings["server"]["write_url"] = "http://write"
        settings["server"]["base_url"] = "http://write"
        client = ApiClient(settings, transport=transport)
        people, error = client.operator_profiles()
        self.assertEqual(error, "")
        self.assertEqual([item["login"] for item in people], ["emir"])
        self.assertTrue(all("password" not in item for item in people))
        signed = client.sign_in("emir", "secret-pass")
        self.assertTrue(signed.ok)
        self.assertEqual(signed.data["fullName"], "Эмир")
        self.assertEqual(signed.data["role"], "admin")
        self.assertNotIn("token", signed.data)
        self.assertNotIn("password", signed.data)
        self.assertNotIn("secret-pass", signed.error or "")
        settings["server"]["login"] = "emir"
        settings["server"]["password"] = "secret-pass"
        client.update(settings)
        posted = client.post_event({"badge": "1", "event": "in"})
        self.assertTrue(posted.ok)
        logins = [item for item in seen if item[1].endswith("/login")]
        self.assertEqual(len(logins), 1)
        checkin = next(item for item in seen if item[1].endswith("/api/qr-checkin"))
        self.assertEqual(checkin[3], "Bearer jwt-9")
        self.assertNotIn(b"secret-pass", checkin[2])
        self.assertFalse(any("secret-pass" in item[1] for item in seen))
        client.sign_out()
        self.assertEqual(client._session_token, "")

    def test_sign_in_rejects_wall_wrong_password_and_plain_user(self) -> None:
        mode = {"value": "wall"}

        def transport(request, timeout, context):
            del timeout, context
            if mode["value"] == "wall":
                body = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
                return 401, body.encode("utf-8")
            if mode["value"] == "bad":
                return 401, b'{"error":"Invalid credentials"}'
            return 200, '{"token": "jwt", "fullName": "Гость", "role": "user"}'.encode("utf-8")

        settings = default_settings()
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["base_url"] = "https://example.test"
        client = ApiClient(settings, transport=transport)
        blocked = client.sign_in("emir", "secret-pass")
        self.assertFalse(blocked.ok)
        self.assertIn("Vercel", blocked.error or "")
        self.assertNotIn("secret-pass", blocked.error or "")
        self.assertEqual(client._session_token, "")
        mode["value"] = "bad"
        rejected = client.sign_in("emir", "secret-pass")
        self.assertFalse(rejected.ok)
        self.assertIn("не принял", rejected.error or "")
        self.assertNotIn("secret-pass", rejected.error or "")
        mode["value"] = "user"
        plain = client.sign_in("guest", "secret-pass")
        self.assertFalse(plain.ok)
        self.assertIn("не может", plain.error or "")
        self.assertEqual(client._session_token, "")

    def test_read_and_write_urls_stay_separate(self) -> None:
        seen = []

        def transport(request, timeout, context):
            del timeout, context
            seen.append((request.method, request.full_url, request.data))
            if request.full_url.startswith("http://read") and request.full_url.endswith("/users"):
                body = '[{"id": 2, "fullName": "Иванова", "badgeId": "55001", "role": "admin"}]'
                return 200, body.encode("utf-8")
            if request.full_url.startswith("http://read") and request.full_url.endswith("/workSessions"):
                return 200, b"[]"
            if request.full_url.startswith("http://write") and request.full_url.endswith("/api/qr-checkin"):
                self.assertEqual(request.get_header("Authorization"), "Bearer jwt-1")
                return 200, '{"success": true, "action": "start", "fullName": "Сотрудник"}'.encode("utf-8")
            return 500, b""

        settings = default_settings()
        settings["server"]["read_url"] = "http://read"
        settings["server"]["write_url"] = "http://write"
        settings["server"]["base_url"] = "http://ignored"
        settings["server"]["token"] = "jwt-1"
        client = ApiClient(settings, transport=transport)
        employee = client.get_employee("55001")
        self.assertEqual(employee.data["fullName"], "Иванова")
        client.post_event({"badge": "55001", "event": "in"})
        self.assertTrue(any(url.startswith("http://read/users") for _method, url, _body in seen))
        self.assertTrue(any(url.startswith("http://write/api/qr-checkin") for _method, url, _body in seen))
        self.assertFalse(any("ignored" in url for _method, url, _body in seen))
        self.assertFalse(any(url.endswith("/login") for _method, url, _body in seen))

    def test_old_single_address_fills_both_links(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text(
                json.dumps({"server": {"base_url": "http://192.168.1.5:8080", "profile": "site"}}),
                encoding="utf-8",
            )
            loaded = load_settings(path)
            self.assertEqual(server_urls(loaded["server"]), ("http://192.168.1.5:8080", "http://192.168.1.5:8080"))
            self.assertEqual(loaded["server"]["read_url"], "http://192.168.1.5:8080")
            self.assertEqual(loaded["server"]["write_url"], "http://192.168.1.5:8080")

    def test_client_uses_transport(self) -> None:
        seen = {}

        def transport(request, timeout, context):
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            seen["auth"] = request.get_header("Authorization")
            return 200, b'{"ok": true}'

        settings = default_settings()
        settings["server"]["profile"] = "generic"
        settings["server"]["base_url"] = "http://server"
        settings["server"]["token"] = "secret"
        client = ApiClient(settings, transport=transport)
        result = client.post_event({"badge": "1", "event": "in"})
        self.assertTrue(result.ok)
        self.assertEqual(seen["url"], "http://server/api/attendance/events")
        self.assertEqual(seen["auth"], "Bearer secret")
        self.assertEqual(client.health_state(), "online")


class DatabaseTests(unittest.TestCase):
    def test_pending_and_last_event(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "t.db")
            moment = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
            event_id = db.insert_event("10", "Иван", "in", moment, {"badge": "10"})
            self.assertEqual(db.count_pending(), 1)
            self.assertEqual(db.last_counted_event("10")["event_type"], "in")
            db.mark_sent(event_id, "Иван Иванов", "in")
            self.assertEqual(db.count_pending(), 0)
            self.assertEqual(db.last_counted_event("10")["status"], "sent")
            db.upsert_employee("10", "Иван Иванов", "manual")
            self.assertEqual(db.get_employee("10")["name"], "Иван Иванов")
            db.close()


class FakeUi:
    def __init__(self) -> None:
        self.views = []
        self.pending = None

    def show(self, view) -> None:
        self.views.append(view)

    def refresh_history(self) -> None:
        pass

    def set_pending(self, count) -> None:
        self.pending = count

    def debug(self, text) -> None:
        pass


class FakeSpeaker:
    def __init__(self) -> None:
        self.said = []

    def say(self, text, options) -> None:
        self.said.append(text)


class StubApi:
    def __init__(self) -> None:
        self.posts = []
        self.employee = HttpResult(False, None, None, "down", True)
        self.status = HttpResult(False, None, None, "down", True)
        self.post = HttpResult(False, None, None, "down", True)

    def update(self, settings) -> None:
        del settings

    def get_employee(self, badge):
        del badge
        return self.employee

    def get_status(self, badge):
        del badge
        return self.status

    def post_event(self, payload):
        self.posts.append(payload)
        if self.post.network_error or not isinstance(self.post.data, dict):
            return self.post
        data = dict(self.post.data)
        data.setdefault("event", payload["event"])
        return HttpResult(self.post.ok, self.post.status, data, self.post.error, False)


class ServiceTests(unittest.TestCase):
    def _service(self, folder: str, base_url: str = ""):
        settings = default_settings()
        settings["server"]["base_url"] = base_url
        settings["scanner"]["cooldown_sec"] = 0
        settings["voice"]["enabled"] = True
        settings["voice"]["beep"] = False
        db = Database(Path(folder) / "t.db")
        ui = FakeUi()
        speaker = FakeSpeaker()
        api = StubApi()
        service = AttendanceService(settings, db, api, speaker, ui)
        return service, db, ui, speaker, api

    def test_local_in_then_out_when_server_is_down(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, speaker, api = self._service(folder)
            try:
                service._process("12345")
                service._process("12345")
                self.assertEqual([row["event_type"] for row in db.recent_events(5)][::-1], ["in", "out"])
                self.assertEqual(db.count_pending(), 2)
                self.assertEqual(ui.views[-1]["state"], "offline")
                self.assertIn("Приход", speaker.said[0])
                self.assertIn("Уход", speaker.said[1])
                self.assertIn("минут", speaker.said[0])
                self.assertEqual(api.posts, [])
            finally:
                db.close()

    def test_server_open_shift_becomes_departure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                api.employee = HttpResult(True, 200, {"name": "Иванов Иван", "active": True}, None, False)
                api.status = HttpResult(True, 200, {"last_event": "in", "name": "Иванов Иван"}, None, False)
                api.post = HttpResult(True, 200, {"ok": True, "name": "Иванов Иван"}, None, False)
                service._process("55001")
                self.assertEqual(api.posts[0]["event"], "out")
                self.assertEqual(api.posts[0]["name"], "Иванов Иван")
                self.assertEqual(ui.views[-1]["state"], "out")
                self.assertEqual(ui.views[-1]["name"], "Иванов Иван")
                self.assertEqual(db.count_pending(), 0)
            finally:
                db.close()

    def test_unknown_badge_is_not_queued(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                service.settings["server"]["strict_employee_check"] = True
                api.employee = HttpResult(False, 404, {"error": "unknown_badge"}, None, False)
                service._process("404")
                self.assertEqual(api.posts, [])
                self.assertEqual(ui.views[-1]["state"], "unknown")
                self.assertEqual(db.count_pending(), 0)
            finally:
                db.close()

    def test_queue_is_sent_later(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, _ui, _speaker, api = self._service(folder, "http://server")
            try:
                service._process("77001")
                self.assertEqual(db.count_pending(), 1)
                api.post = HttpResult(True, 200, {"ok": True, "event": "in", "name": "Семён"}, None, False)
                service._flush_pending()
                self.assertEqual(db.count_pending(), 0)
                self.assertEqual(db.get_employee("77001")["name"], "Семён")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
