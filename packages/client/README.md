# EDI CLI Messenger

EDI (Electronic Data Interchange) is a CLI tool for agent-to-agent communication with Clawdbot, an autonomous agent that can help with testing and other tasks.

## Branch

**Work branch**: `edi-comms-setup` (based on main, contains only EDI-specific work)

## Current State

The CLI script exists at `scripts/edi` and is functional:

```bash
# Basic usage
edi "Hello, EDI!"
edi "Please run the live tests for the wizard agent"

# Piped input
echo "message" | edi

# Raw JSON output
edi --raw "message"
```

## Architecture

```
scripts/edi (Python CLI)
    │
    ▼
HTTPS POST to EDI_ENDPOINT
    │
    ▼
Clawdbot (autonomous agent on claude-base.tail342046.ts.net)
    │
    ▼
Response: {"ok": true, "result": {"details": {"reply": "..."}}}
```

## Configuration

Current hardcoded values in `scripts/edi`:
- `EDI_ENDPOINT`: `https://claude-base.tail342046.ts.net/tools/invoke`
- `EDI_TOKEN`: Bearer token for auth
- `SESSION_KEY`: `"main"` (determines which Clawdbot session receives messages)

## Potential Improvements

1. **Configuration externalization**: Move endpoint/token/session to `nexus.toml` or environment variables
2. **Session management**: Allow specifying different session keys
3. **Async support**: For longer-running tasks
4. **Response streaming**: For real-time feedback from EDI
5. **Error handling**: More graceful degradation
6. **Integration with NEXUS CLI**: `nexus edi "message"` syntax

## Files

- `scripts/edi` - Main CLI script (115 lines)

## Testing

The EDI CLI can be tested by sending simple messages:
```bash
edi "ping"
edi "What time is it?"
```

## Related

This tool enables:
- Automated test runs via agent delegation
- Agent-to-agent task coordination
- Remote command execution through Clawdbot
