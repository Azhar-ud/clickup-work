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
2. Lets them pick one via a Textual TUI (or `--top` to auto-pick top priority)
3. Confirms the cut on a plan screen (ticket card + base-branch input)
4. Cuts a conventional branch in a configured local repo
5. Launches Claude Code with the ticket preloaded as the initial prompt
6. On Claude's exit, opens a post-Claude TUI: push & PR, status, time, reassign — only when there are commits

Plain-text flow (`--no-tui` or non-TTY stdout) is preserved end-to-end for CI and pipes.

Ships as a Python package on PyPI: `pipx install clickup-work`.

## Architecture (small, keep it small)

| File | Responsibility | Notes |
|---|---|---|
| `clickup_work/cli.py` | argparse dispatch, top-level orchestration, slug/branch naming, plain-text picker fallbacks | Subcommands: default (ticket flow), `add-repo`, `login`, `workload`. Larger than 350 lines now (~1500); keep it focused on dispatch and gluing TUI calls — push logic into the TUI modules. |
| `clickup_work/config.py` | TOML load/parse, `[repos.*]` and `[workload]` block handling, atomic writers (`save_token`, `append_repo_block`, `write_workload_capacity`) | Round-trip safe: parse → write → parse always preserves user data. Never loses comments/order. Repo path is no longer validated at config-load — deferred to use-time so a stale `[repos.*]` doesn't block repo-agnostic subcommands. |
| `clickup_work/clickup.py` | ClickUp REST client (stdlib `urllib`) | Auth header is the raw token, no `Bearer` prefix. Map the `ITEMV2_003` 500 lesson: don't use invalid `order_by` values. `Task` includes `due_date` and `time_estimate` (epoch ms / ms or None). |
| `clickup_work/git.py` | `git` + `gh` shell-outs, branch prep, PR creation | Never force-push, never reset, never amend published commits. |
| `clickup_work/claude.py` | Builds the initial Claude prompt and launches `claude` as a subprocess | Interactive — it blocks until the user exits Claude. Sits between the plan-screen TUI and the post-Claude TUI; each TUI exits cleanly before/after Claude takes the terminal. |
| `clickup_work/picker.py` | `TicketPickerApp` — Textual TUI replacing the fzf/numbered picker | Filterable, grouped by folder, color-coded priority. Default surface; fzf falls back when `--no-tui` is set. |
| `clickup_work/plan_screen.py` | `PlanApp` — plan card + base-branch override | Shown between picker and Claude launch. Returns the confirmed base or None on cancel. |
| `clickup_work/post_flow.py` | `PostFlowApp` — post-Claude TUI (push, status, time, reassign), plus `MemberPrompt` modal | Reuses `StatusPrompt` and `EstimatePrompt` from `tui.py`. Modal chain via push_screen callbacks. |
| `clickup_work/tui.py` | `WorkloadApp` — workload TUI; shared `StatusPrompt` and `EstimatePrompt` modals used by other surfaces | Workload report + inline `e`/`s`/`r` mutations. |
| `clickup_work/workload.py` | Pure logic: bucket tasks into this week / next week, render plain-text fallback | No I/O. Used by both the TUI and the `--no-tui` path. |
| `clickup_work/log.py` | `vlog()` + `set_verbose()` helpers | Use sparingly — normal output should already be legible. Verbose goes to stderr. |
| `clickup_work/__main__.py` | `python -m clickup_work` entry point | Delegates to `cli.main`. |
| `clickup_work/__init__.py` | `__version__` lives here | Keep in sync with `pyproject.toml`. |

## Hard rules (do not break)

1. **Runtime deps: `textual` only.** Beyond the stdlib (`urllib`, `tomllib`, `subprocess`, `argparse`, `webbrowser`), `textual` is the one approved runtime dependency — added in 0.14.0 to power the TUI surfaces. **Don't add others** without explicit user approval (and update this rule when you do).
2. **Python ≥ 3.11.** `tomllib` requires it. Don't regress.
3. **Never log or echo `CLICKUP_API_TOKEN`.** Not in `--verbose`, not in errors, not anywhere.
4. **Never commit `config.toml`** — it's gitignored, keep it that way.
5. **Safety rails always on:** verify base branch exists on origin before any git op; skip push/PR if no commits; reuse an existing feature branch instead of resetting it.
6. **Version bump is two-file.** Both `pyproject.toml` and `clickup_work/__init__.py`. If you forget `__init__.py`, `--version` lies.
7. **`--no-tui` works on every TUI surface.** Plain-text flow must stay functional end-to-end (CI, pipes, scripts). When you add a new TUI screen, also keep its plain-text counterpart wired.
8. **Don't name a Widget/Screen method `_render`.** Textual's framework calls `self._render()` zero-arg during its render pipeline — overriding it with required keyword args crashes the widget. Use `_apply_filter`, `_redraw`, anything but `_render`.

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

### Add a new TUI surface

1. Pick the right module: a brand-new screen → its own file (`*_screen.py` or `*_flow.py`); a small modal reused across screens → add to `tui.py`.
2. Subclass `App[ResultType]` for full screens, `ModalScreen[ResultType]` for popovers. The result type goes into `app.run()` / `screen.dismiss(...)`.
3. **Don't name internal helpers `_render`** — see hard rule #8.
4. Gate the launch on `use_tui and sys.stdin.isatty() and sys.stdout.isatty()` from `cli.py`. Keep the existing plain-text branch as the `--no-tui` fallback.
5. Test with `app.run_test(size=(W, H))` plus `pilot.pause()`. Use `app.export_screenshot(...)` to capture rendered SVG; grep `<text>` elements to verify content without a real terminal.
6. Add a row to the README's "Interactive UI" table.

### Add an inline mutation to a TUI surface

1. Use `push_screen(Modal(...), callback)` — the callback receives the dismissed value.
2. Chain modals by pushing the next one inside the previous one's callback (`PostFlowApp` does this for status → time → reassign).
3. API mutations stay synchronous in the main thread (acceptable for short ClickUp calls). If they get slow, wrap in Textual's `@work` decorator.

### When a release fails to publish

Check order:
1. Was the tag pushed? (`git ls-remote --tags origin`)
2. Did the workflow run? (`gh run list --workflow=publish.yml`)
3. Did the build step pass? (`twine check` is in the workflow)
4. PyPI side: pending publisher registered under the exact workflow filename + environment name?

## When tickets don't appear (debugging external-system bugs)

When a user reports the picker is missing tickets, showing wrong data, or returning empty: **do not** start by suspecting ClickUp permissions, scope, or workspace structure. Re-read your own request code first.

The 0.6.2 fix was a one-line removal that took multiple wasted diagnostic rounds to reach — because empty/401 responses from the API got interpreted as external constraints. The actual cause was `OPEN_STATUSES = ("to do", "in progress")` hard-coded in `clickup.py`; the user's workspace renamed those to `DEV ASSIGNED`, so the API correctly returned zero. I'd even told the user "no clickup-work change can fix this" right before the screenshot revealed the truth.

Debugging order:

1. **Grep `clickup.py` for hard-coded literals** first — status names, list/folder/space names, default tuples, anything that assumes a "standard" workspace setup. Workspaces *always* customize statuses; status names are case- and exact-match in API filters; `"to do"` / `"in progress"` are not universal.
2. **Ask for a UI screenshot** of the relevant ClickUp panel. A status column or sidebar view answers naming questions in seconds; a curl against the API can take ten round-trips and still mislead.
3. **Only then** probe external endpoints. When probing: a 401 on `/folder/{id}` does not mean the resource is unreachable — the team-tasks endpoint with the right filters often returns content that direct folder/list endpoints reject. Don't conflate scope errors with reachability.

Resist declaring a bug "external/unfixable" until our calling code has been audited. Empty results, 401s, and missing fields are all consistent with both external-cause and local-cause hypotheses; confirmation bias treats them as proof of the external one when they're not.

## What to ignore

- Pyright "Import could not be resolved" diagnostics on `clickup_work.*` modules when this project isn't pip-installed in the current venv. These are false positives; the runtime `sys.path` shim in `~/.local/bin/clickup-work` handles it. Never "fix" them by rearranging imports.
- Pyright "Import 'textual' could not be resolved" when the project isn't installed in the venv Pyright is using. `textual` is a real runtime dep declared in `pyproject.toml`; it's resolvable in the project venv. Don't restructure imports to silence it.
- Pyright "X is possibly unbound" inside `with Spinner(...) as sp:` blocks. The variable is assigned inside the context manager and used outside in the same scope; Pyright doesn't follow `__exit__` semantics here.
- Pyright "event is not accessed" on `@on(...)` handlers. Textual's `@on` decorator requires the event parameter even when you don't read it.
- Node.js 20 deprecation warnings in the publish workflow. Benign until ~Sep 2026. Bump action versions as part of a normal maintenance pass, not reactively.
