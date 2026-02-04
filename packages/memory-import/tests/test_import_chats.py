import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = Path(__file__).resolve().parents[1]
sys.path.append(str(SCRIPT_DIR))

import_chats = pytest.importorskip("import_chats")


def load_example(name: str):
    path = ROOT / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_detect_format_anthropic():
    data = load_example("example_anthropic.json")
    assert import_chats.detect_format(data) == "anthropic"


def test_detect_format_openai():
    data = load_example("example_openai.json")
    assert import_chats.detect_format(data) == "openai"


def test_anthropic_markdown_generation(tmp_path):
    data = load_example("example_anthropic.json")
    messages = import_chats.parse_conversations(data)
    import_chats.write_markdown_files(messages, tmp_path)

    for date in ("2025-12-20", "2025-12-21", "2025-12-22"):
        assert (tmp_path / f"{date}.md").exists()

    sample = (tmp_path / "2025-12-20.md").read_text(encoding="utf-8")
    assert "Spa intake form design disasters" in sample
    assert "I just checked into a spa" in sample


def test_openai_markdown_generation(tmp_path):
    data = load_example("example_openai.json")
    messages = import_chats.parse_conversations(data)
    import_chats.write_markdown_files(messages, tmp_path)

    output_file = tmp_path / "2026-01-13.md"
    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    assert "Cat bedtime behavior analysis" in content
    assert "Cat psychology consult." in content
