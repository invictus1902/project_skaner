from __future__ import annotations

import ctypes
import os
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk

# Owned Tk windows share one taskbar button: minimizing any of them
# minimizes the whole group. These flags make a secondary window standalone.
GWL_EXSTYLE = -20
GWLP_HWNDPARENT = -8
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
GA_ROOT = 2
GW_OWNER = 4
SPI_GETWORKAREA = 48


class _Rect(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    )


def widget_alive(widget) -> bool:
    if widget is None:
        return False
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False


def work_area(anchor=None) -> tuple:
    """Usable screen in pixels: left, top, right, bottom. The taskbar is excluded."""
    if os.name == "nt":
        rect = _Rect()
        try:
            if _user32().SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
                if rect.right > rect.left and rect.bottom > rect.top:
                    return int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)
        except Exception:
            pass
    if anchor is not None:
        try:
            return 0, 0, int(anchor.winfo_screenwidth()), int(anchor.winfo_screenheight())
        except tk.TclError:
            pass
    return 0, 0, 1280, 720


def pack_mark(parent, theme: dict, anchor: str = "center", pady=(0, 0)) -> tk.Frame:
    """Короткая метка: пурпурная точка и линия акцента."""
    row = tk.Frame(parent, bg=theme["bg"], bd=0, highlightthickness=0)
    hot = tk.Frame(row, bg=theme.get("accent2") or theme["accent"], width=8, height=8, bd=0, highlightthickness=0)
    hot.pack_propagate(False)
    hot.pack(side="left", padx=(0, 8))
    line = tk.Frame(row, bg=theme["accent"], height=2, width=42, bd=0, highlightthickness=0)
    line.pack_propagate(False)
    line.pack(side="left")
    row.pack(anchor=anchor, pady=pady)
    return row


def place_window(
    window,
    width: int,
    height: int,
    *,
    min_width: int,
    min_height: int,
    pad: int = 16,
    dx: int = 0,
    dy: int = 0,
    align: str = "center",
) -> None:
    """Fit a window inside the work area so its buttons stay above the taskbar."""
    left, top, right, bottom = work_area(window)
    area_w = max(1, right - left)
    area_h = max(1, bottom - top)
    max_w = max(320, area_w - pad * 2)
    max_h = max(240, area_h - pad * 2)
    width = min(int(width), max_w)
    height = min(int(height), max_h)
    window.minsize(min(int(min_width), width), min(int(min_height), height))
    if align == "right":
        x = right - width - pad
        y = top + pad
    elif align == "left":
        x = left + pad
        y = top + pad
    else:
        x = left + max(0, (area_w - width) // 2)
        y = top + max(0, (area_h - height) // 2)
    x += int(dx)
    y += int(dy)
    x = min(max(left, x), max(left, right - width))
    y = min(max(top, y), max(top, bottom - height))
    window.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")


def center_over(window, anchor) -> None:
    """Center a small dialog over another window, without leaving the work area."""
    try:
        window.update_idletasks()
        width = max(1, int(window.winfo_width()))
        height = max(1, int(window.winfo_height()))
    except tk.TclError:
        return
    left, top, right, bottom = work_area(window)
    try:
        x = int(anchor.winfo_rootx()) + max(0, (int(anchor.winfo_width()) - width) // 2)
        y = int(anchor.winfo_rooty()) + max(0, (int(anchor.winfo_height()) - height) // 2)
    except tk.TclError:
        x = left + max(0, (right - left - width) // 2)
        y = top + max(0, (bottom - top - height) // 2)
    x = min(max(left, x), max(left, right - width))
    y = min(max(top, y), max(top, bottom - height))
    window.geometry(f"+{int(x)}+{int(y)}")


def describe_window(window) -> str:
    if os.name != "nt":
        return "not-windows"
    parts = []
    try:
        hwnds = _hwnds(window)
    except (tk.TclError, OSError, ValueError):
        return "no-hwnd"
    user32 = _user32()
    for hwnd in hwnds:
        owner = int(user32.GetWindow(hwnd, GW_OWNER) or 0)
        style = _long_get(user32, hwnd, GWL_EXSTYLE)
        parts.append(f"hwnd={hwnd} owner={owner} ex=0x{style:08x}")
    return "; ".join(parts) or "no-hwnd"


def window_owner(window) -> int:
    if os.name != "nt":
        return 0
    try:
        hwnds = _hwnds(window)
    except (tk.TclError, OSError, ValueError):
        return 0
    user32 = _user32()
    for hwnd in hwnds:
        owner = int(user32.GetWindow(hwnd, GW_OWNER) or 0)
        if owner:
            return owner
    return 0


def is_independent(window) -> bool:
    """True when the window is not owned and can show its own taskbar button."""
    if os.name != "nt":
        return True
    if window_owner(window):
        return False
    try:
        hwnds = _hwnds(window)
    except (tk.TclError, OSError, ValueError):
        return False
    if not hwnds:
        return False
    style = _long_get(_user32(), hwnds[0], GWL_EXSTYLE)
    return bool(style & WS_EX_APPWINDOW) and not bool(style & WS_EX_TOOLWINDOW)


def release_owner(window) -> None:
    """Detach the Win32 owner so this window minimizes on its own."""
    if os.name != "nt":
        return
    try:
        if not window.winfo_exists():
            return
        window.update_idletasks()
        hwnds = _hwnds(window)
    except (tk.TclError, OSError, ValueError):
        return
    if not hwnds:
        return
    user32 = _user32()
    frame = hwnds[0]
    for hwnd in hwnds:
        if int(user32.GetWindow(hwnd, GW_OWNER) or 0):
            _long_set(user32, hwnd, GWLP_HWNDPARENT, 0)
    style = _long_get(user32, frame, GWL_EXSTYLE)
    style = (style | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
    _long_set(user32, frame, GWL_EXSTYLE, style)
    user32.SetWindowPos(
        frame,
        None,
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    )


def ensure_independent(window) -> None:
    """Keep a secondary window off the owner's taskbar group, including after remap."""
    if os.name != "nt":
        return

    def on_map(_event=None) -> None:
        release_owner(window)

    try:
        window.bind("<Map>", on_map, add="+")
    except tk.TclError:
        return
    release_owner(window)
    try:
        if window.winfo_ismapped():
            window.withdraw()
            release_owner(window)
            window.deiconify()
    except tk.TclError:
        pass
    try:
        window.after_idle(lambda: release_owner(window))
    except tk.TclError:
        pass


def arm_after(widget, previous, delay: int, callback):
    """Поставить один таймер и снять предыдущий, чтобы старые after не копились."""
    if previous is not None:
        try:
            widget.after_cancel(previous)
        except Exception:
            pass
    if callback is None:
        return None
    try:
        return widget.after(int(delay), callback)
    except Exception:
        return None


def present_window(window) -> None:
    """Show a window above a fullscreen kiosk once, then let the user switch away."""
    try:
        if not window.winfo_exists():
            return
        window.deiconify()
        window.lift()
    except tk.TclError:
        return
    release_owner(window)
    try:
        window.focus_force()
        window.attributes("-topmost", True)
    except tk.TclError:
        return

    def drop() -> None:
        try:
            if window.winfo_exists():
                window.attributes("-topmost", False)
                release_owner(window)
        except tk.TclError:
            pass

    try:
        window.after(250, drop)
    except tk.TclError:
        pass


def _user32():
    lib = ctypes.windll.user32
    if not getattr(lib.GetParent, "argtypes", None):
        lib.GetParent.argtypes = [wintypes.HWND]
        lib.GetParent.restype = wintypes.HWND
    if not getattr(lib.GetAncestor, "argtypes", None):
        lib.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        lib.GetAncestor.restype = wintypes.HWND
    if not getattr(lib.GetWindow, "argtypes", None):
        lib.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        lib.GetWindow.restype = wintypes.HWND
    if not getattr(lib.SetWindowPos, "argtypes", None):
        lib.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        lib.SetWindowPos.restype = wintypes.BOOL
    if not getattr(lib.SystemParametersInfoW, "argtypes", None):
        lib.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
        lib.SystemParametersInfoW.restype = wintypes.BOOL
    return lib


def _hwnds(window) -> list:
    user32 = _user32()
    raw = int(window.winfo_id())
    found = []
    parent = int(user32.GetParent(raw) or 0)
    if parent:
        found.append(parent)
    ancestor = int(user32.GetAncestor(raw, GA_ROOT) or 0)
    if ancestor and ancestor not in found:
        found.append(ancestor)
    if raw not in found:
        found.append(raw)
    return found


def _long_get(user32, hwnd, index: int) -> int:
    fn = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    if not getattr(fn, "argtypes", None):
        fn.argtypes = [wintypes.HWND, ctypes.c_int]
        fn.restype = ctypes.c_ssize_t
    return int(fn(hwnd, index) or 0)


def _long_set(user32, hwnd, index: int, value: int):
    fn = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    if not getattr(fn, "argtypes", None):
        fn.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        fn.restype = ctypes.c_ssize_t
    return fn(hwnd, index, int(value))


class ScrollFrame(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, width=100, height=100)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", self._on_inner)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas)

    def _on_inner(self, _event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def yview_scroll(self, amount: int) -> None:
        self.canvas.yview_scroll(amount, "units")


class Form(ttk.Frame):
    def __init__(self, master, *, on_card: bool = False) -> None:
        super().__init__(master, style="Card.TFrame" if on_card else "TFrame")
        self.on_card = on_card
        self.columnconfigure(1, weight=1)
        self.row = 0

    def _text_style(self) -> str:
        return "Card.TLabel" if self.on_card else "TLabel"

    def _muted_style(self) -> str:
        return "CardMuted.TLabel" if self.on_card else "Muted.TLabel"

    def _check_style(self) -> str:
        return "Card.TCheckbutton" if self.on_card else "TCheckbutton"

    def _frame_style(self) -> str:
        return "Card.TFrame" if self.on_card else "TFrame"

    def card(self, title: str) -> "Form":
        shell = ttk.Frame(self, style="CardShell.TFrame", padding=(16, 12, 16, 8))
        shell.grid(row=self.row, column=0, columnspan=2, sticky="ew", padx=8, pady=(12, 4))
        shell.columnconfigure(0, weight=1)
        self.row += 1
        ttk.Label(shell, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 2))
        inner = Form(shell, on_card=True)
        inner.grid(row=1, column=0, sticky="ew")
        return inner

    def combo(self, label: str, variable, values=(), width: int = 62):
        ttk.Label(self, text=label, style=self._text_style()).grid(
            row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 2)
        )
        self.row += 1
        widget = ttk.Combobox(self, textvariable=variable, values=values, width=width)
        widget.grid(row=self.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 2))
        self.row += 1
        return widget

    def entry(self, label: str, variable, show=None, width: int = 62):
        ttk.Label(self, text=label, style=self._text_style()).grid(
            row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 2)
        )
        self.row += 1
        widget = ttk.Entry(self, textvariable=variable, show=show, width=width)
        widget.grid(row=self.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 2))
        self.row += 1
        return widget

    def check(self, text: str, variable):
        widget = ttk.Checkbutton(self, text=text, variable=variable, style=self._check_style())
        widget.grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=2)
        self.row += 1
        return widget

    def label(self, text: str, wrap: int = 880, muted: bool = False):
        style = self._muted_style() if muted else self._text_style()
        widget = ttk.Label(self, text=text, wraplength=wrap, justify="left", style=style)
        widget.grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.row += 1
        return widget

    def buttons(self, *pairs):
        frame = ttk.Frame(self, style=self._frame_style())
        frame.grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 12))
        for item in pairs:
            text, command = item[0], item[1]
            style_name = item[2] if len(item) > 2 else "TButton"
            ttk.Button(frame, text=text, command=command, style=style_name).pack(side="left", padx=(0, 8))
        self.row += 1
        return frame
