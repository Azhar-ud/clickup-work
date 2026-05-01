"""Textual ticket picker — replaces the fzf/numbered pickers.

Used by ``clickup-work`` (the main flow) when stdout/stdin are TTYs.
Falls back to the numbered picker for pipes/CI; fzf is no longer used.

Returns the selected :class:`~clickup_work.clickup.Task` or ``None`` on
cancel (q / Esc).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, ListItem, ListView, Static

from clickup_work.actions_screen import (
    SEND_TO_CLAUDE,
    ActionsContext,
    TicketActionsScreen,
)
from clickup_work.clickup import Task
from clickup_work.themes import OmnitrixBanner, apply_theme

_PRIORITY_COLOR = {
    "urgent": "bold red",
    "high": "yellow",
    "normal": "",
    "low": "dim",
}


@dataclass
class _Row:
    """One non-header row in the picker list."""

    task: Task
    location: str
    tags: str
    haystack: str  # lower-cased blob used for filter matching


def _row_haystack(task: Task, location: str) -> str:
    parts = [
        task.name,
        task.status or "",
        task.priority or "",
        location,
        " ".join(task.tags or ()),
        task.list_name or "",
        task.folder_name or "",
        task.space_name or "",
    ]
    return " ".join(parts).lower()


def _location_label(task: Task) -> str:
    """Compose 'Folder / List' breadcrumb when possible, falling back gracefully."""
    if task.folder_name and task.list_name:
        return f"{task.folder_name} / {task.list_name}"
    if task.space_name and task.list_name:
        return f"{task.space_name} / {task.list_name}"
    if task.list_name:
        return task.list_name
    return ""


def _build_rows(tasks: list[Task]) -> list[_Row]:
    rows: list[_Row] = []
    for t in tasks:
        loc = _location_label(t)
        tags = " ".join(f"#{tg}" for tg in t.tags) if t.tags else ""
        rows.append(_Row(task=t, location=loc, tags=tags,
                         haystack=_row_haystack(t, loc)))
    return rows


def _format_row_line(row: _Row, *, show_location: bool) -> str:
    """Rich-markup line matching the look of the workload TUI rows."""
    pr = row.task.priority or "—"
    pr_color = _PRIORITY_COLOR.get(pr.lower(), "")
    pr_styled = f"[{pr_color}]{pr:<7}[/]" if pr_color else f"{pr:<7}"

    status = (row.task.status or "").strip() or "—"
    status_styled = f"[dim]{status:<13}[/]"

    name = row.task.name
    if len(name) > 50:
        name = name[:49] + "…"

    extras: list[str] = []
    if show_location and row.location:
        extras.append(f"[dim]{row.location}[/]")
    if row.tags:
        extras.append(f"[dim]{row.tags}[/]")
    extras_str = "  ".join(extras)
    return f"{pr_styled}  {status_styled}  {name}    {extras_str}"


def _group_key(t: Task) -> str:
    if t.folder_name:
        return t.folder_name
    if t.space_name:
        return t.space_name
    return ""


def _grouped_indices(rows: list[_Row]) -> list[tuple[str, list[int]]]:
    """Preserve input order; return (group_label, [indices_into_rows])."""
    seen: dict[str, list[int]] = {}
    order: list[str] = []
    for i, row in enumerate(rows):
        key = _group_key(row.task)
        if key not in seen:
            seen[key] = []
            order.append(key)
        seen[key].append(i)
    return [(label, seen[label]) for label in order]


def _section_header_text(label: str, count: int) -> str:
    safe = label or "(no folder)"
    return f"[dim]── {safe} · {count} ──[/]"


class TicketPickerApp(App[Task | None]):
    """Filterable, keyboard-driven ticket picker.

    Returns the selected ``Task`` from ``run()`` (Textual's app-level run
    return type), or ``None`` if the user quits or cancels.
    """

    CSS = """
    Screen { layout: vertical; }
    #filter-bar {
        height: 3;
        padding: 0 1;
        border: round $surface;
    }
    #filter-bar:focus-within { border: round $accent; }
    #filter-bar Input {
        background: transparent;
        border: none;
    }
    #picker-list {
        height: 1fr;
        border: round $surface;
        padding: 0 1;
    }
    #picker-list:focus-within { border: round $accent; }
    .picker-row { padding: 0 1; }
    .picker-row.-empty { color: $text-muted; }
    .section-header { padding: 0 1; text-style: italic; }
    .status-bar { height: 1; padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "cancel", "quit"),
        Binding("escape", "escape", "back/quit", show=False),
        Binding("enter", "pick", "pick"),
        Binding("a", "view_actions", "actions"),
        Binding("/", "focus_filter", "filter"),
        Binding("ctrl+l", "focus_filter", "filter", show=False),
        Binding("ctrl+u", "clear_filter", "clear", show=False),
    ]

    def __init__(
        self,
        tasks: list[Task],
        *,
        actions_ctx: ActionsContext | None = None,
        theme: str | None = None,
    ) -> None:
        super().__init__()
        self._all_rows = _build_rows(tasks)
        self._show_locations = self._needs_location_column(tasks)
        self._picked: Task | None = None
        self._visible_indices: list[int] = []  # row index → master row index
        self._actions_ctx = actions_ctx
        self._theme = theme

    @staticmethod
    def _needs_location_column(tasks: list[Task]) -> bool:
        """Hide the per-row location when every ticket shares one folder/space."""
        keys = {_group_key(t) for t in tasks if _group_key(t)}
        return len(keys) > 1

    # ------- compose / lifecycle --------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        if self._theme == "ben10":
            yield OmnitrixBanner()
        with Vertical(id="filter-bar"):
            yield Input(placeholder="type to filter · / to refocus · esc to clear",
                        id="filter-input")
        yield ListView(id="picker-list")
        yield Static("", id="status", classes="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        apply_theme(self, self._theme)
        self.title = "clickup-work · pick a ticket"
        self.sub_title = f"{len(self._all_rows)} open"
        self._apply_filter(filter_text="")
        self.query_one("#picker-list", ListView).focus()

    # ------- filter ---------------------------------------------------------

    @on(Input.Changed, "#filter-input")
    def _filter_changed(self, event: Input.Changed) -> None:
        self._apply_filter(filter_text=event.value.strip().lower())

    @on(Input.Submitted, "#filter-input")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        # Pressing Enter in the filter box jumps focus to the list so
        # subsequent Enter picks the highlighted row.
        listview = self.query_one("#picker-list", ListView)
        listview.focus()

    def action_focus_filter(self) -> None:
        self.query_one("#filter-input", Input).focus()

    def action_clear_filter(self) -> None:
        self.query_one("#filter-input", Input).value = ""

    # ------- render ---------------------------------------------------------

    def _apply_filter(self, *, filter_text: str) -> None:
        listview = self.query_one("#picker-list", ListView)
        listview.clear()
        self._visible_indices = []

        if filter_text:
            matching = [
                (i, row) for i, row in enumerate(self._all_rows)
                if filter_text in row.haystack
            ]
        else:
            matching = list(enumerate(self._all_rows))

        if not matching:
            listview.append(
                ListItem(
                    Static("[dim]no tickets match this filter[/]",
                           classes="picker-row -empty"),
                    disabled=True,
                )
            )
            self._update_status("0 / {} tickets visible".format(len(self._all_rows)))
            return

        # Re-group only by what's currently visible so the section headers
        # accurately reflect the filtered view.
        rows_only: list[_Row] = [row for _, row in matching]
        original_indices: list[int] = [i for i, _ in matching]

        groups = _grouped_indices(rows_only)
        for label, indices_in_filtered in groups:
            if len(groups) > 1:
                listview.append(
                    ListItem(
                        Static(_section_header_text(label, len(indices_in_filtered)),
                               classes="section-header"),
                        disabled=True,
                    )
                )
            for idx_in_filtered in indices_in_filtered:
                row = rows_only[idx_in_filtered]
                listview.append(
                    ListItem(
                        Static(_format_row_line(row, show_location=self._show_locations),
                               classes="picker-row"),
                    )
                )
                self._visible_indices.append(original_indices[idx_in_filtered])

        listview.index = self._first_selectable(listview)
        self._update_status(
            f"{len(matching)} / {len(self._all_rows)} tickets visible · "
            f"[dim]↑↓ nav · enter pick · / filter · q quit[/]"
        )

    @staticmethod
    def _first_selectable(listview: ListView) -> int:
        for i, item in enumerate(listview.children):
            if isinstance(item, ListItem) and not item.disabled:
                return i
        return 0

    def _update_status(self, msg: str) -> None:
        self.query_one("#status", Static).update(msg)

    # ------- pick / cancel --------------------------------------------------

    def _selected_task(self) -> Task | None:
        listview = self.query_one("#picker-list", ListView)
        idx = listview.index
        if idx is None:
            return None
        non_disabled_positions = [
            i for i, child in enumerate(listview.children)
            if isinstance(child, ListItem) and not child.disabled
        ]
        if idx not in non_disabled_positions:
            return None
        position_in_visible = non_disabled_positions.index(idx)
        if 0 <= position_in_visible < len(self._visible_indices):
            return self._all_rows[self._visible_indices[position_in_visible]].task
        return None

    def action_pick(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        self._picked = task
        self.exit(task)

    def action_view_actions(self) -> None:
        """Open the per-ticket actions modal on the highlighted row.

        Silently no-op when the picker was launched without an ActionsContext
        (e.g. from a code path that doesn't have the ClickUp client wired up).
        """
        if self._actions_ctx is None:
            return
        task = self._selected_task()
        if task is None:
            return

        def after(result: str | None) -> None:
            if result == SEND_TO_CLAUDE:
                self.exit(task)

        self.push_screen(TicketActionsScreen(task, self._actions_ctx), after)

    def action_cancel(self) -> None:
        self._picked = None
        self.exit(None)

    def action_escape(self) -> None:
        # Esc inside the filter clears it; otherwise quit.
        focused = self.focused
        filter_input = self.query_one("#filter-input", Input)
        if focused is filter_input and filter_input.value:
            filter_input.value = ""
            return
        self.action_cancel()


def pick_task_tui(
    tasks: list[Task],
    *,
    actions_ctx: ActionsContext | None = None,
    theme: str | None = None,
) -> Task | None:
    """Run the picker. Empty input list returns ``None`` immediately.

    When ``actions_ctx`` is provided, pressing ``a`` on a row opens a modal
    where the user can change status, set the time estimate, log time, and
    read or post comments before deciding to send the ticket to Claude.
    Without it, ``a`` is a silent no-op (e.g. fzf-fallback paths or tests
    that don't supply a client).

    ``theme`` selects a registered visual theme (``"ben10"``, etc.). Unknown
    or ``None`` values fall back to the default Textual palette.
    """
    if not tasks:
        return None
    if len(tasks) == 1:
        return tasks[0]
    app = TicketPickerApp(tasks, actions_ctx=actions_ctx, theme=theme)
    return app.run()


def should_use_tui() -> bool:
    """True when both ends of the terminal are interactive."""
    return sys.stdin.isatty() and sys.stdout.isatty()
