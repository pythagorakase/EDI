#!/usr/bin/env python3
"""
EDI Thread Server v3 - Server-side threading with session continuity.

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

  GET /health
    Returns: {"ok": true, "server": "edi-thread-server", "version": "3"}
"""

import hashlib
import hmac
import json
import os
import uuid
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, Any

# Configuration
CLAWDBOT_URL = "http://127.0.0.1:18789"
GATEWAY_TOKEN = "h2WzPZjazQG8CQYrS8RgXI5MMVWFh6SI"  # For /tools/invoke
HOOKS_TOKEN = "edi-hook-secret-2026"  # For /hooks/agent
LISTEN_PORT = 19001
LISTEN_HOST = "0.0.0.0"  # Accessible via Tailscale
DEFAULT_TIMEOUT = 120
POLL_INTERVAL = 1.0  # seconds between polls

# HMAC Authentication
AUTH_SECRET_ENV = "EDI_AUTH_SECRET"
AUTH_SECRET_FILE = Path("/etc/edi/secret")
AUTH_TIMESTAMP_TOLERANCE = 300  # 5 minutes in seconds


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


def canonicalize_auth_payload(payload: Dict[str, Any]) -> str:
    """Create a canonical JSON string for HMAC signing."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def verify_hmac_signature(
    payload: Dict[str, Any],
    timestamp: str,
    signature: str,
    secret: bytes
) -> tuple[bool, str]:
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


class EDIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for EDI thread server."""
    
    def do_GET(self):
        """Handle GET requests (health check)."""
        if self.path == "/health":
            self._send_json(200, {
                "ok": True,
                "server": "edi-thread-server",
                "version": "3"
            })
        else:
            self.send_error(404)
    
    def do_POST(self):
        """Handle POST requests (ask endpoint)."""
        if self.path != "/ask":
            self.send_error(404)
            return

        # Parse request body
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except json.JSONDecodeError:
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        message = body.get("message")
        if not message:
            self._send_json(400, {"ok": False, "error": "message required"})
            return

        # HMAC Authentication
        auth_secret = load_auth_secret()
        if auth_secret:
            timestamp = self.headers.get("X-EDI-Timestamp")
            signature = self.headers.get("X-EDI-Signature")

            if not timestamp or not signature:
                self._send_json(401, {"ok": False, "error": "Missing authentication headers"})
                return

            is_valid, error = verify_hmac_signature(body, timestamp, signature, auth_secret)
            if not is_valid:
                self.log_message(f"Auth failed: {error}")
                self._send_json(401, {"ok": False, "error": f"Authentication failed: {error}"})
                return
        
        timeout_seconds = body.get("timeoutSeconds", DEFAULT_TIMEOUT)
        
        # Server generates thread ID if not provided (this is the key feature!)
        thread_id = body.get("threadId")
        is_new_thread = thread_id is None
        
        if is_new_thread:
            thread_id = str(uuid.uuid4())[:8]
        
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
    
    def _send_json(self, status: int, data: dict):
        """Send JSON response."""
        response = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(response))
        self.end_headers()
        self.wfile.write(response)
    
    def log_message(self, format, *args):
        """Custom log format."""
        if args:
            print(f"[EDI] {format % args}")
        else:
            print(f"[EDI] {format}")


def main():
    """Start the server."""
    print("=" * 60)
    print("EDI Thread Server v3")
    print("=" * 60)
    print(f"Listening: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"Tailscale: http://100.104.206.23:{LISTEN_PORT}/ask")
    print()
    print("Endpoints:")
    print(f"  POST /ask  - Send message to EDI")
    print(f"  GET /health - Health check")
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

    print("=" * 60)
    print()
    
    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), EDIHandler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[EDI] Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
