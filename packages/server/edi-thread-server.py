#!/usr/bin/env python3
"""
EDI Thread Server v4 - Server-side threading with session continuity.

Flow:
1. Client sends message (optionally with threadId)
2. Server generates threadId if not provided
3. For NEW threads: /hooks/agent creates session, then poll for response
4. For CONTINUED threads: sessions_send appends to existing session (preserves history)
5. Server returns response with threadId

Endpoints:
  POST /ask
    Body: {"message": "...", "threadId": null | "<id>", "timeoutSeconds": 120}
    Returns: {"ok": true, "reply": "...", "threadId": "<server-generated-or-existing>"}

  POST /dispatch
    Body: {"agent": "codex|claude|gemini", "message": "...", "threadId": "<id>", "timeout": 3600}
      or raw text/markdown with query params (?agent=codex&threadId=<id>&timeout=3600)
    Returns: {"ok": true, "taskId": "...", "threadId": "...", "status": "running"}

  GET /tasks
    Returns: {"ok": true, "tasks": [...]}

  POST /tasks/<taskId>/cancel
    Returns: {"ok": true, "status": "canceling"}

  GET /thread/<threadId>
    Returns: {"ok": true, "threadId": "...", "entries": [...]}

  GET /health
    Returns: {"ok": true, "server": "edi-thread-server", "version": "4"}
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from urllib.parse import urlparse, parse_qs

# Configuration
CLAWDBOT_URL = "http://127.0.0.1:18789"
GATEWAY_TOKEN = "h2WzPZjazQG8CQYrS8RgXI5MMVWFh6SI"  # For /tools/invoke
HOOKS_TOKEN = "edi-hook-secret-2026"  # For /hooks/agent
LISTEN_PORT = 19001
LISTEN_HOST = "0.0.0.0"  # Accessible via Tailscale
DEFAULT_TIMEOUT = 120
POLL_INTERVAL = 1.0  # seconds between polls

# Dispatch configuration
DISPATCH_DEFAULT_TIMEOUT = int(os.environ.get("EDI_DISPATCH_DEFAULT_TIMEOUT", "3600"))
DISPATCH_DEFAULT_WORKDIR = Path(
    os.environ.get("EDI_DISPATCH_WORKDIR", str(Path.home() / "nexus"))
).expanduser()
DISPATCH_MAX_TURNS = int(os.environ.get("EDI_DISPATCH_MAX_TURNS", "25"))
DISPATCH_EARLY_CHECK_SECONDS = float(os.environ.get("EDI_DISPATCH_EARLY_CHECK_SECONDS", "5"))
THREADS_DIR = Path.home() / ".edi-link" / "threads"
THREAD_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Dispatch runtime state
TASKS_LOCK = threading.Lock()
TASKS: Dict[str, Dict[str, Any]] = {}
THREAD_LOCK = threading.Lock()

# HMAC Authentication
AUTH_SECRET_ENV = "EDI_AUTH_SECRET"
AUTH_SECRET_FILE = Path("/etc/edi/secret")
AUTH_TIMESTAMP_TOLERANCE = 300  # 5 minutes in seconds
MAX_REQUEST_SIZE = 1024 * 1024  # 1MB

# GitHub Webhook Authentication (separate secret for defense in depth)
GITHUB_WEBHOOK_SECRET_ENV = "EDI_GITHUB_SECRET"
GITHUB_WEBHOOK_SECRET_FILE = Path("/etc/edi/github-secret")


def load_auth_secret() -> Optional[bytes]:
    """Load shared secret for HMAC verification.

    Priority: environment variable > file > None (auth disabled)
    """
    # Try environment variable first
    secret = os.environ.get(AUTH_SECRET_ENV)
    if secret:
        return secret.strip().encode()

    # Try file fallback
    if AUTH_SECRET_FILE.exists():
        try:
            secret = AUTH_SECRET_FILE.read_text().strip()
            if secret:
                return secret.encode()
        except Exception:
            pass

    return None


def load_github_secret() -> Optional[bytes]:
    """Load GitHub webhook secret for signature verification.

    Priority: environment variable > file > None (webhook auth disabled)
    """
    # Try environment variable first
    secret = os.environ.get(GITHUB_WEBHOOK_SECRET_ENV)
    if secret:
        return secret.strip().encode()

    # Try file fallback
    if GITHUB_WEBHOOK_SECRET_FILE.exists():
        try:
            secret = GITHUB_WEBHOOK_SECRET_FILE.read_text().strip()
            if secret:
                return secret.encode()
        except Exception:
            pass

    return None


def verify_github_signature(payload: bytes, signature: str, secret: bytes) -> bool:
    """Verify GitHub webhook signature (X-Hub-Signature-256 header).

    GitHub sends: sha256=<hex-hmac>
    We compute: HMAC-SHA256(secret, raw_payload)
    """
    expected = "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def canonicalize_auth_payload(payload: Dict[str, Any]) -> str:
    """Create a canonical JSON string for HMAC signing."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def verify_hmac_signature(
    payload: Dict[str, Any],
    timestamp: str,
    signature: str,
    secret: bytes
) -> Tuple[bool, str]:
    """Verify HMAC signature.

    Returns (is_valid, error_message).
    """
    # Check timestamp freshness (replay protection)
    try:
        ts = int(timestamp)
    except ValueError:
        return False, "Invalid timestamp format"

    now = int(time.time())
    if abs(now - ts) > AUTH_TIMESTAMP_TOLERANCE:
        return False, "Timestamp expired (replay protection)"

    # Recompute and compare signature
    signature_payload = f"{timestamp}:{canonicalize_auth_payload(payload)}"
    expected = hmac.new(secret, signature_payload.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return False, "Invalid signature"

    return True, ""


def make_request(path: str, payload: Optional[dict], token: str, method: str = "POST") -> dict:
    """Make authenticated request to Clawdbot gateway."""
    url = f"{CLAWDBOT_URL}{path}"
    data = json.dumps(payload).encode() if payload else None
    
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        },
        method=method
    )
    
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        return {"ok": False, "error": f"HTTP {e.code}: {error_body}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def trigger_agent_hook(session_key: str, message: str, timeout_seconds: int) -> dict:
    """Trigger an agent run via /hooks/agent."""
    payload = {
        "message": message,
        "sessionKey": session_key,
        "name": "EDI-CLI",
        "wakeMode": "now",
        "deliver": False,  # Don't deliver to chat channels
        "timeoutSeconds": timeout_seconds
    }
    return make_request("/hooks/agent", payload, HOOKS_TOKEN)


def get_session_history(session_key: str) -> dict:
    """Get session history via /tools/invoke."""
    # The gateway prepends 'agent:main:' to hook session keys
    full_key = f"agent:main:{session_key}"

    return make_request("/tools/invoke", {
        "tool": "sessions_history",
        "args": {
            "sessionKey": full_key,
            "limit": 10,
            "includeTools": False
        }
    }, GATEWAY_TOKEN)


def continue_thread(session_key: str, message: str, timeout_seconds: int) -> dict:
    """Continue an existing thread via sessions_send.

    Unlike trigger_agent_hook which creates a fresh session, sessions_send
    appends to an existing session's conversation history.
    """
    full_key = f"agent:main:{session_key}"
    return make_request("/tools/invoke", {
        "tool": "sessions_send",
        "args": {
            "sessionKey": full_key,
            "message": message,
            "timeoutSeconds": timeout_seconds
        }
    }, GATEWAY_TOKEN)


def extract_reply_from_send_result(result: dict) -> Optional[str]:
    """Extract reply from sessions_send result.

    sessions_send returns the assistant's reply directly in the result,
    unlike hooks/agent which requires polling.
    """
    if not result.get("ok"):
        return None

    details = result.get("result", {}).get("details", {})
    return details.get("reply")


def extract_last_assistant_reply(history_result: dict) -> Optional[str]:
    """Extract the last assistant reply from session history."""
    if not history_result.get("ok"):
        return None
    
    details = history_result.get("result", {}).get("details", {})
    messages = details.get("messages", [])
    
    # Find the last assistant message
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            # Content is usually a list of blocks
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text")
            elif isinstance(content, str):
                return content
    return None


def poll_for_response(session_key: str, timeout_seconds: int, initial_delay: float = 2.0) -> Optional[str]:
    """Poll sessions_history until we get an assistant response."""
    start_time = time.time()
    
    # Initial delay to let the agent start
    time.sleep(initial_delay)
    
    last_message_count = 0
    
    while time.time() - start_time < timeout_seconds:
        history = get_session_history(session_key)
        
        if history.get("ok"):
            reply = extract_last_assistant_reply(history)
            if reply:
                return reply
        
        time.sleep(POLL_INTERVAL)
    
    return None


def ensure_threads_dir() -> None:
    """Ensure the thread storage directory exists."""
    THREADS_DIR.mkdir(parents=True, exist_ok=True)


def validate_thread_id(thread_id: str) -> str:
    """Validate the thread ID to prevent path traversal."""
    if not isinstance(thread_id, str):
        raise ValueError("threadId must be a string")
    if not thread_id:
        raise ValueError("threadId required")
    if "/" in thread_id or "\\" in thread_id or ".." in thread_id:
        raise ValueError("Invalid threadId")
    if not THREAD_ID_RE.fullmatch(thread_id):
        raise ValueError("Invalid threadId")
    return thread_id


def thread_file_path(thread_id: str) -> Path:
    """Return the JSONL path for a thread."""
    thread_id = validate_thread_id(thread_id)
    threads_dir = THREADS_DIR.resolve()
    path = (threads_dir / f"{thread_id}.jsonl").resolve()
    try:
        path.relative_to(threads_dir)
    except ValueError as exc:
        raise ValueError("Invalid threadId") from exc
    return path


def load_thread_entries(thread_id: str) -> List[Dict[str, Any]]:
    """Load thread entries from disk."""
    try:
        path = thread_file_path(thread_id)
    except ValueError:
        return []
    if not path.exists():
        return []

    with THREAD_LOCK:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []

    entries: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def append_thread_entry(thread_id: str, entry: Dict[str, Any]) -> None:
    """Append a JSONL entry to the thread log."""
    ensure_threads_dir()
    path = thread_file_path(thread_id)
    line = json.dumps(entry, separators=(",", ":"))
    with THREAD_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def next_turn_number(entries: List[Dict[str, Any]]) -> int:
    """Compute the next turn number for a thread."""
    max_turn = 0
    for entry in entries:
        turn = entry.get("turn")
        try:
            turn_value = int(turn)
        except (TypeError, ValueError):
            continue
        if turn_value > max_turn:
            max_turn = turn_value
    return max_turn + 1


def existing_agent_for_thread(entries: List[Dict[str, Any]]) -> Optional[str]:
    """Return the agent role used in a thread, if any."""
    agents = {entry.get("role") for entry in entries if entry.get("role") not in (None, "edi")}
    if len(agents) == 1:
        return next(iter(agents))
    if len(agents) > 1:
        return "__mixed__"
    return None


def filter_entries_for_prompt(entries: List[Dict[str, Any]], max_turns: int) -> List[Dict[str, Any]]:
    """Return entries limited to the most recent N turns."""
    if max_turns <= 0:
        return []

    turn_order: List[int] = []
    seen_turns = set()
    for entry in entries:
        turn = entry.get("turn")
        if isinstance(turn, int) and turn not in seen_turns:
            seen_turns.add(turn)
            turn_order.append(turn)

    if len(turn_order) <= max_turns:
        return entries

    selected_turns = set(turn_order[-max_turns:])
    return [entry for entry in entries if entry.get("turn") in selected_turns]


def agent_label(agent: str) -> str:
    """Return a human-friendly label for an agent."""
    labels = {
        "codex": "Codex",
        "claude": "Claude",
        "gemini": "Gemini",
    }
    return labels.get(agent, agent.title())


def build_dispatch_prompt(entries: List[Dict[str, Any]], new_message: str, agent: str) -> str:
    """Build a prompt for the dispatch agent."""
    lines: List[str] = []
    lines.append("You are continuing a task. Here is the conversation so far:")
    lines.append("")
    lines.append("---")

    label = agent_label(agent)
    for entry in entries:
        role = entry.get("role")
        content = entry.get("content", "")
        prefix = "EDI" if role == "edi" else label
        lines.append(f"[{prefix}] {content}")

    lines.append("---")
    lines.append("")
    lines.append("Now continue:")
    lines.append(f"[EDI] {new_message}")
    return "\n".join(lines)


def build_agent_command(agent: str, prompt: str, workdir: Path) -> Tuple[List[str], Path]:
    """Build the command to run a headless agent."""
    if agent == "codex":
        cmd = [
            "codex",
            "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--color",
            "never",
            "--skip-git-repo-check",
            "-C",
            str(workdir),
            prompt,
        ]
        return cmd, workdir

    if agent == "claude":
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "text",
            "--permission-mode",
            "bypassPermissions",
            "--allow-dangerously-skip-permissions",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            prompt,
        ]
        return cmd, workdir

    if agent == "gemini":
        cmd = [
            "gemini",
            "-p",
            prompt,
            "--output-format",
            "text",
            "--approval-mode",
            "yolo",
        ]
        return cmd, workdir

    raise ValueError(f"Unsupported agent: {agent}")


def send_dispatch_callback(session_key: str, message: str, timeout_seconds: int) -> None:
    """Send a callback message into an existing EDI session."""
    if session_key.startswith("agent:"):
        full_key = session_key
    else:
        full_key = f"agent:main:{session_key}"

    make_request("/tools/invoke", {
        "tool": "sessions_send",
        "args": {
            "sessionKey": full_key,
            "message": message,
            "timeoutSeconds": timeout_seconds,
        }
    }, GATEWAY_TOKEN)


def run_dispatch_task(
    task_id: str,
    thread_id: str,
    turn: int,
    agent: str,
    prompt: str,
    workdir: Path,
    timeout_seconds: int,
    callback: Optional[Dict[str, Any]],
) -> None:
    """Run a headless agent task in the background."""
    output = ""
    exit_code: Optional[int] = None
    error: Optional[str] = None

    try:
        cmd, cwd = build_agent_command(agent, prompt, workdir)
        env = os.environ.copy()
        env["NO_COLOR"] = "1"

        process = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        with TASKS_LOCK:
            task = TASKS.get(task_id, {})
            task["_process"] = process
            task["pid"] = process.pid
            TASKS[task_id] = task

        try:
            output, _ = process.communicate(timeout=timeout_seconds)
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
            exit_code = process.returncode
            error = "timeout"
    except Exception as exc:
        error = str(exc)

    output = (output or "").strip()
    if error and not output:
        output = f"Error: {error}"

    append_thread_entry(thread_id, {
        "turn": turn,
        "role": agent,
        "content": output,
        "ts": int(time.time()),
        "exitCode": exit_code,
    })

    status = "completed"
    if error:
        status = "failed"
    elif exit_code not in (0, None):
        status = "failed"

    with TASKS_LOCK:
        task = TASKS.get(task_id, {})
        if task.get("cancel_requested"):
            status = "canceled"
        task["status"] = status
        task["endedAt"] = int(time.time())
        task["exitCode"] = exit_code
        if error:
            task["error"] = error
        TASKS[task_id] = task

    if callback and callback.get("sessionKey"):
        callback_message = "\n".join([
            "[EDI-Link Dispatch Result]",
            f"Thread: {thread_id}",
            f"Task: {task_id}",
            f"Agent: {agent}",
            f"Status: {status}",
            f"Exit code: {exit_code}",
            "",
            output,
        ])
        send_dispatch_callback(callback["sessionKey"], callback_message, DEFAULT_TIMEOUT)


class EDIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for EDI thread server."""

    def _read_raw_body(self) -> Optional[bytes]:
        """Read raw request body (returns None on error after sending response)."""
        transfer_encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer_encoding:
            return self._read_chunked_body()

        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "Invalid Content-Length"})
            return None

        if length > MAX_REQUEST_SIZE:
            self._send_json(413, {"ok": False, "error": "Request too large"})
            return None

        return self.rfile.read(length) if length else b""

    def _read_chunked_body(self) -> Optional[bytes]:
        """Read and decode chunked transfer-encoding body."""
        data = bytearray()
        while True:
            line = self.rfile.readline()
            if not line:
                self._send_json(400, {"ok": False, "error": "Invalid chunked encoding"})
                return None
            line = line.strip()
            if not line:
                continue
            try:
                chunk_size = int(line.split(b";", 1)[0], 16)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "Invalid chunk size"})
                return None

            if chunk_size == 0:
                # Consume trailer headers (if any) until blank line.
                while True:
                    trailer = self.rfile.readline()
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break

            chunk = self.rfile.read(chunk_size)
            data.extend(chunk)
            if len(data) > MAX_REQUEST_SIZE:
                self._send_json(413, {"ok": False, "error": "Request too large"})
                return None

            # Discard CRLF after the chunk.
            self.rfile.read(2)

        return bytes(data)

    def _read_json_body(self) -> Optional[Dict[str, Any]]:
        """Read and parse JSON body (returns None on error after sending response)."""
        raw = self._read_raw_body()
        if raw is None:
            return None
        if not raw:
            return {}

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return None

    def _read_dispatch_body(self) -> Tuple[Optional[Dict[str, Any]], bool]:
        """Read dispatch payload, supporting JSON and raw text/markdown."""
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type in {"text/plain", "text/markdown", "text/x-markdown"}:
            raw = self._read_raw_body()
            if raw is None:
                return None, False
            return {"message": raw.decode("utf-8", errors="replace")}, True

        return self._read_json_body(), False

    def _first_query_value(self, query: Dict[str, List[str]], key: str) -> Optional[str]:
        values = query.get(key)
        if not values:
            return None
        value = values[-1]
        return value if value != "" else None

    def _merge_dispatch_params(self, payload: Dict[str, Any], query: Dict[str, List[str]]) -> Dict[str, Any]:
        """Merge query/header params into payload when missing."""
        if not payload.get("agent"):
            agent_value = (
                self._first_query_value(query, "agent")
                or self.headers.get("X-EDI-Agent")
            )
            if agent_value:
                payload["agent"] = agent_value

        if payload.get("threadId") in (None, ""):
            thread_value = (
                self._first_query_value(query, "threadId")
                or self._first_query_value(query, "thread")
                or self.headers.get("X-EDI-Thread")
            )
            if thread_value:
                payload["threadId"] = thread_value

        if "timeout" not in payload and "timeoutSeconds" not in payload:
            timeout_value = (
                self._first_query_value(query, "timeout")
                or self._first_query_value(query, "timeoutSeconds")
                or self.headers.get("X-EDI-Timeout")
            )
            if timeout_value is not None:
                payload["timeout"] = timeout_value

        if "workdir" not in payload:
            workdir_value = (
                self._first_query_value(query, "workdir")
                or self.headers.get("X-EDI-Workdir")
            )
            if workdir_value:
                payload["workdir"] = workdir_value

        if "callback" not in payload:
            callback_session = (
                self._first_query_value(query, "callbackSessionKey")
                or self.headers.get("X-EDI-Callback-Session")
            )
            if callback_session:
                payload["callback"] = {"sessionKey": callback_session}

        return payload

    def _require_auth(self, payload: Dict[str, Any]) -> bool:
        """Enforce HMAC authentication if configured."""
        auth_secret = load_auth_secret()
        if not auth_secret:
            return True

        timestamp = self.headers.get("X-EDI-Timestamp")
        signature = self.headers.get("X-EDI-Signature")

        if not timestamp or not signature:
            self._send_json(401, {"ok": False, "error": "Missing authentication headers"})
            return False

        is_valid, error = verify_hmac_signature(payload, timestamp, signature, auth_secret)
        if not is_valid:
            self.log_message(f"Auth failed: {error}")
            self._send_json(401, {"ok": False, "error": "Authentication failed"})
            return False

        return True
    
    def do_GET(self):
        """Handle GET requests (health check)."""
        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._send_json(200, {
                "ok": True,
                "server": "edi-thread-server",
                "version": "4"
            })
            return

        if parsed.path == "/tasks":
            with TASKS_LOCK:
                tasks = []
                for task in TASKS.values():
                    status = task.get("status")
                    if status not in {"running", "canceling"}:
                        continue
                    tasks.append({
                        k: v
                        for k, v in task.items()
                        if not k.startswith("_") and k != "cancel_requested"
                    })
            tasks.sort(key=lambda item: item.get("startedAt", 0))
            self._send_json(200, {"ok": True, "tasks": tasks})
            return

        if parsed.path.startswith("/thread/"):
            thread_id = parsed.path.split("/thread/", 1)[1]
            if not thread_id:
                self._send_json(400, {"ok": False, "error": "threadId required"})
                return
            try:
                thread_id = validate_thread_id(thread_id)
            except ValueError:
                self._send_json(400, {"ok": False, "error": "Invalid threadId"})
                return

            entries = load_thread_entries(thread_id)
            if not entries:
                path = thread_file_path(thread_id)
                if not path.exists():
                    self._send_json(404, {"ok": False, "error": "thread not found"})
                    return
            self._send_json(200, {"ok": True, "threadId": thread_id, "entries": entries})
            return

        else:
            self.send_error(404)
    
    def do_POST(self):
        """Handle POST requests."""
        # GitHub webhook gets special handling (different auth)
        if self.path == "/github-webhook":
            self._handle_github_webhook()
            return

        parsed = urlparse(self.path)

        if parsed.path == "/ask":
            body = self._read_json_body()
            if body is None:
                return

            message = body.get("message")
            if not message:
                self._send_json(400, {"ok": False, "error": "message required"})
                return

            if not self._require_auth(body):
                return

            timeout_seconds = body.get("timeoutSeconds", DEFAULT_TIMEOUT)

            # Server generates thread ID if not provided (this is the key feature!)
            thread_id = body.get("threadId")
            is_new_thread = thread_id is None

            if is_new_thread:
                thread_id = str(uuid.uuid4())[:8]
            else:
                if not isinstance(thread_id, str):
                    self._send_json(400, {"ok": False, "error": "Invalid threadId"})
                    return
                try:
                    thread_id = validate_thread_id(thread_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "Invalid threadId"})
                    return

            session_key = f"edi:{thread_id}"

            self.log_message(f"{'New' if is_new_thread else 'Continue'} thread={thread_id}")

            if is_new_thread:
                # New thread: use /hooks/agent to create session
                full_message = f"""[EDI CLI Request - Thread: {thread_id}]

You are EDI, responding to Claude Code (a coding assistant helping Neil with NEXUS).
This is a NEW thread. Keep responses focused and technical.

Request: {message}"""

                hook_result = trigger_agent_hook(session_key, full_message, timeout_seconds)

                if not hook_result.get("ok"):
                    self._send_json(500, {
                        "ok": False,
                        "error": f"Failed to trigger agent: {hook_result.get('error')}",
                        "threadId": thread_id
                    })
                    return

                run_id = hook_result.get("runId")
                self.log_message(f"Agent triggered, runId={run_id}")

                # Poll for response (hooks/agent is async, need to wait)
                reply = poll_for_response(session_key, timeout_seconds)
            else:
                # Continue thread: use sessions_send for history preservation
                self.log_message(f"Continuing thread={thread_id} via sessions_send")

                result = continue_thread(session_key, message, timeout_seconds)

                if not result.get("ok"):
                    self._send_json(500, {
                        "ok": False,
                        "error": f"Failed to continue thread: {result.get('error')}",
                        "threadId": thread_id
                    })
                    return

                # Extract reply from sessions_send response (synchronous, no polling needed)
                reply = extract_reply_from_send_result(result)

            if reply:
                self._send_json(200, {
                    "ok": True,
                    "reply": reply,
                    "threadId": thread_id
                })
            else:
                self._send_json(504, {
                    "ok": False,
                    "error": "Timeout waiting for response",
                    "threadId": thread_id
                })
            return

        if parsed.path == "/dispatch":
            query = parse_qs(parsed.query)
            body, is_raw_body = self._read_dispatch_body()
            if body is None:
                return

            if is_raw_body:
                body = self._merge_dispatch_params(body, query)
            if not self._require_auth(body):
                return

            agent = body.get("agent")
            message = body.get("message")
            if not agent or not message:
                self._send_json(400, {"ok": False, "error": "agent and message required"})
                return

            agent = str(agent).lower()
            message = str(message)
            if agent not in {"codex", "claude", "gemini"}:
                self._send_json(400, {"ok": False, "error": "Unsupported agent"})
                return

            raw_thread_id = body.get("threadId")
            if raw_thread_id is None:
                thread_id = str(uuid.uuid4())
            else:
                if not isinstance(raw_thread_id, str):
                    self._send_json(400, {"ok": False, "error": "Invalid threadId"})
                    return
                try:
                    thread_id = validate_thread_id(raw_thread_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "Invalid threadId"})
                    return
            workdir = Path(body.get("workdir") or DISPATCH_DEFAULT_WORKDIR).expanduser()
            try:
                timeout_seconds = int(body.get("timeout") or body.get("timeoutSeconds") or DISPATCH_DEFAULT_TIMEOUT)
            except (TypeError, ValueError):
                self._send_json(400, {"ok": False, "error": "Invalid timeout value"})
                return
            callback = body.get("callback")

            if callback is not None and not isinstance(callback, dict):
                self._send_json(400, {"ok": False, "error": "callback must be an object"})
                return

            if not workdir.exists() or not workdir.is_dir():
                self._send_json(400, {"ok": False, "error": f"workdir not found: {workdir}"})
                return

            entries = load_thread_entries(thread_id)
            existing_agent = existing_agent_for_thread(entries)
            if existing_agent == "__mixed__":
                self._send_json(400, {"ok": False, "error": "Thread has mixed agents"})
                return
            if existing_agent and existing_agent != agent:
                self._send_json(400, {"ok": False, "error": f"Thread already bound to {existing_agent}"})
                return

            filtered_entries = filter_entries_for_prompt(entries, DISPATCH_MAX_TURNS)
            prompt = build_dispatch_prompt(filtered_entries, message, agent)

            turn = next_turn_number(entries)
            append_thread_entry(thread_id, {
                "turn": turn,
                "role": "edi",
                "content": message,
                "ts": int(time.time()),
            })

            task_id = str(uuid.uuid4())
            with TASKS_LOCK:
                TASKS[task_id] = {
                    "taskId": task_id,
                    "threadId": thread_id,
                    "agent": agent,
                    "status": "running",
                    "startedAt": int(time.time()),
                    "workdir": str(workdir),
                    "timeout": timeout_seconds,
                }

            thread = threading.Thread(
                target=run_dispatch_task,
                args=(task_id, thread_id, turn, agent, prompt, workdir, timeout_seconds, callback),
                daemon=True,
            )
            thread.start()

            with TASKS_LOCK:
                TASKS[task_id]["_thread"] = thread

            if DISPATCH_EARLY_CHECK_SECONDS > 0:
                time.sleep(DISPATCH_EARLY_CHECK_SECONDS)

                with TASKS_LOCK:
                    task_snapshot = dict(TASKS.get(task_id, {}))
                status = task_snapshot.get("status")
                exit_code = task_snapshot.get("exitCode")
                error = task_snapshot.get("error")
                process = task_snapshot.get("_process")

                if status and status != "running":
                    response = {
                        "ok": status in {"completed", "canceled"},
                        "taskId": task_id,
                        "threadId": thread_id,
                        "status": status,
                        "exitCode": exit_code,
                    }
                    if status == "failed":
                        response["error"] = error or "Dispatch failed quickly"
                        self._send_json(500, response)
                    else:
                        self._send_json(200, response)
                    return

                if process:
                    poll_code = process.poll()
                    if poll_code is not None:
                        status = "completed" if poll_code == 0 else "failed"
                        response = {
                            "ok": status == "completed",
                            "taskId": task_id,
                            "threadId": thread_id,
                            "status": status,
                            "exitCode": poll_code,
                        }
                        if status == "failed":
                            response["error"] = "Dispatch failed quickly"
                            self._send_json(500, response)
                        else:
                            self._send_json(200, response)
                        return

            self._send_json(200, {
                "ok": True,
                "taskId": task_id,
                "threadId": thread_id,
                "status": "running",
            })
            return

        if parsed.path.startswith("/tasks/") and parsed.path.endswith("/cancel"):
            body = self._read_json_body()
            if body is None:
                return

            if not self._require_auth(body):
                return

            task_id = parsed.path.split("/tasks/", 1)[1].rsplit("/cancel", 1)[0]
            if not task_id:
                self._send_json(400, {"ok": False, "error": "taskId required"})
                return

            with TASKS_LOCK:
                task = TASKS.get(task_id)

            if not task:
                self._send_json(404, {"ok": False, "error": "task not found"})
                return

            if task.get("status") != "running":
                self._send_json(200, {"ok": True, "status": task.get("status")})
                return

            with TASKS_LOCK:
                task = TASKS.get(task_id, {})
                task["cancel_requested"] = True
                task["status"] = "canceling"
                TASKS[task_id] = task
                process = task.get("_process")

            if process and process.poll() is None:
                try:
                    process.terminate()
                except Exception:
                    pass

            self._send_json(200, {"ok": True, "status": "canceling"})
            return

        self.send_error(404)
    
    def _send_json(self, status: int, data: dict):
        """Send JSON response."""
        response = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(response))
        self.end_headers()
        self.wfile.write(response)

    def _handle_github_webhook(self):
        """Handle GitHub webhook for merge notifications.

        Fire-and-forget pattern: triggers EDI and returns immediately.
        """
        # Read raw payload (needed for signature verification)
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length > MAX_REQUEST_SIZE:
                self._send_json(413, {"ok": False, "error": "Request too large"})
                return
            raw_payload = self.rfile.read(length) if length else b""
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"Failed to read body: {e}"})
            return

        # Verify GitHub signature
        github_secret = load_github_secret()
        if not github_secret:
            self.log_message("GitHub webhook: Rejecting request - No secret configured")
            self._send_json(503, {"ok": False, "error": "GitHub webhook secret not configured"})
            return

        signature = self.headers.get("X-Hub-Signature-256", "")
        if not signature:
            self._send_json(401, {"ok": False, "error": "Missing X-Hub-Signature-256 header"})
            return

        if not verify_github_signature(raw_payload, signature, github_secret):
            self.log_message("GitHub webhook: Invalid signature")
            self._send_json(401, {"ok": False, "error": "Invalid signature"})
            return

        # Parse payload
        try:
            body = json.loads(raw_payload) if raw_payload else {}
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        # Extract fields from payload
        repo = body.get("repository", "unknown/repo")
        ref = body.get("ref", "refs/heads/unknown")
        sha = body.get("sha", "unknown")
        commit_message = body.get("message", "")

        # Extract branch name from ref (refs/heads/main -> main)
        branch = ref.split("/")[-1] if "/" in ref else ref

        self.log_message(f"GitHub webhook: {repo} {branch} {sha[:7]}")

        # Create session key using repo and short SHA
        short_sha = sha[:7] if len(sha) >= 7 else sha
        repo_name = repo.split("/")[-1] if "/" in repo else repo
        session_key = f"github:{repo_name}:{short_sha}"

        # Format message for EDI
        message = f"""[GitHub Webhook - Repo Update]

Repository: {repo}
Branch: {branch}
Commit: {short_sha}
Message: "{commit_message[:200]}{'...' if len(commit_message) > 200 else ''}"

Please pull the latest changes and run the test suite."""

        # Fire-and-forget: trigger EDI but don't wait for response
        hook_result = trigger_agent_hook(session_key, message, DEFAULT_TIMEOUT)

        if hook_result.get("ok"):
            run_id = hook_result.get("runId", "unknown")
            self.log_message(f"GitHub webhook: Triggered EDI, runId={run_id}")
            self._send_json(200, {
                "ok": True,
                "message": "Webhook received, EDI notified",
                "runId": run_id,
                "sessionKey": session_key
            })
        else:
            error = hook_result.get("error", "Unknown error")
            self.log_message(f"GitHub webhook: Failed to trigger EDI: {error}")
            self._send_json(500, {
                "ok": False,
                "error": f"Failed to trigger EDI: {error}"
            })

    def log_message(self, format, *args):
        """Custom log format."""
        if args:
            print(f"[EDI] {format % args}")
        else:
            print(f"[EDI] {format}")


def main():
    """Start the server."""
    print("=" * 60)
    print("EDI Thread Server v4")
    print("=" * 60)
    print(f"Listening: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"Tailscale: http://100.104.206.23:{LISTEN_PORT}/ask")
    print()
    print("Endpoints:")
    print(f"  POST /ask            - Send message to EDI")
    print(f"  POST /dispatch       - Run a headless coding agent task")
    print(f"  POST /github-webhook - GitHub merge notifications")
    print(f"  GET  /tasks          - List dispatch tasks")
    print(f"  POST /tasks/<id>/cancel - Cancel a running task")
    print(f"  GET  /thread/<id>    - Fetch thread history")
    print(f"  GET  /health         - Health check")
    print()
    print("Request format:")
    print('  {"message": "...", "threadId": null}     # New thread')
    print('  {"message": "...", "threadId": "abc123"} # Continue thread')
    print()
    print("Server-generated threadId returned in response.")
    print()

    # Authentication status
    auth_secret = load_auth_secret()
    if auth_secret:
        print("Authentication: ENABLED (HMAC-SHA256)")
        print(f"  Timestamp tolerance: {AUTH_TIMESTAMP_TOLERANCE}s")
    else:
        print("Authentication: DISABLED (no secret configured)")
        print(f"  Set {AUTH_SECRET_ENV} env var or create {AUTH_SECRET_FILE}")

    print()

    # GitHub webhook status
    github_secret = load_github_secret()
    if github_secret:
        print("GitHub Webhook: ENABLED (signature verification)")
    else:
        print("GitHub Webhook: DISABLED (no secret configured)")
        print(f"  Set {GITHUB_WEBHOOK_SECRET_ENV} env var or create {GITHUB_WEBHOOK_SECRET_FILE}")

    print("=" * 60)
    print()
    
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), EDIHandler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[EDI] Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
