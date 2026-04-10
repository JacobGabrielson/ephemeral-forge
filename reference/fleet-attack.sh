#!/usr/bin/env bash
# fleet-attack.sh — Run a distributed vegeta attack from fleet agents.
#
# Splits the target rate evenly across agents so each agent
# generates load from its own source IP.  Includes a warmup
# phase to avoid cold-start TLS cert generation outliers.
#
# Usage:
#   ./fleet-attack.sh <tag> <scenario> <total-rate> [duration] [ca-cert]
#
# Example:
#   ./fleet-attack.sh fleet-1234 passthrough 1000 30s /tmp/ca.crt
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TAG="${1:?Usage: fleet-attack.sh <tag> <scenario> <total-rate> [duration] [ca-cert]}"
SCENARIO="${2:?Usage: fleet-attack.sh <tag> <scenario> <total-rate> [duration] [ca-cert]}"
TOTAL_RATE="${3:?Usage: fleet-attack.sh <tag> <scenario> <total-rate> [duration] [ca-cert]}"
DURATION="${4:-30s}"
CA_CERT="${5:-}"

STATE_DIR="$SCRIPT_DIR/fleet-state/$TAG"
KEY="$STATE_DIR/key.pem"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

# Target files live alongside this script (copied from test/perf/targets/)
TARGETS_DIR="$SCRIPT_DIR/../test/perf/targets"
if [[ ! -f "$TARGETS_DIR/$SCENARIO.txt" ]]; then
    # Also check local copy
    TARGETS_DIR="$SCRIPT_DIR/targets"
fi
if [[ ! -f "$TARGETS_DIR/$SCENARIO.txt" ]]; then
    echo "ERROR: Target file not found: $SCENARIO.txt" >&2
    echo "  Looked in ../test/perf/targets/ and ./targets/" >&2
    exit 1
fi

mapfile -t IPS < "$STATE_DIR/public-ips"
AGENT_COUNT=${#IPS[@]}
PER_AGENT_RATE=$(( TOTAL_RATE / AGENT_COUNT ))
REMAINDER=$(( TOTAL_RATE % AGENT_COUNT ))

echo "=== Fleet attack: $SCENARIO at ${TOTAL_RATE}/s ==="
echo "  Agents: $AGENT_COUNT"
echo "  Per-agent rate: ${PER_AGENT_RATE}/s (first agent +${REMAINDER} remainder)"
echo "  Duration: $DURATION"
echo "  Warmup: 2s at 1/s per agent (TLS cert cache priming)"
echo ""

# Prepare results directory
RESULT_DIR="$STATE_DIR/results/${SCENARIO}_${TOTAL_RATE}rps"
mkdir -p "$RESULT_DIR"

# Distribute target file and CA cert to all agents
echo "Distributing target files..."
for ip in "${IPS[@]}"; do
    scp $SSH_OPTS -i "$KEY" "$TARGETS_DIR/$SCENARIO.txt" \
        "ubuntu@$ip:/tmp/target.txt" 2>/dev/null &
done
wait

# Distribute CA cert if provided
CA_OPT=""
if [[ -n "$CA_CERT" && -f "$CA_CERT" ]]; then
    echo "Distributing CA certificate..."
    for ip in "${IPS[@]}"; do
        scp $SSH_OPTS -i "$KEY" "$CA_CERT" \
            "ubuntu@$ip:/tmp/ca.crt" 2>/dev/null &
    done
    wait
    CA_OPT="-root-certs=/tmp/ca.crt"
fi

# ── Warmup phase ─────────────────────────────────────────────
# Send a few requests per agent to prime the TLS cert cache on
# the proxy.  This avoids the ~22ms cold-start outlier from
# first-request ECDSA cert generation.
echo "Running warmup (2s at 1/s per agent)..."
for ip in "${IPS[@]}"; do
    ssh $SSH_OPTS -i "$KEY" "ubuntu@$ip" \
        "vegeta attack -rate=1/s -duration=2s -targets=/tmp/target.txt $CA_OPT -timeout=5s > /dev/null 2>&1" &
done
wait
echo "  Warmup complete"
sleep 1

# ── Main attack ──────────────────────────────────────────────
echo "Launching attack..."
PIDS=()
idx=0
for ip in "${IPS[@]}"; do
    rate=$PER_AGENT_RATE
    # First agent gets the remainder
    if [[ $idx -eq 0 ]]; then
        rate=$(( PER_AGENT_RATE + REMAINDER ))
    fi

    (
        ssh $SSH_OPTS -i "$KEY" "ubuntu@$ip" \
            "vegeta attack \
                -rate=${rate}/s \
                -duration=${DURATION} \
                -targets=/tmp/target.txt \
                $CA_OPT \
                -timeout=10s \
                > ~/perf-results/result.bin 2>/dev/null && \
             vegeta report -type=text < ~/perf-results/result.bin > ~/perf-results/result.txt"
        echo "  [$ip] attack complete (${rate}/s)"
    ) &
    PIDS+=($!)
    ((idx++))
done

FAILED=0
for pid in "${PIDS[@]}"; do
    wait "$pid" || ((FAILED++))
done

if [[ $FAILED -gt 0 ]]; then
    echo "WARNING: $FAILED agent(s) had errors"
fi

# ── Collect results ──────────────────────────────────────────
echo ""
echo "Collecting results..."
idx=0
for ip in "${IPS[@]}"; do
    scp $SSH_OPTS -i "$KEY" \
        "ubuntu@$ip:~/perf-results/result.bin" \
        "$RESULT_DIR/agent-${idx}.bin" 2>/dev/null || true
    scp $SSH_OPTS -i "$KEY" \
        "ubuntu@$ip:~/perf-results/result.txt" \
        "$RESULT_DIR/agent-${idx}.txt" 2>/dev/null || true
    ((idx++))
done

# ── Per-agent summary ────────────────────────────────────────
echo ""
echo "=== Per-Agent Results ==="
printf "  %-16s %-8s %-10s %-10s %-10s %-10s %-8s\n" \
    "Agent" "Rate" "p50" "p95" "p99" "Max" "Success"
printf "  %-16s %-8s %-10s %-10s %-10s %-10s %-8s\n" \
    "────────────────" "────────" "──────────" "──────────" \
    "──────────" "──────────" "────────"

for f in "$RESULT_DIR"/agent-*.txt; do
    [[ -f "$f" ]] || continue
    agent=$(basename "$f" .txt)
    ip="${IPS[${agent#agent-}]}"

    lat=$(grep "^Latencies" "$f" | sed 's/.*\]\s*//')
    suc=$(grep "^Success" "$f" | sed 's/.*\]\s*//')
    rate=$(grep "^Requests" "$f" | sed 's/.*\]\s*//' | cut -d, -f2 | tr -d ' ')

    # Parse latencies: min, mean, p50, p90, p95, p99, max
    IFS=', ' read -ra LATS <<< "$lat"
    p50="${LATS[2]:-?}"
    p95="${LATS[4]:-?}"
    p99="${LATS[5]:-?}"
    max="${LATS[6]:-?}"

    printf "  %-16s %-8s %-10s %-10s %-10s %-10s %-8s\n" \
        "$ip" "${rate}/s" "$p50" "$p95" "$p99" "$max" "$suc"
done

echo ""
echo "Raw results: $RESULT_DIR/"
echo ""
echo "To merge all agents for aggregate analysis:"
echo "  cat $RESULT_DIR/agent-*.bin | vegeta report -type=text"
