import sys
import os
import struct


def resource_path(relative_path: str) -> str:
    """Возвращает абсолютный путь к ресурсу (dev и PyInstaller).

        :param relative_path: относительный путь к ресурсу
        :return: абсолютный путь
    """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# ── Сетевые настройки ─────────────────────────────────────────────────────────

DEFAULT_PORT_TCP = 5000
DEFAULT_PORT_UDP = 5001

# Максимальный UDP-пакет: UDP_HEADER(13) + VIDEO_HEADER(8) + MAX_VIDEO_PAYLOAD(1400)
BUFFER_SIZE = 65536

# 8 МБ — запас для кратковременных спайков трафика без дропов входящих пакетов
UDP_RECV_BUFFER_SIZE = 8 * 1024 * 1024

# 8 МБ — отдельный буфер отправки снижает конкуренцию между recv/send очередями ядра
UDP_SEND_BUFFER_SIZE = 8 * 1024 * 1024


# ── Аудио настройки ───────────────────────────────────────────────────────────

SAMPLE_RATE     = 48000
CHANNELS        = 1
FRAME_DURATION  = 20
CHUNK_SIZE      = int(SAMPLE_RATE * (FRAME_DURATION / 1000))


# ── Видео настройки ───────────────────────────────────────────────────────────

VIDEO_WIDTH   = 1280
VIDEO_HEIGHT  = 720
VIDEO_FPS     = 60
VIDEO_BITRATE = 6_000_000  # 6 Mbps

# Скорость pacing: VIDEO_BITRATE × 1.25 / 8 — покрывает UDP/IP overhead без бёрстов
VIDEO_PACING_RATE_BYTES_SEC = int(VIDEO_BITRATE * 1.25 / 8)  # ~937 500 байт/сек


# ── Флаги UDP-заголовка (битмаска) ────────────────────────────────────────────
# 1=Mute, 2=Deaf, 4=Video, 8=StreamAudio, 16=LoopbackAudio, 32=StreamVoices, 64=Whisper, 254=Ping

FLAG_VIDEO          = 4
FLAG_STREAM_AUDIO   = 8   # аудио стрима → только зрителям
FLAG_LOOPBACK_AUDIO = 16  # подтип: системный звук (WASAPI Loopback), бит поверх FLAG_STREAM_AUDIO

# Приватная передача голоса одному пользователю.
# Payload: [target_uid: 4 байта big-endian] + [opus].
FLAG_WHISPER = 64

# Голосовой микс стримера для зрителей.
# Payload: [speaker_uid: 4 байта big-endian] + [opus].
# Зритель отбрасывает пакет если speaker_uid == my_uid (Mix Minus без DSP).
FLAG_STREAM_VOICES = 32  # подтип поверх FLAG_STREAM_AUDIO

# Struct для извлечения speaker_uid / target_uid из voice-stream и whisper пакетов
STREAM_VOICE_HEADER_STRUCT = struct.Struct("!I")   # 4 байта
STREAM_VOICE_HEADER_SIZE   = STREAM_VOICE_HEADER_STRUCT.size

# Смещение UID для хранения loopback-потоков в stream_remote_users.
# Loopback uid=X хранится под ключом X + LOOPBACK_UID_OFFSET — не смешивается с микрофоном.
LOOPBACK_UID_OFFSET = 1_000_000

MAX_VIDEO_PAYLOAD  = 1300
VIDEO_CHUNK_HEADER = struct.Struct("!IHH")
VIDEO_HEADER_SIZE  = VIDEO_CHUNK_HEADER.size
VIDEO_CHUNK_STRUCT = VIDEO_CHUNK_HEADER   # псевдоним, идентичный формат


# ── Opus настройки ────────────────────────────────────────────────────────────

OPUS_APPLICATION = 2048   # opuslib.APPLICATION_VOIP
DEFAULT_BITRATE  = 64_000


# ── Структура UDP-заголовка: UID(I) + Timestamp(d) + Sequence(I) + Flags(B) ──

UDP_HEADER_STRUCT = struct.Struct("!IdIB")
UDP_HEADER_SIZE   = UDP_HEADER_STRUCT.size

# IDR-таймер в ui_video.py использует эту константу для периодических запросов
VIDEO_LOW_QUALITY_IDR_INTERVAL_MS = 2000


# ── Команды TCP ───────────────────────────────────────────────────────────────

CMD_LOGIN         = 'login'
CMD_JOIN_ROOM     = 'join_room'
CMD_CHAT_MSG      = 'chat_msg'
CMD_SYNC_USERS    = 'sync_users'
CMD_SOUNDBOARD    = 'play_soundboard'
CMD_UPDATE_STATUS = 'update_status'
CMD_STREAM_START  = 'stream_start'
CMD_STREAM_STOP   = 'stream_stop'

# Смена пользовательского статуса (иконка + текст).
# status_icon: имя SVG-файла из assets/status/ или '' — нет статуса.
# status_text: произвольный текст ≤ 30 символов или ''.
CMD_UPDATE_PRESENCE = 'update_presence'

# Фича «Пнуть» (Nudge):
#   CMD_NUDGE_VOTE      — клиент → сервер: голос за «пнуть» target_uid
#   CMD_PLAY_NUDGE      — сервер → цель: воспроизвести Danger.mp3 + писк
#   CMD_NUDGE_TRIGGERED — сервер → все в комнате: broadcast-тост
CMD_NUDGE_VOTE      = 'nudge_vote'
CMD_PLAY_NUDGE      = 'play_nudge'
CMD_NUDGE_TRIGGERED = 'nudge_triggered'

# Один voter может проголосовать за одну цель раз в 10 минут
NUDGE_COOLDOWN_SEC = 600

NUDGE_SOUND_PATH = resource_path(os.path.join("assets", "music", "Danger.mp3"))