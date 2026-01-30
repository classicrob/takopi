# Switch engines

Run a one-off message on a specific engine, or set a persistent default for a chat/topic.

## Use an engine for one message

Prefix the first non-empty line with an engine directive:

```
/codex hard reset the timeline
/claude shrink and store artifacts forever
/opencode hide their paper until they reply
/pi render a diorama of this timeline
/liaison coordinate multiple agents to complete this complex refactor
```

Directives are only parsed at the start of the first non-empty line.

## Set a default engine for the current scope

Use `/agent`:

```
/agent
/agent set claude
/agent clear
```

- Inside a forum topic, `/agent set` affects that topic.
- In normal chats, it affects the whole chat.
- In group chats, only admins can change defaults.

Selection precedence (highest to lowest): resume token → `/<engine-id>` directive → topic default → chat default → project default → global default.

## Engine installation

Takopi shells out to engine CLIs. Install them and make sure they're on your `PATH`
(`codex`, `claude`, `opencode`, `pi`). Liaison is a built-in orchestrator that doesn't require separate installation but needs `tmux` and at least one other engine. Authentication is handled by each CLI.

## Related

- [Commands & directives](../reference/commands-and-directives.md)
- [Config reference](../reference/config.md)
