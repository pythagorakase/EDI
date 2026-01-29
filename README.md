# EDI - Agent-to-Agent Communication

EDI (Electronic Data Interchange) is a communication system for agent-to-agent messaging with Clawdbot, an autonomous agent that can help with testing and other tasks.

## Structure

```
EDI/
├── packages/
│   ├── client/      # CLI tool for sending messages to EDI
│   └── server/      # Server-side handlers (runs on Clawdbot host)
└── README.md
```

## Quick Start

### Client (local machine)

```bash
# From packages/client/
./edi "Hello, EDI!"
./edi "Please run the live tests"
echo "message" | ./edi
```

See [packages/client/README.md](packages/client/README.md) for full documentation.

### Server (Clawdbot host)

Server-side code lives in `packages/server/` and runs on the headless Linux server.

See [packages/server/README.md](packages/server/README.md) for details.

## Architecture

```
┌─────────────────────┐                    ┌─────────────────────────────────┐
│  packages/client/   │     HTTPS POST     │  packages/server/               │
│  edi CLI            │ ─────────────────▶ │  (claude-base.ts.net)           │
│  (local machine)    │                    │  Clawdbot autonomous agent      │
└─────────────────────┘                    └─────────────────────────────────┘
```

## Contributors

- **pythagorakase** - Client development
- **EDI (Clawdbot)** - Server development
