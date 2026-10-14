from __future__ import annotations

import queue
import threading

from scanner_app.logging_setup import logger


# SAPI: говорить сразу и оборвать предыдущую фразу, чтобы очередь людей не копилась в голосе.
_SPEAK_NOW = 1 | 2


class Speaker:
    """Озвучка через голос Windows (SAPI). Работает без интернета."""

    def __init__(self) -> None:
        self.voices: list[dict[str, str]] = []
        self.error = ""
        self._queue: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._voice = None
        self._applied: tuple | None = None
        self._russian_id = ""
        self._thread = threading.Thread(target=self._run, name="speech", daemon=True)
        self._thread.start()

    def say(self, text: str, options: dict | None = None) -> None:
        if self.error:
            return
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            return
        self._drop_waiting()
        self._queue.put((cleaned[:400], dict(options or {})))

    def _drop_waiting(self) -> None:
        """Новая отметка заменяет ещё не сказанные. Команду остановки не теряем."""
        stop = False
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is None:
                stop = True
        if stop:
            self._queue.put(None)

    def list_voices(self, timeout: float = 8) -> list[dict[str, str]]:
        self._ready.wait(timeout)
        return list(self.voices)

    def stop(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        try:
            import comtypes
            import comtypes.client

            comtypes.CoInitialize()
            self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
            self.voices = _list_voices(self._voice)
            self._russian_id = _russian_voice_id(self.voices)
            logger().info("Голоса Windows: %s", ", ".join(item["name"] for item in self.voices) or "нет")
        except Exception as exc:
            self.error = str(exc)
            logger().error("Голос недоступен: %s", exc)
            self._ready.set()
            return
        self._ready.set()
        while True:
            item = self._queue.get()
            if item is None:
                break
            text, options = item
            try:
                self._apply(options)
                try:
                    self._voice.Speak(text, _SPEAK_NOW)
                except Exception:
                    self._voice.Speak(text)
            except Exception:
                logger().exception("Не удалось произнести фразу")

    def _apply(self, options: dict) -> None:
        rate = max(-10, min(10, int(options.get("rate") or 0)))
        volume = max(0, min(100, int(options.get("volume") or 100)))
        voice_id = str(options.get("voice_id") or "")
        if not voice_id and options.get("prefer_russian", True):
            voice_id = self._russian_id
        signature = (rate, volume, voice_id)
        if signature == self._applied:
            return
        self._voice.Rate = rate
        self._voice.Volume = volume
        if voice_id:
            tokens = self._voice.GetVoices()
            for index in range(int(tokens.Count)):
                token = tokens.Item(index)
                if str(token.Id) == voice_id:
                    self._voice.Voice = token
                    break
        self._applied = signature


def _russian_voice_id(voices: list[dict[str, str]]) -> str:
    for voice in voices:
        name = voice["name"].lower()
        if "russian" in name or "irina" in name or "pavel" in name or "ирин" in name or "павел" in name:
            return voice["id"]
    return ""


def _list_voices(voice) -> list[dict[str, str]]:
    found = []
    try:
        tokens = voice.GetVoices()
        count = int(tokens.Count)
    except Exception:
        logger().exception("Не удалось получить список голосов")
        return found
    for index in range(count):
        try:
            token = tokens.Item(index)
            found.append({"id": str(token.Id), "name": str(token.GetDescription())})
        except Exception:
            continue
    return found
