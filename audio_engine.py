import asyncio
import threading
import queue
import math
from collections import deque
from typing import Callable
import numpy as np
import sounddevice as sd
import opuslib
import heapq
import struct
import time
import ctypes
import os

# NativeLoopbackCapture импортируется лениво внутри StreamAudioCapture._try_dll()
# чтобы DLL не грузилась при старте приложения — только когда нужен стрим.

# ── FIX: RUST_LOG на уровне модуля — ДО любых DLL ───────────────────────────
# Rust DLL читает RUST_LOG через GetEnvironmentVariableW (Win32 API).
# Устанавливаем через ВСЕ возможные механизмы:
#   1) kernel32.SetEnvironmentVariableW — основной Win32 API (видят все CRT)
#   2) ucrtbase._wputenv_s — UCRT CRT блок (на случай старого Rust/MSVC)
#   3) os.environ — Python CRT блок
# Сначала УДАЛЯЕМ текущее значение (в т.ч. невалидный ""), потом ставим "warn".
def _setup_rust_env():
    _k32 = ctypes.windll.kernel32
    _k32.SetEnvironmentVariableW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    _k32.SetEnvironmentVariableW.restype  = ctypes.c_bool
    # Удаляем — передача NULL второго аргумента = DeleteEnvironmentVariable
    _k32.SetEnvironmentVariableW("RUST_LOG",       None)
    _k32.SetEnvironmentVariableW("DF_LEVEL",       None)
    _k32.SetEnvironmentVariableW("RUST_LOG_STYLE", None)
    # Ставим валидные значения
    _k32.SetEnvironmentVariableW("RUST_LOG",       "warn")
    _k32.SetEnvironmentVariableW("DF_LEVEL",       "warn")
    _k32.SetEnvironmentVariableW("RUST_LOG_STYLE", "never")
    # Через UCRT (ucrtbase.dll) — Rust на Windows компилируется против него
    try:
        _ucrt = ctypes.CDLL("ucrtbase.dll")
        _ucrt._wputenv_s.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        _ucrt._wputenv_s("RUST_LOG",       "warn")
        _ucrt._wputenv_s("DF_LEVEL",       "warn")
        _ucrt._wputenv_s("RUST_LOG_STYLE", "never")
    except Exception:
        pass
    # Python (CRT копия)
    os.environ.pop("RUST_LOG",       None)
    os.environ.pop("DF_LEVEL",       None)
    os.environ.pop("RUST_LOG_STYLE", None)
    os.environ["RUST_LOG"]       = "warn"
    os.environ["DF_LEVEL"]       = "warn"
    os.environ["RUST_LOG_STYLE"] = "never"
    # Диагностика: проверяем что kernel32 видит то, что мы выставили
    _buf = ctypes.create_unicode_buffer(256)
    _k32.GetEnvironmentVariableW("RUST_LOG", _buf, 256)
    print(f"[DFN] module init: RUST_LOG(kernel32)='{_buf.value}'", flush=True)

_setup_rust_env()

import av
from aiortc import AudioStreamTrack
# ── Встроенная замена scipy.signal (butter / sosfilt / sosfilt_zi) ─────────────
# Причина: scipy.signal при импорте транзитивно подтягивает scipy.stats, которая
# содержит exec()-генерацию в _distn_infrastructure.py. В замороженном exe
# (PyInstaller) имя 'obj' теряется из exec()-контекста → NameError на старте.
# collect_submodules/collect_data_files не устраняют эту проблему.
#
# Данная реализация покрывает ровно три вызова этого файла:
#   butter(4, 1200, btype='low', fs=48000, output='sos')
#   sosfilt(sos, x, zi=zi)
#   sosfilt_zi(sos)
# Алгоритм: аналоговый прототип Баттерворта → bilinear transform → SOS.


class DeepFilterEngine:
    # Файлы модели, которые должны быть запакованы в tar-архив.
    # Порядок важен: df_create читает архив последовательно.
    _MODEL_FILES = ("config.ini", "enc.onnx", "erb_dec.onnx", "df_dec.onnx")

    @staticmethod
    def _ensure_tar(model_dir: str) -> str:
        """
        Возвращает путь к tar-архиву моделей.

        DLL deep_filter.dll написана на Rust и ожидает путь к TAR-файлу
        (не к директории). Передача пути к директории → Rust вызывает
        File::open(dir) → Windows возвращает ERROR_ACCESS_DENIED (os error 5).

        Алгоритм:
          1. Если рядом с папкой уже есть DeepFilterNet3.tar.gz — возвращаем его.
          2. Иначе создаём tar из четырёх файлов модели (без сжатия,
             без вложенных папок — файлы кладутся в корень архива).
        """
        import tarfile

        tar_path = model_dir.rstrip("\\/") + ".tar.gz"

        # Если tar уже создан — проверяем что он не старше файлов модели
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

        # Проверяем наличие всех файлов
        missing = [f for f in DeepFilterEngine._MODEL_FILES
                   if not os.path.exists(os.path.join(model_dir, f))]
        if missing:
            raise FileNotFoundError(
                f"Файлы модели DFN3 не найдены в {model_dir}: {missing}"
            )

        # Пакуем в tar.gz — DLL написана на Rust и ожидает именно gzip-сжатый tar
        print(f"[DFN] Создаём tar-архив моделей: {tar_path}")
        with tarfile.open(tar_path, "w:gz") as tf:
            for fname in DeepFilterEngine._MODEL_FILES:
                fpath = os.path.join(model_dir, fname)
                # arcname=fname → файлы в корне архива, без подпапок
                tf.add(fpath, arcname=fname)
        print(f"[DFN] Архив создан ({os.path.getsize(tar_path) // 1024} KB)")
        return tar_path

    def __init__(self):
        root      = os.path.dirname(os.path.abspath(__file__))
        model_dir = os.path.join(root, "dlls", "DeepFilterNet3")
        dll_path  = os.path.join(model_dir, "deep_filter.dll")

        if not os.path.exists(dll_path):
            raise FileNotFoundError(f"deep_filter.dll не найдена: {dll_path}")

        # Создаём / проверяем tar-архив моделей.
        tar_path = self._ensure_tar(model_dir)

        # ── SUBPROCESS PROBE ─────────────────────────────────────────────────
        # Rust panic() → os::process::abort() — это НЕ Python exception.
        # try/except никогда не поймает abort(). Весь процесс умирает.
        #
        # Решение: запускаем df_create в отдельном subprocess.
        # Если он упал (returncode != 0) → выбрасываем обычный RuntimeError,
        # который caller (AudioHandler.__init__) поймает через except Exception
        # и продолжит работу с RNNoise вместо DFN.
        if not self._probe_dll(dll_path, tar_path):
            raise RuntimeError(
                "deep_filter.dll аварийно завершила subprocess при инициализации "
                "(Rust abort/panic). DFN недоступен — используется RNNoise."
            )

        # Probe выжил → DLL безопасна. Загружаем в основной процесс.
        # Повторно форсируем RUST_LOG непосредственно перед загрузкой DLL.
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

        # ── Настройка типов C-API ─────────────────────────────────────────────
        #
        # КОРЕНЬ ВСЕХ ПРЕДЫДУЩИХ ПАНИК: сигнатура df_create в capi.rs:
        #   fn df_create(path, atten_lim: f32, log_level: *const c_char) -> *mut DFState
        #
        # Мы передавали только path (1 аргумент вместо 3).
        # atten_lim читался из мусора в стеке → случайное float-значение (норм).
        # log_level читался из мусора → случайный указатель → Rust пытался
        # распарсить мусорную строку как log level → ParseLevelError → panic!
        #
        # Исправление: передаём все 3 аргумента явно.
        #   atten_lim = 100.0  — нет ограничения на подавление (макс. эффект)
        #   log_level = None   — NULL pointer → Rust берёт ветку None → логгер
        #                        не инициализируется → паники нет
        self.lib.df_create.argtypes = [
            ctypes.c_char_p,    # path: путь к tar.gz модели
            ctypes.c_float,     # atten_lim: предел подавления в dB
            ctypes.c_char_p,    # log_level: NULL = без логгера (не паникует)
        ]
        self.lib.df_create.restype = ctypes.c_void_p

        self.lib.df_process_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]
        self.lib.df_process_frame.restype = ctypes.c_float  # возвращает local SNR

        # Передаём путь к TAR-архиву + корректные аргументы
        self.handle = self.lib.df_create(
            tar_path.encode('utf-8'),
            ctypes.c_float(100.0),  # atten_lim: 100 dB — без ограничений
            None,                   # log_level: NULL → Rust не создаёт логгер
        )
        if not self.handle:
            raise RuntimeError("df_create вернул NULL — модель не загружена")

        # DFN3: размер кадра.
        # Функция называется df_get_frame_LENGTH (не df_get_frame_LEN).
        try:
            self.lib.df_get_frame_length.argtypes = [ctypes.c_void_p]
            self.lib.df_get_frame_length.restype  = ctypes.c_size_t
            self.frame_len = int(self.lib.df_get_frame_length(self.handle))
        except AttributeError:
            self.frame_len = 480  # стандарт DFN3 @ 48 kHz

    @staticmethod
    def _probe_dll(dll_path: str, tar_path: str) -> bool:
        """
        Тестирует deep_filter.dll в изолированном subprocess.
        Возвращает True если DLL инициализировалась без краша.

        Rust panic() → abort() завершает ВЕСЬ процесс на уровне ОС.
        Python try/except не способен перехватить abort().
        Единственная защита — тест в дочернем процессе.

        ИСПРАВЛЕНИЯ:
          1. Frozen mode (PyInstaller EXE): sys.executable = InPulse.exe.
             InPulse.exe -c "..." PyInstaller bootloader не поддерживает —
             запустил бы приложение заново или вылетел. Probe пропускается,
             загрузка идёт напрямую (RUST_LOG уже выставлен на уровне модуля).

          2. Python 3.8+ Windows: ctypes.CDLL НЕ использует PATH для зависимостей
             загружаемой DLL. Работают только системные директории + директории
             из os.add_dll_directory(). В subprocess os.add_dll_directory не
             вызывался → deep_filter.dll не находила свои зависимости → probe
             падал с returncode 1, хотя DLL физически была на месте.
             Фикс: добавляем нужные директории явно в probe-код.
        """
        import subprocess
        import sys
        import json

        # ── Frozen mode: probe через subprocess невозможен ────────────────────
        # sys.frozen выставляется PyInstaller bootloader'ом.
        # В frozen режиме все DLL уже рядом с exe, RUST_LOG выставлен,
        # поэтому просто сигнализируем что всё ок — загрузка пройдёт напрямую.
        if getattr(sys, 'frozen', False):
            print("[DFN] Frozen mode — subprocess probe пропущен, загрузка напрямую.",
                  flush=True)
            return True

        # ── Python 3.8+ DLL search: собираем директории для add_dll_directory ──
        # deep_filter.dll's собственная директория ищется Windows автоматически.
        # Но её ЗАВИСИМОСТИ (onnxruntime.dll и др.) через PATH уже не находятся.
        # Нужно явно добавить все нужные пути через os.add_dll_directory.

        dll_dir    = os.path.dirname(dll_path)        # dlls/DeepFilterNet3/
        dlls_dir   = os.path.dirname(dll_dir)         # dlls/

        # Onnxruntime capi — если deep_filter.dll динамически линкована с ORT
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

        # Генерируем код add_dll_directory для каждой директории.
        # Вставляется в начало probe-кода перед загрузкой DLL.
        add_dirs_code = "".join(
            f"os.add_dll_directory({json.dumps(d)});" for d in extra_dirs
        )

        # Код для subprocess: сначала добавляем директории, потом грузим DLL.
        # log_level=None → NULL pointer → Rust не создаёт логгер → нет паники.
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
                creationflags=0x08000000,  # CREATE_NO_WINDOW
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
                # Полный stderr — чтобы видеть реальную ошибку (WinError, OSError и т.д.)
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
        # audio_np_float32 имеет размер CHUNK=960. Проходим его двумя кадрами по 480.
        out = np.zeros_like(audio_np_float32)
        for i in range(0, len(audio_np_float32), self.frame_len):
            in_ptr = audio_np_float32[i:].ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            out_ptr = out[i:].ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            self.lib.df_process_frame(self.handle, in_ptr, out_ptr)
        return out

def butter(order: int, cutoff_hz, btype: str = 'low',
           fs: float = None, output: str = 'ba') -> np.ndarray:
    """
    Butterworth LP-фильтр → SOS матрица.
    Поддерживает только btype='low', output='sos'.
    """
    if btype != 'low' or output != 'sos':
        raise NotImplementedError("butter(): только btype='low', output='sos'")
    if fs is None:
        raise ValueError("butter(): требуется параметр fs")

    Wn  = float(cutoff_hz) / (fs * 0.5)          # нормированная (0..1, 1=Найквист)
    wa  = 2.0 * np.tan(np.pi * Wn * 0.5)         # pre-warp → аналоговая частота

    # Аналоговые полюсы Баттерворта (левая полуплоскость, |p|=1)
    k       = np.arange(order)
    poles_a = np.exp(1j * np.pi * (2.0*k + order + 1.0) / (2.0 * order))
    poles_a = poles_a * wa                        # масштаб по частоте среза

    # Bilinear transform z = (1 + s/2) / (1 - s/2)
    # Все нули аналогового LP → z = -1 после преобразования
    z_d    = (1.0 + 0.5*poles_a) / (1.0 - 0.5*poles_a)
    zeros_d = np.full(order, -1.0 + 0j)

    # Сортируем по убыванию Im, чтобы сопряжённые пары стояли рядом
    idx = np.argsort(-z_d.imag)
    z_d = z_d[idx]

    n_sec = order // 2
    sos   = np.zeros((n_sec, 6))

    for i in range(n_sec):
        p1, p2 = z_d[i],      z_d[-(i+1)]        # сопряжённая пара полюсов
        z1, z2 = zeros_d[2*i], zeros_d[2*i+1]    # нули (-1, -1)

        b = np.real(np.poly([z1, z2]))             # числитель:  [1, -(z1+z2), z1*z2]
        a = np.real(np.poly([p1, p2]))             # знаменатель:[1, -(p1+p2), p1*p2]

        sos[i, :3] = b
        sos[i, 3:] = a

    # Нормируем общий DC-gain (H(z=1)) к 1.0.
    # Считаем текущий gain и распределяем коррекцию равномерно по секциям.
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
    """
    Применяет SOS-фильтр к сигналу x. Возвращает (y, zf).
    Direct Form II Transposed (DF2T) — совместимо с scipy.signal.sosfilt.
    """
    x   = np.asarray(x, dtype=np.float64)
    n_s = sos.shape[0]
    zf  = (np.zeros((n_s, 2), dtype=np.float64)
           if zi is None else np.array(zi, dtype=np.float64))
    y   = x.copy()

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        s1, s2 = zf[i, 0], zf[i, 1]
        out = np.empty_like(y)
        for n in range(len(y)):                    # hot-path: 960 итераций × 2 секции
            v      = y[n]
            out[n] = b0 * v + s1
            s1     = b1 * v - a1 * out[n] + s2
            s2     = b2 * v - a2 * out[n]
        zf[i, 0], zf[i, 1] = s1, s2
        y = out

    return y, zf


def sosfilt_zi(sos: np.ndarray) -> np.ndarray:
    """
    Начальные условия для sosfilt (unit step, без переходного процесса).
    DF2T steady-state при x=1: zi[i] = [s1_ss, s2_ss] для каждой секции.
    """
    n_s  = sos.shape[0]
    zi   = np.zeros((n_s, 2), dtype=np.float64)
    scale = 1.0                                    # накопленный gain от предыдущих секций

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        K       = (b0 + b1 + b2) / (1.0 + a1 + a2)   # DC gain этой секции
        zi[i,1] = (b2 - a2 * K) * scale               # s2 в steady-state
        zi[i,0] = (b1 - a1 * K) * scale + zi[i,1]     # s1 в steady-state
        scale  *= K                                    # выход → вход следующей секции

    return zi
from PyQt6.QtCore import QObject, pyqtSignal, QSettings
from config import *

# ---------------------------------------------------------------------------
# Предвычисленные SOS-матрицы фильтров — константы модуля.
# Вычисляются ОДИН РАЗ при импорте (не при каждом __init__ AudioHandler).
# Параметры фиксированы: SAMPLE_RATE=48000 Гц, order=4, Butterworth LP.
#   _WLP_SOS    — fc=1200 Гц, для эффекта «рация» (legacy, не используется в prod)
#   _ANON_LP_SOS — fc=4000 Гц, для эффекта «анонимный голос» (whisper receiver side)
# ---------------------------------------------------------------------------
_WLP_SOS     = butter(4, 1200, btype='low', fs=48000, output='sos')
_ANON_LP_SOS = butter(4, 4000, btype='low', fs=48000, output='sos')

try:
    from pyrnnoise import RNNoise

    PYRNNOISE_AVAILABLE = True
except ImportError:
    PYRNNOISE_AVAILABLE = False
    print("[Audio] Внимание: Модуль pyrnnoise не найден.")





class StreamAudioCapture:
    """
    Захват системного аудио для стрима → WebRTC.

    Два метода захвата с автоматическим fallback:
      [0] loopback_capture.dll v8 (WASAPI Application Loopback API, Win 11+)
          Захват ВСЕГО звука системы, кроме нашего процесса (exclude_pid).
          Требует: Windows Build >= 20348, dlls/loopback_capture.dll.
      [1] pyaudiowpatch WASAPI loopback
          Захват звука дефолтного выходного устройства.
          Работает на Windows 10/11, не требует спецдрайверов.
          Недостаток: не исключает звук нашего приложения (но на практике
          наше приложение в стрим-аудио не попадает т.к. оно тихое).

    Жизненный цикл:
        capture = StreamAudioCapture(pcm_callback)
        capture.start()
        # ... стрим идёт ...
        capture.stop()
    """

    def __init__(self, pcm_callback=None):
        """
        pcm_callback(chunk: np.ndarray) — вызывается на каждый готовый
        20-мс PCM-фрейм (float32, моно, CHUNK_SIZE=960 сэмплов).
        """
        self._pcm_callback = pcm_callback
        self._running      = threading.Event()
        self._thread       = None

    # ── Публичный API ─────────────────────────────────────────────────────────

    def start(self, device_idx=None):
        self.stop()
        self._running.set()
        self._thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="stream-audio-loopback",
        )
        self._thread.start()

    def stop(self):
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    # ── Внутренние методы ─────────────────────────────────────────────────────

    def _capture_loop(self):
        """Пробует [0] DLL, при неудаче — [1] pyaudiowpatch."""
        if self._try_dll():
            return
        if self._try_pyaudiowpatch():
            return
        print("[StreamAudio] ✖ Все методы захвата недоступны. Стрим-аудио отключён.")

    # ── Метод [0]: loopback_capture.dll ──────────────────────────────────────

    def _try_dll(self) -> bool:
        """
        Пробует запустить захват через loopback_capture.dll.
        Возвращает True если захват отработал (даже если завершился с ошибкой
        в процессе работы), False если DLL недоступна/не поддерживается.
        """
        try:
            # Ленивый импорт: DLL не грузится при старте приложения,
            # только когда пользователь нажал «Начать стрим».
            from native_capture import NativeLoopbackCapture
        except ImportError:
            print("[StreamAudio] [0] native_capture.py не найден — пропуск DLL")
            return False

        if not NativeLoopbackCapture.dll_available():
            print("[StreamAudio] [0] loopback_capture.dll не найдена — пропуск")
            return False

        cap = NativeLoopbackCapture()

        if not cap.is_supported():
            print("[StreamAudio] [0] Windows Build < 20348 — пропуск DLL")
            return False

        print(f"[StreamAudio] [0] DLL найдена, Windows поддерживает API. "
              f"Инициализация (pid={os.getpid()})...")

        if not cap.init(os.getpid()):
            dll_log = cap.get_dll_log() if hasattr(cap, 'get_dll_log') else ""
            print(f"[StreamAudio] [0] init_capture вернул ошибку "
                  f"({cap.get_last_error()}) — пропуск")
            if dll_log.strip():
                print(f"[StreamAudio] [0] DLL internal log:\n{dll_log}")
            return False

        print("[StreamAudio] ✔ [0] loopback_capture.dll захват запущен")
        chunk = np.empty(CHUNK_SIZE, dtype=np.float32)
        try:
            while self._running.is_set():
                n = cap.read_frames(chunk)
                if n == CHUNK_SIZE:
                    if self._pcm_callback is not None:
                        try:
                            self._pcm_callback(chunk.copy())
                        except Exception:
                            pass
                else:
                    time.sleep(0.005)
        finally:
            cap.stop()
            print("[StreamAudio] Loopback поток остановлен [0/DLL]")
        return True  # метод отработал — fallback не нужен

    # ── Метод [1]: pyaudiowpatch WASAPI loopback ──────────────────────────────

    @staticmethod
    def _find_loopback_device(pa):
        """
        Находит WASAPI loopback-устройство для текущего дефолтного выхода.
        Возвращает (device_info, sd_idx) или (None, None).
        """
        try:
            # Дефолтное OUTPUT устройство через sounddevice
            default_out = sd.query_devices(kind='output')
            out_name    = default_out['name'] if default_out else None
        except Exception:
            out_name = None

        best = None
        for i in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(i)
            except Exception:
                continue
            # pyaudiowpatch помечает loopback-устройства флагом isLoopbackDevice
            if not info.get('isLoopbackDevice', False):
                continue
            if info.get('maxInputChannels', 0) < 1:
                continue
            # Если знаем имя дефолтного выхода — ищем совпадение
            if out_name and out_name.lower() in info['name'].lower():
                return info, i
            # Иначе запомним первое попавшееся loopback
            if best is None:
                best = (info, i)

        return best if best else (None, None)

    def _try_pyaudiowpatch(self) -> bool:
        """
        Пробует запустить захват через pyaudiowpatch WASAPI loopback.
        Возвращает True если захват отработал, False если недоступен.
        """
        try:
            import pyaudiowpatch as pyaudio
        except ImportError:
            print("[StreamAudio] [1] pyaudiowpatch не установлен — пропуск")
            return False

        pa = pyaudio.PyAudio()
        try:
            dev_info, dev_idx = self._find_loopback_device(pa)
            if dev_info is None:
                print("[StreamAudio] [1] WASAPI loopback устройство не найдено — пропуск")
                return False

            sr             = int(dev_info['defaultSampleRate'])
            nch            = int(dev_info['maxInputChannels'])
            frames_per_buf = int(sr * 0.020)  # 20 мс

            print(f"[StreamAudio] [1] pyaudiowpatch loopback: "
                  f"«{dev_info['name']}» ch={nch} sr={sr}")

            # FIFO для ресэмплинга/downmix — collections.deque быстрее list
            from collections import deque
            pcm_fifo: deque = deque()
            # Предвычисляем коэффициент ресэмплинга (0 = не нужен)
            resample_ratio = (SAMPLE_RATE / sr) if sr != SAMPLE_RATE else 0.0

            def _pa_callback(in_data, frame_count, time_info, status):
                # ── Критически важные проверки ────────────────────────────────
                # 1. in_data может быть None при первом вызове или при overflow.
                #    np.frombuffer(None) -> TypeError -> поток падает -> is_active()=False
                # 2. Весь callback обёрнут в try-except чтобы любое исключение
                #    не убивало поток (pyaudio при исключении в callback = abort).
                if not self._running.is_set():
                    return (None, pyaudio.paComplete)
                if in_data is None:
                    return (None, pyaudio.paContinue)
                try:
                    raw = np.frombuffer(in_data, dtype=np.float32)
                    if len(raw) == 0:
                        return (None, pyaudio.paContinue)

                    # Stereo → Mono downmix
                    if nch > 1:
                        raw = raw.reshape(-1, nch).mean(axis=1)

                    # Ресэмплинг если нужен (линейная интерполяция)
                    if resample_ratio:
                        target_len = max(1, round(len(raw) * resample_ratio))
                        indices = np.linspace(0, len(raw) - 1, target_len)
                        raw = np.interp(indices, np.arange(len(raw)), raw).astype(np.float32)

                    pcm_fifo.extend(raw)

                    # Отправляем готовые CHUNK_SIZE-фреймы
                    while len(pcm_fifo) >= CHUNK_SIZE:
                        chunk = np.fromiter(
                            (pcm_fifo.popleft() for _ in range(CHUNK_SIZE)),
                            dtype=np.float32, count=CHUNK_SIZE)
                        if self._pcm_callback is not None:
                            try:
                                self._pcm_callback(chunk)
                            except Exception:
                                pass
                except Exception as exc:
                    # Логируем только первый раз чтобы не спамить
                    print(f"[StreamAudio] [1] callback error: {exc}")
                return (None, pyaudio.paContinue)

            stream = pa.open(
                format=pyaudio.paFloat32,
                channels=nch,
                rate=sr,
                frames_per_buffer=frames_per_buf,
                input=True,
                input_device_index=dev_idx,
                stream_callback=_pa_callback,
            )
            stream.start_stream()
            print(f"[StreamAudio] ✔ [1] pyaudiowpatch захват запущен (ch={nch} sr={sr})")

            # Ждём пока поток активен или нас попросили остановиться
            while self._running.is_set() and stream.is_active():
                time.sleep(0.1)

            if self._running.is_set() and not stream.is_active():
                # Поток умер сам по себе — скорее всего ошибка драйвера
                print("[StreamAudio] [1] pyaudio stream died unexpectedly")

            stream.stop_stream()
            stream.close()
            print("[StreamAudio] Loopback поток остановлен [1/pyaudiowpatch]")
        finally:
            pa.terminate()

        return True


class MicrophoneTrack(AudioStreamTrack):
    """
    Захват микрофона через sounddevice → WebRTC AudioStreamTrack.

    Используется стримером для передачи голоса зрителям через WebRTC SFU.
    НЕ заменяет AudioHandler.audio_callback() — голосовой чат комнаты
    (opuslib + VAD + JitterBuffer) работает параллельно и независимо.

    WebRTC DTX (Discontinuous Transmission) управляет тишиной вместо VAD:
    при молчании кодек автоматически снижает поток → дополнительный VAD-гейт
    здесь избыточен и только добавил бы задержку старта речи.

    Ленивая инициализация: asyncio.Queue и захват-поток создаются при первом
    вызове recv() из WebRTC asyncio-цикла. Это гарантирует, что loop-ссылка
    получена в правильном контексте без явной передачи в конструктор.

    Формат: s16, 48 000 Гц, моно, CHUNK_SIZE=960 сэмплов (20 мс).
    """

    kind = "audio"

    def __init__(self, device_name: str = None):
        super().__init__()
        self._device    = device_name
        self._running   = True
        # Инициализируются лениво при первом recv():
        self._queue:  "asyncio.Queue | None" = None
        self._loop:   "asyncio.AbstractEventLoop | None" = None
        self._thread: "threading.Thread | None" = None

    # ── Поток захвата ─────────────────────────────────────────────────────────

    def _capture_loop(self) -> None:
        """Синхронный поток sounddevice; данные отправляются в asyncio.Queue."""

        def _cb(indata: np.ndarray, frames: int, time_info, status) -> None:
            if not self._running or self._loop is None or self._queue is None:
                return
            # indata: (CHUNK_SIZE, 1), dtype=int16
            # run_coroutine_threadsafe — единственный thread-safe способ положить
            # данные из PortAudio-потока в asyncio.Queue WebRTC-цикла.
            try:
                asyncio.run_coroutine_threadsafe(
                    self._queue.put(indata.copy()), self._loop
                )
            except Exception:
                pass

        try:
            with sd.InputStream(
                device=self._device,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,   # 1 (моно)
                dtype='int16',
                blocksize=CHUNK_SIZE,
                callback=_cb,
            ):
                while self._running:
                    time.sleep(0.05)
        except Exception as e:
            print(f"[MicrophoneTrack] Ошибка захвата микрофона: {e}")

    # ── aiortc интерфейс ──────────────────────────────────────────────────────

    async def recv(self) -> av.AudioFrame:
        # Ленивая инициализация при первом вызове из WebRTC asyncio-цикла.
        if self._queue is None:
            self._loop   = asyncio.get_event_loop()
            self._queue  = asyncio.Queue(maxsize=10)
            self._thread = threading.Thread(
                target=self._capture_loop,
                daemon=True,
                name="webrtc-mic-capture",
            )
            self._thread.start()

        data = await self._queue.get()  # (CHUNK_SIZE, 1) int16

        # av.AudioFrame.from_ndarray ожидает shape (channels, samples).
        frame = av.AudioFrame.from_ndarray(
            data.T,      # (1, CHUNK_SIZE)
            format='s16',
            layout='mono',
        )
        frame.sample_rate = SAMPLE_RATE
        pts, time_base = await self.next_timestamp()
        frame.pts       = pts
        frame.time_base = time_base
        return frame

    def stop(self) -> None:
        """Останавливает захват микрофона. Вызывать при завершении стрима."""
        self._running = False


class SystemAudioTrack(AudioStreamTrack):
    """
    Захват системного звука через loopback_capture.dll → WebRTC.

    Используется стримером для передачи игрового звука зрителям через WebRTC SFU.
    Ленивая инициализация: захват стартует при первом recv() из WebRTC asyncio-цикла.
    Формат вывода: s16, 48 000 Гц, моно, CHUNK_SIZE=960 сэмплов (20 мс).
    """

    kind = "audio"

    def __init__(self, device_idx: int = None):
        super().__init__()
        self._device_idx = device_idx
        self._running    = True
        self._queue:   "asyncio.Queue | None"             = None
        self._loop:    "asyncio.AbstractEventLoop | None" = None
        self._capture: "StreamAudioCapture | None"        = None

    def _on_pcm_chunk(self, chunk: np.ndarray) -> None:
        """Вызывается StreamAudioCapture на каждый готовый 20-мс float32-фрейм."""
        if not self._running or self._loop is None or self._queue is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._queue.put(chunk), self._loop,
            )
        except Exception:
            pass

    # ── aiortc интерфейс ──────────────────────────────────────────────────────

    async def recv(self) -> av.AudioFrame:
        # Ленивая инициализация при первом вызове из WebRTC asyncio-цикла.
        if self._queue is None:
            self._loop    = asyncio.get_event_loop()
            self._queue   = asyncio.Queue(maxsize=10)
            self._capture = StreamAudioCapture(pcm_callback=self._on_pcm_chunk)
            self._capture.start(self._device_idx)
            print("[SystemAudioTrack] Захват системного звука запущен")

        data = await self._queue.get()  # float32 mono (CHUNK_SIZE,)

        # float32 [-1.0, 1.0] → int16 для av.AudioFrame.
        # np.clip inline — защита от пиков (WASAPI иногда выдаёт > 1.0).
        pcm_int16 = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)

        frame = av.AudioFrame.from_ndarray(
            pcm_int16.reshape(1, -1),   # (channels=1, samples=CHUNK_SIZE)
            format='s16',
            layout='mono',
        )
        frame.sample_rate = SAMPLE_RATE
        pts, time_base = await self.next_timestamp()
        frame.pts       = pts
        frame.time_base = time_base
        return frame

    def stop(self) -> None:
        """Останавливает захват системного звука. Вызывать при завершении стрима."""
        self._running = False
        if self._capture is not None:
            self._capture.stop()
            self._capture = None


# ─────────────────────────────────────────────────────────────────────────────

class JitterBuffer:
    def __init__(self, target_delay=4):
        self.buffer = []
        self.target_delay = target_delay
        self.last_seq = -1
        self._lock = threading.Lock()
        self.is_buffering = True
        self.max_size = 50

    def add(self, seq, data):
        with self._lock:
            if seq <= self.last_seq and self.last_seq != -1:
                return
            heapq.heappush(self.buffer, (seq, data))
            if len(self.buffer) > self.max_size:
                # FIX: дропаем НАИБОЛЬШИЙ seq (самый новый), а не наименьший.
                # heappop() на min-heap дропает наименьший — то есть именно тот
                # пакет, который должен играть следующим. Это гарантированный треск.
                # Правильно: убираем самый новый пакет — он пришёл из сети раньше
                # времени и буфер слишком большой. Используем nlargest+remove.
                # O(n) — но вызывается крайне редко (только при шторме пакетов).
                largest = max(self.buffer, key=lambda x: x[0])
                self.buffer.remove(largest)
                heapq.heapify(self.buffer)

    def get(self):
        with self._lock:
            if not self.buffer:
                self.is_buffering = True
                return None

            if self.is_buffering:
                if len(self.buffer) >= self.target_delay:
                    self.is_buffering = False
                else:
                    return None

            seq, data = heapq.heappop(self.buffer)
            self.last_seq = seq
            return data


class RemoteUser:
    def __init__(self, uid):
        self.uid = uid
        self.jitter_buffer = JitterBuffer()
        self.decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
        self.last_packet_time = 0
        self.volume = 1.0
        self.is_locally_muted = False
        # volume_zero=True когда vol==0.0 (ползунок в 0).
        # Отдельный флаг — не конфликтует с кнопкой is_locally_muted.
        # audio_callback использует его чтобы пропустить Opus-decode (экономия CPU).
        # UI использует его чтобы показать ban-иконку, как при заглушении.
        self.volume_zero = False
        self.remote_muted = False
        self.remote_deafened = False


class AudioHandler(QObject):
    volume_level_signal = pyqtSignal(int)
    status_changed = pyqtSignal(bool, bool)
    whisper_received = pyqtSignal(int)
    whisper_ended = pyqtSignal()
    # Испускается когда ползунок громкости пользователя достигает/покидает 0.
    # (uid, is_zero) — UI показывает ban-иконку при is_zero=True,
    # точно так же как при нажатии кнопки «Заглушить».
    user_volume_zero = pyqtSignal(int, bool)

    def _apply_anonymous_voice_effect(self, s: np.ndarray, state: dict) -> np.ndarray:
        """
        Эффект «анонимного голоса» (Dark TV Interview).
        Использует Vectorized Dual-Tap Delay Line для pitch-shift без артефактов.

        state — per-uid словарь {'history', 'phase', 'lp_zi', 'buf'}.
        Разделение состояний по uid позволяет одновременно обрабатывать
        нескольких шептунов без взаимного наложения и phase-артефактов.
        """
        N = len(s)
        max_delay = 1440  # 30 мс при 48kHz — оптимальный размер окна для голоса
        speed = 2.0 ** (-4.0 / 12.0)  # -4 полутона
        rate = 1.0 - speed  # Скорость накопления задержки

        # 1. Склеиваем историю и текущий фрейм в предаллоцированный буфер.
        # Избегаем np.concatenate (аллокация ~12 KB каждые 20 мс в hot path).
        # state['history'] = 2048 сэмплов при «тёплом» старте содержит реальный
        # сигнал, поэтому pitch-shifter сразу читает данные, не ноль → нет click.
        H = 2048
        history = state['history']
        buf     = state['buf']         # предаллоц. буфер размером H + CHUNK_SIZE
        buf[:H]      = history
        buf[H:H + N] = s
        buf_view = buf[:H + N]

        # 2. Генерируем фазы для двух читающих "головок" (0.0 ... 1.0)
        phases = state['phase'] + np.arange(N) * rate / max_delay
        state['phase'] = float((phases[-1] + rate / max_delay) % 1.0)

        p1 = phases % 1.0
        p2 = (phases + 0.5) % 1.0

        # Задержка в сэмплах
        d1 = p1 * max_delay
        d2 = p2 * max_delay

        # 3. Индексы чтения (относительно начала массива buf_view)
        base_idx = H + np.arange(N)
        r1 = base_idx - d1
        r2 = base_idx - d2

        # 4. Линейная интерполяция для плавности
        i1_floor = np.floor(r1).astype(np.int32)
        i2_floor = np.floor(r2).astype(np.int32)

        # Безопасный +1 индекс
        buf_last = H + N - 1
        i1_ceil = np.clip(i1_floor + 1, 0, buf_last)
        i2_ceil = np.clip(i2_floor + 1, 0, buf_last)

        frac_1 = r1 - i1_floor
        frac_2 = r2 - i2_floor

        val_1 = buf_view[i1_floor] * (1.0 - frac_1) + buf_view[i1_ceil] * frac_1
        val_2 = buf_view[i2_floor] * (1.0 - frac_2) + buf_view[i2_ceil] * frac_2

        # 5. Кроссфейд (окно Ханна) для устранения щелчков
        fade_1 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p1)
        fade_2 = 0.5 - 0.5 * np.cos(2.0 * np.pi * p2)

        shifted = val_1 * fade_1 + val_2 * fade_2

        # 6. Обновляем историю для следующего фрейма (in-place: нет аллокации)
        # buf_view[-H:] == buf_view[N:] — последние H сэмплов окна истории
        history[:] = buf_view[N:]

        # 7. LP-фильтр (4 кГц) для "тёмного" окраса (скрывает артефакты формант)
        out, state['lp_zi'] = sosfilt(self._anon_lp_sos, shifted, zi=state['lp_zi'])

        # 8. Мягкая нормализация пика
        peak = np.max(np.abs(out))
        if peak > 0.9:
            out *= 0.9 / peak

        return out.astype(np.float32)

    def __init__(self):
        super().__init__()

        self.settings = QSettings("MyVoiceChat", "UserVolumes")
        self.global_settings = QSettings("MyVoiceChat", "GlobalSettings")
        self.uid_to_ip = {}
        self.pending_volumes = {}
        self.remote_users = {}
        self.users_lock = threading.Lock()

        self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION)

        saved_bitrate = int(self.global_settings.value("audio_bitrate", DEFAULT_BITRATE))
        self.encoder.bitrate = saved_bitrate
        self.encoder.complexity = 5

        # --- Секция шумоподавления ---
        self.denoiser = None      # Для RNNoise
        self.dfn_engine = None    # Для DeepFilterNet
        self.dfn_available = False  # Инициализируем до try — на случай непредвиденного исключения

        # nr_mode: 0=выкл, 1=RNNoise, 2=DeepFilterNet
        # По умолчанию 0 — пользователь сам выбирает в настройках.
        self.nr_mode = int(self.global_settings.value("audio/nr_mode", 0))

        # 1. Сначала пробуем инициализировать DeepFilterNet
        try:
            self.dfn_engine = DeepFilterEngine()
            self.dfn_available = True
            print("[Audio] DeepFilterNet3 инициализирован успешно.")
        except Exception as e:
            self.dfn_available = False
            print(f"[Audio] DFN не загружен: {e}")

        # 2. Параллельно держим RNNoise как запасной вариант (fallback)
        if PYRNNOISE_AVAILABLE:
            try:
                self.denoiser = RNNoise(sample_rate=SAMPLE_RATE)
                print("[Audio] RNNoise инициализирован.")
            except Exception as e:
                print(f"[Audio] Ошибка RNNoise: {e}")

        self.incoming_packets = queue.Queue(maxsize=500)
        self.send_queue = queue.Queue(maxsize=100)

        # ── Внутренний микшер UI-звуков ──────────────────────────────────────
        # Системные уведомления, Soundboard и Nudge подаются сюда вместо sd.play().
        # audio_callback читает список и подмешивает фреймы в mix_buffer прямо в
        # уже открытый PortAudio-поток — никаких новых WASAPI-устройств не открывается.
        self._local_sounds: list = []
        self._local_sounds_lock = threading.Lock()

        # --- Стрим-аудио (WebRTC) ---
        # Воспроизведение стрим-аудио на стороне зрителя теперь обрабатывается
        # через WebRTC (RTCPeerConnection + AudioStreamTrack).
        # Следующие атрибуты удалены: incoming_stream_packets, stream_remote_users,
        # stream_users_lock, _audio_stream_users_snapshot, _stream_pkt_thread,
        # _sv_sequence, _lb_play_counter, _stream_audio_sending.
        # Громкость стрима для зрителя (0.0–2.0) — управляется через оверлей VideoWindow.
        self._is_running = threading.Event()
        self._is_muted = threading.Event()
        self._is_deafened = threading.Event()

        self.mix_buffer = np.zeros(CHUNK_SIZE, dtype=np.float32)
        saved_vad_slider = int(self.global_settings.value("vad_threshold_slider", 5))
        self.vad_threshold = saved_vad_slider / 1000.0
        self.vad_hangover = 0.4
        self.last_voice_time = 0
        self.my_uid = 0
        self.my_sequence = 0

        # ── Шёпот (приватная передача одному пользователю) ─────────────────
        # Пока whisper_target_uid != 0 — голос идёт только этому uid,
        # остальные участники комнаты отправителя не слышат.
        self.whisper_target_uid: int = 0
        self._whisper_sequence: int = 0

        # ── IIR-фильтр для whisper-эффекта (4-й порядок Butterworth LP, fc=1200 Гц) ──
        # SOS-матрица предвычислена как модульная константа _WLP_SOS (один раз при импорте).
        # Начальные условия zi хранят состояние между фреймами.
        # Сброс zi производится в start_whisper().
        self._wlp_sos = _WLP_SOS
        self._wlp_zi  = sosfilt_zi(self._wlp_sos).astype(np.float64)

        # ── LP-фильтр для эффекта анонимного голоса (fc=4000 Гц) ──────────────
        # Мягче чем whisper LP (1200 Гц): сохраняет согласные и зону присутствия.
        # SOS-матрица предвычислена как модульная константа _ANON_LP_SOS.
        self._anon_lp_sos = _ANON_LP_SOS

        # ── Whisper effect: per-uid состояния ───────────────────────────────────
        # Ключ: uid шептуна. Значение: dict с полями:
        #   'history' — np.ndarray(2048, float32): буфер предыстории pitch-shifter
        #   'phase'   — float: текущая фаза читающей головки
        #   'lp_zi'   — np.ndarray(n_sec, 2, float64): состояние LP-фильтра
        #   'buf'     — np.ndarray(2048+CHUNK_SIZE, float32): предаллоц. рабочий буфер
        #
        # Создаётся лениво при первом пакете шептуна с «тёплым» стартом:
        # history заполняется реальным сигналом (не нулями) → питч-шифтер сразу
        # читает данные из обеих головок без перехода ноль→сигнал → нет click/треск.
        #
        # Поддержка 2+ одновременных шептунов: каждый uid имеет независимое
        # состояние, смешиваются через общий mix_buffer без взаимных артефактов.
        self._whisper_states: dict = {}    # uid → state dict (см. выше)
        self._active_whispers: dict = {}   # uid → float (последний timestamp пакета)

        # Backward compat для UI-сигнала: последний шептун и его время
        self._whisper_in_uid: int = 0
        self._whisper_in_ts: float = 0.0

        # Флаг для start_whisper() — sender-side legacy (сбрасывает _wlp_zi отправителя)
        # На стороне получателя не используется (заменён ленивым созданием per-uid state).
        self._whisper_effect_reset: bool = False
        # Отдельный счётчик для FLAG_STREAM_VOICES удалён (WebRTC заменяет UDP стрим-аудио).
        # _lb_play_counter удалён (нет UDP loopback воспроизведения).
        # FIX #3: deque(maxlen=5) вместо list.
        # vad_pre_buffer.pop(0) на list — O(n): сдвигает все элементы влево.
        # deque.popleft() — O(1), что важно для audio_callback hot path.
        # maxlen=5 заменяет ручную проверку `if len > 5: pop(0)`.
        self.vad_pre_buffer = deque(maxlen=5)
        self.was_talking = False
        self.stream = None
        # Ссылки на рабочие потоки — нужны для корректного join() в stop().
        # Без явного join() повторные вызовы start() (переподключение, смена
        # устройства) накапливают «зомби»-потоки.
        self._pkt_thread: threading.Thread | None = None

        # -------------------------------------------------------------------
        # FIX #1: Copy-on-Write снимки для audio_callback.
        #
        # Проблема: audio_callback — реалтайм-поток с дедлайном 20 мс.
        # Захват users_lock / stream_users_lock внутри callback'а блокировал
        # его на время работы _packet_processor_loop (удерживает тот же лок).
        # Результат: пропуск дедлайна → слышимые щелчки и глитчи в аудио.
        #
        # Решение: _packet_processor_loop берёт снимок dict после каждого
        # изменения remote_users (внутри того же with users_lock).
        # audio_callback читает _audio_users_snapshot БЕЗ лока:
        #   - Присваивание ссылки dict GIL-атомарно → нет torn read.
        #   - Снимок «отстаёт» максимум на 1 пакет (~20 мс) — для аудио незаметно.
        #   - RemoteUser.jitter_buffer имеет собственный лок → thread-safe.
        #   - RemoteUser.volume / .is_locally_muted — простые примитивы, GIL-safe.
        # -------------------------------------------------------------------
        self._audio_users_snapshot: dict = {}

    def set_bitrate(self, bitrate_kbps):
        bitrate_bps = int(bitrate_kbps) * 1000
        try:
            with self.users_lock:
                if hasattr(self, 'encoder'):
                    self.encoder.bitrate = bitrate_bps
                    self.global_settings.setValue("audio_bitrate", bitrate_bps)
                    print(f"[Audio] Bitrate changed to {bitrate_kbps} kbps")
        except Exception as e:
            print(f"[Audio] Error setting bitrate: {e}")

    def play_internal_sound(self, data: np.ndarray, sr: int, vol: float = 1.0):
        """
        Воспроизводит звук через уже открытый PortAudio-поток без аллокации
        нового WASAPI-устройства. Данные подмешиваются в mix_buffer прямо внутри
        audio_callback — нулевой риск WASAPI-перебалансировки и треска.

        data: float32 numpy-массив (моно или стерео)
        sr  : частота дискретизации источника (будет ресемплирован в 48kHz)
        vol : коэффициент громкости (квадратичная кривая уже применена снаружи)
        """
        try:
            data = np.asarray(data, dtype=np.float32)
            # Стерео → моно
            if data.ndim > 1 and data.shape[1] > 1:
                data = np.mean(data, axis=1)
            elif data.ndim > 1:
                data = data[:, 0]

            # Ресемплинг только если нужен (np.interp — достаточно для кратких UI-звуков)
            if sr != SAMPLE_RATE:
                target_len = int(round(len(data) * SAMPLE_RATE / sr))
                if target_len > 0:
                    x_old = np.linspace(0.0, 1.0, len(data), dtype=np.float32)
                    x_new = np.linspace(0.0, 1.0, target_len, dtype=np.float32)
                    data = np.interp(x_new, x_old, data).astype(np.float32)

            with self._local_sounds_lock:
                self._local_sounds.append({'data': data, 'pos': 0, 'vol': float(vol)})
        except Exception as e:
            print(f"[Audio] play_internal_sound error: {e}")

    def set_nr_mode(self, mode: int):
        """
        Устанавливает режим шумоподавления (мгновенно, без перезапуска аудио):
          0 = выкл
          1 = RNNoise
          2 = DeepFilterNet
        """
        self.nr_mode = int(mode)
        self.global_settings.setValue("audio/nr_mode", self.nr_mode)
        labels = {0: "выкл", 1: "RNNoise", 2: "DeepFilterNet"}
        print(f"[Audio] NR mode → {labels.get(self.nr_mode, "?")} ({self.nr_mode})")

    def set_vad_threshold(self, slider_val: int):
        threshold = max(1, min(50, slider_val)) / 1000.0
        self.vad_threshold = threshold
        self.global_settings.setValue("vad_threshold_slider", slider_val)
        print(f"[Audio] VAD threshold set to {threshold:.4f} (slider={slider_val})")

    def find_device_index_by_name(self, name, is_input=True):
        direction = "INPUT" if is_input else "OUTPUT"
        if not name:
            print(f"[DEBUG] find_device_index_by_name: {direction} name=None → вернём None (дефолт системы)", flush=True)
            return None
        devices = sd.query_devices()
        print(f"[DEBUG] find_device_index_by_name: ищем {direction} '{name}'", flush=True)
        for i, d in enumerate(devices):
            try:
                api_name = sd.query_hostapis(d['hostapi'])['name']
                full_name = f"{d['name']} ({api_name})"
                if full_name.strip() == name.strip():
                    if is_input and d['max_input_channels'] > 0:
                        print(f"[DEBUG] find_device_index_by_name: найдено {direction} idx={i} '{full_name}'", flush=True)
                        return i
                    if not is_input and d['max_output_channels'] > 0:
                        print(f"[DEBUG] find_device_index_by_name: найдено {direction} idx={i} '{full_name}'", flush=True)
                        return i
            except:
                continue
        print(f"[DEBUG] find_device_index_by_name: {direction} '{name}' НЕ НАЙДЕНО → None (дефолт)", flush=True)
        return None

    def start(self, input_name=None, output_name=None):
        print(f"[DEBUG] AudioHandler.start: BEGIN — input_name={input_name!r}, output_name={output_name!r}", flush=True)
        if self.my_uid == 0:
            print("[DEBUG] AudioHandler.start: my_uid==0, выход", flush=True)
            return
        print("[DEBUG] AudioHandler.start: вызов stop()...", flush=True)
        self.stop()
        time.sleep(0.1)
        print("[DEBUG] AudioHandler.start: stop() выполнен", flush=True)

        # FIX ROBOT-VOICE: сбрасываем Opus-декодеры и JitterBuffer'ы всех удалённых
        # пользователей после остановки стрима. Opus — CELP-кодек с предсказательным
        # состоянием (LPC). После паузы ≥100ms декодер ожидает следующий фрейм через
        # ровно 20ms; получив пакеты после реального перерыва без PLC-вызова он
        # выдаёт discontinuity → робовойс/артефакты на 1-2 фрейма.
        # Сброс декодера к начальному состоянию + очистка JitterBuffer устраняет это.
        with self.users_lock:
            for user in self.remote_users.values():
                user.decoder       = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
                user.jitter_buffer = JitterBuffer()
            # Обновляем COW-снимок — audio_callback не должен видеть старые декодеры
            self._audio_users_snapshot = dict(self.remote_users)

        print("[DEBUG] AudioHandler.start: поиск устройств...", flush=True)
        in_idx = self.find_device_index_by_name(input_name, True)
        out_idx = self.find_device_index_by_name(output_name, False)
        print(f"[DEBUG] AudioHandler.start: in_idx={in_idx}, out_idx={out_idx}", flush=True)

        self._is_running.set()
        try:
            print("[DEBUG] AudioHandler.start: создание sd.Stream...", flush=True)
            self.stream = sd.Stream(
                device=(in_idx, out_idx),
                samplerate=SAMPLE_RATE, blocksize=CHUNK_SIZE,
                dtype='float32', channels=CHANNELS,
                callback=self.audio_callback
            )
            print("[DEBUG] AudioHandler.start: sd.Stream создан, вызов stream.start()...", flush=True)
            self.stream.start()
            print("[DEBUG] AudioHandler.start: stream.start() выполнен", flush=True)
            self._pkt_thread = threading.Thread(target=self._packet_processor_loop, daemon=True)
            self._pkt_thread.start()
            print("[DEBUG] AudioHandler.start: рабочий поток запущен — DONE", flush=True)
        except Exception as e:
            import traceback
            print(f"[DEBUG] AudioHandler.start: EXCEPTION:\n{traceback.format_exc()}", flush=True)
            self._is_running.clear()

    def stop(self):
        self._is_running.clear()
        # Дожидаемся завершения рабочего потока — иначе повторный start()
        # создаст дублирующие потоки (утечка памяти и CPU)
        t = getattr(self, '_pkt_thread', None)
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
        self._pkt_thread = None
        if hasattr(self, 'stream') and self.stream:
            try:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            except:
                pass

    def cleanup_users(self, active_uids):
        """ Очистка памяти от отключившихся юзеров """
        with self.users_lock:
            for uid in list(self.remote_users.keys()):
                if uid not in active_uids:
                    del self.remote_users[uid]

            for uid in list(self.uid_to_ip.keys()):
                if uid not in active_uids:
                    del self.uid_to_ip[uid]
            for uid in list(self.pending_volumes.keys()):
                if uid not in active_uids:
                    del self.pending_volumes[uid]

            # FIX #1: обновляем COW-снимок после удаления пользователей
            self._audio_users_snapshot = dict(self.remote_users)
        # stream_remote_users удалён: стрим-аудио теперь через WebRTC (RTCPeerConnection).

    def _packet_processor_loop(self):
        while self._is_running.is_set():
            try:
                packet_data = self.incoming_packets.get(timeout=0.1)
                uid, seq, data, flags = packet_data
                if uid == self.my_uid: continue

                with self.users_lock:
                    if uid not in self.remote_users:
                        self.remote_users[uid] = RemoteUser(uid)
                        # Исправление 1.2: убрано чтение QSettings(диск) из high-priority ловушки
                        if uid in self.pending_volumes:
                            val = self.pending_volumes.pop(uid)
                        else:
                            val = 1.0
                        val = float(val)
                        self.remote_users[uid].volume = val
                        self.remote_users[uid].volume_zero = (val == 0.0)

                    user = self.remote_users[uid]
                    user.remote_muted = bool(flags & 1)
                    user.remote_deafened = bool(flags & 2)

                    if data:
                        user.jitter_buffer.add(seq, data)
                        user.last_packet_time = time.perf_counter()  # FIX: perf_counter точнее time.time() на Windows (15.6ms vs мкс)

                    # FIX #1: обновляем COW-снимок внутри лока — согласованное состояние.
                    # dict() копирует только ссылки (не RemoteUser объекты) — это быстро.
                    # audio_callback читает снимок без лока, опираясь на GIL-атомарность
                    # присваивания ссылки.
                    self._audio_users_snapshot = dict(self.remote_users)

            except queue.Empty:
                continue
            except Exception:
                pass

    def audio_callback(self, indata, outdata, frames, time_info, status):
        # Логируем только первый вызов — подтверждает что callback запустился
        if not getattr(self, '_cb_first_logged', False):
            self._cb_first_logged = True
            print("[DEBUG] audio_callback: ПЕРВЫЙ ВЫЗОВ — PortAudio callback работает", flush=True)
        if status:
            print(f"[DEBUG] audio_callback: status={status}", flush=True)

        if not self._is_running.is_set():
            outdata.fill(0)
            return

        curr_time = time.perf_counter()  # FIX: высокоточный таймер Windows (мкс вместо 15.6ms у time.time)
        raw_input = indata.flatten()
        denoised_float = raw_input

        # nr_mode: 0=выкл, 1=RNNoise, 2=DFN — читаем self (не QSettings каждые 20мс)
        _nr = self.nr_mode
        if _nr == 2 and self.dfn_engine:
            try:
                denoised_float = self.dfn_engine.process(denoised_float)
            except Exception:
                pass
        elif _nr == 1 and self.denoiser:
            try:
                pcm_int16 = (denoised_float * 32767).astype(np.int16)
                processed = [f for p, f in self.denoiser.denoise_chunk(pcm_int16)]
                if processed:
                    denoised_float = (
                        processed[0].astype(np.float32) / 32767.0
                        if len(processed) == 1
                        else np.concatenate(processed).astype(np.float32) / 32767.0
                    )
            except Exception:
                pass

        # ── Pre-encode input normalization ──────────────────────────────────
        # Если denoised_float содержит пики > 1.0 (микрофонный буст Windows,
        # RNNoise иногда выходит за ±1.0, некоторые ASIO-драйверы) →
        # умножение на 32767 даёт значения > INT16_MAX → wraparound в
        # отрицательную зону → жёсткий треск именно при громком голосе
        # («на пределе микрофона»). Soft-limit здесь — единственная защита.
        # Используем in-place операцию: аллокаций нет.
        _in_peak = np.max(np.abs(denoised_float))
        if _in_peak > 0.98:
            # Нормализуем к 0.98 — оставляем 2% запас до INT16_MAX
            denoised_float = denoised_float * (0.98 / _in_peak)

        rms = np.sqrt(np.mean(denoised_float ** 2))
        self.volume_level_signal.emit(int(min(rms * 1000, 100)))

        is_talking = rms > self.vad_threshold or (curr_time - self.last_voice_time < self.vad_hangover)
        if rms > self.vad_threshold: self.last_voice_time = curr_time

        if self.my_uid != 0:
            mute_flag = 1 if self._is_muted.is_set() else 0
            deaf_flag = 2 if self._is_deafened.is_set() else 0
            flags = mute_flag | deaf_flag

            # Читаем whisper_target_uid ДО проверки мута — шёпот обходит мут.
            # Это атомарное чтение int (GIL-safe).
            whisper_uid = self.whisper_target_uid

            try:
                if is_talking and whisper_uid != 0:
                    # ── РЕЖИМ ШЁПОТА ─────────────────────────────────────────
                    # Шёпот отправляется НЕЗАВИСИМО от состояния мута.
                    # Мут означает «не говорить в комнату» — шёпот приватный
                    # и не нарушает намерение пользователя заглушить себя от
                    # остальных. PTT-кнопка шёпота — явное действие отправить.
                    #
                    # Нормальный аудио-пакет в комнату НЕ кладём в очередь →
                    # остальные участники не слышат отправителя в этот момент.
                    pcm_to_encode = (denoised_float * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm_to_encode, CHUNK_SIZE)
                    # FIX: убираем лишний my_sequence += 1.
                    # В режиме шёпота пакет в комнату НЕ отправляется — my_sequence
                    # не должен расти. stop_whisper() синхронизирует его с
                    # _whisper_sequence, так что разрыва seq при возврате не будет.
                    self._whisper_sequence += 1
                    w_flags = FLAG_WHISPER
                    w_header = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time,
                                           self._whisper_sequence, w_flags)
                    # Payload: [target_uid: 4 байта] + [opus]
                    w_payload = struct.pack('!I', whisper_uid) + encoded
                    try:
                        self.send_queue.put_nowait(w_header + w_payload)
                    except Exception:
                        pass

                elif is_talking and not self._is_muted.is_set():
                    # ── ОБЫЧНЫЙ РЕЖИМ: пакет в комнату ───────────────────────
                    pcm_to_encode = (denoised_float * 32767).astype(np.int16).tobytes()
                    encoded = self.encoder.encode(pcm_to_encode, CHUNK_SIZE)
                    self.my_sequence += 1
                    packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time, self.my_sequence, flags) + encoded

                    if not self.was_talking:
                        # FIX #3: deque.popleft() — O(1) вместо list.pop(0) — O(n)
                        while self.vad_pre_buffer:
                            try:
                                self.send_queue.put_nowait(self.vad_pre_buffer.popleft())
                            except:
                                pass
                        self.was_talking = True
                    self.send_queue.put_nowait(packet)
                    # Стрим-аудио микрофона теперь передаётся через WebRTC (MicrophoneTrack).
                    # UDP FLAG_STREAM_AUDIO для микрофона удалён.

                else:
                    # Не говорим (или мут без шёпота) — сбрасываем was_talking,
                    # пополняем pre_buffer для следующего старта речи.
                    self.was_talking = False
                    if not is_talking and whisper_uid == 0:
                        empty_packet = UDP_HEADER_STRUCT.pack(self.my_uid, curr_time, 0, flags)
                        # FIX #3: deque(maxlen=5) — автоматически вытесняет старые
                        # элементы при переполнении, ручная проверка len > 5 не нужна.
                        self.vad_pre_buffer.append(empty_packet)
            except:
                pass

        self.mix_buffer.fill(0)
        if not self._is_deafened.is_set():
            # ── N-speaker headroom ────────────────────────────────────────────
            # Проблема: 3+ участников говорят одновременно → сумма амплитуд
            # до 3.0–4.0 → даже soft limiter давит сигнал в 3× → все тихие
            # и «мутные». Это не дисторшн, но воспринимается как «плохое качество».
            #
            # Решение: заранее вычисляем gain для каждого активного спикера
            # по формуле sqrt(2) / sqrt(N_active). При N=1: gain=1.0 (без изменений).
            # При N=2: gain=1.0 (пара = норма). При N=3: gain=0.82. При N=4: gain=0.71.
            # Это стандартный incoherent sources scaling — суммарная RMS остаётся
            # постоянной независимо от числа говорящих.
            #
            # Считаем «активных»: last_packet < 1.5с AND не заглушен AND volume > 0.
            # Не блокируемся — читаем уже готовый COW-снимок без лока.
            # ── Подсчёт активных голосовых спикеров (окно 0.4с) ─────────────────
            # Было 1.5с → «фантомные» спикеры: человек сказал одно слово и
            # полторы секунды тянул gain вниз для всей комнаты. При плохом
            # микрофоне 3-го клиента (постоянный шум = VAD открыт) — он вечно
            # оставался в _n_active и давил громкость всем остальным.
            # 0.4с = комфортный хвост VAD без долгих фантомов.
            #
            # ВАЖНО: порог для воспроизведения пакетов остаётся 1.5с ниже —
            # не трогаем, чтобы не обрезать речь в конце фразы.
            _n_active = sum(
                1 for u in self._audio_users_snapshot.values()
                if (curr_time - u.last_packet_time < 0.4
                    and not u.is_locally_muted
                    and not u.volume_zero)
            )
            # Loopback/stream аудио намеренно НЕ включаем в _n_active.
            # Игровой звук — фоновый поток, а не «конкурирующий голос».
            # Раньше активный стрим постоянно добавлял +1 к N → gain у всех
            # зрителей падал на 18% даже когда никто не говорил.
            # Soft limiter (ниже) надёжно защищает от перегруза при наложении
            # голоса и игрового звука без ручного снижения gain.

            # ── Смягчённая формула headroom ──────────────────────────────────
            # Было: sqrt(2)/sqrt(N) → при N=3 gain=0.816 (внезапные -18%
            # в момент подключения 3-го собеседника — хорошо слышимый провал).
            # Теперь: ≤2 источников → 100%, далее -10% за каждого, пол 0.75.
            # Soft limiter обработает редкие случаи когда все кричат одновременно.
            if _n_active <= 2:
                _speaker_gain = 1.0
            else:
                _speaker_gain = max(0.75, 1.0 - 0.1 * (_n_active - 2))

            # FIX #1: читаем COW-снимок БЕЗ лока.
            # _packet_processor_loop обновляет _audio_users_snapshot внутри
            # users_lock после каждого изменения. Снимок «отстаёт» максимум
            # на 1 пакет (~20 мс) — для аудиомикширования незаметно.
            # JitterBuffer.get() имеет собственный внутренний лок — thread-safe.
            for uid, user in self._audio_users_snapshot.items():
                if curr_time - user.last_packet_time < 1.5:
                    data = user.jitter_buffer.get()
                    if not user.is_locally_muted and not user.volume_zero:
                        try:
                            if data:
                                decoded = user.decoder.decode(data, CHUNK_SIZE)
                                s = np.frombuffer(decoded, dtype=np.int16).astype(np.float32) / 32767.0

                                # ── Per-uid whisper effect ───────────────────────────────────
                                # _active_whispers[uid] обновляется в add_incoming_whisper_packet
                                # на каждый входящий пакет шёпота (~50 раз/сек).
                                #
                                # «Тёплый старт» при первом пакете (uid не в _whisper_states):
                                # history заполняем текущим фреймом s (повторённым до 2048).
                                # Обе читающие головки pitch-shifter'а сразу попадают в реальный
                                # сигнал — переход ноль→сигнал отсутствует → нет треска/click.
                                #
                                # LP-фильтр: нулевые начальные условия оптимальны для голосового
                                # сигнала (mean ≈ 0); sosfilt_zi(sos)*0 == zeros.
                                #
                                # Два шептуна одновременно: каждый uid имеет свой state dict →
                                # независимые history/phase/lp_zi/buf → нет взаимных артефактов →
                                # оба смешиваются в mix_buffer без потерь.
                                _w_ts = self._active_whispers.get(uid, 0.0)
                                if _w_ts and (curr_time - _w_ts) < 2.0:
                                    if uid not in self._whisper_states:
                                        # Ленивое создание: тёплый старт с реальным сигналом
                                        _warm_history = np.resize(
                                            s.astype(np.float32), 2048).copy()
                                        self._whisper_states[uid] = {
                                            'history': _warm_history,
                                            'phase':   0.0,
                                            'lp_zi':   np.zeros(
                                                (self._anon_lp_sos.shape[0], 2),
                                                dtype=np.float64),
                                            'buf':     np.zeros(
                                                2048 + CHUNK_SIZE, dtype=np.float32),
                                        }
                                    s = self._apply_anonymous_voice_effect(
                                        s, self._whisper_states[uid])
                                else:
                                    # Шептун неактивен: освобождаем state (нет утечки памяти)
                                    self._active_whispers.pop(uid, None)
                                    self._whisper_states.pop(uid, None)

                                self.mix_buffer += s * (user.volume * _speaker_gain)
                                # Mix-Minus (FLAG_STREAM_VOICES) через UDP удалён.
                                # Голоса участников комнаты для зрителей стрима будут
                                # реализованы через WebRTC в следующей итерации.
                            else:
                                # FIX PLC: пакет не пришёл вовремя (jitter/потеря сети).
                                # Вызываем Opus PLC (Packet Loss Concealment) с data=None.
                                # Opus генерирует comfort noise и поддерживает внутреннее
                                # LPC-состояние предсказателя синхронизированным.
                                # Без этого вызова при следующем реальном пакете декодер
                                # «не знает» что был пропуск → выдаёт discontinuity → треск.
                                # Результат PLC в mix_buffer НЕ добавляем — тишина правильна
                                # когда пакет потерян, PLC нужен только для состояния декодера.
                                try:
                                    user.decoder.decode(None, CHUNK_SIZE)
                                except Exception:
                                    pass
                        except Exception:
                            pass

        # ── Внутренний микшер UI-звуков (уведомления, Soundboard, Nudge) ────────
        # Подмешиваем в уже заполненный mix_buffer — никаких sd.play() и
        # новых WASAPI-устройств. Lock гарантирует атомарность доступа к списку.
        with self._local_sounds_lock:
            active = []
            for snd in self._local_sounds:
                pos  = snd['pos']
                rem  = len(snd['data']) - pos
                take = min(CHUNK_SIZE, rem)
                if take > 0:
                    self.mix_buffer[:take] += snd['data'][pos:pos + take] * snd['vol']
                    snd['pos'] += take
                    if snd['pos'] < len(snd['data']):
                        active.append(snd)
            self._local_sounds = active

        # ── Математически чистый tanh soft-clipper ───────────────────────────
        # Заменяет старый пропорциональный лимитер (self.mix_buffer *= k).
        # Старая схема: один gain на весь кадр → резкая «ступенька» gain между
        # соседними кадрами → слышимый щелчок/треск при пиках.
        # tanh обрабатывает каждый семпл независимо, плавно загибая только те,
        # что вышли за limit. Результат — аналог лампового сатуратора без артефактов.
        _limit = 0.95
        _over  = np.abs(self.mix_buffer) > _limit
        if np.any(_over):
            _excess = np.abs(self.mix_buffer[_over]) - _limit
            self.mix_buffer[_over] = (
                np.sign(self.mix_buffer[_over])
                * (_limit + (1.0 - _limit) * np.tanh(_excess / (1.0 - _limit)))
            )
        # Safety-clip: float-погрешности после tanh
        np.clip(self.mix_buffer, -1.0, 1.0, out=self.mix_buffer)

        outdata[:] = self.mix_buffer.reshape(-1, 1)

    def register_ip_mapping(self, uid, ip_addr):
        if not ip_addr: return
        with self.users_lock:
            self.uid_to_ip[uid] = ip_addr
            saved_vol = self.settings.value(f"vol_ip_{ip_addr}", None)
            if saved_vol is not None:
                saved_vol = float(saved_vol)
                if uid in self.remote_users:
                    self.remote_users[uid].volume = saved_vol
                    self.remote_users[uid].volume_zero = (saved_vol == 0.0)
                else:
                    self.pending_volumes[uid] = saved_vol

    def set_user_volume(self, uid, vol):
        # FIX: Зажимаем в [0.0 … 10.0].
        # Слайдер 0-200 → экспоненциальная кривая → max = 10^((200-100)/100) = 10.0.
        # Старый клэмп min(2.0, ...) обрезал slider=200 до 2.0, которое обратно
        # конвертировалось в _vol_to_slider(2.0) = 130 → пользователь видел 130 вместо 200.
        vol = max(0.0, min(10.0, float(vol)))
        emit_zero_state = None  # None = состояние не изменилось

        with self.users_lock:
            if uid in self.remote_users:
                user = self.remote_users[uid]
                prev_zero = user.volume_zero
                user.volume = vol
                user.volume_zero = (vol == 0.0)
                if user.volume_zero != prev_zero:
                    emit_zero_state = user.volume_zero
                ip = self.uid_to_ip.get(uid)
                if ip:
                    self.settings.setValue(f"vol_ip_{ip}", vol)
                else:
                    # Fallback: сохраняем по uid если IP ещё не зарегистрирован.
                    # register_ip_mapping() перезапишет правильный ключ при получении IP.
                    self.settings.setValue(f"volume_{uid}", vol)

        # Эмитируем сигнал ВНЕ лока — не блокируем аудиопоток
        if emit_zero_state is not None:
            self.user_volume_zero.emit(uid, emit_zero_state)

    def toggle_user_mute(self, uid):
        with self.users_lock:
            if uid in self.remote_users:
                self.remote_users[uid].is_locally_muted = not self.remote_users[uid].is_locally_muted
                return self.remote_users[uid].is_locally_muted
        return False

    # ── Шёпот ────────────────────────────────────────────────────────────────

    def start_whisper(self, target_uid: int):
        """
        Начинает шёпот к конкретному пользователю.
        Пока активно — голос кодируется и отправляется только ему (FLAG_WHISPER).
        Нормальные аудио-пакеты в комнату НЕ отправляются, остальные не слышат.

        ВАЖНО: _whisper_sequence инициализируется от my_sequence, а НЕ от 0.
        JitterBuffer получателя уже видел seq из нормального потока (my_sequence).
        Сброс в 0 → все шёпот-пакеты отбрасывались бы как seq <= last_seq.
        """
        self.whisper_target_uid = target_uid
        self._whisper_sequence = self.my_sequence  # продолжаем seq без разрыва
        # Сбрасываем состояние sosfilt-фильтра чтобы шёпот каждого нового собеседника
        # начинался с чистого состояния (без «хвоста» от предыдущего шёпота).
        self._wlp_zi = sosfilt_zi(self._wlp_sos).astype(np.float64)
        # FIX race condition: сброс состояния фильтра через флаг, а не напрямую.
        # Прямой вызов _anon_history.fill(0) / _anon_phase=0 из UI-потока конкурирует
        # с audio_callback (PortAudio thread). numpy снимает GIL → torn read/write →
        # щелчки. Флаг — атомарный bool, audio_callback сбросит состояние сам.
        self._whisper_effect_reset = True
        print(f"[Audio] Whisper START → uid={target_uid}, seq_from={self._whisper_sequence}")

    def stop_whisper(self):
        """Останавливает шёпот, возвращает нормальную передачу в комнату.
        Синхронизируем my_sequence чтобы не было обратного прыжка seq."""
        # Переносим счётчик чтобы нормальные пакеты продолжили нумерацию
        # с того места, где остановился шёпот. Иначе получатели в комнате
        # увидят резкий откат seq и часть пакетов будет отброшена JitterBuffer.
        if self._whisper_sequence > self.my_sequence:
            self.my_sequence = self._whisper_sequence
        print(f"[Audio] Whisper STOP  (was → uid={self.whisper_target_uid}), seq_sync={self.my_sequence}")
        self.whisper_target_uid = 0

    def add_incoming_packet(self, uid, seq, data, flags=0):
        try:
            self.incoming_packets.put_nowait((uid, seq, data, flags))
        except:
            pass

    def add_incoming_whisper_packet(self, uid, seq, data):
        """
        Входящий шёпот (FLAG_WHISPER) от uid.

        Испускает whisper_received(uid) на КАЖДЫЙ пакет — это необходимо
        для корректной работы UI-таймера завершения шёпота (_whisper_end_timer).

        Почему раньше было неправильно:
          Сигнал испускался только при первом пакете или после паузы >1.5с.
          _whisper_end_timer (1500 мс, single-shot) перезапускался только тогда.
          Результат: через ~1.5с после начала шёпота таймер срабатывал и скрывал
          оверлей, хотя шептун всё ещё держал PTT-кнопку.

        Почему теперь правильно:
          Сигнал испускается на каждый пакет (~50/сек). MainWindow._on_whisper_received
          перезапускает таймер при каждом сигнале, но обновляет текст/показывает
          оверлей только при смене отправителя (uid != текущий) — без визуального
          мерцания. Пока идут пакеты — таймер никогда не истекает.
        """
        now = time.perf_counter()  # FIX: perf_counter точнее time.time() на Windows (15.6ms vs мкс)

        # Обновляем реестр активных шептунов — dict lookup O(1), GIL-safe.
        # audio_callback читает self._active_whispers[uid] без лока:
        # dict.__setitem__ с существующим ключом (update float значения) GIL-атомарно.
        # При новом uid — новый ключ; audio_callback увидит его на следующем фрейме
        # (max 20 мс опоздания), что приемлемо.
        self._active_whispers[uid] = now

        # Backward compat: UI-сигнал и таймер скрытия оверлея ориентируются на
        # _whisper_in_uid / _whisper_in_ts. Показываем последнего шептуна.
        self._whisper_in_uid = uid
        self._whisper_in_ts  = now

        # Эмитим на каждый пакет — UI-таймер перезапускается, оверлей не гаснет.
        self.whisper_received.emit(uid)

        self.add_incoming_packet(uid, seq, data, 0)

    @property
    def is_muted(self):
        return self._is_muted.is_set()

    @is_muted.setter
    def is_muted(self, value):
        if value:
            self._is_muted.set()
        else:
            self._is_muted.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)

    @property
    def is_deafened(self):
        return self._is_deafened.is_set()

    @is_deafened.setter
    def is_deafened(self, value):
        if value:
            self._is_deafened.set()
        else:
            self._is_deafened.clear()
        self.status_changed.emit(self.is_muted, self.is_deafened)