#!/usr/bin/env python3
"""GCP integration test: Launch 2 Spot VMs and run vegeta load test.

Usage:
    source .venv/bin/activate
    python tests/gcp_vegeta_test.py

Prerequisites:
    gcloud auth application-default login --project <your-project>
"""

from __future__ import annotations

import logging
import sys
import time
import tempfile
import os

from ephemeral_forge import fleet
from ephemeral_forge.config import load_config
from ephemeral_forge.provider import FleetResult
from ephemeral_forge.ssh import run_command, ssh_connect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
)
log = logging.getLogger(__name__)

# GCP small instance types for cheap testing
WEBSERVER_TYPES = ["e2-micro", "e2-small", "e2-medium"]
LOADGEN_TYPES = ["e2-micro", "e2-small"]

SUT_COUNT = 1
LOADGEN_COUNT = 1
VEGETA_DURATION = "30s"
VEGETA_RATE = 1000


def wait_for_ssh(result: FleetResult, ssh_user: str) -> None:
    """Wait until all instances accept SSH."""
    log.info("Waiting for SSH on %d instances...", len(result.instances))
    for inst in result.instances:
        if inst.public_ip:
            client = ssh_connect(inst.public_ip, ssh_user, result.private_key_pem)
            client.close()
            log.info("  %s (%s) — SSH OK", inst.id, inst.public_ip)


def setup_webserver(result: FleetResult, ssh_user: str) -> tuple[str, str]:
    """Install and start nginx on the webserver. Returns (public_ip, internal_ip)."""
    inst = result.instances[0]
    ip = inst.public_ip
    internal_ip = inst.private_ip
    assert ip, "Webserver has no public IP"
    assert internal_ip, "Webserver has no internal IP"

    log.info("Setting up webserver at %s...", ip)
    client = ssh_connect(ip, ssh_user, result.private_key_pem)

    cmds = [
        "sudo apt-get update -qq",
        "sudo apt-get install -y -qq nginx",
        "sudo systemctl start nginx",
        "sudo systemctl enable nginx",
        "curl -s http://localhost/ | head -1",
    ]
    for cmd in cmds:
        log.info("  SUT> %s", cmd)
        stdout, stderr, rc = run_command(client, cmd, timeout=120)
        if rc != 0:
            log.error("  SUT command failed (rc=%d): %s", rc, stderr)
            raise RuntimeError(f"Setup failed: {stderr}")
    client.close()

    log.info("Webserver ready at http://%s/ (internal: %s)", ip, internal_ip)
    return ip, internal_ip


def setup_loadgen(result: FleetResult, ssh_user: str) -> None:
    """Install vegeta on the load generator VM."""
    inst = result.instances[0]
    ip = inst.public_ip
    assert ip, "Loadgen has no public IP"

    log.info("Setting up vegeta on %s...", ip)
    client = ssh_connect(ip, ssh_user, result.private_key_pem)

    # Install vegeta from GitHub release
    cmds = [
        "sudo apt-get update -qq",
        "sudo apt-get install -y -qq wget",
        "wget -q https://github.com/tsenart/vegeta/releases/download/v12.12.0/vegeta_12.12.0_linux_amd64.tar.gz -O /tmp/vegeta.tar.gz",
        "tar -xzf /tmp/vegeta.tar.gz -C /tmp",
        "sudo mv /tmp/vegeta /usr/local/bin/",
        "vegeta --version",
    ]
    for cmd in cmds:
        log.info("  LOADGEN> %s", cmd)
        stdout, stderr, rc = run_command(client, cmd, timeout=120)
        if rc != 0:
            log.error("  Loadgen command failed (rc=%d): %s", rc, stderr)
            raise RuntimeError(f"Setup failed: {stderr}")
    client.close()
    log.info("Vegeta installed on %s", ip)


def run_vegeta_loadtest(
    result: FleetResult,
    ssh_user: str,
    target_ip: str,
) -> str:
    """Run vegeta load test against the target."""
    inst = result.instances[0]
    ip = inst.public_ip
    assert ip, "Loadgen has no public IP"

    log.info("Running vegeta load test from %s against http://%s/", ip, target_ip)
    client = ssh_connect(ip, ssh_user, result.private_key_pem)

    # Create targets file
    targets_content = f"GET http://{target_ip}/\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(targets_content)
        targets_path = f.name

    # Upload targets file
    sftp = client.open_sftp()
    sftp.put(targets_path, "/tmp/targets.txt")
    sftp.close()
    os.unlink(targets_path)

    # Run vegeta attack with report
    vegeta_cmd = (
        f"echo 'GET http://{target_ip}/' | "
        f"vegeta attack -rate={VEGETA_RATE} -duration={VEGETA_DURATION} | "
        f"vegeta report"
    )

    log.info("  Running: %s", vegeta_cmd)
    stdout, stderr, rc = run_command(client, vegeta_cmd, timeout=60)
    client.close()

    if rc != 0:
        log.error("Vegeta failed (rc=%d): %s", rc, stderr)
        raise RuntimeError(f"Load test failed: {stderr}")

    return stdout


def main() -> None:
    config = load_config()
    webserver_result: FleetResult | None = None
    loadgen_result: FleetResult | None = None

    try:
        # Launch webserver
        log.info("=== Launching webserver (1 Spot VM on GCP) ===")
        webserver_result = fleet.launch(
            provider_name="gcp",
            count=SUT_COUNT,
            instance_types=WEBSERVER_TYPES,
            tag=f"webserver-{int(time.time())}",
            config=config,
        )

        ssh_user = config.gcp.ssh_user if config.gcp else "ubuntu"
        wait_for_ssh(webserver_result, ssh_user)
        webserver_public_ip, webserver_internal_ip = setup_webserver(
            webserver_result, ssh_user
        )

        # Launch load generator in same region
        log.info(
            "=== Launching vegeta loadgen (1 Spot VM in %s) ===",
            webserver_result.region,
        )
        loadgen_result = fleet.launch(
            provider_name="gcp",
            count=LOADGEN_COUNT,
            region=webserver_result.region,
            instance_types=LOADGEN_TYPES,
            tag=f"loadgen-{int(time.time())}",
            config=config,
        )

        wait_for_ssh(loadgen_result, ssh_user)
        setup_loadgen(loadgen_result, ssh_user)

        # Run the load test
        log.info(
            "=== Starting vegeta load test (%s @ %s rps) ===",
            VEGETA_DURATION,
            VEGETA_RATE,
        )
        # Use internal IP for load test (firewall allows internal traffic between tagged instances)
        results = run_vegeta_loadtest(loadgen_result, ssh_user, webserver_internal_ip)

        # Print results
        print("\n" + "=" * 60)
        print("VEGETA LOAD TEST RESULTS")
        print("=" * 60)
        print(results)
        print("=" * 60)

    finally:
        # Always tear down
        if loadgen_result:
            log.info("=== Tearing down load generator ===")
            try:
                fleet.destroy(loadgen_result.run_id, config)
            except Exception as e:
                log.error("Loadgen teardown failed: %s", e)

        if webserver_result:
            log.info("=== Tearing down webserver ===")
            try:
                fleet.destroy(webserver_result.run_id, config)
            except Exception as e:
                log.error("Webserver teardown failed: %s", e)


if __name__ == "__main__":
    main()
