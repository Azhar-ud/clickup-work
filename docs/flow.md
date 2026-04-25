# clickup-work flow

State diagram of the runtime behaviour, traced from `clickup_work/cli.py`.
Render on GitHub directly, in any Mermaid-aware editor, or by pasting the
block below into <https://mermaid.live>. To produce an SVG/PNG locally:

```bash
npx -y @mermaid-js/mermaid-cli -i docs/flow.md -o flow.svg
```

## Default command — `clickup-work [...flags]`

```mermaid
stateDiagram-v2
    [*] --> CheckToken

    CheckToken --> CheckBinaries: CLICKUP_API_TOKEN set
    CheckToken --> [*]: missing token / exit 1

    CheckBinaries --> LoadConfig: claude+git+gh on PATH
    CheckBinaries --> [*]: missing binary / exit 1

    LoadConfig --> ResolveUpfrontRepo: ok
    LoadConfig --> [*]: ConfigError / exit 1

    ResolveUpfrontRepo --> FetchTickets: --repo wins, else default_repo, else None
    FetchTickets --> CheckTickets: ok
    FetchTickets --> [*]: ClickUpError / exit 1

    CheckTickets --> [*]: zero tickets / exit 0
    CheckTickets --> PickTask: tickets returned

    PickTask --> RouteTicket: --top OR user picked
    PickTask --> [*]: user cancelled / exit 0

    state RouteTicket {
        [*] --> HasUpfront
        HasUpfront --> UseUpfront: upfront_repo set
        HasUpfront --> MatchTagOrFolder: no upfront_repo
        MatchTagOrFolder --> UseMatched: tag or folder_id matched
        MatchTagOrFolder --> PromptMapping: no match
        PromptMapping --> UseChosen: user picked repo
        PromptMapping --> Cancelled: user cancelled
        UseUpfront --> [*]
        UseMatched --> [*]
        UseChosen --> [*]
    }

    RouteTicket --> ResolveBase: repo decided
    RouteTicket --> [*]: routing cancelled / exit 0

    ResolveBase --> VerifyBaseRemote: --base flag, else config, else origin/HEAD
    ResolveBase --> [*]: GitError / exit 1

    VerifyBaseRemote --> CheckDryRun: base on origin
    VerifyBaseRemote --> [*]: base missing / exit 1

    CheckDryRun --> [*]: --dry-run / exit 0
    CheckDryRun --> PrepareBranch: live run

    PrepareBranch --> LaunchClaude: branch created or reused
    PrepareBranch --> [*]: GitError / exit 1

    LaunchClaude --> CountCommits: claude subprocess exits

    CountCommits --> CheckAhead: ok
    CountCommits --> [*]: GitError / exit 1

    CheckAhead --> [*]: 0 commits ahead / exit 0
    CheckAhead --> ConfirmPush: >=1 commit ahead

    ConfirmPush --> OpenPR: -y OR user said yes
    ConfirmPush --> [*]: declined / exit 0

    OpenPR --> PromptStatus: PR/draft URL printed
    OpenPR --> [*]: GitError / exit 1

    PromptStatus --> FetchListStatuses: --no-status absent
    PromptStatus --> [*]: --no-status / exit 0

    FetchListStatuses --> PickStatus: ok
    FetchListStatuses --> [*]: ClickUpError, warn and exit 0

    PickStatus --> UpdateStatus: user chose new status
    PickStatus --> [*]: skipped or unchanged / exit 0

    UpdateStatus --> [*]: PUT /task/{id} / exit 0
```

## Subcommand — `clickup-work add-repo <path>`

```mermaid
stateDiagram-v2
    [*] --> ValidatePath
    ValidatePath --> ResolveBaseBranch: path is a git repo
    ValidatePath --> [*]: invalid / exit 1

    ResolveBaseBranch --> AskNickname: --base-branch OR origin/HEAD
    ResolveBaseBranch --> [*]: detection failed / exit 1

    AskNickname --> ValidateNickname: prompt or --name
    ValidateNickname --> AppendRepoBlock: lowercase a-z 0-9 _-
    ValidateNickname --> [*]: invalid chars / exit 1

    AppendRepoBlock --> [*]: [repos.<name>] written / exit 0
```

## State legend

| State                | Source                                | Notes                                                              |
|----------------------|----------------------------------------|--------------------------------------------------------------------|
| `CheckToken`         | `cli.py:594-600`                      | Reads `CLICKUP_API_TOKEN` env var.                                |
| `CheckBinaries`      | `cli.py:602-604`, `_check_binaries`   | `claude`, `git`, `gh` must be on `PATH`.                          |
| `LoadConfig`         | `config.load`                         | Parses `~/.config/clickup-work/config.toml`.                      |
| `ResolveUpfrontRepo` | `cli.py:_resolve_upfront_repo`        | `--repo` > `default_repo` > single-repo > None.                   |
| `FetchTickets`       | `clickup.get_open_tasks`              | `GET /team/{id}/task` with `include_closed=false`.                |
| `PickTask`           | `cli.py:pick_task`                    | fzf if available, else numbered prompt.                           |
| `RouteTicket`        | `cli.py:_route_ticket` + prompt       | Tag matches beat folder matches.                                   |
| `ResolveBase`        | `cli.py:_resolve_base_branch`         | `--base` > config > `origin/HEAD`.                                 |
| `VerifyBaseRemote`   | `git.remote_branch_exists`            | Pre-flight before any local mutation.                              |
| `PrepareBranch`      | `git.prepare_branch`                  | Reuses existing branch instead of resetting.                       |
| `LaunchClaude`       | `claude.launch`                       | Blocking subprocess; resumes after Claude exits.                   |
| `CountCommits`       | `git.commits_ahead`                   | Skips push when zero.                                              |
| `OpenPR`             | `git.push_and_open_pr`                | Honours `--draft`.                                                 |
| `PromptStatus`       | `cli._prompt_status_change`           | Calls `GET /list/{id}` then `PUT /task/{id}`.                      |
