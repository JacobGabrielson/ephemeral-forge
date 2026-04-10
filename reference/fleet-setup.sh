#!/usr/bin/env bash
# fleet-setup.sh — Install vegeta and configure DNS on fleet agents.
#
# Usage:
#   ./fleet-setup.sh <tag> <sut-ip>
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TAG="${1:?Usage: fleet-setup.sh <tag> <sut-ip>}"
SUT_IP="${2:?Usage: fleet-setup.sh <tag> <sut-ip>}"
STATE_DIR="$SCRIPT_DIR/fleet-state/$TAG"

if [[ ! -d "$STATE_DIR" ]]; then
    echo "ERROR: No fleet state at $STATE_DIR" >&2
    exit 1
fi

KEY="$STATE_DIR/key.pem"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10"

mapfile -t IPS < "$STATE_DIR/public-ips"
AGENT_COUNT=${#IPS[@]}
echo "=== Setting up $AGENT_COUNT agents (SUT=$SUT_IP) ==="

# Wait for SSH readiness
echo "Waiting for SSH on all agents..."
for ip in "${IPS[@]}"; do
    for attempt in $(seq 1 12); do
        if ssh $SSH_OPTS -i "$KEY" "ubuntu@$ip" "true" 2>/dev/null; then
            break
        fi
        [[ $attempt -eq 12 ]] && { echo "WARN: SSH timeout for $ip"; continue 2; }
        sleep 5
    done
done
echo "  All agents reachable"

# Setup script to run on each agent
cat > "$STATE_DIR/agent-setup.sh" << 'SETUP_EOF'
#!/bin/bash
set -eu
SUT_IP="$1"

# Install vegeta
if ! command -v vegeta &>/dev/null; then
    echo "Installing vegeta..."
    curl -sL https://github.com/tsenart/vegeta/releases/download/v12.12.0/vegeta_12.12.0_linux_amd64.tar.gz \
        | sudo tar -xz -C /usr/local/bin vegeta
fi

# Configure DNS to point at SUT (so upstream.test resolves to proxy)
echo "Configuring DNS..."
sudo systemctl stop systemd-resolved 2>/dev/null || true
sudo systemctl disable systemd-resolved 2>/dev/null || true
sudo rm -f /etc/resolv.conf
printf "nameserver %s\nnameserver 8.8.8.8\n" "$SUT_IP" | sudo tee /etc/resolv.conf > /dev/null

# Create results directory
mkdir -p ~/perf-results

echo "Agent ready: $(hostname -I | awk '{print $1}')"
SETUP_EOF
chmod +x "$STATE_DIR/agent-setup.sh"

# Run setup in parallel on all agents
echo "Running setup on all agents in parallel..."
PIDS=()
for ip in "${IPS[@]}"; do
    (
        scp $SSH_OPTS -i "$KEY" "$STATE_DIR/agent-setup.sh" "ubuntu@$ip:/tmp/agent-setup.sh" 2>/dev/null
        ssh $SSH_OPTS -i "$KEY" "ubuntu@$ip" "bash /tmp/agent-setup.sh $SUT_IP" 2>/dev/null
        echo "  [$ip] setup complete"
    ) &
    PIDS+=($!)
done

FAILED=0
for pid in "${PIDS[@]}"; do
    wait "$pid" || ((FAILED++))
done

if [[ $FAILED -gt 0 ]]; then
    echo "WARNING: $FAILED agent(s) failed setup"
else
    echo "All $AGENT_COUNT agents configured"
fi

echo ""
echo "=== Fleet $TAG agents ready ==="
echo "  Each agent has: vegeta, DNS -> $SUT_IP"
echo ""
echo "Next: ./fleet-attack.sh $TAG <scenario> <total-rate>"
