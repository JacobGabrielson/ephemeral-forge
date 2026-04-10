"""Provider abstraction: base class and shared data types."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FleetConfig:
    """What the user wants to launch."""

    count: int
    instance_types: list[str]
    image: str | None = None
    disk_gb: int = 8
    ssh_user: str = "ubuntu"
    tags: dict[str, str] = field(default_factory=dict)
    max_spot_price: float | None = None


@dataclass
class Instance:
    """A running cloud instance."""

    id: str
    instance_type: str
    zone: str
    public_ip: str | None = None
    private_ip: str = ""


@dataclass
class FleetResult:
    """Returned by launch_fleet — carries everything needed for
    wait, SSH, and teardown."""

    provider: str
    run_id: str
    region: str
    instances: list[Instance] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    private_key_pem: str = ""
    teardown_handles: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SpotPrice:
    """A single spot price observation."""

    region: str
    zone: str
    instance_type: str
    price_per_hour: float


class ProviderBase(abc.ABC):
    """Abstract base for all cloud providers."""

    @abc.abstractmethod
    def probe_spot_prices(
        self,
        instance_types: list[str],
        regions: list[str] | None = None,
    ) -> list[SpotPrice]:
        """Find spot prices. regions=None means probe all."""
        ...

    @abc.abstractmethod
    def resolve_image(self, region: str, gpu: bool = False) -> str:
        """Return the image ID for the given region."""
        ...

    @abc.abstractmethod
    def launch_fleet(
        self,
        config: FleetConfig,
        run_id: str,
        region: str,
        zone: str | None = None,
    ) -> FleetResult:
        """Create prerequisites and launch instances.  Returns
        immediately — call wait_until_ready to block."""
        ...

    @abc.abstractmethod
    def wait_until_ready(
        self,
        result: FleetResult,
        timeout: int = 300,
    ) -> FleetResult:
        """Block until instances are running with IPs."""
        ...

    @abc.abstractmethod
    def teardown(self, result: FleetResult) -> None:
        """Destroy all per-run resources."""
        ...
