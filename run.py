# run.py — точка входа PyInstaller для InPulse
#
# ПОЧЕМУ ЭТОТ ПОДХОД РАБОТАЕТ:
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
import sys
import os
import logging
import logging.handlers

# ── Пути ─────────────────────────────────────────────────────────────────────
_base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

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
    _ch = logging.StreamHandler(sys.stdout)
    _ch.setLevel(logging.WARNING)
    _ch.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    _root.addHandler(_ch)

    # ── Перехват необработанных исключений главного потока ────────────────────
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

    logging.info('=== InPulse запущен. Логи: %s ===', _logs_dir)
    return _logs_dir


LOGS_DIR: str = _setup_logging()

# ── Явный import — PyInstaller включает client_main.client_main в сборку ─────
from client_main.client_main import main  # noqa: E402

main()