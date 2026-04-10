"""Cloud-agnostic fleet orchestrator."""

from __future__ import annotations

import json
import logging
import stat
import time
from pathlib import Path

from ephemeral_forge.config import Config, load_config
from ephemeral_forge.history import (
    LaunchRecord,
    get_median_launch_time,
    save_record,
)
from ephemeral_forge.provider import FleetConfig, FleetResult, Instance, SpotPrice
from ephemeral_forge.providers import get_provider

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ephemeral-forge" / "runs"


# ── launch ───────────────────────────────────────────────────


def launch(
    provider_name: str,
    count: int,
    gpu: bool = False,
    region: str | None = None,
    instance_types: list[str] | None = None,
    tag: str | None = None,
    config: Config | None = None,
) -> FleetResult:
    """Launch a fleet.  Returns FleetResult with running
    instances."""
    if config is None:
        config = load_config()

    provider = get_provider(provider_name, config)
    run_id = tag or f"ef-{int(time.time())}"

    pconfig = getattr(config, provider_name)
    if instance_types is None:
        instance_types = pconfig.gpu_instance_types if gpu else pconfig.instance_types
    if not instance_types:
        raise ValueError(f"No instance types configured for {provider_name}")

    # Timing record
    record = LaunchRecord(
        run_id=run_id,
        provider=provider_name,
        region=region or "",
        zone="",
        instance_types=instance_types,
        count_requested=count,
    )
    record.ts_probe_start = time.monotonic()

    # ── region selection ─────────────────────────────────────
    zone: str | None = None
    if region is None:
        logger.info("Probing spot prices across all regions...")
        probe_regions = None if config.probe_all_regions else pconfig.candidate_regions
        prices = provider.probe_spot_prices(instance_types, probe_regions)
        if not prices:
            raise RuntimeError("No spot capacity found in any region")

        scored = _score_prices(prices, provider_name)
        best = scored[0]
        region = best.region
        zone = best.zone
        record.spot_price = best.price_per_hour
        logger.info(
            "Selected %s (%s) — $%.4f/hr",
            region,
            zone,
            best.price_per_hour,
        )

    record.region = region
    record.zone = zone or ""

    # ── build config and launch ──────────────────────────────
    fleet_config = FleetConfig(
        count=count,
        instance_types=instance_types,
        disk_gb=100 if gpu else 8,
        ssh_user=pconfig.ssh_user,
        tags={"Purpose": config.purpose_tag, "RunID": run_id},
        max_spot_price=pconfig.max_spot_price,
    )

    record.ts_api_call = time.monotonic()
    logger.info("Launching %d instances in %s...", count, region)

    result = provider.launch_fleet(fleet_config, run_id, region, zone)
    record.ts_fleet_created = time.monotonic()

    launched = len(result.instances)
    logger.info("Fleet created: %d/%d instances", launched, count)
    if result.errors:
        for err in result.errors:
            logger.warning("Fleet warning: %s", err)

    # ── wait ─────────────────────────────────────────────────
    logger.info("Waiting for instances to be ready...")
    result = provider.wait_until_ready(result)
    record.ts_all_running = time.monotonic()
    record.count_fulfilled = len(result.instances)

    # ── persist ──────────────────────────────────────────────
    save_state(result)
    save_record(record)

    return result


# ── scoring ──────────────────────────────────────────────────


def _score_prices(
    prices: list[SpotPrice],
    provider_name: str,
) -> list[SpotPrice]:
    """Rank spot prices, penalising slow provider/regions using
    historical launch times."""

    def score(p: SpotPrice) -> float:
        median_time = get_median_launch_time(provider_name, p.region)
        if median_time is not None:
            # Treat boot time as wasted cost
            return p.price_per_hour * (1 + median_time / 3600)
        return p.price_per_hour

    return sorted(prices, key=score)


# ── destroy ──────────────────────────────────────────────────


def destroy(run_id: str, config: Config | None = None) -> None:
    if config is None:
        config = load_config()
    result = load_state(run_id)
    provider = get_provider(result.provider, config)
    logger.info("Destroying fleet %s...", run_id)
    provider.teardown(result)
    logger.info("Fleet %s destroyed", run_id)


def destroy_all(config: Config | None = None) -> None:
    for run_id in list_runs():
        try:
            destroy(run_id, config)
        except Exception as e:
            logger.error("Failed to destroy %s: %s", run_id, e)


# ── state management ─────────────────────────────────────────


def list_runs() -> list[str]:
    if not STATE_DIR.exists():
        return []
    return sorted(
        d.name
        for d in STATE_DIR.iterdir()
        if d.is_dir() and (d / "state.json").exists()
    )


def save_state(result: FleetResult) -> None:
    run_dir = STATE_DIR / result.run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = {
        "provider": result.provider,
        "run_id": result.run_id,
        "region": result.region,
        "instances": [
            {
                "id": i.id,
                "instance_type": i.instance_type,
                "zone": i.zone,
                "public_ip": i.public_ip,
                "private_ip": i.private_ip,
            }
            for i in result.instances
        ],
        "errors": result.errors,
        "teardown_handles": result.teardown_handles,
    }
    with open(run_dir / "state.json", "w") as f:
        json.dump(state, f, indent=2)

    key_path = run_dir / "private_key.pem"
    with open(key_path, "w") as f:
        f.write(result.private_key_pem)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def load_state(run_id: str) -> FleetResult:
    run_dir = STATE_DIR / run_id
    state_file = run_dir / "state.json"
    if not state_file.exists():
        raise FileNotFoundError(f"No state for run {run_id}")

    with open(state_file) as f:
        state = json.load(f)

    key_path = run_dir / "private_key.pem"
    private_key_pem = key_path.read_text() if key_path.exists() else ""

    return FleetResult(
        provider=state["provider"],
        run_id=state["run_id"],
        region=state["region"],
        instances=[
            Instance(
                id=i["id"],
                instance_type=i["instance_type"],
                zone=i["zone"],
                public_ip=i.get("public_ip"),
                private_ip=i.get("private_ip", ""),
            )
            for i in state["instances"]
        ],
        errors=state.get("errors", []),
        private_key_pem=private_key_pem,
        teardown_handles=state.get("teardown_handles", {}),
    )
