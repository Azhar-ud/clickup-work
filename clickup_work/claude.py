from __future__ import annotations

import subprocess
from pathlib import Path

from clickup_work.clickup import Task


def build_prompt(task: Task, branch: str, base_branch: str) -> str:
    desc = task.description or "(no description)"
    priority = task.priority or "unset"
    return (
        f"You are starting work on a ClickUp ticket. The branch "
        f"`{branch}` has already been created from `{base_branch}` "
        f"in this repo; commit your work there.\n\n"
        f"## Ticket\n"
        f"**{task.name}**\n"
        f"- ID: {task.id}\n"
        f"- Status: {task.status}\n"
        f"- Priority: {priority}\n"
        f"- List: {task.list_name}\n"
        f"- URL: {task.url}\n\n"
        f"## Description\n{desc}\n\n"
        f"Start by reading the relevant code to understand scope, then "
        f"propose a short plan before making changes."
    )


def launch(prompt: str, cwd: Path) -> int:
    """Run `claude <prompt>` interactively in cwd. Returns the exit code."""
    result = subprocess.run(["claude", prompt], cwd=cwd, check=False)
    return result.returncode
