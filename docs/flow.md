# clickup-work — finite state diagram

One run of `clickup-work`, expressed as a finite state machine. Each box is
a state the program is in; each arrow is the event that moves it to the
next state. Every state has one escape hatch labelled "error or cancel"
that leads to `Halt`.

```mermaid
stateDiagram-v2
    direction TB

    [*] --> Start

    Start: Start\n(no work done yet)
    TicketReady: Ticket chosen\n(token ok, config loaded,\nticket picked, repo routed)
    BranchReady: Branch ready\n(base verified, feature\nbranch prepared, Claude\nlaunched in that repo)
    ClaudeFinished: Claude finished\n(session exited,\nbranch has new commits)
    PrOpen: Pull request open\n(branch pushed, PR\ncreated on GitHub)
    Done: Done\n(ClickUp ticket status\nupdated, or user skipped)
    Halt: Halt\n(error, cancel, dry-run,\nor no commits — exit early)

    Start          --> TicketReady     : fetch tickets, pick one, route to repo
    TicketReady    --> BranchReady     : verify base, prepare branch, launch Claude
    BranchReady    --> ClaudeFinished  : Claude exits with at least one commit
    ClaudeFinished --> PrOpen          : push branch and open PR
    PrOpen         --> Done            : update ClickUp status (or skip)
    Done           --> [*]

    Start          --> Halt : error or cancel
    TicketReady    --> Halt : error or cancel
    BranchReady    --> Halt : error or cancel
    ClaudeFinished --> Halt : error, no commits, or declined push
    PrOpen         --> Halt : error or cancel
    Halt           --> [*]
```

## How to read it

- The happy path runs straight down the middle:
  `Start → Ticket chosen → Branch ready → Claude finished → PR open → Done`.
- Every state has the same fallback: anything that goes wrong (network
  error, user pressing Ctrl-C, `--dry-run` stopping the run, no commits to
  push) sends the program to `Halt` and the process exits.
- `Done` is the only accepting state. Everything else either keeps moving
  forward or halts.
