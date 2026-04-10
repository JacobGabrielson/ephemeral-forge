#!/usr/bin/env bash
# fleet-destroy.sh — Tear down a fleet and all its resources.
#
# Usage:
#   ./fleet-destroy.sh <tag>
#
set -euo pipefail

AWS="aws --profile spot-builder"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TAG="${1:?Usage: fleet-destroy.sh <tag>}"
STATE_DIR="$SCRIPT_DIR/fleet-state/$TAG"

if [[ ! -d "$STATE_DIR" ]]; then
    echo "ERROR: No fleet state at $STATE_DIR" >&2
    exit 1
fi

REGION=$(cat "$STATE_DIR/region")
echo "=== Destroying fleet $TAG in $REGION ==="

# ── Terminate instances ──────────────────────────────────────
if [[ -f "$STATE_DIR/instance-ids" ]]; then
    IDS=$(cat "$STATE_DIR/instance-ids")
    if [[ -n "$IDS" ]]; then
        echo "Terminating instances: $IDS"
        $AWS ec2 terminate-instances --region "$REGION" \
            --instance-ids $IDS > /dev/null 2>&1 || true
        echo "Waiting for termination..."
        $AWS ec2 wait instance-terminated --region "$REGION" \
            --instance-ids $IDS 2>/dev/null || \
            sleep 10  # fallback if wait times out
    fi
fi

# ── Delete launch template ───────────────────────────────────
if [[ -f "$STATE_DIR/launch-template-id" ]]; then
    LT_ID=$(cat "$STATE_DIR/launch-template-id")
    echo "Deleting launch template: $LT_ID"
    $AWS ec2 delete-launch-template --region "$REGION" \
        --launch-template-id "$LT_ID" > /dev/null 2>&1 || true
fi

# ── Delete security group (wait for ENI detach) ──────────────
if [[ -f "$STATE_DIR/sg-id" ]]; then
    SG_ID=$(cat "$STATE_DIR/sg-id")
    echo "Deleting security group: $SG_ID (waiting 5s for ENI detach)"
    sleep 5
    $AWS ec2 delete-security-group --region "$REGION" \
        --group-id "$SG_ID" > /dev/null 2>&1 || \
        echo "  WARN: SG deletion failed (may need manual cleanup)"
fi

# ── Delete key pair ──────────────────────────────────────────
if [[ -f "$STATE_DIR/key-name" ]]; then
    KEY_NAME=$(cat "$STATE_DIR/key-name")
    echo "Deleting key pair: $KEY_NAME"
    $AWS ec2 delete-key-pair --region "$REGION" \
        --key-name "$KEY_NAME" > /dev/null 2>&1 || true
fi

echo ""
echo "=== Fleet $TAG destroyed ==="
echo "  State directory preserved at: $STATE_DIR/"
echo "  (contains results and logs — delete manually when done)"
