from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "clickup-work" / "config.toml"


class ConfigError(Exception):
    pass


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
    return Repo(name=name, path=path, base_branch=base, branch_prefix=prefix)


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise ConfigError(
            f"config not found at {CONFIG_PATH}\n"
            f"create it with:\n"
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
        )

    names = ", ".join(sorted(cfg.repos.keys())) or "(none defined)"
    raise ConfigError(
        f"unknown repo: '{repo_arg}'\n"
        f"known names: {names}\n"
        f"(or pass an absolute/~ path)"
    )
