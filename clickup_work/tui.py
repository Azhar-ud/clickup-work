"""Textual-based interactive workload TUI.

Default surface for ``clickup-work workload`` when stdout is a TTY. The plain
ANSI report in :mod:`clickup_work.workload` is still used as a fallback when
stdout isn't a TTY (pipes, CI, ``> file.txt``) or ``--no-tui`` is passed.

Keybinds:
    q          quit
    r          refresh (re-fetch from ClickUp)
    ↑ / ↓      move ticket cursor
    enter      open the selected ticket's URL in a browser
    e          set time estimate on the selected ticket
    s          change status on the selected ticket
"""

from __future__ import annotations

import datetime as dt
import webbrowser
from dataclasses import dataclass

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Select,
    Static,
)

from clickup_work.clickup import ClickUp, ClickUpError, Task
from clickup_work.workload import (
    MS_PER_HOUR,
    WeekBucket,
    WorkloadReport,
    build_report,
)

# Treated as "warm" once load ratio crosses this. Below = green, above = yellow,
# above 1.0 = red. Pulled out so themes/tweaks are one-line changes.
_WARN_RATIO = 0.85
_BAR_WIDTH = 24
# Sub-block characters give 8x finer resolution than full blocks alone, which
# matters at low totals (4h on a 20h bar should still be visibly non-zero).
_SUB_BLOCKS = " ▏▎▍▌▋▊▉"


def _ms_to_hours(ms: int | None) -> float:
    if not ms or ms <= 0:
        return 0.0
    return ms / MS_PER_HOUR


def _format_hours(hours: float) -> str:
    if abs(hours - round(hours)) < 0.05:
        return f"{int(round(hours))}h"
    return f"{hours:.1f}h"


def _bar(hours: float, capacity: float, width: int = _BAR_WIDTH) -> str:
    """Render a sub-block-precision bar. Caps at ``width`` cells when over."""
    if capacity <= 0:
        return "·" * width
    ratio = hours / capacity
    cells_total = ratio * width
    full = min(width, int(cells_total))
    fractional = cells_total - full
    sub_idx = min(len(_SUB_BLOCKS) - 1, int(fractional * len(_SUB_BLOCKS)))
    bar = "█" * full
    if full < width and sub_idx > 0:
        bar += _SUB_BLOCKS[sub_idx]
        bar += "░" * (width - full - 1)
    else:
        bar += "░" * (width - full)
    return bar


def _style_for(hours: float, capacity: float) -> str:
    """Return a Textual rich-markup color tag matching the load level."""
    if capacity <= 0:
        return "white"
    ratio = hours / capacity
    if ratio > 1.0:
        return "bold red"
    if ratio >= _WARN_RATIO:
        return "yellow"
    return "green"


def _format_due(due_ms: int | None, today: dt.date) -> str:
    if due_ms is None:
        return ""
    due = dt.datetime.fromtimestamp(due_ms / 1000).date()
    if due < today:
        return f"OVERDUE {due.isoformat()}"
    if due == today:
        return "today"
    if (due - today).days < 7:
        return due.strftime("%a")
    return due.isoformat()


@dataclass
class TicketRow:
    """One row in the navigable list. ``task`` may be None for placeholders."""

    task: Task
    section: str  # "this", "next", "missing", "undated"
    due_label: str
    estimate_label: str  # "4h" or "—"


class WeekPanel(Static):
    """Capacity bar + summary line for a single week."""

    DEFAULT_CSS = """
    WeekPanel {
        width: 1fr;
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
        border: round $surface;
    }
    WeekPanel:focus { border: round $accent; }
    """

    def __init__(self, title: str, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.border_title = title

    def update_bucket(self, bucket: WeekBucket, capacity: float) -> None:
        bar = _bar(bucket.hours, capacity)
        color = _style_for(bucket.hours, capacity)
        if bucket.hours > capacity:
            verdict = (
                f"[bold red]⚠ {_format_hours(bucket.hours - capacity)} "
                f"over[/]"
            )
        elif bucket.hours == 0:
            verdict = "[dim]nothing scheduled[/]"
        elif bucket.hours >= capacity * _WARN_RATIO:
            verdict = (
                f"[yellow]near capacity, "
                f"{_format_hours(capacity - bucket.hours)} free[/]"
            )
        else:
            verdict = (
                f"[green]✓ {_format_hours(capacity - bucket.hours)} free[/]"
            )

        win = bucket.window
        date_range = f"{win.start.strftime('%b %-d')} – {win.end.strftime('%b %-d')}"
        body = (
            f"[dim]{date_range}[/]\n"
            f"[{color}]{bar}[/]  "
            f"[{color}]{_format_hours(bucket.hours)}[/]"
            f" [dim]/ {_format_hours(capacity)}[/]\n"
            f"{verdict}"
        )
        self.update(body)


class EstimatePrompt(ModalScreen[str | None]):
    """Pop-up that asks for a duration string like ``2h``, ``90m``, ``1h 30m``."""

    DEFAULT_CSS = """
    EstimatePrompt { align: center middle; }
    EstimatePrompt > Vertical {
        width: 50; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    EstimatePrompt Input { margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, ticket_name: str, current_ms: int | None) -> None:
        super().__init__()
        self._ticket_name = ticket_name
        self._current = (
            _format_hours(_ms_to_hours(current_ms)) if current_ms else "—"
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[bold]Time estimate[/] · {self._ticket_name[:40]}")
            yield Label(f"[dim]current: {self._current}[/]")
            yield Input(placeholder="e.g. 2h, 90m, 1h 30m", id="estimate-input")
            yield Label("[dim]Enter to save · Esc to cancel[/]")

    @on(Input.Submitted, "#estimate-input")
    def _submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class StatusPrompt(ModalScreen[str | None]):
    """Pop-up that lets the user pick a new status from the list's options."""

    DEFAULT_CSS = """
    StatusPrompt { align: center middle; }
    StatusPrompt > Vertical {
        width: 50; height: auto; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    """

    BINDINGS = [Binding("escape", "dismiss(None)", "cancel", show=False)]

    def __init__(self, ticket_name: str, current: str, options: list[str]) -> None:
        super().__init__()
        self._ticket_name = ticket_name
        self._current = current
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"[bold]Move status[/] · {self._ticket_name[:40]}")
            yield Label(f"[dim]current: {self._current}[/]")
            yield Select(
                [(s, s) for s in self._options],
                value=self._current if self._current in self._options else Select.BLANK,
                id="status-select",
            )
            yield Label("[dim]Enter to save · Esc to cancel[/]")

    @on(Select.Changed, "#status-select")
    def _changed(self, event: Select.Changed) -> None:
        if event.value is not Select.BLANK:
            self.dismiss(str(event.value))


class WorkloadApp(App[None]):
    """Top-level TUI app for ``clickup-work workload``."""

    CSS = """
    Screen { layout: vertical; }
    #tldr {
        height: 3; padding: 1 2;
        background: $boost;
    }
    #weeks { height: auto; }
    #weeks > WeekPanel { width: 1fr; }
    #tickets {
        border: round $surface;
        margin-top: 1;
        padding: 0 1;
    }
    #tickets:focus-within { border: round $accent; }
    .status-bar { padding: 0 1; height: 1; color: $text-muted; }
    .section-header {
        padding: 0 1;
        color: $text-muted;
        text-style: italic;
    }
    .ticket-row { padding: 0 1; }
    .ticket-row.-overdue { color: $error; }
    .ticket-row.-missing { color: $warning; }
    .empty-row { padding: 0 1; color: $text-muted; }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh"),
        Binding("enter", "open_url", "open"),
        Binding("e", "set_estimate", "estimate"),
        Binding("s", "set_status", "status"),
    ]

    def __init__(
        self,
        client: ClickUp,
        team_id: str,
        user_id: str,
        list_id: str,
        hours_per_day: float,
    ) -> None:
        super().__init__()
        self._client = client
        self._team_id = team_id
        self._user_id = user_id
        self._list_id = list_id
        self._hours_per_day = hours_per_day
        self._rows: list[TicketRow] = []
        self._report: WorkloadReport | None = None

    # ------- compose / lifecycle --------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static("", id="tldr")
        with Horizontal(id="weeks"):
            yield WeekPanel("This week", id="this-week")
            yield WeekPanel("Next week", id="next-week")
        yield Static("Loading…", id="status", classes="status-bar")
        yield ListView(id="tickets")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "clickup-work · workload"
        self.sub_title = (
            f"{_format_hours(self._hours_per_day)}/day · "
            f"{_format_hours(self._hours_per_day * 5)}/week"
        )
        self.action_refresh()

    # ------- data refresh ---------------------------------------------------

    def action_refresh(self) -> None:
        self.query_one("#status", Static).update("Loading tickets…")
        self.refresh_data()

    @staticmethod
    def _fetch_tasks(
        client: ClickUp, team_id: str, user_id: str, list_id: str
    ) -> list[Task]:
        return client.get_open_tasks(
            team_id=team_id,
            user_id=user_id,
            list_id=list_id,
            limit=100,
        )

    def refresh_data(self) -> None:
        try:
            tasks = self._fetch_tasks(
                self._client, self._team_id, self._user_id, self._list_id
            )
        except ClickUpError as e:
            self.query_one("#status", Static).update(f"[red]error:[/] {e}")
            return

        self._report = build_report(tasks, hours_per_day=self._hours_per_day)
        self._render_report(self._report)

    # ------- render ---------------------------------------------------------

    def _render_report(self, report: WorkloadReport) -> None:
        cap = report.weekly_capacity_hours
        self.query_one("#this-week", WeekPanel).update_bucket(report.this_week, cap)
        self.query_one("#next-week", WeekPanel).update_bucket(report.next_week, cap)

        # Top-line "what do I need to know" — surfaces overload first, then near-cap,
        # then missing estimates. Falls back to "all clear" only when nothing shouts.
        problems: list[str] = []
        for bucket, label in (
            (report.this_week, "This week"),
            (report.next_week, "Next week"),
        ):
            if bucket.hours > cap:
                problems.append(
                    f"[bold red]⚠ {label} {_format_hours(bucket.hours - cap)} "
                    f"over[/]"
                )
            elif cap > 0 and bucket.hours >= cap * _WARN_RATIO:
                problems.append(
                    f"[yellow]{label} near capacity, "
                    f"{_format_hours(cap - bucket.hours)} free[/]"
                )
        if report.unestimated:
            problems.append(
                f"[yellow]{len(report.unestimated)} ticket(s) missing "
                f"time estimates[/]"
            )
        tldr = (
            "  ·  ".join(problems)
            if problems
            else "[green]✓ all clear — under capacity, nothing missing[/]"
        )
        self.query_one("#tldr", Static).update(tldr)

        # Build the unified, navigable ticket list — order matters here, since
        # the cursor flows top-to-bottom through it.
        rows: list[TicketRow] = []
        today = dt.date.today()

        for t in sorted(report.this_week.tasks, key=lambda x: x.due_date or 0):
            rows.append(
                TicketRow(
                    task=t,
                    section="this",
                    due_label=_format_due(t.due_date, today),
                    estimate_label=_format_hours(_ms_to_hours(t.time_estimate)),
                )
            )
        for t in sorted(report.next_week.tasks, key=lambda x: x.due_date or 0):
            rows.append(
                TicketRow(
                    task=t,
                    section="next",
                    due_label=_format_due(t.due_date, today),
                    estimate_label=_format_hours(_ms_to_hours(t.time_estimate)),
                )
            )
        for t in report.unestimated:
            rows.append(
                TicketRow(
                    task=t,
                    section="missing",
                    due_label=_format_due(t.due_date, today),
                    estimate_label="—",
                )
            )
        for t in report.undated:
            rows.append(
                TicketRow(
                    task=t,
                    section="undated",
                    due_label="(no due date)",
                    estimate_label=(
                        _format_hours(_ms_to_hours(t.time_estimate))
                        if t.time_estimate
                        else "—"
                    ),
                )
            )

        self._rows = rows
        listview = self.query_one("#tickets", ListView)
        listview.clear()

        if not rows:
            listview.append(
                ListItem(Static("[dim]no open tickets assigned to you[/]",
                                classes="empty-row"))
            )
            self.query_one("#status", Static).update(
                "[green]inbox zero[/] · nothing assigned to you"
            )
            return

        last_section = ""
        for row in rows:
            if row.section != last_section:
                listview.append(
                    ListItem(
                        Static(_section_header(row.section),
                               classes="section-header"),
                        disabled=True,
                    )
                )
                last_section = row.section
            listview.append(_ticket_list_item(row))

        listview.index = self._first_selectable_index(listview)
        self.query_one("#status", Static).update(
            f"{len(rows)} ticket(s) loaded · "
            f"[dim]q quit · r refresh · e estimate · s status · enter open[/]"
        )

    @staticmethod
    def _first_selectable_index(listview: ListView) -> int:
        for i, item in enumerate(listview.children):
            if isinstance(item, ListItem) and not item.disabled:
                return i
        return 0

    def _selected_row(self) -> TicketRow | None:
        listview = self.query_one("#tickets", ListView)
        idx = listview.index
        if idx is None:
            return None
        # Skip section headers (disabled list items).
        seen = -1
        for child in listview.children:
            if not isinstance(child, ListItem):
                continue
            if child.disabled:
                continue
            seen += 1
            # Map listview index back through skipping disabled items.
        # Simpler: walk the children list mapping non-disabled to rows in order.
        non_disabled = [
            i
            for i, child in enumerate(listview.children)
            if isinstance(child, ListItem) and not child.disabled
        ]
        if idx not in non_disabled:
            return None
        row_position = non_disabled.index(idx)
        if 0 <= row_position < len(self._rows):
            return self._rows[row_position]
        return None

    # ------- actions --------------------------------------------------------

    def action_open_url(self) -> None:
        row = self._selected_row()
        if row is None or not row.task.url:
            return
        webbrowser.open(row.task.url)
        self.query_one("#status", Static).update(
            f"opened {row.task.id} in browser"
        )

    def action_set_estimate(self) -> None:
        row = self._selected_row()
        if row is None:
            return

        def after(value: str | None) -> None:
            if not value:
                return
            from clickup_work.cli import _parse_duration  # avoid circular at import time

            ms = _parse_duration(value)
            if ms is None:
                self.query_one("#status", Static).update(
                    f"[red]could not parse '{value}' as a duration[/]"
                )
                return
            try:
                self._client.set_time_estimate(row.task.id, ms)
            except ClickUpError as e:
                self.query_one("#status", Static).update(f"[red]{e}[/]")
                return
            self.query_one("#status", Static).update(
                f"[green]✓ {row.task.id} estimate set to "
                f"{_format_hours(_ms_to_hours(ms))}[/]"
            )
            self.refresh_data()

        self.push_screen(
            EstimatePrompt(row.task.name, row.task.time_estimate),
            after,
        )

    def action_set_status(self) -> None:
        row = self._selected_row()
        if row is None or not row.task.list_id:
            return
        try:
            statuses = self._client.get_list_statuses(row.task.list_id)
        except ClickUpError as e:
            self.query_one("#status", Static).update(f"[red]{e}[/]")
            return
        if not statuses:
            self.query_one("#status", Static).update(
                "[yellow]no statuses configured on this list[/]"
            )
            return

        def after(value: str | None) -> None:
            if not value or value.lower() == row.task.status.lower():
                return
            try:
                self._client.update_task_status(row.task.id, value)
            except ClickUpError as e:
                self.query_one("#status", Static).update(f"[red]{e}[/]")
                return
            self.query_one("#status", Static).update(
                f"[green]✓ {row.task.id} moved to {value}[/]"
            )
            self.refresh_data()

        self.push_screen(
            StatusPrompt(row.task.name, row.task.status, statuses),
            after,
        )


def _section_header(section: str) -> str:
    return {
        "this": "── This week ──",
        "next": "── Next week ──",
        "missing": "── ⚠ Missing time estimates ──",
        "undated": "── Tickets without a due date ──",
    }.get(section, "──")


def _ticket_list_item(row: TicketRow) -> ListItem:
    classes = ["ticket-row"]
    if row.section == "missing":
        classes.append("-missing")
    if row.due_label.startswith("OVERDUE"):
        classes.append("-overdue")

    name = row.task.name
    if len(name) > 50:
        name = name[:49] + "…"

    line = (
        f"[dim]{row.task.id[:8]:>8}[/]  "
        f"{name:<50s}  "
        f"{row.estimate_label:>5s}  "
        f"[dim]{row.due_label}[/]"
    )
    return ListItem(Static(line, classes=" ".join(classes)))


def run_app(
    *,
    client: ClickUp,
    team_id: str,
    user_id: str,
    list_id: str,
    hours_per_day: float,
) -> None:
    """Entry point used by ``cli._workload_report_cmd``."""
    WorkloadApp(
        client=client,
        team_id=team_id,
        user_id=user_id,
        list_id=list_id,
        hours_per_day=hours_per_day,
    ).run()
