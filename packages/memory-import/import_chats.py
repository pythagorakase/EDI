#!/usr/bin/env python3
"""Import chat exports and write dated markdown files."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class ParsedMessage:
    provider: str
    conversation_id: str
    conversation_title: str
    role: str
    created_at: datetime
    content: str


def load_json(path: Path) -> Any:
    """Load JSON content from a file."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def detect_format(data: Any) -> str:
    """Detect export format based on top-level structure."""
    if isinstance(data, list) and data:
        sample = data[0]
        if isinstance(sample, dict):
            if "chat_messages" in sample:
                return "anthropic"
            if "mapping" in sample:
                return "openai"
    raise ValueError("Unrecognized export format")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse ISO-8601 or epoch timestamps into UTC-aware datetimes."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            return None
    return None


def normalize_title(title: Optional[str], fallback: str) -> str:
    """Normalize conversation titles into single-line strings."""
    if not title:
        return fallback
    cleaned = " ".join(title.split())
    return cleaned or fallback


def extract_openai_text(content: Any) -> Optional[str]:
    """Extract text payloads from OpenAI message content objects."""
    if not isinstance(content, dict):
        return None
    content_type = content.get("content_type")
    if content_type not in ("text", "multimodal_text", "assistant_text"):
        return None
    parts = content.get("parts")
    if not isinstance(parts, list):
        return None
    pieces: List[str] = []
    for part in parts:
        if isinstance(part, str):
            pieces.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                pieces.append(text)
    joined = "".join(pieces).strip()
    return joined or None


def parse_anthropic(conversations: Iterable[Dict[str, Any]]) -> List[ParsedMessage]:
    """Parse Anthropic export format into ParsedMessage objects."""
    messages: List[ParsedMessage] = []
    for conv in conversations:
        conversation_id = conv.get("uuid") or conv.get("id") or "unknown"
        title = normalize_title(conv.get("name"), f"Untitled {conversation_id}")
        for msg in conv.get("chat_messages", []):
            sender = msg.get("sender")
            if sender == "human":
                role = "user"
            elif sender == "assistant":
                role = "assistant"
            else:
                continue
            content = msg.get("text") or msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            created_at = parse_timestamp(msg.get("created_at"))
            if not created_at:
                continue
            messages.append(
                ParsedMessage(
                    provider="anthropic",
                    conversation_id=conversation_id,
                    conversation_title=title,
                    role=role,
                    created_at=created_at,
                    content=content.strip(),
                )
            )
    return messages


def _find_openai_timestamp(
    message: Dict[str, Any],
    node_id: str,
    mapping: Dict[str, Any],
    conversation: Dict[str, Any],
) -> Optional[datetime]:
    for key in ("create_time", "update_time"):
        dt = parse_timestamp(message.get(key))
        if dt:
            return dt
    parent_id = mapping.get(node_id, {}).get("parent")
    while parent_id:
        parent = mapping.get(parent_id, {})
        parent_msg = parent.get("message") or {}
        for key in ("create_time", "update_time"):
            dt = parse_timestamp(parent_msg.get(key))
            if dt:
                return dt
        parent_id = parent.get("parent")
    for key in ("create_time", "update_time"):
        dt = parse_timestamp(conversation.get(key))
        if dt:
            return dt
    return None


def _openai_path(mapping: Dict[str, Any], current_node: Optional[str]) -> List[str]:
    path: List[str] = []
    node_id = current_node
    while node_id:
        node = mapping.get(node_id)
        if not node:
            break
        path.append(node_id)
        node_id = node.get("parent")
    path.reverse()
    return path


def parse_openai(conversations: Iterable[Dict[str, Any]]) -> List[ParsedMessage]:
    """Parse OpenAI export format into ParsedMessage objects."""
    messages: List[ParsedMessage] = []
    for conv in conversations:
        conversation_id = (
            conv.get("conversation_id") or conv.get("id") or "unknown"
        )
        title = normalize_title(conv.get("title"), f"Untitled {conversation_id}")
        mapping = conv.get("mapping") or {}
        path_ids = _openai_path(mapping, conv.get("current_node"))
        for node_id in path_ids:
            node = mapping.get(node_id) or {}
            msg = node.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("author", {}).get("role")
            if role not in ("user", "assistant"):
                continue
            if msg.get("metadata", {}).get("is_visually_hidden_from_conversation"):
                continue
            content = extract_openai_text(msg.get("content"))
            if not content:
                continue
            created_at = _find_openai_timestamp(msg, node_id, mapping, conv)
            if not created_at:
                continue
            messages.append(
                ParsedMessage(
                    provider="openai",
                    conversation_id=conversation_id,
                    conversation_title=title,
                    role=role,
                    created_at=created_at,
                    content=content,
                )
            )
    return messages


def parse_conversations(data: Any) -> List[ParsedMessage]:
    """Parse export data into a flat list of ParsedMessage entries."""
    if not isinstance(data, list):
        raise ValueError("Export JSON must be a list of conversations")
    export_format = detect_format(data)
    if export_format == "anthropic":
        return parse_anthropic(data)
    if export_format == "openai":
        return parse_openai(data)
    raise ValueError("Unsupported export format")


def format_message(message: ParsedMessage) -> Optional[str]:
    """Format a single message as a markdown bullet with timestamp."""
    role_label = "Human" if message.role in ("user", "human") else "Assistant"
    time_str = message.created_at.strftime("%H:%M:%S")
    lines = [line.rstrip() for line in message.content.splitlines()]
    if not lines or not lines[0].strip():
        return None
    formatted = f"- `{time_str}` **{role_label}:** {lines[0]}"
    if len(lines) > 1:
        tail = "\n".join(f"  {line}" for line in lines[1:])
        return f"{formatted}\n{tail}"
    return formatted


def group_messages(messages: Iterable[ParsedMessage]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Group messages by date and conversation."""
    grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for msg in sorted(messages, key=lambda m: m.created_at):
        date_key = msg.created_at.date().isoformat()
        day_bucket = grouped.setdefault(date_key, {})
        conv_bucket = day_bucket.setdefault(
            msg.conversation_id,
            {
                "title": msg.conversation_title,
                "provider": msg.provider,
                "messages": [],
            },
        )
        conv_bucket["messages"].append(msg)
    return grouped


def write_markdown_files(messages: Iterable[ParsedMessage], output_dir: Path) -> None:
    """Write grouped messages into YYYY-MM-DD.md files."""
    output_dir.mkdir(parents=True, exist_ok=True)
    grouped = group_messages(messages)
    for date_key in sorted(grouped.keys()):
        lines: List[str] = [f"# {date_key}"]
        day_bucket = grouped[date_key]
        conv_items = sorted(
            day_bucket.items(),
            key=lambda item: item[1]["messages"][0].created_at,
        )
        for conv_id, conv_data in conv_items:
            title = conv_data["title"] or f"Untitled {conv_id}"
            provider = conv_data["provider"]
            lines.append("")
            lines.append(f"## {title} ({provider}, {conv_id})")
            for msg in conv_data["messages"]:
                formatted = format_message(msg)
                if formatted:
                    lines.append(formatted)
        output_path = output_dir / f"{date_key}.md"
        output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert chat exports into dated markdown files.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to exported JSON file.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Directory for markdown output files.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    output_dir = Path(args.output).expanduser()

    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")
    data = load_json(input_path)
    messages = parse_conversations(data)
    if not messages:
        raise SystemExit("No messages found in export")
    write_markdown_files(messages, output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
