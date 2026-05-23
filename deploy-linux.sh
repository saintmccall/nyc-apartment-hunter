#!/usr/bin/env bash
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}▶${NC} $*"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $*"; }
error() { echo -e "${RED}✗${NC}  $*" >&2; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="nyc-apartment-hunter"
SERVICE_SRC="$PROJECT_DIR/systemd/$SERVICE_NAME.service"
SERVICE_DEST="/etc/systemd/system/$SERVICE_NAME.service"
VENV="$PROJECT_DIR/.venv"
ENV_FILE="$PROJECT_DIR/.env"

# ── 1. System packages ────────────────────────────────────────────────────────
info "Installing system packages…"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git curl

# ── 2. Python venv ────────────────────────────────────────────────────────────
info "Creating Python virtual environment…"
python3 -m venv "$VENV"
source "$VENV/bin/activate"

# ── 3. Install dependencies ───────────────────────────────────────────────────
info "Installing Python dependencies…"
pip install --quiet --upgrade pip setuptools
pip install --quiet -e "$PROJECT_DIR[browser]"

info "Installing Playwright + Chromium (this takes a minute)…"
playwright install chromium
playwright install-deps chromium

# ── 4. Credentials ────────────────────────────────────────────────────────────
echo ""
info "Setting up credentials"

if [[ -f "$ENV_FILE" ]]; then
  while IFS='=' read -r key val; do
    [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
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

if [[ -n "$ANTHROPIC_API_KEY" && -n "$HUNTER_EMAIL_ADDRESS" && -n "$HUNTER_EMAIL_APP_PASSWORD" ]]; then
  info "All credentials loaded from .env — skipping prompts"
else
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

  cat > "$ENV_FILE" <<ENVEOF
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"
HUNTER_EMAIL_ADDRESS="${HUNTER_EMAIL_ADDRESS}"
HUNTER_EMAIL_APP_PASSWORD="${HUNTER_EMAIL_APP_PASSWORD}"
ENVEOF
  chmod 600 "$ENV_FILE"
  info ".env written (chmod 600)"
fi

# ── 5. Data directory ─────────────────────────────────────────────────────────
mkdir -p "$PROJECT_DIR/data"

# ── 6. Install systemd service ────────────────────────────────────────────────
info "Installing systemd service…"

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$SERVICE_SRC" > "$SERVICE_DEST"
chmod 644 "$SERVICE_DEST"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
info "Service started — runs every 10 minutes"

# ── 7. Verify ─────────────────────────────────────────────────────────────────
echo ""
sleep 3
if systemctl is-active --quiet "$SERVICE_NAME"; then
  info "Service is running ✓"
else
  error "Service failed to start. Check logs with: journalctl -u $SERVICE_NAME -n 50"
  exit 1
fi

echo ""
info "Deploy complete!"
echo ""
echo "  Useful commands:"
echo "    make logs       — tail live log"
echo "    make run        — run once in foreground"
echo "    make status     — check if service is running"
echo "    make stop       — stop the service"
echo "    make start      — start the service"
echo ""
warn "Your .env at $ENV_FILE contains API keys — do not share it."
