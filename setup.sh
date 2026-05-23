#!/usr/bin/env bash
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}▶${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
error() { echo -e "${RED}✗${NC}  $*" >&2; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.nyc-apartment-hunter"
PLIST_SRC="$PROJECT_DIR/launchd/$PLIST_NAME.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"
VENV="$PROJECT_DIR/.venv"
ENV_FILE="$PROJECT_DIR/.env"

# ── 1. Python venv ────────────────────────────────────────────────────────────
info "Creating Python virtual environment…"
python3 -m venv "$VENV"
source "$VENV/bin/activate"

# ── 2. Install dependencies ───────────────────────────────────────────────────
info "Installing dependencies…"
pip install --quiet --upgrade pip setuptools
pip install --quiet -e "$PROJECT_DIR[browser]"
info "Installing Playwright browser (Chromium)…"
playwright install chromium 2>/dev/null || python -m playwright install chromium

# ── 3. Credentials ────────────────────────────────────────────────────────────
echo ""
info "Setting up credentials"

# Load existing .env
if [[ -f "$ENV_FILE" ]]; then
  # Parse .env manually to avoid bash sourcing issues with special characters
  while IFS='=' read -r key val; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
    # Strip surrounding quotes from value
    val="${val%\"}"
    val="${val#\"}"
    val="${val%\'}"
    val="${val#\'}"
    export "$key"="$val"
  done < "$ENV_FILE"
fi

ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
HUNTER_EMAIL_ADDRESS="${HUNTER_EMAIL_ADDRESS:-}"
HUNTER_EMAIL_APP_PASSWORD="${HUNTER_EMAIL_APP_PASSWORD:-}"

# If all three are already set, skip prompts
if [[ -n "$ANTHROPIC_API_KEY" && -n "$HUNTER_EMAIL_ADDRESS" && -n "$HUNTER_EMAIL_APP_PASSWORD" ]]; then
  info "All credentials loaded from .env — skipping prompts"
else
  # Only prompt for missing values
  if [[ -z "$ANTHROPIC_API_KEY" ]]; then
    printf "  Anthropic API key (sk-ant-…): "
    read -r ANTHROPIC_API_KEY
  fi
  if [[ -z "$HUNTER_EMAIL_ADDRESS" ]]; then
    printf "  Gmail address: "
    read -r HUNTER_EMAIL_ADDRESS
  fi
  if [[ -z "$HUNTER_EMAIL_APP_PASSWORD" ]]; then
    printf "  Gmail App Password (16 chars): "
    read -r HUNTER_EMAIL_APP_PASSWORD
  fi

  # Write updated .env
  cat > "$ENV_FILE" <<ENVEOF
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"
HUNTER_EMAIL_ADDRESS="${HUNTER_EMAIL_ADDRESS}"
HUNTER_EMAIL_APP_PASSWORD="${HUNTER_EMAIL_APP_PASSWORD}"
ENVEOF
  chmod 600 "$ENV_FILE"
  info ".env written (chmod 600)"
fi

# ── 4. Install launchd plist ─────────────────────────────────────────────────
info "Installing launchd agent…"
mkdir -p "$HOME/Library/LaunchAgents"

sed \
  -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
  -e "s|__ANTHROPIC_API_KEY__|${ANTHROPIC_API_KEY}|g" \
  -e "s|__HUNTER_EMAIL_ADDRESS__|${HUNTER_EMAIL_ADDRESS}|g" \
  -e "s|__HUNTER_EMAIL_APP_PASSWORD__|${HUNTER_EMAIL_APP_PASSWORD}|g" \
  "$PLIST_SRC" > "$PLIST_DEST"
chmod 644 "$PLIST_DEST"

if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
  launchctl unload "$PLIST_DEST" 2>/dev/null || true
fi
launchctl load "$PLIST_DEST"
info "launchd agent loaded — runs every 10 minutes"

# ── 5. First test run ─────────────────────────────────────────────────────────
echo ""
info "Running first scrape (this may take a minute)…"
mkdir -p "$PROJECT_DIR/data"

cd "$PROJECT_DIR"
"$VENV/bin/python" main.py &
PID=$!
sleep 30
kill "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true

echo ""
info "Setup complete!"
echo ""
echo "  Useful commands:"
echo "    make logs       — tail live log"
echo "    make run        — run once in foreground"
echo "    make stop       — unload the launchd agent"
echo "    make status     — check if agent is running"
echo "    make scrape-test — test scrapers only"
echo ""
warn "The plist at $PLIST_DEST contains your API keys."
warn "Do not share or commit that file."
