"""GCP provider — Spot VMs via bulkInsert."""

from __future__ import annotations

import asyncio
import logging
import time

from google.cloud import compute_v1

from ephemeral_forge.config import GCPConfig
from ephemeral_forge.provider import (
    FleetConfig,
    FleetResult,
    Instance,
    ProviderBase,
    SpotPrice,
)
from ephemeral_forge.ssh import generate_ssh_keypair

logger = logging.getLogger(__name__)

PERSISTENT_FW_NAME = "ephemeral-forge-allow-ssh"
PERSISTENT_FW_INTERNAL = "ephemeral-forge-allow-internal"
INSTANCE_TAG = "ephemeral-forge"

# Approximate spot prices ($/hr) for common machine types.
# GCP spot prices are fixed per (type, zone) and don't fluctuate
# like AWS.  These are close enough for ranking; the real test
# is whether bulkInsert succeeds.
_APPROX_SPOT_PRICES: dict[str, float] = {
    "e2-standard-2": 0.013,
    "e2-standard-4": 0.027,
    "n2-standard-2": 0.016,
    "n2-standard-4": 0.032,
    "n1-standard-2": 0.014,
    "n1-standard-4": 0.028,
    "e2-micro": 0.002,
    "e2-small": 0.003,
    "e2-medium": 0.007,
    # GPU types (n1-standard-4 already listed above — used with T4)
    "g2-standard-4": 0.28,
    "g2-standard-8": 0.56,
    "a2-highgpu-1g": 1.10,
}


class GCPProvider(ProviderBase):
    def __init__(self, config: GCPConfig) -> None:
        self.config = config
        if not config.project_id:
            raise ValueError("GCP project_id is required in ephemeral-forge.toml")
        self._project = config.project_id

    @property
    def default_instance_types(self) -> list[str]:
        return [
            "e2-standard-2",
            "e2-standard-4",
            "n2-standard-2",
            "n2-standard-4",
        ]

    @property
    def default_gpu_instance_types(self) -> list[str]:
        return [
            "g2-standard-4",
            "g2-standard-8",
            "n1-standard-4",
            "a2-highgpu-1g",
        ]

    # ── probe_spot_prices ────────────────────────────────────

    def probe_spot_prices(
        self,
        instance_types: list[str],
        regions: list[str] | None = None,
    ) -> list[SpotPrice]:
        """Check which (machine_type, zone) combos are available.

        GCP spot prices are fixed, so we check availability via
        MachineTypesClient and use approximate known prices for
        ranking.
        """
        zones_client = compute_v1.ZonesClient()
        all_zones = list(zones_client.list(project=self._project))

        if regions:
            # Filter zones to requested regions
            all_zones = [
                z for z in all_zones if any(z.name.startswith(r) for r in regions)
            ]

        zone_names = [z.name for z in all_zones if z.status == "UP"]
        return asyncio.run(self._probe_all_zones(instance_types, zone_names))

    async def _probe_all_zones(
        self,
        instance_types: list[str],
        zones: list[str],
    ) -> list[SpotPrice]:
        async def probe_one(zone: str) -> list[SpotPrice]:
            return await asyncio.to_thread(self._probe_zone_sync, zone, instance_types)

        results = await asyncio.gather(
            *[probe_one(z) for z in zones],
            return_exceptions=True,
        )
        prices: list[SpotPrice] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("Zone probe failed: %s", result)
                continue
            prices.extend(result)
        return prices

    def _probe_zone_sync(
        self,
        zone: str,
        instance_types: list[str],
    ) -> list[SpotPrice]:
        mt_client = compute_v1.MachineTypesClient()
        prices: list[SpotPrice] = []
        region = zone.rsplit("-", 1)[0]  # us-central1-a → us-central1

        for itype in instance_types:
            try:
                mt_client.get(
                    project=self._project,
                    zone=zone,
                    machine_type=itype,
                )
                # Machine type exists in this zone
                price = _APPROX_SPOT_PRICES.get(itype, 0.05)
                if self.config.max_spot_price and price > self.config.max_spot_price:
                    continue
                prices.append(
                    SpotPrice(
                        region=region,
                        zone=zone,
                        instance_type=itype,
                        price_per_hour=price,
                    )
                )
            except Exception:
                # Machine type not available in this zone
                pass
        return prices

    # ── resolve_image ────────────────────────────────────────

    def resolve_image(self, region: str, gpu: bool = False) -> str:
        """Return the source image URL for Ubuntu."""
        images_client = compute_v1.ImagesClient()
        if gpu:
            # Deep Learning VM image
            image = images_client.get_from_family(
                project="deeplearning-platform-release",
                family="pytorch-latest-gpu-ubuntu-2204-py310",
            )
        else:
            image = images_client.get_from_family(
                project="ubuntu-os-cloud",
                family="ubuntu-2404-lts-amd64",
            )
        return image.self_link

    # ── persistent infrastructure ────────────────────────────

    def _ensure_firewall(self, network_url: str) -> None:
        """Create persistent firewall rules if they don't exist."""
        fw_client = compute_v1.FirewallsClient()

        existing = set()
        for fw in fw_client.list(project=self._project):
            existing.add(fw.name)

        if PERSISTENT_FW_NAME not in existing:
            logger.info("Creating firewall rule %s", PERSISTENT_FW_NAME)
            fw_client.insert(
                project=self._project,
                firewall_resource=compute_v1.Firewall(
                    name=PERSISTENT_FW_NAME,
                    network=network_url,
                    direction="INGRESS",
                    priority=1000,
                    target_tags=[INSTANCE_TAG],
                    source_ranges=["0.0.0.0/0"],
                    allowed=[
                        compute_v1.Allowed(I_p_protocol="tcp", ports=["22"]),
                    ],
                    description="ephemeral-forge: SSH access",
                ),
            ).result()

        if PERSISTENT_FW_INTERNAL not in existing:
            logger.info("Creating firewall rule %s", PERSISTENT_FW_INTERNAL)
            fw_client.insert(
                project=self._project,
                firewall_resource=compute_v1.Firewall(
                    name=PERSISTENT_FW_INTERNAL,
                    network=network_url,
                    direction="INGRESS",
                    priority=1000,
                    target_tags=[INSTANCE_TAG],
                    source_tags=[INSTANCE_TAG],
                    allowed=[
                        compute_v1.Allowed(I_p_protocol="all"),
                    ],
                    description="ephemeral-forge: internal traffic",
                ),
            ).result()

    def _ensure_network(self) -> str:
        """Ensure the default network exists.  Returns network URL."""
        net_client = compute_v1.NetworksClient()
        network_url = f"projects/{self._project}/global/networks/default"

        try:
            net_client.get(project=self._project, network="default")
            return network_url
        except Exception:
            pass

        # Try ephemeral-forge network
        ef_network_url = f"projects/{self._project}/global/networks/ephemeral-forge"
        try:
            net_client.get(project=self._project, network="ephemeral-forge")
            logger.info("Reusing ephemeral-forge network")
            return ef_network_url
        except Exception:
            pass

        # Create auto-mode network (auto-creates subnets in all regions)
        logger.info("Creating ephemeral-forge network")
        net_client.insert(
            project=self._project,
            network_resource=compute_v1.Network(
                name="ephemeral-forge",
                auto_create_subnetworks=True,
                description="ephemeral-forge fleet network",
            ),
        ).result()
        return ef_network_url

    # ── launch_fleet ─────────────────────────────────────────

    def launch_fleet(
        self,
        config: FleetConfig,
        run_id: str,
        region: str,
        zone: str | None = None,
    ) -> FleetResult:
        if not zone:
            # Pick first available zone in the region for the
            # cheapest instance type
            prices = self.probe_spot_prices(
                config.instance_types, regions=[region]
            )
            if not prices:
                raise RuntimeError(
                    f"No spot availability in {region}"
                )
            prices.sort(key=lambda p: p.price_per_hour)
            zone = prices[0].zone

        # SSH key
        private_pem, public_openssh = generate_ssh_keypair()

        # Persistent infra
        network_url = self._ensure_network()
        self._ensure_firewall(network_url)

        # Image
        source_image = config.image or self.resolve_image(region)

        # GCP Ubuntu image requires >= 10 GB disk
        disk_gb = max(config.disk_gb, 10)

        # Try each instance type until one succeeds (bulkInsert
        # accepts only a single machine type per call)
        instances_client = compute_v1.InstancesClient()
        last_error: Exception | None = None

        for itype in config.instance_types:
            logger.info(
                "Trying bulkInsert: %d × %s in %s",
                config.count,
                itype,
                zone,
            )
            try:
                op = instances_client.bulk_insert(
                    project=self._project,
                    zone=zone,
                    bulk_insert_instance_resource_resource=compute_v1.BulkInsertInstanceResource(
                        count=config.count,
                        min_count=1,
                        name_pattern=f"ef-{run_id}-####",
                        instance_properties=compute_v1.InstanceProperties(
                            machine_type=itype,
                            disks=[
                                compute_v1.AttachedDisk(
                                    boot=True,
                                    auto_delete=True,
                                    type_="PERSISTENT",
                                    initialize_params=compute_v1.AttachedDiskInitializeParams(
                                        source_image=source_image,
                                        disk_size_gb=disk_gb,
                                        disk_type="pd-balanced",
                                    ),
                                ),
                            ],
                            network_interfaces=[
                                compute_v1.NetworkInterface(
                                    network=network_url,
                                    access_configs=[
                                        compute_v1.AccessConfig(
                                            name="External NAT",
                                            type_="ONE_TO_ONE_NAT",
                                        ),
                                    ],
                                ),
                            ],
                            metadata=compute_v1.Metadata(
                                items=[
                                    compute_v1.Items(
                                        key="ssh-keys",
                                        value=f"{config.ssh_user}:{public_openssh}",
                                    ),
                                ]
                            ),
                            scheduling=compute_v1.Scheduling(
                                provisioning_model="SPOT",
                                instance_termination_action="DELETE",
                                on_host_maintenance="TERMINATE",
                            ),
                            labels={
                                "purpose": config.tags.get(
                                    "Purpose", "ephemeral-forge"
                                ).lower(),
                                "run-id": run_id,
                            },
                            tags=compute_v1.Tags(
                                items=[INSTANCE_TAG],
                            ),
                        ),
                    ),
                )
                op.result()  # Wait for bulkInsert to complete
                logger.info("bulkInsert succeeded with %s", itype)
                break
            except Exception as e:
                last_error = e
                logger.warning(
                    "bulkInsert failed for %s in %s: %s",
                    itype,
                    zone,
                    e,
                )
                continue
        else:
            raise RuntimeError(
                f"All instance types exhausted. Last error: {last_error}"
            )

        # List instances we just created
        instance_ids = self._list_run_instances(zone, run_id)
        if not instance_ids:
            raise RuntimeError("bulkInsert reported success but no instances found")

        logger.info("Created %d instances", len(instance_ids))
        return FleetResult(
            provider="gcp",
            run_id=run_id,
            region=region,
            instances=[
                Instance(id=iid, instance_type="", zone=zone) for iid in instance_ids
            ],
            errors=[],
            private_key_pem=private_pem,
            teardown_handles={
                "zone": zone,
                "instance_names": instance_ids,
                "project": self._project,
            },
        )

    def _list_run_instances(self, zone: str, run_id: str) -> list[str]:
        """List instance names matching a run ID."""
        client = compute_v1.InstancesClient()
        request = compute_v1.ListInstancesRequest(
            project=self._project,
            zone=zone,
            filter=f'labels.run-id="{run_id}"',
        )
        return [inst.name for inst in client.list(request=request)]

    # ── wait_until_ready ─────────────────────────────────────

    def wait_until_ready(
        self,
        result: FleetResult,
        timeout: int = 300,
    ) -> FleetResult:
        client = compute_v1.InstancesClient()
        zone = result.teardown_handles["zone"]
        instance_names = result.teardown_handles["instance_names"]

        deadline = time.monotonic() + timeout
        ready: dict[str, Instance] = {}

        while time.monotonic() < deadline:
            all_running = True
            for name in instance_names:
                if name in ready:
                    continue
                try:
                    inst = client.get(
                        project=self._project,
                        zone=zone,
                        instance=name,
                    )
                    if inst.status == "RUNNING":
                        public_ip = None
                        private_ip = ""
                        for nic in inst.network_interfaces:
                            private_ip = nic.network_i_p or ""
                            for ac in nic.access_configs:
                                if ac.nat_i_p:
                                    public_ip = ac.nat_i_p
                        ready[name] = Instance(
                            id=name,
                            instance_type=inst.machine_type.rsplit("/", 1)[-1],
                            zone=zone,
                            public_ip=public_ip,
                            private_ip=private_ip,
                        )
                    else:
                        all_running = False
                except Exception:
                    all_running = False

            if all_running and len(ready) == len(instance_names):
                break
            time.sleep(5)

        result.instances = list(ready.values())
        return result

    # ── teardown ─────────────────────────────────────────────

    def teardown(self, result: FleetResult) -> None:
        handles = result.teardown_handles
        zone = handles["zone"]
        instance_names = handles.get("instance_names", [])
        project = handles.get("project", self._project)

        client = compute_v1.InstancesClient()
        ops = []
        for name in instance_names:
            logger.info("Deleting instance %s", name)
            try:
                op = client.delete(project=project, zone=zone, instance=name)
                ops.append(op)
            except Exception as e:
                logger.warning("Failed to delete instance %s: %s", name, e)

        # Wait for all deletions
        for op in ops:
            try:
                op.result()
            except Exception as e:
                logger.warning("Delete operation error: %s", e)

        logger.info("Teardown complete for %s", result.run_id)
