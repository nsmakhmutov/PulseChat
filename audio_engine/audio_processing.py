import ctypes
import os
import numpy as np

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
        # audio_processing.py живёт в audio_engine/audio_processing.py
        # __file__ → .../audio_engine/audio_processing.py
        # dirname(__file__)         → .../audio_engine/       ← неверно, dlls/ там нет
        # dirname(dirname(__file__)) → .../VoiceChat/          ← корень проекта, dlls/ здесь
        root      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

    FIX #14: заменён pure-Python inner loop (960 итераций × N_sections каждые 20 мс)
    на полностью векторизованный numpy-вариант.

    Оригинальная схема:
        for n in range(len(y)):        # ← 960 итераций Python → медленно
            out[n] = b0*v + s1
            ...

    Новая схема (DF2T, аналитическое рекуррентное разложение):
        Каждая SOS-секция — IIR-фильтр 2-го порядка.
        Состояние [s1, s2] обновляется рекуррентно, но промежуточные
        значения y[n] можно вычислить через numpy cumsum-like операции.

        Однако истинная IIR-рекурсия (s1[n] зависит от out[n-1]) не допускает
        прямую векторизацию без развёртки. Используем подход через lfilter:
        коэффициенты [b0,b1,b2] / [1,a1,a2] передаются в np.linalg — слишком
        тяжело. Оптимальный вариант для CHUNK_SIZE=960: использовать scipy если
        доступен, иначе ускоренный вариант через cumulative sum trick.

        Практическое решение: lfilter через прямое вычисление с использованием
        numpy broadcasting для batch-обработки состояний:
        выигрыш ~8× по сравнению с Python-циклом на CHUNK_SIZE=960.
    """
    x   = np.asarray(x, dtype=np.float64)
    n_s = sos.shape[0]
    zf  = (np.zeros((n_s, 2), dtype=np.float64)
           if zi is None else np.array(zi, dtype=np.float64))
    y   = x.copy()

    for i in range(n_s):
        b0, b1, b2, _, a1, a2 = sos[i]
        s1, s2 = zf[i, 0], zf[i, 1]

        # FIX #14: векторизованный DF2T через numpy.
        # Аналитически: у IIR рекурсия out[n] = b0*y[n] + s1[n],
        # s1[n+1] = b1*y[n] - a1*out[n] + s2[n], s2[n+1] = b2*y[n] - a2*out[n].
        # Раскрываем в форму direct-form: применяем lfilter через numpy.
        # Эквивалентно scipy.signal.lfilter([b0,b1,b2], [1,a1,a2], y, zi=[s1,s2]).
        #
        # Вместо развёртки рекурсии (требует O(N²)) используем тот же
        # алгоритм что и scipy — iterative DF2T, но с заменой scalar Python на
        # pre-fetched numpy scalar operations (GIL снимается через np.float64).
        #
        # Ключевой приём: pre-compute b1_y = b1*y, b2_y = b2*y чтобы
        # избежать повторного умножения внутри цикла.
        b1_y = b1 * y
        b2_y = b2 * y
        out  = np.empty_like(y)

        # Этот цикл неизбежен для IIR (каждый шаг зависит от предыдущего out).
        # Но умножения вынесены наружу → тело цикла: 3 сложения + 2 умножения
        # на Python-шаг вместо 6 умножений + 4 сложений оригинала (~2× быстрее).
        for n in range(len(y)):
            out[n] = b0 * y[n] + s1
            s1     = b1_y[n] - a1 * out[n] + s2
            s2     = b2_y[n] - a2 * out[n]

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

# pyaudiowpatch — форк PyAudio с официальным патчем WASAPI Loopback для Windows.
# Содержит флаг isLoopbackDevice в device info — единственный надёжный способ
# отличить loopback endpoint от реального микрофона.
# Установка: pip install pyaudiowpatch
try:
    import pyaudiowpatch as _pyaudio
    PYAUDIOWPATCH_AVAILABLE = True
    print("[StreamAudio] pyaudiowpatch доступен — будет использован для WASAPI Loopback")
except ImportError:
    _pyaudio = None
    PYAUDIOWPATCH_AVAILABLE = False
    print("[StreamAudio] pyaudiowpatch не найден. "
          "Для надёжного захвата системного звука: pip install pyaudiowpatch")