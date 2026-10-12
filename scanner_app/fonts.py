from __future__ import annotations

import ctypes
import os
import tkinter.font as tkfont

# Cormorant Garamond — шрифт заголовков из референса, в нём есть кириллица.
# DM Sans оттуда кириллицы не содержит, поэтому основной текст остаётся системным.
SERIF_NAME = "Cormorant Garamond"
SERIF_BOLD_NAME = "Cormorant Garamond Bold"
SANS_CANDIDATES = ("Segoe UI", "Tahoma", "Arial")
SERIF_FALLBACKS = ("Georgia", "Times New Roman")

_loaded = False
_faces: tuple[str, str, str] | None = None


def _fonts_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")


def load_brand_fonts() -> None:
    global _loaded
    if _loaded or os.name != "nt":
        _loaded = True
        return
    gdi32 = ctypes.windll.gdi32
    gdi32.AddFontResourceExW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_void_p]
    gdi32.AddFontResourceExW.restype = ctypes.c_int
    folder = _fonts_dir()
    if os.path.isdir(folder):
        for name in os.listdir(folder):
            if name.lower().endswith((".ttf", ".otf")):
                gdi32.AddFontResourceExW(os.path.join(folder, name), 0x10, None)
    # Шрифт подключается только этому процессу. Широкое сообщение всем окнам
    # Windows здесь не нужно и может зависнуть, если какое-то окно не отвечает.
    _loaded = True


def faces(widget) -> tuple[str, str, str]:
    """Обычный сериф, жирный сериф и гротеск для остального интерфейса.

    Жирное начертание подключено отдельным семейством: у Tk на Windows
    есть только «обычный» и «жирный», и оба файла иначе склеиваются в один.
    """
    global _faces
    load_brand_fonts()
    if _faces is not None:
        return _faces
    available = set(tkfont.families(widget))
    sans = next((name for name in SANS_CANDIDATES if name in available), "TkDefaultFont")
    if SERIF_NAME in available:
        serif = SERIF_NAME
    else:
        serif = next((name for name in SERIF_FALLBACKS if name in available), sans)
    serif_bold = SERIF_BOLD_NAME if SERIF_BOLD_NAME in available else serif
    _faces = (serif, serif_bold, sans)
    return _faces
