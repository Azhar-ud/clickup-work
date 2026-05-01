"""Post-Claude flow as a Textual app.

Replaces the chain of plain-text prompts that ran after Claude exited:
push confirmation → status picker → time tracking → reassign. Each step
becomes a modal screen that chains via callbacks, so the user moves
forward (or skips) with a single keystroke per step.

Single entrypoint: :func:`run_post_flow`. Returns ``None``; side effects
(API calls, git push, PR creation) happen inside the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from clickup_work.clickup import ClickUp, ClickUpError, Member, Task
from clickup_work.git import GitError, commit_subjects, push_and_open_pr
from clickup_work.tui import EstimatePrompt, StatusPrompt


# ---------- inputs --------------------------------------------------------


@dataclass(frozen=True)
class PostFlowInputs:
    """Everything PostFlowApp needs. Pure data."""

    client: ClickUp
    team_id: str
    user_id: str
    task: Task
    repo_path: Path
    branch: str
    base: str
    commits_ahead: int
    merge_commits_ahead: int
    draft: bool
    pr_body_builder: Callable[[list[str]], str]  # commits → body markdown
    prompt_status: bool = True
    prompt_time: bool = True
    prompt_assign: bool = True


# ---------- member picker modal ------------------------------------------


class MemberPrompt(ModalScreen[Member | None]):
    """Filterable list of workspace members. Returns the chosen one or None."""

    DEFAULT_CSS = """
    MemberPrompt { align: center middle; }
    MemberPrompt > Vertical {
        width: 70; height: 24; padding: 1 2;
        background: $surface; border: tall $accent;
    }
    MemberPrompt Input { background: transparent; border: none; }
    #member-filter { border: round $surface; height: 3; }
    #member-filter:focus-within { border: round $accent; }
    #member-list { border: round $surface; height: 1fr; padding: 0 1; }
    #member-list:focus-within { border: round $accent; }
    .member-row { padding: 0 1; }
    """

    BINDINGS = [
        Binding("escape", "dismiss(None)", "skip"),
        Binding("enter", "pick", "pick"),
        Binding("/", "focus_filter", "filter", show=False),
    ]

    def __init__(self, members: list[Member], current_user_id: str) -> None:
        super().__init__()
        self._all = sorted(members, key=lambda m: m.username.lower())
        self._current_user_id = current_user_id
        self._visible: list[Member] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Reassign ticket to…[/]")
            yield Label(
                "[dim]type to filter · enter to pick · esc to skip[/]"
            )
            with Vertical(id="member-filter"):
                yield Input(placeholder="filter…", id="member-filter-input")
            yield ListView(id="member-list")

    def on_mount(self) -> None:
        self._apply_filter(filter_text="")
        self.query_one("#member-list", ListView).focus()

    def action_focus_filter(self) -> None:
        self.query_one("#member-filter-input", Input).focus()

    @on(Input.Changed, "#member-filter-input")
    def _on_filter(self, event: Input.Changed) -> None:
        self._apply_filter(filter_text=event.value.strip().lower())

    @on(Input.Submitted, "#member-filter-input")
    def _on_filter_submit(self, event: Input.Submitted) -> None:
        self.query_one("#member-list", ListView).focus()

    def _apply_filter(self, *, filter_text: str) -> None:
        listview = self.query_one("#member-list", ListView)
        listview.clear()
        self._visible = []

        for m in self._all:
            hay = f"{m.username} {m.email}".lower()
            if filter_text and filter_text not in hay:
                continue
            self._visible.append(m)
            you_tag = "  [dim](you)[/]" if m.user_id == self._current_user_id else ""
            email_tag = f"  [dim]<{m.email}>[/]" if m.email else ""
            listview.append(
                ListItem(
                    Static(f"{m.username}{email_tag}{you_tag}", classes="member-row"),
                )
            )
        if self._visible:
            listview.index = 0

    def action_pick(self) -> None:
        listview = self.query_one("#member-list", ListView)
        idx = listview.index
        if idx is None or not (0 <= idx < len(self._visible)):
            return
        self.dismiss(self._visible[idx])


# ---------- main app ------------------------------------------------------


class PostFlowApp(App[None]):
    """One screen, progressive sections, each step chained via modal."""

    CSS = """
    Screen { layout: vertical; padding: 0 1; }
    #branch-card {
        height: auto;
        padding: 1 2;
        margin: 1 0;
        border: round $surface;
    }
    #buttons { height: 3; padding: 0 1; }
    #buttons Button { margin-right: 1; }
    #log {
        height: 1fr;
        padding: 0 1;
        border: round $surface;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("y", "push", "push & open PR"),
        Binding("n", "skip_push", "skip push"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, inputs: PostFlowInputs) -> None:
        super().__init__()
        self._i = inputs
        self._pr_url: str | None = None
        self._pushed = False
        self._log_lines: list[str] = []

    # ------- compose / lifecycle --------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self._render_branch_card(), id="branch-card")
        with Horizontal(id="buttons"):
            yield Button(
                "Push & open PR" if not self._i.draft else "Push & open draft PR",
                id="push-btn",
                variant="primary",
            )
            yield Button("Skip — branch stays local", id="skip-btn")
        yield Static("", id="log")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "clickup-work · post-Claude"
        self.sub_title = self._i.task.id
        self.query_one("#push-btn", Button).focus()

    # ------- render ---------------------------------------------------------

    def _render_branch_card(self) -> str:
        merges_line = ""
        if self._i.merge_commits_ahead:
            merges_line = (
                f"\n[yellow]⚠ {self._i.merge_commits_ahead} of those are merge "
                f"commits — branch may be cut from a stale base.[/]"
            )
        draft = " (draft)" if self._i.draft else ""
        return (
            f"[bold]{self._i.task.name}[/]  [dim]· {self._i.task.id}[/]\n"
            f"\n"
            f"[dim]branch:[/]   {self._i.branch}\n"
            f"[dim]base:[/]     {self._i.base}\n"
            f"[dim]commits:[/]  {self._i.commits_ahead} ahead"
            f"{merges_line}\n"
            f"\n"
            f"Press [bold]Y[/] (or click) to push and open the PR{draft}.\n"
            f"Press [bold]N[/] to leave the branch local."
        )

    def _log(self, line: str) -> None:
        self._log_lines.append(line)
        self.query_one("#log", Static).update("\n".join(self._log_lines))

    # ------- buttons --------------------------------------------------------

    @on(Button.Pressed, "#push-btn")
    def _on_push_btn(self, event: Button.Pressed) -> None:
        self.action_push()

    @on(Button.Pressed, "#skip-btn")
    def _on_skip_btn(self, event: Button.Pressed) -> None:
        self.action_skip_push()

    # ------- actions --------------------------------------------------------

    def action_push(self) -> None:
        if self._pushed:
            return
        self._pushed = True
        self.query_one("#push-btn", Button).disabled = True
        self.query_one("#skip-btn", Button).disabled = True
        self._log(
            f"[dim]→[/] pushing {self._i.branch} and opening "
            f"{'draft PR' if self._i.draft else 'PR'}…"
        )
        try:
            commits = commit_subjects(self._i.repo_path, self._i.base)
        except GitError:
            commits = []
        try:
            url = push_and_open_pr(
                self._i.repo_path,
                branch=self._i.branch,
                base_branch=self._i.base,
                title=self._i.task.name,
                body=self._i.pr_body_builder(commits),
                draft=self._i.draft,
            )
        except GitError as e:
            self._log(f"[red]✗ push failed:[/] {e}")
            return

        self._pr_url = url
        label = "draft PR" if self._i.draft else "PR"
        # Plain URL — Textual's content markup parser refuses [link=…] when
        # the URL contains '://'. Most modern terminals make raw URLs
        # clickable anyway.
        self._log(f"[green]✓[/] {label} opened: {url}")
        self._start_post_pr_chain()

    def action_skip_push(self) -> None:
        if self._pushed:
            return
        self._log(
            f"[dim]skipping push. push manually with:[/] "
            f"git push -u origin {self._i.branch}"
        )
        self.exit(None)

    def action_quit(self) -> None:
        self.exit(None)

    # ------- post-PR modal chain --------------------------------------------

    def _start_post_pr_chain(self) -> None:
        """Walk through status → time spent → time estimate → reassign."""
        if self._i.prompt_status:
            self._step_status()
        elif self._i.prompt_time:
            self._step_time_spent()
        elif self._i.prompt_assign:
            self._step_reassign()
        else:
            self._finish()

    def _step_status(self) -> None:
        try:
            statuses = self._i.client.get_list_statuses(self._i.task.list_id)
        except ClickUpError as e:
            self._log(f"[yellow]could not load statuses ({e}); skipping[/]")
            self._step_time_spent()
            return
        if not statuses:
            self._log("[dim]no statuses configured on this list; skipping[/]")
            self._step_time_spent()
            return

        def after(value: str | None) -> None:
            if value and value.lower() != self._i.task.status.lower():
                try:
                    self._i.client.update_task_status(self._i.task.id, value)
                    self._log(f"[green]✓[/] ticket moved to [bold]{value}[/]")
                except ClickUpError as e:
                    self._log(f"[yellow]could not update status ({e})[/]")
            self._after_status()

        self.push_screen(
            StatusPrompt(self._i.task.name, self._i.task.status, statuses),
            after,
        )

    def _after_status(self) -> None:
        if self._i.prompt_time:
            self._step_time_spent()
        elif self._i.prompt_assign:
            self._step_reassign()
        else:
            self._finish()

    def _step_time_spent(self) -> None:
        # Reuse EstimatePrompt — same input shape, different submission.
        def after(value: str | None) -> None:
            if value:
                from clickup_work.cli import _parse_duration

                ms = _parse_duration(value)
                if ms is None:
                    self._log(f"[yellow]'{value}' isn't a duration; skipping log[/]")
                else:
                    try:
                        self._i.client.add_time_entry(
                            self._i.team_id, self._i.user_id, self._i.task.id, ms,
                        )
                        self._log(
                            f"[green]✓[/] logged {_format_ms(ms)} of time"
                        )
                    except ClickUpError as e:
                        self._log(f"[yellow]time log failed ({e})[/]")
            self._after_time_spent()

        prompt = EstimatePrompt(
            f"⏱  Track time SPENT — {self._i.task.name}", None
        )
        self.push_screen(prompt, after)

    def _after_time_spent(self) -> None:
        # Always offer estimate prompt as part of "time" since the original
        # flow paired them. --no-time skips both.
        self._step_time_estimate()

    def _step_time_estimate(self) -> None:
        def after(value: str | None) -> None:
            if value:
                from clickup_work.cli import _parse_duration

                ms = _parse_duration(value)
                if ms is None:
                    self._log(
                        f"[yellow]'{value}' isn't a duration; skipping estimate[/]"
                    )
                else:
                    try:
                        self._i.client.set_time_estimate(self._i.task.id, ms)
                        self._log(f"[green]✓[/] estimate set to {_format_ms(ms)}")
                    except ClickUpError as e:
                        self._log(f"[yellow]estimate failed ({e})[/]")
            self._after_time_estimate()

        prompt = EstimatePrompt(
            f"📐  Set time ESTIMATE — {self._i.task.name}",
            self._i.task.time_estimate,
        )
        self.push_screen(prompt, after)

    def _after_time_estimate(self) -> None:
        if self._i.prompt_assign:
            self._step_reassign()
        else:
            self._finish()

    def _step_reassign(self) -> None:
        try:
            members = self._i.client.get_team_members(self._i.team_id)
        except ClickUpError as e:
            self._log(f"[yellow]could not load members ({e}); skipping[/]")
            self._finish()
            return
        if not members:
            self._log("[dim]no members visible; skipping reassign[/]")
            self._finish()
            return

        def after(chosen: Member | None) -> None:
            if chosen and chosen.user_id != self._i.user_id:
                try:
                    self._i.client.update_task_assignees(
                        self._i.task.id,
                        add_ids=[chosen.user_id],
                        remove_ids=[],
                    )
                    self._log(
                        f"[green]✓[/] also assigned [bold]{chosen.username}[/]"
                    )
                except ClickUpError as e:
                    self._log(f"[yellow]reassign failed ({e})[/]")
            self._finish()

        self.push_screen(
            MemberPrompt(members, current_user_id=self._i.user_id),
            after,
        )

    def _finish(self) -> None:
        self._log("")
        self._log("[bold green]all done.[/]  press [bold]q[/] to exit.")


def _format_ms(ms: int) -> str:
    minutes = max(0, int(round(ms / 60_000)))
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def run_post_flow(inputs: PostFlowInputs) -> None:
    """Launch the post-Claude TUI. Side effects only; returns None."""
    PostFlowApp(inputs).run()
