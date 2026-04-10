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

BUFFER_SIZE = 65536

UDP_RECV_BUFFER_SIZE = 2 * 1024 * 1024   # 2 MB
UDP_SEND_BUFFER_SIZE = 2 * 1024 * 1024   # 2 MB

# ── Аудио настройки (голос комнаты — без изменений) ──────────────────────────
SAMPLE_RATE     = 48000
CHANNELS        = 1
FRAME_DURATION  = 20
CHUNK_SIZE      = int(SAMPLE_RATE * (FRAME_DURATION / 1000))  # 960 сэмплов

# ── Видео настройки (захват DXCam → WebRTC) ──────────────────────────────────
VIDEO_WIDTH   = 1280
VIDEO_HEIGHT  = 720
VIDEO_FPS     = 30

# Jitter buffer для зрителя (мс). Сглаживает неравномерность доставки пакетов.
# 0 = отключён (старое поведение), 500-700 = оптимально для просмотра стримов.
VIEWER_JITTER_BUFFER_MS = 600

VIDEO_BITRATE = 4_500_000   # 4.5 Mbps (720p default, maxrate для CQ режима)

# ── HQ битрейты по разрешению ─────────────────────────────────────────────────
#
# Расширена таблица — добавлены 1080p / 1440p / 4K для режима «Источник».
# С CQ crf=20 реальный битрейт для статичного UI в 3–5× ниже maxrate.
# Значение в таблице — потолок (maxrate), не цель.
VIDEO_BITRATES: dict = {
    (3840, 2160): 20_000_000,   # 4K     — 20 Mbps
    (2560, 1440): 12_000_000,   # 1440p  — 12 Mbps
    (1920, 1080): 10_000_000,   # 1080p  — 10 Mbps
    (1280,  720):  4_500_000,   # 720p   —  4.5 Mbps
    ( 854,  480):  2_000_000,   # 480p   —  2 Mbps (CQ экономит на статике)
    ( 640,  360):  1_000_000,   # 360p   —  1 Mbps (CQ экономит на статике)
}

# ── LQ (Simulcast) битрейты ──────────────────────────────────────────────────
#
# LQ-трек — второй WebRTC видеотрек на половинном разрешении.
# Стример отправляет оба трека (HQ + LQ) в один RTCPeerConnection.
# SFU маршрутизирует каждому зрителю нужный поток по полю quality=hq|lq
# в команде stream_watch_start.
#
# Аудиотрек общий для обоих потоков — дублировать не нужно.
#
# Примеры разрешений LQ (= HQ / 2, выровнено до чётного):
#   720p  (1280×720)  → LQ 360p  (640×360)
#   1080p (1920×1080) → LQ 540p  (960×540)
VIDEO_BITRATES_LQ: dict = {
    (3840, 2160):  4_000_000,   # 4K     → LQ ~1920×1080, 4 Mbps
    (2560, 1440):  3_000_000,   # 1440p  → LQ ~1280×720,  3 Mbps
    (1920, 1080):  2_000_000,   # 1080p  → LQ ~960×540,   2 Mbps
    (1280,  720):  1_000_000,   # 720p   → LQ ~640×360,   1 Mbps
    ( 854,  480):    400_000,   # 480p   → LQ ~426×240, 400 kbps
    ( 640,  360):    250_000,   # 360p   → LQ ~320×180, 250 kbps
}
VIDEO_BITRATE_LQ_DEFAULT = 1_000_000  # fallback если разрешение не в таблице


def get_lq_resolution(hq_width: int, hq_height: int) -> tuple:
    """
    Вычисляет LQ-разрешение как HQ/2, выровненное до чётного числа.
    Минимум 320×180 — кодек не принимает меньше.
    """
    lq_w = max(320, (hq_width  // 2) & ~1)
    lq_h = max(180, (hq_height // 2) & ~1)
    return lq_w, lq_h


def get_bitrate_for_resolution(width: int, height: int, lq: bool = False) -> int:
    """
    Возвращает битрейт для заданного разрешения.
    Если разрешение не в таблице — вычисляет пропорционально 720p
    по сублинейной шкале (^0.75): большее разрешение не требует
    линейного роста битрейта, т.к. большие блоки кодируются эффективнее.
    """
    table = VIDEO_BITRATES_LQ if lq else VIDEO_BITRATES
    if (width, height) in table:
        return table[(width, height)]

    ref_w, ref_h = 1280, 720
    ref_br = 1_000_000 if lq else 4_500_000
    pixels = width * height
    ref_pixels = ref_w * ref_h
    estimated = int(ref_br * (pixels / ref_pixels) ** 0.75)

    if lq:
        return max(400_000, min(estimated, 4_000_000))
    return max(1_500_000, min(estimated, 20_000_000))


# ── Opus (голос комнаты — без изменений) ─────────────────────────────────────
OPUS_APPLICATION = 2048   # opuslib.APPLICATION_VOIP
DEFAULT_BITRATE        = 64000   # битрейт голоса (Opus, моно, 64 kbps)
STREAM_AUDIO_BITRATE   = 128000  # битрейт звука при демонстрации (стерео, 128 kbps — лучше качество)

# ── UDP-заголовок ─────────────────────────────────────────────────────────────
UDP_HEADER_STRUCT = struct.Struct("!IdIB")
UDP_HEADER_SIZE   = UDP_HEADER_STRUCT.size

# ── UDP Flags ─────────────────────────────────────────────────────────────────
FLAG_LOOPBACK_AUDIO = 16
FLAG_STREAM_VOICES  = 32
FLAG_WHISPER        = 64

STREAM_VOICE_HEADER_STRUCT = struct.Struct("!I")
STREAM_VOICE_HEADER_SIZE   = STREAM_VOICE_HEADER_STRUCT.size

# ── WebRTC ───────────────────────────────────────────────────────────────────
CMD_WEBRTC_OFFER  = 'webrtc_offer'
CMD_WEBRTC_ANSWER = 'webrtc_answer'
CMD_WEBRTC_ICE    = 'webrtc_ice'

WEBRTC_ICE_TIMEOUT  = 3.0
WEBRTC_ICE_SERVERS: list = []

# ── TCP-команды ───────────────────────────────────────────────────────────────
CMD_LOGIN           = 'login'
CMD_JOIN_ROOM       = 'join_room'
CMD_CHAT_MSG        = 'chat_msg'
CMD_SYNC_USERS      = 'sync_users'
CMD_SOUNDBOARD      = 'play_soundboard'
CMD_UPDATE_STATUS   = 'update_status'
CMD_STREAM_START    = 'stream_start'
CMD_STREAM_STOP     = 'stream_stop'
CMD_UPDATE_PRESENCE = 'update_presence'

CMD_NUDGE_VOTE      = 'nudge_vote'
CMD_PLAY_NUDGE      = 'play_nudge'
CMD_NUDGE_TRIGGERED = 'nudge_triggered'
NUDGE_COOLDOWN_SEC  = 600
NUDGE_SOUND_PATH    = resource_path(os.path.join("assets", "music", "Danger.mp3"))

CMD_FILE_OFFER        = 'file_offer'
CMD_FILE_OFFER_ROOM   = 'file_offer_room'
FILE_CHUNK_SIZE       = 65536
FILE_TRANSFER_TIMEOUT = 30

def get_appdata_dir() -> str:
    appdata = os.environ.get('APPDATA')
    if appdata:
        path = os.path.join(appdata, "InPulse")
    else:
        path = os.path.join(os.path.expanduser("~"), ".InPulse")
    os.makedirs(path, exist_ok=True)
    return path

USER_CONFIG_PATH = os.path.join(get_appdata_dir(), "user_config.json")
KNOWN_USERS_PATH = os.path.join(get_appdata_dir(), "known_users.json")

DISCOVERY_PORT     = 5002
DISCOVERY_INTERVAL = 0.5
DISCOVERY_TIMEOUT  = 1.5

CMD_SERVER_TRANSFER = 'server_transfer'
CMD_SERVER_MIGRATE  = 'server_migrate'

CMD_QUICK_MSG     = 'quick_msg'
QUICK_MSG_MAX_LEN = 20

CMD_CHAT_HISTORY     = 'chat_history'
CMD_CHAT_HISTORY_REQ = 'chat_history_req'
CMD_CHAT_MEDIA       = 'chat_media'
CHAT_MSG_MAX_LEN     = 500
CHAT_HISTORY_MAX     = 500
CHAT_MEDIA_MAX_B64   = 10_000_000

CMD_HOST_MUTE   = 'host_mute'
CMD_FORCE_MUTED = 'force_muted'

# ── Typing indicator ──────────────────────────────────────────────────────────
CMD_TYPING         = 'typing'
TYPING_THROTTLE_MS = 3000    # клиент шлёт не чаще одного раза в 3 секунды
TYPING_EXPIRE_SEC  = 5.0     # "печатает" гаснет через 5 секунд без обновления

# ── SQLite чат (хост хранит историю на диске) ────────────────────────────────
CHAT_DB_PATH = os.path.join(get_appdata_dir(), "chat_history.db")

# ── Единый конфиг приложения ─────────────────────────────────────────────────
APP_CONFIG_PATH = os.path.join(get_appdata_dir(), "inpulse_config.json")

# ── Аннотации стрима: рисование зрителем поверх стрима ──────────────────────
# Зритель рисует → клиент отправляет draw_stroke серверу.
# Сервер ретранслирует всем зрителям + стримеру.
# points: нормализованные координаты [[x,y], ...] 0.0–1.0 относительно кадра.
CMD_DRAW_STROKE = 'draw_stroke'
DRAW_MAX_POINTS = 300       # макс. точек в одном мазке
DRAW_FADE_SEC   = 5.0       # мазок живёт 5 секунд, последние 1 с плавно гасится

CMD_CREATE_CHANNEL    = 'create_channel'
CMD_CHANNEL_CREATED   = 'channel_created'
CMD_CHANNEL_DELETED   = 'channel_deleted'
CMD_JOIN_CHANNEL_AUTH = 'join_channel_auth'
CMD_CHANNEL_LIST      = 'channel_list'
CHANNEL_NAME_MAX_LEN  = 32
CHANNEL_PASS_MAX_LEN  = 64

SERVER_NAME_MAX_LEN = 40
SERVER_NAME_DEFAULT = 'InPulse Server'