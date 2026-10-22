from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
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
from scanner_app.logic import decide_event, manual_badge, normalize_badge, plural, render_speech, speak_time
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

    def test_manual_badge_keeps_symbols_and_short_codes(self) -> None:
        scanner = dict(default_settings()["scanner"])
        scanner["badge_regex"] = r"badge=([A-Za-z0-9]+)"
        scanner["min_length"] = 3
        self.assertEqual(manual_badge("12", scanner), "12")
        self.assertEqual(manual_badge("A-12/б", scanner), "A-12/б")
        self.assertEqual(manual_badge("]Q312345", scanner), "12345")
        self.assertEqual(manual_badge("   ", scanner), "")
        self.assertEqual(manual_badge("https://corp.local/pass?badge=AB12", scanner), "AB12")

    def test_shift_decision(self) -> None:
        now = datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(decide_event(None, None, now, 16), "in")
        self.assertEqual(decide_event("in", now - timedelta(hours=2), now, 16), "out")
        self.assertEqual(decide_event("out", now - timedelta(hours=1), now, 16), "in")
        self.assertEqual(decide_event("in", now - timedelta(hours=20), now, 16), "in")
        self.assertEqual(decide_event("in", now + timedelta(minutes=5), now, 16), "out")

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

    def test_closed_checkin_writes_the_shift_like_manual_edit(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        store = {"rows": []}
        seen = []

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            seen.append((request.method, url, request.data, request.get_header("Apikey")))
            if url.startswith("https://example.test"):
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                body = '[{"id": 2, "fullName": "Эмир", "badgeId": "adm001", "password": "secret"}]'
                return 200, body.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                return 200, json.dumps(store["rows"]).encode("utf-8")
            if request.method == "POST" and "/rest/v1/workSessions" in url:
                body = json.loads(request.data.decode("utf-8"))
                row = {"id": 118, "userId": body["userId"], "date": body["date"], "startTime": body["startTime"], "endTime": None, "status": "active"}
                store["rows"] = [row]
                return 201, json.dumps([row]).encode("utf-8")
            if request.method == "PATCH" and "id=eq.118" in url:
                body = json.loads(request.data.decode("utf-8"))
                store["rows"][0].update(body)
                return 200, json.dumps([store["rows"][0]]).encode("utf-8")
            return 500, b""

        settings = default_settings()
        settings["server"]["base_url"] = "https://example.test"
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["supabase_url"] = "https://db.test"
        settings["server"]["supabase_key"] = "anon-key"
        settings["server"]["login"] = ""
        settings["server"]["password"] = ""
        client = ApiClient(settings, transport=transport)
        started = client.post_event(
            {"badge": "adm001", "event": "in", "timestamp": "2026-10-14T09:15:04+06:00", "name": "Эмир"}
        )
        self.assertTrue(started.ok)
        self.assertEqual(extract_event(started.data), "in")
        self.assertEqual(store["rows"][0]["startTime"], "09:15:04")
        self.assertEqual(store["rows"][0]["status"], "active")
        finished = client.post_event(
            {"badge": "adm001", "event": "out", "timestamp": "2026-10-14T18:05:00+06:00", "name": "Эмир"}
        )
        self.assertTrue(finished.ok)
        self.assertEqual(extract_event(finished.data), "out")
        self.assertEqual(store["rows"][0]["status"], "completed")
        self.assertEqual(store["rows"][0]["endTime"], "18:05:00")
        self.assertEqual(store["rows"][0]["durationMinutes"], 530)
        posts = [item for item in seen if item[0] in ("POST", "PATCH") and "db.test" in item[1]]
        self.assertEqual(len(posts), 2)
        for item in posts:
            self.assertEqual(item[3], "anon-key")
            self.assertNotIn("anon-key", item[1])
            self.assertNotIn(b"secret", item[2])
            self.assertNotIn(b"password", item[2])
        self.assertTrue(any(item[0] == "POST" and json.loads(item[2].decode("utf-8"))["source"] == "qr" for item in posts))
        self.assertFalse(any(item[1].endswith("/api/qr-checkin") for item in seen))

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
        hosted_sessions = [
            item[1]
            for item in seen
            if item[0] == "GET" and item[1].startswith("https://db.test/rest/v1/workSessions")
        ]
        self.assertTrue(hosted_sessions)
        self.assertTrue(all("order=id.desc" in url for url in hosted_sessions))
        self.assertEqual(client.canonical_badge("55001"), "55001")
        self.assertEqual(client.canonical_badge("  55001  "), "55001")

    def test_directory_is_remembered_and_a_closed_host_is_skipped(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        hits = {"host": 0, "users": 0, "sessions": 0}

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            if "db.test" not in url:
                hits["host"] += 1
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                hits["users"] += 1
                return 200, '[{"id": 2, "fullName": "Эмир", "badgeId": "adm001"}]'.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                hits["sessions"] += 1
                return 200, b"[]"
            return 500, b""

        settings = default_settings()
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["base_url"] = "https://example.test"
        settings["server"]["supabase_url"] = "https://db.test"
        settings["server"]["supabase_key"] = "anon-key"
        client = ApiClient(settings, transport=transport)
        self.assertEqual(client.get_employee("adm001").data["fullName"], "Эмир")
        self.assertEqual(client.get_status("adm001").data["last_event"], "out")
        self.assertEqual(hits, {"host": 1, "users": 1, "sessions": 1})
        client.update(copy.deepcopy(settings))
        self.assertEqual(client.get_employee("ADM001").data["fullName"], "Эмир")
        self.assertEqual(client.get_status("adm001").data["last_event"], "out")
        self.assertEqual(hits, {"host": 1, "users": 1, "sessions": 1})
        users, _sessions = client.load_people()
        users.data.clear()
        again, _sessions = client.load_people()
        self.assertEqual(again.data[0]["fullName"], "Эмир")
        self.assertEqual(hits["users"], 1)
        changed = copy.deepcopy(settings)
        changed["server"]["read_url"] = "https://other.test"
        changed["server"]["write_url"] = "https://other.test"
        changed["server"]["base_url"] = "https://other.test"
        client.update(changed)
        client.get_employee("adm001")
        self.assertGreater(hits["host"], 1)

    def test_written_shift_stays_in_memory_for_the_next_scan(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        store = {"rows": []}
        gets = {"sessions": 0}

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            if "example.test" in url:
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                return 200, '[{"id": 2, "fullName": "Эмир", "badgeId": "adm001"}]'.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                gets["sessions"] += 1
                return 200, b"[]"
            if request.method == "POST" and "/rest/v1/workSessions" in url:
                body = json.loads(request.data.decode("utf-8"))
                row = {
                    "id": 5,
                    "userId": body["userId"],
                    "date": body["date"],
                    "startTime": body["startTime"],
                    "endTime": None,
                    "status": "active",
                }
                store["rows"] = [row]
                return 201, json.dumps([row]).encode("utf-8")
            if request.method == "PATCH" and "id=eq.5" in url:
                body = json.loads(request.data.decode("utf-8"))
                store["rows"][0].update(body)
                return 200, json.dumps([store["rows"][0]]).encode("utf-8")
            return 500, b""

        settings = default_settings()
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["base_url"] = "https://example.test"
        settings["server"]["supabase_url"] = "https://db.test"
        settings["server"]["supabase_key"] = "anon-key"
        client = ApiClient(settings, transport=transport)
        started = client.post_event(
            {"badge": "adm001", "event": "in", "timestamp": "2026-10-14T09:15:04+06:00"}
        )
        self.assertTrue(started.ok)
        self.assertEqual(gets["sessions"], 1)
        finished = client.post_event(
            {"badge": "adm001", "event": "out", "timestamp": "2026-10-14T18:05:00+06:00"}
        )
        self.assertTrue(finished.ok)
        self.assertEqual(extract_event(finished.data), "out")
        self.assertEqual(gets["sessions"], 1)
        self.assertEqual(store["rows"][0]["status"], "completed")
        self.assertEqual(store["rows"][0]["endTime"], "18:05:00")

    def test_ordinary_rejection_does_not_skip_the_server(self) -> None:
        hits = {"n": 0}

        def transport(request, timeout, context):
            del request, timeout, context
            hits["n"] += 1
            return 401, b'{"message":"no key"}'

        settings = default_settings()
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["base_url"] = "https://example.test"
        client = ApiClient(settings, transport=transport)
        client.get_employee("55001")
        client.get_employee("55001")
        self.assertEqual(hits["n"], 2)

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

    def test_canonical_badge_follows_directory_spelling(self) -> None:
        def transport(request, timeout, context):
            del timeout, context
            if request.full_url.startswith("http://read/users"):
                body = '[{"id": 2, "fullName": "Эмир", "badgeId": "Adm001"}]'
                return 200, body.encode("utf-8")
            return 500, b""

        settings = default_settings()
        settings["server"]["read_url"] = "http://read"
        settings["server"]["write_url"] = "http://write"
        settings["server"]["base_url"] = "http://read"
        client = ApiClient(settings, transport=transport)
        self.assertEqual(client.canonical_badge("adm001"), "Adm001")
        self.assertEqual(client.canonical_badge("  ADM001 "), "Adm001")
        self.assertEqual(client.canonical_badge("нет-такого"), "нет-такого")

    def _walled_client(self, transport):
        settings = default_settings()
        settings["server"]["base_url"] = "https://example.test"
        settings["server"]["read_url"] = "https://example.test"
        settings["server"]["write_url"] = "https://example.test"
        settings["server"]["supabase_url"] = "https://db.test"
        settings["server"]["supabase_key"] = "anon-key"
        return ApiClient(settings, transport=transport)

    def test_fresh_open_shift_is_closed_instead_of_a_second_arrival(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        store = {
            "rows": [
                {
                    "id": 9,
                    "userId": 2,
                    "date": "2026-10-14",
                    "startTime": "09:00:00",
                    "endTime": None,
                    "status": "active",
                }
            ]
        }
        seen = []

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            seen.append((request.method, url))
            if url.startswith("https://example.test"):
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                return 200, '[{"id": 2, "fullName": "Эмир", "badgeId": "adm001"}]'.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                return 200, json.dumps(store["rows"]).encode("utf-8")
            if request.method == "PATCH" and "id=eq.9" in url:
                body = json.loads(request.data.decode("utf-8"))
                store["rows"][0].update(body)
                return 200, json.dumps([store["rows"][0]]).encode("utf-8")
            return 500, b""

        client = self._walled_client(transport)
        result = client.post_event(
            {"badge": "adm001", "event": "in", "timestamp": "2026-10-14T12:00:00+06:00"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(extract_event(result.data), "out")
        self.assertEqual(store["rows"][0]["status"], "completed")
        self.assertEqual(store["rows"][0]["endTime"], "12:00:00")
        self.assertEqual(store["rows"][0]["durationMinutes"], 180)
        self.assertFalse(any(method == "POST" and "workSessions" in url for method, url in seen))

    def test_stale_open_shift_is_closed_and_a_new_one_starts(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        store = {
            "rows": [
                {
                    "id": 9,
                    "userId": 2,
                    "date": "2026-10-13",
                    "startTime": "08:00:00",
                    "endTime": None,
                    "status": "active",
                }
            ]
        }

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            if url.startswith("https://example.test"):
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                return 200, '[{"id": 2, "fullName": "Эмир", "badgeId": "adm001"}]'.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                return 200, json.dumps(store["rows"]).encode("utf-8")
            if request.method == "PATCH" and "id=eq.9" in url:
                body = json.loads(request.data.decode("utf-8"))
                store["rows"][0].update(body)
                return 200, json.dumps([store["rows"][0]]).encode("utf-8")
            if request.method == "POST" and "/rest/v1/workSessions" in url:
                body = json.loads(request.data.decode("utf-8"))
                row = {
                    "id": 10,
                    "userId": body["userId"],
                    "date": body["date"],
                    "startTime": body["startTime"],
                    "endTime": None,
                    "status": "active",
                }
                store["rows"].append(row)
                return 201, json.dumps([row]).encode("utf-8")
            return 500, b""

        client = self._walled_client(transport)
        result = client.post_event(
            {"badge": "adm001", "event": "in", "timestamp": "2026-10-14T18:00:00+06:00"}
        )
        self.assertTrue(result.ok)
        self.assertEqual(extract_event(result.data), "in")
        self.assertEqual(store["rows"][0]["status"], "completed")
        self.assertEqual(store["rows"][0]["endTime"], "18:00:00")
        self.assertEqual(store["rows"][0]["durationMinutes"], 2040)
        self.assertEqual(store["rows"][1]["status"], "active")
        self.assertEqual(store["rows"][1]["startTime"], "18:00:00")
        self.assertEqual(store["rows"][1]["date"], "2026-10-14")

    def test_empty_shift_representation_is_not_a_saved_shift(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            if url.startswith("https://example.test"):
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                return 200, '[{"id": 2, "fullName": "Эмир", "badgeId": "adm001"}]'.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                return 200, b"[]"
            if request.method == "POST" and "/rest/v1/workSessions" in url:
                return 201, b"[]"
            return 500, b""

        client = self._walled_client(transport)
        result = client.post_event(
            {"badge": "adm001", "event": "in", "timestamp": "2026-10-14T09:00:00+06:00"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 422)

    def test_open_shift_past_the_row_cap_is_still_the_current_shift(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        recent = [
            {
                "id": 2000 + index,
                "userId": 9,
                "date": "2026-10-14",
                "startTime": "09:00:00",
                "endTime": "18:00:00",
                "status": "completed",
            }
            for index in range(1000)
        ]
        forgotten = {
            "id": 4,
            "userId": 2,
            "date": "2026-09-01",
            "startTime": "09:00:00",
            "endTime": None,
            "status": "active",
        }
        patched = {"value": False}

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            if url.startswith("https://example.test"):
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                return 200, '[{"id": 2, "fullName": "Эмир", "badgeId": "adm001"}]'.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                if "endTime.is.null" in url:
                    return 200, json.dumps([forgotten]).encode("utf-8")
                return 200, json.dumps(recent).encode("utf-8")
            if request.method == "PATCH" and "id=eq.4" in url:
                patched["value"] = True
                body = json.loads(request.data.decode("utf-8"))
                row = dict(forgotten)
                row.update(body)
                return 200, json.dumps([row]).encode("utf-8")
            return 500, b""

        client = self._walled_client(transport)
        status = client.get_status("adm001")
        self.assertEqual(status.data["last_event"], "in")
        self.assertIn("2026-09-01", status.data["last_time"])
        # Смена старше окна, поэтому приход закрывает её и открывает новую.
        # Здесь проверяем, что закрывается именно забытая строка, а не «её нет».
        result = client.post_event(
            {"badge": "adm001", "event": "out", "timestamp": "2026-10-14T18:00:00+06:00"}
        )
        self.assertTrue(result.ok)
        self.assertTrue(patched["value"])
        self.assertEqual(extract_event(result.data), "out")

    def test_repeat_lookup_does_not_download_the_directory_again(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        calls = []

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            calls.append(url)
            if url.startswith("https://example.test"):
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                body = json.dumps(
                    [
                        {"id": 1, "fullName": "Первый", "badgeId": "Ab"},
                        {"id": 2, "fullName": "Второй", "badgeId": "ab"},
                    ]
                )
                return 200, body.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                body = json.dumps(
                    [
                        {
                            "id": 3,
                            "userId": 2,
                            "date": "2026-10-14",
                            "startTime": "08:00:00",
                            "endTime": None,
                            "status": "active",
                        },
                        {
                            "id": 10,
                            "userId": 2,
                            "date": "2026-10-14",
                            "startTime": "11:00:00",
                            "endTime": None,
                            "status": "active",
                        },
                    ]
                )
                return 200, body.encode("utf-8")
            return 500, b""

        client = self._walled_client(transport)
        self.assertEqual(extract_name(client.get_employee("Ab").data), "Первый")
        self.assertEqual(extract_name(client.get_employee("AB").data), "Второй")
        status = client.get_status("ab")
        self.assertEqual(status.data["last_event"], "in")
        self.assertIn("11:00:00", status.data["last_time"])
        downloaded = len(calls)
        self.assertEqual(client.get_status("ab").data["last_event"], "in")
        self.assertEqual(client.canonical_badge("Ab"), "Ab")
        self.assertEqual(client.health_state(), "online")
        self.assertEqual(len(calls), downloaded)

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
            for kind in ("in", "in", "out", "in", "in"):
                db.insert_event("77", "Иван", kind, moment, {"badge": "77", "event": kind})
            self.assertEqual(db.suppress_repeated_pending("77"), 2)
            self.assertEqual([row["event_type"] for row in db.pending_events() if row["badge"] == "77"], ["in", "out", "in"])
            self.assertFalse(db.has_older_pending("77", db.pending_events()[0]["id"]))
            held = db.insert_event("88", "Иван", "in", moment, {"badge": "88", "event": "in"})
            self.assertTrue(db.claim_event(held))
            self.assertFalse(db.claim_event(held))
            self.assertEqual(db.count_pending(), 4)
            self.assertFalse(any(row["badge"] == "88" for row in db.pending_events()))
            self.assertTrue(db.has_older_pending("88", held + 1))
            duplicate = db.insert_event("88", "Иван", "in", moment, {"badge": "88", "event": "in"})
            self.assertEqual(db.suppress_repeated_pending("88"), 1)
            self.assertEqual(db.get_event(duplicate)["status"], "rejected")
            self.assertEqual(db.get_event(held)["status"], "sending")
            db.close()
            reopened = Database(Path(folder) / "t.db")
            try:
                self.assertEqual(reopened.get_event(held)["status"], "pending")
                self.assertIn(held, [row["id"] for row in reopened.pending_events()])
            finally:
                reopened.close()


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

    def test_manual_symbolic_code_is_stored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, _api = self._service(folder)
            try:
                service.settings["scanner"]["badge_regex"] = r"badge=([A-Za-z0-9]+)"
                service.settings["scanner"]["min_length"] = 4
                service._process("№15-А", manual=True)
                self.assertEqual(db.recent_events(1)[0]["badge"], "№15-А")
                self.assertEqual(ui.views[-1]["state"], "offline")
                service._process("№15-А", manual=False)
                self.assertEqual(ui.views[-1]["title"], "КОД НЕ РАСПОЗНАН")
            finally:
                db.close()

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

    def test_named_unknown_badge_is_rejected_without_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                api.employee = HttpResult(False, 404, {"error": "unknown_badge"}, None, False)
                service._process("404")
                self.assertEqual(ui.views[-1]["state"], "unknown")
                self.assertEqual(ui.views[-1]["title"], "БЕЙДЖ НЕ НАЙДЕН")
                self.assertEqual(api.posts, [])
                self.assertEqual(db.count_pending(), 0)
            finally:
                db.close()

    def test_plain_404_without_strict_mode_is_still_stored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                api.employee = HttpResult(False, 404, {}, None, False)
                service._process("4040")
                self.assertEqual(ui.views[-1]["state"], "offline")
                self.assertEqual(db.pending_events()[0]["event_type"], "in")
            finally:
                db.close()

    def test_unknown_badge_does_not_eat_the_next_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                service.settings["scanner"]["cooldown_sec"] = 30
                service.settings["server"]["strict_employee_check"] = True
                api.employee = HttpResult(False, 404, {"error": "unknown_badge"}, None, False)
                service._process("55001")
                service._process("55001")
                self.assertEqual([view["state"] for view in ui.views], ["unknown", "unknown"])
            finally:
                db.close()

    def test_cooldown_blocks_a_second_real_scan(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, _api = self._service(folder, "http://server")
            try:
                service.settings["scanner"]["cooldown_sec"] = 30
                service._process("55001")
                service._process("55001")
                self.assertEqual(ui.views[-1]["state"], "duplicate")
                self.assertEqual([row["event_type"] for row in db.pending_events()], ["in"])
            finally:
                db.close()

    def test_stale_server_shift_starts_a_new_arrival(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, _db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                old = (datetime.now().astimezone() - timedelta(hours=20)).isoformat()
                api.employee = HttpResult(True, 200, {"name": "Иван", "active": True}, None, False)
                api.status = HttpResult(True, 200, {"last_event": "in", "last_time": old, "name": "Иван"}, None, False)
                api.post = HttpResult(True, 200, {"ok": True, "event": "in", "name": "Иван"}, None, False)
                service._process("10001")
                self.assertEqual(api.posts[0]["event"], "in")
                self.assertEqual(ui.views[-1]["title"], "ПРИХОД")
            finally:
                _db.close()

    def test_waiting_shift_does_not_block_other_badges(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, _ui, _speaker, _api = self._service(folder, "http://server")
            try:
                class SelectiveApi(StubApi):
                    def post_event(self, payload):
                        self.posts.append(payload)
                        if payload["badge"] == "A":
                            return HttpResult(
                                False,
                                None,
                                None,
                                "Открытая смена ещё не появилась на сервере.",
                                True,
                            )
                        return HttpResult(True, 200, {"ok": True, "event": payload["event"], "name": "Б"}, None, False)

                service.api = SelectiveApi()
                moment = datetime.now().astimezone()
                db.insert_event("A", "А", "out", moment, {"badge": "A", "event": "out"})
                db.insert_event("B", "Б", "in", moment, {"badge": "B", "event": "in"})
                service._flush_pending()
                self.assertEqual(db.get_event(1)["status"], "pending")
                self.assertEqual(db.get_event(2)["status"], "sent")
                self.assertEqual([item["badge"] for item in service.api.posts], ["A", "B"])
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

    def test_manual_arrival_then_scan_closes_shift_while_server_is_unaware(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, speaker, api = self._service(folder, "http://server")
            try:
                api.employee = HttpResult(True, 200, {"name": "Эмир Абдуразаков", "active": True}, None, False)
                api.status = HttpResult(True, 200, {"last_event": "out", "name": "Эмир Абдуразаков"}, None, False)
                api.post = HttpResult(False, 401, {"message": "Для отметки нужен ключ доступа"}, None, False)
                service._process("adm001", manual=True)
                service._process("adm001", manual=False)
                pending = [row["event_type"] for row in db.pending_events()]
                self.assertEqual(pending, ["in", "out"])
                self.assertEqual(ui.views[-1]["title"], "УХОД")
                self.assertEqual(ui.views[-1]["state"], "offline")
                self.assertIn("Уход", speaker.said[-1])
                self.assertEqual(len(api.posts), 1)
                self.assertEqual(api.posts[0]["event"], "in")
            finally:
                db.close()

    def test_manual_then_scan_reaches_the_site_database(self) -> None:
        wall = '{"message":"Protected by Vercel Authentication","protection":{"vercel_auth_enabled":true}}'
        store = {"rows": []}

        def transport(request, timeout, context):
            del timeout, context
            url = request.full_url
            if url.startswith("https://example.test"):
                return 401, wall.encode("utf-8")
            if request.method == "GET" and "/rest/v1/users" in url:
                body = '[{"id": 2, "fullName": "Эмир Абдуразаков", "badgeId": "adm001", "role": "admin"}]'
                return 200, body.encode("utf-8")
            if request.method == "GET" and "/rest/v1/workSessions" in url:
                return 200, json.dumps(store["rows"]).encode("utf-8")
            if request.method == "POST" and "/rest/v1/workSessions" in url:
                body = json.loads(request.data.decode("utf-8"))
                store["rows"] = [
                    {
                        "id": 118,
                        "userId": body["userId"],
                        "date": body["date"],
                        "startTime": body["startTime"],
                        "endTime": None,
                        "status": "active",
                        "source": body["source"],
                    }
                ]
                return 201, json.dumps(store["rows"]).encode("utf-8")
            if request.method == "PATCH" and "id=eq.118" in url:
                body = json.loads(request.data.decode("utf-8"))
                store["rows"][0].update(body)
                return 200, json.dumps([store["rows"][0]]).encode("utf-8")
            return 500, b""

        with tempfile.TemporaryDirectory() as folder:
            settings = default_settings()
            settings["server"]["base_url"] = "https://example.test"
            settings["server"]["read_url"] = "https://example.test"
            settings["server"]["write_url"] = "https://example.test"
            settings["server"]["supabase_url"] = "https://db.test"
            settings["server"]["supabase_key"] = "anon-key"
            settings["server"]["login"] = ""
            settings["server"]["password"] = ""
            settings["scanner"]["cooldown_sec"] = 0
            settings["voice"]["enabled"] = False
            settings["voice"]["beep"] = False
            db = Database(Path(folder) / "t.db")
            ui = FakeUi()
            api = ApiClient(settings, transport=transport)
            service = AttendanceService(settings, db, api, FakeSpeaker(), ui)
            try:
                service._process("adm001", manual=True)
                service._process("adm001", manual=False)
                self.assertEqual(ui.views[-1]["title"], "УХОД")
                self.assertEqual(ui.views[-1]["state"], "out")
                self.assertEqual(db.count_pending(), 0)
                self.assertEqual(store["rows"][0]["status"], "completed")
                self.assertEqual(store["rows"][0]["source"], "qr")
                self.assertTrue(store["rows"][0]["endTime"])
            finally:
                db.close()

    def test_repeated_pending_arrivals_collapse_before_departure(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                api.status = HttpResult(True, 200, {"last_event": "out", "name": "Иван"}, None, False)
                api.post = HttpResult(False, 401, {"message": "ключ"}, None, False)
                moment = datetime.now().astimezone()
                for _ in range(3):
                    db.insert_event("emp001", "Иван", "in", moment, {"badge": "emp001", "event": "in"})
                service._process("emp001")
                self.assertEqual([row["event_type"] for row in db.pending_events()], ["in", "out"])
                self.assertEqual(ui.views[-1]["title"], "УХОД")
                self.assertEqual(api.posts, [])
            finally:
                db.close()

    def test_old_unsent_arrival_can_start_a_new_shift_and_then_close(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                api.status = HttpResult(True, 200, {"last_event": "out"}, None, False)
                api.post = HttpResult(False, 401, {"message": "ключ"}, None, False)
                old = datetime.now().astimezone() - timedelta(hours=20)
                db.insert_event("10001", "Иван", "in", old, {"badge": "10001", "event": "in"})
                service._process("10001")
                self.assertEqual(db.pending_events()[-1]["event_type"], "in")
                service._process("10001")
                self.assertEqual([row["event_type"] for row in db.pending_events()], ["in", "out"])
                self.assertEqual(ui.views[-1]["title"], "УХОД")
            finally:
                db.close()

    def test_closed_server_shift_starts_again_after_a_sent_arrival(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, _ui, _speaker, api = self._service(folder, "http://server")
            try:
                moment = datetime.now().astimezone()
                event_id = db.insert_event("10001", "Иван", "in", moment, {"badge": "10001", "event": "in"})
                db.mark_sent(event_id, "Иван", "in")
                api.status = HttpResult(True, 200, {"last_event": "out", "name": "Иван"}, None, False)
                api.post = HttpResult(True, 200, {"ok": True, "event": "in", "name": "Иван"}, None, False)
                service._process("10001")
                self.assertEqual(api.posts[0]["event"], "in")
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

    def test_screen_is_shown_before_the_server_answers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, speaker, api = self._service(folder, "http://server")
            try:
                api.employee = HttpResult(True, 200, {"name": "Иван", "active": True}, None, False)
                api.status = HttpResult(True, 200, {"last_event": "out", "name": "Иван"}, None, False)
                api.post = HttpResult(True, 200, {"ok": True, "event": "in", "name": "Иван"}, None, False)
                event_id = service._process("10001", background=True)
                self.assertEqual(ui.views[-1]["state"], "in")
                self.assertEqual(ui.views[-1]["title"], "ПРИХОД")
                self.assertEqual(ui.views[-1]["name"], "Иван")
                self.assertIn("Приход", speaker.said[-1])
                self.assertEqual(api.posts, [])
                self.assertEqual(db.count_pending(), 1)
                self.assertEqual(len(ui.views), 1)
                service._send_saved(event_id)
                self.assertEqual(db.count_pending(), 0)
                self.assertEqual(len(ui.views), 1)
                self.assertEqual(api.posts[0]["event"], "in")
            finally:
                db.close()

    def test_departure_follows_the_arrival_without_covering_the_screen(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            try:
                api.employee = HttpResult(True, 200, {"name": "Иван", "active": True}, None, False)
                api.status = HttpResult(True, 200, {"last_event": "out", "name": "Иван"}, None, False)
                api.post = HttpResult(True, 200, {"ok": True}, None, False)
                arrival = service._process("10001", background=True)
                service._process("10001", background=True)
                self.assertEqual([view["title"] for view in ui.views], ["ПРИХОД", "УХОД"])
                self.assertEqual(api.posts, [])
                self.assertEqual([row["event_type"] for row in db.pending_events()], ["in", "out"])
                departure = next(row["id"] for row in db.pending_events() if row["event_type"] == "out")
                service._send_saved(arrival)
                self.assertEqual([item["event"] for item in api.posts], ["in", "out"])
                self.assertEqual(db.count_pending(), 0)
                self.assertEqual(len(ui.views), 2)
                self.assertEqual(db.get_event(departure)["status"], "sent")
            finally:
                db.close()

    def test_next_person_is_not_waiting_on_the_server(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            service, db, ui, _speaker, api = self._service(folder, "http://server")
            release = threading.Event()

            def blocking(payload):
                api.posts.append(dict(payload))
                release.wait(3)
                return HttpResult(True, 200, {"ok": True, "event": payload["event"]}, None, False)

            api.post_event = blocking
            try:
                service.start()
                service.submit_scan("11111")
                service.submit_scan("22222")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and len(ui.views) < 2:
                    time.sleep(0.01)
                self.assertEqual([view["state"] for view in ui.views], ["in", "in"])
                self.assertEqual([view["title"] for view in ui.views], ["ПРИХОД", "ПРИХОД"])
            finally:
                release.set()
                service.stop()
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and db.count_pending():
                    time.sleep(0.01)
                db.close()
