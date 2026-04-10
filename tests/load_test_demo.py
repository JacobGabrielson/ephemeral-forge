#!/usr/bin/env python3
"""Demo load test: 1 SUT running nginx, 10 agents blasting it with wrk.

Usage:
    source .venv/bin/activate
    python tests/load_test_demo.py
"""

from __future__ import annotations

import logging
import sys
import time

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

# Small, cheap instance types for load gen — we need IPs, not CPU
LOADGEN_TYPES = ["t3.micro", "t3a.micro", "t3.small", "t3a.small"]
# SUT can be slightly bigger to run nginx
SUT_TYPES = ["t3.small", "t3a.small", "t3.micro", "t3a.micro"]

SUT_COUNT = 1
LOADGEN_COUNT = 10
WRK_DURATION = "30s"
WRK_THREADS = 2
WRK_CONNECTIONS = 100


def wait_for_ssh(result: FleetResult, ssh_user: str) -> None:
    """Wait until all instances accept SSH."""
    log.info("Waiting for SSH on %d instances...", len(result.instances))
    for inst in result.instances:
        if inst.public_ip:
            client = ssh_connect(inst.public_ip, ssh_user, result.private_key_pem)
            client.close()
            log.info("  %s (%s) — SSH OK", inst.id, inst.public_ip)


def setup_sut(result: FleetResult, ssh_user: str) -> str:
    """Install and start nginx on the SUT.  Returns public IP."""
    inst = result.instances[0]
    ip = inst.public_ip
    assert ip, "SUT has no public IP"

    log.info("Setting up SUT at %s...", ip)
    client = ssh_connect(ip, ssh_user, result.private_key_pem)

    cmds = [
        "sudo apt-get update -qq",
        "sudo apt-get install -y -qq nginx",
        "sudo systemctl start nginx",
        "sudo systemctl enable nginx",
        "curl -s http://localhost/ | head -5",
    ]
    for cmd in cmds:
        log.info("  SUT> %s", cmd)
        stdout, stderr, rc = run_command(client, cmd, timeout=120)
        if rc != 0:
            log.error("  SUT command failed (rc=%d): %s", rc, stderr)
    client.close()

    log.info("SUT ready at http://%s/", ip)
    return ip


def run_loadgen(
    result: FleetResult,
    ssh_user: str,
    target_ip: str,
) -> list[str]:
    """Install wrk on all load gen instances and blast the SUT."""
    log.info("Setting up %d load generators...", len(result.instances))

    # Install wrk on all instances
    for inst in result.instances:
        assert inst.public_ip
        client = ssh_connect(inst.public_ip, ssh_user, result.private_key_pem)
        log.info("  Installing wrk on %s...", inst.public_ip)
        run_command(
            client,
            "sudo apt-get update -qq && sudo apt-get install -y -qq wrk",
            timeout=120,
        )
        client.close()

    # Run wrk on all instances concurrently
    log.info(
        "Starting load test: %d agents × %s connections × %s",
        len(result.instances),
        WRK_CONNECTIONS,
        WRK_DURATION,
    )

    import concurrent.futures

    wrk_cmd = (
        f"wrk -t{WRK_THREADS} -c{WRK_CONNECTIONS} -d{WRK_DURATION} http://{target_ip}/"
    )

    def run_wrk_on(inst_ip: str) -> str:
        client = ssh_connect(inst_ip, ssh_user, result.private_key_pem)
        stdout, stderr, rc = run_command(client, wrk_cmd, timeout=120)
        client.close()
        output = stdout.strip() or stderr.strip()
        return f"=== {inst_ip} ===\n{output}"

    outputs: list[str] = []
    ips = [i.public_ip for i in result.instances if i.public_ip]
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(ips)) as pool:
        futures = {pool.submit(run_wrk_on, ip): ip for ip in ips}
        for future in concurrent.futures.as_completed(futures):
            ip = futures[future]
            try:
                outputs.append(future.result())
                log.info("  %s — wrk complete", ip)
            except Exception as e:
                log.error("  %s — wrk failed: %s", ip, e)

    return outputs


def main() -> None:
    config = load_config()
    sut_result: FleetResult | None = None
    loadgen_result: FleetResult | None = None

    try:
        # Launch SUT
        log.info("=== Launching SUT (%d instance) ===", SUT_COUNT)
        sut_result = fleet.launch(
            provider_name="aws",
            count=SUT_COUNT,
            instance_types=SUT_TYPES,
            tag=f"sut-{int(time.time())}",
            config=config,
        )

        ssh_user = config.aws.ssh_user if config.aws else "ubuntu"
        wait_for_ssh(sut_result, ssh_user)
        target_ip = setup_sut(sut_result, ssh_user)

        # Launch load generators in the same region as SUT
        log.info(
            "=== Launching %d load generators in %s ===",
            LOADGEN_COUNT,
            sut_result.region,
        )
        loadgen_result = fleet.launch(
            provider_name="aws",
            count=LOADGEN_COUNT,
            region=sut_result.region,
            instance_types=LOADGEN_TYPES,
            tag=f"loadgen-{int(time.time())}",
            config=config,
        )

        wait_for_ssh(loadgen_result, ssh_user)

        # Run the load test
        log.info("=== Starting load test ===")
        outputs = run_loadgen(loadgen_result, ssh_user, target_ip)

        # Print results
        print("\n" + "=" * 60)
        print("LOAD TEST RESULTS")
        print("=" * 60)
        for output in outputs:
            print(output)
            print()

    finally:
        # Always tear down
        if loadgen_result:
            log.info("=== Tearing down load generators ===")
            try:
                fleet.destroy(loadgen_result.run_id, config)
            except Exception as e:
                log.error("Loadgen teardown failed: %s", e)

        if sut_result:
            log.info("=== Tearing down SUT ===")
            try:
                fleet.destroy(sut_result.run_id, config)
            except Exception as e:
                log.error("SUT teardown failed: %s", e)


if __name__ == "__main__":
    main()
