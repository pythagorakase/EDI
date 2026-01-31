# EDI-Link Thread Server

Server-side component of EDI-Link. Runs on EDI's host (`edi-base.tail342046.ts.net`).

## Overview

The EDI-Link Thread Server provides a **synchronous HTTP interface** for Claude Code to communicate with **EDI** (the Clawdbot agent). It handles:

- **Server-side thread ID generation** — Proper API design where EDI-Link owns identity
- **Session creation and continuity** — Via EDI's `/hooks/agent` endpoint
- **Response polling** — Converts EDI's async model to sync request-response
- **Dispatch orchestration** — Runs headless coding agents with thread persistence

## Files

```
server/
├── README.md              # This file
├── edi-thread-server.py   # Main server (Python 3)
└── start-edi-server.sh    # Management script
```

## Architecture

```
┌─────────────┐     ┌───────────────────────┐     ┌──────────────┐
│ Claude Code │────▶│ EDI-Link Thread Server│────▶│     EDI      │
│  (MacBook)  │     │  (port 19001)         │     │  (Clawdbot)  │
└─────────────┘     └───────────────────────┘     └──────────────┘
       │                     │                       │
       │  POST /ask          │                       │
       │  {"message": ".."}  │                       │
       │────────────────────▶│                       │
       │                     │                       │
       │                     │  POST /hooks/agent    │
       │                     │  (create session)     │
       │                     │──────────────────────▶│
       │                     │                       │
       │                     │  Poll sessions_history│
       │                     │──────────────────────▶│
       │                     │◀──────────────────────│
       │                     │                       │
       │  {"reply": "...",   │                       │
       │   "threadId": "x"}  │                       │
       │◀────────────────────│                       │
```

## API

### `POST /ask`

Send a message to EDI and receive a response.

**Request:**
```json
{
  "message": "Your question or request",
  "threadId": null,           // null = new thread, or existing ID to continue
  "timeoutSeconds": 120       // optional, default 120
}
```

**Response (success):**
```json
{
  "ok": true,
  "reply": "EDI's response text",
  "threadId": "a1b2c3d4"      // EDI-Link generated, use to continue thread
}
```

**Response (error):**
```json
{
  "ok": false,
  "error": "Error description",
  "threadId": "a1b2c3d4"      // Still returned if thread was created
}
```

### `GET /health`

Health check endpoint.

**Response:**
```json
{
  "ok": true,
  "server": "edi-thread-server",
  "version": "4"
}
```

### `POST /dispatch`

Dispatch a headless coding agent (codex, claude, gemini). The server persists the
thread history to disk (`~/.edi-link/threads/<threadId>.jsonl`) and feeds the
conversation back into the prompt on each run.

**Request:**
```json
{
  "agent": "codex",
  "message": "Run the test suite and summarize failures",
  "threadId": "optional-thread-id",
  "timeout": 3600,
  "workdir": "/home/edi/nexus",
  "callback": {
    "sessionKey": "edi:abc12345"
  }
}
```

**Response:**
```json
{
  "ok": true,
  "taskId": "uuid",
  "threadId": "uuid",
  "status": "running"
}
```

### `GET /tasks`

List dispatch tasks with their status.

**Response:**
```json
{
  "ok": true,
  "tasks": [
    {
      "taskId": "uuid",
      "threadId": "uuid",
      "agent": "codex",
      "status": "running"
    }
  ]
}
```

### `POST /tasks/<taskId>/cancel`

Cancel a running dispatch task.

**Response:**
```json
{
  "ok": true,
  "status": "canceling"
}
```

### `GET /thread/<threadId>`

Fetch the persisted thread history.

**Response:**
```json
{
  "ok": true,
  "threadId": "uuid",
  "entries": [
    {
      "turn": 1,
      "role": "edi",
      "content": "Run the tests",
      "ts": 1738341000
    }
  ]
}
```

## Thread Lifecycle

1. **New Thread**: Client sends `{"message": "...", "threadId": null}`
   - EDI-Link generates unique 8-character thread ID
   - Creates new EDI session `edi:<threadId>`
   - Returns thread ID in response

2. **Continue Thread**: Client sends `{"message": "...", "threadId": "<id>"}`
   - EDI-Link uses existing session with conversation history
   - EDI remembers prior context from this thread

3. **Thread Persistence**: Threads are EDI sessions — they persist across server restarts

## Deployment

### Prerequisites

- Python 3.8+
- EDI (Clawdbot) running on port 18789
- Webhooks enabled in EDI's config:
  ```json
  {
    "hooks": {
      "enabled": true,
      "token": "edi-hook-secret-2026",
      "path": "/hooks"
    }
  }
  ```

### Start/Stop

```bash
# Start the server
./start-edi-server.sh start

# Check status
./start-edi-server.sh status

# Stop
./start-edi-server.sh stop

# Restart
./start-edi-server.sh restart
```

### Manual Start

```bash
python3 edi-thread-server.py
# Listens on 0.0.0.0:19001
```

### Logs

- Server log: `/tmp/edi-server.log`
- PID file: `/tmp/edi-server.pid`

## Configuration

Edit `edi-thread-server.py` to change:

```python
CLAWDBOT_URL = "http://127.0.0.1:18789"   # EDI (Clawdbot) URL
GATEWAY_TOKEN = "..."                       # For /tools/invoke
HOOKS_TOKEN = "edi-hook-secret-2026"        # For /hooks/agent
LISTEN_PORT = 19001                         # Server port
LISTEN_HOST = "0.0.0.0"                     # Bind address
DEFAULT_TIMEOUT = 120                       # Default request timeout
POLL_INTERVAL = 1.0                         # Polling interval (seconds)
DISPATCH_DEFAULT_TIMEOUT = 3600             # Default dispatch timeout
DISPATCH_DEFAULT_WORKDIR = ~/nexus          # Default dispatch working directory
DISPATCH_MAX_TURNS = 25                     # Dispatch thread history window
```

## Network Access

The server binds to `0.0.0.0` so it's accessible via:

- **Local**: `http://127.0.0.1:19001`
- **Tailscale**: `http://100.104.206.23:19001`

Tailscale provides secure access without exposing the server to the public internet.

## How It Works

1. **Request arrives** at `/ask` with message and optional threadId
2. **Thread ID generation**: If threadId is null, EDI-Link generates one (8-char UUID prefix)
3. **Session key**: Mapped to `edi:<threadId>` for EDI
4. **Agent trigger**: POST to `/hooks/agent` starts an isolated agent turn
5. **Polling**: EDI-Link polls `sessions_history` every second until response appears
6. **Response**: Returns EDI's reply along with the thread ID

This converts EDI's async agent model into a synchronous request-response API suitable for CLI usage.

## Troubleshooting

### Server won't start
- Check if port 19001 is already in use: `ss -tlnp | grep 19001`
- Check EDI is running: `curl http://127.0.0.1:18789/`

### "Failed to trigger agent" error
- Verify hooks are enabled in EDI's config
- Check hooks token matches: `hooks.token` in config

### Timeout waiting for response
- Increase `timeoutSeconds` in request
- Check EDI logs for errors
- Verify EDI is responsive

### Connection refused from MacBook
- Ensure server is running: `./start-edi-server.sh status`
- Check Tailscale connection: `tailscale status`
- Verify firewall allows port 19001
