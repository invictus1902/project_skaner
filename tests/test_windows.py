from __future__ import annotations

import os
import time
import tkinter as tk
from tkinter import ttk
import unittest

from scanner_app.scanner import accepts_typing
from scanner_app.ui_helpers import (
    arm_after,
    describe_window,
    ensure_independent,
    is_independent,
    place_window,
    window_owner,
    work_area,
)


class WindowLayoutTests(unittest.TestCase):
    def test_place_window_stays_inside_the_work_area(self) -> None:
        root = tk.Tk()
        try:
            root.geometry("200x200+0+0")
            root.update_idletasks()
            place_window(root, 5000, 5000, min_width=960, min_height=640)
            root.update_idletasks()
            left, top, right, bottom = work_area(root)
            width, height, x, y = _parse_geometry(root.winfo_geometry())
            self.assertLess(width, 5000)
            self.assertLess(height, 5000)
            self.assertGreaterEqual(x, left)
            self.assertGreaterEqual(y, top)
            self.assertLessEqual(x + width, right)
            self.assertLessEqual(y + height, bottom)
            self.assertGreaterEqual(width, min(960, right - left - 32))
            self.assertGreaterEqual(height, min(640, bottom - top - 32))
            place_window(root, 900, 600, min_width=760, min_height=520, align="right", dy=40)
            root.update_idletasks()
            width, height, x, y = _parse_geometry(root.winfo_geometry())
            self.assertGreaterEqual(x, left)
            self.assertGreaterEqual(y, top)
            self.assertLessEqual(x + width, right)
            self.assertLessEqual(y + height, bottom)
            self.assertGreater(x, left + 40)
        finally:
            root.destroy()

    def test_secondary_window_minimizes_on_its_own(self) -> None:
        if os.name != "nt":
            self.skipTest("windows only")
        root = tk.Tk()
        top = tk.Toplevel(root)
        try:
            root.geometry("320x200+40+40")
            top.geometry("280x180+80+80")
            root.update()
            ensure_independent(top)
            root.update()
            self.assertEqual(window_owner(top), 0, describe_window(top))
            self.assertTrue(is_independent(top), describe_window(top))
            top.iconify()
            root.update()
            self.assertEqual(root.state(), "normal", describe_window(top))
            self.assertEqual(top.state(), "iconic")
            top.deiconify()
            root.update()
            self.assertTrue(is_independent(top), describe_window(top))
            root.iconify()
            root.update()
            self.assertEqual(root.state(), "iconic")
            self.assertNotEqual(top.state(), "iconic", describe_window(top))
        finally:
            root.destroy()

    def test_text_entry_pauses_scanner_input(self) -> None:
        root = tk.Tk()
        try:
            root.withdraw()
            entry = tk.Entry(root)
            typed = ttk.Entry(root)
            button = tk.Button(root, text="ok")
            self.assertTrue(accepts_typing(entry))
            self.assertTrue(accepts_typing(typed))
            self.assertFalse(accepts_typing(button))
            self.assertFalse(accepts_typing(None))
        finally:
            root.destroy()

    def test_new_timer_cancels_the_previous_one(self) -> None:
        root = tk.Tk()
        fired = []
        try:
            root.withdraw()
            first = arm_after(root, None, 500, lambda: fired.append("old"))
            second = arm_after(root, first, 40, lambda: fired.append("new"))
            self.assertIsNotNone(second)
            root.update()
            time.sleep(0.08)
            root.update()
            self.assertEqual(fired, ["new"])
            self.assertIsNone(arm_after(root, second, 0, None))
            time.sleep(0.05)
            root.update()
            self.assertEqual(fired, ["new"])
        finally:
            root.destroy()


def _parse_geometry(value: str):
    size, x_text, y_text = value.split("+", 2)
    width, height = size.split("x", 1)
    return int(width), int(height), int(x_text), int(y_text)
