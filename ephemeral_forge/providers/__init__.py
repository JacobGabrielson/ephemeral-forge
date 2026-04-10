"""Provider factory."""

from __future__ import annotations

from ephemeral_forge.config import Config
from ephemeral_forge.provider import ProviderBase


def get_provider(name: str, config: Config) -> ProviderBase:
    """Instantiate a cloud provider by name."""
    if name == "aws":
        from ephemeral_forge.providers.aws import AWSProvider

        if config.aws is None:
            raise ValueError("AWS not configured in ephemeral-forge.toml")
        return AWSProvider(config.aws)
    elif name == "gcp":
        from ephemeral_forge.providers.gcp import GCPProvider

        if config.gcp is None:
            raise ValueError("GCP not configured in ephemeral-forge.toml")
        return GCPProvider(config.gcp)
    elif name == "azure":
        from ephemeral_forge.providers.azure import AzureProvider

        if config.azure is None:
            raise ValueError("Azure not configured in ephemeral-forge.toml")
        return AzureProvider(config.azure)
    else:
        raise ValueError(f"Unknown provider: {name}")
