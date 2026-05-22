
import json, os, threading
from config import APP_CONFIG_PATH

DEFAULTS = {
    "server.name": "Мой сервер",
    "server.pin": "",
    "server.general_channel": "General",
    "audio.bitrate": 64000,
    "audio.nr_mode": 1,
    "audio.vad_threshold": 5,
    "audio.input_device": "",
    "audio.output_device": "",
    "stream.default_resolution": "480p",
    "stream.default_fps": 30,
    "stream.audio_enabled": True,
    "ui.theme": "Темная",
    "ui.soundboard_volume": 40,
    "ui.minimize_to_tray": True,
    "ui.show_link_previews": True,
    "net.tcp_port": 5000,
    "net.udp_port": 5001,
    "net.sfu_port": 7788,
    "net.discovery_port": 5002,
    "update.auto_check": True,
    "update.github_repo": "",
}

class AppConfig:
    def __init__(self, path: str = APP_CONFIG_PATH):
        self._path = path
        self._data: dict = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        if os.path.isfile(self._path):
            try:
                with open(self._path, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
            except Exception as e:
                print(f"[Config] Read error: {e}"); self._data = {}
        changed = False
        for k, v in DEFAULTS.items():
            if k not in self._data:
                self._data[k] = v; changed = True
        if changed or not os.path.isfile(self._path):
            self.save()

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default if default is not None else DEFAULTS.get(key))

    def set(self, key: str, value):
        with self._lock: self._data[key] = value

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            try:
                with open(self._path, 'w', encoding='utf-8') as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except OSError as e:
                print(f"[Config] Write error: {e}")

    def get_all(self) -> dict:
        with self._lock: return dict(self._data)

cfg = AppConfig()
