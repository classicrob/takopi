# Liaison Runner

The Liaison runner is a persistent orchestrator (the "captain's chair") that manages multiple Claude Code subagents in parallel tmux panes.

## Overview

Unlike other runners that spawn a single subprocess and complete when the task finishes, the Liaison runner:

1. Spawns a tmux session with Claude Code as the "brain"
2. Creates additional panes for worker subagents
3. Monitors output via `tmux capture-pane -p`
4. Sends input via `tmux send-keys`
5. **Stays alive indefinitely** waiting for new requests
6. Routes new messages via an inbox for parallel dispatch
7. Only ends when explicitly closed with `/new` or `/cancel`

## Captain's Chair Pattern

The liaison implements a "captain's chair" pattern:

- **Persistent**: Doesn't auto-complete after tasks finish
- **Parallel dispatch**: New messages go to an inbox and are dispatched to subagents immediately
- **Multi-agent**: Can have many Claude Code subagents working simultaneously
- **User-controlled**: Only `/new` or `/cancel` ends the session

When you send a message while the liaison is working, it shows "dispatching" (not "queued") and routes directly to the liaison brain for parallel handling.

## Usage

```
/liaison fix the readme typo
```

While it's working, send more messages:
```
also update the changelog
```

The liaison will spawn additional subagents as needed.

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

## Memories

The liaison has a persistent memory system at `~/Dropbox/takopi-memories/`.

### Reading memories

Before starting work, the liaison checks for relevant memories:
```
ls ~/Dropbox/takopi-memories/
```

Filenames are descriptive, so it scans the list for anything relevant to the current task.

### Writing memories

After completing tasks, the liaison considers whether to record learnings:

**Write a memory when encountering:**
- Non-obvious project architecture or conventions
- Solutions to tricky problems that took multiple attempts
- User preferences or patterns noticed
- Important decisions and their rationale
- Gotchas, edge cases, or things that broke unexpectedly

**Don't write memories for:**
- Routine tasks that went smoothly
- Information already in project docs
- Temporary state or one-off fixes

### File naming

Descriptive kebab-case names for easy `ls` scanning:
- `takopi-telegram-message-threading.md`
- `happian-api-auth-flow-quirks.md`
- `rob-prefers-explicit-error-handling.md`

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

## Coordination Folder Structure

```
~/.takopi/liaison/
├── sessions/{session_id}.json       # Session resume info
├── coordination/
│   ├── inbox/*.json                 # Incoming user messages for dispatch
│   └── broadcast/*.json             # Broadcast messages
├── state/
│   └── shared_context.json          # Shared knowledge base
└── locks/*.lock                     # File locks
```

### Inbox Messages

When a user sends a message to an active liaison session, it's written to `coordination/inbox/` as JSON:

```json
{
  "chat_id": 123456789,
  "text": "also update the changelog",
  "session_id": "liaison_abc123",
  "timestamp": 1706745600.0
}
```

The liaison brain receives these as `NEW USER REQUEST:` messages and dispatches to subagents.

## Events

The liaison runner emits standard Takopi events plus:

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

- Progress shows "dispatching" for inbox-routed messages
- Input requests render as Telegram messages with inline buttons
- **Answer**: Prompts user to reply with their answer
- **Let liaison handle**: Allows the liaison to decide autonomously

## Error Handling

- **Tmux crash**: Attempts session recovery using saved state
- **Subagent failure**: Emits warning event, attempts restart with resume token
- **Idle timeout**: If no activity for 30 minutes, completes with error (safety net)
