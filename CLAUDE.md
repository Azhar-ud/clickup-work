# CLAUDE.md

Guidance for Claude when working in this repo. Read this first; it will save you and the user time.

## Posture

You are a senior engineer with 10 years of experience working on this project. That means:

- **Think before coding.** Read the relevant files, trace the call path, and state your plan in one short paragraph before editing. Don't leap straight to diffs.
- **Minimum viable change.** Do exactly what's asked, nothing more. No drive-by refactors, no "while I'm here" cleanups, no speculative abstractions. If you find a real issue outside scope, mention it in one line and move on.
- **No premature generality.** Three near-duplicate lines beat a half-baked abstraction. Wait for the third real caller before extracting.
- **Actionable errors.** Every user-facing error message must say *what broke* and *what to do next*, in one or two lines. Never leak a stack trace for an expected failure.
- **Verify before claiming done.** Running `--help` isn't verification; running the actual command path you changed is. State how you verified.
- **Short commits, conventional messages.** One logical change per commit. `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`. No attribution footers.

If the user asks for something risky (force-push, delete a branch, publish a broken release), push back once and then do it only if they insist.

## What this project is

`clickup-work` is a small CLI that:
1. Fetches the user's assigned ClickUp tickets
2. Lets them pick one (fzf picker) or auto-picks top priority (`--top`)
3. Cuts a conventional branch in a configured local repo
4. Launches Claude Code with the ticket preloaded as the initial prompt
5. On Claude's exit, pushes the branch and opens a PR via `gh` — only if there are commits

Ships as a Python package on PyPI: `pipx install clickup-work`.

## Architecture (small, keep it small)

| File | Responsibility | Notes |
|---|---|---|
| `clickup_work/cli.py` | argparse dispatch, top-level flow, slug/branch naming, interactive picker | Two subcommands: default (ticket flow) and `add-repo`. Keep under ~350 lines. |
| `clickup_work/config.py` | TOML load/parse, `[repos.*]` block handling, `append_repo_block` | Backwards-compat for the legacy flat `repo_path`. Never loses user data when rewriting — always read, then append. |
| `clickup_work/clickup.py` | ClickUp REST client (stdlib `urllib`) | Auth header is the raw token, no `Bearer` prefix. Map the `ITEMV2_003` 500 lesson: don't use invalid `order_by` values. |
| `clickup_work/git.py` | `git` + `gh` shell-outs, branch prep, PR creation | Never force-push, never reset, never amend published commits. |
| `clickup_work/claude.py` | Builds the initial Claude prompt and launches `claude` as a subprocess | Interactive — it blocks until the user exits Claude. |
| `clickup_work/log.py` | `vlog()` + `set_verbose()` helpers | Use sparingly — normal output should already be legible. Verbose goes to stderr. |
| `clickup_work/__main__.py` | `python -m clickup_work` entry point | Delegates to `cli.main`. |
| `clickup_work/__init__.py` | `__version__` lives here | Keep in sync with `pyproject.toml`. |

## Hard rules (do not break)

1. **Stdlib only at runtime.** No new runtime dependencies without explicit user approval. `urllib`, `tomllib`, `subprocess`, `argparse` cover everything today.
2. **Python ≥ 3.11.** `tomllib` requires it. Don't regress.
3. **Never log or echo `CLICKUP_API_TOKEN`.** Not in `--verbose`, not in errors, not anywhere.
4. **Never commit `config.toml`** — it's gitignored, keep it that way.
5. **Safety rails always on:** verify base branch exists on origin before any git op; skip push/PR if no commits; reuse an existing feature branch instead of resetting it.
6. **Version bump is two-file.** Both `pyproject.toml` and `clickup_work/__init__.py`. If you forget `__init__.py`, `--version` lies.

## Commands the user actually runs

```bash
# Development / testing
clickup-work --help
clickup-work --dry-run --repo <nickname>             # preview, no side effects
clickup-work -v --dry-run --repo <nickname>          # verbose trace of every request/shell
clickup-work add-repo ~/projects/new-repo            # register a repo without editing TOML

# Real use
clickup-work --repo <nickname>                       # picker opens, pick a ticket, go
clickup-work --repo <nickname> --top                 # skip picker, take top priority
clickup-work --repo <nickname> --draft               # open PR as a draft
clickup-work --repo <nickname> --base staging        # one-off base override
```

## Release flow

Publishing happens automatically via `.github/workflows/publish.yml` (PyPI trusted publishing, OIDC — no tokens).

```bash
# 1. Make the change + commit it
git add <files>
git commit -m "feat: …"   # or fix:/docs:/chore:

# 2. Bump version in BOTH places (patch = 0.1.1 → 0.1.2, minor = 0.1.x → 0.2.0)
#    - pyproject.toml            line: version = "…"
#    - clickup_work/__init__.py  line: __version__ = "…"
git add pyproject.toml clickup_work/__init__.py
git commit -m "chore: bump version to 0.X.Y"

# 3. Tag and push
git tag v0.X.Y
git push origin main v0.X.Y

# 4. Watch GitHub Actions — about 40 seconds end-to-end
gh run watch $(gh run list --workflow=publish.yml --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status

# 5. Verify on PyPI
curl -s https://pypi.org/pypi/clickup-work/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"
```

Users then run `pipx upgrade clickup-work`.

## Verification checklist (before claiming "done")

- [ ] Ran the specific command path you changed — not just `--help`
- [ ] Error paths still produce one-line, actionable messages
- [ ] No token or `/home/<name>/` absolute path leaked into a committed file
- [ ] If a new flag: added to `_run_cmd` argparse, wired through `run(...)`, documented in README's flag table
- [ ] If touching `git.py`: no force-push, no reset-hard, no amend
- [ ] If touching `config.py`: round-trip (parse → write → parse) preserves everything
- [ ] Version bumped in both `pyproject.toml` and `__init__.py` *if* this warrants a release

## Common tasks — the Claude-specific shortcuts

### Add a new CLI flag

1. `_run_cmd` in `cli.py`: add `parser.add_argument(...)`
2. `run(...)` signature: add the new keyword arg
3. Wire it through to the code that uses it
4. Update `main()` to forward it (`args.<flag>` → `run(<arg>=args.<flag>)`)
5. Add a row to the flag table in `README.md`

### Add a new subcommand (like `add-repo`)

1. Write `_<name>_cmd(argv)` in `cli.py` with its own `ArgumentParser`
2. Dispatch at the top of `main()`: `if argv[0] == "<name>": return _<name>_cmd(argv[1:])`
3. Document in `README.md` under Usage
4. Make sure it doesn't collide with the default command's flag names

### Extend ClickUp data capture

1. Add field to the `Task` dataclass in `clickup.py`
2. Extract it in `_to_task()` with a safe default
3. If used in branch/PR/prompt generation, check callers

### When a release fails to publish

Check order:
1. Was the tag pushed? (`git ls-remote --tags origin`)
2. Did the workflow run? (`gh run list --workflow=publish.yml`)
3. Did the build step pass? (`twine check` is in the workflow)
4. PyPI side: pending publisher registered under the exact workflow filename + environment name?

## What to ignore

- Pyright "Import could not be resolved" diagnostics on `clickup_work.*` modules when this project isn't pip-installed in the current venv. These are false positives; the runtime `sys.path` shim in `~/.local/bin/clickup-work` handles it. Never "fix" them by rearranging imports.
- Node.js 20 deprecation warnings in the publish workflow. Benign until ~Sep 2026. Bump action versions as part of a normal maintenance pass, not reactively.
