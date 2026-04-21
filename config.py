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

# Jitter buffer для зрителя (мс).
VIEWER_JITTER_BUFFER_MS = 700

VIDEO_BITRATE = 4_500_000   # 4.5 Mbps (720p default, maxrate для CQ режима)

# ── HQ битрейты по разрешению ─────────────────────────────────────────────────
VIDEO_BITRATES: dict = {
    (3840, 2160): 20_000_000,
    (2560, 1440): 12_000_000,
    (1920, 1080): 10_000_000,
    (1280,  720):  5_500_000,
    ( 854,  480):  3_000_000,
    ( 640,  360):  1_500_000,
}

VIDEO_BITRATES_LQ: dict = {
    (3840, 2160):  4_000_000,
    (2560, 1440):  3_000_000,
    (1920, 1080):  2_000_000,
    (1280,  720):  1_000_000,
    ( 854,  480):    400_000,
    ( 640,  360):    250_000,
}
VIDEO_BITRATE_LQ_DEFAULT = 1_000_000


def get_lq_resolution(hq_width: int, hq_height: int) -> tuple:
    """Вычисляет LQ-разрешение как HQ/2, выровненное до чётного числа."""
    lq_w = max(320, (hq_width  // 2) & ~1)
    lq_h = max(180, (hq_height // 2) & ~1)
    return lq_w, lq_h


def get_bitrate_for_resolution(width: int, height: int, lq: bool = False) -> int:
    """Возвращает битрейт для заданного разрешения."""
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


# ── Opus (голос комнаты) ─────────────────────────────────────────────────────
OPUS_APPLICATION = 2048
DEFAULT_BITRATE        = 64000
STREAM_AUDIO_BITRATE   = 128000

# ── UDP-заголовок ─────────────────────────────────────────────────────────────
UDP_HEADER_STRUCT = struct.Struct("!IdIB")
UDP_HEADER_SIZE   = UDP_HEADER_STRUCT.size

# ── UDP Flags ─────────────────────────────────────────────────────────────────
# Биты 1 (mute) и 2 (deaf) резервируются под базовое состояние участника в
# обычных аудио-пакетах. 16/32/64/128 — режимные флаги пакета.
FLAG_LOOPBACK_AUDIO = 16
FLAG_STREAM_VOICES  = 32
FLAG_WHISPER        = 64
# Анонимный шёпот: устанавливается отправителем совместно с FLAG_WHISPER.
# Сервер при ретрансляции переписывает sender_uid в UDP-заголовке на
# ANONYMOUS_UID — получатель физически не видит реальный uid отправителя.
FLAG_ANONYMOUS      = 128

STREAM_VOICE_HEADER_STRUCT = struct.Struct("!I")
STREAM_VOICE_HEADER_SIZE   = STREAM_VOICE_HEADER_STRUCT.size

# ── Anonymous whisper ─────────────────────────────────────────────────────────
# Зарезервированный «псевдо-uid» для анонимного шёпота. Реальные uid генерируются
# как secrets.randbelow(10**9)+1 = 1..1_000_000_000, поэтому 0xFFFFFFFE
# (4_294_967_294) гарантированно не пересекается с ними.
# Используется:
#   * сервером — подставляется в UDP-заголовок вместо реального sender_uid при
#     ретрансляции пакета с FLAG_ANONYMOUS;
#   * получателем — маркер в UI для отображения «Аноним» вместо ника;
#   * audio_engine — ключ отдельного RemoteUser/JitterBuffer для анонимов.
ANONYMOUS_UID = 0xFFFFFFFE


def is_anonymous_uid(uid: int) -> bool:
    """True если переданный uid — зарезервированный маркер анонимного шёпота."""
    return uid == ANONYMOUS_UID

# ── WebRTC ───────────────────────────────────────────────────────────────────
CMD_WEBRTC_OFFER  = 'webrtc_offer'
CMD_WEBRTC_ANSWER = 'webrtc_answer'
CMD_WEBRTC_ICE    = 'webrtc_ice'

WEBRTC_ICE_TIMEOUT  = 3.0
WEBRTC_ICE_SERVERS: list = []

# ── SFU (Go sidecar) ─────────────────────────────────────────────────────────
SFU_PORT       = 7788
SFU_EXE_NAME   = "sidecar.exe"
SFU_PORT_RANGE = 20

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
TYPING_THROTTLE_MS = 3000
TYPING_EXPIRE_SEC  = 5.0

# ── SQLite чат (хост хранит историю на диске) ────────────────────────────────
CHAT_DB_PATH = os.path.join(get_appdata_dir(), "chat_history.db")

# ── Единый конфиг приложения ─────────────────────────────────────────────────
APP_CONFIG_PATH = os.path.join(get_appdata_dir(), "inpulse_config.json")

# ── Аннотации стрима ────────────────────────────────────────────────────────
CMD_DRAW_STROKE = 'draw_stroke'
DRAW_MAX_POINTS = 300
DRAW_FADE_SEC   = 5.0

CMD_CREATE_CHANNEL    = 'create_channel'
CMD_CHANNEL_CREATED   = 'channel_created'
CMD_CHANNEL_DELETED   = 'channel_deleted'
CMD_JOIN_CHANNEL_AUTH = 'join_channel_auth'
CMD_CHANNEL_LIST      = 'channel_list'
CHANNEL_NAME_MAX_LEN  = 32
CHANNEL_PASS_MAX_LEN  = 64

SERVER_NAME_MAX_LEN = 40
SERVER_NAME_DEFAULT = 'InPulse Server'

# ═══════════════════════════════════════════════════════════════════════════
# Миграция сервера — таймауты и команды
# ═══════════════════════════════════════════════════════════════════════════
MIGRATION_ANNOUNCE_WAIT_SEC = 4.0
MIGRATION_TCP_TIMEOUT_SEC   = 1.0
MIGRATION_TCP_RETRY_SEC     = 0.3
MIGRATION_TOTAL_DEADLINE    = 10.0

# ── Обрыв связи с хостом (без CMD_SERVER_MIGRATE) ────────────────────────────
RECONNECT_SAME_HOST_WINDOW_SEC = 8.0
DISCOVERY_WINDOW_SEC           = 6.0
BECOME_HOST_POS0_DELAY_SEC     = 0.5
BECOME_HOST_POS_N_DELAY_SEC    = 1.2

RECONNECT_SAME_HOST_TCP_ATTEMPTS = 20

# ── Обратная совместимость со старым API ────────────────────────────────────
MAX_SILENT_RECONNECT_ATTEMPTS = 2
RECONNECT_DELAY               = 1.0

# ── Команды 2-шаговой ручной передачи сервера ───────────────────────────────
CMD_MIGRATE_PREPARE        = 'migrate_prepare'
CMD_MIGRATE_READY          = 'migrate_ready'
MIGRATE_PREPARE_TIMEOUT_SEC = 7.0

# ══════════════════════════════════════════════════════════════════════════════
# Диагностические флаги (FIX: раньше RMS/peak считались всегда, даже если
# не нужны для лога. Это O(n) на каждый audio-фрейм в hot path.)
# ══════════════════════════════════════════════════════════════════════════════
AUDIO_DIAG_ENABLED = False   # True → логи [VIEWER-DIAG], [OUT-DIAG], [DLL-DIAG]
