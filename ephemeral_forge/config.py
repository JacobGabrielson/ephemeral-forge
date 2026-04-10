"""Load configuration from ephemeral-forge.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_DEFAULT_AWS_TYPES = [
    "t3.small",
    "t3a.small",
    "t3.micro",
    "t3a.micro",
    "m6i.large",
    "m5.large",
    "c6i.large",
    "c5.large",
]


@dataclass
class ProviderConfig:
    instance_types: list[str] = field(default_factory=list)
    gpu_instance_types: list[str] = field(default_factory=list)
    ssh_user: str = "ubuntu"
    candidate_regions: list[str] | None = None
    max_spot_price: float | None = None


@dataclass
class AWSConfig(ProviderConfig):
    profile: str = "default"


@dataclass
class GCPConfig(ProviderConfig):
    project_id: str = ""


@dataclass
class AzureConfig(ProviderConfig):
    subscription_id: str = ""
    ssh_user: str = "azureuser"


@dataclass
class Config:
    aws: AWSConfig | None = None
    gcp: GCPConfig | None = None
    azure: AzureConfig | None = None
    purpose_tag: str = "ephemeral-forge"
    probe_all_regions: bool = True


_SEARCH_PATHS = [
    Path("ephemeral-forge.toml"),
    Path.home() / ".config" / "ephemeral-forge.toml",
]


def load_config(path: Path | None = None) -> Config:
    """Load config from TOML file.  Falls back to defaults if
    no file is found."""
    if path is not None:
        paths = [path]
    else:
        paths = _SEARCH_PATHS

    for p in paths:
        if p.exists():
            with open(p, "rb") as f:
                data = tomllib.load(f)
            return _parse_config(data)

    return Config(aws=AWSConfig(instance_types=_DEFAULT_AWS_TYPES))


def _parse_config(data: dict[str, object]) -> Config:
    config = Config()

    if "aws" in data:
        raw = data["aws"]
        assert isinstance(raw, dict)
        config.aws = AWSConfig(
            profile=raw.get("profile", "default"),
            instance_types=raw.get("default_instance_types", _DEFAULT_AWS_TYPES),
            gpu_instance_types=raw.get("gpu_instance_types", []),
            ssh_user=raw.get("ssh_user", "ubuntu"),
            candidate_regions=raw.get("candidate_regions"),
            max_spot_price=raw.get("max_spot_price"),
        )

    if "gcp" in data:
        raw = data["gcp"]
        assert isinstance(raw, dict)
        config.gcp = GCPConfig(
            project_id=raw.get("project_id", ""),
            instance_types=raw.get("default_instance_types", []),
            gpu_instance_types=raw.get("gpu_instance_types", []),
            ssh_user=raw.get("ssh_user", "ubuntu"),
            candidate_regions=raw.get("candidate_regions"),
            max_spot_price=raw.get("max_spot_price"),
        )

    if "azure" in data:
        raw = data["azure"]
        assert isinstance(raw, dict)
        config.azure = AzureConfig(
            subscription_id=raw.get("subscription_id", ""),
            instance_types=raw.get("default_instance_types", []),
            gpu_instance_types=raw.get("gpu_instance_types", []),
            ssh_user=raw.get("ssh_user", "azureuser"),
            candidate_regions=raw.get("candidate_regions"),
            max_spot_price=raw.get("max_spot_price"),
        )

    if "general" in data:
        raw = data["general"]
        assert isinstance(raw, dict)
        config.purpose_tag = raw.get("purpose_tag", "ephemeral-forge")
        config.probe_all_regions = raw.get("probe_all_regions", True)

    return config
