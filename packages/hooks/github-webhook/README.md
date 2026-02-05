# GitHub Merge Webhook (OpenClaw)

This hook transform lets OpenClaw accept GitHub webhooks at `/hooks/github` and
wake EDI when a merge lands. It supports:

- **Merged pull requests** (`pull_request` event, `action=closed`, `merged=true`)
- **Push events** (including merge commits to `main`)
- The **custom payload** used by the NEXUS `notify-edi.yml` workflow

## OpenClaw Config

Add a mapping in your OpenClaw config (typically `~/.openclaw/openclaw.json`).
Point `transformsDir` at this directory and use `transform.mjs`.

```json
{
  "hooks": {
    "enabled": true,
    "path": "/hooks",
    "token": "edi-hook-secret-2026",
    "transformsDir": "/home/edi/EDI/packages/hooks/github-webhook",
    "mappings": [
      {
        "id": "github-merge",
        "match": { "path": "github" },
        "action": "agent",
        "wakeMode": "now",
        "name": "GitHub",
        "transform": {
          "module": "./transform.mjs",
          "export": "transformGithubWebhook"
        }
      }
    ]
  }
}
```

Restart the OpenClaw gateway after updating the config.

## Sending Webhooks

OpenClaw expects a token on the hook request. Prefer a header:

- `Authorization: Bearer <token>`
- `X-OpenClaw-Token: <token>`

Example test call:

```bash
curl -X POST "http://100.104.206.23:18789/hooks/github" \
  -H "Authorization: Bearer edi-hook-secret-2026" \
  -H "Content-Type: application/json" \
  -d '{"repository":"pythagorakase/nexus","ref":"refs/heads/main","sha":"abc123","message":"Merge pull request #123"}'
```

## NEXUS Workflow Integration

If you want the existing `notify-edi.yml` workflow to hit OpenClaw directly,
set its `EDI_WEBHOOK_URL` to the `/hooks/github` endpoint and add a token header
(or append `?token=...` if you accept query tokens). The transform accepts the
workflow's compact JSON payload without changes.

## Behavior Notes

- Non-merged PR events are ignored (returns 204).
- Branch deletion push events are ignored.
- Messages include repo, branch, short SHA, and a brief summary.
