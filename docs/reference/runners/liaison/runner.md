# Liaison Runner

The Liaison runner provides natural language interpretation via an intermediate agent layer that orchestrates other CLI agents (Claude Code, Codex) using tmux.

## Overview

Unlike other runners that spawn a single subprocess and stream JSONL events, the Liaison runner:

1. Spawns a tmux session with Claude Code as the "brain"
2. Creates additional panes for worker subagents
3. Monitors output via `tmux capture-pane -p`
4. Sends input via `tmux send-keys`
5. Emits `input_request` events when user escalation is needed

## Resume Token Format

```
`liaison --session {session_id}`
```

Session state is persisted at `~/.takopi/liaison/sessions/{session_id}.json`.

## Configuration

In `takopi.toml`:

```toml
[liaison]
coordination_folder = "~/.takopi/liaison"  # default
poll_interval_s = 0.5                       # tmux capture interval
capture_lines = 50                          # lines to capture per poll

[liaison.escalation]
timeout_s = 300  # auto-handle timeout (5 minutes)
```

## Escalation Policy

The liaison uses an escalation policy to determine when to ask the user vs auto-respond.

### Always Escalate (default patterns)

- Destructive operations: `delete`, `remove`, `destroy`, `drop`, `truncate`
- Production environments: `production`, `prod`, `live`
- Credentials: `api-key`, `secret`, `password`, `credential`, `token`
- Financial: `billing`, `payment`, `cost`, `charge`
- Force flags: `--force`, `-f`
- Main branch operations: `push.*main`, `merge.*master`

### Auto-Approve (default patterns)

- Directory creation: `mkdir`, `create.*directory`
- Dev dependencies: `install.*dev.*depend`
- Testing: `run.*test`, `npm test`, `pytest`, `cargo test`
- Formatting: `format.*code`, `prettier`, `black`, `ruff`
- Linting: `lint`, `eslint`, `flake8`
- Building: `build`, `compile`
- Read operations: `read`, `view`, `show`, `list`, `ls`, `cat`

## Multi-Liaison Coordination

Multiple liaisons can run in parallel and coordinate via shared files:

```
~/.takopi/liaison/
├── sessions/{session_id}.json       # Session resume info
├── coordination/
│   ├── inbox/{liaison_id}/*.json    # Direct messages
│   └── broadcast/*.json             # Broadcast messages
├── state/
│   ├── active_liaisons.json         # Registry of running liaisons
│   ├── task_registry.json           # Tasks and their owners
│   └── shared_context.json          # Shared knowledge base
└── locks/*.lock                     # File locks
```

### Message Types

- `info_share`: Share discovered information with other liaisons
- `question`: Ask another liaison a question
- `task_claim`: Claim a task to prevent duplicate work
- `task_complete`: Mark a task as done

## Events

The liaison runner emits standard Takopi events plus two new types:

### `input_request`

Emitted when the liaison needs user input:

```python
InputRequestEvent(
    type="input_request",
    engine="liaison",
    request_id="liaison_abc123_1",
    question="Delete all files in /tmp? (y/n)",
    source="subagent",
    context="From claude in pane worker_1",
    urgency="high",
)
```

### `input_response`

Emitted when the user responds:

```python
InputResponseEvent(
    type="input_response",
    engine="liaison",
    request_id="liaison_abc123_1",
    response="n",
    responder="user",
)
```

## Telegram Integration

Input requests are rendered as Telegram messages with inline buttons:

- **Answer**: Prompts user to reply with their answer
- **Let liaison handle**: Allows the liaison to decide autonomously

## Error Handling

The liaison runner handles errors gracefully:

- **Tmux crash**: Attempts session recovery using saved state
- **Subagent failure**: Emits warning event, attempts restart with resume token
- **Timeout**: If no activity for 5 minutes, completes with error
