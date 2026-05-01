"""Bucket open tickets into this/next week and render a personal workload report.

Pure logic — no I/O, no argparse. The CLI layer fetches tasks and config, hands
them in, and prints whatever ``render_report`` returns. That separation keeps
the bucketing rules unit-testable without mocking ClickUp.

Bucketing rules (each task lands in exactly one section):

* No ``due_date``               → ``undated`` section.
* Has ``due_date`` but no
  positive ``time_estimate``    → ``unestimated`` section. Workload view is
                                  blind to these too — calling them out is the
                                  whole reason this report exists.
* Due in this week, or overdue  → ``this_week``. Counts toward this week's
                                  hour total.
* Due in next week              → ``next_week``. Counts toward next week's
                                  hour total.
* Due after next week           → out of horizon, omitted.

Capacity is ``hours_per_day * 5`` weekdays. Adjustable in v2 if part-timers
need 3- or 4-day weeks.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from clickup_work.clickup import Task

WEEKDAYS_PER_WEEK = 5
MS_PER_HOUR = 3_600_000
_BAR_WIDTH = 20


@dataclass(frozen=True)
class WeekWindow:
    start: dt.date
    end: dt.date  # inclusive (Sunday)
    label: str


@dataclass(frozen=True)
class WeekBucket:
    window: WeekWindow
    tasks: tuple[Task, ...]
    hours: float


@dataclass(frozen=True)
class WorkloadReport:
    hours_per_day: float
    weekly_capacity_hours: float
    this_week: WeekBucket
    next_week: WeekBucket
    unestimated: tuple[Task, ...]
    undated: tuple[Task, ...]


def _ms_to_local_date(ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ms / 1000).date()


def _ms_to_hours(ms: int | None) -> float:
    if not ms or ms <= 0:
        return 0.0
    return ms / MS_PER_HOUR


def _week_windows(today: dt.date) -> tuple[WeekWindow, WeekWindow]:
    monday = today - dt.timedelta(days=today.weekday())
    return (
        WeekWindow(
            start=monday,
            end=monday + dt.timedelta(days=6),
            label="This week",
        ),
        WeekWindow(
            start=monday + dt.timedelta(days=7),
            end=monday + dt.timedelta(days=13),
            label="Next week",
        ),
    )


def build_report(
    tasks: list[Task],
    hours_per_day: float,
    today: dt.date | None = None,
) -> WorkloadReport:
    today = today or dt.date.today()
    this_window, next_window = _week_windows(today)
    weekly_capacity = hours_per_day * WEEKDAYS_PER_WEEK

    this_week_tasks: list[Task] = []
    next_week_tasks: list[Task] = []
    unestimated: list[Task] = []
    undated: list[Task] = []

    for t in tasks:
        if t.due_date is None:
            undated.append(t)
            continue
        if not t.time_estimate or t.time_estimate <= 0:
            unestimated.append(t)
            continue
        due = _ms_to_local_date(t.due_date)
        if due <= this_window.end:
            this_week_tasks.append(t)
        elif due <= next_window.end:
            next_week_tasks.append(t)
        # else: out of two-week horizon — intentionally dropped.

    return WorkloadReport(
        hours_per_day=hours_per_day,
        weekly_capacity_hours=weekly_capacity,
        this_week=WeekBucket(
            window=this_window,
            tasks=tuple(this_week_tasks),
            hours=sum(_ms_to_hours(t.time_estimate) for t in this_week_tasks),
        ),
        next_week=WeekBucket(
            window=next_window,
            tasks=tuple(next_week_tasks),
            hours=sum(_ms_to_hours(t.time_estimate) for t in next_week_tasks),
        ),
        unestimated=tuple(unestimated),
        undated=tuple(undated),
    )


def _render_bar(hours: float, capacity: float) -> str:
    if capacity <= 0:
        return "·" * _BAR_WIDTH
    ratio = hours / capacity
    filled = max(0, min(_BAR_WIDTH, int(round(ratio * _BAR_WIDTH))))
    return "█" * filled + "░" * (_BAR_WIDTH - filled)


def _render_hours(hours: float) -> str:
    """`4h` for whole hours, `4.5h` otherwise."""
    if abs(hours - round(hours)) < 0.05:
        return f"{int(round(hours))}h"
    return f"{hours:.1f}h"


def _render_window(win: WeekWindow) -> str:
    # Use %-d (no zero-pad) so "May 4 – May 10" reads naturally. Falls back to
    # %d on platforms where %-d isn't supported (Windows). We don't run there
    # in production but stay defensive.
    fmt = "%b %-d"
    try:
        return f"{win.start.strftime(fmt)} – {win.end.strftime(fmt)}"
    except ValueError:
        return f"{win.start.strftime('%b %d')} – {win.end.strftime('%b %d')}"


def _render_due(due_ms: int, today: dt.date) -> str:
    due = _ms_to_local_date(due_ms)
    if due < today:
        return f"OVERDUE ({due.isoformat()})"
    if due == today:
        return "due today"
    if (due - today).days < 7:
        return f"due {due.strftime('%a')}"
    return f"due {due.isoformat()}"


def _truncate(text: str, max_len: int) -> str:
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def _pad_id(task_id: str, width: int = 10) -> str:
    """ClickUp IDs vary in length; pad to a fixed width for column alignment."""
    return task_id.ljust(width)


def _render_week(bucket: WeekBucket, capacity: float, today: dt.date) -> list[str]:
    bar = _render_bar(bucket.hours, capacity)
    if bucket.hours > capacity:
        marker = f"⚠ OVER by {_render_hours(bucket.hours - capacity)}"
    elif bucket.hours == capacity:
        marker = "= at capacity"
    else:
        marker = "✓ under"
    lines = [
        f"{bucket.window.label} ({_render_window(bucket.window)}):",
        f"  {bar}  {_render_hours(bucket.hours)} / "
        f"{_render_hours(capacity)}  {marker}",
    ]
    for t in sorted(bucket.tasks, key=lambda x: x.due_date or 0):
        est = _render_hours(_ms_to_hours(t.time_estimate))
        due_str = _render_due(t.due_date, today) if t.due_date else ""
        name = _truncate(t.name, 40)
        lines.append(f"  • {_pad_id(t.id)}  {name:<40s}  {est:>5s}  {due_str}")
    return lines


def render_report(
    report: WorkloadReport,
    *,
    show_unestimated: bool = True,
    today: dt.date | None = None,
) -> str:
    today = today or dt.date.today()
    blocks: list[list[str]] = [
        [
            f"Capacity: {_render_hours(report.hours_per_day)}/day · "
            f"{_render_hours(report.weekly_capacity_hours)}/week"
        ],
        _render_week(report.this_week, report.weekly_capacity_hours, today),
        _render_week(report.next_week, report.weekly_capacity_hours, today),
    ]

    if show_unestimated and report.unestimated:
        section = [
            f"⚠ {len(report.unestimated)} assigned ticket(s) have no time "
            f"estimate — Workload can't see them:"
        ]
        for t in report.unestimated:
            section.append(f"  • {_pad_id(t.id)}  {_truncate(t.name, 50)}")
        blocks.append(section)

    if report.undated:
        section = [
            f"Tickets without a due date (not bucketed): {len(report.undated)}"
        ]
        for t in report.undated:
            est_str = (
                _render_hours(_ms_to_hours(t.time_estimate))
                if t.time_estimate and t.time_estimate > 0
                else "(no estimate)"
            )
            name = _truncate(t.name, 50)
            section.append(f"  • {_pad_id(t.id)}  {name:<50s}  {est_str}")
        blocks.append(section)

    return "\n\n".join("\n".join(block) for block in blocks)
