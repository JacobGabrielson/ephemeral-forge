#!/usr/bin/env bash
# Spin up a cheap GPU spot instance via CreateFleet, run TGI,
# test it, tear it down. Uses Karpenter-style fleet patterns
# from iron-proxy perfspace.
set -euo pipefail

AWS="aws --profile spot-builder"
REGION="us-east-2"
TAG="tgi-test-$(date +%s)"

# GPU instance types to offer — wide pool for spot capacity.
# All have at least 16GB VRAM, enough for small LLMs.
GPU_TYPES=(
    g4dn.xlarge    # T4 16GB, cheapest
    g4dn.2xlarge   # T4 16GB, more CPU/RAM
    g5.xlarge      # A10G 24GB
    g5.2xlarge     # A10G 24GB, more CPU/RAM
    g6.xlarge      # L4 24GB
)

echo "=== TGI GPU Test: $TAG ==="
echo "  Region: $REGION"
echo "  Strategy: CreateFleet, price-capacity-optimized, spot-only"

# ── AMI (Deep Learning Base — has NVIDIA drivers + Docker) ──
echo "Finding Deep Learning AMI..."
AMI_ID=$($AWS ec2 describe-images --region "$REGION" \
    --owners amazon \
    --filters \
        "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*" \
        "Name=state,Values=available" \
    --query 'sort_by(Images,&CreationDate)[-1].ImageId' \
    --output text)
echo "  AMI: $AMI_ID"

# ── VPC / All subnets (let CreateFleet pick AZ) ─────────────
VPC_ID=$($AWS ec2 describe-vpcs --region "$REGION" \
    --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text)

SUBNET_IDS=$($AWS ec2 describe-subnets --region "$REGION" \
    --filters "Name=vpc-id,Values=$VPC_ID" \
    --query 'Subnets[].SubnetId' --output text)
echo "  VPC: $VPC_ID"
echo "  Subnets: $SUBNET_IDS"

# ── Key pair ─────────────────────────────────────────────────
KEY_NAME="tgi-$TAG"
KEY_FILE="/tmp/$KEY_NAME.pem"
echo "Creating key pair: $KEY_NAME"
$AWS ec2 create-key-pair --region "$REGION" \
    --key-name "$KEY_NAME" \
    --query 'KeyMaterial' --output text > "$KEY_FILE"
chmod 600 "$KEY_FILE"

# ── Security group ───────────────────────────────────────────
SG_NAME="tgi-$TAG"
SG_ID=$($AWS ec2 create-security-group --region "$REGION" \
    --vpc-id "$VPC_ID" \
    --group-name "$SG_NAME" \
    --description "TGI GPU test $TAG" \
    --query 'GroupId' --output text)

MY_IP=$(curl -s https://checkip.amazonaws.com)/32
$AWS ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$SG_ID" \
    --protocol tcp --port 22 --cidr "$MY_IP" > /dev/null
$AWS ec2 authorize-security-group-ingress --region "$REGION" \
    --group-id "$SG_ID" \
    --protocol tcp --port 8080 --cidr "$MY_IP" > /dev/null
echo "  SG: $SG_ID"

# ── Cleanup function ────────────────────────────────────────
INSTANCE_ID=""
LT_ID=""
cleanup() {
    echo ""
    echo "=== Cleaning up ==="
    if [[ -n "$INSTANCE_ID" ]]; then
        echo "  Terminating $INSTANCE_ID..."
        $AWS ec2 terminate-instances --region "$REGION" \
            --instance-ids "$INSTANCE_ID" > /dev/null 2>&1 || true
        $AWS ec2 wait instance-terminated --region "$REGION" \
            --instance-ids "$INSTANCE_ID" 2>/dev/null || true
    fi
    if [[ -n "$LT_ID" ]]; then
        echo "  Deleting launch template..."
        $AWS ec2 delete-launch-template --region "$REGION" \
            --launch-template-id "$LT_ID" > /dev/null 2>&1 || true
    fi
    echo "  Deleting key pair..."
    $AWS ec2 delete-key-pair --region "$REGION" \
        --key-name "$KEY_NAME" 2>/dev/null || true
    echo "  Deleting security group..."
    sleep 5
    $AWS ec2 delete-security-group --region "$REGION" \
        --group-id "$SG_ID" 2>/dev/null || true
    rm -f "$KEY_FILE"
    echo "  Done."
}
trap cleanup EXIT

# ── Launch template ──────────────────────────────────────────
LT_NAME="tgi-$TAG"
echo "Creating launch template: $LT_NAME"
LT_ID=$($AWS ec2 create-launch-template --region "$REGION" \
    --launch-template-name "$LT_NAME" \
    --launch-template-data "{
        \"ImageId\": \"$AMI_ID\",
        \"KeyName\": \"$KEY_NAME\",
        \"SecurityGroupIds\": [\"$SG_ID\"],
        \"BlockDeviceMappings\": [{
            \"DeviceName\": \"/dev/sda1\",
            \"Ebs\": {\"VolumeSize\": 80, \"VolumeType\": \"gp3\"}
        }],
        \"TagSpecifications\": [{
            \"ResourceType\": \"instance\",
            \"Tags\": [
                {\"Key\": \"Name\", \"Value\": \"tgi-gpu-test\"},
                {\"Key\": \"Purpose\", \"Value\": \"tgi-gpu-test\"},
                {\"Key\": \"RunID\", \"Value\": \"$TAG\"}
            ]
        }]
    }" \
    --query 'LaunchTemplate.LaunchTemplateId' --output text)
echo "  LT: $LT_ID"

# ── Build CreateFleet overrides (Karpenter pattern) ──────────
# GPU types × all subnets — EC2 picks best (type, AZ) combo.
OVERRIDES_JSON="["
first=true
for itype in "${GPU_TYPES[@]}"; do
    for subnet in $SUBNET_IDS; do
        $first && first=false || OVERRIDES_JSON+=","
        OVERRIDES_JSON+="{\"InstanceType\":\"$itype\",\"SubnetId\":\"$subnet\"}"
    done
done
OVERRIDES_JSON+="]"

# ── CreateFleet (spot-only, price-capacity-optimized) ────────
FLEET_REQ="/tmp/tgi-fleet-$TAG.json"
cat > "$FLEET_REQ" <<EOF
{
    "Type": "instant",
    "TargetCapacitySpecification": {
        "TotalTargetCapacity": 1,
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
    --cli-input-json "file://$FLEET_REQ" \
    --output json)

# Extract instance ID
INSTANCE_ID=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
ids = []
for inst in data.get('Instances', []):
    ids.extend(inst.get('InstanceIds', []))
errs = [f'{e[\"ErrorCode\"]}: {e[\"ErrorMessage\"]}' for e in data.get('Errors', [])]
if errs:
    print('Fleet errors: ' + '; '.join(errs), file=sys.stderr)
if ids:
    print(ids[0])
else:
    sys.exit(1)
" <<< "$FLEET_RESULT")

echo "  Instance: $INSTANCE_ID"

# ── Wait for running + get details ──────────────────────────
echo "Waiting for instance..."
$AWS ec2 wait instance-running --region "$REGION" \
    --instance-ids "$INSTANCE_ID"

INSTANCE_INFO=$($AWS ec2 describe-instances --region "$REGION" \
    --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].[PublicIpAddress,InstanceType,Placement.AvailabilityZone]' \
    --output text)
PUBLIC_IP=$(echo "$INSTANCE_INFO" | awk '{print $1}')
GOT_TYPE=$(echo "$INSTANCE_INFO" | awk '{print $2}')
GOT_AZ=$(echo "$INSTANCE_INFO" | awk '{print $3}')

echo "  Got: $GOT_TYPE in $GOT_AZ"
echo "  Public IP: $PUBLIC_IP"

# ── Wait for SSH ────────────────────────────────────────────
SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i $KEY_FILE ubuntu@$PUBLIC_IP"
echo "Waiting for SSH..."
for i in $(seq 1 30); do
    $SSH true 2>/dev/null && break
    sleep 5
done

echo "=== Instance ready ==="

# ── Verify GPU ──────────────────────────────────────────────
echo ""
echo "--- GPU Info ---"
$SSH "nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader" 2>/dev/null

# ── Pull and start TGI ──────────────────────────────────────
echo ""
echo "Pulling TGI image..."
$SSH "sudo docker pull ghcr.io/huggingface/text-generation-inference:latest" 2>&1 | tail -3

HF_TOKEN=$(cat ~/.cache/huggingface/token 2>/dev/null || cat ~/.huggingface/token 2>/dev/null || echo "")

echo "Starting TGI with microsoft/phi-2 on GPU..."
$SSH "sudo docker run -d --name tgi --gpus all -p 8080:80 \
    -e HF_TOKEN=$HF_TOKEN \
    -e HUGGING_FACE_HUB_TOKEN=$HF_TOKEN \
    ghcr.io/huggingface/text-generation-inference:latest \
    --model-id microsoft/phi-2 \
    --max-input-length 512 \
    --max-total-tokens 1024"

echo "Waiting for TGI to load model..."
for i in $(seq 1 60); do
    if $SSH "curl -s http://localhost:8080/health" 2>/dev/null | grep -q "true"; then
        echo "  TGI is healthy! (${i}0s)"
        break
    fi
    if [[ $i -eq 60 ]]; then
        echo "  Timeout. Last logs:"
        $SSH "sudo docker logs tgi 2>&1" | tail -20
        exit 1
    fi
    sleep 10
done

# ── Tests ───────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "  TGI on GPU — Running Tests"
echo "=========================================="

echo ""
echo "--- Test 1: Code generation ---"
$SSH 'curl -s http://localhost:8080/generate \
    -H "Content-Type: application/json" \
    -d "{\"inputs\": \"def merge_sort(arr):\", \"parameters\": {\"max_new_tokens\": 150}}" | python3 -m json.tool' 2>/dev/null

echo ""
echo "--- Test 2: Streaming tokens ---"
$SSH 'curl -sN http://localhost:8080/generate_stream \
    -H "Content-Type: application/json" \
    -d "{\"inputs\": \"Explain distributed consensus in 3 sentences:\", \"parameters\": {\"max_new_tokens\": 100}}"' 2>/dev/null
echo ""

echo ""
echo "--- Test 3: Throughput (10 concurrent requests) ---"
$SSH 'for i in $(seq 1 10); do
    curl -s -o /dev/null -w "%{time_total}\n" http://localhost:8080/generate \
        -H "Content-Type: application/json" \
        -d "{\"inputs\": \"Write a haiku about number $i\", \"parameters\": {\"max_new_tokens\": 30}}" &
done
wait' 2>/dev/null | sort -n | awk '
    {a[NR]=$1; s+=$1}
    END {
        printf "  Requests: %d\n", NR
        printf "  Min:      %.3fs\n", a[1]
        printf "  Median:   %.3fs\n", a[int(NR/2)+1]
        printf "  Max:      %.3fs\n", a[NR]
        printf "  Mean:     %.3fs\n", s/NR
    }'

echo ""
echo "--- Test 4: Model info ---"
$SSH 'curl -s http://localhost:8080/info | python3 -m json.tool' 2>/dev/null

echo ""
echo "=========================================="
echo "  Tests complete. Total cost: ~\$0.02"
echo "=========================================="
# cleanup trap tears everything down
