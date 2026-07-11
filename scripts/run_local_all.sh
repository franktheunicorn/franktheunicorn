#!/usr/bin/env bash
# Run franktheunicorn locally without Docker.
#
# Usage:
#   ./scripts/run_local_all.sh              # start web + worker
#   ./scripts/run_local_all.sh up --follow  # start and tail logs
#   ./scripts/run_local_all.sh status       # show process status
#   ./scripts/run_local_all.sh logs         # tail web + worker logs
#   ./scripts/run_local_all.sh down         # stop web + worker

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

APP_NAME="franktheunicorn"
WEB_URL="http://localhost:7742/"
WEB_BIND="0.0.0.0:7742"
RUN_DIR="${REPO_ROOT}/data/run"
LOG_DIR="${REPO_ROOT}/data/logs"
BACKEND_FILE="${RUN_DIR}/local_all.backend"

SERVICES=("web" "worker")

info() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
err()  { printf 'Error: %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
Usage: ./scripts/run_local_all.sh [up|down|status|logs] [options]

Commands:
  up                 Start web, wait for health, then start worker (default)
  down               Stop web and worker, removing stale pid files
  status             Show process state and attach instructions
  logs [web|worker]  Tail logs; defaults to both

Options:
  --foreground       With up, tail logs after startup
  --follow           Alias for --foreground
  -h, --help         Show this help

Ctrl-C while following logs only stops the tail. Processes keep running.
Use './scripts/run_local_all.sh down' or 'make down' to stop them.
USAGE
}

ensure_dirs() {
    mkdir -p "${RUN_DIR}" "${LOG_DIR}"
}

python_cmd() {
    if [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
        printf '%s\n' "${REPO_ROOT}/.venv/bin/python"
    else
        printf '%s\n' "python3"
    fi
}

load_env() {
    if [ -f "${REPO_ROOT}/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        . "${REPO_ROOT}/.env"
        set +a
    fi

    export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-franktheunicorn.settings}"
    export DJANGO_DEBUG="${DJANGO_DEBUG:-false}"
}

detect_backend() {
    if command -v screen >/dev/null 2>&1; then
        printf '%s\n' "screen"
    elif command -v tmux >/dev/null 2>&1; then
        printf '%s\n' "tmux"
    else
        printf '%s\n' "nohup"
    fi
}

service_session() {
    local service="$1"
    printf '%s-%s\n' "${APP_NAME}" "${service}"
}

service_pid_file() {
    local service="$1"
    printf '%s/%s.pid\n' "${RUN_DIR}" "${service}"
}

service_backend_file() {
    local service="$1"
    printf '%s/%s.backend\n' "${RUN_DIR}" "${service}"
}

service_log() {
    local service="$1"
    printf '%s/%s.log\n' "${LOG_DIR}" "${service}"
}

record_service_backend() {
    local service="$1" backend="$2"
    printf '%s\n' "${backend}" > "$(service_backend_file "${service}")"
}

read_service_backend() {
    local service="$1" backend_file
    backend_file="$(service_backend_file "${service}")"
    if [ -f "${backend_file}" ]; then
        cat "${backend_file}"
    elif [ -f "${BACKEND_FILE}" ]; then
        cat "${BACKEND_FILE}"
    else
        detect_backend
    fi
}

screen_running() {
    local session="$1"
    command -v screen >/dev/null 2>&1 || return 1
    { screen -ls 2>/dev/null || true; } | grep -q "[.]${session}[[:space:]]"
}

tmux_running() {
    local session="$1"
    command -v tmux >/dev/null 2>&1 || return 1
    tmux has-session -t "=${session}" 2>/dev/null
}

pid_running() {
    local pid_file="$1"
    if [ ! -f "${pid_file}" ]; then
        return 1
    fi

    local pid
    pid="$(cat "${pid_file}" 2>/dev/null || true)"
    if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi

    rm -f "${pid_file}"
    return 1
}

service_running_with_backend() {
    local service="$1" backend="$2" session pid_file
    session="$(service_session "${service}")"
    pid_file="$(service_pid_file "${service}")"

    case "${backend}" in
        screen) screen_running "${session}" || pid_running "${pid_file}" ;;
        tmux) tmux_running "${session}" || pid_running "${pid_file}" ;;
        nohup) pid_running "${pid_file}" ;;
        *) return 1 ;;
    esac
}

service_running() {
    local service="$1"
    local backend
    backend="$(read_service_backend "${service}")"
    service_running_with_backend "${service}" "${backend}"
}

command_for_service() {
    local service="$1" py="$2"
    case "${service}" in
        web)
            printf 'cd %q && export DJANGO_SETTINGS_MODULE=%q DJANGO_DEBUG=false && exec %q -m gunicorn franktheunicorn.wsgi:application --bind %q --workers 2 --access-logfile -' \
                "${REPO_ROOT}" "${DJANGO_SETTINGS_MODULE}" "${py}" "${WEB_BIND}"
            ;;
        worker)
            printf 'cd %q && export DJANGO_SETTINGS_MODULE=%q DJANGO_DEBUG=%q && exec %q -m franktheunicorn.worker.runner' \
                "${REPO_ROOT}" "${DJANGO_SETTINGS_MODULE}" "${DJANGO_DEBUG}" "${py}"
            ;;
        *)
            err "Unknown service: ${service}"
            exit 1
            ;;
    esac
}

start_service() {
    local service="$1" backend="$2" py="$3"
    local session log pid_file cmd run_cmd pane_cmd
    session="$(service_session "${service}")"
    log="$(service_log "${service}")"
    pid_file="$(service_pid_file "${service}")"

    if service_running "${service}"; then
        info "${service}: already running; skipping"
        return 0
    fi

    : > "${log}"
    rm -f "${pid_file}"
    cmd="$(command_for_service "${service}" "${py}")"
    run_cmd="printf '%s\n' \"\$\$\" > $(printf '%q' "${pid_file}"); ${cmd}"
    pane_cmd="exec > >(tee -a $(printf '%q' "${log}")) 2>&1; ${run_cmd}"

    case "${backend}" in
        screen)
            screen -dmS "${session}" bash -lc "${pane_cmd}"
            ;;
        tmux)
            tmux new-session -d -s "${session}" "bash -lc $(printf '%q' "${pane_cmd}")"
            ;;
        nohup)
            if command -v setsid >/dev/null 2>&1; then
                nohup setsid bash -lc "${run_cmd}" >> "${log}" 2>&1 &
            else
                nohup bash -lc "trap '' INT; ${run_cmd}" >> "${log}" 2>&1 &
            fi
            ;;
        *)
            err "Unknown backend: ${backend}"
            exit 1
            ;;
    esac

    record_service_backend "${service}" "${backend}"
    info "${service}: started with ${backend}"
}

stop_pid_if_running() {
    local service="$1" pid_file pid
    pid_file="$(service_pid_file "${service}")"

    if ! pid_running "${pid_file}"; then
        return 1
    fi

    pid="$(cat "${pid_file}")"
    kill "${pid}" 2>/dev/null || true
    wait_for_pid_exit "${pid}" 10 || kill -9 "${pid}" 2>/dev/null || true
    if kill -0 "${pid}" 2>/dev/null; then
        warn "${service}: pid ${pid} is still running"
        return 1
    fi

    rm -f "${pid_file}"
    info "${service}: stopped pid ${pid}"
    return 0
}

stop_service() {
    local service="$1" backend session pid_file stopped="false"
    backend="$(read_service_backend "${service}")"
    session="$(service_session "${service}")"
    pid_file="$(service_pid_file "${service}")"

    case "${backend}" in
        screen)
            if screen_running "${session}"; then
                screen -S "${session}" -X quit || true
                info "${service}: stopped screen session ${session}"
                stopped="true"
                sleep 1
            fi
            # Always use the pidfile as a backstop. Session detection can be
            # flaky, and the pidfile is the only handle for orphaned children.
            if stop_pid_if_running "${service}"; then
                stopped="true"
            fi
            if [ "${stopped}" = "false" ]; then
                info "${service}: not running"
            fi
            ;;
        tmux)
            if tmux_running "${session}"; then
                tmux kill-session -t "${session}" || true
                info "${service}: stopped tmux session ${session}"
                stopped="true"
                sleep 1
            fi
            # Always use the pidfile as a backstop. Session detection can be
            # flaky, and the pidfile is the only handle for orphaned children.
            if stop_pid_if_running "${service}"; then
                stopped="true"
            fi
            if [ "${stopped}" = "false" ]; then
                info "${service}: not running"
            fi
            ;;
        nohup)
            if stop_pid_if_running "${service}"; then
                stopped="true"
            fi
            if [ "${stopped}" = "false" ]; then
                info "${service}: not running"
            fi
            ;;
        *)
            if stop_pid_if_running "${service}"; then
                stopped="true"
            fi
            if [ "${stopped}" = "false" ]; then
                info "${service}: not running"
            fi
            ;;
    esac

    if ! pid_running "${pid_file}"; then
        rm -f "$(service_backend_file "${service}")"
    fi
}

wait_for_pid_exit() {
    local pid="$1" timeout="$2" elapsed=0
    while kill -0 "${pid}" 2>/dev/null; do
        if [ "${elapsed}" -ge "${timeout}" ]; then
            return 1
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
}

http_ok() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --max-time 3 "${WEB_URL}" >/dev/null 2>&1
    else
        "$(python_cmd)" - <<PY >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("${WEB_URL}", timeout=3).read(1)
PY
    fi
}

wait_for_web() {
    local timeout="${1:-60}" elapsed=0
    info "Waiting for web health at ${WEB_URL} ..."
    until http_ok; do
        if ! service_running web; then
            err "web exited before becoming healthy"
            err "Recent web log:"
            tail -n 40 "$(service_log web)" >&2 || true
            return 1
        fi
        if [ "${elapsed}" -ge "${timeout}" ]; then
            err "web did not respond within ${timeout}s"
            err "Recent web log:"
            tail -n 40 "$(service_log web)" >&2 || true
            return 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
    info "web: healthy at ${WEB_URL}"
}

bootstrap() {
    local py="$1"
    info "Running migrations ..."
    "${py}" manage.py migrate --run-syncdb
    info "Collecting static files ..."
    DJANGO_DEBUG=false "${py}" manage.py collectstatic --noinput
}

attach_hint() {
    local service="$1" backend="$2" session
    session="$(service_session "${service}")"
    case "${backend}" in
        screen)
            if screen_running "${session}"; then
                printf 'screen -r %s  (detach: Ctrl-A then D)' "${session}"
            else
                printf 'tail -f %s' "$(service_log "${service}")"
            fi
            ;;
        tmux)
            if tmux_running "${session}"; then
                printf 'tmux attach -t %s  (detach: Ctrl-B then D)' "${session}"
            else
                printf 'tail -f %s' "$(service_log "${service}")"
            fi
            ;;
        nohup)
            printf 'tail -f %s' "$(service_log "${service}")"
            ;;
    esac
}

print_status_line() {
    local service="$1" backend status
    backend="$(read_service_backend "${service}")"
    if service_running_with_backend "${service}" "${backend}"; then
        status="up"
    else
        status="down"
    fi

    printf '%-6s %-4s backend=%-6s log=%s\n' \
        "${service}:" "${status}" "${backend}" "$(service_log "${service}")"
    if [ "${status}" = "up" ]; then
        printf '       attach: '
        attach_hint "${service}" "${backend}"
        printf '\n'
    fi
}

print_instructions() {
    local backend="$1"
    cat <<EOF

Dashboard: ${WEB_URL%/}
Backend:   ${backend}

Attach:
  web:    $(attach_hint web "${backend}")
  worker: $(attach_hint worker "${backend}")

Logs:
  ./scripts/run_local_all.sh logs

Stop:
  ./scripts/run_local_all.sh down
  make down
EOF
}

cmd_up() {
    local follow="$1" backend py started_web="false"
    ensure_dirs
    load_env
    backend="$(detect_backend)"
    py="$(python_cmd)"
    printf '%s\n' "${backend}" > "${BACKEND_FILE}"

    info "Using Python: ${py}"
    info "Using backend: ${backend}"

    if ! service_running web; then
        bootstrap "${py}"
        started_web="true"
    else
        info "web: already running; skipping bootstrap"
    fi

    start_service web "${backend}" "${py}"
    if ! wait_for_web 60; then
        if [ "${started_web}" = "true" ]; then
            stop_service web || true
        fi
        exit 1
    fi
    start_service worker "${backend}" "${py}"
    print_instructions "${backend}"

    if [ "${follow}" = "true" ]; then
        info ""
        info "Following logs. Ctrl-C stops following only; processes keep running."
        tail_logs ""
    fi
}

cmd_down() {
    ensure_dirs
    for service in worker web; do
        stop_service "${service}"
    done
    rm -f "${BACKEND_FILE}"
}

cmd_status() {
    ensure_dirs
    for service in "${SERVICES[@]}"; do
        print_status_line "${service}"
    done
    info ""
    info "Dashboard: ${WEB_URL%/}"
}

tail_logs() {
    local service="${1:-}"
    ensure_dirs
    case "${service}" in
        "")
            touch "$(service_log web)" "$(service_log worker)"
            tail -n 80 -F "$(service_log web)" "$(service_log worker)"
            ;;
        web|worker)
            touch "$(service_log "${service}")"
            tail -n 120 -F "$(service_log "${service}")"
            ;;
        *)
            err "Unknown log target: ${service}"
            usage
            exit 1
            ;;
    esac
}

main() {
    local command="${1:-up}" follow="false" log_target=""
    if [ $# -gt 0 ]; then
        shift
    fi

    case "${command}" in
        -h|--help)
            usage
            exit 0
            ;;
    esac

    while [ $# -gt 0 ]; do
        case "$1" in
            --foreground|--follow)
                follow="true"
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            web|worker)
                log_target="$1"
                ;;
            *)
                err "Unknown argument: $1"
                usage
                exit 1
                ;;
        esac
        shift
    done

    case "${command}" in
        up) cmd_up "${follow}" ;;
        down) cmd_down ;;
        status) cmd_status ;;
        logs) tail_logs "${log_target}" ;;
        *)
            err "Unknown command: ${command}"
            usage
            exit 1
            ;;
    esac
}

main "$@"
