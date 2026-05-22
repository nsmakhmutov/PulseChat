# run.py — точка входа PyInstaller для InPulse
#
# ПОЧЕМУ ЯВНЫЙ ИМПОРТ РАБОТАЕТ:
#
#   "from client_main.client_main import main" — явный статический импорт.
#   PyInstaller видит его при анализе зависимостей и включает модуль в сборку.
#
#   При выполнении этой строки Python:
#     1. Ищет пакет client_main (находит в _MEIPASS благодаря hidden_imports)
#     2. Загружает client_main/client_main.py КАК ПОДМОДУЛЬ ПАКЕТА
#        → __package__ = 'client_main'
#     3. Выполняет top-level код: from . import app_init  ← теперь работает!
#     4. Возвращает функцию main
#   Затем main() запускает приложение.
#
#   sys.path: PyInstaller сам добавляет _MEIPASS в sys.path для frozen exe.
#

# ── Стандартные импорты ───────────────────────────────────────────────────────
import sys
import os
import logging
import logging.handlers

# ── Пути ─────────────────────────────────────────────────────────────────────
_base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)


# ── Stdout → logging (чтобы print() тоже попадал в файл) ─────────────────────
# Все print([Net] ...), print([Server] ...) и т.д. будут в inpulse.log.
# Строки [OUT-DIAG] и [Stats] — периодический диагностический шум —
# фильтруются: в файл не пишутся (остаются только в консоли/stdout).
class _PrintToLog:
    """
    Перехватывает sys.stdout.write() и направляет каждую непустую строку
    в logging.info() под именем «app».

    Исключения (пишутся только в консоль, не в файл):
      • строки, начинающиеся с [OUT-DIAG] — аудио-диагностика раз в секунду
      • строки, начинающиеся с [Stats]    — сетевая статистика раз в 5 с
    """

    # Префиксы, которые НЕ нужно писать в файл
    _SKIP_PREFIXES = ('[OUT-DIAG]', '[Stats]')

    def __init__(self, original_stream, logger: logging.Logger):
        self._orig = original_stream
        self._log  = logger
        self._buf  = ''

    def write(self, text: str) -> int:
        # Всегда дублируем в оригинальный stdout (для консоли/отладчика)
        if self._orig is not None:
            try:
                self._orig.write(text)
            except Exception:
                pass

        self._buf += text
        # Флашим по строкам
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            line = line.rstrip('\r')
            if line and not any(line.startswith(p) for p in self._SKIP_PREFIXES):
                self._log.info('%s', line)

        return len(text)

    def flush(self):
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:
                pass

    # Proxy остальных атрибутов потока (encoding, isatty, etc.)
    def __getattr__(self, name):
        return getattr(self._orig, name)


# ── Настройка логирования ─────────────────────────────────────────────────────
# Логи пишутся в %APPDATA%\InPulse\logs\inpulse.log
# Ротация: 5 MB × 5 файлов. Крашлог отдельно: crash.log
def _setup_logging() -> str:
    """Настраивает логирование в файл и консоль. Возвращает путь к папке логов."""
    _appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
    _logs_dir = os.path.join(_appdata, 'InPulse', 'logs')
    os.makedirs(_logs_dir, exist_ok=True)

    _log_file = os.path.join(_logs_dir, 'inpulse.log')

    _root = logging.getLogger()
    _root.setLevel(logging.DEBUG)

    # ── Файловый хендлер (RotatingFileHandler: 5 MB × 5 файлов) ────────────
    _fh = logging.handlers.RotatingFileHandler(
        _log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8',
    )
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    _root.addHandler(_fh)

    # ── Консольный хендлер (только WARNING+, чтобы не мусорить в stdout) ────
    _ch = logging.StreamHandler(sys.__stdout__)   # sys.__stdout__ = настоящий stdout до редиректа
    _ch.setLevel(logging.WARNING)
    _ch.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    _root.addHandler(_ch)

    # ══════════════════════════════════════════════════════════════════════
    # ФИЛЬТРАЦИЯ ШУМНЫХ СТОРОННИХ БИБЛИОТЕК
    # ══════════════════════════════════════════════════════════════════════
    #
    # aiortc.rtcrtpsender — пишет КАЖДЫЙ RTP-пакет на уровне DEBUG.
    #   При голосовом чате: 50 пакетов/сек на соединение = 3 000 строк/мин.
    #   На 3 участниках это ~9 000 строк/мин — весь 5 MB файл за ~8 минут.
    #
    # aioice.ice — STUN Binding keepalive каждые ~5 сек на каждый ICE-кандидат.
    #
    # comtypes / comtypes._post_coinit.unknwn — Release() COM-объектов DirectX
    #   при инициализации захвата экрана (WGC/DXGI).
    #
    # asyncio — «Using proactor: IocpProactor» и прочие внутренности event loop.
    #
    # Уровень WARNING оставляет: ошибки ICE, ошибки DTLS, сбои кодека.

    _THIRD_PARTY_SILENCE = (
        # ── aiortc: пакетный трафик (самый громкий) ──────────────────────
        'aiortc.rtcrtpsender',      # RtpPacket / RtcpSrPacket / RtcpSdesPacket
        'aiortc.rtcrtpreceiver',    # входящие RTP
        'aiortc.rtcrtpparameters',  # параметры кодека при старте
        # ── aioice: ICE STUN keepalive ───────────────────────────────────
        'aioice',                   # покрывает aioice.ice, aioice.stun, ...
        # ── comtypes: COM / DirectX ──────────────────────────────────────
        'comtypes',                 # покрывает comtypes._post_coinit.unknwn, ...
        # ── asyncio internals ────────────────────────────────────────────
        'asyncio',
    )
    for _name in _THIRD_PARTY_SILENCE:
        logging.getLogger(_name).setLevel(logging.WARNING)

    # ── aiortc state-change логгеры: оставить на INFO ────────────────────
    # Полезны для диагностики: «ICE connected», «DTLS handshake done»,
    # смена состояний PeerConnection — именно по ним видно, подключился ли клиент.
    _AIORTC_INFO = (
        'aiortc.rtcpeerconnection',  # iceConnectionState, connectionState
        'aiortc.rtcdtlstransport',   # DTLS handshake, State.CONNECTED/CLOSED
        'aiortc.rtcicetransport',    # ICE completed / failed / closed
        'aiortc.rtcdatachannel',     # DataChannel open/close
    )
    for _name in _AIORTC_INFO:
        logging.getLogger(_name).setLevel(logging.INFO)

    # ── Перехват необработанных исключений главного потока ────────────────
    _crash_file = os.path.join(_logs_dir, 'crash.log')

    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback
        msg = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logging.critical('CRASH (main thread):\n%s', msg)
        try:
            with open(_crash_file, 'a', encoding='utf-8') as _f:
                import datetime
                _f.write(f'\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] CRASH:\n{msg}')
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    # ── Перехват необработанных исключений в потоках (threading) ──────────
    import threading

    def _thread_excepthook(args):
        import traceback
        msg = ''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        thread_name = args.thread.name if args.thread else 'unknown'
        logging.critical('CRASH (thread=%s):\n%s', thread_name, msg)
        try:
            with open(_crash_file, 'a', encoding='utf-8') as _f:
                import datetime
                _f.write(
                    f'\n[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}]'
                    f' CRASH (thread={thread_name}):\n{msg}'
                )
        except Exception:
            pass

    threading.excepthook = _thread_excepthook
    logging.debug('threading.excepthook установлен')

    # ── Перенаправление sys.stdout → logging (print() попадает в файл) ───
    _app_logger = logging.getLogger('app')
    _app_logger.setLevel(logging.DEBUG)
    sys.stdout = _PrintToLog(sys.__stdout__, _app_logger)

    logging.info('=== InPulse запущен. Логи: %s ===', _logs_dir)
    return _logs_dir


LOGS_DIR: str = _setup_logging()

# ── Явный import — PyInstaller гарантированно включает updater в сборку ──────
# hiddenimports ненадёжен для локальных .py файлов: если PyInstaller не смог
# импортировать модуль при анализе, он молча выпадает из бандла без ошибки.
# Статический import здесь = модуль всегда виден через граф зависимостей.
import core.updater  # noqa: F401
import core.win_input  # noqa: F401  (WinAPI-инъекция для удалённого управления)

# ── Явный import — PyInstaller включает client_main.client_main в сборку ─────
from client_main.client_main import main  # noqa: E402

main()