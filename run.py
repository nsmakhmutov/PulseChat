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

# В frozen-режиме _MEIPASS — корень сборки где лежат все пакеты.
# В dev-режиме — директория run.py (корень проекта).
_base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

# Явный import — PyInstaller включает client_main.client_main в сборку
from client_main.client_main import main  # noqa: E402

main()