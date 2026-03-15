# client_main.py
# ──────────────────────────────────────────────────────────────────────────────
# Точка входа приложения InPulse.
#
# ПОРЯДОК ИМПОРТОВ КРИТИЧЕН:
#   1. app_init  — UTF-8, faulthandler, DLL-загрузка (ДО всего остального)
#   2. PyQt6     — после DLL
#   3. остальное — после PyQt6
#
# ИЗМЕНЕНИЕ ДЛЯ PYINSTALLER:
#   Вся логика вынесена в main() — run.py вызывает её напрямую через
#   "from client_main.client_main import main; main()".
#   PyInstaller видит явный import → включает модуль в сборку.
#   Относительные импорты (from . import app_init) работают потому что
#   client_main.client_main загружается как часть пакета (__package__='client_main').
# ──────────────────────────────────────────────────────────────────────────────

# ── 1. Инициализация (должна быть первой!) ────────────────────────────────────
from . import app_init

# ── 2. Стандартная библиотека ─────────────────────────────────────────────────
import sys
import multiprocessing

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
    # ── Защита от форк-бомб и subprocess-вызовов ──────────────────────────────
    # Сторонние библиотеки (aiortc, ffmpeg) могут вызывать .exe с аргументами
    # для проверки кодеков. Завершаем тихо — не плодим новые окна GUI.
    multiprocessing.freeze_support()
    if len(sys.argv) > 1:
        sys.exit(0)
    if multiprocessing.current_process().name != 'MainProcess':
        sys.exit(0)

    # ── Дамп аудио-устройств до создания QApplication ─────────────────────────
    # Если PortAudio крашится при query_devices() — увидим это в логе.
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
    import threading as _threading
    import traceback as _tb

    _orig_thread_excepthook = getattr(_threading, 'excepthook', None)

    def _thread_excepthook(args):
        msg = "".join(_tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        print(f"[CRASH] Исключение в потоке '{args.thread.name}':\n{msg}", flush=True)
        with open("crash_python.log", "a", encoding="utf-8") as _f:
            _f.write(f"Thread '{args.thread.name}':\n{msg}")
        if _orig_thread_excepthook:
            _orig_thread_excepthook(args)

    _threading.excepthook = _thread_excepthook
    print("[DEBUG] threading.excepthook установлен", flush=True)

    # ── QSurfaceFormat ДО создания QApplication ───────────────────────────────
    _gl_fmt = QSurfaceFormat()
    _gl_fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    _gl_fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(_gl_fmt)

    # ── QApplication ──────────────────────────────────────────────────────────
    app = QApplication(sys.argv)

    # ✅ Держим глобальные ссылки — GC не уничтожит окна после выхода из if/else.
    _login_window:   LoginWindow        | None = None
    _connect_screen: MultiServerScreen  | None = None

    config = load_config()

    if config:
        # Конфиг найден → мульти-серверный экран
        nick        = config.get("nick",        "User")
        avatar      = config.get("avatar",      "1.svg")
        server_name = config.get("server_name", "InPulse Server")

        _connect_screen = MultiServerScreen(nick, avatar, server_name=server_name)
        _connect_screen.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))

        def _fallback_to_login(f_ip: str, f_nick: str, f_avatar: str):
            """
            ✅ LoginWindow сохраняется в глобальную переменную —
            иначе GC убьёт объект после выхода из функции.
            """
            nonlocal _login_window
            _login_window = LoginWindow(
                ip=f_ip, nick=f_nick, avatar=f_avatar,
                error_msg=(
                    f"⚠️  Сервер недоступен: {f_ip}\n"
                    "Измените адрес и нажмите «Войти»."
                ) if f_ip else "",
            )
            _login_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
            _login_window.show()

        _connect_screen.open_login.connect(_fallback_to_login)
        _connect_screen.show()

    else:
        # Первый запуск → форма логина
        _login_window = LoginWindow()
        _login_window.setWindowIcon(QIcon(resource_path("assets/icon/logo.ico")))
        _login_window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    # Dev-режим: python -m client_main.client_main
    main()