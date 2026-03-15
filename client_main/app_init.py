# app_init.py
# ──────────────────────────────────────────────────────────────────────────────
# Инициализация приложения: кодировки, логирование краша, загрузка DLL.
# Должен быть импортирован ПЕРВЫМ в client_main.py, ДО всех остальных импортов.
# ──────────────────────────────────────────────────────────────────────────────

import os
import sys
import ctypes
import traceback
import faulthandler

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


# ══════════════════════════════════════════════════════════════════════════════
# 1. UTF-8 консоль
# ══════════════════════════════════════════════════════════════════════════════
# На Windows кодировка консоли по умолчанию cp1251 (Russian) или cp866.
# Символы за пределами кодировки (→, ✔, ✖, 🎵 и т.д.) вызывают
# UnicodeEncodeError уже при первом print() с такими символами.
#
# Решение — переключить stdout/stderr на UTF-8 БЕЗ смены кодировки терминала.
# io.TextIOWrapper(buffer, encoding='utf-8', errors='replace') безопасно:
#   • errors='replace' гарантирует что print() никогда не бросит исключение
#   • console=False в EXE (PyInstaller) → stdout/stderr = None: проверяем
import io as _io

for _stream_name in ('stdout', 'stderr'):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None:
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, _io.UnsupportedOperation):
            try:
                setattr(
                    sys, _stream_name,
                    _io.TextIOWrapper(
                        _stream.buffer,
                        encoding='utf-8',
                        errors='replace',
                        line_buffering=_stream.line_buffering,
                    )
                )
            except Exception:
                pass  # frozen без консоли (console=False): None — игнорируем

del _io, _stream_name, _stream


# ══════════════════════════════════════════════════════════════════════════════
# 2. Crash diagnostics
# ══════════════════════════════════════════════════════════════════════════════
# faulthandler пишет нативный C-стектрейс при SIGSEGV / STATUS_STACK_BUFFER_OVERRUN
# прямо в файл — даже если Python уже не работает.

_crash_log = open("crash_native.log", "w", buffering=1)
faulthandler.enable(file=_crash_log)


def _global_excepthook(exc_type, exc_value, exc_tb):
    """Глобальный перехват необработанных Python-исключений → в файл + консоль."""
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"[CRASH] Необработанное исключение:\n{msg}", flush=True)
    with open("crash_python.log", "a", encoding="utf-8") as f:
        f.write(msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _global_excepthook
print("[DEBUG] faulthandler активирован → crash_native.log", flush=True)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Пути к ресурсам
# ══════════════════════════════════════════════════════════════════════════════

def resource_path(relative_path: str) -> str:
    """Получает абсолютный путь к ресурсам, работает для dev и для PyInstaller."""
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


# ══════════════════════════════════════════════════════════════════════════════
# 4. Загрузка DLL (opus, rnnoise, DeepFilterNet)
# ══════════════════════════════════════════════════════════════════════════════
# ПОЧЕМУ os.add_dll_directory НЕДОСТАТОЧНО ДЛЯ opuslib:
#   opuslib использует ctypes.util.find_library('opus'), которая на Windows
#   ищет через PATH (не через директории из add_dll_directory).
#   Решение — два шага:
#     1. Добавить dlls/ в PATH (для find_library).
#     2. Предзагрузить opus.dll через ctypes.CDLL напрямую (гарантия).
#   После предзагрузки DLL уже в памяти процесса → opuslib найдёт её
#   при любом способе поиска.
#
# FIX RUST_LOG: deep_filter.dll (Rust) читает RUST_LOG при инициализации.
#   PyCharm выставляет RUST_LOG="" → ParseLevelError → panic → abort.
if not os.environ.get("RUST_LOG"):
    os.environ["RUST_LOG"] = "error"

_package_dir = os.path.dirname(os.path.abspath(__file__))   # .../client_main/
_project_dir = os.path.dirname(_package_dir)                # .../VoiceChat/  ← корень
_dlls_dir    = os.path.join(_project_dir, "dlls")
_dfn_dir     = os.path.join(_dlls_dir, "DeepFilterNet3")

# ── 1. PATH — нужен для ctypes.util.find_library (opuslib) ──────────────────
_extra_paths = [p for p in [_dlls_dir, _dfn_dir, _project_dir] if os.path.isdir(p)]
if _extra_paths:
    os.environ["PATH"] = os.pathsep.join(_extra_paths) + os.pathsep + os.environ.get("PATH", "")

# ── 2. os.add_dll_directory — нужен для ctypes.CDLL без абсолютного пути ────
for _d in _extra_paths:
    os.add_dll_directory(_d)
try:
    os.add_dll_directory(sys._MEIPASS)   # PyInstaller frozen bundle
except Exception:
    pass

# ── 3. Предзагрузка opus.dll через абсолютный путь ──────────────────────────
_opus_candidates = [
    os.path.join(_dlls_dir, "opus.dll"),
    os.path.join(_dlls_dir, "libopus.dll"),
    os.path.join(_dlls_dir, "libopus-0.dll"),
    os.path.join(_project_dir, "opus.dll"),
]
_opus_loaded = False
for _opus_path in _opus_candidates:
    if os.path.exists(_opus_path):
        try:
            ctypes.CDLL(_opus_path)
            print(f"[DLL] opus предзагружен: {_opus_path}", flush=True)
            _opus_loaded = True
            break
        except Exception as _e:
            print(f"[DLL] Не удалось загрузить {_opus_path}: {_e}", flush=True)

if not _opus_loaded:
    print(f"[DLL] ВНИМАНИЕ: opus.dll не найдена в {_dlls_dir}", flush=True)

# Чистим временные переменные из namespace
del _opus_path, _opus_candidates, _opus_loaded, _d, _extra_paths
del _project_dir, _dlls_dir, _dfn_dir