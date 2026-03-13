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
# ── Встроенный сервер: обнаружение в сети (Discovery) ────────────────────────
#
# Клиент при старте слушает UDP broadcast на DISCOVERY_PORT.
# Встроенный сервер (EmbeddedServer) рассылает анонсы каждые DISCOVERY_INTERVAL с.
# Если за DISCOVERY_TIMEOUT с никто не ответил — предлагаем создать сервер.
#
# Порт 5002 выбран чтобы не конфликтовать с TCP (5000) и UDP голоса (5001).
DISCOVERY_PORT     = 5002   # UDP broadcast: поиск / анонс встроенного сервера
DISCOVERY_INTERVAL = 0.5    # было 1.5с — анонс 2 раза в секунду, сервер виден мгновенно
DISCOVERY_TIMEOUT  = 1.5    # было 3.5с — за 1.5с клиент получит 3 анонса (при 0.5с интервале)

# ── Передача/миграция сервера ─────────────────────────────────────────────────
#
# Два сценария:
#   1. Ручная: хост ПКМ → «Передать сервер» → CMD_SERVER_TRANSFER (client→server)
#              Сервер находит IP цели, рассылает CMD_SERVER_MIGRATE всем.
#              Цель стартует встроенный сервер; остальные переподключаются.
#
#   2. Авто:   Хост уходит без предупреждения (выключил ПК, упал инет).
#              Клиенты видят обрыв соединения, запускают _auto_host_check().
#              Первый в host_order стартует сервер; остальные обнаруживают его
#              через UDP broadcast (ServerDiscovery) и переподключаются.
#
CMD_SERVER_TRANSFER = 'server_transfer'  # клиент → сервер: передать хостинг uid
CMD_SERVER_MIGRATE  = 'server_migrate'   # сервер → все:    IP нового хоста

# ── Быстрый чат (Quick Message) ───────────────────────────────────────────────
# Лёгкий обмен короткими сообщениями без БД и истории.
# Сообщение живёт 5 секунд в UI рядом с ником отправителя.
# Максимальная длина текста: QUICK_MSG_MAX_LEN символов.
CMD_QUICK_MSG     = 'quick_msg'   # клиент → сервер → все в комнате
QUICK_MSG_MAX_LEN = 20            # символов — ограничение на клиенте и сервере

# ══════════════════════════════════════════════════════════════════════════════
# ПАТЧ для config.py — добавить в конец файла
# Новые команды для мульти-серверной архитектуры и временных каналов
# ══════════════════════════════════════════════════════════════════════════════

# ── Управление временными каналами ────────────────────────────────────────────
#
# Временный канал создаётся хостом через ПКМ в пустом месте дерева.
# Канал существует пока в нём есть хотя бы один участник.
# При выходе последнего — сервер автоматически рассылает CMD_CHANNEL_DELETED.
#
# Пароль опционален. Если задан — клиент должен отправить CMD_JOIN_CHANNEL_AUTH
# перед CMD_JOIN_ROOM, иначе сервер отклонит JOIN с ошибкой 'channel_auth_required'.
#
CMD_CREATE_CHANNEL   = 'create_channel'    # хост → сервер: создать временный канал
CMD_CHANNEL_CREATED  = 'channel_created'   # сервер → все: новый канал добавлен
CMD_CHANNEL_DELETED  = 'channel_deleted'   # сервер → все: канал удалён (пустой)
CMD_JOIN_CHANNEL_AUTH = 'join_channel_auth' # клиент → сервер: войти в защищённый канал
CMD_CHANNEL_LIST     = 'channel_list'       # сервер → все: список каналов (в sync_users)

# Ограничения на имя и пароль канала
CHANNEL_NAME_MAX_LEN = 32
CHANNEL_PASS_MAX_LEN = 64

# ── Имя сервера ───────────────────────────────────────────────────────────────
#
# Хранится в user_config.json вместе с nick/avatar.
# Используется ServerAnnouncer для идентификации сервера в списке.
# Отображается в DiscoveryScreen и ServerBarWidget MainWindow.
#
SERVER_NAME_MAX_LEN  = 40   # символов — ограничение на имя сервера
SERVER_NAME_DEFAULT  = 'InPulse Server'