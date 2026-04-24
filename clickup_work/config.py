from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "clickup-work" / "config.toml"


class ConfigError(Exception):
    pass


def add_folder_to_repo(nickname: str, folder_id: str) -> None:
    """Add `folder_id` to `[repos.<nickname>].folder_ids` in the config.

    Preserves the file byte-for-byte outside the single line we touch:
    comments, ordering, other keys, all kept. If `folder_ids` doesn't
    exist yet, a new single-line entry is inserted at the end of the
    block. If it exists (single- or multi-line), it's rewritten as a
    single-line list with the new id appended. Duplicates are no-ops.

    Writes are atomic: we build the new text in memory, parse it with
    `tomllib` to verify validity, write to a `.tmp` sibling, then
    `replace()` it into place. On any failure the original is
    untouched.
    """
    if not folder_id:
        raise ConfigError("folder_id is empty; nothing to add")
    if not CONFIG_PATH.exists():
        raise ConfigError(f"config not found at {CONFIG_PATH}")

    text = CONFIG_PATH.read_text()

    # Find the [repos.<nickname>] header. Allow both quoted and bare form.
    header_re = re.compile(
        rf'^\[\s*repos\.(?:"{re.escape(nickname)}"|{re.escape(nickname)})\s*\]\s*$',
        re.MULTILINE,
    )
    header_match = header_re.search(text)
    if not header_match:
        raise ConfigError(f"[repos.{nickname}] not found in {CONFIG_PATH}")

    # Section body runs from the end of the header line to the next [section]
    # header (any kind) or EOF.
    body_start = header_match.end()
    next_header = re.search(r'^\[', text[body_start:], re.MULTILINE)
    body_end = body_start + next_header.start() if next_header else len(text)

    block = text[body_start:body_end]

    # Existing single-or-multi-line `folder_ids = [...]` line.
    # `[^\]]*` spans newlines since character classes aren't bounded by `.`.
    folder_ids_re = re.compile(
        r'^(\s*folder_ids\s*=\s*)\[([^\]]*)\](.*)$',
        re.MULTILINE,
    )
    fm = folder_ids_re.search(block)

    if fm:
        existing: list[str] = []
        for part in fm.group(2).split(","):
            part = part.strip().strip('"').strip("'")
            if part:
                existing.append(part)
        if folder_id in existing:
            return  # already mapped — no-op, don't churn the file
        existing.append(folder_id)
        formatted = ", ".join(f'"{i}"' for i in existing)
        new_line = f"{fm.group(1)}[{formatted}]{fm.group(3)}"
        new_block = block[:fm.start()] + new_line + block[fm.end():]
    else:
        # Insert before trailing whitespace (so a blank line before the next
        # section stays a blank line).
        head = block.rstrip("\n")
        tail = block[len(head):]
        new_block = head + f'\nfolder_ids  = ["{folder_id}"]' + tail

    new_text = text[:body_start] + new_block + text[body_end:]

    # Round-trip through tomllib — if the rewrite isn't valid TOML, bail out
    # before the user's config is touched.
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(
            f"internal error: folder_ids rewrite produced invalid TOML "
            f"({e}). Config not modified."
        ) from None

    # Atomic write: tmp file, then replace.
    tmp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    tmp_path.write_text(new_text)
    tmp_path.replace(CONFIG_PATH)


def append_repo_block(nickname: str, path: str, base_branch: str) -> None:
    """Append a [repos.<nickname>] block to the config file.

    Creates the file (and its parent dir) if it doesn't exist yet. Preserves
    existing content/ordering/comments — plain text append, no TOML rewrite.
    Raises ConfigError if the nickname is already defined.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if CONFIG_PATH.exists():
        existing = CONFIG_PATH.read_text()
        # Detect existing [repos.<nickname>] (allow quoted form too).
        pattern = re.compile(
            rf'^\s*\[\s*repos\.(?:"{re.escape(nickname)}"|{re.escape(nickname)})\s*\]',
            re.MULTILINE,
        )
        if pattern.search(existing):
            raise ConfigError(
                f"a repo named '{nickname}' already exists in {CONFIG_PATH}. "
                f"Pick a different nickname or edit the config directly."
            )

    block = (
        f"\n[repos.{nickname}]\n"
        f'path        = "{path}"\n'
        f'base_branch = "{base_branch}"\n'
    )

    # Ensure a trailing newline before appending.
    if existing and not existing.endswith("\n"):
        existing += "\n"

    CONFIG_PATH.write_text(existing + block)


@dataclass(frozen=True)
class Repo:
    name: str
    path: Path
    base_branch: str  # may be "" to mean auto-detect at runtime
    branch_prefix: str  # may be "" to mean infer from ClickUp task type
    folder_ids: tuple[str, ...]  # ClickUp folder ids that route to this repo


@dataclass(frozen=True)
class Config:
    default_repo: str
    team_id: str
    list_id: str
    repos: dict[str, Repo]  # keyed by nickname


def validate_repo_path(raw: str) -> Path:
    repo_path = Path(os.path.expanduser(raw)).resolve()
    if not repo_path.exists():
        raise ConfigError(f"repo path does not exist: {repo_path}")
    if not (repo_path / ".git").exists():
        raise ConfigError(f"repo path is not a git repo (no .git dir): {repo_path}")
    return repo_path


def _parse_repo_block(name: str, block: dict) -> Repo:
    raw_path = (block.get("path") or "").strip()
    if not raw_path:
        raise ConfigError(f"[repos.{name}] is missing required `path`")
    path = validate_repo_path(raw_path)
    base = str(block.get("base_branch", "")).strip()
    prefix = str(block.get("branch_prefix", "")).strip().strip("/")
    raw_folders = block.get("folder_ids") or []
    if not isinstance(raw_folders, list):
        raise ConfigError(f"[repos.{name}].folder_ids must be an array of strings")
    folders = tuple(str(f).strip() for f in raw_folders if str(f).strip())
    return Repo(
        name=name,
        path=path,
        base_branch=base,
        branch_prefix=prefix,
        folder_ids=folders,
    )


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise ConfigError(
            f"no config yet at {CONFIG_PATH}\n"
            f"\n"
            f"the easy way — register your first repo with one command:\n"
            f"  clickup-work add-repo /path/to/your/repo\n"
            f"\n"
            f"or hand-edit (see config.toml.example in the repo for the format):\n"
            f"  mkdir -p {CONFIG_PATH.parent} && $EDITOR {CONFIG_PATH}"
        )

    with CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)

    repos: dict[str, Repo] = {}

    # New shape: [repos.<name>] tables.
    repos_tbl = raw.get("repos") or {}
    if not isinstance(repos_tbl, dict):
        raise ConfigError("`repos` must be a table of named repo blocks")
    for name, block in repos_tbl.items():
        if not isinstance(block, dict):
            raise ConfigError(f"[repos.{name}] must be a table")
        repos[name] = _parse_repo_block(name, block)

    # Back-compat: flat repo_path + base_branch at the top becomes an unnamed
    # repo available only when --repo is omitted or given as a path.
    legacy_path = (raw.get("repo_path") or "").strip()
    if legacy_path and not repos:
        repos["default"] = Repo(
            name="default",
            path=validate_repo_path(legacy_path),
            base_branch=str(raw.get("base_branch", "")).strip(),
            branch_prefix="",
            folder_ids=(),
        )

    default_repo = str(raw.get("default_repo", "")).strip()
    if default_repo and default_repo not in repos:
        raise ConfigError(
            f"default_repo='{default_repo}' is not defined under [repos.*]. "
            f"Known names: {', '.join(sorted(repos.keys())) or '(none)'}"
        )

    return Config(
        default_repo=default_repo,
        team_id=str(raw.get("team_id", "")).strip(),
        list_id=str(raw.get("list_id", "")).strip(),
        repos=repos,
    )


def resolve_repo(
    cfg: Config,
    repo_arg: str | None,
) -> Repo:
    """Resolve `--repo` argument (nickname or path) against the loaded config."""
    # Case 1: no --repo → use default_repo from config
    if not repo_arg:
        if not cfg.default_repo:
            if len(cfg.repos) == 1:
                return next(iter(cfg.repos.values()))
            names = ", ".join(sorted(cfg.repos.keys())) or "(none defined)"
            raise ConfigError(
                "no --repo given and no default_repo in config.\n"
                f"available repos: {names}\n"
                "pass --repo <name> or set default_repo in config.toml"
            )
        return cfg.repos[cfg.default_repo]

    # Case 2: nickname lookup
    if repo_arg in cfg.repos:
        return cfg.repos[repo_arg]

    # Case 3: looks like a path → treat as ad-hoc repo, no preset base_branch
    if repo_arg.startswith(("/", "~", "./", "../")):
        return Repo(
            name=Path(repo_arg).name,
            path=validate_repo_path(repo_arg),
            base_branch="",
            branch_prefix="",
            folder_ids=(),
        )

    names = ", ".join(sorted(cfg.repos.keys())) or "(none defined)"
    raise ConfigError(
        f"unknown repo: '{repo_arg}'\n"
        f"known names: {names}\n"
        f"(or pass an absolute/~ path)"
    )
