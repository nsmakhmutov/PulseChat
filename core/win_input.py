"""
core/win_input.py — низкоуровневая инъекция ввода через WinAPI SendInput.

Зачем не pyautogui:
  pyautogui.write() на Windows шлёт VK-скан-коды и НЕ умеет Unicode —
  кириллица и часть символов просто не печатаются (видно по логам:
  write('р') вызывается, но ничего не вводится). SendInput с флагом
  KEYEVENTF_UNICODE печатает ЛЮБОЙ символ по его Unicode-кодпоинту,
  независимо от активной раскладки.

Модуль безопасен к импорту на не-Windows: WIN_INPUT_AVAILABLE=False,
все функции становятся no-op.
"""

import sys

WIN_INPUT_AVAILABLE = False

if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes

    WIN_INPUT_AVAILABLE = True

    user32 = ctypes.WinDLL('user32', use_last_error=True)

    # ── Константы ───────────────────────────────────────────────────────────
    INPUT_MOUSE    = 0
    INPUT_KEYBOARD = 1

    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP       = 0x0002
    KEYEVENTF_UNICODE     = 0x0004
    KEYEVENTF_SCANCODE    = 0x0008

    MOUSEEVENTF_MOVE       = 0x0001
    MOUSEEVENTF_LEFTDOWN   = 0x0002
    MOUSEEVENTF_LEFTUP     = 0x0004
    MOUSEEVENTF_RIGHTDOWN  = 0x0008
    MOUSEEVENTF_RIGHTUP    = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP   = 0x0040
    MOUSEEVENTF_WHEEL      = 0x0800
    MOUSEEVENTF_ABSOLUTE   = 0x8000
    MOUSEEVENTF_VIRTUALDESK = 0x4000

    WHEEL_DELTA = 120

    SM_XVIRTUALSCREEN  = 76
    SM_YVIRTUALSCREEN  = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79
    SM_CXSCREEN = 0
    SM_CYSCREEN = 1

    ULONG_PTR = ctypes.POINTER(ctypes.c_ulong)

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class _INPUTunion(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]

    def _send(*inputs):
        n = len(inputs)
        arr = (INPUT * n)(*inputs)
        return user32.SendInput(n, arr, ctypes.sizeof(INPUT))

    # ── Виртуальные коды (VK) для модификаторов/спец-клавиш ─────────────────
    # Имена совпадают с теми, что отдаёт _qt_key_to_special() в ui_main.
    VK = {
        'enter': 0x0D, 'backspace': 0x08, 'delete': 0x2E, 'tab': 0x09,
        'esc': 0x1B, 'space': 0x20,
        'left': 0x25, 'up': 0x26, 'right': 0x27, 'down': 0x28,
        'home': 0x24, 'end': 0x23, 'pageup': 0x21, 'pagedown': 0x22,
        'insert': 0x2D, 'printscreen': 0x2C,
        'capslock': 0x14, 'numlock': 0x90, 'scrolllock': 0x91,
        'ctrl': 0x11, 'shift': 0x10, 'alt': 0x12, 'win': 0x5B,
        'f1': 0x70, 'f2': 0x71, 'f3': 0x72, 'f4': 0x73, 'f5': 0x74,
        'f6': 0x75, 'f7': 0x76, 'f8': 0x77, 'f9': 0x78, 'f10': 0x79,
        'f11': 0x7A, 'f12': 0x7B,
    }
    # Клавиши, требующие флаг EXTENDEDKEY (правый блок / навигация)
    _EXTENDED = {
        'right', 'left', 'up', 'down', 'home', 'end',
        'pageup', 'pagedown', 'insert', 'delete', 'win', 'printscreen',
    }

    # ── Мышь ────────────────────────────────────────────────────────────────
    def move_to(nx: float, ny: float):
        """
        Перемещение по нормализованным (0..1) координатам ПЕРВИЧНОГО монитора.
        Стрим по умолчанию захватывает primary (dxcam output_idx=0), поэтому
        мапим на первичный экран (MOUSEEVENTF_ABSOLUTE без VIRTUALDESK
        нормирует 0..65535 именно по primary monitor).
        """
        ax = int(max(0.0, min(1.0, nx)) * 65535)
        ay = int(max(0.0, min(1.0, ny)) * 65535)
        flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
        mi = MOUSEINPUT(ax, ay, 0, flags, 0, None)
        _send(INPUT(INPUT_MOUSE, _INPUTunion(mi=mi)))

    _BTN_DOWN = {1: MOUSEEVENTF_LEFTDOWN, 2: MOUSEEVENTF_RIGHTDOWN, 4: MOUSEEVENTF_MIDDLEDOWN}
    _BTN_UP   = {1: MOUSEEVENTF_LEFTUP,   2: MOUSEEVENTF_RIGHTUP,   4: MOUSEEVENTF_MIDDLEUP}

    def mouse_button(button: int, down: bool, nx: float = None, ny: float = None):
        """Нажатие/отпускание кнопки. Если заданы nx/ny — сперва двигаем туда."""
        if nx is not None and ny is not None:
            move_to(nx, ny)
        flag = (_BTN_DOWN if down else _BTN_UP).get(int(button), 
                MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP)
        mi = MOUSEINPUT(0, 0, 0, flag, 0, None)
        _send(INPUT(INPUT_MOUSE, _INPUTunion(mi=mi)))

    def mouse_scroll(delta: int):
        mi = MOUSEINPUT(0, 0, ctypes.c_int32(int(delta)).value & 0xFFFFFFFF,
                        MOUSEEVENTF_WHEEL, 0, None)
        _send(INPUT(INPUT_MOUSE, _INPUTunion(mi=mi)))

    # ── Клавиатура ───────────────────────────────────────────────────────────
    def key_vk(vk: int, down: bool, extended: bool = False):
        """Нажатие/отпускание по виртуальному коду (для модификаторов/спец-клавиш)."""
        flags = 0
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= KEYEVENTF_KEYUP
        ki = KEYBDINPUT(vk, 0, flags, 0, None)
        _send(INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki)))

    def key_named(name: str, down: bool):
        """Нажатие/отпускание спец-клавиши по имени ('ctrl','enter','f5'...)."""
        vk = VK.get(name)
        if vk is None:
            return False
        key_vk(vk, down, extended=(name in _EXTENDED))
        return True

    def type_unicode(ch: str):
        """
        Печатает один символ через Unicode (KEYEVENTF_UNICODE) — down+up.
        Работает для ЛЮБОГО символа (латиница, кириллица, !@#, эмодзи)
        независимо от раскладки.
        """
        for cp in ch:
            code = ord(cp)
            # Суррогатные пары (символы вне BMP) SendInput принимает по одному 16-битному коду
            down = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None)
            up   = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)
            _send(INPUT(INPUT_KEYBOARD, _INPUTunion(ki=down)),
                  INPUT(INPUT_KEYBOARD, _INPUTunion(ki=up)))

else:
    # Не-Windows: заглушки, чтобы импорт не падал.
    VK = {}

    def move_to(nx, ny): pass
    def mouse_button(button, down, nx=None, ny=None): pass
    def mouse_scroll(delta): pass
    def key_vk(vk, down, extended=False): pass
    def key_named(name, down): return False
    def type_unicode(ch): pass
