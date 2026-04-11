"""GCP provider — stub for future implementation."""

from __future__ import annotations

from ephemeral_forge.config import GCPConfig
from ephemeral_forge.provider import (
    FleetConfig,
    FleetResult,
    ProviderBase,
    SpotPrice,
)


class GCPProvider(ProviderBase):
    def __init__(self, config: GCPConfig) -> None:
        self.config = config

    @property
    def default_instance_types(self) -> list[str]:
        return ["e2-standard-2", "e2-standard-4", "n2-standard-2", "n2-standard-4"]

    @property
    def default_gpu_instance_types(self) -> list[str]:
        return ["g2-standard-4", "g2-standard-8", "n1-standard-4", "a2-highgpu-1g"]

    def probe_spot_prices(
        self,
        instance_types: list[str],
        regions: list[str] | None = None,
    ) -> list[SpotPrice]:
        raise NotImplementedError("GCP provider not yet implemented")

    def resolve_image(self, region: str, gpu: bool = False) -> str:
        raise NotImplementedError("GCP provider not yet implemented")

    def launch_fleet(
        self,
        config: FleetConfig,
        run_id: str,
        region: str,
        zone: str | None = None,
    ) -> FleetResult:
        raise NotImplementedError("GCP provider not yet implemented")

    def wait_until_ready(
        self,
        result: FleetResult,
        timeout: int = 300,
    ) -> FleetResult:
        raise NotImplementedError("GCP provider not yet implemented")

    def teardown(self, result: FleetResult) -> None:
        raise NotImplementedError("GCP provider not yet implemented")
