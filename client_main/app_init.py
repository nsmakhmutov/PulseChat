
import os
import sys
import ctypes
import traceback
import faulthandler

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

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
                pass

del _io, _stream_name, _stream

def _resolve_logs_dir() -> str:
    appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
    logs_dir = os.path.join(appdata, 'InPulse', 'logs')
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except OSError:
        import tempfile
        logs_dir = tempfile.gettempdir()
    return logs_dir

_LOGS_DIR = _resolve_logs_dir()
_crash_native_path = os.path.join(_LOGS_DIR, 'crash_native.log')
try:
    _crash_log = open(_crash_native_path, 'w', buffering=1, encoding='utf-8')
    faulthandler.enable(file=_crash_log)
except OSError as _e:
    print(f"[DEBUG] Не удалось открыть {_crash_native_path}: {_e}", flush=True)
    faulthandler.enable()

def _global_excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    print(f"[CRASH] Необработанное исключение:\n{msg}", flush=True)
    _crash_python_path = os.path.join(_LOGS_DIR, 'crash_python.log')
    try:
        with open(_crash_python_path, 'a', encoding='utf-8') as f:
            f.write(msg)
    except OSError:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_tb)


sys.excepthook = _global_excepthook
print(f"[DEBUG] faulthandler активирован → {_crash_native_path}", flush=True)

def resource_path(relative_path: str) -> str:
    try:
        base_path = sys._MEIPASS
    except AttributeError:
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

if not os.environ.get("RUST_LOG"):
    os.environ["RUST_LOG"] = "error"

_package_dir = os.path.dirname(os.path.abspath(__file__))
_project_dir = os.path.dirname(_package_dir)
_dlls_dir    = os.path.join(_project_dir, "dlls")
_dfn_dir     = os.path.join(_dlls_dir, "DeepFilterNet3")

_extra_paths = [p for p in [_dlls_dir, _dfn_dir, _project_dir] if os.path.isdir(p)]
if _extra_paths:
    os.environ["PATH"] = os.pathsep.join(_extra_paths) + os.pathsep + os.environ.get("PATH", "")

for _d in _extra_paths:
    try:
        os.add_dll_directory(_d)
    except (OSError, AttributeError):
        pass
try:
    os.add_dll_directory(sys._MEIPASS)
except (AttributeError, OSError):
    pass

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

for _v in ('_opus_path', '_opus_candidates', '_opus_loaded',
           '_d', '_extra_paths', '_project_dir', '_dlls_dir', '_dfn_dir',
           '_package_dir'):
    globals().pop(_v, None)
del _v
