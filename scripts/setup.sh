#!/usr/bin/env bash
# Guided setup script for franktheunicorn.
#
# Usage:
#   ./scripts/setup.sh             # Interactive guided setup
#   ./scripts/setup.sh --docker    # Skip to Docker setup
#   ./scripts/setup.sh --local     # Skip to local dev setup
#   ./scripts/setup.sh --mock      # Local setup in mock/demo mode (no API keys)

set -euo pipefail

# --- Helpers ----------------------------------------------------------------

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

info()  { printf "${CYAN}%s${NC}\n" "$*"; }
ok()    { printf "${GREEN}%s${NC}\n" "$*"; }
warn()  { printf "${YELLOW}%s${NC}\n" "$*"; }
err()   { printf "${RED}%s${NC}\n" "$*" >&2; }

ask() {
    local prompt="$1" default="${2:-}"
    if [ -n "$default" ]; then
        printf "${BOLD}%s${NC} [%s] " "$prompt" "$default" > /dev/tty
    else
        printf "${BOLD}%s${NC} " "$prompt" > /dev/tty
    fi
    read -r answer < /dev/tty || answer=""
    echo "${answer:-$default}"
}

portable_sed_i() {
    # macOS BSD sed requires -i '' while GNU sed uses -i alone.
    if sed --version 2>/dev/null | grep -q 'GNU'; then
        sed -i "$@"
    else
        sed -i '' "$@"
    fi
}

set_yaml_value() {
    # Set a top-level key in a YAML file, portable across GNU and BSD sed.
    # Uses a temp-file approach for inserts to avoid BSD sed '1i' incompatibility.
    local key="$1" value="$2" file="$3"
    if grep -q "^${key}:" "$file" 2>/dev/null; then
        portable_sed_i "s/^${key}: .*/${key}: ${value}/" "$file"
    else
        local tmpfile
        tmpfile=$(mktemp "${file}.XXXXXX")
        printf '%s: %s\n' "$key" "$value" > "$tmpfile"
        cat "$file" >> "$tmpfile" 2>/dev/null || true
        mv "$tmpfile" "$file"
    fi
}

set_env() {
    # Safely set a key=value in .env without sed pattern injection.
    local key="$1" value="$2" file="${3:-.env}"
    local tmpfile
    tmpfile=$(mktemp "${file}.XXXXXX")
    grep -v "^${key}=" "$file" > "$tmpfile" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$value" >> "$tmpfile"
    mv "$tmpfile" "$file"
}

validate_github_token() {
    # Validate a GitHub token by hitting GET /user. Returns 0 on success.
    local token="$1"
    if ! command -v curl &>/dev/null; then
        warn "curl not found — skipping GitHub token validation"
        return 0
    fi
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -H "Authorization: token ${token}" \
        -H "Accept: application/vnd.github+json" \
        https://api.github.com/user 2>/dev/null) || {
        warn "Could not reach api.github.com — skipping token validation"
        return 0
    }
    if [ "$http_code" = "200" ]; then
        ok "GitHub token is valid"
        return 0
    else
        err "GitHub token validation failed (HTTP $http_code)"
        err "Check that the token is correct and has repo + read:org scopes."
        return 1
    fi
}

# --- Parse flags ------------------------------------------------------------

MODE=""
MOCK_MODE=""

for arg in "$@"; do
    case "$arg" in
        --docker) MODE="docker" ;;
        --local)  MODE="local" ;;
        --mock)   MOCK_MODE="true" ;;
        --help|-h)
            echo "Usage: $0 [--docker|--local] [--mock]"
            echo ""
            echo "  --docker   Set up with Docker Compose (quickest start)"
            echo "  --local    Set up for local Python development"
            echo "  --mock     Use mock/demo mode (no API keys needed)"
            echo ""
            echo "Without flags, the script runs an interactive guided setup."
            exit 0
            ;;
        *)
            err "Unknown flag: $arg"
            exit 1
            ;;
    esac
done

# --- Banner -----------------------------------------------------------------

echo ""
printf "${BOLD}franktheunicorn setup${NC}\n"
echo "Local-first AI code review assistant for open-source maintainers"
echo "================================================================"
echo ""

# --- Pre-flight checks ------------------------------------------------------

info "Checking prerequisites..."

errors=0

# Git
if command -v git &>/dev/null; then
    ok "  git: $(git --version | head -1)"
else
    err "  git: not found (required)"
    errors=$((errors + 1))
fi

# Python (only strictly required for local mode, but check anyway)
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        py_version=$("$cmd" --version 2>&1 | sed -n 's/.*Python \([0-9]*\.[0-9]*\).*/\1/p')
        py_major=$(echo "$py_version" | cut -d. -f1)
        py_minor=$(echo "$py_version" | cut -d. -f2)
        if [ "$py_major" -ge 3 ] && [ "$py_minor" -ge 11 ]; then
            PYTHON_CMD="$cmd"
            ok "  python: $($cmd --version) ($cmd)"
            break
        fi
    fi
done
if [ -z "$PYTHON_CMD" ]; then
    warn "  python: 3.11+ not found (required for local dev, not needed for Docker)"
fi

# Docker
if command -v docker &>/dev/null; then
    ok "  docker: $(docker --version | head -1)"
    HAS_DOCKER=true
else
    warn "  docker: not found (optional, needed for Docker setup)"
    HAS_DOCKER=false
fi

# Ollama
if command -v ollama &>/dev/null; then
    ok "  ollama: found (optional, for local LLM)"
else
    info "  ollama: not found (optional, for local LLM)"
fi

# llama.cpp
if command -v llama-server &>/dev/null; then
    ok "  llama.cpp: found (optional, for local LLM)"
else
    info "  llama.cpp: not found (optional, for local LLM)"
fi

echo ""

if [ "$errors" -gt 0 ]; then
    err "Missing required tools. Please install them and try again."
    exit 1
fi

# --- Choose setup mode ------------------------------------------------------

if [ -z "$MODE" ]; then
    echo "How would you like to set up franktheunicorn?"
    echo ""
    echo "  1. Docker (recommended for trying it out)"
    echo "     Runs in containers. Configure API keys for live PR ingestion."
    echo ""
    echo "  2. Local development"
    echo "     Python virtualenv, editable install, full guided configuration."
    echo ""
    choice=$(ask "Choose [1/2]:" "1")
    case "$choice" in
        1|docker|d) MODE="docker" ;;
        2|local|l)  MODE="local" ;;
        *)          MODE="docker" ;;
    esac
    echo ""
fi

# ============================================================================
# Docker setup
# ============================================================================

if [ "$MODE" = "docker" ]; then
    if [ "$HAS_DOCKER" = false ]; then
        err "Docker is not installed. Install Docker and try again,"
        err "or re-run with: $0 --local"
        exit 1
    fi

    info "Setting up with Docker Compose..."

    # Create .env for secrets if missing
    if [ ! -f .env ]; then
        cp .env.example .env
        ok "Created .env from .env.example (secrets only)"
    else
        ok ".env already exists, keeping it"
    fi

    # Create operator config if missing
    mkdir -p config/active/projects
    if [ ! -f config/active/operator.yaml ]; then
        cp config/examples/operator.yaml config/active/operator.yaml
        ok "Created config/active/operator.yaml from examples"
    else
        ok "config/active/operator.yaml already exists, keeping it"
    fi

    # Ask about mock mode
    if [ -z "$MOCK_MODE" ]; then
        echo ""
        echo "Mock mode uses fixture data so you can explore the dashboard"
        echo "without a GitHub token or API keys."
        echo ""
        use_mock=$(ask "Start in mock mode? (y/N):" "n")
        case "$use_mock" in
            [yY]*) MOCK_MODE="true" ;;
            *)     MOCK_MODE="false" ;;
        esac
    fi

    if [ "$MOCK_MODE" != "true" ]; then

        # Check for existing FRANK_GITHUB_TOKEN in .env
        existing_gh_token=$(grep "^FRANK_GITHUB_TOKEN=" .env 2>/dev/null | cut -d= -f2-) || true
        if [ -z "$existing_gh_token" ]; then
            for var in GITHUB_TOKEN GH_TOKEN; do
                val="${!var:-}"
                if [ -n "$val" ]; then
                    preview="${val:0:4}****"
                    ok "Found $var = $preview (usable for GitHub integration)"
                    set_env "FRANK_GITHUB_TOKEN" "$val"
                    ok "Auto-populated FRANK_GITHUB_TOKEN from $var"
                    existing_gh_token="$val"
                    break
                fi
            done
        fi

        if [ -z "$existing_gh_token" ]; then
            echo ""
            info "For real PR ingestion, you need a GitHub personal access token."
            info "Create one at: https://github.com/settings/tokens/new"
            info "Required scopes: repo, read:org"
            echo ""
            token=$(ask "GitHub token (or press Enter to skip):" "")
            if [ -n "$token" ]; then
                set_env "FRANK_GITHUB_TOKEN" "$token"
                ok "Saved token to .env (referenced as \${FRANK_GITHUB_TOKEN} in operator.yaml)"
                existing_gh_token="$token"
            else
                warn "Skipped. Set FRANK_GITHUB_TOKEN in .env before starting containers."
            fi
        else
            ok "GitHub token already set in .env"
        fi

        # Validate the token if we have one
        if [ -n "$existing_gh_token" ]; then
            validate_github_token "$existing_gh_token" || \
                warn "Continuing setup — fix the token in .env before starting containers."
        fi
    fi

    # --- Persist environment LLM keys to .env (survive across sessions) --------

    # Source .env so docker compose can resolve ${VAR} references and
    # setup_llm can detect API keys.
    if [ -f .env ]; then
        set -a
        # shellcheck disable=SC1091
        source .env
        set +a
    fi

    for var in ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY; do
        val="${!var:-}"
        existing_val=$(grep "^${var}=" .env 2>/dev/null | cut -d= -f2-)
        if [ -n "$val" ] && [ -z "$existing_val" ]; then
            set_env "$var" "$val"
        fi
    done

    # --- LLM backend configuration (via Docker container) ---------------------

    echo ""
    if [ "$MOCK_MODE" = "true" ]; then
        info "You can pre-configure LLM backends now for when you switch to real mode."
    else
        info "Configuring LLM backends..."
    fi
    echo "This wizard sets up which AI models to use for code review."
    echo ""

    info "Building Docker image (this may take a minute on first run)..."
    docker compose build web

    docker compose run --rm web python manage.py setup_llm
    echo ""

    # Persist the mock_mode choice into operator.yaml (created/updated by setup_llm above).
    if [ "$MOCK_MODE" = "true" ]; then
        set_yaml_value "mock_mode" "true" config/active/operator.yaml
        ok "Set mock_mode: true in config/active/operator.yaml"
    else
        set_yaml_value "mock_mode" "false" config/active/operator.yaml
    fi

    echo ""
    echo "================================================================"
    ok "Setup complete!"
    echo "================================================================"
    echo ""
    echo "Next steps:"
    echo ""
    echo "  ${BOLD}Start the services:${NC}"
    echo "    docker compose up"
    echo "    # Dashboard: http://localhost:8000"
    echo ""
    echo "  ${BOLD}Configuration:${NC}"
    echo "    config/active/operator.yaml  (app config, LLM backends)"
    echo "    .env                         (API keys, secrets)"
    echo ""
    if [ "$MOCK_MODE" = "true" ]; then
        echo "  ${BOLD}Switch to real mode later:${NC}"
        echo "    Edit config/active/operator.yaml: set mock_mode: false"
        echo "    Add API keys to .env"
        echo "    Re-run: ./scripts/setup.sh --docker"
        echo ""
    fi
    exit 0
fi

# ============================================================================
# Local development setup
# ============================================================================

if [ -z "$PYTHON_CMD" ]; then
    err "Python 3.11+ is required for local development."
    err "Install Python 3.11+ and try again, or use: $0 --docker"
    exit 1
fi

info "Setting up local development environment..."
echo ""

# --- Secrets file (before make setup, so it doesn't warn) ------------------

if [ ! -f .env ]; then
    cp .env.example .env
    ok "Created .env from .env.example (secrets only)"
else
    ok ".env already exists, keeping it"
fi

# --- Virtualenv + dependencies via Make ------------------------------------

do_install=$(ask "Install dependencies now? (Y/n):" "y")
if [[ "$do_install" =~ ^[yY] ]] || [ -z "$do_install" ]; then
    if [ -f Makefile ]; then
        info "Running 'make setup' (creates venv, installs deps, runs migrations)..."
        set -x
        make setup
        set +x
        ok "make setup complete"
        # shellcheck disable=SC1091
        source .venv/bin/activate
    else
        # Fallback if Makefile doesn't exist
        if [ ! -d ".venv" ]; then
            info "Creating virtualenv with $PYTHON_CMD..."
            "$PYTHON_CMD" -m venv .venv
            ok "Created .venv"
        else
            ok "Virtualenv .venv already exists"
        fi

        # shellcheck disable=SC1091
        source .venv/bin/activate
        ok "Activated virtualenv"

        info "Installing dependencies (this may take a minute)..."
        set -x
        pip install -e ".[dev]" --quiet
        set +x
        ok "Dependencies installed"
    fi
else
    warn "Skipping dependency install. Run 'make setup' manually later."
    if [ -d ".venv" ]; then
        # shellcheck disable=SC1091
        source .venv/bin/activate
        ok "Activated existing virtualenv"
    else
        warn "No existing .venv found, so local Python setup steps will be skipped."
        info "Next steps:"
        info "  1. Run 'make setup' to create the virtualenv and install dependencies."
        info "  2. Re-run ./scripts/setup.sh --local (or ./scripts/setup.sh)."
        exit 0
    fi
fi
echo ""

# --- Mock mode vs real mode -------------------------------------------------

if [ -z "$MOCK_MODE" ]; then
    echo ""
    echo "franktheunicorn can run in two modes:"
    echo ""
    echo "  Real mode  — connects to GitHub and an LLM provider."
    echo "               Requires a GitHub token and at least one API key."
    echo ""
    echo "  Mock mode  — uses fixture data, no API keys needed."
    echo "               Great for exploring the dashboard and running tests."
    echo ""
    use_mock=$(ask "Start in mock mode? (y/N):" "n")
    case "$use_mock" in
        [yY]*) MOCK_MODE="true" ;;
        *)     MOCK_MODE="false" ;;
    esac
fi

if [ "$MOCK_MODE" = "false" ]; then

    # ------------------------------------------------------------------
    # GitHub token (auto-detect from environment or prompt)
    # ------------------------------------------------------------------

    # Check for existing GitHub tokens in environment
    existing_gh_token=$(grep "^FRANK_GITHUB_TOKEN=" .env 2>/dev/null | cut -d= -f2-) || true
    if [ -z "$existing_gh_token" ]; then
        for var in GITHUB_TOKEN GH_TOKEN; do
            val="${!var:-}"
            if [ -n "$val" ]; then
                preview="${val:0:4}****"
                ok "  Found $var = $preview (usable for GitHub integration)"
                set_env "FRANK_GITHUB_TOKEN" "$val"
                ok "  Auto-populated FRANK_GITHUB_TOKEN from $var"
                existing_gh_token="$val"
                break
            fi
        done
    fi

    if [ -z "$existing_gh_token" ]; then
        echo ""
        info "GitHub Personal Access Token"
        info "Create one at: https://github.com/settings/tokens/new"
        info "Required scopes: repo, read:org"
        echo ""
        token=$(ask "GitHub token (paste here):" "")
        if [ -n "$token" ]; then
            set_env "FRANK_GITHUB_TOKEN" "$token"
            ok "Saved GitHub token to .env"
            existing_gh_token="$token"
        else
            warn "Skipped. Set FRANK_GITHUB_TOKEN in .env later for real PR ingestion."
        fi
    else
        ok "GitHub token set in .env"
    fi

    # Validate the token if we have one
    if [ -n "$existing_gh_token" ]; then
        validate_github_token "$existing_gh_token" || \
            warn "Continuing setup — fix the token in .env before running the worker."
    fi

    # Persist any env-sourced LLM keys (ANTHROPIC_API_KEY etc.) to .env
    # so they survive across shell sessions.
    for var in ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY; do
        val="${!var:-}"
        existing_val=$(grep "^${var}=" .env 2>/dev/null | cut -d= -f2-)
        if [ -n "$val" ] && [ -z "$existing_val" ]; then
            set_env "$var" "$val"
        fi
    done
fi

echo ""

# --- Data directory + migrations --------------------------------------------

mkdir -p data
if [ ! -f Makefile ]; then
    # Only run migrations if make setup didn't already handle it
    info "Setting up database..."
    python manage.py migrate --verbosity 0
    ok "Database ready"
fi
echo ""

# --- LLM backend configuration (single wizard handles everything) ----------
if [ "$MOCK_MODE" = "true" ]; then
    info "You can pre-configure LLM backends now for when you switch to real mode."
else
    info "Configuring LLM backends..."
fi
echo "This wizard sets up which AI models to use for code review."
echo ""

# Source .env so setup_llm can detect API keys written earlier in this script.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

python manage.py setup_llm
echo ""

# Persist the mock_mode choice into operator.yaml (created by setup_llm above).
if [ "$MOCK_MODE" = "true" ]; then
    set_yaml_value "mock_mode" "true" config/active/operator.yaml
    ok "Set mock_mode: true in config/active/operator.yaml"
fi

# --- Verification -----------------------------------------------------------

echo ""
info "Verifying setup..."

# Quick import check
if python -c "import franktheunicorn" 2>/dev/null; then
    ok "  Package imports successfully"
else
    warn "  Package import failed — check installation"
fi

# Check migrations
if python manage.py showmigrations --plan 2>/dev/null | grep -q '\[X\]'; then
    ok "  Database migrations applied"
else
    warn "  Some migrations may be pending"
fi

echo ""

# --- Done -------------------------------------------------------------------

echo "================================================================"
ok "Setup complete!"
echo "================================================================"
echo ""
echo "Next steps:"
echo ""
echo "  ${BOLD}Start the dashboard:${NC}"
echo "    make serve                         # or: python manage.py runserver"
echo "    # Open http://localhost:8000"
echo ""
echo "  ${BOLD}Start the worker (separate terminal):${NC}"
echo "    make worker                        # or: python manage.py run_worker"
echo ""
echo "  ${BOLD}Run tests:${NC}"
echo "    make check                         # lint + typecheck + tests"
echo "    make test                          # tests only"
echo ""
if [ "$MOCK_MODE" = "true" ]; then
    echo "  ${BOLD}Switch to real mode later:${NC}"
    echo "    Edit config/active/operator.yaml: set mock_mode: false"
    echo "    Add API keys to .env (referenced via \${VAR} in operator.yaml)"
    echo "    Re-run: ./scripts/setup.sh --local"
    echo ""
fi
echo "  ${BOLD}Configure projects and workspaces:${NC}"
echo "    See docs/install.md for full configuration guide"
echo ""
echo "  ${BOLD}Or use Docker instead:${NC}"
echo "    docker compose up"
echo ""
