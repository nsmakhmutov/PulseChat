# -*- mode: python ; coding: utf-8 -*-
# SFU_Server.spec — PyInstaller spec для SFU_Server
# Сборка: pyinstaller --clean SFU_Server.spec
#
# Зависимости сервера:
#   server.py  — основной процесс (TCP/UDP SFU)
#   config.py  — константы, структуры UDP-заголовка (struct, os, sys)
#   Всё остальное — stdlib (socket, threading, json, time, struct)

import os
import sys

sys.setrecursionlimit(5000)

base_path = r'E:\Mychat\VoiceChat'

a = Analysis(
    ['server.py'],
    pathex=[base_path],
    binaries=[],
    datas=[
        # config.py лежит рядом с server.py — PyInstaller подтянет его
        # автоматически как импортируемый модуль, но явно указываем
        # для надёжности (он не является пакетом, а обычным .py)
        (os.path.join(base_path, 'config.py'), '.'),
    ],
    hiddenimports=[
        'config',   # server.py делает: from config import *
        'struct',
        'json',
        'threading',
        'socket',
        'time',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Исключаем всё клиентское — сервер это не нужно
        'PyQt6',
        'av',
        'dxcam',
        'PIL',
        'pygame',
        'sounddevice',
        'opuslib',
        'pyrnnoise',
        'audiolab',
        'keyboard',
        'numpy',
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SFU_Server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,           # Сервер — консольное приложение
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
