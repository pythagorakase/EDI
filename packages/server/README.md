# EDI-Link Thread Server

Server-side component of EDI-Link, part of the EDI Utilities monorepo. Runs on
EDI's host and exposes a synchronous HTTP API for clients.

## Overview

The EDI-Link Thread Server provides a **synchronous HTTP interface** for clients
(the EDI-Link CLI, Claude Code, and other tools) to communicate with **EDI** (the
OpenClaw-based agent). It handles:

- **Server-side thread ID generation** - EDI-Link owns identity
- **Session creation and continuity** - via EDI's `/hooks/agent` endpoint
- **Response polling** - converts EDI's async model to sync request-response
- **Dispatch orchestration** - runs headless coding agents with thread persistence

## Files

```
packages/server/
├── README.md                  # This file
├── edi-thread-server.py       # Main server (Python 3)
├── start-edi-server.sh        # Management script
└── edi-thread-server.service  # systemd user unit
```

## Architecture

```
┌──────────────────────────┐     ┌───────────────────────┐     ┌─────────────────────┐
│ EDI-Link CLI / Client    │────▶│ EDI-Link Thread Server│────▶│ EDI (OpenClaw agent)│
│ (local machine)          │     │ (port 19001)          │     │ (port 18789)        │
└──────────────────────────┘     └───────────────────────┘     └─────────────────────┘
          │                               │                            │
          │  POST /ask                    │                            │
          │  {"message": ".."}            │                            │
          │──────────────────────────────▶│                            │
          │                               │  POST /hooks/agent         │
          │                               │  (create session)          │
          │                               │───────────────────────────▶│
          │                               │  Poll sessions_history     │
          │                               │───────────────────────────▶│
          │                               │◀───────────────────────────│
          │  {"reply": "...",              │                            │
          │   "threadId": "x"}             │                            │
          │◀──────────────────────────────│                            │
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

Callback defaults to `callbackSessionKey` (query), then `X-EDI-Callback-Session`
header, then `EDI_DISPATCH_DEFAULT_CALLBACK` (falls back to
`agent:main:discord:channel:1465948033253511320`).

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

**Piped text or markdown prompt:**
```bash
cat prompt.md | curl -X POST "http://127.0.0.1:19001/dispatch?agent=codex&workdir=/home/edi/nexus" \
  -H "Content-Type: text/markdown" \
  --data-binary @-
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
2. **Thread ID generation**: EDI-Link generates a unique 8-character thread ID
3. **Session key**: Mapped to `edi:<threadId>` for EDI
4. **Agent trigger**: POST to `/hooks/agent` starts an isolated agent turn
5. **Polling**: EDI-Link polls `sessions_history` every second until response appears
6. **Response**: Returns EDI's reply along with the thread ID

## Deployment

### Prerequisites

- Python 3.8+
- EDI (OpenClaw agent) running on port 18789
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

### Systemd User Service (Recommended)

Install the unit from this repo and enable it as a user service:

1. `mkdir -p ~/.config/systemd/user`
2. `cp packages/server/edi-thread-server.service ~/.config/systemd/user/`
3. `systemctl --user daemon-reload`
4. `systemctl --user enable --now edi-thread-server`

To start on boot (recommended):

1. `loginctl enable-linger $USER`

Check status and logs:

1. `systemctl --user status edi-thread-server`
2. `journalctl --user -u edi-thread-server -f`

If the repo is not at `~/EDI`, edit the unit `WorkingDirectory` and `ExecStart`.

### Legacy Script (nohup)

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

- Systemd logs: `journalctl --user -u edi-thread-server`
- Legacy script log: `/tmp/edi-server.log`
- Legacy PID file: `/tmp/edi-server.pid`

## Configuration

Edit `edi-thread-server.py` to change:

```python
CLAWDBOT_URL = "http://127.0.0.1:18789"   # EDI URL
GATEWAY_TOKEN = "..."                     # For /tools/invoke
HOOKS_TOKEN = "edi-hook-secret-2026"      # For /hooks/agent
LISTEN_PORT = 19001                       # Server port
LISTEN_HOST = "0.0.0.0"                   # Bind address
DEFAULT_TIMEOUT = 120                     # Default request timeout
POLL_INTERVAL = 1.0                       # Polling interval (seconds)
DISPATCH_DEFAULT_TIMEOUT = 3600           # Default dispatch timeout
DISPATCH_DEFAULT_WORKDIR = ~/nexus        # Default dispatch working directory
DISPATCH_MAX_TURNS = 25                   # Dispatch thread history window
DISPATCH_EARLY_CHECK_SECONDS = 5          # Early failure detection delay
DISPATCH_DEFAULT_CALLBACK = agent:main:discord:channel:1465948033253511320
```

## Authentication

- Optional HMAC auth for `/ask` and `/dispatch`
- Server secret from `EDI_AUTH_SECRET` or `/etc/edi/secret`
- Client sends `X-EDI-Timestamp` and `X-EDI-Signature`
- GitHub webhook secret from `EDI_GITHUB_SECRET`, `/etc/edi/github-secret`, or `~/.config/edi/github-secret`
  (file must be readable by the server user)

## GitHub Webhook

- Endpoint: `POST /github-webhook`
- Payload: `{"repository":"owner/repo","ref":"refs/heads/main","sha":"...","message":"..."}`
- Signature: `X-Hub-Signature-256: sha256=<hmac>`

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

### Connection refused from client

- Ensure server is running: `./start-edi-server.sh status`
- Check Tailscale connection: `tailscale status`
- Verify firewall allows port 19001
