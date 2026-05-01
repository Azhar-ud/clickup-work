"""Per-ticket actions modal — view details, mutate, send to Claude.

Pushed from :class:`~clickup_work.picker.TicketPickerApp` when the user
presses ``a`` on the highlighted ticket. Lets them inspect details, change
status, set the time estimate, log time, and read or post comments without
leaving the picker. Choosing "send to Claude" exits the picker as if the
user had pressed Enter on the ticket.

The picker itself stays oblivious to ClickUp wiring: it only sees
:class:`ActionsContext`, which carries the API client and ids needed to
perform mutations.
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
import tempfile
import webbrowser
from dataclasses import dataclass, replace

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)

from clickup_work.clickup import (
    ClickUp,
    ClickUpError,
    Comment,
    Member,
    Task,
    TimeEntry,
)
from clickup_work.tui import EstimatePrompt, StatusPrompt


# ClickUp's priority field is a 1-4 int (or null for unset). Keep the
# user-facing label alongside so the priority modal can display both.
_PRIORITY_LABELS: tuple[tuple[str, int | None], ...] = (
    ("urgent", 1),
    ("high", 2),
    ("normal", 3),
    ("low", 4),
    ("none", None),
)
_PRIORITY_NAME_TO_NUM = {label: num for label, num in _PRIORITY_LABELS}


# Sentinel returned by TicketActionsScreen when the user wants to launch
# Claude on the (possibly mutated) task. Anything else (None included) means
# they backed out and the picker should stay open.
SEND_TO_CLAUDE = "send"


@dataclass(frozen=True)
class ActionsContext:
    """Dependencies the actions screen needs to do its job."""

    client: ClickUp
    team_id: str
    user_id: str


# ---------- helpers -------------------------------------------------------


def _format_ms_short(ms: int | None) -> str:
    """Render a duration in 'Xh Ym' / 'Xh' / 'Ym', or '—' for unset."""
    if not ms or ms <= 0:
        return "—"
    minutes = max(0, int(round(ms / 60_000)))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def _format_due(due_ms: int | None) -> str:
    if due_ms is None:
        return "—"
    today = dt.date.today()
    due = dt.datetime.fromtimestamp(due_ms / 1000).date()
    if due < today:
        return f"OVERDUE {due.isoformat()}"
    if due == today:
        return "today"
    if (due - today).days < 7:
        return due.strftime("%a %b %-d")
    return due.isoformat()


def _format_date(ms: int | None) -> str:
    """Plain ISO date for display (no relative shortcuts), '—' for unset."""
    if ms is None:
        return "—"
    return dt.datetime.fromtimestamp(ms / 1000).date().isoformat()


_CLEAR_TOKENS = ("clear", "none", "-", "remove", "unset")


def _parse_date_input(text: str) -> int | None:
    """Parse a user-typed date into epoch ms (UTC midnight), or None to clear.

    Accepts ``2026-05-10`` (ISO date), ``+3d`` / ``+1w`` / ``+2m`` (relative
    from today), ``today``, ``tomorrow``, and ``clear``/``none``/``-`` to
    indicate the field should be cleared. Empty input also means clear.

    Raises ``ValueError`` on anything else so the caller can show a one-line
    parse error.
    """
    raw = (text or "").strip().lower()
    if not raw or raw in _CLEAR_TOKENS:
        return None
    today = dt.date.today()
    if raw == "today":
        d = today
    elif raw == "tomorrow":
        d = today + dt.timedelta(days=1)
    elif raw.startswith("+") and len(raw) >= 3 and raw[-1] in "dwm" and raw[1:-1].isdigit():
        n = int(raw[1:-1])
        unit = raw[-1]
        if unit == "d":
            d = today + dt.timedelta(days=n)
        elif unit == "w":
            d = today + dt.timedelta(weeks=n)
        else:  # m — calendar months are messy; close-enough = 30d.
            d = today + dt.timedelta(days=n * 30)
    else:
        try:
            d = dt.date.fromisoformat(raw)
        except ValueError as e:
            raise ValueError(
                f"could not parse '{text}' — try 2026-05-10, +3d, today, "
                f"tomorrow, or 'clear'"
            ) from e
    return int(dt.datetime.combine(d, dt.time(0, 0)).timestamp() * 1000)


def _format_assignees(assignees: tuple[Member, ...]) -> str:
    """Show first three names, then '+N' for the rest. '—' for unassigned."""
    if not assignees:
        return "—"
    names = [a.username for a in assignees[:3]]
    rest = len(assignees) - 3
    base = ", ".join(names)
    if rest > 0:
        return f"{base}, +{rest}"
    return base


def _format_comment_when(ms: int | None) -> str:
    if ms is None:
        return ""
    when = dt.datetime.fromtimestamp(ms / 1000)
    today = dt.datetime.now().date()
    if when.date() == today:
        return when.strftime("today %H:%M")
    if (today - when.date()).days < 7:
        return when.strftime("%a %H:%M")
    return when.strftime("%Y-%m-%d")


# ---------- field prompts ------------------------------------------------


class RenamePrompt(ModalScreen[str | None]):
    """Edit the task name in-place. Returns the new name or None (cancel)."""

    DEFAULT_CSS = """
    RenamePrompt { align: center middle; }
    RenamePrompt > Vertical {
        width: 80; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    RenamePrompt Input { margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, current_name: str) -> None:
        super().__init__()
        self._current = current_name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Rename ticket[/]")
            yield Input(value=self._current, id="rename-input")
            yield Label("[dim]Enter to save · Esc to cancel[/]")

    @on(Input.Submitted, "#rename-input")
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        # Treat "no change" the same as cancel so we don't issue a redundant
        # PUT for users who hit Enter out of habit.
        if not text or text == self._current:
            self.dismiss(None)
            return
        self.dismiss(text)


class DatePrompt(ModalScreen[str | None]):
    """Returns the user's raw date input, or None on Esc.

    Empty input is a real submission (means "clear"); the caller parses the
    return string with :func:`_parse_date_input`.
    """

    DEFAULT_CSS = """
    DatePrompt { align: center middle; }
    DatePrompt > Vertical {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    DatePrompt Input { margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, label: str, current_ms: int | None) -> None:
        super().__init__()
        self._label = label
        self._current = _format_date(current_ms)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[bold]{self._label}[/]")
            yield Label(f"[dim]current: {self._current}[/]")
            yield Input(
                placeholder="2026-05-10 · +3d · today · tomorrow · clear",
                id="date-input",
            )
            yield Label("[dim]Enter to save · Esc to cancel · empty = clear[/]")

    @on(Input.Submitted, "#date-input")
    def _submit(self, event: Input.Submitted) -> None:
        # Note: empty string is intentional (means clear). Distinct from None
        # which the BINDING for Esc returns.
        self.dismiss(event.value.strip())


class PriorityPrompt(ModalScreen[str | None]):
    """Returns the chosen priority label ('urgent','high','normal','low','none')
    or None on cancel."""

    DEFAULT_CSS = """
    PriorityPrompt { align: center middle; }
    PriorityPrompt > Vertical {
        width: 50; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, ticket_name: str, current: str | None) -> None:
        super().__init__()
        self._ticket_name = ticket_name
        self._current = (current or "").lower()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[bold]Priority[/] · {self._ticket_name[:40]}")
            yield Label(f"[dim]current: {self._current or 'none'}[/]")
            options = [(label, label) for label, _ in _PRIORITY_LABELS]
            # Start blank so the mount-time Select.Changed doesn't auto-dismiss
            # the modal when the current priority matches one of the options.
            # (Same gotcha lives in StatusPrompt; we leave that one alone since
            # it's the pattern used in post_flow.)
            yield Select(options, prompt="-- pick a priority --", id="priority-select")
            yield Label("[dim]pick to apply · Esc to cancel[/]")

    @on(Select.Changed, "#priority-select")
    def _changed(self, event: Select.Changed) -> None:
        if event.value is not Select.NULL:
            self.dismiss(str(event.value))


class TagPrompt(ModalScreen[str | None]):
    """Free-text tag input. Submit toggles the tag (add if absent, remove if
    present). Returns the tag name to toggle, or None on cancel."""

    DEFAULT_CSS = """
    TagPrompt { align: center middle; }
    TagPrompt > Vertical {
        width: 70; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    TagPrompt Input { margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, current_tags: tuple[str, ...]) -> None:
        super().__init__()
        self._current_tags = current_tags

    def compose(self) -> ComposeResult:
        on_ticket = (
            ", ".join(f"#{t}" for t in self._current_tags)
            if self._current_tags
            else "—"
        )
        with Vertical():
            yield Label("[bold]Toggle tag[/]")
            yield Label(f"[dim]on this ticket: {on_ticket}[/]")
            yield Input(
                placeholder="tag name (must already exist in the space)",
                id="tag-input",
            )
            yield Label("[dim]Enter to toggle · Esc to cancel[/]")

    @on(Input.Submitted, "#tag-input")
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.dismiss(text or None)


# ---------- time entries -------------------------------------------------


class EditTimeEntryPrompt(ModalScreen[str | None]):
    """Edit the duration on an existing time entry. Returns input string or None."""

    DEFAULT_CSS = """
    EditTimeEntryPrompt { align: center middle; }
    EditTimeEntryPrompt > Vertical {
        width: 60; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    EditTimeEntryPrompt Input { margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, current_ms: int) -> None:
        super().__init__()
        self._current = _format_ms_short(current_ms)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Edit time entry duration[/]")
            yield Label(f"[dim]current: {self._current}[/]")
            yield Input(placeholder="e.g. 2h, 90m, 1h 30m", id="te-edit-input")
            yield Label("[dim]Enter to save · Esc to cancel[/]")

    @on(Input.Submitted, "#te-edit-input")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class ConfirmPrompt(ModalScreen[bool]):
    """y/n confirmation modal. Returns True only if user pressed 'y'."""

    DEFAULT_CSS = """
    ConfirmPrompt { align: center middle; }
    ConfirmPrompt > Vertical {
        width: 60; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    """

    BINDINGS = [
        Binding("y", "confirm", "yes"),
        Binding("n", "deny", "no", show=False),
        Binding("escape", "deny", "no", show=False),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._message)
            yield Label("[dim]y to confirm · n / Esc to cancel[/]")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class TimeEntriesScreen(ModalScreen[None]):
    """List time entries on the task; allow edit & delete on the highlighted row."""

    DEFAULT_CSS = """
    TimeEntriesScreen { align: center middle; }
    TimeEntriesScreen > Vertical {
        width: 100; height: 30; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    #te-header { height: auto; padding: 0 1; }
    #te-list { height: 1fr; padding: 0 1; margin-top: 1; border: round $surface; }
    #te-list:focus-within { border: round $accent; }
    .te-row { padding: 0 1; }
    #te-log { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "back"),
        Binding("q", "dismiss(None)", "back", show=False),
        Binding("e", "edit_entry", "edit"),
        Binding("d", "delete_entry", "delete"),
        Binding("r", "reload", "reload"),
    ]

    def __init__(self, task: Task, ctx: ActionsContext) -> None:
        super().__init__()
        self._ticket = task
        self._ctx = ctx
        self._entries: list[TimeEntry] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Time entries[/] · {self._ticket.name[:60]}  "
                f"[dim]· {self._ticket.id}[/]",
                id="te-header",
            )
            yield ListView(id="te-list")
            yield Static(
                "[dim]e edit · d delete · r reload · esc back[/]",
                id="te-log",
            )

    def on_mount(self) -> None:
        self._reload_entries()

    def _reload_entries(self) -> None:
        try:
            entries = self._ctx.client.get_task_time_entries(
                self._ctx.team_id, self._ticket.id,
            )
        except ClickUpError as e:
            self._set_log(f"[red]could not load entries:[/] {e}")
            return
        self._entries = entries
        self._render_entries()

    def _render_entries(self) -> None:
        listview = self.query_one("#te-list", ListView)
        listview.clear()
        if not self._entries:
            listview.append(
                ListItem(
                    Static(
                        "[dim]no time logged on this ticket yet[/]",
                        classes="te-row",
                    ),
                    disabled=True,
                )
            )
            return
        for e in self._entries:
            when = _format_comment_when(e.start_ms) or "—"
            dur = _format_ms_short(e.duration_ms)
            who = e.user or "—"
            desc = f" · {e.description}" if e.description else ""
            listview.append(
                ListItem(
                    Static(
                        f"[bold]{dur:<8}[/]  [dim]{when:<14}[/]  {who}{desc}",
                        classes="te-row",
                    ),
                )
            )
        listview.index = 0

    def _set_log(self, msg: str) -> None:
        self.query_one("#te-log", Static).update(msg)

    def _selected_entry(self) -> TimeEntry | None:
        listview = self.query_one("#te-list", ListView)
        idx = listview.index
        if idx is None or not self._entries:
            return None
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    def action_reload(self) -> None:
        self._reload_entries()

    def action_edit_entry(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return

        def after(value: str | None) -> None:
            if not value:
                return
            from clickup_work.cli import _parse_duration  # local: avoid circular

            ms = _parse_duration(value)
            if ms is None:
                self._set_log(f"[yellow]could not parse '{value}' as a duration[/]")
                return
            try:
                self._ctx.client.update_time_entry(
                    self._ctx.team_id, entry.id, {"duration": ms},
                )
            except ClickUpError as e:
                self._set_log(f"[red]edit failed:[/] {e}")
                return
            self._set_log(f"[green]✓ entry updated to {_format_ms_short(ms)}[/]")
            self._reload_entries()

        self.app.push_screen(EditTimeEntryPrompt(entry.duration_ms), after)

    def action_delete_entry(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return

        def confirmed(yes: bool) -> None:
            if not yes:
                return
            try:
                self._ctx.client.delete_time_entry(self._ctx.team_id, entry.id)
            except ClickUpError as e:
                self._set_log(f"[red]delete failed:[/] {e}")
                return
            self._set_log("[green]✓ entry deleted[/]")
            self._reload_entries()

        self.app.push_screen(
            ConfirmPrompt(
                f"Delete this entry? [bold]{_format_ms_short(entry.duration_ms)}[/] "
                f"by {entry.user or '—'}"
            ),
            confirmed,
        )


# ---------- assignees ----------------------------------------------------


class AssigneesScreen(ModalScreen[tuple[Member, ...] | None]):
    """List current assignees; add via member picker, remove the highlighted row.

    Returns the latest assignee tuple on dismiss so the parent screen can
    refresh its meta panel without refetching the task. Returns ``None`` only
    if the user backed out before mounting (defensive — normal flow always
    returns a tuple).
    """

    DEFAULT_CSS = """
    AssigneesScreen { align: center middle; }
    AssigneesScreen > Vertical {
        width: 80; height: 26; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    #asg-header { height: auto; padding: 0 1; }
    #asg-list { height: 1fr; padding: 0 1; margin-top: 1; border: round $surface; }
    #asg-list:focus-within { border: round $accent; }
    .asg-row { padding: 0 1; }
    #asg-log { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "back", "back", show=False),
        Binding("q", "back", "back"),
        Binding("a", "add_assignee", "add"),
        Binding("r", "remove_assignee", "remove"),
        Binding("R", "reload", "reload", show=False),
    ]

    def __init__(self, task: Task, ctx: ActionsContext) -> None:
        super().__init__()
        self._ticket = task
        self._ctx = ctx
        self._assignees: list[Member] = list(task.assignees)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Assignees[/] · {self._ticket.name[:60]}",
                id="asg-header",
            )
            yield ListView(id="asg-list")
            yield Static(
                "[dim]a add · r remove · q back[/]",
                id="asg-log",
            )

    def on_mount(self) -> None:
        self._render_list()

    def _render_list(self) -> None:
        listview = self.query_one("#asg-list", ListView)
        listview.clear()
        if not self._assignees:
            listview.append(
                ListItem(
                    Static(
                        "[dim]nobody assigned · press [bold]a[/] to add[/]",
                        classes="asg-row",
                    ),
                    disabled=True,
                )
            )
            return
        for m in self._assignees:
            you = "  [dim](you)[/]" if m.user_id == self._ctx.user_id else ""
            email = f"  [dim]<{m.email}>[/]" if m.email else ""
            listview.append(
                ListItem(
                    Static(f"{m.username}{email}{you}", classes="asg-row"),
                )
            )
        listview.index = 0

    def _set_log(self, msg: str) -> None:
        self.query_one("#asg-log", Static).update(msg)

    def _selected(self) -> Member | None:
        listview = self.query_one("#asg-list", ListView)
        idx = listview.index
        if idx is None or not self._assignees:
            return None
        if 0 <= idx < len(self._assignees):
            return self._assignees[idx]
        return None

    def action_back(self) -> None:
        self.dismiss(tuple(self._assignees))

    def action_reload(self) -> None:
        # No refetch path here — assignees come in on the parent Task. This
        # action only exists so the user has a way to redraw if the listview
        # gets out of sync (rare). Keep the no-op handler so the binding line
        # in CLAUDE.md stays accurate.
        self._render_list()

    def action_add_assignee(self) -> None:
        try:
            members = self._ctx.client.get_team_members(self._ctx.team_id)
        except ClickUpError as e:
            self._set_log(f"[red]could not load members:[/] {e}")
            return
        if not members:
            self._set_log("[yellow]no workspace members visible[/]")
            return

        # Reuse MemberPrompt from post_flow — same widget, same UX. Not
        # extracted to a shared module yet; let the third caller force that.
        from clickup_work.post_flow import MemberPrompt

        def after(chosen: Member | None) -> None:
            if chosen is None:
                return
            if any(a.user_id == chosen.user_id for a in self._assignees):
                self._set_log(f"[yellow]{chosen.username} already assigned[/]")
                return
            try:
                self._ctx.client.update_task_assignees(
                    self._ticket.id,
                    add_ids=[chosen.user_id],
                    remove_ids=[],
                )
            except ClickUpError as e:
                self._set_log(f"[red]add failed:[/] {e}")
                return
            self._assignees.append(chosen)
            self._render_list()
            self._set_log(f"[green]✓ added {chosen.username}[/]")

        self.app.push_screen(
            MemberPrompt(members, current_user_id=self._ctx.user_id),
            after,
        )

    def action_remove_assignee(self) -> None:
        m = self._selected()
        if m is None:
            return
        try:
            self._ctx.client.update_task_assignees(
                self._ticket.id,
                add_ids=[],
                remove_ids=[m.user_id],
            )
        except ClickUpError as e:
            self._set_log(f"[red]remove failed:[/] {e}")
            return
        self._assignees = [a for a in self._assignees if a.user_id != m.user_id]
        self._render_list()
        self._set_log(f"[green]✓ removed {m.username}[/]")


# ---------- subtasks -----------------------------------------------------


class SubtaskNamePrompt(ModalScreen[str | None]):
    """Single-line input for a new subtask title."""

    DEFAULT_CSS = """
    SubtaskNamePrompt { align: center middle; }
    SubtaskNamePrompt > Vertical {
        width: 80; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    SubtaskNamePrompt Input { margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]New subtask[/]")
            yield Input(placeholder="subtask title…", id="subtask-name")
            yield Label("[dim]Enter to create · Esc to cancel[/]")

    @on(Input.Submitted, "#subtask-name")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class SubtasksScreen(ModalScreen[None]):
    """List subtasks, create new ones, change status on the highlighted row.

    Mutating subtask fields beyond status is intentionally out of scope for
    v1 — pushing a nested ``TicketActionsScreen`` works but the nested
    ``g`` send-to-Claude binding would be ambiguous. Use the picker after
    the subtask appears in your assignment list.
    """

    DEFAULT_CSS = """
    SubtasksScreen { align: center middle; }
    SubtasksScreen > Vertical {
        width: 100; height: 30; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    #st-header { height: auto; padding: 0 1; }
    #st-list { height: 1fr; padding: 0 1; margin-top: 1; border: round $surface; }
    #st-list:focus-within { border: round $accent; }
    .st-row { padding: 0 1; }
    #st-log { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "back"),
        Binding("q", "dismiss(None)", "back", show=False),
        Binding("n", "new_subtask", "new"),
        Binding("s", "set_status", "status"),
        Binding("o", "open_browser", "open"),
        Binding("R", "reload", "reload"),
    ]

    def __init__(self, task: Task, ctx: ActionsContext) -> None:
        super().__init__()
        self._ticket = task
        self._ctx = ctx
        self._subtasks: list[Task] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Subtasks[/] · {self._ticket.name[:60]}  "
                f"[dim]· {self._ticket.id}[/]",
                id="st-header",
            )
            yield ListView(id="st-list")
            yield Static(
                "[dim]n new · s status · o open · R reload · esc back[/]",
                id="st-log",
            )

    def on_mount(self) -> None:
        self._reload_subtasks()

    def _reload_subtasks(self) -> None:
        try:
            subs = self._ctx.client.get_subtasks(self._ticket.id)
        except ClickUpError as e:
            self._set_log(f"[red]could not load:[/] {e}")
            return
        self._subtasks = subs
        self._render_list()

    def _render_list(self) -> None:
        listview = self.query_one("#st-list", ListView)
        listview.clear()
        if not self._subtasks:
            listview.append(
                ListItem(
                    Static(
                        "[dim]no subtasks · press [bold]n[/] to create one[/]",
                        classes="st-row",
                    ),
                    disabled=True,
                )
            )
            return
        for sub in self._subtasks:
            status = (sub.status or "—").strip()
            name = sub.name if len(sub.name) < 60 else sub.name[:59] + "…"
            line = (
                f"[dim]{sub.id[:8]:>8}[/]  "
                f"[dim]{status:<14}[/]  {name}"
            )
            listview.append(ListItem(Static(line, classes="st-row")))
        listview.index = 0

    def _set_log(self, msg: str) -> None:
        self.query_one("#st-log", Static).update(msg)

    def _selected(self) -> Task | None:
        listview = self.query_one("#st-list", ListView)
        idx = listview.index
        if idx is None or not self._subtasks:
            return None
        if 0 <= idx < len(self._subtasks):
            return self._subtasks[idx]
        return None

    def action_reload(self) -> None:
        self._reload_subtasks()

    def action_new_subtask(self) -> None:
        def after(name: str | None) -> None:
            if not name:
                return
            try:
                created = self._ctx.client.create_subtask(
                    self._ticket.list_id, self._ticket.id, name,
                )
            except ClickUpError as e:
                self._set_log(f"[red]create failed:[/] {e}")
                return
            self._subtasks.append(created)
            self._render_list()
            self._set_log(f"[green]✓ created subtask '{name}'[/]")

        self.app.push_screen(SubtaskNamePrompt(), after)

    def action_set_status(self) -> None:
        sub = self._selected()
        if sub is None:
            return
        if not sub.list_id:
            self._set_log("[yellow]subtask has no list_id; cannot fetch statuses[/]")
            return
        try:
            statuses = self._ctx.client.get_list_statuses(sub.list_id)
        except ClickUpError as e:
            self._set_log(f"[red]could not load statuses:[/] {e}")
            return
        if not statuses:
            self._set_log("[yellow]no statuses configured on this list[/]")
            return

        def after(value: str | None) -> None:
            if not value or value.lower() == sub.status.lower():
                return
            try:
                self._ctx.client.update_task_status(sub.id, value)
            except ClickUpError as e:
                self._set_log(f"[red]status update failed:[/] {e}")
                return
            # Replace the local row in-place so reload-free navigation stays
            # accurate.
            for i, s in enumerate(self._subtasks):
                if s.id == sub.id:
                    self._subtasks[i] = replace(s, status=value)
                    break
            self._render_list()
            self._set_log(f"[green]✓ {sub.id} → {value}[/]")

        self.app.push_screen(
            StatusPrompt(sub.name, sub.status, statuses),
            after,
        )

    def action_open_browser(self) -> None:
        sub = self._selected()
        if sub is None or not sub.url:
            self._set_log("[yellow]no URL on this subtask[/]")
            return
        webbrowser.open(sub.url)
        self._set_log(f"opened {sub.id}")


# ---------- comments ------------------------------------------------------


class CommentComposeScreen(ModalScreen[str | None]):
    """Single-line input for posting a new comment. Returns the text or None."""

    DEFAULT_CSS = """
    CommentComposeScreen { align: center middle; }
    CommentComposeScreen > Vertical {
        width: 80; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    CommentComposeScreen Input { margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, ticket_name: str) -> None:
        super().__init__()
        self._ticket_name = ticket_name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[bold]Add comment[/] · {self._ticket_name[:48]}")
            yield Input(placeholder="comment text…", id="comment-input")
            yield Label("[dim]Enter to post · Esc to cancel[/]")

    @on(Input.Submitted, "#comment-input")
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.dismiss(text or None)


class CommentsScreen(ModalScreen[None]):
    """Read-only list of comments, with ``n`` to add a new one."""

    DEFAULT_CSS = """
    CommentsScreen { align: center middle; }
    CommentsScreen > Vertical {
        width: 100; height: 32; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    #cs-header { height: auto; padding: 0 1; }
    #cs-list {
        height: 1fr; padding: 0 1; margin-top: 1;
        border: round $surface;
    }
    #cs-log { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "back"),
        Binding("n", "new_comment", "new comment"),
        Binding("r", "reload", "reload"),
    ]

    def __init__(self, task: Task, ctx: ActionsContext) -> None:
        super().__init__()
        self._ticket = task
        self._ctx = ctx
        self._status_msg = ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Comments[/] · {self._ticket.name[:60]}  [dim]· {self._ticket.id}[/]",
                id="cs-header",
            )
            with VerticalScroll(id="cs-list"):
                yield Static("[dim]loading…[/]", id="cs-body")
            yield Static(
                "[dim]n new comment · r reload · esc back[/]",
                id="cs-log",
            )

    def on_mount(self) -> None:
        self._reload()

    def _reload(self) -> None:
        try:
            comments = self._ctx.client.get_task_comments(self._ticket.id)
        except ClickUpError as e:
            self.query_one("#cs-body", Static).update(
                f"[red]could not load comments:[/] {e}"
            )
            return
        self._render_comments(comments)

    def _render_comments(self, comments: list[Comment]) -> None:
        if not comments:
            self.query_one("#cs-body", Static).update(
                "[dim]no comments on this ticket yet — press [bold]n[/] to add one[/]"
            )
            return
        lines: list[str] = []
        for c in comments:
            when = _format_comment_when(c.created_ms)
            header = f"[bold]{c.author}[/]"
            if when:
                header += f"  [dim]· {when}[/]"
            text = c.text or "[dim](empty)[/]"
            lines.append(header)
            lines.append(text)
            lines.append("")
        self.query_one("#cs-body", Static).update("\n".join(lines).rstrip())

    def action_reload(self) -> None:
        self._reload()

    def action_new_comment(self) -> None:
        def after(text: str | None) -> None:
            if not text:
                return
            try:
                self._ctx.client.create_task_comment(self._ticket.id, text)
            except ClickUpError as e:
                self.query_one("#cs-log", Static).update(
                    f"[red]post failed:[/] {e}"
                )
                return
            self.query_one("#cs-log", Static).update(
                "[green]✓ comment posted · r to reload[/]"
            )
            self._reload()

        self.app.push_screen(CommentComposeScreen(self._ticket.name), after)


# ---------- main actions screen ------------------------------------------


class TicketActionsScreen(ModalScreen[str | None]):
    """Modal showing ticket details + an action menu.

    Returns :data:`SEND_TO_CLAUDE` when the user picks "send to Claude" so the
    picker can exit with this task; ``None`` for any other dismissal (Esc /
    back), which keeps the picker open.
    """

    DEFAULT_CSS = """
    TicketActionsScreen { align: center middle; }
    TicketActionsScreen > Vertical {
        width: 100; height: 36; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    #ta-header { height: auto; padding: 0 1; }
    #ta-meta { height: auto; padding: 0 1; margin-top: 1; }
    #ta-desc-wrap {
        height: 1fr; margin-top: 1; padding: 0 1;
        border: round $surface;
    }
    #ta-log { height: 1; padding: 0 1; color: $text-muted; margin-top: 1; }
    #ta-help { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("escape", "back", "back", show=False),
        Binding("q", "back", "back"),
        Binding("g", "send", "send to Claude"),
        Binding("s", "set_status", "status"),
        Binding("p", "set_priority", "priority"),
        Binding("d", "set_due_date", "due"),
        Binding("b", "set_start_date", "start"),
        Binding("e", "set_estimate", "estimate"),
        Binding("t", "track_time", "track time"),
        Binding("r", "rename", "rename"),
        Binding("D", "edit_description", "description"),
        Binding("T", "toggle_tag", "tags"),
        Binding("c", "view_comments", "comments"),
        Binding("H", "view_time_entries", "history"),
        Binding("A", "manage_assignees", "assignees"),
        Binding("S", "manage_subtasks", "subtasks"),
        Binding("o", "open_browser", "open"),
    ]

    def __init__(self, task: Task, ctx: ActionsContext) -> None:
        super().__init__()
        self._ticket = task
        self._ctx = ctx
        self._log_msg = ""

    # ------- compose / lifecycle ------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._render_header(), id="ta-header")
            yield Static(self._render_meta(), id="ta-meta")
            with VerticalScroll(id="ta-desc-wrap"):
                yield Static(self._render_description(), id="ta-desc")
            yield Static("", id="ta-log")
            yield Static(
                "[dim]g send · s status · p priority · d due · b start · "
                "e estimate · t track[/]\n"
                "[dim]r rename · D description · T tags · A assignees · "
                "S subtasks · c comments · H history · o open · q back[/]",
                id="ta-help",
            )
            yield Footer()

    # ------- rendering ----------------------------------------------------

    def _render_header(self) -> str:
        return (
            f"[bold]{self._ticket.name}[/]  [dim]· {self._ticket.id}[/]"
        )

    def _render_meta(self) -> str:
        loc = ""
        if self._ticket.folder_name and self._ticket.list_name:
            loc = f"{self._ticket.folder_name} / {self._ticket.list_name}"
        elif self._ticket.list_name:
            loc = self._ticket.list_name
        priority = self._ticket.priority or "—"
        status = self._ticket.status or "—"
        due = _format_due(self._ticket.due_date)
        start = _format_date(self._ticket.start_date)
        est = _format_ms_short(self._ticket.time_estimate)
        tags = " ".join(f"#{t}" for t in self._ticket.tags) if self._ticket.tags else "—"
        assignees = _format_assignees(self._ticket.assignees)
        return (
            f"[dim]status:[/]    {status}\n"
            f"[dim]priority:[/]  {priority}\n"
            f"[dim]due:[/]       {due}\n"
            f"[dim]start:[/]     {start}\n"
            f"[dim]estimate:[/]  {est}\n"
            f"[dim]assignees:[/] {assignees}\n"
            f"[dim]location:[/]  {loc or '—'}\n"
            f"[dim]tags:[/]      {tags}"
        )

    def _render_description(self) -> str:
        if not self._ticket.description:
            return "[dim](no description)[/]"
        return self._ticket.description

    def _refresh_panels(self) -> None:
        self.query_one("#ta-header", Static).update(self._render_header())
        self.query_one("#ta-meta", Static).update(self._render_meta())
        self.query_one("#ta-desc", Static).update(self._render_description())

    def _set_log(self, msg: str) -> None:
        self._log_msg = msg
        self.query_one("#ta-log", Static).update(msg)

    # ------- actions ------------------------------------------------------

    def action_back(self) -> None:
        self.dismiss(None)

    def action_send(self) -> None:
        self.dismiss(SEND_TO_CLAUDE)

    def action_open_browser(self) -> None:
        if not self._ticket.url:
            self._set_log("[yellow]no URL on this ticket[/]")
            return
        webbrowser.open(self._ticket.url)
        self._set_log(f"opened {self._ticket.id} in browser")

    def action_set_status(self) -> None:
        try:
            statuses = self._ctx.client.get_list_statuses(self._ticket.list_id)
        except ClickUpError as e:
            self._set_log(f"[red]could not load statuses:[/] {e}")
            return
        if not statuses:
            self._set_log("[yellow]no statuses configured on this list[/]")
            return

        def after(value: str | None) -> None:
            if not value or value.lower() == self._ticket.status.lower():
                return
            try:
                self._ctx.client.update_task_status(self._ticket.id, value)
            except ClickUpError as e:
                self._set_log(f"[red]status update failed:[/] {e}")
                return
            self._ticket = replace(self._ticket, status=value)
            self._refresh_panels()
            self._set_log(f"[green]✓ moved to {value}[/]")

        self.app.push_screen(
            StatusPrompt(self._ticket.name, self._ticket.status, statuses),
            after,
        )

    def action_set_estimate(self) -> None:
        def after(value: str | None) -> None:
            if not value:
                return
            from clickup_work.cli import _parse_duration  # local import: avoid circular

            ms = _parse_duration(value)
            if ms is None:
                self._set_log(f"[yellow]could not parse '{value}' as a duration[/]")
                return
            try:
                self._ctx.client.set_time_estimate(self._ticket.id, ms)
            except ClickUpError as e:
                self._set_log(f"[red]estimate failed:[/] {e}")
                return
            self._ticket = replace(self._ticket, time_estimate=ms)
            self._refresh_panels()
            self._set_log(f"[green]✓ estimate set to {_format_ms_short(ms)}[/]")

        self.app.push_screen(
            EstimatePrompt(self._ticket.name, self._ticket.time_estimate),
            after,
        )

    def action_track_time(self) -> None:
        def after(value: str | None) -> None:
            if not value:
                return
            from clickup_work.cli import _parse_duration  # local import: avoid circular

            ms = _parse_duration(value)
            if ms is None:
                self._set_log(f"[yellow]could not parse '{value}' as a duration[/]")
                return
            try:
                self._ctx.client.add_time_entry(
                    self._ctx.team_id, self._ctx.user_id, self._ticket.id, ms,
                )
            except ClickUpError as e:
                self._set_log(f"[red]time log failed:[/] {e}")
                return
            self._set_log(f"[green]✓ logged {_format_ms_short(ms)} of time[/]")

        self.app.push_screen(
            EstimatePrompt(f"⏱  Track time SPENT — {self._ticket.name}", None),
            after,
        )

    def action_view_comments(self) -> None:
        self.app.push_screen(CommentsScreen(self._ticket, self._ctx))

    def action_view_time_entries(self) -> None:
        self.app.push_screen(TimeEntriesScreen(self._ticket, self._ctx))

    def action_manage_assignees(self) -> None:
        def after(updated: tuple[Member, ...] | None) -> None:
            if updated is None or updated == self._ticket.assignees:
                return
            self._ticket = replace(self._ticket, assignees=updated)
            self._refresh_panels()
            self._set_log(
                f"[green]✓ assignees updated ({len(updated)} on ticket)[/]"
            )

        self.app.push_screen(AssigneesScreen(self._ticket, self._ctx), after)

    def action_manage_subtasks(self) -> None:
        if not self._ticket.list_id:
            self._set_log("[yellow]ticket has no list_id; cannot list subtasks[/]")
            return
        self.app.push_screen(SubtasksScreen(self._ticket, self._ctx))

    def action_rename(self) -> None:
        def after(value: str | None) -> None:
            if not value:
                return
            try:
                self._ctx.client.update_task_fields(
                    self._ticket.id, {"name": value},
                )
            except ClickUpError as e:
                self._set_log(f"[red]rename failed:[/] {e}")
                return
            self._ticket = replace(self._ticket, name=value)
            self._refresh_panels()
            self._set_log("[green]✓ ticket renamed[/]")

        self.app.push_screen(RenamePrompt(self._ticket.name), after)

    def action_set_priority(self) -> None:
        def after(label: str | None) -> None:
            if not label or label.lower() == (self._ticket.priority or "").lower():
                return
            num = _PRIORITY_NAME_TO_NUM.get(label.lower())
            # ClickUp treats null as "no priority" — preserved through JSON.
            try:
                self._ctx.client.update_task_fields(
                    self._ticket.id, {"priority": num},
                )
            except ClickUpError as e:
                self._set_log(f"[red]priority update failed:[/] {e}")
                return
            new_priority = None if label.lower() == "none" else label.lower()
            self._ticket = replace(self._ticket, priority=new_priority)
            self._refresh_panels()
            self._set_log(f"[green]✓ priority set to {label}[/]")

        self.app.push_screen(
            PriorityPrompt(self._ticket.name, self._ticket.priority),
            after,
        )

    def action_set_due_date(self) -> None:
        self._set_date("due_date", "Set due date", self._ticket.due_date)

    def action_set_start_date(self) -> None:
        self._set_date("start_date", "Set start date", self._ticket.start_date)

    def _set_date(self, field: str, label: str, current_ms: int | None) -> None:
        """Shared modal+update path for due_date and start_date.

        ``field`` is the ClickUp PUT body key. The modal returns:
          - ``None``        → cancel (Esc)
          - ``""``          → clear (empty submit)
          - any other text  → parse via :func:`_parse_date_input`
        """

        def after(value: str | None) -> None:
            if value is None:
                return  # Esc
            try:
                ms = _parse_date_input(value)
            except ValueError as e:
                self._set_log(f"[yellow]{e}[/]")
                return
            try:
                self._ctx.client.update_task_fields(
                    self._ticket.id, {field: ms},
                )
            except ClickUpError as e:
                self._set_log(f"[red]{field} update failed:[/] {e}")
                return
            kw = {field: ms}
            self._ticket = replace(self._ticket, **kw)
            self._refresh_panels()
            label_human = "cleared" if ms is None else _format_date(ms)
            self._set_log(f"[green]✓ {field.replace('_', ' ')} {label_human}[/]")

        self.app.push_screen(DatePrompt(label, current_ms), after)

    def action_toggle_tag(self) -> None:
        def after(tag: str | None) -> None:
            if not tag:
                return
            currently = tag in self._ticket.tags
            try:
                if currently:
                    self._ctx.client.remove_task_tag(self._ticket.id, tag)
                else:
                    self._ctx.client.add_task_tag(self._ticket.id, tag)
            except ClickUpError as e:
                self._set_log(f"[red]tag update failed:[/] {e}")
                return
            new_tags = (
                tuple(t for t in self._ticket.tags if t != tag)
                if currently
                else self._ticket.tags + (tag,)
            )
            self._ticket = replace(self._ticket, tags=new_tags)
            self._refresh_panels()
            verb = "removed" if currently else "added"
            self._set_log(f"[green]✓ tag '{tag}' {verb}[/]")

        self.app.push_screen(TagPrompt(self._ticket.tags), after)

    def action_edit_description(self) -> None:
        """Open ``$EDITOR`` on the description, send the diff back to ClickUp.

        Uses :meth:`textual.app.App.suspend` so the terminal is released to
        the editor (typically vim/nano) and restored cleanly afterwards.
        """
        editor = (
            os.environ.get("EDITOR")
            or shutil.which("nano")
            or shutil.which("vim")
            or shutil.which("vi")
        )
        if not editor:
            self._set_log(
                "[yellow]no $EDITOR set and no nano/vim/vi on PATH[/]"
            )
            return

        initial = self._ticket.description or ""
        # `.md` suffix so editors load markdown highlighting; ClickUp treats
        # the `description` field as markdown.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(initial)
            path = tf.name

        try:
            with self.app.suspend():
                subprocess.run([editor, path], check=False)
            with open(path, encoding="utf-8") as f:
                new = f.read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

        # Strip a single trailing newline most editors append; preserve
        # everything else (intentional blank lines, indentation).
        if new.endswith("\n") and not initial.endswith("\n"):
            new = new[:-1]

        if new == initial:
            self._set_log("[dim]description unchanged[/]")
            return

        try:
            self._ctx.client.update_task_fields(
                self._ticket.id, {"description": new},
            )
        except ClickUpError as e:
            self._set_log(f"[red]description update failed:[/] {e}")
            return

        self._ticket = replace(self._ticket, description=new)
        self._refresh_panels()
        self._set_log("[green]✓ description updated[/]")
