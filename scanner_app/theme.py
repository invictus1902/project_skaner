from __future__ import annotations

THEMES = {
    "dark": {
        "bg": "#0e0d0b",
        "header": "#0e0d0b",
        "card": "#171410",
        "row": "#221e19",
        "text": "#f7f1e6",
        "muted": "#a89a88",
        "line": "#3a3228",
        "accent": "#c6a15b",
        "button": "#f3eadc",
        "button_hover": "#fff8ee",
        "button_text": "#1a140e",
        "select": "#2e2820",
        "select_text": "#f7f1e6",
        "online": "#46c98a",
        "offline": "#e15d56",
        "unset": "#8a7f72",
        "in_text": "#5ecf9a",
        "out_text": "#8eb6ef",
        "warn_text": "#e6c27a",
        "error_text": "#f08b84",
        "entry": "#12100d",
        "banner_bg": "#2c2416",
        "banner_fg": "#f6e7c8",
        "panels": {
            "idle": {"bg": "#171410", "fg": "#f7f1e6", "muted": "#c4b7a6"},
            "progress": {"bg": "#221e19", "fg": "#f7f1e6", "muted": "#d8cbb8"},
            "in": {"bg": "#0f5c38", "fg": "#f4fff8", "muted": "#d4efe0"},
            "out": {"bg": "#163a78", "fg": "#f4f7ff", "muted": "#d5e2f8"},
            "offline": {"bg": "#8a5a12", "fg": "#fffaf0", "muted": "#ffe4b8"},
            "unknown": {"bg": "#8e2a22", "fg": "#fff6f4", "muted": "#ffd8d4"},
            "disabled": {"bg": "#8e2a22", "fg": "#fff6f4", "muted": "#ffd8d4"},
            "error": {"bg": "#8e2a22", "fg": "#fff6f4", "muted": "#ffd8d4"},
            "duplicate": {"bg": "#8a5a12", "fg": "#fffaf0", "muted": "#ffe4b8"},
        },
    },
    "light": {
        "bg": "#f3eee6",
        "header": "#f3eee6",
        "card": "#fffdf8",
        "row": "#f4eee4",
        "text": "#1a1612",
        "muted": "#6e6458",
        "line": "#e0d5c6",
        "accent": "#8c6232",
        "button": "#1a1612",
        "button_hover": "#3a3228",
        "button_text": "#f8f3ea",
        "select": "#efe4d2",
        "select_text": "#1a1612",
        "online": "#0e7a3e",
        "offline": "#a33a32",
        "unset": "#6e6458",
        "in_text": "#0e7a3e",
        "out_text": "#1a3f86",
        "warn_text": "#8a5a12",
        "error_text": "#8e2a22",
        "entry": "#fffdf8",
        "banner_bg": "#f3e2b8",
        "banner_fg": "#5c4518",
        "panels": {
            "idle": {"bg": "#fffdf8", "fg": "#1a1612", "muted": "#6e6458"},
            "progress": {"bg": "#f4eee4", "fg": "#1a1612", "muted": "#6e6458"},
            "in": {"bg": "#0f6a3c", "fg": "#f7fff9", "muted": "#e5f8ec"},
            "out": {"bg": "#1a3f86", "fg": "#f5f8ff", "muted": "#e4ecff"},
            "offline": {"bg": "#8a5410", "fg": "#fffaf0", "muted": "#fff1d0"},
            "unknown": {"bg": "#8c2c24", "fg": "#fff6f4", "muted": "#ffe4e4"},
            "disabled": {"bg": "#8c2c24", "fg": "#fff6f4", "muted": "#ffe4e4"},
            "error": {"bg": "#8c2c24", "fg": "#fff6f4", "muted": "#ffe4e4"},
            "duplicate": {"bg": "#8a5410", "fg": "#fffaf0", "muted": "#fff1d0"},
        },
    },
    "contrast": {
        "bg": "#000000",
        "header": "#000000",
        "card": "#000000",
        "row": "#1a1a1a",
        "text": "#ffe14a",
        "muted": "#fff2a8",
        "line": "#ffe14a",
        "accent": "#ffe14a",
        "button": "#ffe14a",
        "button_hover": "#fff6c2",
        "button_text": "#000000",
        "select": "#ffe14a",
        "select_text": "#000000",
        "online": "#b6ff4a",
        "offline": "#ff5a5a",
        "unset": "#ffffff",
        "in_text": "#b6ff4a",
        "out_text": "#7af0ff",
        "warn_text": "#ffe14a",
        "error_text": "#ff5a5a",
        "entry": "#000000",
        "banner_bg": "#ffe14a",
        "banner_fg": "#000000",
        "panels": {
            "idle": {"bg": "#000000", "fg": "#ffe14a", "muted": "#fff2a8"},
            "progress": {"bg": "#1a1a1a", "fg": "#ffffff", "muted": "#ffe14a"},
            "in": {"bg": "#b6ff4a", "fg": "#000000", "muted": "#173000"},
            "out": {"bg": "#7af0ff", "fg": "#000000", "muted": "#00343a"},
            "offline": {"bg": "#ffd24a", "fg": "#000000", "muted": "#3d2e00"},
            "unknown": {"bg": "#ff5a5a", "fg": "#000000", "muted": "#3a0000"},
            "disabled": {"bg": "#ff5a5a", "fg": "#000000", "muted": "#3a0000"},
            "error": {"bg": "#ff5a5a", "fg": "#000000", "muted": "#3a0000"},
            "duplicate": {"bg": "#ffd24a", "fg": "#000000", "muted": "#3d2e00"},
        },
    },
}

THEME_LABELS = (
    ("dark", "Тёмная"),
    ("light", "Светлая"),
    ("contrast", "Высокий контраст"),
)


def theme_by_name(name: str) -> dict:
    return THEMES.get(name) or THEMES["dark"]


def apply_ttk_style(style, theme: dict, sans: str) -> None:
    """Общий вид полей, списков и кнопок. Акцент только у главных действий."""
    bg = theme["bg"]
    fg = theme["text"]
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(".", background=bg, foreground=fg, font=(sans, 11))
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg, font=(sans, 11))
    style.configure("Muted.TLabel", background=bg, foreground=theme["muted"], font=(sans, 11))
    style.configure("TCheckbutton", background=bg, foreground=fg, font=(sans, 11), indicatorcolor=theme["entry"])
    style.map(
        "TCheckbutton",
        indicatorcolor=[("selected", theme["accent"]), ("!selected", theme["entry"])],
        background=[("active", bg)],
        foreground=[("disabled", theme["muted"])],
    )
    style.configure("TRadiobutton", background=bg, foreground=fg, font=(sans, 11), indicatorcolor=theme["entry"])
    style.map(
        "TRadiobutton",
        indicatorcolor=[("selected", theme["accent"]), ("!selected", theme["entry"])],
        background=[("active", bg)],
    )
    style.configure(
        "TNotebook",
        background=bg,
        borderwidth=0,
        tabmargins=(4, 12, 4, 0),
        bordercolor=bg,
        lightcolor=bg,
        darkcolor=bg,
    )
    style.configure(
        "TNotebook.Tab",
        padding=(22, 12),
        background=bg,
        foreground=theme["muted"],
        font=(sans, 11),
        borderwidth=0,
        bordercolor=bg,
        lightcolor=bg,
        darkcolor=bg,
        focuscolor=bg,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", theme["card"]), ("active", theme["row"])],
        foreground=[("selected", theme["text"]), ("active", theme["text"])],
        bordercolor=[("selected", theme["card"]), ("!selected", bg)],
        lightcolor=[("selected", theme["card"])],
        darkcolor=[("selected", theme["card"])],
    )
    style.layout("TNotebook", [("Notebook.client", {"sticky": "nswe"})])
    style.configure(
        "TEntry",
        fieldbackground=theme["entry"],
        foreground=fg,
        insertcolor=fg,
        font=(sans, 11),
        bordercolor=theme["line"],
        lightcolor=theme["line"],
        darkcolor=theme["line"],
        padding=10,
    )
    style.configure(
        "TCombobox",
        fieldbackground=theme["entry"],
        foreground=fg,
        background=theme["card"],
        arrowcolor=fg,
        font=(sans, 11),
        bordercolor=theme["line"],
        padding=4,
    )
    style.map("TCombobox", fieldbackground=[("readonly", theme["entry"])], foreground=[("readonly", fg)])
    style.configure(
        "Treeview",
        background=theme["card"],
        fieldbackground=theme["card"],
        foreground=fg,
        font=(sans, 12),
        rowheight=42,
        borderwidth=0,
        relief="flat",
        bordercolor=theme["bg"],
        lightcolor=theme["bg"],
        darkcolor=theme["bg"],
    )
    style.layout(
        "Treeview",
        [(
            "Treeview.field",
            {
                "sticky": "nswe",
                "border": "0",
                "children": [(
                    "Treeview.padding",
                    {"sticky": "nswe", "children": [("Treeview.treearea", {"sticky": "nswe"})]},
                )],
            },
        )],
    )
    style.map("Treeview", background=[("selected", theme["select"])], foreground=[("selected", theme["select_text"])])
    style.configure(
        "Treeview.Heading",
        background=theme["row"],
        foreground=theme["muted"],
        font=(sans, 10),
        relief="flat",
        borderwidth=0,
    )
    style.map("Treeview.Heading", background=[("active", theme["row"])], foreground=[("active", theme["text"])])
    style.configure(
        "TButton",
        padding=(16, 10),
        background=theme["card"],
        foreground=fg,
        font=(sans, 11),
        borderwidth=1,
        bordercolor=theme["line"],
        lightcolor=theme["line"],
        darkcolor=theme["line"],
        focusthickness=0,
        focuscolor=theme["bg"],
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("active", theme["row"]), ("pressed", theme["row"]), ("disabled", theme["card"])],
        foreground=[("disabled", theme["muted"])],
    )
    style.layout("Accent.TButton", style.layout("TButton"))
    style.configure(
        "Accent.TButton",
        padding=(18, 10),
        background=theme["button"],
        foreground=theme["button_text"],
        font=(sans, 11),
        borderwidth=0,
        focusthickness=0,
        relief="flat",
    )
    style.map(
        "Accent.TButton",
        background=[("active", theme["button_hover"]), ("pressed", theme["button_hover"]), ("disabled", theme["row"])],
        foreground=[("disabled", theme["muted"]), ("!disabled", theme["button_text"])],
    )
    _scrollbar = {
        "background": theme["row"],
        "troughcolor": theme["bg"],
        "bordercolor": theme["bg"],
        "arrowcolor": theme["muted"],
        "lightcolor": theme["row"],
        "darkcolor": theme["row"],
        "gripcount": 0,
        "borderwidth": 0,
        "relief": "flat",
    }
    style.configure("Vertical.TScrollbar", **_scrollbar)
    style.configure("Horizontal.TScrollbar", **_scrollbar)
    style.map(
        "Vertical.TScrollbar",
        background=[("active", theme["line"]), ("pressed", theme["line"])],
    )
    style.map(
        "Horizontal.TScrollbar",
        background=[("active", theme["line"]), ("pressed", theme["line"])],
    )
    style.layout(
        "Vertical.TScrollbar",
        [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})],
    )
    style.layout(
        "Horizontal.TScrollbar",
        [("Horizontal.Scrollbar.trough", {"sticky": "ew", "children": [("Horizontal.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})],
    )
    style.configure("TPanedwindow", background=bg)
    style.configure("Sash", sashthickness=8, background=bg)
