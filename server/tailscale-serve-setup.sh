#!/bin/bash
#
# Smartt - Tailscale Serve Setup
#
# Exposes the Smartt HTTP server as a named Tailscale service,
# making it accessible via HTTPS on your tailnet at:
#   svc:<service-name>.<tailnet>.ts.net
#
# Prerequisites:
#   - Tailscale installed and logged in on this machine
#   - Smartt server running (Docker or native) on the configured port
#
# Usage:
#   ./tailscale-serve-setup.sh                        # Setup with defaults (port 8080, name "smartt")
#   ./tailscale-serve-setup.sh 8081                   # Custom port
#   ./tailscale-serve-setup.sh 8080 my-reader         # Custom port and service name
#   ./tailscale-serve-setup.sh --remove               # Remove (default service name)
#   ./tailscale-serve-setup.sh --remove my-reader     # Remove specific service name
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Defaults
DEFAULT_PORT="8080"
DEFAULT_SERVICE_NAME="smartt"

echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Smartt - Tailscale Serve Setup${NC}"
echo -e "${BLUE}============================================${NC}"
echo

# Handle --remove flag
if [ "$1" = "--remove" ]; then
    SERVICE_NAME="${2:-$DEFAULT_SERVICE_NAME}"
    echo "Removing Tailscale service svc:${SERVICE_NAME}..."
    tailscale serve --service="svc:${SERVICE_NAME}" off 2>/dev/null || true
    echo -e "${GREEN}Done.${NC} Service svc:${SERVICE_NAME} removed from tailnet."
    exit 0
fi

# Parse arguments
PORT="${1:-$DEFAULT_PORT}"
SERVICE_NAME="${2:-$DEFAULT_SERVICE_NAME}"

# Check if Tailscale is installed
if ! command -v tailscale &> /dev/null; then
    echo -e "${RED}ERROR: Tailscale is not installed${NC}"
    echo "Install Tailscale: https://tailscale.com/download"
    exit 1
fi
echo -e "${GREEN}✓${NC} Tailscale is installed"

# Check if Tailscale is connected
TS_STATUS=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('BackendState',''))" 2>/dev/null || echo "")
if [ "$TS_STATUS" != "Running" ]; then
    echo -e "${RED}ERROR: Tailscale is not connected${NC}"
    echo "Run: sudo tailscale up"
    exit 1
fi
echo -e "${GREEN}✓${NC} Tailscale is connected"

# Get tailnet name for display
TS_HOSTNAME=$(tailscale status --json 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['Self']['DNSName'].rstrip('.'))" 2>/dev/null || echo "unknown")
TS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")

echo -e "${GREEN}✓${NC} Machine hostname: ${BLUE}${TS_HOSTNAME}${NC}"
echo -e "${GREEN}✓${NC} Tailscale IP: ${BLUE}${TS_IP}${NC}"
echo

# Check if the local service is running
echo "Checking if Smartt is running on port ${PORT}..."
if curl -sf "http://localhost:${PORT}/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Smartt is responding on port ${PORT}"
else
    echo -e "${YELLOW}!${NC} Smartt is not responding on port ${PORT}"
    echo "  Make sure the Docker container is running and mapped to port ${PORT}"
    echo "  Continuing with setup anyway..."
fi
echo

# Derive the tailnet suffix from the machine's DNS name
# e.g., "myhost.tail1234.ts.net" -> "tail1234.ts.net"
TAILNET_SUFFIX=$(echo "$TS_HOSTNAME" | sed 's/^[^.]*\.//')
SERVICE_FQDN="${SERVICE_NAME}.${TAILNET_SUFFIX}"

# Configure Tailscale Serve as a named service
echo "Configuring Tailscale service..."
echo "  Service:  svc:${SERVICE_NAME}"
echo "  Target:   http://localhost:${PORT}"
echo

sudo tailscale serve --service="svc:${SERVICE_NAME}" --bg --https=443 "http://localhost:${PORT}"

echo
echo -e "${GREEN}✓${NC} Tailscale service configured successfully!"
echo
echo -e "${BLUE}============================================${NC}"
echo -e "${BLUE}  Service is now accessible on your tailnet${NC}"
echo -e "${BLUE}============================================${NC}"
echo
echo "Your Smartt is now available at:"
echo -e "  ${GREEN}https://${SERVICE_FQDN}${NC}"
echo
echo "Test it from any device on your tailnet:"
echo -e "  ${GREEN}curl https://${SERVICE_FQDN}/health${NC}"
echo
echo -e "${YELLOW}============================================${NC}"
echo -e "${YELLOW}  IMPORTANT: Approve ports in admin console${NC}"
echo -e "${YELLOW}============================================${NC}"
echo
echo "The service is now advertising on your tailnet, but you must"
echo "approve the required ports in the Tailscale admin console:"
echo
echo "  1. Go to https://login.tailscale.com/admin/services"
echo "  2. Find the '${SERVICE_NAME}' service"
echo "  3. Click on it and add port ${BLUE}443 (HTTPS)${NC} to the service"
echo "  4. Save the configuration"
echo
echo "Until this step is done, the admin console will show:"
echo -e "  ${YELLOW}\"Advertising the service, but some required ports are missing\"${NC}"
echo
echo "Useful commands:"
echo -e "  View serve status:  ${GREEN}tailscale serve status${NC}"
echo -e "  Remove service:     ${GREEN}$0 --remove ${SERVICE_NAME}${NC}"
echo -e "  Tailscale status:   ${GREEN}tailscale status${NC}"
echo
