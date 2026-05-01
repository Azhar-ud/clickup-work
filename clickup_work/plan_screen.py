"""Plan screen — shown between picker and Claude launch.

Displays the chosen ticket, the resolved repo, and the branch about to be
cut. Lets the user override the base branch before committing to git ops.
Returns the confirmed base, or ``None`` on cancel.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Header, Input, Label, Static

from clickup_work.clickup import Task


_PRIORITY_COLOR = {
    "urgent": "bold red",
    "high": "yellow",
    "normal": "",
    "low": "dim",
}


@dataclass(frozen=True)
class PlanInputs:
    """Everything the plan screen needs to render. Pure data, no behavior."""

    task: Task
    repo_name: str
    repo_path: str
    base_branch: str
    base_source: str  # human-readable provenance ("config", "origin/HEAD", "--base")
    branch_name: str


class _PlanCard(Static):
    """Boxed summary panel showing what's about to happen."""

    DEFAULT_CSS = """
    _PlanCard {
        height: auto;
        padding: 1 2;
        margin: 1 0;
        border: round $surface;
    }
    """


class PlanApp(App[str | None]):
    """Confirm the cut: shows the plan, lets the user tweak base, returns it."""

    CSS = """
    Screen { layout: vertical; padding: 0 1; }
    #base-row {
        height: 5;
        padding: 0 1;
        border: round $surface;
        margin-bottom: 1;
    }
    #base-row:focus-within { border: round $accent; }
    #base-row Label { padding: 0 1; }
    #base-row Input {
        background: transparent;
        border: none;
    }
    #status-bar {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    .label-key { color: $text-muted; width: 8; }
    .label-val { width: 1fr; }
    """

    BINDINGS = [
        Binding("enter", "confirm", "go"),
        Binding("escape", "cancel", "cancel"),
        Binding("q", "cancel", "cancel"),
        Binding("ctrl+u", "clear_base", "reset base", show=False),
    ]

    def __init__(self, inputs: PlanInputs, theme: str | None = None) -> None:
        super().__init__()
        self._inputs = inputs
        self._theme = theme

    # ------- compose / lifecycle --------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield _PlanCard(self._render_plan(), id="plan-card")
        with Vertical(id="base-row"):
            yield Label(
                f"[dim]base branch (from {self._inputs.base_source}) — "
                f"edit to override:[/]"
            )
            yield Input(
                value=self._inputs.base_branch,
                id="base-input",
            )
        yield Static(
            "[dim]Enter to launch Claude · Esc / q to cancel · Ctrl+U to reset base[/]",
            id="status-bar",
        )
        yield Footer()

    def on_mount(self) -> None:
        from clickup_work.themes import apply_theme

        apply_theme(self, self._theme)
        self.title = "clickup-work · ready to cut"
        self.sub_title = self._inputs.task.id
        # Focus the plan card by default; user can tab into the Input to edit.
        self.query_one("#base-input", Input).focus()
        # Cursor at end so Enter is one keystroke if the default's right.
        inp = self.query_one("#base-input", Input)
        inp.cursor_position = len(inp.value)

    # ------- render ---------------------------------------------------------

    def _render_plan(self) -> str:
        t = self._inputs.task
        priority_raw = t.priority or "—"
        priority_color = _PRIORITY_COLOR.get(priority_raw.lower(), "")
        priority_styled = (
            f"[{priority_color}]{priority_raw}[/]"
            if priority_color
            else priority_raw
        )
        status = (t.status or "—").strip()
        list_label = self._list_breadcrumb(t)
        url = t.url or "[dim](no url)[/]"
        description_snip = (
            (t.description.strip().splitlines() or [""])[0][:80]
            if t.description
            else "[dim](no description)[/]"
        )

        rows = [
            f"[bold]{t.name}[/]",
            f"[dim]{t.id}  ·  {priority_styled} priority  ·  {status}[/]",
            f"[dim]list:[/]  {list_label}",
            f"[dim]url:[/]   {url}",
            f"[dim]desc:[/]  {description_snip}",
            "",
            f"[dim]repo:[/]    {self._inputs.repo_path}  "
            f"[dim](nickname: {self._inputs.repo_name})[/]",
            f"[dim]branch:[/]  {self._inputs.branch_name}  →  PR into "
            f"[bold]{self._inputs.base_branch}[/]",
        ]
        return "\n".join(rows)

    @staticmethod
    def _list_breadcrumb(t: Task) -> str:
        parts: list[str] = []
        if t.space_name:
            parts.append(t.space_name)
        if t.folder_name:
            parts.append(t.folder_name)
        if t.list_name:
            parts.append(t.list_name)
        return " / ".join(parts) if parts else "[dim](none)[/]"

    # ------- actions --------------------------------------------------------

    def action_confirm(self) -> None:
        chosen = self.query_one("#base-input", Input).value.strip()
        if not chosen:
            self.query_one("#status-bar", Static).update(
                "[red]base branch can't be empty — type one or press Esc to cancel[/]"
            )
            return
        self.exit(chosen)

    def action_cancel(self) -> None:
        self.exit(None)

    def action_clear_base(self) -> None:
        self.query_one("#base-input", Input).value = self._inputs.base_branch

    @on(Input.Submitted, "#base-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        # Pressing Enter inside the Input also confirms — same as the keybind.
        self.action_confirm()


def run_plan(inputs: PlanInputs, theme: str | None = None) -> str | None:
    """Launch the plan screen. Returns the confirmed base or None if cancelled."""
    return PlanApp(inputs, theme=theme).run()
