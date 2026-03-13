"""
native_capture.py
Python-обёртка над loopback_capture.dll.
"""

import ctypes
import os
import sys
import numpy as np
from typing import Optional

try:
    from config import resource_path
except ImportError:
    def resource_path(rel: str) -> str:
        base = getattr(sys, "_MEIPASS", os.path.abspath("."))
        return os.path.join(base, rel)


class NativeLoopbackCapture:
    @staticmethod
    def dll_available() -> bool:
        """Проверяет физическое наличие DLL перед её загрузкой."""
        dll_path = resource_path(os.path.join("dlls", "loopback_capture.dll"))
        return os.path.exists(dll_path)

    def __init__(self):
        self._lib: Optional[ctypes.CDLL] = None
        self._last_err: str = ""
        self._load_dll()

    def _load_dll(self):
        dll_path = resource_path(os.path.join("dlls", "loopback_capture.dll"))
        if not os.path.exists(dll_path):
            self._last_err = f"DLL не найдена: {dll_path}"
            return

        try:
            self._lib = ctypes.CDLL(dll_path)
            # Настройка сигнатур
            self._lib.is_supported.restype = ctypes.c_bool
            self._lib.init_capture.argtypes = [ctypes.c_uint32]
            self._lib.init_capture.restype = ctypes.c_int
            self._lib.read_frames.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._lib.read_frames.restype = ctypes.c_int
            self._lib.get_last_log.restype = ctypes.c_char_p
        except Exception as e:
            self._last_err = f"Ошибка загрузки DLL: {e}"

    def is_supported(self) -> bool:
        if not self._lib: return False
        return self._lib.is_supported()

    def init(self, exclude_pid: int) -> bool:
        if not self._lib: return False
        res = self._lib.init_capture(ctypes.c_uint32(exclude_pid))
        if res != 0:
            log = self._lib.get_last_log().decode('utf-8', errors='replace')
            self._last_err = f"HRESULT {hex(res & 0xFFFFFFFF)}"
            print(f"[NativeCapture] Ошибка init ({self._last_err}): {log}")
            return False
        return True

    def read_frames(self, out_buf: np.ndarray) -> int:
        if not self._lib: return 0
        ptr = ctypes.c_void_p(out_buf.ctypes.data)
        return self._lib.read_frames(ptr, len(out_buf))

    def stop(self):
        if self._lib:
            self._lib.stop_capture()

    def get_last_error(self) -> str:
        return self._last_err

    def get_dll_log(self) -> str:
        if not self._lib: return ""
        return self._lib.get_last_log().decode('utf-8', errors='replace')