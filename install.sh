#!/usr/bin/env bash
# =============================================================================
# ASL3-API Installer
# https://github.com/KJ5IRQ/ASL3-API
#
# Usage:
#   ./install.sh          Guided mode: explains each step before running it
#   ./install.sh --auto   Auto mode: runs everything, only prompts for config
#
# What this script does:
#   1. Checks prerequisites (Python 3.9+, Asterisk running)
#   2. Creates /opt/asl3-api and copies project files
#   3. Creates a Python virtual environment and installs dependencies
#   4. Walks you through creating config.yaml
#   5. Adds the AMI user block to /etc/asterisk/manager.conf
#   6. Installs and enables the systemd service
#   7. Runs a smoke test against the live API
#
# Requirements:
#   - ASL3 installed and Asterisk running
#   - Python 3.10 or later
#   - sudo access
#   - Run from the directory containing the ASL3-API source files
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------

INSTALL_DIR="/opt/asl3-api"
SERVICE_FILE="asl3-api.service"
SERVICE_NAME="asl3-api"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTO=false
INSTALL_USER="$(whoami)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()    { echo -e "${GREEN}[INFO]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
step()    { echo -e "\n${BOLD}${CYAN}==>${RESET}${BOLD} $*${RESET}"; }
explain() { echo -e "${CYAN}$*${RESET}"; }

pause() {
    if [ "$AUTO" = false ]; then
        echo ""
        read -rp "Press Enter to continue (or Ctrl+C to abort)..."
    fi
}

confirm() {
    # confirm "message" [default: y]
    local msg="$1"
    local default="${2:-y}"
    if [ "$AUTO" = true ]; then
        return 0
    fi
    local prompt
    if [ "$default" = "y" ]; then
        prompt="[Y/n]"
    else
        prompt="[y/N]"
    fi
    read -rp "$msg $prompt " answer
    answer="${answer:-$default}"
    [[ "$answer" =~ ^[Yy]$ ]]
}

require_root() {
    if [ "$EUID" -ne 0 ]; then
        error "This step requires sudo. Re-run as: sudo $0 $*"
        exit 1
    fi
}

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

for arg in "$@"; do
    case $arg in
        --auto) AUTO=true ;;
        --help|-h)
            echo "Usage: $0 [--auto]"
            echo ""
            echo "  (no flags)   Guided mode — explains each step before running it"
            echo "  --auto       Auto mode — runs everything, only prompts for config values"
            exit 0
            ;;
        *)
            error "Unknown argument: $arg"
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

echo ""
echo -e "${BOLD}ASL3-API Installer${RESET}"
echo "================================================="
if [ "$AUTO" = true ]; then
    echo "Mode: Auto (--auto)"
else
    echo "Mode: Guided (run with --auto to skip explanations)"
fi
echo "Install directory: $INSTALL_DIR"
echo "Running as user:   $INSTALL_USER"
echo "================================================="
echo ""

if [ "$AUTO" = false ]; then
    explain "This installer will set up ASL3-API on your AllStar node."
    explain "You will be asked to confirm each major step before it runs."
    explain "At any point, press Ctrl+C to stop without making changes."
    pause
fi

# ---------------------------------------------------------------------------
# Step 1: Check prerequisites
# ---------------------------------------------------------------------------

step "Step 1: Checking prerequisites"

if [ "$AUTO" = false ]; then
    explain "Before installing, we need to verify:"
    explain "  - Python 3.10 or later is installed"
    explain "  - Asterisk is installed and currently running"
    explain "  - We are running from the correct directory"
    pause
fi

# Python version check
if ! command -v python3 &>/dev/null; then
    error "Python 3 is not installed. Install it with: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    error "Python 3.10+ required. Found Python $PY_VERSION."
    exit 1
fi
info "Python $PY_VERSION found"

# python3-venv check
if ! python3 -m venv --help &>/dev/null; then
    error "python3-venv is not installed. Install it with: sudo apt install python3-venv"
    exit 1
fi
info "python3-venv available"

# Asterisk check
if ! command -v asterisk &>/dev/null; then
    error "Asterisk is not installed. Install ASL3 before running this installer."
    exit 1
fi
info "Asterisk found"

if ! systemctl is-active --quiet asterisk 2>/dev/null; then
    warn "Asterisk does not appear to be running."
    warn "ASL3-API requires Asterisk to be active. You can still install now"
    warn "and start the service after Asterisk is running."
    if ! confirm "Continue anyway?"; then
        exit 0
    fi
else
    info "Asterisk is running"
fi

# Source file check
REQUIRED_FILES=("asl_agent.py" "ami_client.py" "config.py" "event_handler.py" "node_cache.py"
                "config.yaml.example" "requirements.txt" "asl3-api.service")
for f in "${REQUIRED_FILES[@]}"; do
    if [ ! -f "$SCRIPT_DIR/$f" ]; then
        error "Required file not found: $SCRIPT_DIR/$f"
        error "Run this installer from the ASL3-API source directory."
        exit 1
    fi
done
info "All source files present"

# ---------------------------------------------------------------------------
# Step 2: Create install directory and copy files
# ---------------------------------------------------------------------------

step "Step 2: Installing files to $INSTALL_DIR"

if [ "$AUTO" = false ]; then
    explain "ASL3-API will be installed to $INSTALL_DIR."
    explain "The installer will:"
    explain "  - Create $INSTALL_DIR if it doesn't exist"
    explain "  - Copy the Python source files there"
    explain "  - Set ownership to $INSTALL_USER"
    pause
fi

if confirm "Create $INSTALL_DIR and install files?" "y"; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo cp "$SCRIPT_DIR/asl_agent.py"       "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/ami_client.py"      "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/config.py"          "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/event_handler.py"   "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/node_cache.py"      "$INSTALL_DIR/"
    sudo cp "$SCRIPT_DIR/requirements.txt"   "$INSTALL_DIR/"

    # Copy example config only if config.yaml doesn't already exist
    if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
        sudo cp "$SCRIPT_DIR/config.yaml.example" "$INSTALL_DIR/config.yaml.example"
    fi

    sudo chown -R "$INSTALL_USER:$INSTALL_USER" "$INSTALL_DIR"
    info "Files installed to $INSTALL_DIR"
else
    warn "Skipped file installation"
fi

# ---------------------------------------------------------------------------
# Step 3: Create Python virtual environment
# ---------------------------------------------------------------------------

step "Step 3: Setting up Python virtual environment"

if [ "$AUTO" = false ]; then
    explain "A virtual environment keeps ASL3-API's Python packages isolated from"
    explain "the rest of your system. This prevents version conflicts and makes"
    explain "it easy to update or remove ASL3-API without affecting anything else."
    pause
fi

if confirm "Create virtual environment and install Python packages?" "y"; then
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    info "Virtual environment created and packages installed"
else
    warn "Skipped virtual environment setup"
fi

# ---------------------------------------------------------------------------
# Step 4: Configure ASL3-API
# ---------------------------------------------------------------------------

step "Step 4: Configuring ASL3-API"

if [ "$AUTO" = false ]; then
    explain "Now we'll set up your config.yaml. You'll need:"
    explain "  - Your AllStar node number (e.g. 637050)"
    explain "  - Your callsign (e.g. KJ5IRQ)"
    explain "  - A password for the AMI user we'll create in Asterisk"
    explain "  - An API key (we can generate one for you)"
    explain ""
    explain "config.yaml contains passwords — it is set to be readable only by"
    explain "your user account, not by other users on the system."
    pause
fi

CONFIG_FILE="$INSTALL_DIR/config.yaml"

if [ -f "$CONFIG_FILE" ]; then
    warn "config.yaml already exists at $CONFIG_FILE"
    if ! confirm "Overwrite it with a new configuration?" "n"; then
        info "Keeping existing config.yaml"
        SKIP_CONFIG=true
    else
        SKIP_CONFIG=false
    fi
else
    SKIP_CONFIG=false
fi

if [ "$SKIP_CONFIG" = false ]; then
    # Collect values
    echo ""
    read -rp "Your AllStar node number: " NODE_NUMBER
    read -rp "Your callsign:            " NODE_CALLSIGN

    echo ""
    echo "Generating AMI password..."
    AMI_PASSWORD=$(openssl rand -base64 16)
    info "AMI password generated (will be written to config.yaml and manager.conf)"

    echo ""
    echo "Generating API key..."
    API_KEY=$(openssl rand -base64 32)
    info "API key generated (will be written to config.yaml)"

    # Write config.yaml
    cat > "$CONFIG_FILE" <<EOF
# ASL3-API Configuration
# Generated by install.sh on $(date)

ami:
  host: "127.0.0.1"
  port: 5038
  username: "asl3-api"
  password: "$AMI_PASSWORD"

node:
  number: "$NODE_NUMBER"
  callsign: "$NODE_CALLSIGN"

api:
  host: "0.0.0.0"
  port: 8073
  api_key: "$API_KEY"

webhooks:
  enabled: false
  url: ""

logging:
  level: "INFO"
  audit_file: "/opt/asl3-api/audit.log"

security:
  rate_limit_per_minute: 60
EOF

    chmod 600 "$CONFIG_FILE"
    chown "$INSTALL_USER:$INSTALL_USER" "$CONFIG_FILE"
    info "config.yaml written (permissions: 600, owner: $INSTALL_USER)"

    echo ""
    echo -e "${BOLD}Your API key (save this — you'll need it in your client):${RESET}"
    echo -e "${CYAN}$API_KEY${RESET}"
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 5: Configure Asterisk AMI
# ---------------------------------------------------------------------------

step "Step 5: Configuring Asterisk AMI"

if [ "$AUTO" = false ]; then
    explain "ASL3-API communicates with Asterisk through the Asterisk Manager"
    explain "Interface (AMI). We need to add a dedicated user account for ASL3-API"
    explain "in /etc/asterisk/manager.conf."
    explain ""
    explain "The AMI user is locked to localhost (127.0.0.1) only — it cannot"
    explain "be accessed from outside your Pi. The password in manager.conf must"
    explain "match the password in config.yaml."
    explain ""
    explain "IMPORTANT: Asterisk reads AMI passwords as literal text. Do not"
    explain "wrap the password in quotes in manager.conf."
    pause
fi

MANAGER_CONF="/etc/asterisk/manager.conf"

if [ ! -f "$MANAGER_CONF" ]; then
    warn "$MANAGER_CONF not found. You will need to add the AMI user manually."
    warn "See the INSTALLATION.md for the required configuration block."
else
    if grep -q "\[asl3-api\]" "$MANAGER_CONF" 2>/dev/null; then
        warn "[asl3-api] block already exists in $MANAGER_CONF"
        if ! confirm "Replace the existing [asl3-api] block?" "n"; then
            info "Leaving existing AMI configuration in place"
            SKIP_AMI=true
        else
            SKIP_AMI=false
            # Remove existing block
            sudo sed -i '/^\[asl3-api\]/,/^$/d' "$MANAGER_CONF"
        fi
    else
        SKIP_AMI=false
    fi

    if [ "${SKIP_AMI:-false}" = false ] && [ "${SKIP_CONFIG:-false}" = false ]; then
        if confirm "Add [asl3-api] AMI user to $MANAGER_CONF?" "y"; then
            sudo tee -a "$MANAGER_CONF" > /dev/null <<EOF

[asl3-api]
secret = $AMI_PASSWORD
read = system,call,reporting,command
write = command,reporting
deny = 0.0.0.0/0.0.0.0
permit = 127.0.0.1/255.255.255.255
EOF
            info "AMI user [asl3-api] added to $MANAGER_CONF"

            info "Reloading Asterisk manager..."
            sudo asterisk -rx "manager reload" > /dev/null 2>&1 || \
                warn "Could not reload Asterisk manager. Run: sudo asterisk -rx 'manager reload'"
        else
            warn "Skipped AMI configuration. Add the [asl3-api] block manually."
        fi
    fi
fi

# ---------------------------------------------------------------------------
# Step 6: Install systemd service
# ---------------------------------------------------------------------------

step "Step 6: Installing systemd service"

if [ "$AUTO" = false ]; then
    explain "The systemd service ensures ASL3-API starts automatically when"
    explain "your Pi boots and restarts automatically if it crashes."
    explain ""
    explain "The service runs as $INSTALL_USER (your current user), which already"
    explain "has the correct permissions for the Asterisk configuration files."
    pause
fi

if confirm "Install and enable the systemd service?" "y"; then
    # Substitute INSTALL_USER placeholder in service file
    TEMP_SERVICE=$(mktemp)
    sed "s/INSTALL_USER/$INSTALL_USER/g" "$SCRIPT_DIR/$SERVICE_FILE" > "$TEMP_SERVICE"

    sudo cp "$TEMP_SERVICE" "/etc/systemd/system/$SERVICE_NAME.service"
    rm "$TEMP_SERVICE"

    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    info "Service installed and enabled (will start on next boot)"
else
    warn "Skipped service installation"
fi

# ---------------------------------------------------------------------------
# Step 7: Start the service
# ---------------------------------------------------------------------------

step "Step 7: Starting ASL3-API"

if [ "$AUTO" = false ]; then
    explain "Starting the service now. If anything is misconfigured (wrong AMI"
    explain "password, wrong node number, etc.) the service will fail to start."
    explain "We'll check the status immediately after starting."
    pause
fi

if confirm "Start ASL3-API now?" "y"; then
    sudo systemctl start "$SERVICE_NAME" || true
    sleep 5

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        info "ASL3-API is running"
    else
        warn "ASL3-API failed to start. Checking logs..."
        echo ""
        sudo journalctl -u "$SERVICE_NAME" -n 20 --no-pager
        echo ""
        warn "Fix the issue and run: sudo systemctl start $SERVICE_NAME"
    fi
fi

# ---------------------------------------------------------------------------
# Step 8: Smoke test
# ---------------------------------------------------------------------------

step "Step 8: Smoke test"

if [ "$AUTO" = false ]; then
    explain "We'll send a request to the /ping endpoint to verify the API is"
    explain "responding. /ping requires no authentication — it just confirms"
    explain "the service is up and AMI is connected."
    pause
fi

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    sleep 2
    PING_RESULT=$(curl -s --max-time 5 "http://127.0.0.1:8073/ping" 2>/dev/null || true)

    if echo "$PING_RESULT" | grep -q '"ami_connected": true'; then
        info "Smoke test passed — API is up and AMI is connected"
        echo "$PING_RESULT"
    elif echo "$PING_RESULT" | grep -q '"ami_connected": false'; then
        warn "API is up but AMI is NOT connected"
        warn "Check your AMI password in config.yaml and manager.conf"
        echo "$PING_RESULT"
    else
        warn "Could not reach /ping endpoint"
        warn "Check: sudo journalctl -u $SERVICE_NAME -n 30"
    fi
else
    info "Service is not running — skipping smoke test"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo -e "${BOLD}${GREEN}Installation complete.${RESET}"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status $SERVICE_NAME     — check service status"
echo "  sudo systemctl restart $SERVICE_NAME    — restart after config changes"
echo "  sudo journalctl -u $SERVICE_NAME -f     — follow live logs"
echo "  curl http://127.0.0.1:8073/ping         — check API health"
echo ""
echo "Your API is available at: http://$(hostname -I | awk '{print $1}'):8073"
echo ""
if [ "${SKIP_CONFIG:-false}" = false ] && [ -n "${API_KEY:-}" ]; then
    echo -e "${BOLD}API Key:${RESET} $API_KEY"
    echo "(Also saved in $CONFIG_FILE)"
    echo ""
fi
echo "See docs/INSTALLATION.md for next steps."
