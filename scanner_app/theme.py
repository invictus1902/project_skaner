from __future__ import annotations

THEMES = {
    "dark": {
        "bg": "#101114",
        "header": "#101114",
        "card": "#191c22",
        "row": "#21262e",
        "text": "#f4f1ea",
        "muted": "#a39b92",
        "line": "#313843",
        "accent": "#e4b15a",
        "button": "#f3efe6",
        "button_hover": "#ffffff",
        "button_text": "#16140f",
        "select": "#2c3542",
        "select_text": "#f4f1ea",
        "online": "#3ddc84",
        "offline": "#ff6b6b",
        "unset": "#8d8680",
        "in_text": "#3ddc84",
        "out_text": "#7cb3ff",
        "warn_text": "#f0c36a",
        "error_text": "#ff7b7b",
        "entry": "#14181e",
        "banner_bg": "#3a2d16",
        "banner_fg": "#f8e7c4",
        "panels": {
            "idle": {"bg": "#191c22", "fg": "#f4f1ea", "muted": "#b7afa6"},
            "progress": {"bg": "#232830", "fg": "#f4f1ea", "muted": "#d5cbbd"},
            "in": {"bg": "#147a45", "fg": "#f4fff8", "muted": "#d9f5e4"},
            "out": {"bg": "#1d4ed8", "fg": "#f5f8ff", "muted": "#dbe7ff"},
            "offline": {"bg": "#a16207", "fg": "#fffaf0", "muted": "#ffe7b8"},
            "unknown": {"bg": "#b42318", "fg": "#fff5f5", "muted": "#ffd5d5"},
            "disabled": {"bg": "#b42318", "fg": "#fff5f5", "muted": "#ffd5d5"},
            "error": {"bg": "#b42318", "fg": "#fff5f5", "muted": "#ffd5d5"},
            "duplicate": {"bg": "#a16207", "fg": "#fffaf0", "muted": "#ffe7b8"},
        },
    },
    "light": {
        "bg": "#f4f1eb",
        "header": "#f4f1eb",
        "card": "#fffcf8",
        "row": "#f3eee6",
        "text": "#1c1915",
        "muted": "#6f675e",
        "line": "#e3dbd0",
        "accent": "#8d5e24",
        "button": "#1c1915",
        "button_hover": "#3a342c",
        "button_text": "#f7f3ec",
        "select": "#efe4d2",
        "select_text": "#1c1915",
        "online": "#157a3a",
        "offline": "#c24141",
        "unset": "#6f675e",
        "in_text": "#157a3a",
        "out_text": "#1d4ed8",
        "warn_text": "#9a6700",
        "error_text": "#b42318",
        "entry": "#fffcf8",
        "banner_bg": "#f3e2b8",
        "banner_fg": "#5c4518",
        "panels": {
            "idle": {"bg": "#fffcf8", "fg": "#1c1915", "muted": "#6f675e"},
            "progress": {"bg": "#f3eee6", "fg": "#1c1915", "muted": "#6f675e"},
            "in": {"bg": "#157a3a", "fg": "#f7fff9", "muted": "#e5f8ec"},
            "out": {"bg": "#1d4ed8", "fg": "#f5f8ff", "muted": "#e4ecff"},
            "offline": {"bg": "#a16207", "fg": "#fffaf0", "muted": "#fff1d0"},
            "unknown": {"bg": "#b42318", "fg": "#fff5f5", "muted": "#ffe4e4"},
            "disabled": {"bg": "#b42318", "fg": "#fff5f5", "muted": "#ffe4e4"},
            "error": {"bg": "#b42318", "fg": "#fff5f5", "muted": "#ffe4e4"},
            "duplicate": {"bg": "#a16207", "fg": "#fffaf0", "muted": "#fff1d0"},
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
        tabmargins=(8, 10, 8, 0),
        bordercolor=bg,
        lightcolor=bg,
        darkcolor=bg,
    )
    style.configure(
        "TNotebook.Tab",
        padding=(18, 10),
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
        padding=8,
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
        rowheight=38,
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
        background=theme["bg"],
        foreground=theme["muted"],
        font=(sans, 10),
        relief="flat",
        borderwidth=0,
    )
    style.map("Treeview.Heading", background=[("active", theme["bg"])])
    style.configure(
        "TButton",
        padding=(14, 9),
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
        padding=(16, 9),
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
