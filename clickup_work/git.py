from __future__ import annotations

import subprocess
from pathlib import Path

from clickup_work.log import vlog


class GitError(Exception):
    pass


def _run(cmd: list[str], cwd: Path, check: bool = True, capture: bool = False) -> str:
    vlog(f"run (cwd={cwd}): {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            text=True,
            capture_output=capture,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        stdout = (e.stdout or "").strip()
        detail = stderr or stdout or f"exit {e.returncode}"
        raise GitError(f"{' '.join(cmd)} failed: {detail}") from None
    return (proc.stdout or "").strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def detect_default_branch(repo: Path) -> str | None:
    """Return the repo's default branch per origin/HEAD, or None if unknown."""
    # Try the local cached symbolic ref first (fast path).
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        ref = result.stdout.strip()
        if ref.startswith("origin/"):
            return ref.split("/", 1)[1]

    # Fall back to asking origin (requires network + access).
    result = subprocess.run(
        ["git", "ls-remote", "--symref", "origin", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            if line.startswith("ref: "):
                ref = line.split()[1]
                if ref.startswith("refs/heads/"):
                    return ref[len("refs/heads/"):]
    return None


def remote_branch_exists(repo: Path, branch: str) -> bool:
    """True iff origin has a head ref named <branch>."""
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", "origin", branch],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def prepare_branch(repo: Path, base_branch: str, branch: str) -> str:
    """Fetch, fast-forward base, then check out (or create) the feature branch.

    Returns "created" if the feature branch was newly created, "reused" if it
    already existed locally and was checked out again. The caller owns any
    user-facing message — this function stays quiet so it composes with a
    spinner wrapper.
    """
    _run(["git", "fetch", "origin", "--prune"], cwd=repo)
    _run(["git", "checkout", base_branch], cwd=repo)
    # Fast-forward only; fail loudly if base has diverged locally.
    _run(["git", "pull", "--ff-only", "origin", base_branch], cwd=repo)

    if _branch_exists(repo, branch):
        _run(["git", "checkout", branch], cwd=repo)
        return "reused"
    _run(["git", "checkout", "-b", branch], cwd=repo)
    return "created"


def commits_ahead(repo: Path, base_branch: str) -> int:
    out = _run(
        ["git", "rev-list", "--count", f"origin/{base_branch}..HEAD"],
        cwd=repo,
        capture=True,
    )
    return int(out or "0")


def commit_subjects(repo: Path, base_branch: str) -> list[str]:
    """Return commit subjects on the current branch since base, oldest first."""
    out = _run(
        ["git", "log", "--reverse", "--format=%s", f"origin/{base_branch}..HEAD"],
        cwd=repo,
        capture=True,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def push_and_open_pr(
    repo: Path,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
    draft: bool = False,
) -> str:
    _run(["git", "push", "-u", "origin", branch], cwd=repo)
    cmd = [
        "gh", "pr", "create",
        "--base", base_branch,
        "--head", branch,
        "--title", title,
        "--body", body,
    ]
    if draft:
        cmd.insert(3, "--draft")
    url = _run(cmd, cwd=repo, capture=True)
    return url
