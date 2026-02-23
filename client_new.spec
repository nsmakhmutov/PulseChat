# -*- mode: python ; coding: utf-8 -*-
# client.spec — PyInstaller spec для VoiceChatClient
# Сборка: pyinstaller --clean client.spec
#
# Зависимости проекта:
#   PyQt6 (QtWidgets, QtCore, QtGui, QtOpenGLWidgets, QtOpenGL)
#   av (PyAV) — кодирование/декодирование H.264 видео
#   dxcam    — захват экрана через DXGI (Windows only)
#   PIL      — масштабирование кадров перед энкодом
#   opuslib  + opus.dll — аудио кодек
#   pyrnnoise           — шумоподавление
#   audiolab            — аудио движок
#   pyaudiowpatch       — WASAPI Loopback захват системного звука
#   sounddevice, pygame, keyboard, numpy — аудио/ввод/обработка

from PyInstaller.utils.hooks import collect_data_files, collect_submodules
import glob as _glob
import os
import sys
import audiolab
import pyaudiowpatch as _paw

sys.setrecursionlimit(5000)

# ── Пути ─────────────────────────────────────────────────────────────────────
base_path          = r'E:\Mychat\VoiceChat'
audiolab_path      = os.path.dirname(audiolab.__file__)
pyaudiowpatch_path = os.path.dirname(_paw.__file__)
# _portaudiowpatch.cp312-win_amd64.pyd лежит прямо в site-packages,
# а не внутри папки pyaudiowpatch — поднимаемся на уровень выше
site_packages_path = os.path.dirname(pyaudiowpatch_path)

# Python 3.12+ использует теги платформы в именах .pyd.
# Реальное имя файла: _portaudiowpatch.cp312-win_amd64.pyd
_portaudio_candidates = _glob.glob(
    os.path.join(site_packages_path, '_portaudiowpatch*.pyd')
)
if not _portaudio_candidates:
    _all_files = [f for f in os.listdir(site_packages_path) if '_portaudio' in f]
    raise FileNotFoundError(
        "_portaudiowpatch*.pyd не найден в: " + site_packages_path + "\n"
        "Файлы с '_portaudio' в имени: " + str(_all_files) + "\n"
        "Убедитесь что pyaudiowpatch установлен: pip install pyaudiowpatch"
    )
_portaudio_pyd = _portaudio_candidates[0]
print("[spec] pyaudiowpatch_path    :", pyaudiowpatch_path)
print("[spec] _portaudiowpatch*.pyd :", _portaudio_pyd)

# ── Автосбор данных пакетов ───────────────────────────────────────────────────
extra_datas  = collect_data_files('pyrnnoise')
extra_datas += collect_data_files('audiolab')
extra_datas += collect_data_files('jinja2')
extra_datas += collect_data_files('av')          # PyAV: нативные кодеки, .dll/.so

# ── Ручные ассеты проекта ─────────────────────────────────────────────────────
manual_datas = [
    (os.path.join(base_path, 'assets/music'),   'assets/music'),
    (os.path.join(base_path, 'assets/panel'),   'assets/panel'),
    (os.path.join(base_path, 'assets/avatars'), 'assets/avatars'),
    (os.path.join(base_path, 'assets/font'),    'assets/font'),
    (os.path.join(base_path, 'assets/icon'),    'assets/icon'),
    (os.path.join(audiolab_path, 'av/templates'), 'audiolab/av/templates'),
]

all_datas = extra_datas + manual_datas

# ── Бинарники ─────────────────────────────────────────────────────────────────
added_binaries = [
    # opuslib: нативный кодек, кладём рядом с exe
    (os.path.join(base_path, 'opus.dll'), '.'),
    # pyaudiowpatch: _portaudiowpatch.pyd кладём в папку pyaudiowpatch/
    # — именно там Python ищет нативное расширение при импорте пакета.
    (_portaudio_pyd, 'pyaudiowpatch'),
]

# ── Analysis ──────────────────────────────────────────────────────────────────
a = Analysis(
    ['client_main.py'],
    pathex=[base_path],
    binaries=added_binaries,
    datas=all_datas,
    hiddenimports=[
        # Аудио
        'pyrnnoise',
        'opuslib',
        'pyaudiowpatch',
        'sounddevice',
        'audiolab',
        # Видео
        'av',
        'av.codec',
        'av.codec.codec',
        'av.container',
        'av.video',
        'av.video.frame',
        'dxcam',
        'PIL',
        'PIL.Image',
        # Qt — OpenGL-виджеты (ui_video.py использует QOpenGLWidget)
        'PyQt6.QtOpenGLWidgets',
        'PyQt6.QtOpenGL',
        'PyQt6.QtGui',
        'PyQt6.QtCore',
        # Прочее
        'numpy',
        'pygame',
        'pygame.mixer',
        'keyboard',
        'jinja2',
        'ctypes',
        'fractions',
    ] + collect_submodules('pyrnnoise') + collect_submodules('py7zr'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VoiceChatClient',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(base_path, 'assets', 'icon', 'logo.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='VoiceChatClient',
)
