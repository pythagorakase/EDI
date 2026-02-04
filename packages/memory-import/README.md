# Memory Import (Phase 1)

Small CLI for converting OpenAI or Anthropic JSON exports into dated markdown
files for the EDI memory pipeline.

## Usage

```bash
python3 import_chats.py \
  --input ../../example_anthropic.json \
  --output ./out
```

```bash
python3 import_chats.py \
  --input ../../example_openai.json \
  --output ./out
```

The script auto-detects the provider format, groups messages by day, and
writes one file per date in `YYYY-MM-DD.md` format.

## Output Format

Each date file includes the conversation title and timestamped message
bullets. Example:

```markdown
# 2025-12-20

## Spa intake form design disasters (anthropic, 019b3ddc-...)
- `22:23:15` **Human:** I just checked into a spa with my wife...
- `22:24:10` **Assistant:** ...
```

Notes:
- Only user/human and assistant turns are included.
- Messages without text content or timestamps are skipped.
- Timestamps are written in UTC.
