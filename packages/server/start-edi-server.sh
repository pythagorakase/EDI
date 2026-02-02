#!/bin/bash
# Start EDI Thread Server
# Usage: ./start-edi-server.sh [start|stop|restart|status]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_SCRIPT="$SCRIPT_DIR/edi-thread-server.py"
PID_FILE="/tmp/edi-server.pid"
LOG_FILE="/tmp/edi-server.log"
ENV_FILE="/etc/edi/env"

start() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        echo "EDI server already running (PID: $(cat $PID_FILE))"
        return 1
    fi

    # Load environment (includes EDI_AUTH_SECRET)
    if [ -f "$ENV_FILE" ]; then
        echo "Loading environment from $ENV_FILE"
        source "$ENV_FILE"
    fi

    # Ensure coding agent CLIs are in PATH
    export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

    echo "Starting EDI Thread Server..."
    nohup env EDI_AUTH_SECRET="${EDI_AUTH_SECRET:-}" python3 "$SERVER_SCRIPT" > "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 2
    
    if kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        echo "EDI server started (PID: $(cat $PID_FILE))"
        echo "Log: $LOG_FILE"
        echo "Endpoint: http://100.104.206.23:19001/ask"
    else
        echo "Failed to start EDI server"
        cat "$LOG_FILE"
        return 1
    fi
}

stop() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "Stopping EDI server (PID: $PID)..."
            kill "$PID"
            rm -f "$PID_FILE"
            echo "Stopped."
        else
            echo "EDI server not running (stale PID file)"
            rm -f "$PID_FILE"
        fi
    else
        echo "EDI server not running (no PID file)"
    fi
}

status() {
    if [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null; then
        echo "EDI server running (PID: $(cat $PID_FILE))"
        curl -s http://127.0.0.1:19001/health | python3 -m json.tool
    else
        echo "EDI server not running"
    fi
}

case "${1:-start}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}" ;;
esac
