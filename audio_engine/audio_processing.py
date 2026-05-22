import ctypes
import os
import numpy as np

def _setup_rust_env():
    _k32 = ctypes.windll.kernel32
    _k32.SetEnvironmentVariableW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    _k32.SetEnvironmentVariableW.restype  = ctypes.c_bool
    _k32.SetEnvironmentVariableW("RUST_LOG",       None)
    _k32.SetEnvironmentVariableW("DF_LEVEL",       None)
    _k32.SetEnvironmentVariableW("RUST_LOG_STYLE", None)
    _k32.SetEnvironmentVariableW("RUST_LOG",       "warn")
    _k32.SetEnvironmentVariableW("DF_LEVEL",       "warn")
    _k32.SetEnvironmentVariableW("RUST_LOG_STYLE", "never")
    try:
        _ucrt = ctypes.CDLL("ucrtbase.dll")
        _ucrt._wputenv_s.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        _ucrt._wputenv_s("RUST_LOG",       "warn")
        _ucrt._wputenv_s("DF_LEVEL",       "warn")
        _ucrt._wputenv_s("RUST_LOG_STYLE", "never")
    except Exception:
        pass
    os.environ.pop("RUST_LOG",       None)
    os.environ.pop("DF_LEVEL",       None)
    os.environ.pop("RUST_LOG_STYLE", None)
    os.environ["RUST_LOG"]       = "warn"
    os.environ["DF_LEVEL"]       = "warn"
    os.environ["RUST_LOG_STYLE"] = "never"
    _buf = ctypes.create_unicode_buffer(256)
    _k32.GetEnvironmentVariableW("RUST_LOG", _buf, 256)
    print(f"[DFN] module init: RUST_LOG(kernel32)='{_buf.value}'", flush=True)

_setup_rust_env()

class DeepFilterEngine:
    _MODEL_FILES = ("config.ini", "enc.onnx", "erb_dec.onnx", "df_dec.onnx")

    @staticmethod
    def _ensure_tar(model_dir: str) -> str:
        import tarfile

        tar_path = model_dir.rstrip("\\/") + ".tar.gz"

        if os.path.exists(tar_path):
            tar_mtime = os.path.getmtime(tar_path)
            need_rebuild = False
            for fname in DeepFilterEngine._MODEL_FILES:
                fpath = os.path.join(model_dir, fname)
                if os.path.exists(fpath) and os.path.getmtime(fpath) > tar_mtime:
                    need_rebuild = True
                    break
            if not need_rebuild:
                return tar_path

        missing = [f for f in DeepFilterEngine._MODEL_FILES
                   if not os.path.exists(os.path.join(model_dir, f))]
        if missing:
            raise FileNotFoundError(
                f"Файлы модели DFN3 не найдены в {model_dir}: {missing}"
            )

        print(f"[DFN] Создаём tar-архив моделей: {tar_path}")
        with tarfile.open(tar_path, "w:gz") as tf:
            for fname in DeepFilterEngine._MODEL_FILES:
                fpath = os.path.join(model_dir, fname)
                tf.add(fpath, arcname=fname)
        print(f"[DFN] Архив создан ({os.path.getsize(tar_path) // 1024} KB)")
        return tar_path

    def __init__(self):
        root      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model_dir = os.path.join(root, "dlls", "DeepFilterNet3")
        dll_path  = os.path.join(model_dir, "deep_filter.dll")

        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"deep_filter.dll не найдена: {dll_path}")

        tar_path = self._ensure_tar(model_dir)

        if not self._probe_dll(dll_path, tar_path):
            raise RuntimeError(
                "deep_filter.dll аварийно завершила subprocess при инициализации "
                "(Rust abort/panic). DFN недоступен — используется RNNoise."
            )

        _k32 = ctypes.windll.kernel32
        _k32.SetEnvironmentVariableW("RUST_LOG",       "warn")
        _k32.SetEnvironmentVariableW("DF_LEVEL",       "warn")
        _k32.SetEnvironmentVariableW("RUST_LOG_STYLE", "never")
        try:
            _ucrt = ctypes.CDLL("ucrtbase.dll")
            _ucrt._wputenv_s.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            _ucrt._wputenv_s("RUST_LOG", "warn")
            _ucrt._wputenv_s("DF_LEVEL", "warn")
        except Exception:
            pass

        self.lib = ctypes.CDLL(dll_path)

        self.lib.df_create.argtypes = [
            ctypes.c_char_p,
            ctypes.c_float,
            ctypes.c_char_p,
        ]
        self.lib.df_create.restype = ctypes.c_void_p

        self.lib.df_process_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]
        self.lib.df_process_frame.restype = ctypes.c_float

        self.handle = self.lib.df_create(
            tar_path.encode('utf-8'),
            ctypes.c_float(100.0),
            None,
        )
        if not self.handle:
            raise RuntimeError("df_create вернул NULL — модель не загружена")

        try:
            self.lib.df_get_frame_length.argtypes = [ctypes.c_void_p]
            self.lib.df_get_frame_length.restype  = ctypes.c_size_t
            self.frame_len = int(self.lib.df_get_frame_length(self.handle))
        except AttributeError:
            self.frame_len = 480

    @staticmethod
    def _probe_dll(dll_path: str, tar_path: str) -> bool:
        import subprocess
        import sys
        import json

        if getattr(sys, 'frozen', False):
            print("[DFN] Frozen mode — subprocess probe пропущен, загрузка напрямую.",
                  flush=True)
            return True

        dll_dir    = os.path.dirname(dll_path)        # dlls/DeepFilterNet3/
        dlls_dir   = os.path.dirname(dll_dir)         # dlls/

        ort_capi_dir = ""
        try:
            import onnxruntime as _ort
            _ort_pkg = os.path.dirname(_ort.__file__)
            _ort_capi = os.path.join(_ort_pkg, "capi")
            ort_capi_dir = _ort_capi if os.path.isdir(_ort_capi) else _ort_pkg
        except ImportError:
            pass

        extra_dirs = [d for d in [dll_dir, dlls_dir, ort_capi_dir]
                      if d and os.path.isdir(d)]

        add_dirs_code = "".join(
            f"os.add_dll_directory({json.dumps(d)});" for d in extra_dirs
        )

        probe_code = (
            "import ctypes,sys,os;"
            f"{add_dirs_code}"
            f"lib=ctypes.CDLL({json.dumps(dll_path)});"
            "lib.df_create.argtypes=[ctypes.c_char_p,ctypes.c_float,ctypes.c_char_p];"
            "lib.df_create.restype=ctypes.c_void_p;"
            f"h=lib.df_create({json.dumps(tar_path)}.encode(),ctypes.c_float(100.0),None);"
            "print('DFN_PROBE_OK' if h else 'DFN_PROBE_NULL',flush=True);"
            "sys.exit(0)"
        )

        probe_env = {
            **os.environ,
            "RUST_LOG":       "warn",
            "DF_LEVEL":       "warn",
            "RUST_LOG_STYLE": "never",
        }

        try:
            result = subprocess.run(
                [sys.executable, "-c", probe_code],
                capture_output=True,
                timeout=20,
                env=probe_env,
                creationflags=0x08000000,
            )
            stdout = result.stdout.decode(errors="replace")
            stderr = result.stderr.decode(errors="replace")

            if result.returncode != 0:
                print(
                    f"[DFN] Probe FAILED exit={result.returncode} "
                    f"(0x{result.returncode & 0xFFFFFFFF:08X}). "
                    f"DFN недоступен, будет использован RNNoise.",
                    flush=True,
                )
                if stderr.strip():
                    print(f"[DFN] Probe stderr:\n{stderr[:800]}", flush=True)
                if stdout.strip():
                    print(f"[DFN] Probe stdout: {stdout[:200]}", flush=True)
                return False

            if "DFN_PROBE_OK" in stdout:
                print("[DFN] Probe OK — DLL работает, загружаем в основной процесс.",
                      flush=True)
                return True
            else:
                print(f"[DFN] Probe: df_create вернул NULL (модель не загружена). "
                      f"stdout={stdout[:100]!r}", flush=True)
                return False

        except subprocess.TimeoutExpired:
            print("[DFN] Probe timeout — DLL зависла. DFN недоступен.", flush=True)
            return False
        except Exception as e:
            print(f"[DFN] Probe exception: {e}", flush=True)
            return False

    def process(self, audio_np_float32):
        out = np.zeros_like(audio_np_float32)
        for i in range(0, len(audio_np_float32), self.frame_len):
            in_ptr = audio_np_float32[i:].ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            out_ptr = out[i:].ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self.lib.df_process_frame(self.handle, in_ptr, out_ptr)
        return out

def butter(order: int, cutoff_hz, btype: str = 'low',
           fs: float = None, output: str = 'ba') -> np.ndarray:

    if btype != 'low' or output != 'sos':
        raise NotImplementedError("butter(): только btype='low', output='sos'")
    if fs is None:
        raise ValueError("butter(): требуется параметр fs")

    Wn  = float(cutoff_hz) / (fs * 0.5)          # нормированная (0..1, 1=Найквист)
    wa  = 2.0 * np.tan(np.pi * Wn * 0.5)         # pre-warp → аналоговая частота

    k       = np.arange(order)
    poles_a = np.exp(1j * np.pi * (2.0*k + order + 1.0) / (2.0 * order))
    poles_a = poles_a * wa                        # масштаб по частоте среза

    z_d    = (1.0 + 0.5*poles_a) / (1.0 - 0.5*poles_a)
    zeros_d = np.full(order, -1.0 + 0j)

    idx = np.argsort(-z_d.imag)
    z_d = z_d[idx]

    n_sec = order // 2
    sos   = np.zeros((n_sec, 6))

    for i in range(n_sec):
        p1, p2 = z_d[i],      z_d[-(i+1)]
        z1, z2 = zeros_d[2*i], zeros_d[2*i+1]

        b = np.real(np.poly([z1, z2]))
        a = np.real(np.poly([p1, p2]))

        sos[i, :3] = b
        sos[i, 3:] = a

    section_gains = np.array([
        np.sum(sos[i, :3]) / np.sum(sos[i, 3:]) for i in range(n_sec)
    ])
    total_gain = np.prod(section_gains)
    per_sec_corr = total_gain ** (1.0 / n_sec)
    for i in range(n_sec):
        sos[i, :3] /= per_sec_corr

    return sos


def sosfilt(sos: np.ndarray, x: np.ndarray,
            zi: np.ndarray = None):

    x   = np.asarray(x, dtype=np.float64)
    n_s = sos.shape[0]
    zf  = (np.zeros((n_s, 2), dtype=np.float64)
           if zi is None else np.array(zi, dtype=np.float64))
    y   = x.copy()

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        s1, s2 = zf[i, 0], zf[i, 1]

        b1_y = b1 * y
        b2_y = b2 * y
        out  = np.empty_like(y)

        for n in range(len(y)):
            out[n] = b0 * y[n] + s1
            s1     = b1_y[n] - a1 * out[n] + s2
            s2     = b2_y[n] - a2 * out[n]

        zf[i, 0], zf[i, 1] = s1, s2
        y = out

    return y, zf


def sosfilt_zi(sos: np.ndarray) -> np.ndarray:

    n_s  = sos.shape[0]
    zi   = np.zeros((n_s, 2), dtype=np.float64)
    scale = 1.0

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        K       = (b0 + b1 + b2) / (1.0 + a1 + a2)
        zi[i,1] = (b2 - a2 * K) * scale
        zi[i,0] = (b1 - a1 * K) * scale + zi[i,1]
        scale  *= K
    return zi

_WLP_SOS     = butter(4, 1200, btype='low', fs=48000, output='sos')
_ANON_LP_SOS = butter(4, 4000, btype='low', fs=48000, output='sos')

try:
    from pyrnnoise import RNNoise

    PYRNNOISE_AVAILABLE = True
except ImportError:
    PYRNNOISE_AVAILABLE = False
    print("[Audio] Внимание: Модуль pyrnnoise не найден.")

try:
    import pyaudiowpatch as _pyaudio
    PYAUDIOWPATCH_AVAILABLE = True
except ImportError:
    _pyaudio = None
    PYAUDIOWPATCH_AVAILABLE = False