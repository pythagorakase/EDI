# EDI-Link - Agent-to-Agent Communication

**EDI-Link** is the communication system for messaging with **EDI**, a Clawdbot instance that serves as an autonomous agent for testing and other tasks.

- **EDI** = the Clawdbot agent instance running on the remote server
- **EDI-Link** = this communication layer (client + server)

## Structure

```
EDI/
├── packages/
│   ├── client/      # CLI tool for sending messages to EDI
│   └── server/      # EDI-Link thread server (runs on EDI's host)
└── README.md
```

## Quick Start

### Client (local machine)

```bash
# New conversation (server generates thread ID)
edi "Hello, EDI!"
# Response includes threadId for continuity

# Continue conversation
edi --thread abc12345 "Follow up question"
```

See [packages/client/README.md](packages/client/README.md) for full documentation.

### Server (Clawdbot host)

```bash
cd packages/server
./start-edi-server.sh start
```

See [packages/server/README.md](packages/server/README.md) for details.

## Architecture

```
┌─────────────────────┐                    ┌─────────────────────────────────┐
│  packages/client/   │   HTTP POST /ask   │  packages/server/               │
│  edi CLI            │ ─────────────────▶ │  EDI-Link Thread Server (:19001)│
│  (local machine)    │                    │                                 │
│                     │ ◀───────────────── │  Polls EDI for response         │
│                     │  {reply, threadId} │                                 │
└─────────────────────┘                    └───────────────┬─────────────────┘
                                                           │
                                                           ▼
                                           ┌─────────────────────────────────┐
                                           │  EDI (Clawdbot on :18789)       │
                                           │  /hooks/agent → sessions        │
                                           └─────────────────────────────────┘
```

## Key Features

- **Server-generated thread IDs** — Proper API design where EDI-Link server owns session identity
- **Conversation continuity** — Thread IDs allow multi-turn conversations with EDI
- **Synchronous interface** — Polls EDI's async model into blocking request-response
- **Tailscale access** — Secure remote access without public exposure

## Protocol

### New Thread
```json
POST /ask
{"message": "Hello EDI", "threadId": null}

Response:
{"ok": true, "reply": "Hello! How can I help?", "threadId": "a1b2c3d4"}
```

### Continue Thread
```json
POST /ask
{"message": "Follow up", "threadId": "a1b2c3d4"}

Response:
{"ok": true, "reply": "...", "threadId": "a1b2c3d4"}
```

## Network

- **Server endpoint**: `http://100.104.206.23:19001/ask` (via Tailscale)
- **Health check**: `http://100.104.206.23:19001/health`

## Contributors

- **pythagorakase** - Client development
- **EDI-moltbot** - Server development
