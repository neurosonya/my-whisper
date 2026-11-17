#!/usr/bin/env python3
"""NeuroDictate - локальная диктовка голосом в любом окне.

Зажала Option - говоришь - отпустила - текст вставился.
Короткое нажатие - запись идет до следующего нажатия. Esc - отмена записи.
Распознавание полностью локальное: mlx-whisper на чипе Apple.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import rumps
import sounddevice as sd
from pynput import keyboard

BASE_DIR = Path(__file__).resolve().parent

if os.environ.get("NEURODICTATE_BUNDLED"):
    # Запуск из NeuroDictate.app: настройки и лог живут в стандартной папке
    # macOS, чтобы пересборка приложения их не трогала
    DATA_DIR = Path.home() / "Library" / "Application Support" / "NeuroDictate"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH = DATA_DIR / "config.json"
    LOG_PATH = DATA_DIR / "neurodictate.log"
    if not CONFIG_PATH.exists() and (BASE_DIR / "config.json").exists():
        shutil.copy(BASE_DIR / "config.json", CONFIG_PATH)
else:
    # Запуск из папки проекта (start.command)
    CONFIG_PATH = BASE_DIR / "config.json"
    LOG_PATH = BASE_DIR / "neurodictate.log"

SAMPLE_RATE = 16000

# Иконки состояний в меню-баре
ICON_LOADING = "⌛"
ICON_IDLE = "🎤"
ICON_RECORDING = "🔴"
ICON_BUSY = "⏳"

# Состояния
IDLE, RECORDING, BUSY = "idle", "recording", "busy"

# Клавиатура может прислать Option любым из этих кодов (у Сони правый
# Option приходит как общий Key.alt <58>), поэтому принимаем всю семью.
ALT_KEYS = {
    getattr(keyboard.Key, name)
    for name in ("alt", "alt_l", "alt_r", "alt_gr")
    if hasattr(keyboard.Key, name)
}
# Если зажат другой модификатор - это сочетание клавиш, а не диктовка
GUARD_MODIFIERS = {
    getattr(keyboard.Key, name)
    for name in (
        "cmd", "cmd_l", "cmd_r",
        "ctrl", "ctrl_l", "ctrl_r",
        "shift", "shift_l", "shift_r",
    )
    if hasattr(keyboard.Key, name)
}

# Whisper галлюцинирует на тишине - типичный мусор отсекаем
JUNK_PHRASES = {
    "субтитры сделал dimatorzok",
    "субтитры создавал dimatorzok",
    "продолжение следует...",
    "спасибо за просмотр!",
    "спасибо за просмотр",
    "редактор субтитров а.семкин корректор а.егорова",
    "thank you.",
    "thanks for watching!",
    "you",
}

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("neurodictate")


def load_config() -> dict:
    defaults = {
        "hotkey": "alt_r",
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "auto",
        "sounds": True,
        "append_space": True,
        "hold_threshold_sec": 0.5,
        "max_recording_sec": 300,
    }
    try:
        defaults.update(json.loads(CONFIG_PATH.read_text("utf-8")))
    except FileNotFoundError:
        pass
    return defaults


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")


def play_sound(name: str, enabled: bool) -> None:
    if enabled:
        subprocess.Popen(
            ["afplay", f"/System/Library/Sounds/{name}.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def get_clipboard() -> str:
    try:
        return subprocess.run(["pbpaste"], capture_output=True, timeout=3).stdout.decode(
            "utf-8", "replace"
        )
    except Exception:
        return ""


def set_clipboard(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=3)


class NeuroDictateApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_LOADING, quit_button=None)
        self.cfg = load_config()

        self.state = IDLE
        self.lock = threading.Lock()
        self.model_ready = False

        self.chunks: list[np.ndarray] = []
        self.frames_recorded = 0
        self.stream: sd.InputStream | None = None
        self.press_time = 0.0
        self.press_started_recording = False
        self.auto_stopped = False

        self.last_text = ""
        self.kb = keyboard.Controller()

        # --- меню ---
        self.item_status = rumps.MenuItem("Загружаю модель...")
        self.item_status.set_callback(None)
        self.item_copy = rumps.MenuItem(
            "Скопировать последний текст", callback=self.menu_copy_last
        )
        self.lang_items = {
            "auto": rumps.MenuItem("Язык: авто", callback=self.menu_lang),
            "ru": rumps.MenuItem("Язык: русский", callback=self.menu_lang),
            "en": rumps.MenuItem("Язык: английский", callback=self.menu_lang),
        }
        self.item_sounds = rumps.MenuItem("Звуки", callback=self.menu_sounds)
        self.item_sounds.state = 1 if self.cfg["sounds"] else 0
        self.item_polish = rumps.MenuItem(
            "Авторедактура (исправлять текст)", callback=self.menu_polish
        )
        self.item_polish.state = 1 if self.cfg.get("polish") else 0
        self.menu = [
            self.item_status,
            None,
            self.item_copy,
            None,
            *self.lang_items.values(),
            self.item_polish,
            self.item_sounds,
            None,
            rumps.MenuItem("Выход", callback=self.menu_quit),
        ]
        self._sync_lang_menu()

        # Глобальный слушатель клавиатуры (нужен «Мониторинг ввода»)
        hotkey_name = self.cfg["hotkey"]
        if hotkey_name.startswith("alt"):
            self.hotkeys = ALT_KEYS
        else:
            self.hotkeys = {getattr(keyboard.Key, hotkey_name, keyboard.Key.alt_r)}
        self.held_modifiers: set = set()
        self.hotkey_down = False
        self.listener = keyboard.Listener(
            on_press=self.on_press, on_release=self.on_release
        )
        self.listener.start()

        threading.Thread(target=self.warmup_model, daemon=True).start()
        log.info("Запуск. Хоткей: %s, модель: %s", hotkey_name, self.cfg["model"])

    # ---------- модель ----------

    def warmup_model(self):
        """Грузим веса заранее, чтобы первая диктовка не тормозила."""
        try:
            import mlx_whisper

            mlx_whisper.transcribe(
                np.zeros(SAMPLE_RATE, dtype=np.float32),
                path_or_hf_repo=self.cfg["model"],
            )
            self.model_ready = True
            self.title = ICON_IDLE
            self.item_status.title = "Готово: зажми Option и говори"
            play_sound("Glass", self.cfg["sounds"])
            log.info("Модель загружена")
            if self.cfg.get("polish"):
                self.polish("прогрев")  # заранее грузим редактора в память
        except Exception:
            log.exception("Не удалось загрузить модель")
            self.title = "⚠️"
            self.item_status.title = "Ошибка загрузки модели (см. neurodictate.log)"

    # ---------- запись ----------

    def audio_callback(self, indata, frames, time_info, status):
        self.chunks.append(indata.copy())
        self.frames_recorded += frames
        if (
            self.frames_recorded > self.cfg["max_recording_sec"] * SAMPLE_RATE
            and not self.auto_stopped
        ):
            self.auto_stopped = True
            threading.Thread(target=self.stop_and_transcribe, daemon=True).start()

    def start_recording(self):
        self.chunks = []
        self.frames_recorded = 0
        self.auto_stopped = False
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            callback=self.audio_callback,
        )
        self.stream.start()
        self.state = RECORDING
        self.title = ICON_RECORDING
        play_sound("Pop", self.cfg["sounds"])
        log.info("Запись началась")

    def _close_stream(self) -> np.ndarray:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        if not self.chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.chunks)[:, 0]

    def cancel_recording(self, silent=False):
        with self.lock:
            if self.state != RECORDING:
                return
            self._close_stream()
            self.state = IDLE
        self.title = ICON_IDLE
        if not silent:
            play_sound("Basso", self.cfg["sounds"])
        log.info("Запись отменена%s", " (шорткат с Option)" if silent else " (Esc)")

    def stop_and_transcribe(self):
        with self.lock:
            if self.state != RECORDING:
                return
            audio = self._close_stream()
            self.state = BUSY
        self.title = ICON_BUSY
        play_sound("Bottle", self.cfg["sounds"])
        try:
            self.transcribe_and_paste(audio)
        except Exception:
            log.exception("Ошибка распознавания")
        finally:
            with self.lock:
                self.state = IDLE
            self.title = ICON_IDLE

    # ---------- распознавание и вставка ----------

    def transcribe_and_paste(self, audio: np.ndarray):
        duration = len(audio) / SAMPLE_RATE
        if duration < 0.3:
            log.info("Слишком короткая запись (%.2f c) - пропускаю", duration)
            return
        if np.abs(audio).max() < 0.01:
            log.info("Тишина - пропускаю")
            return

        import mlx_whisper

        lang = None if self.cfg["language"] == "auto" else self.cfg["language"]
        t0 = time.time()
        result = mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.cfg["model"],
            language=lang,
            # Пример оформленного текста настраивает модель ставить
            # пунктуацию и заглавные буквы
            initial_prompt=self.cfg.get("initial_prompt") or None,
        )
        text = result["text"].strip()
        log.info(
            "Распознано за %.1f c (%.1f c аудио, язык %s): %r",
            time.time() - t0,
            duration,
            result.get("language"),
            text,
        )

        if not text or text.lower().strip(" .!") in {
            p.strip(" .!") for p in JUNK_PHRASES
        }:
            log.info("Пустой или мусорный результат - пропускаю")
            return

        if self.cfg.get("polish"):
            text = self.polish(text)

        if self.cfg.get("no_yo", True):
            # Автор пишет без буквы ё - приводим к ее стилю жестко, не надеясь на модель
            text = text.replace("ё", "е").replace("Ё", "Е")

        self.last_text = text
        self.paste_text(text + (" " if self.cfg["append_space"] else ""))
        play_sound("Glass", self.cfg["sounds"])

    def polish(self, text: str) -> str:
        """Авторедактура через локальную модель в Ollama.

        Любая ошибка (Ollama не запущена, таймаут) - возвращаем текст как есть,
        диктовка важнее редактуры.
        """
        try:
            t0 = time.time()
            req = urllib.request.Request(
                "http://localhost:11434/api/chat",
                json.dumps(
                    {
                        "model": self.cfg["polish_model"],
                        "messages": [
                            {"role": "system", "content": self.cfg["polish_prompt"]},
                            {"role": "user", "content": text},
                        ],
                        "stream": False,
                        "options": {"temperature": 0.2},
                        "keep_alive": "2h",
                    }
                ).encode(),
                headers={"Content-Type": "application/json"},
            )
            reply = json.loads(urllib.request.urlopen(req, timeout=60).read())
            polished = reply["message"]["content"].strip()
            if not polished:
                return text
            log.info("Редактура за %.1f c: %r", time.time() - t0, polished)
            return polished
        except Exception as e:
            log.warning("Редактура недоступна (%s) - вставляю как есть", e)
            return text

    def paste_text(self, text: str):
        """Вставка через буфер обмена + Cmd+V, старый буфер возвращаем на место."""
        old_clipboard = get_clipboard()
        set_clipboard(text)
        time.sleep(0.15)  # даем системе отпустить модификаторы хоткея
        # Жмем физическую клавишу V по коду (kVK_ANSI_V = 9), а не символ 'v' -
        # иначе на русской раскладке Cmd+V не срабатывает
        v_key = keyboard.KeyCode.from_vk(9)
        with self.kb.pressed(keyboard.Key.cmd):
            self.kb.press(v_key)
            self.kb.release(v_key)
        time.sleep(0.6)  # приложение должно успеть прочитать буфер до отката
        if old_clipboard:
            set_clipboard(old_clipboard)

    # ---------- хоткей ----------

    def on_press(self, key):
        # Отладка: пишем в лог только служебные клавиши (не буквы), чтобы
        # понять, каким кодом приходит хоткей. Включается в config.json.
        if self.cfg.get("debug") and not isinstance(key, keyboard.KeyCode):
            log.info("DEBUG нажата клавиша: %r (state=%s)", key, self.state)
        if key == keyboard.Key.esc:
            if self.state == RECORDING:
                self.cancel_recording()
            return
        if key in GUARD_MODIFIERS:
            self.held_modifiers.add(key)
        if key not in self.hotkeys:
            # Другая клавиша, пока Option еще зажат = это сочетание клавиш,
            # а не диктовка - тихо отменяем случайно начатую запись
            if (
                self.hotkey_down
                and self.state == RECORDING
                and self.press_started_recording
            ):
                self.cancel_recording(silent=True)
            return
        self.hotkey_down = True
        with self.lock:
            if self.state == BUSY or not self.model_ready:
                return
            if self.state == IDLE:
                if self.held_modifiers:
                    return  # зажат Cmd/Ctrl/Shift - это шорткат, не диктовка
                self.press_time = time.time()
                self.press_started_recording = True
                self.start_recording()
                return
            # state == RECORDING: второе нажатие в режиме переключателя
            self.press_started_recording = False
        threading.Thread(target=self.stop_and_transcribe, daemon=True).start()

    def on_release(self, key):
        if key in GUARD_MODIFIERS:
            self.held_modifiers.discard(key)
        if key not in self.hotkeys:
            return
        self.hotkey_down = False
        if self.state != RECORDING or not self.press_started_recording:
            return
        held = time.time() - self.press_time
        if held >= self.cfg["hold_threshold_sec"]:
            # режим рации: отпустила - распознаем
            threading.Thread(target=self.stop_and_transcribe, daemon=True).start()
        else:
            # короткий тап: переключатель, запись продолжается
            log.info("Режим переключателя: запись до следующего нажатия")

    # ---------- меню ----------

    def _sync_lang_menu(self):
        for code, item in self.lang_items.items():
            item.state = 1 if self.cfg["language"] == code else 0

    def menu_lang(self, sender):
        for code, item in self.lang_items.items():
            if item is sender:
                self.cfg["language"] = code
        self._sync_lang_menu()
        save_config(self.cfg)

    def menu_polish(self, sender):
        self.cfg["polish"] = not self.cfg.get("polish")
        sender.state = 1 if self.cfg["polish"] else 0
        save_config(self.cfg)

    def menu_sounds(self, sender):
        self.cfg["sounds"] = not self.cfg["sounds"]
        sender.state = 1 if self.cfg["sounds"] else 0
        save_config(self.cfg)

    def menu_copy_last(self, _):
        if self.last_text:
            set_clipboard(self.last_text)

    def menu_quit(self, _):
        self.listener.stop()
        rumps.quit_application()


if __name__ == "__main__":
    NeuroDictateApp().run()
