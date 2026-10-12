from __future__ import annotations

import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk

from scanner_app.fonts import faces
from scanner_app.shifts import (
    STATE_AT_WORK,
    STATE_AWAY,
    STATE_LABELS,
    STATE_NEVER,
    clock_short,
    format_date_ru,
    format_duration,
    is_open,
    person_name,
    person_state,
    sessions_for_user,
)
from scanner_app.theme import theme_by_name


class ShiftsWindow(tk.Toplevel):
    """Кто на работе, и ручная правка смен. Список берётся с сервера целиком."""

    def __init__(self, master, app) -> None:
        super().__init__(master)
        self.app = app
        self.title("Смены")
        self.minsize(920, 560)
        width = min(1180, master.winfo_screenwidth() - 80)
        height = min(760, master.winfo_screenheight() - 80)
        self.geometry(f"{width}x{height}+{master.winfo_rootx() + 40}+{master.winfo_rooty() + 40}")
        self._theme = theme_by_name((app.settings.get("ui") or {}).get("theme") or "dark")
        _serif, _bold, self.sans = faces(self)
        self._users: list[dict] = []
        self._sessions: list[dict] = []
        self._selected_user = None
        self._filter = tk.StringVar(value="all")
        self._query = tk.StringVar(value="")
        self._status = tk.StringVar(value="Загружаю сотрудников и смены…")
        self._style()
        self._build()
        self._query.trace_add("write", lambda *_args: self._fill_people())
        self._filter.trace_add("write", lambda *_args: self._fill_people())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._reload()

    def _style(self) -> None:
        theme = self._theme
        self.configure(bg=theme["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        sans = self.sans
        bg = theme["bg"]
        fg = theme["text"]
        style.configure(".", background=bg, foreground=fg, font=(sans, 11))
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg, font=(sans, 11))
        style.configure("Treeview", background=theme["entry"], fieldbackground=theme["entry"], foreground=fg, font=(sans, 11), rowheight=28)
        style.map("Treeview", background=[("selected", theme["button"])], foreground=[("selected", theme["button_text"])])
        style.configure("Treeview.Heading", background=theme["header"], foreground=fg, font=(sans, 11, "bold"))
        style.configure("TButton", padding=(12, 6), background=theme["button"], foreground=theme["button_text"], font=(sans, 11), borderwidth=0)
        style.map("TButton", background=[("active", theme["button_hover"])])
        style.configure("TEntry", fieldbackground=theme["entry"], foreground=fg, font=(sans, 11))

    def _build(self) -> None:
        theme = self._theme
        top = ttk.Frame(self)
        top.pack(fill="x", padx=12, pady=(10, 4))
        ttk.Label(top, textvariable=self._status, wraplength=900, justify="left").pack(anchor="w")
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", padx=12, pady=4)
        ttk.Button(buttons, text="Обновить с сервера", command=self._reload).pack(side="left")
        ttk.Button(buttons, text="Отправить очередь", command=self._flush).pack(side="left", padx=8)
        for value, label in (
            ("all", "Все"),
            (STATE_AT_WORK, "На работе"),
            (STATE_AWAY, "Не на работе"),
            (STATE_NEVER, "Ещё не сканировался"),
        ):
            ttk.Radiobutton(buttons, text=label, value=value, variable=self._filter).pack(side="left", padx=(8, 0))
        search = ttk.Frame(self)
        search.pack(fill="x", padx=12, pady=4)
        ttk.Label(search, text="Поиск").pack(side="left")
        ttk.Entry(search, textvariable=self._query, width=40).pack(side="left", padx=8)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=12, pady=8)
        left = ttk.Frame(pane)
        right = ttk.Frame(pane)
        pane.add(left, weight=3)
        pane.add(right, weight=4)

        self.people = ttk.Treeview(left, columns=("name", "badge", "state"), show="headings", height=16)
        self.people.heading("name", text="Сотрудник")
        self.people.heading("badge", text="Бейдж")
        self.people.heading("state", text="Сейчас")
        self.people.column("name", width=220, anchor="w")
        self.people.column("badge", width=110, anchor="w")
        self.people.column("state", width=170, anchor="w")
        self.people.pack(fill="both", expand=True)
        self.people.bind("<<TreeviewSelect>>", lambda _event: self._fill_sessions())

        ttk.Label(right, text="Смены выбранного сотрудника").pack(anchor="w")
        self.sessions = ttk.Treeview(right, columns=("date", "start", "end", "length", "state"), show="headings", height=12)
        for column, title, width in (
            ("date", "Дата", 110),
            ("start", "Приход", 80),
            ("end", "Уход", 80),
            ("length", "Длительность", 110),
            ("state", "Состояние", 180),
        ):
            self.sessions.heading(column, text=title)
            self.sessions.column(column, width=width, anchor="w")
        self.sessions.pack(fill="both", expand=True, pady=4)
        self.sessions.bind("<Double-1>", lambda _event: self._edit())

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(4, 0))
        ttk.Button(actions, text="Начать смену", command=self._start).pack(side="left")
        ttk.Button(actions, text="Завершить смену", command=self._finish).pack(side="left", padx=8)
        ttk.Button(actions, text="Изменить смену", command=self._edit).pack(side="left")
        ttk.Label(
            right,
            text="У идущей смены меняется только приход. У завершённой — приход и уход. Время можно задать своё.",
            wraplength=520,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

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
        self._users = list(snapshot.get("users") or [])
        self._sessions = list(snapshot.get("sessions") or [])
        counts = {STATE_AT_WORK: 0, STATE_AWAY: 0, STATE_NEVER: 0}
        for user in self._users:
            counts[person_state(sessions_for_user(self._sessions, user.get("id")))] += 1
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
        children = self.people.get_children()
        if children:
            self.people.delete(*children)
        chosen = None
        for user, state, name, badge in rows:
            iid = str(user.get("id"))
            self.people.insert("", "end", iid=iid, values=(name, badge, STATE_LABELS.get(state, state)))
            if iid == selected:
                chosen = iid
        if chosen:
            self.people.selection_set(chosen)
            self.people.see(chosen)
        elif rows:
            first = str(rows[0][0].get("id"))
            self.people.selection_set(first)
        self._fill_sessions()

    def _fill_sessions(self) -> None:
        user = self._current_user()
        children = self.sessions.get_children()
        if children:
            self.sessions.delete(*children)
        if not user:
            return
        for session in sessions_for_user(self._sessions, user.get("id")):
            iid = _session_iid(session)
            if is_open(session):
                state = "На работе"
                length = "идёт"
            else:
                state = "Завершена"
                length = format_duration(session.get("durationMinutes")) or "—"
            if session.get("pending"):
                state += ", ждёт отправки"
            self.sessions.insert(
                "",
                "end",
                iid=iid,
                values=(
                    format_date_ru(str(session.get("date") or "")),
                    clock_short(str(session.get("startTime") or "")) or "—",
                    clock_short(str(session.get("endTime") or "")) or "—",
                    length,
                    state,
                ),
            )

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
                "end_date": format_date_ru(str(session.get("endDate") or session.get("date") or "")),
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
                self._post(lambda: messagebox.showerror("Смена", message, parent=self))

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

    def _post(self, callback) -> None:
        try:
            self.after(0, callback)
        except (tk.TclError, RuntimeError):
            pass


def _session_iid(session: dict) -> str:
    local_key = str(session.get("localKey") or "")
    if local_key:
        return "l:" + local_key
    return "s:" + str(session.get("id"))


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
        row.pack(fill="x", padx=12, pady=4)
        ttk.Label(row, text=label, width=22).pack(side="left")
        variable = tk.StringVar(value=value)
        ttk.Entry(row, textvariable=variable, width=24).pack(side="left")
        fields[key] = variable

    if mode == "end":
        ttk.Label(dialog, text="Приход: " + str(initial.get("arrival") or "—")).pack(anchor="w", padx=12, pady=(12, 4))
        add("end_date", "Дата ухода", str(initial.get("end_date") or ""))
        add("end_time", "Время ухода", str(initial.get("end_time") or ""))
    elif mode == "edit_closed":
        add("date", "Дата прихода", str(initial.get("date") or ""))
        add("time", "Время прихода", str(initial.get("time") or ""))
        add("end_date", "Дата ухода", str(initial.get("end_date") or ""))
        add("end_time", "Время ухода", str(initial.get("end_time") or ""))
    else:
        note = "Новая смена откроется с этим временем." if mode == "start" else "Меняется только время прихода. Уход здесь не закрывается."
        ttk.Label(dialog, text=note, wraplength=420, justify="left").pack(anchor="w", padx=12, pady=(12, 4))
        add("date", "Дата прихода", str(initial.get("date") or ""))
        add("time", "Время прихода", str(initial.get("time") or ""))

    buttons = ttk.Frame(dialog)
    buttons.pack(fill="x", padx=12, pady=12)

    def accept() -> None:
        result.clear()
        result.update({key: variable.get().strip() for key, variable in fields.items()})
        dialog.destroy()

    ttk.Button(buttons, text="Сохранить", command=accept).pack(side="right")
    ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right", padx=8)
    dialog.grab_set()
    dialog.wait_window()
    return result or None
