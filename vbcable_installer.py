"""
vbcable_installer.py
────────────────────────────────────────────────────────────────────────────────
Автоматическая проверка и тихая установка VB-CABLE из локального zip-архива.

VB-CABLE создаёт пару виртуальных устройств:
  «CABLE Input»  — виртуальный динамик (OUTPUT) → игра выводит сюда
  «CABLE Output» — виртуальный микрофон (INPUT)  → мы захватываем отсюда

Поскольку голоса зрителей физически НИКОГДА не попадают в «CABLE Input»,
захват с «CABLE Output» математически чист — эхо невозможно как явление.
AEC, ducking и любые другие костыли не нужны.

Публичный API:
    is_vbcable_installed()           → bool
    find_zip()                       → str | None
    install_vbcable(zip_path=None)   → (success: bool, message: str)
"""

import os
import sys
import ctypes
import zipfile
import subprocess
import tempfile

# ── Константы ────────────────────────────────────────────────────────────────

CABLE_OUTPUT_KEYWORD = "CABLE Output"
CABLE_INPUT_KEYWORD  = "CABLE Input"

SETUP_EXE_X64 = "VBCABLE_Setup_x64.exe"
SETUP_EXE_X86 = "VBCABLE_Setup.exe"

VBCABLE_ZIP_NAMES = [
    "VBCABLE_Driver_Pack45.zip",
    "VBCABLE_Driver_Pack44.zip",
    "VBCABLE_Driver_Pack43.zip",
    "VBCABLE_Driver_Pack.zip",
]


# ── Обнаружение ───────────────────────────────────────────────────────────────

def is_vbcable_installed() -> bool:
    """True если CABLE Output присутствует среди аудио-устройств."""
    try:
        import sounddevice as sd
        for d in sd.query_devices():
            if CABLE_OUTPUT_KEYWORD.lower() in d['name'].lower():
                return True
    except Exception as e:
        print(f"[VBC] Ошибка проверки устройств: {e}")
    return False


def find_zip():
    """
    Ищет архив VB-CABLE в текущей папке и папке рядом с этим модулем.
    Возвращает полный путь или None.
    """
    search_dirs = [
        os.path.abspath("."),
        os.path.dirname(os.path.abspath(__file__)),
    ]
    for directory in search_dirs:
        for name in VBCABLE_ZIP_NAMES:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                print(f"[VBC] Найден архив: {path}")
                return path
    return None


# ── Права администратора ──────────────────────────────────────────────────────

def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ── Установка ─────────────────────────────────────────────────────────────────

def install_vbcable(zip_path=None):
    """
    Устанавливает VB-CABLE из zip-архива.
    Возвращает (success: bool, message: str).

    Если уже администратор — ставим напрямую.
    Иначе — ShellExecute runas (UAC-диалог Windows).
    """
    zip_path = zip_path or find_zip()
    if not zip_path:
        return False, (
            "Архив VB-CABLE не найден.\n"
            "Убедитесь что файл VBCABLE_Driver_Pack45.zip\n"
            "лежит в папке с программой:\n"
            + os.path.abspath('.')
        )

    if not os.path.exists(zip_path):
        return False, f"Файл не найден: {zip_path}"

    if _is_admin():
        return _do_install(zip_path)
    else:
        return _elevate_and_install(zip_path)


def _do_install(zip_path):
    """Распаковывает архив и запускает установщик (вызывается с правами администратора)."""
    try:
        extract_dir = tempfile.mkdtemp(prefix="vbcable_")
        print(f"[VBC] Распаковка в {extract_dir}...")

        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(extract_dir)

        # Ищем установочный exe
        setup_exe = None
        for name in [SETUP_EXE_X64, SETUP_EXE_X86]:
            candidate = os.path.join(extract_dir, name)
            if os.path.exists(candidate):
                setup_exe = candidate
                break

        if setup_exe is None:
            for root, _dirs, files in os.walk(extract_dir):
                for f in files:
                    if f.lower() in [SETUP_EXE_X64.lower(), SETUP_EXE_X86.lower()]:
                        setup_exe = os.path.join(root, f)
                        break
                if setup_exe:
                    break

        if setup_exe is None:
            return False, (
                "Установочный .exe не найден в архиве.\n"
                f"Ожидались: {SETUP_EXE_X64} или {SETUP_EXE_X86}"
            )

        print(f"[VBC] Запуск: {setup_exe} /S")
        result = subprocess.run([setup_exe, "/S"], timeout=120)

        if result.returncode == 0:
            return True, (
                "VB-CABLE успешно установлен!\n\n"
                "Необходима перезагрузка компьютера.\n"
                "После перезагрузки:\n"
                "  1. Откройте настройки звука Windows\n"
                "  2. Для игры/плеера выберите вывод «CABLE Input»\n"
                "  3. Наушники/динамики оставьте как есть\n\n"
                "Зрители будут слышать игру — эхо невозможно."
            )
        else:
            return False, (
                f"Установщик завершился с кодом {result.returncode}.\n"
                f"Попробуйте установить вручную из архива:\n{zip_path}"
            )

    except subprocess.TimeoutExpired:
        return False, "Установщик завис (таймаут 120 сек). Установите вручную."
    except Exception as e:
        return False, f"Ошибка установки: {e}"


def _elevate_and_install(zip_path):
    """
    Создаёт временный helper-скрипт и запускает его с UAC-повышением.
    Возвращает сразу — установка асинхронная в отдельном процессе.
    """
    try:
        helper_code = (
            'import zipfile, subprocess, tempfile, os, sys, ctypes\n'
            '\n'
            'zip_path = r"' + zip_path.replace('\\', '\\\\') + '"\n'
            'exe_x64  = "' + SETUP_EXE_X64 + '"\n'
            'exe_x86  = "' + SETUP_EXE_X86 + '"\n'
            '\n'
            'def msg(text, title="VB-CABLE"):\n'
            '    ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)\n'
            '\n'
            'try:\n'
            '    d = tempfile.mkdtemp(prefix="vbcable_")\n'
            '    with zipfile.ZipFile(zip_path, "r") as z:\n'
            '        z.extractall(d)\n'
            '    exe = None\n'
            '    for name in [exe_x64, exe_x86]:\n'
            '        p = os.path.join(d, name)\n'
            '        if os.path.exists(p): exe = p; break\n'
            '    if not exe:\n'
            '        for root, dirs, files in os.walk(d):\n'
            '            for f in files:\n'
            '                if f.lower() in [exe_x64.lower(), exe_x86.lower()]:\n'
            '                    exe = os.path.join(root, f); break\n'
            '            if exe: break\n'
            '    if not exe:\n'
            '        msg("Установочный файл не найден в архиве!")\n'
            '    else:\n'
            '        r = subprocess.run([exe, "/S"], timeout=120)\n'
            '        if r.returncode == 0:\n'
            '            msg("VB-CABLE установлен!\\n\\nПерезагрузите компьютер.\\n"\n'
            '                "После перезагрузки направьте вывод игры на CABLE Input.")\n'
            '        else:\n'
            '            msg(f"Ошибка установки (код {r.returncode})")\n'
            'except Exception as ex:\n'
            '    msg(f"Ошибка: {ex}")\n'
            'finally:\n'
            '    try: os.unlink(__file__)\n'
            '    except: pass\n'
        )

        helper_path = os.path.join(
            os.path.dirname(zip_path),
            "_vbcable_install_helper.py"
        )
        with open(helper_path, 'w', encoding='utf-8') as f:
            f.write(helper_code)

        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable,
            f'"{helper_path}"', None, 1
        )

        if ret <= 32:
            return False, "Установка отменена или не удалось запустить с правами администратора."

        return True, (
            "Установщик запущен с правами администратора.\n"
            "Дождитесь завершения,\n"
            "затем перезагрузите компьютер."
        )

    except Exception as e:
        return False, f"Ошибка запуска UAC: {e}"
