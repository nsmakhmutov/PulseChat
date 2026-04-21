# client_main.py
# ──────────────────────────────────────────────────────────────────────────────
# Точка входа приложения InPulse.
#
# ПОРЯДОК ИМПОРТОВ КРИТИЧЕН:
#   1. multiprocessing.freeze_support() — ДО всего, требование Windows spawn
#   2. app_init  — UTF-8, faulthandler, DLL-загрузка
#   3. PyQt6     — после DLL
#   4. остальное — после PyQt6
# ──────────────────────────────────────────────────────────────────────────────

# ── 0. multiprocessing freeze_support — ПЕРВЫЙ вызов в __main__ ───────────────
# Windows spawn-режим: подпроцессы re-импортируют main module и должны
# завершиться до возврата из freeze_support(). Вызов в main() уже поздно.
import multiprocessing as _mp
import sys as _sys

if __name__ == "__main__" or getattr(_sys, 'frozen', False):
    _mp.freeze_support()
    # Если это дочерний multiprocessing-процесс — завершиться немедленно.
    # current_process().name == 'MainProcess' только в главном.
    if _mp.current_process().name != 'MainProcess':
        _sys.exit(0)

del _mp, _sys

# ── 1. Инициализация (должна быть первой после freeze_support!) ───────────────
from . import app_init

# ── 2. Стандартная библиотека ─────────────────────────────────────────────────
import sys

# ── 3. Qt ─────────────────────────────────────────────────────────────────────
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QIcon, QSurfaceFormat

# ── 4. Приложение ─────────────────────────────────────────────────────────────
from config import resource_path
from .ui_login import load_config, LoginWindow
from .ui_server_select import MultiServerScreen


# ══════════════════════════════════════════════════════════════════════════════
# Точка входа
# ══════════════════════════════════════════════════════════════════════════════

def main():
    """
    Основная функция приложения.

    Вызывается из:
      • run.py (PyInstaller exe): from client_main.client_main import main; main()
      • dev-режим: python -m client_main.client_main → if __name__ == '__main__'
    """
    # ── Защита от subprocess-вызовов ffmpeg/aiortc проверкой кодеков ──────────
    # FIX: Раньше было `if len(sys.argv) > 1: sys.exit(0)` — слишком грубо,
    # блокировало любые CLI-флаги. Теперь проверяем только известные маркеры
    # multiprocessing. Обычные CLI флаги (--debug, --log-level) пройдут.
    for _arg in sys.argv[1:]:
        if _arg.startswith('--multiprocessing') or _arg == '--freeze':
            sys.exit(0)

    # ── Дамп аудио-устройств до создания QApplication ─────────────────────────
    try:
        import sounddevice as _sd
        print("[DEBUG] Аудио-устройства системы:", flush=True)
        for _i, _d in enumerate(_sd.query_devices()):
            _api = _sd.query_hostapis(_d['hostapi'])['name']
            print(
                f"  [{_i:2d}] IN={_d['max_input_channels']} OUT={_d['max_output_channels']}"
                f" | {_d['name']} ({_api})",
                flush=True,
            )
        print(
            f"[DEBUG] Дефолтное устройство: "
            f"IN={_sd.default.device[0]}, OUT={_sd.default.device[1]}",
            flush=True,
        )
    except Exception as _ex:
        print(f"[DEBUG] query_devices() упал: {_ex}", flush=True)

    # ── Перехват исключений в дочерних потоках ────────────────────────────────
    import logging as _logging
    import threading as _threading
    import traceback as _tb

    _orig_thread_excepthook = getattr(_threading, 'excepthook', None)

    def _thread_excepthook(args):
        msg = "".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        _logging.critical("CRASH (thread '%s'):\n%s", args.thread.name, msg)
        if _orig_thread_excepthook:
            _orig_thread_excepthook(args)

    _threading.excepthook = _thread_excepthook
    _logging.debug("threading.excepthook установлен")

    # ── QSurfaceFormat ДО создания QApplication ───────────────────────────────
    _gl_fmt = QSurfaceFormat()
    _gl_fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    _gl_fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(_gl_fmt)

    # ── QApplication ──────────────────────────────────────────────────────────
    app = QApplication(sys.argv)

    # Держим глобальные ссылки — GC не уничтожит окна после выхода из if/else.
    _login_window:   LoginWindow        | None = None
    _connect_screen: MultiServerScreen  | None = None

    config = load_config()

    if config:
        nick        = config.get("nick",        "User")
        avatar      = config.get("avatar",      "1.svg")
        server_name = config.get("server_name", "InPulse Server")

        _connect_screen = MultiServerScreen(nick, avatar, server_name=server_name)
        _connect_screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

        def _fallback_to_login(f_ip: str, f_nick: str, f_avatar: str):
            nonlocal _login_window
            _login_window = LoginWindow(
                ip=f_ip, nick=f_nick, avatar=f_avatar,
                error_msg=(
                    f"⚠️  Сервер недоступен: {f_ip}\n"
                    "Измените адрес и нажмите «Войти»."
                ) if f_ip else "",
            )
            _login_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

            def _back_to_servers():
                nonlocal _connect_screen
                _connect_screen = MultiServerScreen(nick, avatar, server_name=server_name)
                _connect_screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
                _connect_screen.open_login.connect(_fallback_to_login)
                _connect_screen.show()

            _login_window.go_back.connect(_back_to_servers)
            _login_window.show()

        _connect_screen.open_login.connect(_fallback_to_login)
        _connect_screen.show()

    else:
        _login_window = LoginWindow()
        _login_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        _login_window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
