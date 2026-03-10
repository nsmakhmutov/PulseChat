import sys
import os
import struct

# --- Утилиты ---
def resource_path(relative_path):
    """Получает абсолютный путь к ресурсам, работает для dev и для PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ── Сетевые настройки ────────────────────────────────────────────────────────
DEFAULT_PORT_TCP = 5000
DEFAULT_PORT_UDP = 5001

# BUFFER_SIZE: максимальный UDP-пакет (голос, ping, whisper).
# С переходом на WebRTC видео UDP нужен только для голоса комнаты.
BUFFER_SIZE = 65536

# Размер системных UDP-буферов на сервере.
# Голос комнаты: ~64 kbps на пользователя × 20 юзеров = ~160 KB/s.
# 2 MB хватит для поглощения любых кратковременных спайков.
UDP_RECV_BUFFER_SIZE = 2 * 1024 * 1024   # 2 MB
UDP_SEND_BUFFER_SIZE = 2 * 1024 * 1024   # 2 MB

# ── Аудио настройки (голос комнаты — без изменений) ──────────────────────────
SAMPLE_RATE     = 48000
CHANNELS        = 1
FRAME_DURATION  = 20                                  # мс
CHUNK_SIZE      = int(SAMPLE_RATE * (FRAME_DURATION / 1000))  # 960 сэмплов

# ── Видео настройки (захват DXCam → WebRTC) ──────────────────────────────────
VIDEO_WIDTH   = 1280
VIDEO_HEIGHT  = 720
VIDEO_FPS     = 30
# VIDEO_BITRATE: начальный целевой битрейт для WebRTC.
# aiortc + TWCC будут адаптировать его динамически.
VIDEO_BITRATE = 3_000_000   # 3 Mbps

# ── Opus (голос комнаты — без изменений) ─────────────────────────────────────
OPUS_APPLICATION = 2048   # opuslib.APPLICATION_VOIP
DEFAULT_BITRATE  = 64000

# ── UDP-заголовок (голос, ping, whisper) ─────────────────────────────────────
# Формат: UID (I) + Timestamp (d) + Sequence (I) + Flags (B)
UDP_HEADER_STRUCT = struct.Struct("!IdIB")
UDP_HEADER_SIZE   = UDP_HEADER_STRUCT.size

# ── UDP Flags (битмаска) ─────────────────────────────────────────────────────
# 1=Mute, 2=Deaf, 32=StreamVoices, 64=Whisper, 254=Ping
#
# Активные флаги после WebRTC-рефакторинга:
#   FLAG_LOOPBACK_AUDIO — dead-import в network_engine.py; сохранён чтобы
#                         не сломать его import-список до следующей чистки
#   FLAG_STREAM_VOICES  — Mix Minus голосов зрителей стрима (UDP-путь сохранён)
#   FLAG_WHISPER        — шёпот (приватный голос)
#
# Удалены (перешли на WebRTC или не нужны):
#   FLAG_STREAM_AUDIO (8)   — стрим-аудио → WebRTC (MicrophoneTrack / SystemAudioTrack)
#   FLAG_VIDEO (4), FLAG_VIDEO_LQ (128) — видео → WebRTC треки
#   LOOPBACK_UID_OFFSET     — stream_remote_users удалён из AudioHandler (Шаг 3)

FLAG_LOOPBACK_AUDIO = 16   # dead-import network_engine.py; сохранён для совместимости
FLAG_STREAM_VOICES  = 32   # голоса участников стрима (Mix Minus, UDP)
FLAG_WHISPER        = 64   # шёпот → только target_uid

# Struct для извлечения speaker_uid из voice-stream пакета
STREAM_VOICE_HEADER_STRUCT = struct.Struct("!I")   # 4 байта: speaker_uid
STREAM_VOICE_HEADER_SIZE   = STREAM_VOICE_HEADER_STRUCT.size

# ── WebRTC (НОВОЕ) ───────────────────────────────────────────────────────────
#
# Три TCP-команды для WebRTC signaling поверх существующего JSON-протокола.
# Транспорт — тот же TCP-сокет с JSON-фреймингом.
#
# Жизненный цикл стримера:
#   клиент → CMD_WEBRTC_OFFER (role="streamer") → сервер (WebRTCSFU)
#   сервер → CMD_WEBRTC_ANSWER → стример
#
# Жизненный цикл зрителя:
#   клиент → stream_watch_start → сервер
#   сервер → CMD_WEBRTC_OFFER (role="viewer") → зритель
#   зритель → CMD_WEBRTC_ANSWER → сервер
#   trickle ICE (опционально): CMD_WEBRTC_ICE в обе стороны
#
CMD_WEBRTC_OFFER  = 'webrtc_offer'    # стример/зритель ↔ сервер: SDP offer
CMD_WEBRTC_ANSWER = 'webrtc_answer'   # стример/зритель ↔ сервер: SDP answer
CMD_WEBRTC_ICE    = 'webrtc_ice'      # trickle ICE candidate (опционально)

# Таймаут ожидания завершения ICE gathering (секунды).
# В RadminVPN (host-only ICE, нет STUN) gathering завершается за ~50–200 мс.
# 3.0 с — большой запас на случай нагрузки ОС при первом коннекте.
WEBRTC_ICE_TIMEOUT = 3.0

# WEBRTC_ICE_SERVERS: список STUN/TURN серверов для ICE.
# Пусто — только host-кандидаты (достаточно для RadminVPN, все в одной VLAN).
# При необходимости работы через NAT добавить: [{"urls": "stun:stun.l.google.com:19302"}]
WEBRTC_ICE_SERVERS: list = []

# ── TCP-команды (без изменений) ───────────────────────────────────────────────
CMD_LOGIN         = 'login'
CMD_JOIN_ROOM     = 'join_room'
CMD_CHAT_MSG      = 'chat_msg'
CMD_SYNC_USERS    = 'sync_users'
CMD_SOUNDBOARD    = 'play_soundboard'
CMD_UPDATE_STATUS = 'update_status'
CMD_STREAM_START  = 'stream_start'
CMD_STREAM_STOP   = 'stream_stop'

# Смена пользовательского статуса (иконка + текст).
# status_icon: имя SVG-файла из assets/status/ ('' = нет статуса).
# status_text: произвольный текст ≤ 30 символов.
CMD_UPDATE_PRESENCE = 'update_presence'

# ── Фича «Пнуть» (Nudge) ─────────────────────────────────────────────────────
CMD_NUDGE_VOTE      = 'nudge_vote'      # клиент → сервер: голос за «пнуть»
CMD_PLAY_NUDGE      = 'play_nudge'      # сервер → цель: воспроизвести звук
CMD_NUDGE_TRIGGERED = 'nudge_triggered' # сервер → все в комнате: broadcast-тост

# Кулдаун: один voter голосует за одну цель не чаще чем раз в 10 минут.
NUDGE_COOLDOWN_SEC = 600

NUDGE_SOUND_PATH = resource_path(os.path.join("assets", "music", "Danger.mp3"))

# ── Файловая передача P2P (Direct TCP) ───────────────────────────────────────
# Файлы идут напрямую между клиентами. Сервер только relay-агент для сигнализации.
CMD_FILE_OFFER      = 'file_offer'       # личная: sender → server → target
CMD_FILE_OFFER_ROOM = 'file_offer_room'  # массовая: sender → server → все в комнате

FILE_CHUNK_SIZE       = 65536   # 64 KB — оптимум syscall vs задержка
FILE_TRANSFER_TIMEOUT = 30      # секунд — таймаут P2P подключения / recv

# ── Пути к файлам конфигурации пользователя (AppData) ────────────────────────
def get_appdata_dir() -> str:
    """Возвращает путь к папке InPulse в AppData пользователя, создаёт при необходимости."""
    appdata = os.environ.get('APPDATA')
    if appdata:
        path = os.path.join(appdata, "InPulse")
    else:
        path = os.path.join(os.path.expanduser("~"), ".InPulse")
    os.makedirs(path, exist_ok=True)
    return path


USER_CONFIG_PATH = os.path.join(get_appdata_dir(), "user_config.json")
KNOWN_USERS_PATH = os.path.join(get_appdata_dir(), "known_users.json")