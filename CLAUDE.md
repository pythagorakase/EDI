# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**EDI-Link** is the communication system that enables Claude Code to communicate with **EDI**, a Clawdbot instance running on a remote host via Tailscale. EDI-Link converts EDI's asynchronous agent model into a synchronous request-response API.

- **EDI** = the Clawdbot agent instance (the remote autonomous agent)
- **EDI-Link** = this communication system (client CLI + thread server)

## Architecture

```
Claude Code CLI  ──HTTP POST──▶  EDI-Link Thread Server  ──▶  EDI (Clawdbot)
(packages/client/edi)           (packages/server/)           (claude-base:18789)
```

**Key architectural concepts:**
- **Thread Server** generates and owns thread IDs (8-char UUID prefixes)
- **New threads** use `/hooks/agent` endpoint with polling for response
- **Thread continuations** use `/tools/invoke` → `sessions_send` for synchronous reply
- Session keys follow pattern: `edi:<threadId>` mapped to `agent:main:edi:<threadId>`

## Commands

### Server Management
```bash
# Start/stop/restart the thread server
./packages/server/start-edi-server.sh start
./packages/server/start-edi-server.sh stop
./packages/server/start-edi-server.sh status

# Check server health
curl http://127.0.0.1:19001/health

# View server logs
tail -f /tmp/edi-server.log
```

### Client Usage
```bash
# Send message (auto-continues last thread or starts new)
edi "your message"

# Force new thread
edi --new "message"

# Continue specific thread
edi --thread <thread-id> "message"

# Show thread ID in output
edi --show-thread "message"

# Pipe input
echo "message" | edi
```

## Key Files

| File | Purpose |
|------|---------|
| `packages/client/edi` | Python CLI (~145 lines) - sends messages via HTTP |
| `packages/server/edi-thread-server.py` | Python HTTP server (~316 lines) - threading and polling logic |
| `packages/server/start-edi-server.sh` | Bash script - server lifecycle management |

## Configuration (Hardcoded)

**Server** (`edi-thread-server.py`):
- `CLAWDBOT_URL = "http://127.0.0.1:18789"`
- `LISTEN_PORT = 19001`
- `POLL_INTERVAL = 1.0` seconds
- `DEFAULT_TIMEOUT = 120` seconds

**Client** (`edi`):
- Endpoint: `http://100.104.206.23:19001/ask`
- Thread persistence: `~/.edi-thread`

## Dependencies

- **Python 3.8+** for both client and server
- **requests** library for client only
- Server uses standard library only
- **Tailscale** required for client-to-server connectivity

## Protocol Notes

- Thread IDs are server-generated (client never creates them)
- Request format: `{"message": "...", "threadId": null | "existing-id"}`
- Response format: `{"ok": true, "reply": "...", "threadId": "..."}`
- Error responses include `"ok": false` with error details
