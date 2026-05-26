import sys
import os
import struct
def resource_path(relative_path):
    """Получает абсолютный путь к ресурсам, работает для dev и для PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# ── Сетевые настройки
DEFAULT_PORT_TCP = 5000
DEFAULT_PORT_UDP = 5001

BUFFER_SIZE = 65536

UDP_RECV_BUFFER_SIZE = 2 * 1024 * 1024
UDP_SEND_BUFFER_SIZE = 2 * 1024 * 1024

# ── Аудио настройки
SAMPLE_RATE     = 48000
CHANNELS        = 1
FRAME_DURATION  = 20
CHUNK_SIZE      = int(SAMPLE_RATE * (FRAME_DURATION / 1000))

# ── Видео настройки (захват DXCam → WebRTC)
VIDEO_WIDTH   = 1280
VIDEO_HEIGHT  = 720
VIDEO_FPS     = 30

# Jitter buffer для зрителя (мс).
#
#   VIEWER_JITTER_BUFFER_MS         — обычный просмотр. Большой буфер сглаживает
#       рывки на слабом/нестабильном интернете (jitter, потери, ABR-переключения).
#       Цена — фиксированная задержка показа ~0.7 с, для пассивного просмотра
#       футбола/клипов незаметная и желательная.
#
#   VIEWER_JITTER_BUFFER_MS_CONTROL — действует ТОЛЬКО пока зритель активно
#       управляет рабочим столом стримера. Здесь важна не плавность, а отклик:
#       клик → видимый результат. Маленький буфер убирает основную часть
#       задержки. Как только управление завершается, буфер возвращается к
#       VIEWER_JITTER_BUFFER_MS, и сглаживание для слабого интернета снова в силе.
#
#   Переключение делается на лету и только для того VideoReceiver, чей стрим
#       сейчас под управлением, — см. VideoReceiver.set_low_latency().
VIEWER_JITTER_BUFFER_MS         = 700
VIEWER_JITTER_BUFFER_MS_CONTROL = 90
VIDEO_BITRATE = 4_500_000

# ── HQ битрейты по разрешению
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


# ── Opus (голос комнаты)
OPUS_APPLICATION = 2048
DEFAULT_BITRATE        = 64000
STREAM_AUDIO_BITRATE   = 128000

# ── UDP-заголовок
UDP_HEADER_STRUCT = struct.Struct("!IdIB")
UDP_HEADER_SIZE   = UDP_HEADER_STRUCT.size

# ── UDP Flags

FLAG_LOOPBACK_AUDIO = 16
FLAG_STREAM_VOICES  = 32
FLAG_WHISPER        = 64
FLAG_ANONYMOUS      = 128

STREAM_VOICE_HEADER_STRUCT = struct.Struct("!I")
STREAM_VOICE_HEADER_SIZE   = STREAM_VOICE_HEADER_STRUCT.size

# ── Anonymous whisper
ANONYMOUS_UID = 0xFFFFFFFE


def is_anonymous_uid(uid: int) -> bool:
    """True если переданный uid — зарезервированный маркер анонимного шёпота."""
    return uid == ANONYMOUS_UID

# ── WebRTC
CMD_WEBRTC_OFFER  = 'webrtc_offer'
CMD_WEBRTC_ANSWER = 'webrtc_answer'
CMD_WEBRTC_ICE    = 'webrtc_ice'

WEBRTC_ICE_TIMEOUT  = 3.0
WEBRTC_ICE_SERVERS: list = []

# ── SFU (Go sidecar)
SFU_PORT       = 7788
SFU_EXE_NAME   = "sidecar.exe"
SFU_PORT_RANGE = 20

# ── Камера-SFU (отдельный инстанс sidecar.exe на своём порту) ──────────────────
# Камера использует ТОТ ЖЕ бинарь sidecar.exe, но поднимается как отдельный
# процесс на собственном диапазоне портов, чтобы не конфликтовать с экраном.
# Так у нас два независимых single-streamer SFU: один под экран, один под камеру.
CAM_SFU_PORT       = 7820
CAM_SFU_PORT_RANGE = 20

# Параметры H.264-кодирования камеры (отправка в свой SFU через aiortc).
# Камера маленькая (квадрат в кружке), поэтому держим скромно — экономия трафика.
#
# ВАЖНО про bitrate: у aiortc встроенный жёсткий пол H.264-энкодера
# MIN_BITRATE = 500 кбит/с — именно поэтому один поток не опускался ниже
# ~670–900 кбит/с. Мы понижаем этот пол на рантайме (CAM_H264_MIN_BITRATE)
# и явно ставим target, т.к. pion-SFU не шлёт REMB и энкодер иначе «висит»
# на дефолте.
CAM_STREAM_FPS       = 25          # ровно 25, больше не надо
CAM_STREAM_SIZE      = 240         # сторона квадрата кружка (px)
CAM_STREAM_SIZE_BIG  = 400         # при разворачивании окна на полэкрана

# Целевые битрейты (bps) — ЖЁСТКИЙ потолок, который НЕЛЬЗЯ превысить.
CAM_STREAM_BITRATE       = 200_000   # обычный кружок ~240px@25fps
CAM_STREAM_BITRATE_BIG   = 500_000   # развёрнутое окно
CAM_H264_MIN_BITRATE     = 60_000    # снимаем заводской пол 500к → 60к
CAM_H264_MAX_BITRATE     = 600_000   # АБСОЛЮТНЫЙ потолок энкодера камеры

# ── TCP-команды
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

# ── Хост: кик/бан участника
CMD_HOST_KICK     = 'host_kick'
CMD_HOST_BAN      = 'host_ban'
CMD_HOST_UNBAN    = 'host_unban'
CMD_BAN_LIST_REQ  = 'ban_list_req'
CMD_BAN_LIST      = 'ban_list'
CMD_KICKED        = 'kicked'
CMD_BANNED        = 'banned'
BAN_LIST_PATH     = os.path.join(get_appdata_dir(), "bans.json")

CMD_QUERY_BAN      = 'query_ban'
CMD_QUERY_BAN_RESP = 'query_ban_resp'

# ── Typing indicator
CMD_TYPING         = 'typing'
TYPING_THROTTLE_MS = 3000
TYPING_EXPIRE_SEC  = 5.0

# ── SQLite чат (хост хранит историю на диске)
CHAT_DB_PATH = os.path.join(get_appdata_dir(), "chat_history.db")

# ── Единый конфиг приложения
APP_CONFIG_PATH = os.path.join(get_appdata_dir(), "inpulse_config.json")

# ── Аннотации стрима
CMD_DRAW_STROKE = 'draw_stroke'

CMD_REMOTE_CONTROL_REQUEST  = 'remote_control_request'
CMD_REMOTE_CONTROL_RESPONSE = 'remote_control_response'
CMD_REMOTE_CONTROL_EVENT    = 'remote_control_event'
CMD_REMOTE_CONTROL_STOP     = 'remote_control_stop'

# ── Remote Control: антиспам запросов управления
# После RC_DENY_LIMIT отказов подряд для пары (стример, зритель)
# зритель уходит в cooldown на RC_COOLDOWN_SEC секунд: его запросы
# молча отбрасываются сервером (стример не видит диалог).
RC_DENY_LIMIT     = 3
RC_COOLDOWN_SEC   = 600          # 10 минут
# Сколько кадров ESC подряд (на стороне стримера) останавливают управление.
RC_ESC_STOP_COUNT = 3
RC_ESC_WINDOW_SEC = 2.0          # три ESC должны уложиться в это окно
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

# Миграция сервера — таймауты и команды
MIGRATION_ANNOUNCE_WAIT_SEC = 4.0
MIGRATION_TCP_TIMEOUT_SEC   = 1.0
MIGRATION_TCP_RETRY_SEC     = 0.3
MIGRATION_TOTAL_DEADLINE    = 10.0

# ── Обрыв связи с хостом (без CMD_SERVER_MIGRATE)
RECONNECT_SAME_HOST_WINDOW_SEC = 8.0
DISCOVERY_WINDOW_SEC           = 6.0
BECOME_HOST_POS0_DELAY_SEC     = 0.5
BECOME_HOST_POS_N_DELAY_SEC    = 1.2

RECONNECT_SAME_HOST_TCP_ATTEMPTS = 20

# ── Обратная совместимость со старым API
MAX_SILENT_RECONNECT_ATTEMPTS = 2
RECONNECT_DELAY               = 1.0

# ── Команды 2-шаговой ручной передачи сервера
CMD_MIGRATE_PREPARE        = 'migrate_prepare'
CMD_MIGRATE_READY          = 'migrate_ready'
MIGRATE_PREPARE_TIMEOUT_SEC = 7.0


AUDIO_DIAG_ENABLED = False   # True → логи [VIEWER-DIAG], [OUT-DIAG], [DLL-DIAG]
