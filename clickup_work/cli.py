from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

from clickup_work import __version__
from clickup_work.claude import build_prompt, launch
from clickup_work.clickup import ClickUp, ClickUpError, Task
from clickup_work.config import (
    CONFIG_PATH,
    Config,
    ConfigError,
    Repo,
    add_folder_to_repo,
    append_repo_block,
    load as load_config,
    resolve_repo,
    validate_repo_path,
)
from clickup_work.git import (
    GitError,
    commits_ahead,
    detect_default_branch,
    prepare_branch,
    push_and_open_pr,
    remote_branch_exists,
)
from clickup_work.log import set_verbose
from clickup_work.spinner import Spinner


MAX_SLUG_LEN = 50
MIN_SLUG_LEN = 8
# Clause separators: em-dash and en-dash surrounded by whitespace.
# Colons are intentionally NOT split on — too often part of real titles.
_CLAUSE_SEP = re.compile(r"\s+[—–]\s+")


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def slug(text: str) -> str:
    # Keep only the lead clause when the title has em/en-dash subtitle.
    first_clause = _CLAUSE_SEP.split(text, maxsplit=1)[0]
    candidate = _slugify(first_clause)
    # If the lead clause is too short (e.g. "Fix — ..."), use the full title.
    if len(candidate) < MIN_SLUG_LEN:
        candidate = _slugify(text)
    if not candidate:
        return "task"
    if len(candidate) <= MAX_SLUG_LEN:
        return candidate
    # Truncate at the last word boundary before the limit.
    truncated = candidate[:MAX_SLUG_LEN]
    last_dash = truncated.rfind("-")
    if last_dash >= MIN_SLUG_LEN:
        truncated = truncated[:last_dash]
    return truncated.rstrip("-") or "task"


def infer_prefix(task_type: str) -> str:
    """Map a ClickUp task type to a conventional branch prefix."""
    t = (task_type or "").strip().lower()
    if "bug" in t or t in {"incident", "issue", "hotfix"}:
        return "fix"
    if "chore" in t or t == "task":
        # Generic "Task" covers most work; treat as feat for a cleaner default.
        return "feat"
    if "doc" in t:
        return "docs"
    return "feat"


def branch_name(task: Task, repo: Repo, cli_prefix: str | None) -> str:
    prefix = (
        (cli_prefix or "").strip().strip("/")
        or repo.branch_prefix
        or infer_prefix(task.task_type)
    )
    return f"{prefix}/{slug(task.name)}"


def pr_body(task: Task) -> str:
    desc = task.description.strip() or "_(no description on ticket)_"
    return (
        f"Closes ClickUp task [{task.id}]({task.url}).\n\n"
        f"## Ticket\n{desc}\n"
    )


def _die(msg: str, code: int = 1) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def _check_binaries() -> str | None:
    for bin_name in ("claude", "git", "gh"):
        if shutil.which(bin_name) is None:
            return bin_name
    return None


def _format_task_row(i: int, t: Task, location_tag: str) -> str:
    pr = (t.priority or "-").ljust(7)
    status = t.status.ljust(13)[:13]
    suffix = _row_suffix(t, location_tag)
    body = f"{t.name}  {suffix}" if suffix else t.name
    return f"{i}\t{pr}  {status}  {body}"


def _row_suffix(t: Task, location_tag: str) -> str:
    """Compose the trailing '[List] #tag1 (also in: …)' suffix for a row."""
    parts: list[str] = []
    if location_tag:
        parts.append(location_tag)
    if t.tags:
        parts.append(" ".join(f"#{tag}" for tag in t.tags))
    if t.locations:
        names = ", ".join(name for _, name in t.locations)
        parts.append(f"(also in: {names})")
    return "  ".join(parts)


def _task_location_tag(t: Task) -> str:
    """Render a breadcrumb like '[Folder / List]' for a non-grouped view."""
    if t.folder_name and t.list_name:
        return f"[{t.folder_name} / {t.list_name}]"
    if t.space_name and t.list_name:
        return f"[{t.space_name} / {t.list_name}]"
    if t.list_name:
        return f"[{t.list_name}]"
    return ""


def _group_key(t: Task) -> str:
    """Pick the most meaningful grouping label for a ticket.

    Folder is the primary grouping level when the ticket is in one. When the
    ticket is in a folderless list (list directly under a Space), the Space
    is the meaningful level — not some synthetic "(no folder)" bucket.
    """
    if t.folder_name:
        return t.folder_name
    if t.space_name:
        return t.space_name
    return ""


def _group_task_indices(tasks: list[Task]) -> list[tuple[str, list[int]]]:
    """Group task indices by their most meaningful level, preserving order.

    Input tasks arrive priority-sorted, so the first index in each group is
    that group's highest-priority ticket. Group order follows first-seen —
    equivalent to "sort groups by the priority of their top ticket".
    """
    indices: dict[str, list[int]] = {}
    seen: list[str] = []
    for i, t in enumerate(tasks):
        key = _group_key(t)
        if key not in indices:
            indices[key] = []
            seen.append(key)
        indices[key].append(i)
    return [(label, indices[label]) for label in seen]


def _group_list_summary(group_tasks: list[Task]) -> str | None:
    """If every ticket in the group shares one list, return that list name."""
    names = {t.list_name for t in group_tasks if t.list_name}
    if len(names) == 1:
        return names.pop()
    return None


_HEADER_WIDTH = 60


def _folder_header(text: str, count: int) -> str:
    text = text or "(no folder)"
    prefix = f"━━━ {text} ({count}) "
    pad = "━" * max(4, _HEADER_WIDTH - len(prefix))
    return prefix + pad


def _include_space_prefix(tasks: list[Task]) -> bool:
    """Only show Space in headers when the view actually spans multiple spaces.

    Single-space workspaces get a cleaner header without a repeated prefix;
    multi-space workspaces get disambiguation where it's meaningful.
    """
    spaces = {t.space_name for t in tasks if t.space_name}
    return len(spaces) > 1


def _group_display(
    group_tasks: list[Task],
    *,
    include_space: bool,
) -> tuple[str, bool]:
    """Build the header label for a group and report whether the list is in it.

    Returns (label, list_in_header). When list_in_header is True, the caller
    should drop the per-row [List] tag — it would be redundant.

    The label is a ' / '-joined breadcrumb of Space / Folder / List, with
    each segment skipped when unavailable. If every segment is unavailable
    the label falls back to "(no folder)".
    """
    t0 = group_tasks[0]
    parts: list[str] = []
    if include_space and t0.space_name:
        parts.append(t0.space_name)
    if t0.folder_name:
        parts.append(t0.folder_name)
    sublabel = _group_list_summary(group_tasks)
    if sublabel:
        parts.append(sublabel)
    label = " / ".join(parts) if parts else "(no folder)"
    return label, sublabel is not None


def pick_task(tasks: list[Task]) -> Task | None:
    """Interactive picker. Returns None if the user cancels."""
    if not tasks:
        return None
    if len(tasks) == 1:
        return tasks[0]

    if shutil.which("fzf"):
        return _pick_fzf(tasks)
    return _pick_numbered(tasks)


def _pick_fzf(tasks: list[Task]) -> Task | None:
    groups = _group_task_indices(tasks)
    multi_group = len(groups) > 1
    include_space = _include_space_prefix(tasks)

    rows: list[str] = []
    for _, indices in groups:
        group_tasks = [tasks[i] for i in indices]
        if multi_group:
            header_text, list_in_header = _group_display(
                group_tasks, include_space=include_space,
            )
            # Sentinel index -1 on header rows: fzf shows them (column 2+),
            # but an accidental "selection" falls through to the idx<0 branch
            # below and is treated as cancel.
            rows.append(f"-1\t{_folder_header(header_text, len(indices))}")
        else:
            list_in_header = False
        for i in indices:
            t = tasks[i]
            if multi_group:
                tag = "" if list_in_header or not t.list_name else f"[{t.list_name}]"
            else:
                tag = _task_location_tag(t)
            rows.append(_format_task_row(i, t, tag))

    result = subprocess.run(
        [
            "fzf",
            "--delimiter", "\t",
            "--with-nth", "2..",
            "--prompt", "pick a ticket > ",
            "--height", "40%",
            "--reverse",
            "--no-mouse",
        ],
        input="\n".join(rows),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        idx = int(result.stdout.split("\t", 1)[0])
    except ValueError:
        return None
    if 0 <= idx < len(tasks):
        return tasks[idx]
    return None


def _confirm(prompt: str, default_yes: bool = True) -> bool:
    """Yes/no prompt. Enter accepts the default. Ctrl-C / EOF → no."""
    try:
        raw = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not raw:
        return default_yes
    return raw in {"y", "yes"}


def _prompt_status_change(client: ClickUp, task: Task) -> None:
    """Offer to move the ClickUp ticket to a new status.

    Never raises: on any failure, prints a one-line warning that tells the
    user to move the ticket manually in ClickUp, and returns. The PR is
    already open by the time this runs, so partial failure here shouldn't
    tank the flow.
    """
    if not task.list_id:
        print(
            "[clickup-work] ticket has no list id; move it manually in ClickUp.",
            file=sys.stderr,
        )
        return

    try:
        with Spinner("loading ticket statuses") as sp:
            statuses = client.get_list_statuses(task.list_id)
            sp.silent()
    except ClickUpError as e:
        print(
            f"[clickup-work] could not fetch statuses ({e}); "
            f"move the ticket manually in ClickUp.",
            file=sys.stderr,
        )
        return

    if not statuses:
        print(
            "[clickup-work] no statuses configured on this list; "
            "move the ticket manually in ClickUp.",
            file=sys.stderr,
        )
        return

    chosen = _pick_status(statuses, current=task.status)
    if chosen is None or chosen.lower() == task.status.lower():
        print("ticket status unchanged.")
        return

    try:
        with Spinner(f"moving ticket to {chosen}") as sp:
            client.update_task_status(task.id, chosen)
            sp.ok(f"ticket moved: {task.status} → {chosen}")
    except ClickUpError as e:
        print(
            f"[clickup-work] could not update status ({e}); "
            f"move the ticket manually in ClickUp.",
            file=sys.stderr,
        )
        return


def _pick_status(statuses: list[str], current: str) -> str | None:
    if not statuses:
        return None
    if shutil.which("fzf"):
        return _pick_status_fzf(statuses, current)
    return _pick_status_numbered(statuses, current)


def _pick_status_fzf(statuses: list[str], current: str) -> str | None:
    def label(s: str) -> str:
        return f"{s} (current)" if s.lower() == current.lower() else s

    lines = "\n".join(f"{i}\t{label(s)}" for i, s in enumerate(statuses))
    result = subprocess.run(
        [
            "fzf",
            "--delimiter", "\t",
            "--with-nth", "2..",
            "--prompt", "move ticket to > ",
            "--height", "40%",
            "--reverse",
            "--no-mouse",
        ],
        input=lines,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        idx = int(result.stdout.split("\t", 1)[0])
    except ValueError:
        return None
    if 0 <= idx < len(statuses):
        return statuses[idx]
    return None


def _pick_status_numbered(statuses: list[str], current: str) -> str | None:
    print("\nMove ticket to which status?\n")
    for i, s in enumerate(statuses, 1):
        marker = "  (current)" if s.lower() == current.lower() else ""
        print(f"  {i:2}. {s}{marker}")
    print()
    while True:
        try:
            raw = input("pick a number (or q to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() in {"q", "quit", ""}:
            return None
        try:
            idx = int(raw) - 1
        except ValueError:
            print("  not a number; try again")
            continue
        if 0 <= idx < len(statuses):
            return statuses[idx]
        print(f"  out of range; choose 1–{len(statuses)}")


def _pick_numbered(tasks: list[Task]) -> Task | None:
    groups = _group_task_indices(tasks)
    multi_group = len(groups) > 1

    # Display-order → original index map, built in the same pass as the print.
    ordered: list[int] = []

    include_space = _include_space_prefix(tasks)
    print("\nOpen tickets assigned to you:\n")
    for _, indices in groups:
        group_tasks = [tasks[i] for i in indices]
        if multi_group:
            header_text, list_in_header = _group_display(
                group_tasks, include_space=include_space,
            )
            print(f"  {_folder_header(header_text, len(indices))}")
        else:
            list_in_header = False
        for i in indices:
            t = tasks[i]
            display_num = len(ordered) + 1
            ordered.append(i)
            pr = (t.priority or "-").ljust(7)
            if multi_group:
                # Drop [List] when the header already names it.
                if list_in_header or not t.list_name:
                    location_tag = ""
                else:
                    location_tag = f"[{t.list_name}]"
            else:
                location_tag = _task_location_tag(t)
            suffix = _row_suffix(t, location_tag)
            tail = f"  {suffix}" if suffix else ""
            print(f"  {display_num:2}. [{pr}] {t.name}{tail}")
        if multi_group:
            print()
    if not multi_group:
        print()
    while True:
        try:
            raw = input("pick a number (or q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() in {"q", "quit", ""}:
            return None
        try:
            n = int(raw) - 1
        except ValueError:
            print("  not a number; try again")
            continue
        if 0 <= n < len(ordered):
            return tasks[ordered[n]]
        print(f"  out of range; choose 1–{len(ordered)}")


def _resolve_base_branch(repo: Repo, cli_override: str | None) -> tuple[str, str]:
    """Return (branch, source) where source explains where it came from."""
    if cli_override:
        return cli_override, "--base flag"
    if repo.base_branch:
        return repo.base_branch, f"config [repos.{repo.name}].base_branch"
    detected = detect_default_branch(repo.path)
    if detected:
        return detected, "origin/HEAD"
    raise GitError(
        f"could not determine base branch for {repo.path}.\n"
        f"Fix by either:\n"
        f"  - setting base_branch under [repos.{repo.name}] in config, or\n"
        f"  - running `git remote set-head origin --auto` in the repo, or\n"
        f"  - passing --base <branch>"
    )


def _print_plan(task: Task, repo: Repo, base: str, base_source: str, branch: str) -> None:
    print(f"Ticket:   {task.name}  ({task.id})")
    print(f"Status:   {task.status}")
    print(f"Priority: {task.priority or 'unset'}")
    print(f"List:     {task.list_name}")
    print(f"URL:      {task.url}")
    print(f"Repo:     {repo.path}  (nickname: {repo.name})")
    print(f"Base:     {base}  (resolved from {base_source})")
    print(f"Branch:   {branch}  →  PR into {base}")


def _route_ticket(cfg: Config, task: Task) -> Repo | None:
    """Find the repo this ticket belongs to.

    Precedence: tag matches (case-insensitive) win over folder matches. A tag
    is an intentional label ("for project alpha"), folder is structural ("lives
    in QA"). Intent beats structure — important when a single shared folder
    holds tickets from many projects, each tagged with the project name.

    Returns None when nothing matches; caller falls through to the prompt.
    """
    if task.tags:
        ticket_tags_lc = {tg.lower() for tg in task.tags}
        for repo in cfg.repos.values():
            if any(rt.lower() in ticket_tags_lc for rt in repo.tags):
                return repo
    if task.folder_id:
        for repo in cfg.repos.values():
            if task.folder_id in repo.folder_ids:
                return repo
    return None


def _prompt_folder_mapping(cfg: Config, task: Task) -> Repo | None:
    """Ask which repo a newly-seen folder should route to.

    Returns the chosen repo, or None if the user cancels. The caller is
    responsible for persisting the mapping via add_folder_to_repo — this
    function only handles the interactive part.
    """
    if not cfg.repos:
        print(
            "no repos registered. run `clickup-work add-repo <path>` first.",
            file=sys.stderr,
        )
        return None

    folder_label = task.folder_name or "(no folder)"
    name_snip = task.name if len(task.name) <= 60 else task.name[:57] + "…"
    print()
    print(f"Ticket \"{name_snip}\" is in folder \"{folder_label}\",")
    print("which isn't linked to any repo yet.")
    print()
    print("Which repo should this folder route to?")
    repos = list(cfg.repos.values())
    for i, r in enumerate(repos, 1):
        print(f"  {i}. {r.name}  ({r.path})")
    print("  (or q to cancel and pass --repo manually)")
    print()
    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if raw.lower() in {"q", "quit", ""}:
            return None
        try:
            idx = int(raw) - 1
        except ValueError:
            print("  not a number; try again")
            continue
        if 0 <= idx < len(repos):
            return repos[idx]
        print(f"  out of range; choose 1–{len(repos)}")


def _resolve_upfront_repo(cfg: Config, repo_override: str | None) -> Repo | None:
    """Pick the repo to use before ticket selection, or None to route later.

    - --repo always wins.
    - default_repo or single-repo configs resolve upfront (backward compat).
    - Multi-repo configs with no default fall through to folder-based routing.
    """
    if repo_override:
        return resolve_repo(cfg, repo_override)
    if cfg.default_repo:
        return cfg.repos[cfg.default_repo]
    if len(cfg.repos) == 1:
        return next(iter(cfg.repos.values()))
    return None


def run(
    dry_run: bool,
    repo_override: str | None,
    base_override: str | None,
    prefix_override: str | None,
    pick: bool,
    draft: bool,
    prompt_status: bool,
    skip_confirm: bool,
) -> int:
    token = os.environ.get("CLICKUP_API_TOKEN", "").strip()
    if not token:
        return _die(
            "CLICKUP_API_TOKEN is not set.\n"
            "Get one at ClickUp → Settings → Apps → API Token, then:\n"
            "  export CLICKUP_API_TOKEN=pk_xxx..."
        )

    missing = _check_binaries()
    if missing:
        return _die(f"required binary not on PATH: {missing}")

    try:
        cfg = load_config()
        upfront_repo = _resolve_upfront_repo(cfg, repo_override)
    except ConfigError as e:
        return _die(str(e))

    client = ClickUp(token)
    # Scope the picker to the upfront repo's folders when set; otherwise show
    # every assigned ticket so the user can route by folder after picking.
    folder_filter = list(upfront_repo.folder_ids) if upfront_repo else []
    try:
        with Spinner("fetching open tickets") as sp:
            user_id = client.get_user_id()
            team_id = cfg.team_id or client.get_first_team_id()
            tasks = client.get_open_tasks(
                team_id=team_id,
                user_id=user_id,
                list_id=cfg.list_id,
                folder_ids=folder_filter,
            )
            sp.ok(f"found {len(tasks)} open ticket(s)")
    except ClickUpError as e:
        return _die(str(e))

    if not tasks:
        if upfront_repo and upfront_repo.folder_ids:
            print(
                f"no open tickets in folders linked to '{upfront_repo.name}'. "
                f"Run without --repo to see all your tickets."
            )
        else:
            print("no open tickets assigned to you (status: to do / in progress)")
        return 0

    if pick:
        task = pick_task(tasks)
        if task is None:
            print("cancelled.")
            return 0
    else:
        task = tasks[0]

    # Decide which repo this ticket belongs to.
    if upfront_repo:
        repo = upfront_repo
    else:
        routed = _route_ticket(cfg, task)
        if routed is not None:
            repo = routed
        else:
            chosen = _prompt_folder_mapping(cfg, task)
            if chosen is None:
                print("cancelled.")
                return 0
            repo = chosen
            # Only persist a mapping if we have a folder id to map against.
            # Tickets in folderless lists (folder.hidden=true) don't get saved.
            if task.folder_id:
                try:
                    add_folder_to_repo(repo.name, task.folder_id)
                    print(
                        f"✓ folder \"{task.folder_name}\" now routes to "
                        f"'{repo.name}' (saved to {CONFIG_PATH})"
                    )
                except ConfigError as e:
                    print(
                        f"[clickup-work] warning: couldn't save mapping ({e}). "
                        f"continuing with '{repo.name}' for this run only.",
                        file=sys.stderr,
                    )

    try:
        base, base_source = _resolve_base_branch(repo, base_override)
    except GitError as e:
        return _die(str(e))

    branch = branch_name(task, repo, prefix_override)
    _print_plan(task, repo, base, base_source, branch)

    # Safety check: confirm the base actually exists on origin BEFORE any git ops.
    # This catches typos, renamed defaults, and missing branches.
    if not remote_branch_exists(repo.path, base):
        return _die(
            f"base branch '{base}' does not exist on origin in {repo.path}.\n"
            f"  Check `git branch -r` or fix base_branch in config / --base flag."
        )

    if dry_run:
        print("\n--dry-run: stopping before touching git")
        return 0

    try:
        with Spinner(f"preparing branch {branch}") as sp:
            state = prepare_branch(repo.path, base, branch)
            verb = "reused" if state == "reused" else "created"
            sp.ok(f"branch {verb}: {branch}")
    except GitError as e:
        return _die(str(e))

    prompt = build_prompt(task, branch=branch, base_branch=base)
    print("\nlaunching Claude Code… (exit the session to come back here)\n")
    exit_code = launch(prompt, cwd=repo.path)
    print(f"\nclaude exited with status {exit_code}")

    try:
        ahead = commits_ahead(repo.path, base)
    except GitError as e:
        return _die(f"could not count commits: {e}")

    if ahead == 0:
        print("no new commits on the branch — skipping PR")
        return 0

    label = "draft PR" if draft else "PR"
    print(f"{ahead} commit(s) ahead of {base}.")

    if not skip_confirm and not _confirm(f"push branch and open {label}? [Y/n] "):
        print(
            f"skipping push. branch '{branch}' is local; "
            f"push manually when ready (git push -u origin {branch})."
        )
        return 0

    try:
        with Spinner(f"opening {label}") as sp:
            url = push_and_open_pr(
                repo.path,
                branch=branch,
                base_branch=base,
                title=task.name,
                body=pr_body(task),
                draft=draft,
            )
            sp.ok(f"{label} opened: {url}")
    except GitError as e:
        return _die(str(e))

    if prompt_status:
        _prompt_status_change(client, task)

    return 0


def _run_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="clickup-work",
        description="Pick a ClickUp ticket and start a Claude Code session on it.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and print the selected ticket without touching git or launching Claude",
    )
    parser.add_argument(
        "--repo",
        metavar="NAME_OR_PATH",
        help="repo nickname from config [repos.*], or an absolute/~ path for ad-hoc use",
    )
    parser.add_argument(
        "--base",
        metavar="BRANCH",
        help="override base branch for this invocation (wins over config and auto-detect)",
    )
    parser.add_argument(
        "--prefix",
        metavar="NAME",
        help="override branch prefix (e.g. feat, fix, chore, docs). "
             "Wins over repo config and task-type inference.",
    )
    parser.add_argument(
        "-t", "--top",
        action="store_true",
        help="skip the picker and auto-select the top-priority ticket",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="open the resulting PR as a draft",
    )
    parser.add_argument(
        "--no-status",
        action="store_true",
        help="skip the 'move ticket to which status?' prompt after the PR opens",
    )
    parser.add_argument(
        "-y", "--yes",
        action="store_true",
        help="skip the 'push branch and open PR?' confirmation prompt",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="print every API call and git/gh command the tool runs",
    )
    parser.add_argument("--version", action="version", version=f"clickup-work {__version__}")
    args = parser.parse_args(argv)
    set_verbose(args.verbose)
    return run(
        dry_run=args.dry_run,
        repo_override=args.repo,
        base_override=args.base,
        prefix_override=args.prefix,
        pick=not args.top,
        draft=args.draft,
        prompt_status=not args.no_status,
        skip_confirm=args.yes,
    )


def _default_nickname(path: str) -> str:
    name = os.path.basename(os.path.abspath(os.path.expanduser(path)))
    # Trim common suffixes that don't add signal to a nickname.
    for suffix in ("-platform", "-app", "-web", "-service", "-api"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name or "repo"


def _prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{question}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return raw or default


def _add_repo_cmd(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="clickup-work add-repo",
        description="Register a new repo in the clickup-work config.",
    )
    parser.add_argument("path", help="absolute or ~ path to an existing git repo")
    parser.add_argument(
        "--name",
        metavar="NICKNAME",
        help="nickname to use (skips the interactive prompt)",
    )
    parser.add_argument(
        "--base-branch",
        metavar="BRANCH",
        help="base branch (skips auto-detect from origin/HEAD)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="print git commands the tool runs",
    )
    args = parser.parse_args(argv)
    set_verbose(args.verbose)

    try:
        repo_path = validate_repo_path(args.path)
    except ConfigError as e:
        return _die(str(e))

    print(f"Found repo at {repo_path}")

    base = (args.base_branch or "").strip()
    if not base:
        detected = detect_default_branch(repo_path)
        if not detected:
            return _die(
                "could not auto-detect default branch "
                "(`git symbolic-ref refs/remotes/origin/HEAD` failed).\n"
                "Pass --base-branch <branch> explicitly."
            )
        base = detected
        print(f"Detected default branch: {base}")

    default_name = args.name or _default_nickname(str(repo_path))
    if args.name:
        nickname = args.name.strip()
    else:
        nickname = _prompt("Nickname for this repo", default_name)
    if not nickname:
        return _die("no nickname given; aborting.")
    if not re.fullmatch(r"[a-z0-9_-]+", nickname):
        return _die(
            f"nickname '{nickname}' contains invalid characters "
            "(allowed: lowercase a-z, 0-9, '-', '_')."
        )

    try:
        append_repo_block(nickname, str(repo_path), base)
    except ConfigError as e:
        return _die(str(e))

    print(f"Added [repos.{nickname}] to {CONFIG_PATH}")
    print(f"Now run: clickup-work --repo {nickname}")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if argv and argv[0] == "add-repo":
        return _add_repo_cmd(argv[1:])
    return _run_cmd(argv)
