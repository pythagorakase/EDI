# EDI Utilities

This repo is a monorepo of standalone tools that extend EDI (the OpenClaw
agent) beyond the core OpenClaw framework. Each utility lives under
`packages/` with its own README.

- EDI = the OpenClaw agent instance (remote autonomous agent)
- OpenClaw = the core framework EDI runs on
- This repo = utilities and integrations that sit alongside OpenClaw

## Packages

| Package | Path | Purpose |
|---------|------|---------|
| **EDI-Link** | `packages/client/`, `packages/server/` | Communication system (CLI + thread server) |
| **Memory Import** | `packages/memory-import/` | Chat export ingestion pipeline |

## Structure

```
EDI/
├── packages/
│   ├── client/         # EDI-Link CLI
│   ├── server/         # EDI-Link thread server
│   └── memory-import/  # Chat export ingestion
└── README.md
```

## Quick Start

### EDI-Link (message EDI)

```bash
# New conversation (server generates thread ID)
edi "Hello, EDI!"

# Continue conversation
edi --thread abc12345 "Follow up question"
```

If `edi` is not on your PATH, run `./packages/client/edi` from the repo root.

Server on the EDI host:

```bash
cd packages/server
./start-edi-server.sh start
```

Docs: `packages/client/README.md` and `packages/server/README.md`.

For persistent runs, prefer the systemd user service in
`packages/server/edi-thread-server.service` (see `packages/server/README.md`).

### Memory Import (chat exports to markdown)

```bash
python3 packages/memory-import/import_chats.py \
  --input example_anthropic.json \
  --output packages/memory-import/out
```

Docs: `packages/memory-import/README.md`.

## EDI-Link Overview

EDI-Link converts EDI's async agent model into a synchronous request-response
API for tools like Claude Code.

### Architecture

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
                                           │  EDI (OpenClaw on :18789)       │
                                           │  /hooks/agent -> sessions       │
                                           └─────────────────────────────────┘
```

### Key Features

- **Server-generated thread IDs** - EDI-Link owns session identity
- **Conversation continuity** - thread IDs allow multi-turn conversations with EDI
- **Synchronous interface** - polls EDI's async model into blocking request-response
- **Tailscale access** - secure remote access without public exposure
- **Dispatch orchestration** - headless coding agents with persisted thread logs

### Protocol

New thread:
```json
POST /ask
{"message": "Hello EDI", "threadId": null}

Response:
{"ok": true, "reply": "Hello! How can I help?", "threadId": "a1b2c3d4"}
```

Continue thread:
```json
POST /ask
{"message": "Follow up", "threadId": "a1b2c3d4"}

Response:
{"ok": true, "reply": "...", "threadId": "a1b2c3d4"}
```

### Network

- **Server endpoint**: `http://100.104.206.23:19001/ask` (via Tailscale)
- **Health check**: `http://100.104.206.23:19001/health`

### Dispatch API

In addition to `/ask`, the server can dispatch headless coding agents and keep a
JSONL thread log at `~/.edi-link/threads/<threadId>.jsonl` on the server host.

```json
POST /dispatch
{
  "agent": "codex",
  "message": "Run tests and summarize failures",
  "threadId": null
}
```

Piped text or markdown prompt:
```bash
cat prompt.md | curl -X POST "http://127.0.0.1:19001/dispatch?agent=codex" \
  -H "Content-Type: text/markdown" \
  --data-binary @-
```

## Adding Utilities

Checklist:
1. Create `packages/<name>/` with a README and entry point.
2. Add a row to the Packages table and update the Structure block if needed.
3. Add a Quick Start snippet above if the utility is user-facing.
4. Document configuration, secrets, and defaults in the package README.
5. Add sample inputs or fixtures if the tool expects them.

## Contributors

- **pythagorakase** - Client development
- **EDI-moltbot** - Server development
