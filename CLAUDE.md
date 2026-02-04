# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**EDI-Link** is the communication system that enables Claude Code to communicate with **EDI**, a Clawdbot instance running on a remote host via Tailscale. EDI-Link converts EDI's asynchronous agent model into a synchronous request-response API.

- **EDI** = the Clawdbot agent instance (the remote autonomous agent)
- **EDI-Link** = this communication system (client CLI + thread server)

## Architecture

```
Claude Code CLI  ──HTTP POST──▶  EDI-Link Thread Server  ──▶  EDI (Clawdbot)
(packages/client/edi)           (packages/server/)           (edi-base:18789)
```

**Key architectural concepts:**
- **Thread Server** generates and owns thread IDs (8-char UUID prefixes)
- **New threads** use `/hooks/agent` endpoint with polling for response
- **Thread continuations** use `/tools/invoke` → `sessions_send` for synchronous reply
- **Dispatch** uses headless agents (codex/claude/gemini) with JSONL thread logs
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

### Dispatch Usage (server API)
```bash
# Dispatch a headless agent task (returns taskId + threadId)
curl -X POST http://127.0.0.1:19001/dispatch \
  -H "Content-Type: application/json" \
  -d '{"agent":"codex","message":"Run tests and summarize failures","threadId":null}'

# Dispatch using a piped text/markdown prompt
cat prompt.md | curl -X POST "http://127.0.0.1:19001/dispatch?agent=codex" \
  -H "Content-Type: text/markdown" \
  --data-binary @-

# List running/canceling tasks
curl http://127.0.0.1:19001/tasks

# Fetch thread history (JSONL-backed)
curl http://127.0.0.1:19001/thread/<thread-id>
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
- `DISPATCH_DEFAULT_TIMEOUT = 3600` seconds (env: `EDI_DISPATCH_DEFAULT_TIMEOUT`)
- `DISPATCH_DEFAULT_WORKDIR = ~/nexus` (env: `EDI_DISPATCH_WORKDIR`)
- `DISPATCH_MAX_TURNS = 25` (env: `EDI_DISPATCH_MAX_TURNS`)
- Thread logs stored at `~/.edi-link/threads/<threadId>.jsonl`

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
- Dispatch endpoint: `POST /dispatch` with `agent`, `message`, optional `threadId`, `timeout`, `workdir`
- Task status endpoint: `GET /tasks` (running/canceling only)
- Thread history endpoint: `GET /thread/<threadId>`

## GitHub Webhook Integration

The thread server accepts webhook notifications from GitHub Actions when branches are merged to main. This allows EDI to automatically pull and test updates.

### Endpoint

```
POST /github-webhook
```

### Request Format

```json
{
  "repository": "pythagorakase/nexus",
  "ref": "refs/heads/main",
  "sha": "abc1234567890...",
  "message": "Commit message here"
}
```

### Headers

```
Content-Type: application/json
X-Hub-Signature-256: sha256=<hmac-hex-digest>
```

### Signature Verification

The signature is computed as:
```
HMAC-SHA256(secret, raw_json_payload)
```

Formatted as `sha256=<hex-digest>` (same format as native GitHub webhooks).

### Secret Configuration

On the EDI server host (choose one):

```bash
# Option 1: Environment variable (preferred)
export EDI_GITHUB_SECRET="<hex-secret>"

# Option 2: File
echo "<hex-secret>" > /etc/edi/github-secret
chmod 600 /etc/edi/github-secret
```

Generate a new secret:
```bash
openssl rand -hex 32
```

### GitHub Repository Secrets Required

- `TS_OAUTH_CLIENT_ID`: Tailscale OAuth client ID (for network access)
- `TS_OAUTH_SECRET`: Tailscale OAuth secret
- `EDI_GITHUB_SECRET`: Shared secret for HMAC signature verification

### Behavior

- Fire-and-forget: returns 200 immediately after triggering EDI
- Creates sessions with key pattern: `github:<repo-name>:<short-sha>`
- EDI receives a formatted message requesting it to pull and run tests

## Authentication

EDI-Link uses HMAC-SHA256 to verify that messages originate from trusted sources.

### How It Works

1. Client signs each request with a shared secret
2. Server verifies the signature before processing
3. Timestamps prevent replay attacks (5-minute window)

### Request Headers

```
X-EDI-Timestamp: <unix_timestamp>
X-EDI-Signature: <hex_hmac_sha256>
```

Signature is computed as: `HMAC-SHA256(secret, "{timestamp}:{canonical_json_payload}")`

Where `canonical_json_payload` is the request body serialized with sorted keys and no whitespace:
`{"message":"...","threadId":null,"timeoutSeconds":120}`

### Setup

**Generate shared secret:**
```bash
mkdir -p ~/.config/edi
openssl rand -hex 32 > ~/.config/edi/secret
chmod 600 ~/.config/edi/secret
```

**Deploy to server (choose one):**
```bash
# Option 1: Environment variable (preferred)
export EDI_AUTH_SECRET="<paste-secret-here>"

# Option 2: File
echo "<paste-secret-here>" > /etc/edi/secret
chmod 600 /etc/edi/secret
```

### Graceful Degradation

- **No secret configured on server:** Requests allowed (backward compatible)
- **Secret configured:** Authentication enforced, unsigned requests rejected
- **Mismatched secrets:** Server returns 401 Unauthorized
