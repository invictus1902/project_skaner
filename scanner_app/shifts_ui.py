from __future__ import annotations

import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from tkinter import messagebox, ttk

from scanner_app.fonts import faces
from scanner_app.shifts import (
    STATE_AT_WORK,
    STATE_AWAY,
    STATE_LABELS,
    STATE_NEVER,
    clock_short,
    end_clock_label,
    format_date_ru,
    format_duration,
    is_open,
    session_end_day,
    person_name,
    person_state,
    sessions_for_user,
)
from scanner_app.theme import apply_ttk_style, theme_by_name
from scanner_app.ui_helpers import center_over, ensure_independent, pack_mark, place_window, present_window


class ShiftsWindow(tk.Toplevel):
    """Кто на работе, и ручная правка смен. Список берётся с сервера целиком."""

    def __init__(self, master, app) -> None:
        super().__init__(master)
        self.app = app
        self.title("Смены · учёт времени")
        place_window(self, 1040, 660, min_width=760, min_height=520, align="right", dy=40)
        self._theme = theme_by_name((app.settings.get("ui") or {}).get("theme") or "dark")
        self.serif, _bold, self.sans = faces(self)
        self._users: list[dict] = []
        self._sessions: list[dict] = []
        self._selected_user = None
        self._filter = tk.StringVar(value="all")
        self._query = tk.StringVar(value="")
        self._counts = {STATE_AT_WORK: 0, STATE_AWAY: 0, STATE_NEVER: 0}
        self._filter_buttons: dict = {}
        self._status = tk.StringVar(value="Загружаю сотрудников и смены…")
        self._person_title = tk.StringVar(value="Выберите сотрудника")
        self._style()
        self._build()
        self._query.trace_add("write", lambda *_args: self._fill_people())
        self._filter.trace_add("write", lambda *_args: self._fill_people())
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self.bind("<Destroy>", self._forget, add="+")
        self.people.bind("<Configure>", self._fit_people, add="+")
        self.sessions.bind("<Configure>", self._fit_sessions, add="+")
        ensure_independent(self)
        present_window(self)
        self._reload()

    def _style(self) -> None:
        theme = self._theme
        self.configure(bg=theme["bg"])
        apply_ttk_style(ttk.Style(self), theme, self.sans)

    def _build(self) -> None:
        top = ttk.Frame(self)
        top.pack(fill="x", padx=20, pady=(16, 4))
        tk.Label(top, text="Смены", bg=self._theme["bg"], fg=self._theme["text"], font=(self.serif, 28)).pack(anchor="w")
        pack_mark(top, self._theme, anchor="w", pady=(8, 0))
        ttk.Label(
            top,
            text="Окно сворачивается отдельно от остальных. Esc — закрыть.",
            style="Muted.TLabel",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(top, textvariable=self._status, wraplength=700, justify="left").pack(anchor="w", pady=(4, 0))

        search = ttk.Frame(self)
        search.pack(fill="x", padx=20, pady=(10, 4))
        ttk.Label(search, text="Найти").pack(side="left")
        ttk.Entry(search, textvariable=self._query).pack(side="left", fill="x", expand=True, padx=8, ipady=3)
        ttk.Button(search, text="Обновить", command=self._reload).pack(side="left")
        ttk.Button(search, text="Отправить очередь", command=self._flush).pack(side="left", padx=(8, 0))

        filters = tk.Frame(self, bg=self._theme["bg"])
        filters.pack(fill="x", padx=20, pady=(4, 10))
        for value, _label in _FILTERS:
            button = tk.Button(
                filters,
                font=(self.sans, 11),
                relief="flat",
                bd=0,
                padx=14,
                pady=6,
                cursor="hand2",
                command=lambda chosen=value: self._filter.set(chosen),
            )
            button.pack(side="left", padx=(0, 8))
            self._filter_buttons[value] = button
        self._paint_filters()

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=3)
        pane.add(right, weight=4)

        self.people = ttk.Treeview(left, columns=("name", "badge", "state"), show="headings", height=8)
        self.people.heading("name", text="Сотрудник")
        self.people.heading("badge", text="Бейдж")
        self.people.heading("state", text="Сейчас")
        self.people.column("name", width=260, minwidth=140, stretch=True, anchor="w")
        self.people.column("badge", width=76, minwidth=64, stretch=False, anchor="w")
        self.people.column("state", width=176, minwidth=160, stretch=False, anchor="w")
        self.people.pack(fill="both", expand=True)
        self.people.bind("<Motion>", self._people_motion, add="+")
        self.people.bind("<Leave>", self._hide_people_tip, add="+")
        self._people_tip = None
        self._people_tip_label = None
        self._people_tip_text = ""
        self.people.tag_configure("at_work", foreground=self._theme["in_text"])
        self.people.tag_configure("away", foreground=self._theme["text"])
        self.people.tag_configure("never", foreground=self._theme["muted"])
        self.people.bind("<<TreeviewSelect>>", lambda _event: self._fill_sessions())

        ttk.Label(right, textvariable=self._person_title, font=(self.serif, 22)).pack(anchor="w", pady=(0, 8))
        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = ttk.Button(actions, text="Начать смену", command=self._start, style="Accent.TButton")
        self.start_button.pack(side="left")
        self.finish_button = ttk.Button(actions, text="Завершить смену", command=self._finish)
        self.finish_button.pack(side="left", padx=8)
        ttk.Button(actions, text="Изменить смену", command=self._edit).pack(side="left")
        self.session_hint = ttk.Label(
            right,
            text="Двойной щелчок меняет смену. У идущей — только приход, у завершённой — приход и уход.",
            wraplength=420,
            justify="left",
        )
        self.session_hint.pack(side="bottom", fill="x", anchor="w", pady=(8, 0))
        self.sessions = ttk.Treeview(right, columns=("date", "start", "end", "length", "state"), show="headings", height=6)
        date_w, start_w, end_w, length_w = self._session_fixed_widths()
        for column, title, width, stretch in (
            ("date", "Дата", date_w, False),
            ("start", "Приход", start_w, False),
            ("end", "Уход", end_w, False),
            ("length", "Длительность", length_w, False),
            ("state", "Состояние", 120, True),
        ):
            self.sessions.heading(column, text=title)
            self.sessions.column(
                column,
                width=width,
                minwidth=width if not stretch else 48,
                stretch=stretch,
                anchor="w",
            )
        self.sessions.pack(side="top", fill="both", expand=True, pady=(0, 0))
        self._paint_shift_actions()
        self.sessions.tag_configure(
            "open",
            background=self._theme["open_bg"],
            foreground=self._theme["in_text"],
        )
        self.sessions.tag_configure("closed", foreground=self._theme["text"])
        self.sessions.tag_configure("pending", foreground=self._theme["warn_text"])
        self.sessions.bind("<Double-1>", lambda _event: self._edit())

    def _reload(self) -> None:
        self._status.set("Загружаю сотрудников и смены…")
        threading.Thread(target=self._load_worker, daemon=True).start()

    def _load_worker(self) -> None:
        from scanner_app.shifts import ShiftBook

        snapshot = ShiftBook(self.app.db, self.app.api).load()
        self._post(lambda: self._apply(snapshot))

    def _flush(self) -> None:
        self._status.set("Отправляю очередь…")

        def run() -> None:
            self.app.service.run_shift(lambda book: book.flush_pending())
            snapshot = None
            from scanner_app.shifts import ShiftBook

            snapshot = ShiftBook(self.app.db, self.app.api).load()
            self._post(lambda: self._apply(snapshot, "Очередь отправлена, список обновлён."))

        threading.Thread(target=run, daemon=True).start()

    def _apply(self, snapshot: dict, prefix: str = "") -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        self._users = list(snapshot.get("users") or [])
        self._sessions = list(snapshot.get("sessions") or [])
        counts = {STATE_AT_WORK: 0, STATE_AWAY: 0, STATE_NEVER: 0}
        for user in self._users:
            counts[person_state(sessions_for_user(self._sessions, user.get("id")))] += 1
        self._counts = counts
        self._paint_filters()
        text = (
            f"На работе {counts[STATE_AT_WORK]}, не на работе {counts[STATE_AWAY]}, "
            f"ещё не сканировался {counts[STATE_NEVER]}. В очереди {snapshot.get('pending') or 0}."
        )
        if snapshot.get("message"):
            text += " " + str(snapshot.get("message"))
        if prefix:
            text = prefix + " " + text
        self._status.set(text)
        self._fill_people()

    def _fill_people(self) -> None:
        selected = self._selected_id()
        query = self._query.get().strip().casefold()
        wanted = self._filter.get()
        rows = []
        for user in self._users:
            state = person_state(sessions_for_user(self._sessions, user.get("id")))
            if wanted != "all" and state != wanted:
                continue
            name = person_name(user)
            badge = str(user.get("badgeId") or "")
            if query and query not in name.casefold() and query not in badge.casefold():
                continue
            rows.append((user, state, name, badge))
        order = {STATE_AT_WORK: 0, STATE_AWAY: 1, STATE_NEVER: 2}
        rows.sort(key=lambda item: (order.get(item[1], 9), item[2].casefold()))
        tree_rows = []
        chosen = None
        for user, state, name, badge in rows:
            iid = str(user.get("id"))
            tree_rows.append((iid, (name, badge or "—", STATE_LABELS.get(state, state)), (state,)))
            if iid == selected:
                chosen = iid
        self._filling = True
        try:
            _sync_tree(self.people, tree_rows)
            if chosen:
                if self.people.selection() != (chosen,):
                    self.people.selection_set(chosen)
                    self.people.see(chosen)
            elif rows:
                first = str(rows[0][0].get("id"))
                if self.people.selection() != (first,):
                    self.people.selection_set(first)
            elif self.people.selection():
                self.people.selection_remove(*self.people.selection())
        finally:
            self._filling = False
        self._paint_filters()
        self._fill_sessions()

    def _fill_sessions(self) -> None:
        if getattr(self, "_filling", False):
            return
        user = self._current_user()
        if not user:
            children = self.sessions.get_children()
            if children:
                self.sessions.delete(*children)
            if self._person_title.get() != "Выберите сотрудника":
                self._person_title.set("Выберите сотрудника")
            self._paint_shift_actions()
            return
        title = person_name(user)
        if self._person_title.get() != title:
            self._person_title.set(title)
        tree_rows = []
        for session in sessions_for_user(self._sessions, user.get("id")):
            iid = _session_iid(session)
            if is_open(session):
                state = "На работе"
                length = "идёт"
            else:
                state = "Завершена"
                length = format_duration(session.get("durationMinutes")) or "—"
            tag = "open" if is_open(session) else "closed"
            if session.get("pending"):
                state += ", ждёт отправки"
                if not is_open(session):
                    tag = "pending"
            tree_rows.append(
                (
                    iid,
                    (
                        format_date_ru(str(session.get("date") or "")),
                        clock_short(str(session.get("startTime") or "")) or "—",
                        end_clock_label(session),
                        length,
                        state,
                    ),
                    (tag,),
                )
            )
        _sync_tree(self.sessions, tree_rows)
        self._paint_shift_actions()

    def _paint_shift_actions(self) -> None:
        user = self._current_user()
        open_shift = bool(user) and any(is_open(row) for row in sessions_for_user(self._sessions, user.get("id")))
        self.start_button.configure(style="TButton" if open_shift else "Accent.TButton")
        self.finish_button.configure(style="Accent.TButton" if open_shift else "TButton")

    def _start(self) -> None:
        user = self._current_user()
        if not user or user.get("id") is None:
            messagebox.showinfo("Смена", "Сначала выберите сотрудника.", parent=self)
            return
        open_rows = [row for row in sessions_for_user(self._sessions, user.get("id")) if is_open(row)]
        if open_rows and not messagebox.askyesno(
            "Смена",
            "У этого сотрудника уже есть незакрытая смена. Начать ещё одну?",
            parent=self,
        ):
            return
        now = datetime.now().astimezone()
        values = _ask_times(
            self,
            "Начать смену",
            "start",
            {"date": now.strftime("%d.%m.%Y"), "time": now.strftime("%H:%M")},
            self._theme,
        )
        if not values:
            return
        self._run(lambda book: book.start(user.get("id"), values["date"], values["time"]))

    def _finish(self) -> None:
        user = self._current_user()
        session = self._current_session()
        rows = sessions_for_user(self._sessions, user.get("id")) if user else []
        if session is None or not is_open(session):
            session = next((row for row in rows if is_open(row)), None)
        if session is None:
            messagebox.showinfo("Смена", "У сотрудника нет идущей смены.", parent=self)
            return
        now = datetime.now().astimezone()
        values = _ask_times(
            self,
            "Завершить смену",
            "end",
            {
                "arrival": f"{format_date_ru(str(session.get('date') or ''))} {clock_short(str(session.get('startTime') or ''))}",
                "end_date": now.strftime("%d.%m.%Y"),
                "end_time": now.strftime("%H:%M"),
            },
            self._theme,
        )
        if not values:
            return
        self._run(lambda book: book.finish(session, values["end_date"], values["end_time"]))

    def _edit(self) -> None:
        session = self._current_session()
        if session is None:
            messagebox.showinfo("Смена", "Выберите смену в списке справа.", parent=self)
            return
        if is_open(session):
            values = _ask_times(
                self,
                "Время прихода",
                "edit_open",
                {
                    "date": format_date_ru(str(session.get("date") or "")),
                    "time": clock_short(str(session.get("startTime") or "")),
                },
                self._theme,
            )
            if not values:
                return
            self._run(lambda book: book.change_arrival(session, values["date"], values["time"]))
            return
        values = _ask_times(
            self,
            "Приход и уход",
            "edit_closed",
            {
                "date": format_date_ru(str(session.get("date") or "")),
                "time": clock_short(str(session.get("startTime") or "")),
                "end_date": format_date_ru(session_end_day(session) or str(session.get("date") or "")),
                "end_time": clock_short(str(session.get("endTime") or "")),
            },
            self._theme,
        )
        if not values:
            return
        self._run(
            lambda book: book.change_closed(
                session,
                values["date"],
                values["time"],
                values["end_date"],
                values["end_time"],
            )
        )

    def _run(self, action) -> None:
        self._status.set("Сохраняю…")

        def worker() -> None:
            kind, message = self.app.service.run_shift(action)
            from scanner_app.shifts import ShiftBook

            snapshot = ShiftBook(self.app.db, self.app.api).load()
            self._post(lambda: self._apply(snapshot, message if kind != "rejected" else ""))
            if kind == "rejected":
                self._post(lambda text=message: self._show_error(text))

        threading.Thread(target=worker, daemon=True).start()

    def _current_user(self) -> dict | None:
        selected = self.people.selection()
        if not selected:
            return None
        wanted = selected[0]
        for user in self._users:
            if str(user.get("id")) == wanted:
                self._selected_user = wanted
                return user
        return None

    def _selected_id(self) -> str:
        selected = self.people.selection()
        if selected:
            return selected[0]
        return str(self._selected_user or "")

    def _current_session(self) -> dict | None:
        user = self._current_user()
        selected = self.sessions.selection()
        if not user or not selected:
            return None
        for session in sessions_for_user(self._sessions, user.get("id")):
            if _session_iid(session) == selected[0]:
                return session
        return None

    def _paint_filters(self) -> None:
        theme = self._theme
        for value, label in _FILTERS:
            button = self._filter_buttons.get(value)
            if button is None:
                continue
            text = label
            if value != "all":
                text = f"{label}  {self._counts.get(value, 0)}"
            selected = self._filter.get() == value
            button.configure(
                text=text,
                bg=theme["button"] if selected else theme["card"],
                fg=theme["button_text"] if selected else theme["text"],
                activebackground=theme["button_hover"] if selected else theme["row"],
                activeforeground=theme["button_text"] if selected else theme["text"],
                highlightthickness=1,
                highlightbackground=theme["button"] if selected else theme["line"],
                highlightcolor=theme["button"] if selected else theme["line"],
            )

    def _close(self) -> None:
        self._hide_people_tip()
        tip = getattr(self, "_people_tip", None)
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass
            self._people_tip = None
        app = self.app
        if getattr(app, "_shifts", None) is self:
            app._shifts = None
        self.destroy()
        try:
            app._apply_window_mode()
        except tk.TclError:
            pass

    def _forget(self, event) -> None:
        if event.widget is not self:
            return
        if getattr(self.app, "_shifts", None) is self:
            self.app._shifts = None

    def _fit_people(self, event) -> None:
        if event.widget is not self.people or event.width < 200:
            return
        badge_w = 76
        state_w = 176
        name_w = max(140, event.width - badge_w - state_w - 24)
        self.people.column("badge", width=badge_w, minwidth=64, stretch=False)
        self.people.column("state", width=state_w, minwidth=160, stretch=False)
        self.people.column("name", width=name_w, minwidth=140, stretch=True)

    def _session_fixed_widths(self) -> tuple[int, int, int, int]:
        """Ширина даты, прихода, ухода и длительности. Лишнее остаётся у состояния."""
        cached = getattr(self, "_session_fixed", None)
        if cached:
            return cached
        style = ttk.Style(self)
        cell = tkfont.Font(self, font=style.lookup("Treeview", "font"))
        head = tkfont.Font(self, font=style.lookup("Treeview.Heading", "font"))

        def width(sample: str, title: str) -> int:
            # У ячейки внутренний отступ 4px с каждой стороны, у заголовка padding 3px.
            return max(cell.measure(sample) + 16, head.measure(title) + 14)

        self._session_fixed = (
            width("30.10.2026", "Дата"),
            width("08:40", "Приход"),
            width("14.10 10:00", "Уход"),
            width("12 ч 30 мин", "Длительность"),
        )
        return self._session_fixed

    def _fit_sessions(self, event) -> None:
        if event.widget is not self.sessions or event.width < 200:
            return
        date_w, start_w, end_w, length_w = self._session_fixed_widths()
        state_w = max(48, event.width - date_w - start_w - end_w - length_w - 4)
        fitted = (date_w, start_w, end_w, length_w, state_w)
        if fitted != getattr(self, "_session_fit", None):
            self._session_fit = fitted
            self.sessions.column("date", width=date_w, minwidth=date_w, stretch=False)
            self.sessions.column("start", width=start_w, minwidth=start_w, stretch=False)
            self.sessions.column("end", width=end_w, minwidth=end_w, stretch=False)
            self.sessions.column("length", width=length_w, minwidth=length_w, stretch=False)
            self.sessions.column("state", width=state_w, minwidth=48, stretch=True)
        if getattr(self, "session_hint", None) is not None:
            self.session_hint.configure(wraplength=max(240, event.width - 8))

    def _people_motion(self, event) -> None:
        row = self.people.identify_row(event.y)
        column = self.people.identify_column(event.x)
        if column != "#1" or not row:
            self._hide_people_tip()
            return
        name = str(self.people.set(row, "name") or "")
        if not name or name == "—":
            self._hide_people_tip()
            return
        try:
            font = tkfont.Font(font=(self.sans, 12))
            if font.measure(name) + 28 <= int(self.people.column("name", "width")):
                self._hide_people_tip()
                return
        except (tk.TclError, ValueError):
            pass
        self._show_people_tip(name, event)

    def _show_people_tip(self, text: str, event) -> None:
        theme = self._theme
        if self._people_tip is None:
            tip = tk.Toplevel(self)
            tip.wm_overrideredirect(True)
            try:
                tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            label = tk.Label(tip, justify="left", anchor="w", padx=8, pady=4, font=(self.sans, 12))
            label.pack()
            self._people_tip = tip
            self._people_tip_label = label
        if text != self._people_tip_text:
            self._people_tip_text = text
            self._people_tip_label.configure(text=text, bg=theme["card"], fg=theme["text"])
        x = self.people.winfo_rootx() + event.x + 14
        y = self.people.winfo_rooty() + event.y + 18
        self._people_tip.geometry(f"+{x}+{y}")
        try:
            self._people_tip.deiconify()
        except tk.TclError:
            self._people_tip = None

    def _hide_people_tip(self, _event=None) -> None:
        tip = getattr(self, "_people_tip", None)
        if tip is None:
            return
        try:
            tip.withdraw()
        except tk.TclError:
            self._people_tip = None

    def _show_error(self, message: str) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        messagebox.showerror("Смена", message, parent=self)

    def _post(self, callback) -> None:
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass


_FILTERS = (
    ("all", "Все"),
    (STATE_AT_WORK, "На работе"),
    (STATE_AWAY, "Не на работе"),
    (STATE_NEVER, "Ещё не сканировались"),
)


def _session_iid(session: dict) -> str:
    local_key = str(session.get("localKey") or "")
    if local_key:
        return "l:" + local_key
    return "s:" + str(session.get("id"))


def _tree_tuple(value) -> tuple:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _sync_tree(tree, rows: list) -> None:
    """Обновить только изменившиеся строки. Выбор остаётся, если эта строка ещё есть."""
    wanted = {iid for iid, _values, _tags in rows}
    for iid in list(tree.get_children()):
        if iid not in wanted:
            tree.delete(iid)
    for index, (iid, values, tags) in enumerate(rows):
        values = tuple(values)
        tags = tuple(tags)
        if tree.exists(iid):
            if _tree_tuple(tree.item(iid, "values")) != values or _tree_tuple(tree.item(iid, "tags")) != tags:
                tree.item(iid, values=values, tags=tags)
            if tree.index(iid) != index:
                tree.move(iid, "", index)
        else:
            tree.insert("", index, iid=iid, values=values, tags=tags)


def _ask_times(master, title: str, mode: str, initial: dict, theme: dict) -> dict | None:
    dialog = tk.Toplevel(master)
    dialog.title(title)
    dialog.transient(master)
    dialog.configure(bg=theme["bg"])
    dialog.resizable(False, False)
    result: dict = {}
    fields: dict[str, tk.StringVar] = {}

    def add(key: str, label: str, value: str) -> None:
        row = ttk.Frame(dialog)
        row.pack(fill="x", padx=18, pady=6)
        ttk.Label(row, text=label, width=22).pack(side="left")
        variable = tk.StringVar(value=value)
        ttk.Entry(row, textvariable=variable, width=24).pack(side="left")
        fields[key] = variable

    if mode == "end":
        ttk.Label(dialog, text="Приход: " + str(initial.get("arrival") or "—"), font=("", 12)).pack(anchor="w", padx=18, pady=(16, 6))
        add("end_date", "Дата ухода", str(initial.get("end_date") or ""))
        add("end_time", "Время ухода", str(initial.get("end_time") or ""))
    elif mode == "edit_closed":
        add("date", "Дата прихода", str(initial.get("date") or ""))
        add("time", "Время прихода", str(initial.get("time") or ""))
        add("end_date", "Дата ухода", str(initial.get("end_date") or ""))
        add("end_time", "Время ухода", str(initial.get("end_time") or ""))
    else:
        note = "Новая смена откроется с этим временем." if mode == "start" else "Меняется только время прихода. Уход здесь не закрывается."
        ttk.Label(dialog, text=note, wraplength=440, justify="left").pack(anchor="w", padx=18, pady=(16, 6))
        add("date", "Дата прихода", str(initial.get("date") or ""))
        add("time", "Время прихода", str(initial.get("time") or ""))

    buttons = ttk.Frame(dialog)
    buttons.pack(fill="x", padx=18, pady=16)

    def accept(_event=None) -> str:
        result.clear()
        result.update({key: variable.get().strip() for key, variable in fields.items()})
        dialog.destroy()
        return "break"

    def cancel(_event=None) -> str:
        dialog.destroy()
        return "break"

    ttk.Button(buttons, text="Сохранить", command=accept, style="Accent.TButton").pack(side="right")
    ttk.Button(buttons, text="Отмена", command=cancel).pack(side="right", padx=8)
    dialog.bind("<Return>", accept)
    dialog.bind("<Escape>", cancel)
    center_over(dialog, master)
    dialog.grab_set()
    dialog.wait_window()
    return result or None
