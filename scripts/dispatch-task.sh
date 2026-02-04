#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage:
  dispatch-task.sh --log-file <path> [--command <string> | --command-file <path>]
  dispatch-task.sh --log-file <path> --command -    # read command from stdin

Options:
  --log-file <path>           Base log file path (creates .out, .exit, .pid)
  --command <string>          Command or script content to run
  --command-file <path>       Read command content from a file
  --early-check-seconds <n>   Seconds to wait before reporting early failure
  -h, --help                  Show this help

Env:
  EARLY_CHECK_SECONDS         Default early check delay (fallback when flag unset)
USAGE
}

COMMAND=""
COMMAND_FILE=""
LOG_FILE=""
EARLY_CHECK_SECONDS="${EARLY_CHECK_SECONDS:-5}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --command)
            COMMAND="${2:-}"
            shift 2
            ;;
        --command-file)
            COMMAND_FILE="${2:-}"
            shift 2
            ;;
        --log-file)
            LOG_FILE="${2:-}"
            shift 2
            ;;
        --early-check-seconds)
            EARLY_CHECK_SECONDS="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            if [[ -z "$COMMAND" ]]; then
                COMMAND="$1"
                shift
            else
                echo "Unknown argument: $1" >&2
                usage
                exit 2
            fi
            ;;
    esac
    
done

if [[ -n "$COMMAND_FILE" ]]; then
    if [[ "$COMMAND_FILE" == "-" ]]; then
        COMMAND="$(cat)"
    else
        COMMAND="$(cat "$COMMAND_FILE")"
    fi
elif [[ "$COMMAND" == "-" ]]; then
    COMMAND="$(cat)"
fi

if [[ -z "$LOG_FILE" ]]; then
    echo "Error: --log-file is required" >&2
    usage
    exit 2
fi

if [[ -z "$COMMAND" ]]; then
    echo "Error: command is required (use --command or --command-file)" >&2
    usage
    exit 2
fi

mkdir -p "$(dirname "$LOG_FILE")"

OUT_FILE="${LOG_FILE}.out"
EXIT_FILE="${LOG_FILE}.exit"
PID_FILE="${LOG_FILE}.pid"

: > "$OUT_FILE"

COMMAND_SCRIPT="$(mktemp "${TMPDIR:-/tmp}/dispatch-command.XXXXXX")"
{
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' 'set -euo pipefail'
    printf '%s\n' "$COMMAND"
} > "$COMMAND_SCRIPT"
chmod +x "$COMMAND_SCRIPT"

WRAPPER_SCRIPT="$(mktemp "${TMPDIR:-/tmp}/dispatch-wrapper.XXXXXX")"
cat > "$WRAPPER_SCRIPT" <<'WRAPPER'
#!/usr/bin/env bash
set -u
script="$1"
out_file="$2"
exit_file="$3"
exit_code=0
trap 'printf "%s" "$exit_code" > "$exit_file"' EXIT

bash "$script" > "$out_file" 2>&1
exit_code=$?
WRAPPER
chmod +x "$WRAPPER_SCRIPT"

nohup "$WRAPPER_SCRIPT" "$COMMAND_SCRIPT" "$OUT_FILE" "$EXIT_FILE" >/dev/null 2>&1 &
pid=$!
echo "$pid" > "$PID_FILE"

sleep "$EARLY_CHECK_SECONDS"

if kill -0 "$pid" 2>/dev/null; then
    echo "Dispatched $pid"
    exit 0
fi

if [[ -f "$EXIT_FILE" ]]; then
    exit_code="$(cat "$EXIT_FILE" 2>/dev/null || printf '1')"
    if [[ "$exit_code" == "0" ]]; then
        echo "Dispatched $pid (completed quickly)"
        exit 0
    fi
    echo "Dispatch failed quickly (exit $exit_code). See $OUT_FILE" >&2
    exit 1
fi

echo "Dispatch failed to start. See $OUT_FILE" >&2
exit 1
