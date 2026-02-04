# EDI-Link CLI

Command-line client for the EDI-Link Thread Server, part of the EDI Utilities
monorepo. It sends messages to EDI (an OpenClaw-based agent) via the `/ask` API.

## Usage

From the repo root:

```bash
./packages/client/edi "Hello, EDI!"
./packages/client/edi --new "Start a fresh thread"
./packages/client/edi --thread abc12345 "Follow up"
./packages/client/edi --show-thread "Show thread id"
echo "message" | ./packages/client/edi
```

If `packages/client/edi` is on your PATH, you can use `edi ...` instead.

## Thread Behavior

- Auto-continues the last thread using `~/.edi-thread`
- `--new` clears the saved thread
- `--thread <id>` continues a specific thread

## Configuration

Hardcoded defaults live in `packages/client/edi`:

- `EDI_ENDPOINT`: `http://100.104.206.23:19001/ask`
- Optional HMAC auth: create `~/.config/edi/secret`
- Secret must match server `EDI_AUTH_SECRET` or `/etc/edi/secret`

## Architecture

```
EDI-Link CLI -> HTTP POST /ask -> EDI-Link Thread Server -> EDI (OpenClaw agent)
                                   <- {reply, threadId} <-
```

## Dependencies

- Python 3.8+
- `requests`

## Files

- `packages/client/edi` - Main CLI script

## Testing

```bash
./packages/client/edi "ping"
./packages/client/edi --show-thread "What time is it?"
```
