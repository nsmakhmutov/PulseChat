import sys
import os
import struct

# --- Утилиты ---
def resource_path(relative_path):
    """ Получает абсолютный путь к ресурсам, работает и для dev, и для PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

# --- Сетевые настройки ---
DEFAULT_PORT_TCP = 5000
DEFAULT_PORT_UDP = 5001

# BUFFER_SIZE: максимальный UDP-пакет.
# UDP_HEADER(13) + VIDEO_HEADER(8) + MAX_VIDEO_PAYLOAD(1400) = 1421 байт.
# Запас оставлен для роста MAX_VIDEO_PAYLOAD.
BUFFER_SIZE = 65536

# Размер системного буфера приёма UDP на сервере (2 MB).
# При 6 Mbps видео → ~750 KB/s входящего трафика.
# Было 8 MB (10 сек буфера) — слишком много для реал-тайма: пинг-пакеты
# встают в очередь позади видео и показывают 1000+ мс RTT.
# 2 MB = ~2.7 сек при 6 Mbps — достаточно для буферизации кратких всплесков,
# не создавая многосекундной задержки в очереди ядра.
UDP_RECV_BUFFER_SIZE = 2 * 1024 * 1024  # 2 MB

# Размер системного буфера отправки UDP на сервере (2 MB).
# Сервер форвардит IDR-кадр (~300 KB при 6 Mbps) сразу 2-3 зрителям → ~900 KB.
# 256 KB (предложение ИИ) = sendto() заблокирует при форварде большого IDR.
# 2 MB = достаточный запас без риска блокировки на сервере.
UDP_SEND_BUFFER_SIZE = 2 * 1024 * 1024  # 2 MB

# --- Аудио настройки ---
SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_DURATION = 20
CHUNK_SIZE = int(SAMPLE_RATE * (FRAME_DURATION / 1000))

# --- Видео настройки ---
VIDEO_WIDTH = 1280
VIDEO_HEIGHT = 720
VIDEO_FPS = 60
VIDEO_BITRATE = 6000000  # 6 Mbps

# VIDEO_PACING_RATE: скорость отправки видео-пакетов в pacing-потоке (байт/сек).
# = VIDEO_BITRATE * 1.25 / 8, чтобы покрыть UDP+IP overhead и не создавать бёрсты.
# При VIDEO_BITRATE=6Mbps → 937 500 байт/сек → ~720 пакетов/сек → ~1.39 мс/пакет.
# Увеличивать с осторожностью: чем выше — тем меньше эффект pacing.
VIDEO_PACING_RATE_BYTES_SEC = int(VIDEO_BITRATE * 1.25 / 8)  # ~937 500 байт/сек

# Флаг для UDP заголовка (битмаска):
# 1=Mute, 2=Deaf, 4=Video, 8=StreamAudio, 16=LoopbackAudio, 32=StreamVoices, 64=Whisper, 254=Ping
FLAG_VIDEO          = 4
# FLAG_VIDEO_LQ: признак низкокачественного видеопотока (Simulcast).
# Пересылается в заголовке UDP вместе с FLAG_VIDEO (итого flags=132).
# Сервер маршрутизирует LQ-пакеты только слабым зрителям,
# HQ-пакеты (flags=4) — только сильным зрителям.
# 128 не пересекается ни с одним существующим флагом.
FLAG_VIDEO_LQ       = 128

# --- Simulcast LQ-поток настройки ---
# Активируется автоматически при наличии h264_nvenc (нулевая нагрузка на CPU).
# При libx264 fallback — simulcast отключён (двойной CPU-кодек недопустим).
LQ_VIDEO_BITRATE = 800_000   # 800 kbps — хватает для 480p
LQ_VIDEO_WIDTH   = 854       # 16:9 480p
LQ_VIDEO_HEIGHT  = 480

# ABR_LQ_THRESHOLD: зрители, запрашивающие ≤ этого битрейта, получают LQ-поток.
# 1 500 000 = 1.5 Mbps → зрители на 800k и 1.5M идут в LQ; 2.5M и выше — HQ.
ABR_LQ_THRESHOLD = 1_500_000

FLAG_STREAM_AUDIO   = 8   # Аудио стрима — маршрутизируется только зрителям
FLAG_LOOPBACK_AUDIO = 16  # Подтип: системный звук (WASAPI Loopback), бит поверх FLAG_STREAM_AUDIO
# FLAG_WHISPER: приватная передача голоса одному пользователю.
# Payload: [target_uid: 4 байта big-endian unsigned int] + [opus данные].
# Сервер смотрит target_uid и доставляет пакет только ему.
# Остальные участники комнаты пакет не получают.
FLAG_WHISPER        = 64

# FLAG_STREAM_VOICES: голосовой микс стримера — отдельный поток для зрителей.
# Payload: [speaker_uid: 4 байта big-endian unsigned int] + [opus данные]
# Зритель проверяет speaker_uid и ОТБРАСЫВАЕТ пакет если speaker_uid == my_uid,
# тем самым не слыша собственного голоса в стриме (Mix Minus без DSP).
FLAG_STREAM_VOICES  = 32  # Подтип: голоса чата из стрима, бит поверх FLAG_STREAM_AUDIO

# Struct для извлечения speaker_uid из voice-stream пакета
STREAM_VOICE_HEADER_STRUCT = struct.Struct("!I")   # 4 байта: speaker_uid
STREAM_VOICE_HEADER_SIZE   = STREAM_VOICE_HEADER_STRUCT.size

# Смещение UID для хранения loopback-потоков в словаре stream_remote_users.
# Loopback от стримера с uid=X хранится под ключом X + LOOPBACK_UID_OFFSET,
# чтобы не смешиваться с его же микрофонным потоком (ключ X).
LOOPBACK_UID_OFFSET = 1_000_000

# MAX_VIDEO_PAYLOAD: максимальный payload одного чанка в UDP-пакете.
# Расчёт безопасного MTU для RadminVPN:
#   IP header    : 20 байт
#   UDP header   :  8 байт
#   UDP_HEADER   : 17 байт  ("!IdIB" = 4+8+4+1)
#   VIDEO_HEADER :  8 байт  ("!IHH" = 4+2+2)
#   Payload      : 1150 байт
#   ИТОГО        : 1203 байт — гарантированно пролетит без фрагментации
#   (RadminVPN MTU ≈ 1350–1400; запас ~150–200 байт на инкапсуляцию)
#
# Было 1300 → итого 1353 байт — опасно близко к порогу VPN; фрагментация
# UDP-пакета = потеря всего чанка при любой «дырке» в маршруте.
MAX_VIDEO_PAYLOAD = 1150
VIDEO_CHUNK_HEADER = struct.Struct("!IHH")
VIDEO_HEADER_SIZE = VIDEO_CHUNK_HEADER.size

# --- Opus настройки ---
# opuslib.APPLICATION_VOIP обычно равен 2048
OPUS_APPLICATION = 2048
DEFAULT_BITRATE = 64000

# Структура для видео-пакетов (псевдоним VIDEO_CHUNK_HEADER — идентичный формат "!IHH")
VIDEO_CHUNK_STRUCT = VIDEO_CHUNK_HEADER

# --- Структура заголовка UDP пакета ---
# UID (I) + Timestamp (d) + Sequence (I) + Flags (B)
UDP_HEADER_STRUCT = struct.Struct("!IdIB")
UDP_HEADER_SIZE = UDP_HEADER_STRUCT.size

# VIDEO_LOW_QUALITY_IDR_INTERVAL_MS: константа нужна ui_video.py для IDR-таймера.
# В текущей архитектуре (без quality routing) таймер запускается, но отправляет
# запрос в stub-метод net.request_viewer_keyframe() → ничего не происходит.
# Значение 2000мс оставлено для совместимости с ui_video.py.
VIDEO_LOW_QUALITY_IDR_INTERVAL_MS = 2000

# --- ABR (Adaptive Bitrate) настройки трансляции ---
# Порог RTT → целевой битрейт энкодера (байт/сек).
# Сервер вычисляет min по всем зрителям и сообщает стримеру.
# Уровни: 6 Мбит → 4 → 2.5 → 1.5 → 0.8 Мбит.
# Гистерезис: повышение только если RTT держится 2 цикла подряд.
ABR_TIERS = [
    (65,  6_000_000),   # RTT < 65 мс  → 6 Mbps (покрывает стабильные 50-55 мс RadminVPN)
    (90,  4_000_000),   # RTT < 90 мс  → 4 Mbps
    (130, 2_500_000),   # RTT < 130 мс → 2.5 Mbps
    (180, 1_500_000),   # RTT < 180 мс → 1.5 Mbps
    (999, 800_000),     # RTT ≥ 180 мс → 800 Kbps (минимум, хватит для 480p)
]
# Интервал отправки фидбека зрителем (мс)
ABR_FEEDBACK_INTERVAL_MS = 4000

# TCP-команды для ABR
CMD_BITRATE_FEEDBACK = 'bitrate_feedback'   # зритель → сервер
CMD_ADJUST_BITRATE   = 'adjust_bitrate'     # сервер → стример

# --- Команды ---
CMD_LOGIN         = 'login'
CMD_JOIN_ROOM     = 'join_room'
CMD_CHAT_MSG      = 'chat_msg'
CMD_SYNC_USERS    = 'sync_users'
CMD_SOUNDBOARD    = 'play_soundboard'
CMD_UPDATE_STATUS = 'update_status'
CMD_STREAM_START  = 'stream_start'
CMD_STREAM_STOP   = 'stream_stop'

# Команда смены пользовательского статуса (иконка + текст).
# Клиент → сервер при изменении статуса.
# Сервер ретранслирует через sync_users всем участникам.
# status_icon: имя SVG-файла из папки assets/status/ (или '' — нет статуса).
# status_text: произвольный текст ≤ 30 символов (или '' — нет подсказки).
CMD_UPDATE_PRESENCE = 'update_presence'
# --- Фича «Пнуть» (Nudge) ---
# CMD_NUDGE_VOTE      — клиент → сервер: голос за «пнуть» target_uid
# CMD_PLAY_NUDGE      — сервер → цель: воспроизвести Danger.mp3 + писк
# CMD_NUDGE_TRIGGERED — сервер → все в комнате: broadcast-тост (кто пнул / кого)
CMD_NUDGE_VOTE      = 'nudge_vote'
CMD_PLAY_NUDGE      = 'play_nudge'
CMD_NUDGE_TRIGGERED = 'nudge_triggered'

# Кулдаун: один voter может проголосовать за одну цель раз в 10 минут.
# После срабатывания звука голоса сбрасываются, кулдаун остаётся.
NUDGE_COOLDOWN_SEC  = 600  # 10 минут

# Путь к звуковому файлу «Пнуть» — всегда через resource_path (работает и в dev, и в exe).
NUDGE_SOUND_PATH = resource_path(os.path.join("assets", "music", "Danger.mp3"))