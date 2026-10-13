from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class ScrollFrame(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
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
    def __init__(self, master) -> None:
        super().__init__(master)
        self.columnconfigure(1, weight=1)
        self.row = 0

    def combo(self, label: str, variable, values=(), width: int = 62):
        ttk.Label(self, text=label).grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 2))
        self.row += 1
        widget = ttk.Combobox(self, textvariable=variable, values=values, width=width)
        widget.grid(row=self.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 2))
        self.row += 1
        return widget

    def entry(self, label: str, variable, show=None, width: int = 62):
        ttk.Label(self, text=label).grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(8, 2))
        self.row += 1
        widget = ttk.Entry(self, textvariable=variable, show=show, width=width)
        widget.grid(row=self.row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 2))
        self.row += 1
        return widget

    def check(self, text: str, variable):
        widget = ttk.Checkbutton(self, text=text, variable=variable)
        widget.grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=2)
        self.row += 1
        return widget

    def label(self, text: str, wrap: int = 880):
        widget = ttk.Label(self, text=text, wraplength=wrap, justify="left")
        widget.grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(12, 2))
        self.row += 1
        return widget

    def buttons(self, *pairs):
        frame = ttk.Frame(self)
        frame.grid(row=self.row, column=0, columnspan=2, sticky="w", padx=16, pady=(4, 12))
        for item in pairs:
            text, command = item[0], item[1]
            style_name = item[2] if len(item) > 2 else "TButton"
            ttk.Button(frame, text=text, command=command, style=style_name).pack(side="left", padx=(0, 8))
        self.row += 1
        return frame
