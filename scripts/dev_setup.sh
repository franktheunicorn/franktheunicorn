#!/usr/bin/env bash
# Guided setup script for franktheunicorn.
#
# Usage:
#   ./scripts/dev_setup.sh             # Interactive guided setup
#   ./scripts/dev_setup.sh --docker    # Skip to Docker setup
#   ./scripts/dev_setup.sh --local     # Skip to local dev setup
#   ./scripts/dev_setup.sh --mock      # Local setup in mock/demo mode (no API keys)

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
        printf "${BOLD}%s${NC} [%s] " "$prompt" "$default"
    else
        printf "${BOLD}%s${NC} " "$prompt"
    fi
    read -r answer
    echo "${answer:-$default}"
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
        py_version=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+' | head -1)
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
    info "  ollama: not found (optional, for local LLM — https://ollama.com)"
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

    # Create .env if missing
    if [ ! -f .env ]; then
        cp .env.example .env
        ok "Created .env from .env.example"
    else
        ok ".env already exists, keeping it"
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

    if [ "$MOCK_MODE" = "true" ]; then
        set_env "FRANK_MOCK_MODE" "true"
    else
        echo ""
        info "For real PR ingestion, you need a GitHub personal access token."
        info "Create one at: https://github.com/settings/tokens"
        info "Required scopes: repo, read:org"
        echo ""
        token=$(ask "GitHub token (or press Enter to skip):" "")
        if [ -n "$token" ]; then
            set_env "FRANK_GITHUB_TOKEN" "$token"
            ok "Saved token to .env"
        fi
    fi

    echo ""
    info "Starting containers..."
    echo "  docker compose up"
    echo ""
    info "The dashboard will be available at: http://localhost:8000"
    info "Press Ctrl+C to stop."
    echo ""
    ok "Setup complete! Run 'docker compose up' to start."
    echo ""
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

# --- Environment file (before make setup, so it doesn't warn) ---------------

if [ ! -f .env ]; then
    cp .env.example .env
    ok "Created .env from .env.example"
else
    ok ".env already exists, keeping it"
fi

# --- Virtualenv + dependencies via Make ------------------------------------

if [ -f Makefile ]; then
    info "Running 'make setup' (creates venv, installs deps, runs migrations)..."
    make setup
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
    pip install -e ".[dev]" --quiet
    ok "Dependencies installed"
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
    set_env "FRANK_MOCK_MODE" "false"

    # ------------------------------------------------------------------
    # Scan environment for existing credentials
    # ------------------------------------------------------------------
    echo ""
    info "Scanning environment for LLM credentials..."
    echo ""

    detected_count=0
    default_provider=""

    # Tier 1: native provider keys
    for var in ANTHROPIC_API_KEY OPENAI_API_KEY GOOGLE_API_KEY; do
        val="${!var:-}"
        if [ -n "$val" ]; then
            preview="${val:0:4}****"
            ok "  Found $var = $preview"
            # Write into .env
            set_env "$var" "$val"
            detected_count=$((detected_count + 1))
            case "$var" in
                ANTHROPIC_API_KEY) [ -z "$default_provider" ] && default_provider="1" ;;
                OPENAI_API_KEY)    [ -z "$default_provider" ] && default_provider="2" ;;
                GOOGLE_API_KEY)    [ -z "$default_provider" ] && default_provider="3" ;;
            esac
        fi
    done

    # GitHub tokens
    existing_gh_token=$(grep "^FRANK_GITHUB_TOKEN=" .env | cut -d= -f2-)
    if [ -z "$existing_gh_token" ]; then
        for var in GITHUB_TOKEN GH_TOKEN; do
            val="${!var:-}"
            if [ -n "$val" ]; then
                preview="${val:0:4}****"
                ok "  Found $var = $preview (usable for GitHub integration)"
                set_env "FRANK_GITHUB_TOKEN" "$val"
                ok "  Auto-populated FRANK_GITHUB_TOKEN from $var"
                break
            fi
        done
    fi

    # Tier 2: known third-party LLM providers
    for var in MISTRAL_API_KEY DEEPSEEK_API_KEY GROQ_API_KEY TOGETHER_API_KEY \
               TOGETHER_AI_API_KEY FIREWORKS_API_KEY REPLICATE_API_TOKEN \
               COHERE_API_KEY AI21_API_KEY AZURE_OPENAI_API_KEY HF_TOKEN \
               HUGGING_FACE_HUB_TOKEN; do
        val="${!var:-}"
        if [ -n "$val" ]; then
            preview="${val:0:4}****"
            info "  Found $var = $preview (OpenAI-compatible backend possible)"
            detected_count=$((detected_count + 1))
        fi
    done

    # Tier 3: fuzzy endpoint detection
    while IFS='=' read -r name value; do
        # Skip empty or already-handled vars
        [ -z "$value" ] && continue
        case "$name" in
            *_URL|*_BASE_URL|*_ENDPOINT|*_HOST|*_BASE)
                if echo "$value" | grep -qE '(/api/.*(/v1|/chat/completions)|/v1(/|$)|/chat/completions)'; then
                    preview="${value:0:20}****"
                    info "  Possible LLM endpoint: $name = $preview"
                    detected_count=$((detected_count + 1))
                fi
                ;;
        esac
    done < <(env)

    if [ "$detected_count" -gt 0 ]; then
        echo ""
        ok "  Detected $detected_count credential(s) in environment."
    fi

    # ------------------------------------------------------------------
    # GitHub token (manual entry if not detected)
    # ------------------------------------------------------------------
    existing_token=$(grep "^FRANK_GITHUB_TOKEN=" .env | cut -d= -f2-)
    if [ -z "$existing_token" ]; then
        echo ""
        info "GitHub Personal Access Token"
        info "Create one at: https://github.com/settings/tokens"
        info "Required scopes: repo, read:org"
        echo ""
        token=$(ask "GitHub token (paste here):" "")
        if [ -n "$token" ]; then
            set_env "FRANK_GITHUB_TOKEN" "$token"
            ok "Saved GitHub token to .env"
        else
            warn "Skipped. Set FRANK_GITHUB_TOKEN in .env later for real PR ingestion."
        fi
    else
        ok "GitHub token set in .env"
    fi

    # ------------------------------------------------------------------
    # LLM API key selection
    # ------------------------------------------------------------------
    echo ""
    info "LLM API Key"
    info "franktheunicorn supports Anthropic Claude, OpenAI, Google Gemini, and Ollama."
    info "You can configure multiple providers. For now, set at least one API key."
    echo ""

    existing_anthropic=$(grep "^ANTHROPIC_API_KEY=" .env | cut -d= -f2-)
    if [ -n "$existing_anthropic" ]; then
        ok "Anthropic API key already set in .env"
    else
        echo "  1. Anthropic (Claude) — recommended"
        echo "  2. OpenAI"
        echo "  3. Google (Gemini)"
        echo "  4. Ollama (local, no key needed)"
        echo "  5. Skip for now"
        echo ""
        provider_choice=$(ask "Which provider?" "${default_provider:-1}")
        case "$provider_choice" in
            1)
                key=$(ask "Anthropic API key:" "")
                [ -n "$key" ] && set_env "ANTHROPIC_API_KEY" "$key" && ok "Saved to .env"
                ;;
            2)
                key=$(ask "OpenAI API key:" "")
                [ -n "$key" ] && set_env "OPENAI_API_KEY" "$key" && ok "Saved to .env"
                ;;
            3)
                key=$(ask "Google API key:" "")
                [ -n "$key" ] && set_env "GOOGLE_API_KEY" "$key" && ok "Saved to .env"
                ;;
            4)
                info "No API key needed for Ollama. Make sure it's running locally."
                ;;
            5)
                warn "Skipped. Set an API key in .env before using real mode."
                ;;
        esac
    fi
else
    set_env "FRANK_MOCK_MODE" "true"
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

# --- Project initialization -------------------------------------------------

info "Initializing configuration..."
echo "This creates your operator config at ~/.review-agent/"
echo ""
python manage.py init_project
echo ""

# --- LLM backend configuration ---------------------------------------------

if [ "$MOCK_MODE" = "false" ]; then
    echo ""
    info "Configuring LLM backends..."
    echo "This wizard sets up which AI models to use for code review."
    echo ""
    python manage.py setup_llm
    echo ""
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
    echo "    Edit .env: set FRANK_MOCK_MODE=false and add your API keys"
    echo "    Re-run: ./scripts/dev_setup.sh --local"
    echo ""
fi
echo "  ${BOLD}Configure projects and workspaces:${NC}"
echo "    See docs/install.md for full configuration guide"
echo ""
echo "  ${BOLD}Or use Docker instead:${NC}"
echo "    docker compose up"
echo ""
