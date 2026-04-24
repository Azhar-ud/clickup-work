# clickup-work

> A tiny CLI that picks the next ClickUp ticket assigned to you, cuts the
> right branch in the right repo, and launches a Claude Code session with
> the ticket pre-loaded. On exit, it opens a GitHub PR.

One command, from terminal to solving the problem.

## Quick start

Four commands from zero to running:

```bash
# 1. Install
pipx install clickup-work

# 2. Set your ClickUp API token (generate at ClickUp → Settings → Apps → API Token)
export CLICKUP_API_TOKEN=pk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 3. Register the repo you want to work in
clickup-work add-repo ~/projects/my-app

# 4. Go
clickup-work --repo my-app
```

Make the token permanent by appending the `export` line to `~/.zshrc` or `~/.bashrc`.
Full details on each step are below.

## What it does

```
$ clickup-work --repo my-app

  urgent   in progress    Fix flaky checkout tests                  [Sprint 12]
  high     to do          Add dark-mode toggle                      [Sprint 12]
  normal   to do          Refactor notification queue               [Infra]
pick a ticket >

Ticket:   Add dark-mode toggle  (86c9abc)
Repo:     /home/you/projects/my-app  (nickname: my-app)
Base:     main  (resolved from config [repos.my-app].base_branch)
Branch:   feat/add-dark-mode-toggle  →  PR into main

launching Claude Code… (exit the session to come back here)

(you work with Claude, commit, exit)

1 commit(s) to push; opening PR…
PR opened: https://github.com/you/my-app/pull/42
```

## Why

Because switching context between ClickUp, your terminal, your git branches,
and your editor is the slowest part of shipping a ticket. This automates the
mechanical parts and hands control to Claude Code for the actual work.

- Picks the right ticket (interactive fzf picker, or `--top` to auto-pick)
- Cuts a conventional branch (`feat/<slug>`, `fix/<slug>`, `docs/<slug>`,
  inferred from ClickUp task type — or override with `--prefix`)
- Never guesses the base branch (explicit per-repo config, `origin/HEAD`
  fallback, verified against origin *before* any git operation)
- Opens a PR automatically via `gh` when Claude's session ends with commits
- Skips the PR cleanly if no commits were made (no noise, no force-push)

## Requirements

- Python 3.11+
- [`claude`](https://github.com/anthropics/claude-code) — Claude Code CLI
- [`git`](https://git-scm.com/) and [`gh`](https://cli.github.com/), with `gh auth login` done
- `fzf` (optional; falls back to a numbered picker)
- A ClickUp API token ([generate one](https://clickup.com/api) under Settings → Apps → API Token)

## Install

### With pipx (recommended)

```bash
pipx install clickup-work
```

Don't have `pipx`? `pip install --user clickup-work` also works on most systems.
On Arch/Debian-managed Pythons (PEP 668), use `pipx` or a venv.

### Install the latest dev version directly from GitHub

```bash
pipx install git+https://github.com/Azhar-ud/clickup-work.git
```

### From source

```bash
git clone https://github.com/Azhar-ud/clickup-work.git
cd clickup-work
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

`clickup-work` is now on your PATH.

## Configure

### 1. Token

```bash
export CLICKUP_API_TOKEN=pk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Add this to `~/.zshrc` / `~/.bashrc` so it persists across shells.

### 2. Config file

Copy the example and edit:

```bash
mkdir -p ~/.config/clickup-work
cp config.toml.example ~/.config/clickup-work/config.toml
$EDITOR ~/.config/clickup-work/config.toml
```

The quickest way to add a repo is the built-in helper — no TOML syntax to
remember:

```bash
clickup-work add-repo ~/projects/my-app
# Found repo at /home/you/projects/my-app
# Detected default branch: main
# Nickname for this repo [my-app]: my-app
# Added [repos.my-app] to ~/.config/clickup-work/config.toml
```

Or hand-write a block:

```toml
[repos.my-app]
path          = "/home/you/projects/my-app"
base_branch   = "main"
# branch_prefix = "feat"   # optional override
```

See [`config.toml.example`](config.toml.example) for all fields.

## Usage

```bash
# Pick a ticket from the fzf picker, open it in the named repo
clickup-work --repo my-app

# Skip the picker; take the top-priority ticket
clickup-work --repo my-app --top

# One-off overrides
clickup-work --repo my-app --base staging          # target a different base
clickup-work --repo my-app --prefix fix            # force fix/… prefix
clickup-work --repo my-app --draft                 # open PR as a draft

# Preview only — no git, no Claude
clickup-work --repo my-app --dry-run

# See every API call and git command
clickup-work --repo my-app --verbose

# Register a new repo in config
clickup-work add-repo ~/projects/new-repo [--name nickname] [--base-branch main]
```

### Full flag list

| Flag | Purpose |
|---|---|
| `--repo NAME_OR_PATH` | Repo nickname from config, or an absolute/`~` path |
| `--base BRANCH` | Override base branch for this run |
| `--prefix NAME` | Override branch prefix (`feat`, `fix`, `chore`, `docs`, …) |
| `--top`, `-t` | Auto-pick top-priority ticket (skip picker) |
| `--draft` | Open the resulting PR as a draft |
| `--no-status` | Skip the "move ticket to which status?" prompt after the PR opens |
| `--yes`, `-y` | Skip the "push branch and open PR?" confirmation prompt |
| `--dry-run` | Preview the ticket + plan, touch nothing |
| `--verbose`, `-v` | Print every HTTP request and shell command |

## How it picks the base branch

Resolution order, first non-empty wins:

1. `--base <branch>` flag
2. `[repos.<name>].base_branch` in config
3. `git symbolic-ref refs/remotes/origin/HEAD` (auto-detect)
4. Error out — the tool refuses to guess

Before any branching, it verifies the resolved base exists on `origin`:

```
git ls-remote --exit-code --heads origin <base>
```

If it doesn't, the tool aborts with a clear message — no stray branches, no
PRs targeting a dead base.

## How it picks the branch prefix

| Condition | Prefix |
|---|---|
| `--prefix <name>` | Whatever you pass |
| `[repos.<name>].branch_prefix` in config | That value |
| ClickUp task type contains "bug" / is incident/hotfix | `fix` |
| ClickUp task type contains "doc" | `docs` |
| Anything else | `feat` |

## Post-session behavior

When you exit Claude:

```
ahead = git rev-list --count origin/<base>..HEAD
```

- `ahead == 0` → print "no new commits on the branch — skipping PR", exit 0
- `ahead ≥ 1` → ask `push branch and open PR? [Y/n]`; on yes, `git push -u origin <branch>` then `gh pr create --base <base> --head <branch>`. Pass `--yes` / `-y` to skip the prompt.
- `--draft` passed → PR is opened as a draft

If you answer `n` at the confirmation, the feature branch stays local —
you can push it by hand whenever you're ready. No force-push, no reset.

Once the PR is open, the tool prompts you to move the ClickUp ticket to a
new status — pulled live from the ticket's list, so whatever your workspace
is configured to use (`in review`, `qa`, `blocked`, …) is what you'll see.
Pick one to update, or hit `q` / `Esc` to leave it where it is. Pass
`--no-status` to skip the prompt entirely.

## Safety

- Plaintext of your token never leaves `CLICKUP_API_TOKEN` (env) or your shell rc.
  Nothing is written back to disk by this tool.
- The tool never does `git reset --hard`, force-pushes, or amends.
- If a feature branch already exists, it's reused (not reset) — good for
  resuming partial work.
- `--dry-run` runs everything up to "touch disk", including the
  branch-exists-on-origin check, and stops there.

## License

MIT. See [LICENSE](LICENSE).
