from __future__ import annotations

THEMES = {
    "dark": {
        "bg": "#070a10",
        "header": "#070a10",
        "card": "#0d131c",
        "open_bg": "#123528",
        "row": "#101822",
        "text": "#e7f2ff",
        "muted": "#8ea0b8",
        "line": "#1c3148",
        "accent": "#3ee0ff",
        "accent2": "#ff3d9a",
        "button": "#3ee0ff",
        "button_hover": "#8af0ff",
        "button_text": "#041018",
        "select": "#163044",
        "select_text": "#e7f2ff",
        "online": "#3dffb0",
        "offline": "#ff5d6c",
        "unset": "#6d7e94",
        "in_text": "#5dffa8",
        "out_text": "#7ecbff",
        "warn_text": "#ffc14a",
        "error_text": "#ff7d86",
        "entry": "#070c14",
        "banner_bg": "#142033",
        "banner_fg": "#d7f6ff",
        "panels": {
            "idle": {"bg": "#0d1522", "fg": "#e7f2ff", "muted": "#9bb0c8"},
            "progress": {"bg": "#122033", "fg": "#e7f2ff", "muted": "#b7cbe0"},
            "in": {"bg": "#0c6a42", "fg": "#f3fff8", "muted": "#d5f8e6"},
            "out": {"bg": "#143e86", "fg": "#f4f8ff", "muted": "#d5e4ff"},
            "offline": {"bg": "#8d5a10", "fg": "#fff8ea", "muted": "#ffe6b8"},
            "unknown": {"bg": "#8e2430", "fg": "#fff5f6", "muted": "#ffd4d8"},
            "disabled": {"bg": "#8e2430", "fg": "#fff5f6", "muted": "#ffd4d8"},
            "error": {"bg": "#8e2430", "fg": "#fff5f6", "muted": "#ffd4d8"},
            "duplicate": {"bg": "#8d5a10", "fg": "#fff8ea", "muted": "#ffe6b8"},
        },
    },
    "light": {
        "bg": "#e7eef6",
        "header": "#e7eef6",
        "card": "#f7fbff",
        "open_bg": "#d7f3e4",
        "row": "#ffffff",
        "text": "#0c1422",
        "muted": "#5c6d82",
        "line": "#c5d4e4",
        "accent": "#0c7c92",
        "accent2": "#b01b6a",
        "button": "#0c7c92",
        "button_hover": "#0a6274",
        "button_text": "#f4fbff",
        "select": "#d5e8f4",
        "select_text": "#0c1422",
        "online": "#0d8a4d",
        "offline": "#c43746",
        "unset": "#5c6d82",
        "in_text": "#0d8a4d",
        "out_text": "#1a4f9c",
        "warn_text": "#8d5a10",
        "error_text": "#b42332",
        "entry": "#ffffff",
        "banner_bg": "#d7eef5",
        "banner_fg": "#0c3c4a",
        "panels": {
            "idle": {"bg": "#f7fbff", "fg": "#0c1422", "muted": "#5c6d82"},
            "progress": {"bg": "#e7f2fb", "fg": "#0c1422", "muted": "#5c6d82"},
            "in": {"bg": "#0c6a42", "fg": "#f3fff8", "muted": "#d5f8e6"},
            "out": {"bg": "#143e86", "fg": "#f4f8ff", "muted": "#d5e4ff"},
            "offline": {"bg": "#8d5a10", "fg": "#fff8ea", "muted": "#ffe6b8"},
            "unknown": {"bg": "#8e2430", "fg": "#fff5f6", "muted": "#ffd4d8"},
            "disabled": {"bg": "#8e2430", "fg": "#fff5f6", "muted": "#ffd4d8"},
            "error": {"bg": "#8e2430", "fg": "#fff5f6", "muted": "#ffd4d8"},
            "duplicate": {"bg": "#8d5a10", "fg": "#fff8ea", "muted": "#ffe6b8"},
        },
    },
    "contrast": {
        "bg": "#000000",
        "header": "#000000",
        "card": "#000000",
        "open_bg": "#143000",
        "row": "#1a1a1a",
        "text": "#ffe14a",
        "muted": "#fff2a8",
        "line": "#ffe14a",
        "accent": "#ffe14a",
        "accent2": "#ffe14a",
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
    style.configure("Card.TFrame", background=theme["card"], relief="flat", borderwidth=0)
    style.configure(
        "CardShell.TFrame",
        background=theme["card"],
        relief="solid",
        borderwidth=1,
        bordercolor=theme["line"],
        lightcolor=theme["line"],
        darkcolor=theme["line"],
    )
    style.configure("Card.TLabel", background=theme["card"], foreground=fg, font=(sans, 11))
    style.configure("CardTitle.TLabel", background=theme["card"], foreground=fg, font=(sans, 13))
    style.configure("CardMuted.TLabel", background=theme["card"], foreground=theme["muted"], font=(sans, 11))
    style.configure(
        "Card.TCheckbutton",
        background=theme["card"],
        foreground=fg,
        font=(sans, 11),
        indicatorcolor=theme["entry"],
    )
    style.map(
        "Card.TCheckbutton",
        indicatorcolor=[("selected", theme["accent"]), ("!selected", theme["entry"])],
        background=[("active", theme["card"])],
        foreground=[("disabled", theme["muted"])],
    )
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
