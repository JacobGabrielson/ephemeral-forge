#!/usr/bin/env bash
# fleet-launch.sh — Launch a fleet of small spot instances for
# multi-agent load testing.  Each instance represents a distinct
# source IP, useful for testing per-IP rate limiting.
#
# Uses EC2 CreateFleet with a launch template + multiple instance
# type overrides and price-capacity-optimized allocation
# (Karpenter-style).  Spot-only — no on-demand fallback.
#
# Usage:
#   ./fleet-launch.sh [options]
#
# Options:
#   --count N          Number of agents (default: 5)
#   --region REGION    Force region (default: auto-select cheapest)
#   --tag TAG          Run tag (default: fleet-<timestamp>)
#
# Output:
#   Writes fleet state to perfspace/fleet-state/<tag>/
#   including instance IDs, IPs, and SSH key.
#
set -euo pipefail

AWS="aws --profile spot-builder"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Defaults ─────────────────────────────────────────────────
AGENT_COUNT=5
REGION=""
TAG="fleet-$(date +%s)"

# Instance types to offer — wide pool for spot capacity.
# Small/cheap: we need distinct IPs, not CPU.
INSTANCE_TYPES=(
    t3.small
    t3a.small
    t3.micro
    t3a.micro
    m6i.large
    m5.large
    c6i.large
    c5.large
)

# Regions with default VPCs
CANDIDATE_REGIONS=(us-west-2 us-east-2)

# ── Parse args ───────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --count)    AGENT_COUNT="$2"; shift 2 ;;
        --region)   REGION="$2"; shift 2 ;;
        --tag)      TAG="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ── State directory ──────────────────────────────────────────
STATE_DIR="$SCRIPT_DIR/fleet-state/$TAG"
mkdir -p "$STATE_DIR"

echo "=== Fleet launcher: $AGENT_COUNT agents, tag=$TAG ==="

# ── Cleanup trap ─────────────────────────────────────────────
CLEANUP_LT=""
CLEANUP_SG=""
CLEANUP_KEY=""
CLEANUP_IDS=""
CLEANUP_REGION=""

cleanup_on_error() {
    echo ""
    echo "ERROR — cleaning up partial resources..."
    if [[ -n "$CLEANUP_IDS" && -n "$CLEANUP_REGION" ]]; then
        echo "  Terminating instances..."
        $AWS ec2 terminate-instances --region "$CLEANUP_REGION" \
            --instance-ids $CLEANUP_IDS > /dev/null 2>&1 || true
    fi
    if [[ -n "$CLEANUP_LT" && -n "$CLEANUP_REGION" ]]; then
        echo "  Deleting launch template..."
        $AWS ec2 delete-launch-template --region "$CLEANUP_REGION" \
            --launch-template-id "$CLEANUP_LT" > /dev/null 2>&1 || true
    fi
    # Don't delete SG/key on error — fleet-destroy handles that
}
# Only trap on genuine errors, not normal exit
trap 'if [[ $? -ne 0 ]]; then cleanup_on_error; fi' EXIT

# ── Region selection ─────────────────────────────────────────
probe_spot_price() {
    local region="$1"
    local best_price="" best_az=""
    for itype in t3.small t3a.small t3.micro t3a.micro; do
        local result
        result=$($AWS ec2 describe-spot-price-history \
            --region "$region" \
            --instance-types "$itype" \
            --product-descriptions "Linux/UNIX" \
            --start-time "$(date -u +%Y-%m-%dT%H:%M:%S)" \
            --query 'SpotPriceHistory[0].[SpotPrice,AvailabilityZone]' \
            --output text 2>/dev/null) || continue
        local p=$(echo "$result" | awk '{print $1}')
        local az=$(echo "$result" | awk '{print $2}')
        [[ -z "$p" || "$p" == "None" ]] && continue
        if [[ -z "$best_price" ]] || (( $(echo "$p < $best_price" | bc -l) )); then
            best_price="$p"
            best_az="$az"
        fi
    done
    [[ -n "$best_price" ]] && echo "$best_price $best_az"
}

if [[ -z "$REGION" ]]; then
    echo "Probing spot prices..."
    BEST_PRICE="" BEST_AZ="" BEST_REGION=""
    for r in "${CANDIDATE_REGIONS[@]}"; do
        result=$(probe_spot_price "$r")
        if [[ -n "$result" ]]; then
            p=$(echo "$result" | awk '{print $1}')
            az=$(echo "$result" | awk '{print $2}')
            echo "  $r: \$$p ($az)"
            if [[ -z "$BEST_PRICE" ]] || (( $(echo "$p < $BEST_PRICE" | bc -l) )); then
                BEST_PRICE="$p" BEST_AZ="$az" BEST_REGION="$r"
            fi
        fi
    done
    [[ -z "$BEST_REGION" ]] && { echo "ERROR: No spot capacity" >&2; exit 1; }
    REGION="$BEST_REGION"
    echo "Selected: $REGION ($BEST_AZ) at \$$BEST_PRICE/hr"
else
    result=$(probe_spot_price "$REGION")
    BEST_AZ=$(echo "$result" | awk '{print $2}')
    echo "Using $REGION ($BEST_AZ)"
fi
CLEANUP_REGION="$REGION"
echo "$REGION" > "$STATE_DIR/region"
echo "$BEST_AZ" > "$STATE_DIR/az"

# ── AMI ──────────────────────────────────────────────────────
echo "Finding Ubuntu 24.04 AMI..."
AMI_ID=$($AWS ec2 describe-images --region "$REGION" \
    --owners 099720109477 \
    --filters \
        "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
        "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
    --output text)
echo "  AMI: $AMI_ID"

# ── VPC / Subnet ─────────────────────────────────────────────
VPC_ID=$($AWS ec2 describe-vpcs --region "$REGION" \
    --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)

SUBNET_ID=$($AWS ec2 describe-subnets --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" \
              "Name=availability-zone,Values=$BEST_AZ" \
    --query 'Subnets[0].SubnetId' --output text)
echo "  VPC: $VPC_ID  Subnet: $SUBNET_ID"

# ── Key pair ─────────────────────────────────────────────────
KEY_NAME="perf-fleet-$TAG"
echo "Creating key pair: $KEY_NAME"
$AWS ec2 create-key-pair --region "$REGION" \
    --key-name "$KEY_NAME" \
    --query 'KeyMaterial' --output text \
    > "$STATE_DIR/key.pem"
chmod 600 "$STATE_DIR/key.pem"
echo "$KEY_NAME" > "$STATE_DIR/key-name"
CLEANUP_KEY="$KEY_NAME"

# ── Security group ───────────────────────────────────────────
SG_NAME="perf-fleet-$TAG"
echo "Creating security group: $SG_NAME"
SG_ID=$($AWS ec2 create-security-group --region "$REGION" \
    --vpc-id "$VPC_ID" \
    --group-name "$SG_NAME" \
    --description "perf fleet $TAG" \
    --query 'GroupId' --output text)
echo "$SG_ID" > "$STATE_DIR/sg-id"
CLEANUP_SG="$SG_ID"

MY_IP=$(curl -s https://checkip.amazonaws.com)/32
$AWS ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "$MY_IP" > /dev/null

VPC_CIDR=$($AWS ec2 describe-vpcs --region "$REGION" \
    --vpc-ids "$VPC_ID" \
    --query 'Vpcs[0].CidrBlock' --output text)
$AWS ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$SG_ID" \
    --protocol -1 --cidr "$VPC_CIDR" > /dev/null
echo "  SG: $SG_ID (SSH from $MY_IP, all within $VPC_CIDR)"

# ── Launch template ──────────────────────────────────────────
LT_NAME="perf-fleet-$TAG"
echo "Creating launch template: $LT_NAME"

LT_ID=$($AWS ec2 create-launch-template --region "$REGION" \
    --launch-template-name "$LT_NAME" \
    --launch-template-data "{
        \"ImageId\": \"$AMI_ID\",
        \"KeyName\": \"$KEY_NAME\",
        \"SecurityGroupIds\": [\"$SG_ID\"],
        \"BlockDeviceMappings\": [{
            \"DeviceName\": \"/dev/sda1\",
            \"Ebs\": {\"VolumeSize\": 8, \"VolumeType\": \"gp3\"}
        }],
        \"TagSpecifications\": [{
            \"ResourceType\": \"instance\",
            \"Tags\": [
                {\"Key\": \"Name\", \"Value\": \"perf-fleet-agent\"},
                {\"Key\": \"Purpose\", \"Value\": \"perf-lab\"},
                {\"Key\": \"RunID\", \"Value\": \"$TAG\"}
            ]
        }]
    }" \
    --query 'LaunchTemplate.LaunchTemplateId' --output text)
echo "  LT: $LT_ID"
echo "$LT_ID" > "$STATE_DIR/launch-template-id"
CLEANUP_LT="$LT_ID"

# ── Build CreateFleet overrides (Karpenter pattern) ──────────
# Multiple instance types × single subnet — EC2 picks best.
OVERRIDES_JSON="["
first=true
for itype in "${INSTANCE_TYPES[@]}"; do
    $first && first=false || OVERRIDES_JSON+=","
    OVERRIDES_JSON+="{\"InstanceType\":\"$itype\",\"SubnetId\":\"$SUBNET_ID\"}"
done
OVERRIDES_JSON+="]"

# ── CreateFleet request ──────────────────────────────────────
cat > "$STATE_DIR/fleet-request.json" <<EOF
{
    "Type": "instant",
    "TargetCapacitySpecification": {
        "TotalTargetCapacity": $AGENT_COUNT,
        "DefaultTargetCapacityType": "spot"
    },
    "SpotOptions": {
        "AllocationStrategy": "price-capacity-optimized"
    },
    "LaunchTemplateConfigs": [
        {
            "LaunchTemplateSpecification": {
                "LaunchTemplateId": "$LT_ID",
                "Version": "\$Default"
            },
            "Overrides": $OVERRIDES_JSON
        }
    ]
}
EOF

echo "Calling CreateFleet (spot-only, price-capacity-optimized)..."
FLEET_RESULT=$($AWS ec2 create-fleet --region "$REGION" \
    --cli-input-json "file://$STATE_DIR/fleet-request.json" \
    --output json)
echo "$FLEET_RESULT" > "$STATE_DIR/fleet-response.json"

# Extract IDs and errors
read -r INSTANCE_IDS FLEET_ERRORS < <(python3 -c "
import json, sys
data = json.load(sys.stdin)
ids = []
for inst in data.get('Instances', []):
    ids.extend(inst.get('InstanceIds', []))
errs = []
for e in data.get('Errors', []):
    errs.append(f'{e.get(\"ErrorCode\")}: {e.get(\"ErrorMessage\")}')
print(' '.join(ids), '|||', '; '.join(errs))
" <<< "$FLEET_RESULT")

FLEET_ERRORS="${FLEET_ERRORS#*||| }"
INSTANCE_IDS="${INSTANCE_IDS%% |||*}"
INSTANCE_IDS=$(echo "$INSTANCE_IDS" | xargs)  # trim

if [[ -n "$FLEET_ERRORS" && "$FLEET_ERRORS" != " " ]]; then
    echo "  Fleet warnings: $FLEET_ERRORS"
fi

LAUNCHED=$(echo "$INSTANCE_IDS" | wc -w)
echo "Launched $LAUNCHED / $AGENT_COUNT instances"

if [[ $LAUNCHED -eq 0 ]]; then
    echo "ERROR: No instances launched" >&2
    cat "$STATE_DIR/fleet-response.json" >&2
    exit 1
fi

echo "$INSTANCE_IDS" > "$STATE_DIR/instance-ids"
CLEANUP_IDS="$INSTANCE_IDS"

# ── Wait for running + collect IPs ───────────────────────────
echo "Waiting for instances..."
$AWS ec2 wait instance-running --region "$REGION" \
    --instance-ids $INSTANCE_IDS

$AWS ec2 describe-instances --region "$REGION" \
    --instance-ids $INSTANCE_IDS \
    --query 'Reservations[].Instances[].[InstanceId,InstanceType,PrivateIpAddress,PublicIpAddress]' \
    --output text > "$STATE_DIR/instance-table.txt"

python3 "$SCRIPT_DIR/fleet-print-instances.py" "$STATE_DIR"

echo ""
echo "=== Fleet $TAG ready ($LAUNCHED agents) ==="
echo "  State:   $STATE_DIR/"
echo "  SSH:     ssh -i $STATE_DIR/key.pem ubuntu@<public-ip>"
echo "  IPs:     $STATE_DIR/public-ips"
echo ""
echo "Next:"
echo "  ./fleet-setup.sh $TAG <sut-ip>    # install vegeta, configure DNS"
echo "  ./fleet-attack.sh $TAG <scenario> <rate>"
echo "  ./fleet-destroy.sh $TAG"
