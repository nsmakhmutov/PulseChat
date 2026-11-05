# -*- mode: python ; coding: utf-8 -*-
# ══════════════════════════════════════════════════════════════════════════════
#  InPulse.spec — v5.1  (Pion SFU + Rust WebRTC Edition + senior fixes)
#
# ─── Изменения vs v5 ──────────────────────────────────────────────────────────
#   ИСПРАВЛЕНО:
#     hiddenimports — 7 модулей указывались по плоским путям (старая структура
#       v4), но в v4.8+ они живут внутри пакетов:
#         sfu_bridge          → network_engine.sfu_bridge
#         media_engine_bridge → network_engine.media_engine_bridge
#         rust_video_track    → core.rust_video_track
#         video_engine        → ui_main.video_engine
#         ui_video            → ui_main.ui_video
#         server_discovery    → network_engine.server_discovery
#         updater             → core.updater
#     Без этих правок PyInstaller молча выкидывал их → runtime ImportError.
#
#   ДОБАВЛЕНО в hiddenimports:
#     server.chat_db                — используется server/server.py (FIX v4.8)
#     client_main.client_main       — точка входа из run.py
#     core (пакет) + core.job_object — новый пакет в v4.8
#     network_engine.* submodules   — mixin'ы для NetworkClient
#       (core, webrtc, chat, features, sdp_utils)
#     app_config                    — единый конфиг приложения (v4.8)
#     scipy, scipy.signal           — resample_poly для качественного
#       ресемплинга (senior fix v4.9, без него aliasing в музыке)
#
#   УБРАНО из excludes:
#     scipy — теперь нужен для audio_engine
#
# ─── Изменения v5 vs v4 ───────────────────────────────────────────────────────
#   НОВОЕ:
#     sidecar.exe       — Go Pion SFU (WebRTC relay, ~1% CPU)
#     media-engine.exe  — Rust Media Engine (WGC + NVENC + webrtc-rs)
#     network_engine.sfu_bridge     — Python HTTP-клиент к sidecar.exe
#     network_engine.media_engine_bridge — Python IPC к media-engine.exe
#
#   УДАЛЕНО из архитектуры (но модули оставлены для совместимости):
#     Named Pipe (RustVideoTrack, RustFramePipeReader) → deprecated stubs
#     WebRTCSFU (aiortc SFU на сервере) → PionSfuProxy
#
# ─── Требования к сборочной машине ───────────────────────────────────────────
#   sidecar.exe и media-engine.exe должны лежать в корне проекта рядом со spec.
#   Собрать: cd sfu && go build -ldflags="-s -w" -o ../sidecar.exe .
#            cd media-engine && cargo build --release && copy target\release\*.exe ..\media-engine.exe
#
# ─── Сборка ───────────────────────────────────────────────────────────────────
#   cd E:\Mychat\VoiceChat
#   pyinstaller InPulse.spec --noconfirm
#   Результат: dist\InPulse\InPulse.exe  (onedir)
# ══════════════════════════════════════════════════════════════════════════════

import os
import warnings
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_dynamic_libs

block_cipher = None

# ══════════════════════════════════════════════════════════════════════════════
# DATAS
# ══════════════════════════════════════════════════════════════════════════════

datas = []

# ── Ресурсы: графика, звуки, шрифты, иконки ──────────────────────────────────
datas += [('assets', 'assets')]

# ── pyrnnoise — data-файлы
datas += collect_data_files('pyrnnoise')

# ── audiolab — транзитивная зависимость pyrnnoise
datas += collect_data_files('audiolab')

# ── aiortc и PyAV — codec-метаданные, ffmpeg filter definitions
datas += collect_data_files('aiortc')
datas += collect_data_files('av')

# ── imageio — плагины и ресурсы
try:
    datas += collect_data_files('imageio')
except Exception as _e:
    print(f"[SPEC] imageio data files: {_e}")

# ── onnxruntime — провайдеры и нативные DLL
datas += collect_data_files('onnxruntime')

# ── DeepFilterNet3: ONNX-модели и config.ini
_DFN_DIR   = os.path.join('dlls', 'DeepFilterNet3')
_DFN_FILES = ['config.ini', 'enc.onnx', 'erb_dec.onnx', 'df_dec.onnx']

for _f in _DFN_FILES:
    _src = os.path.join(_DFN_DIR, _f)
    if os.path.exists(_src):
        datas += [(_src, _DFN_DIR)]
    else:
        warnings.warn(
            f"[SPEC] DeepFilterNet3: файл не найден: {_src}\n"
            f"       DeepFilter будет недоступен.",
            stacklevel=1,
        )

# ── DeepFilterNet3.tar.gz
_DFN_TAR = os.path.join('dlls', 'DeepFilterNet3.tar.gz')
if os.path.exists(_DFN_TAR):
    datas += [(_DFN_TAR, 'dlls')]


# ══════════════════════════════════════════════════════════════════════════════
# BINARIES
# ══════════════════════════════════════════════════════════════════════════════

binaries = [
    # ── Opus — кодек голосового чата ─────────────────────────────────────────
    ('dlls/opus.dll', 'dlls'),

    # ── RNNoise — нативная DLL
    ('dlls/rnnoise.dll', 'dlls'),
]

# ── Go Pion SFU sidecar (обязателен для стриминга) ───────────────────────────
_SIDECAR_EXE = 'sidecar.exe'
if os.path.exists(_SIDECAR_EXE):
    binaries += [(_SIDECAR_EXE, '.')]
    print(f"[SPEC] ✔ sidecar.exe найден: {os.path.getsize(_SIDECAR_EXE) // 1024} KB")
else:
    warnings.warn(
        "[SPEC] ✘ sidecar.exe НЕ НАЙДЕН!\n"
        "       cd sfu && go build -ldflags=\"-s -w\" -o ../sidecar.exe .\n"
        "       Без него стрим и просмотр работать не будут!",
        stacklevel=1,
    )

# ── Rust Media Engine (обязателен для стриминга) ─────────────────────────────
_MEDIA_EXE = 'media-engine.exe'
if os.path.exists(_MEDIA_EXE):
    binaries += [(_MEDIA_EXE, '.')]
    print(f"[SPEC] ✔ media-engine.exe найден: {os.path.getsize(_MEDIA_EXE) // 1024} KB")
else:
    warnings.warn(
        "[SPEC] ✘ media-engine.exe НЕ НАЙДЕН!\n"
        "       cd media-engine && cargo build --release\n"
        "       copy target\\release\\inpulse-media-engine.exe ..\\media-engine.exe",
        stacklevel=1,
    )

# ── InPulseAudioExclusion.dll — C++ WASAPI Process Loopback ──────────────────
_AUDIO_EXCL_DLL = os.path.join('dlls', 'InPulseAudioExclusion.dll')
if os.path.exists(_AUDIO_EXCL_DLL):
    binaries += [(_AUDIO_EXCL_DLL, 'dlls')]
    print(f"[SPEC] ✔ InPulseAudioExclusion.dll найдена")
else:
    warnings.warn(
        "[SPEC] ✘ InPulseAudioExclusion.dll НЕ НАЙДЕНА!\n"
        f"       Ожидается: {os.path.abspath(_AUDIO_EXCL_DLL)}\n"
        "       Системный звук в стриме будет ОТСУТСТВОВАТЬ.",
        stacklevel=1,
    )

# ── pyrnnoise: rnnoise.dll из пакета ─────────────────────────────────────────
try:
    _pyrnnoise_bins = collect_dynamic_libs('pyrnnoise')
    if _pyrnnoise_bins:
        binaries += _pyrnnoise_bins
    else:
        import pyrnnoise as _pn
        _pn_dir = os.path.dirname(_pn.__file__)
        for _dll_name in ('rnnoise.dll', 'librnnoise.dll', 'librnnoise.so'):
            _dll_path = os.path.join(_pn_dir, _dll_name)
            if os.path.exists(_dll_path):
                binaries += [(_dll_path, 'pyrnnoise')]
                break
except Exception as _e:
    print(f"[SPEC] WARNING: pyrnnoise binaries: {_e}")

# ── audiolab: нативные компоненты ────────────────────────────────────────────
try:
    _audiolab_bins = collect_dynamic_libs('audiolab')
    if _audiolab_bins:
        binaries += _audiolab_bins
except Exception:
    pass

# ── deep_filter.dll — Rust-библиотека DeepFilterNet3 ─────────────────────────
_DFN_DLL_IN_DIR  = os.path.join('dlls', 'DeepFilterNet3', 'deep_filter.dll')
_DFN_DLL_IN_ROOT = 'deep_filter.dll'
_DFN_DLL_DEST    = os.path.join('dlls', 'DeepFilterNet3')

if os.path.exists(_DFN_DLL_IN_DIR):
    binaries += [(_DFN_DLL_IN_DIR, _DFN_DLL_DEST)]
elif os.path.exists(_DFN_DLL_IN_ROOT):
    binaries += [(_DFN_DLL_IN_ROOT, _DFN_DLL_DEST)]
else:
    warnings.warn(
        "[SPEC] deep_filter.dll не найдена! DeepFilterNet3 недоступен.",
        stacklevel=1,
    )

# ── PyAV / ffmpeg DLL (для декодирования у зрителя через aiortc) ─────────────
# av.libs содержит libavcodec, libavformat и т.д. нужные для aiortc decode
try:
    _av_bins = collect_dynamic_libs('av')
    if _av_bins:
        binaries += _av_bins
        print(f"[SPEC] PyAV native libs: {len(_av_bins)} файлов")
except Exception as _e:
    print(f"[SPEC] PyAV dynamic libs: {_e}")


# ══════════════════════════════════════════════════════════════════════════════
# HIDDEN IMPORTS
# ══════════════════════════════════════════════════════════════════════════════

hidden = [
    # ── v3: Pion SFU + Media Engine bridges ──────────────────────────────────
    # SENIOR FIX: правильные пакетные пути. Эти модули живут внутри
    # network_engine/ и core/, не в корне.
    'network_engine.sfu_bridge',
    'network_engine.media_engine_bridge',
    'core.rust_video_track',       # deprecated stubs — оставлены для совместимости

    # ── Пакеты проекта ────────────────────────────────────────────────────────
    'audio_engine',
    'audio_engine.audio_engine',
    'audio_engine.audio_capture',
    'audio_engine.audio_processing',

    'server',
    'server.server',
    'server.server_webrtc',
    'server.chat_db',              # SENIOR FIX: импортируется server/server.py

    'ui_main',
    'ui_main.ui_main',
    'ui_main.ui_widgets',
    'ui_main.ui_channel',
    'ui_main.ui_video',            # SENIOR FIX: переехал из корня в ui_main/
    'ui_main.video_engine',        # SENIOR FIX: переехал из корня в ui_main/

    'ui_dialogs',
    'ui_dialogs.ui_dialogs',
    'ui_dialogs.ui_settings',
    'ui_dialogs.ui_soundboard',
    'ui_dialogs.ui_stream_settings',

    'client_main',
    'client_main.client_main',     # SENIOR FIX: run.py делает `from client_main.client_main import main`
    'client_main.app_init',
    'client_main.ui_login',
    'client_main.ui_server_select',
    'client_main.ui_connecting',
    'client_main.ui_styles',
    'client_main.ui_titlebar',

    # ── core/ — новый пакет (job_object, updater, rust_video_track) ───────────
    'core',
    'core.job_object',             # SENIOR FIX: используется sfu_bridge и media_engine_bridge
    'core.updater',                # SENIOR FIX: переехал в core/, run.py импортит явно

    # ── network_engine/ — полный набор submodules ─────────────────────────────
    # SENIOR FIX: NetworkClient = mixin из core + webrtc + chat + features.
    # Если забыть хотя бы один — at runtime AttributeError "no mixin method".
    'network_engine',
    'network_engine.core',
    'network_engine.webrtc',
    'network_engine.chat',
    'network_engine.features',
    'network_engine.sdp_utils',
    'network_engine.server_discovery',  # SENIOR FIX: переехал из корня

    # Корневые модули (оставшиеся реально в корне)
    'config',
    'version',
    'app_config',                  # SENIOR FIX: используется через `from app_config import cfg`

    # ── Аудио ────────────────────────────────────────────────────────────────
    'opuslib',
    'sounddevice',
    'soundfile',
    'pyrnnoise',
    'pyrnnoise.rnnoise',
    'pyrnnoise.pyrnnoise',
    'audiolab',
    *collect_submodules('audiolab'),
    'pyaudiowpatch',

    # ── DeepFilterNet3 ────────────────────────────────────────────────────────
    'onnxruntime',
    'onnxruntime.capi',

    # ── WebRTC (aiortc — для зрителя) ─────────────────────────────────────────
    *collect_submodules('aiortc'),
    'av',
    'aioice',
    'aioice.stun',

    # ── Видеозахват (dxcam оставлен для совместимости, не используется активно)
    'dxcam',

    # ── Медиа в чате ─────────────────────────────────────────────────────────
    'imageio',
    'imageio.plugins',
    'imageio.plugins.ffmpeg',
    'imageio.v3',
    'imageio_ffmpeg',
    'numpy',
    'numpy.core',

    # ── UI / Qt ───────────────────────────────────────────────────────────────
    'PyQt6',
    'PyQt6.QtCore',
    'PyQt6.QtWidgets',
    'PyQt6.QtGui',
    'PyQt6.QtOpenGLWidgets',
    'PyQt6.QtMultimedia',
    'PyQt6.QtMultimediaWidgets',

    # ── Системные / Windows API ───────────────────────────────────────────────
    'pycaw',
    'pycaw.pycaw',
    'comtypes',
    'comtypes.client',
    'keyboard',
    'winsound',
    'jinja2',
    'jinja2.ext',

    # ── Обновления ────────────────────────────────────────────────────────────
    'requests',
    'packaging',
    'packaging.version',
    'packaging.specifiers',
    'packaging.requirements',
    'py7zr',

    # ── Транзитивные ─────────────────────────────────────────────────────────
    'tqdm',
    'click',

    # ── SENIOR FIX v4.9: scipy добавлен в hidden И убран из excludes ──────────
    # audio_engine.audio_capture._resample и audio_engine.output_callback
    # используют scipy.signal.resample_poly для качественного ресемплинга.
    # Fallback на np.interp есть, но без scipy качество музыки в стриме падает
    # (aliasing). Импорт ленивый (внутри функции) — нужен hidden import.
    'scipy',
    'scipy.signal',

    # ── Remote Control — управление мышью/клавиатурой (pyautogui) ────────────
    # pyautogui подгружает платформенный бэкенд динамически через platform.system()
    # — PyInstaller его не видит при статическом анализе. Нужны явные hidden imports.
    'pyautogui',
    'pyautogui._pyautogui_win',   # Windows-бэкенд (ОБЯЗАТЕЛЬНО — иначе ImportError в рантайме)
    'pyscreeze',                   # скриншоты — транзитивная зависимость pyautogui
    'pytweening',                  # easing-функции для moveTo; импортируется при старте модуля
    'mouseinfo',                   # опциональная зависимость pyautogui
    'pymsgbox',                    # диалоги pyautogui
]


# ══════════════════════════════════════════════════════════════════════════════
# ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════

a = Analysis(
    ['run.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        # SENIOR FIX v4.9: scipy УБРАН из excludes — используется для
        # качественного ресемплинга в audio_engine/audio_capture.py
        # (scipy.signal.resample_poly). Без него aliasing на стримах музыки.
        'torch',
        'torchvision',
        'torchaudio',
        'deepfilternet',
        'tensorflow',
        'keras',
        'IPython',
        'notebook',
        'jupyter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='InPulse',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # ↓ console=True пока тестируем на двух клиентах — видно все логи и ошибки
    # После успешного теста поменять на False
    console=False,
    icon='assets/icon/logo.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='InPulse',
)
