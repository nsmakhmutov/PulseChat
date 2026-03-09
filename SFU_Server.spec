# -*- mode: python ; coding: utf-8 -*-
# ──────────────────────────────────────────────────────────────────────────────
# export.spec — InPulse v1.0.32+
# Обновлено: добавлен DeepFilterNet3 (DLL + модели), aiortc, av, dxcam,
#            pyaudiowpatch, py7zr; удалён мёртвый audiolab.
#
# ВАЖНО перед сборкой:
#   deep_filter.dll сейчас лежит в корне проекта.
#   Код (audio_engine.py) ищет её по пути:  dlls/DeepFilterNet3/deep_filter.dll
#   Либо перенеси DLL вручную:
#       move deep_filter.dll dlls\DeepFilterNet3\deep_filter.dll
#   Либо оставь как есть — spec упакует её из корня в нужное место сам.
# ──────────────────────────────────────────────────────────────────────────────

import os
import glob
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# ── Datas ─────────────────────────────────────────────────────────────────────

datas = []

# Графика, звуки, иконки
datas += [('assets', 'assets')]

# pyrnnoise — мягкая зависимость (try/except в audio_engine.py).
# collect_data_files подтянет format.txt и прочие ресурсы пакета.
datas += collect_data_files('pyrnnoise')

# DeepFilterNet3 — файлы ONNX-моделей и config.ini.
# DLL (deep_filter.dll) упакована отдельно через binaries (см. ниже).
# Все четыре файла должны лежать в dlls/DeepFilterNet3/:
#   config.ini  enc.onnx  erb_dec.onnx  df_dec.onnx
_dfn_model_dir = os.path.join('dlls', 'DeepFilterNet3')
_dfn_model_files = ['config.ini', 'enc.onnx', 'erb_dec.onnx', 'df_dec.onnx']
for _f in _dfn_model_files:
    _src = os.path.join(_dfn_model_dir, _f)
    if os.path.exists(_src):
        datas += [(_src, _dfn_model_dir)]

# DeepFilterNet3.tar.gz — предсобранный архив для df_create().
# Если архив рядом с папкой — упаковываем его. Иначе он создастся при первом запуске.
_dfn_tar = os.path.join('dlls', 'DeepFilterNet3.tar.gz')
if os.path.exists(_dfn_tar):
    datas += [(_dfn_tar, 'dlls')]

# ── Binaries ──────────────────────────────────────────────────────────────────

binaries = [
    # Opus — кодек голосового чата (ctypes, всегда нужен)
    ('dlls/opus.dll', 'dlls'),
    # RNNoise — старый шумодав (мягкая зависимость, оставляем для совместимости)
    ('dlls/rnnoise.dll', 'dlls'),
]

# deep_filter.dll — шумодав DeepFilterNet3.
# Код ищет по пути: {__file__}/../dlls/DeepFilterNet3/deep_filter.dll
# Сначала проверяем «правильное» место, потом корень проекта (текущее фактическое).
_dfn_dll_in_dir  = os.path.join('dlls', 'DeepFilterNet3', 'deep_filter.dll')
_dfn_dll_in_root = 'deep_filter.dll'

if os.path.exists(_dfn_dll_in_dir):
    binaries += [(_dfn_dll_in_dir, os.path.join('dlls', 'DeepFilterNet3'))]
elif os.path.exists(_dfn_dll_in_root):
    # DLL лежит в корне — упаковываем в нужную папку назначения
    binaries += [(_dfn_dll_in_root, os.path.join('dlls', 'DeepFilterNet3'))]
else:
    import warnings
    warnings.warn(
        "[SPEC] deep_filter.dll не найдена ни в dlls/DeepFilterNet3/, ни в корне! "
        "DeepFilterNet будет недоступен в сборке.",
        stacklevel=1,
    )

# ── Hidden imports ─────────────────────────────────────────────────────────────
# PyInstaller не всегда обнаруживает:
#   • пакеты с lazy-import (try/except ImportError внутри функций)
#   • пакеты, которые регистрируют плагины через entry_points
#   • aiortc — использует внутренние codec-модули через строковые ключи

hidden = [
    # ── Аудио ────────────────────────────────────────────────────────────────
    'opuslib',
    'pyrnnoise',
    'sounddevice',
    'pyaudiowpatch',         # WASAPI Loopback (мягкая зависимость)

    # ── WebRTC ───────────────────────────────────────────────────────────────
    # aiortc загружает кодеки динамически → нужен весь пакет
    *collect_submodules('aiortc'),
    'av',                    # PyAV — декодирование/кодирование медиа
    'aioice',                # ICE-стек (зависимость aiortc)
    'aioice.stun',

    # ── Видео захват ─────────────────────────────────────────────────────────
    'dxcam',                 # захват экрана через DirectX

    # ── UI / системные ───────────────────────────────────────────────────────
    'pycaw',                 # управление громкостью Windows
    'comtypes',
    'jinja2',                # шаблоны (aiohttp/aiortc)
    'jinja2.ext',

    # ── Сеть / обновления ────────────────────────────────────────────────────
    'requests',
    'packaging',             # SemVer сравнение в updater.py
    'py7zr',                 # распаковка .7z в updater.py

    # ── Зависимости pyrnnoise ─────────────────────────────────────────────────
    'tqdm',
    'click',
]

# ── Analysis ──────────────────────────────────────────────────────────────────

a = Analysis(
    ['client_main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter и matplotlib точно не нужны; scipy удалён (заменён встроенной реализацией)
    excludes=['tkinter', 'matplotlib', 'scipy'],
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
    exclude_binaries=True,   # режим папки (onedir) — быстрее запуск
    name='InPulse',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # UPX выключен: нативные DLL (Opus, DFN3) не любят сжатие
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
