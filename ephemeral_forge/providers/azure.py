"""Azure provider — stub for future implementation."""

from __future__ import annotations

from ephemeral_forge.config import AzureConfig
from ephemeral_forge.provider import (
    FleetConfig,
    FleetResult,
    ProviderBase,
    SpotPrice,
)


class AzureProvider(ProviderBase):
    def __init__(self, config: AzureConfig) -> None:
        self.config = config

    @property
    def default_instance_types(self) -> list[str]:
        return ["Standard_D2s_v5", "Standard_D4s_v5", "Standard_B2ms"]

    @property
    def default_gpu_instance_types(self) -> list[str]:
        return ["Standard_NC4as_T4_v3", "Standard_NC8as_T4_v3"]

    def probe_spot_prices(
        self,
        instance_types: list[str],
        regions: list[str] | None = None,
    ) -> list[SpotPrice]:
        raise NotImplementedError("Azure provider not yet implemented")

    def resolve_image(self, region: str, gpu: bool = False) -> str:
        raise NotImplementedError("Azure provider not yet implemented")

    def launch_fleet(
        self,
        config: FleetConfig,
        run_id: str,
        region: str,
        zone: str | None = None,
    ) -> FleetResult:
        raise NotImplementedError("Azure provider not yet implemented")

    def wait_until_ready(
        self,
        result: FleetResult,
        timeout: int = 300,
    ) -> FleetResult:
        raise NotImplementedError("Azure provider not yet implemented")

    def teardown(self, result: FleetResult) -> None:
        raise NotImplementedError("Azure provider not yet implemented")
